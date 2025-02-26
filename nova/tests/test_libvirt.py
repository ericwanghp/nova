# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
#    Copyright 2010 OpenStack Foundation
#    Copyright 2012 University Of Minho
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import copy
import errno
import eventlet
import fixtures
import json
import mox
import os
import re
import shutil
import tempfile

from lxml import etree
from oslo.config import cfg
from xml.dom import minidom

from nova.api.ec2 import cloud
from nova.compute import instance_types
from nova.compute import power_state
from nova.compute import task_states
from nova.compute import vm_mode
from nova.compute import vm_states
from nova import context
from nova import db
from nova import exception
from nova.openstack.common import fileutils
from nova.openstack.common import importutils
from nova.openstack.common import jsonutils
from nova.openstack.common import log as logging
from nova.openstack.common import uuidutils
from nova import test
from nova.tests import fake_libvirt_utils
from nova.tests import fake_network
import nova.tests.image.fake
from nova.tests import matchers
from nova import utils
from nova import version
from nova.virt.disk import api as disk
from nova.virt import driver
from nova.virt import event as virtevent
from nova.virt import fake
from nova.virt import firewall as base_firewall
from nova.virt import images
from nova.virt.libvirt import blockinfo
from nova.virt.libvirt import config as vconfig
from nova.virt.libvirt import driver as libvirt_driver
from nova.virt.libvirt import firewall
from nova.virt.libvirt import imagebackend
from nova.virt.libvirt import utils as libvirt_utils
from nova.virt import netutils

try:
    import libvirt
except ImportError:
    import nova.tests.fakelibvirt as libvirt
libvirt_driver.libvirt = libvirt


CONF = cfg.CONF
CONF.import_opt('compute_manager', 'nova.service')
CONF.import_opt('host', 'nova.netconf')
CONF.import_opt('my_ip', 'nova.netconf')
CONF.import_opt('base_dir_name', 'nova.virt.libvirt.imagecache')
LOG = logging.getLogger(__name__)

_fake_network_info = fake_network.fake_get_instance_nw_info
_fake_stub_out_get_nw_info = fake_network.stub_out_nw_api_get_instance_nw_info
_ipv4_like = fake_network.ipv4_like


def _concurrency(signal, wait, done, target):
    signal.send()
    wait.wait()
    done.send()


class FakeVirDomainSnapshot(object):

    def __init__(self, dom=None):
        self.dom = dom

    def delete(self, flags):
        pass


class FakeVirtDomain(object):

    def __init__(self, fake_xml=None, uuidstr=None):
        self.uuidstr = uuidstr
        if fake_xml:
            self._fake_dom_xml = fake_xml
        else:
            self._fake_dom_xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                        </disk>
                    </devices>
                </domain>
            """

    def name(self):
        return "fake-domain %s" % self

    def info(self):
        return [power_state.RUNNING, None, None, None, None]

    def create(self):
        pass

    def managedSave(self, *args):
        pass

    def createWithFlags(self, launch_flags):
        pass

    def XMLDesc(self, *args):
        return self._fake_dom_xml

    def UUIDString(self):
        return self.uuidstr


class CacheConcurrencyTestCase(test.TestCase):
    def setUp(self):
        super(CacheConcurrencyTestCase, self).setUp()

        self.flags(instances_path=self.useFixture(fixtures.TempDir()).path)

        # utils.synchronized() will create the lock_path for us if it
        # doesn't already exist. It will also delete it when it's done,
        # which can cause race conditions with the multiple threads we
        # use for tests. So, create the path here so utils.synchronized()
        # won't delete it out from under one of the threads.
        self.lock_path = os.path.join(CONF.instances_path, 'locks')
        fileutils.ensure_tree(self.lock_path)

        def fake_exists(fname):
            basedir = os.path.join(CONF.instances_path, CONF.base_dir_name)
            if fname == basedir or fname == self.lock_path:
                return True
            return False

        def fake_execute(*args, **kwargs):
            pass

        def fake_extend(image, size):
            pass

        self.stubs.Set(os.path, 'exists', fake_exists)
        self.stubs.Set(utils, 'execute', fake_execute)
        self.stubs.Set(imagebackend.disk, 'extend', fake_extend)
        self.useFixture(fixtures.MonkeyPatch(
            'nova.virt.libvirt.imagebackend.libvirt_utils',
            fake_libvirt_utils))

    def tearDown(self):
        super(CacheConcurrencyTestCase, self).tearDown()

    def test_same_fname_concurrency(self):
        # Ensures that the same fname cache runs at a sequentially.
        uuid = uuidutils.generate_uuid()

        backend = imagebackend.Backend(False)
        wait1 = eventlet.event.Event()
        done1 = eventlet.event.Event()
        sig1 = eventlet.event.Event()
        thr1 = eventlet.spawn(backend.image({'name': 'instance',
                                             'uuid': uuid},
                                            'name').cache,
                _concurrency, 'fname', None,
                signal=sig1, wait=wait1, done=done1)
        eventlet.sleep(0)
        # Thread 1 should run before thread 2.
        sig1.wait()

        wait2 = eventlet.event.Event()
        done2 = eventlet.event.Event()
        sig2 = eventlet.event.Event()
        thr2 = eventlet.spawn(backend.image({'name': 'instance',
                                             'uuid': uuid},
                                            'name').cache,
                _concurrency, 'fname', None,
                signal=sig2, wait=wait2, done=done2)

        wait2.send()
        eventlet.sleep(0)
        try:
            self.assertFalse(done2.ready())
        finally:
            wait1.send()
        done1.wait()
        eventlet.sleep(0)
        self.assertTrue(done2.ready())
        # Wait on greenthreads to assert they didn't raise exceptions
        # during execution
        thr1.wait()
        thr2.wait()

    def test_different_fname_concurrency(self):
        # Ensures that two different fname caches are concurrent.
        uuid = uuidutils.generate_uuid()

        backend = imagebackend.Backend(False)
        wait1 = eventlet.event.Event()
        done1 = eventlet.event.Event()
        sig1 = eventlet.event.Event()
        thr1 = eventlet.spawn(backend.image({'name': 'instance',
                                             'uuid': uuid},
                                            'name').cache,
                _concurrency, 'fname2', None,
                signal=sig1, wait=wait1, done=done1)
        eventlet.sleep(0)
        # Thread 1 should run before thread 2.
        sig1.wait()

        wait2 = eventlet.event.Event()
        done2 = eventlet.event.Event()
        sig2 = eventlet.event.Event()
        thr2 = eventlet.spawn(backend.image({'name': 'instance',
                                             'uuid': uuid},
                                            'name').cache,
                _concurrency, 'fname1', None,
                signal=sig2, wait=wait2, done=done2)
        eventlet.sleep(0)
        # Wait for thread 2 to start.
        sig2.wait()

        wait2.send()
        eventlet.sleep(0)
        try:
            self.assertTrue(done2.ready())
        finally:
            wait1.send()
            eventlet.sleep(0)
        # Wait on greenthreads to assert they didn't raise exceptions
        # during execution
        thr1.wait()
        thr2.wait()


class FakeVolumeDriver(object):
    def __init__(self, *args, **kwargs):
        pass

    def attach_volume(self, *args):
        pass

    def detach_volume(self, *args):
        pass

    def get_xml(self, *args):
        return ""


class FakeConfigGuestDisk(object):
    def __init__(self, *args, **kwargs):
        self.source_type = None
        self.driver_cache = None


class FakeConfigGuest(object):
    def __init__(self, *args, **kwargs):
        self.driver_cache = None


class LibvirtConnTestCase(test.TestCase):

    def setUp(self):
        super(LibvirtConnTestCase, self).setUp()
        self.flags(fake_call=True)
        self.user_id = 'fake'
        self.project_id = 'fake'
        self.context = context.get_admin_context()
        self.flags(instances_path='')
        self.flags(libvirt_snapshots_directory='')
        self.useFixture(fixtures.MonkeyPatch(
            'nova.virt.libvirt.driver.libvirt_utils',
            fake_libvirt_utils))
        self.useFixture(fixtures.MonkeyPatch(
            'nova.virt.libvirt.imagebackend.libvirt_utils',
            fake_libvirt_utils))

        def fake_extend(image, size):
            pass

        self.stubs.Set(libvirt_driver.disk, 'extend', fake_extend)

        nova.tests.image.fake.stub_out_image_service(self.stubs)
        self.test_instance = {
                'uuid': '32dfcb37-5af1-552b-357c-be8c3aa38310',
                'memory_kb': '1024000',
                'basepath': '/some/path',
                'bridge_name': 'br100',
                'vcpus': 2,
                'project_id': 'fake',
                'bridge': 'br101',
                'image_ref': '155d900f-4e14-4e4c-a73d-069cbf4541e6',
                'root_gb': 10,
                'ephemeral_gb': 20,
                'instance_type_id': '5',  # m1.small
                'extra_specs': {}}

    def tearDown(self):
        nova.tests.image.fake.FakeImageService_reset()
        super(LibvirtConnTestCase, self).tearDown()

    def create_fake_libvirt_mock(self, **kwargs):
        """Defining mocks for LibvirtDriver(libvirt is not used)."""

        # A fake libvirt.virConnect
        class FakeLibvirtDriver(object):
            def defineXML(self, xml):
                return FakeVirtDomain()

        # Creating mocks
        volume_driver = 'iscsi=nova.tests.test_libvirt.FakeVolumeDriver'
        self.flags(libvirt_volume_drivers=[volume_driver])
        fake = FakeLibvirtDriver()
        # Customizing above fake if necessary
        for key, val in kwargs.items():
            fake.__setattr__(key, val)

        self.flags(libvirt_vif_driver="nova.tests.fake_network.FakeVIFDriver")

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn = fake

    def fake_lookup(self, instance_name):
        return FakeVirtDomain()

    def fake_execute(self, *args, **kwargs):
        open(args[-1], "a").close()

    def create_service(self, **kwargs):
        service_ref = {'host': kwargs.get('host', 'dummy'),
                       'binary': 'nova-compute',
                       'topic': 'compute',
                       'report_count': 0}

        return db.service_create(context.get_admin_context(), service_ref)

    def test_get_connector(self):
        initiator = 'fake.initiator.iqn'
        ip = 'fakeip'
        host = 'fakehost'
        wwpns = ['100010604b019419']
        wwnns = ['200010604b019419']
        self.flags(my_ip=ip)
        self.flags(host=host)

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        expected = {
            'ip': ip,
            'initiator': initiator,
            'host': host,
            'wwpns': wwpns,
            'wwnns': wwnns
        }
        volume = {
            'id': 'fake'
        }
        result = conn.get_volume_connector(volume)
        self.assertThat(expected, matchers.DictMatches(result))

    def test_get_guest_config(self):
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        cfg = conn.get_guest_config(instance_ref,
                                    _fake_network_info(self.stubs, 1),
                                    None, disk_info)
        self.assertEquals(cfg.acpi, True)
        self.assertEquals(cfg.apic, True)
        self.assertEquals(cfg.memory, 1024 * 1024 * 2)
        self.assertEquals(cfg.vcpus, 1)
        self.assertEquals(cfg.os_type, vm_mode.HVM)
        self.assertEquals(cfg.os_boot_dev, "hd")
        self.assertEquals(cfg.os_root, None)
        self.assertEquals(len(cfg.devices), 7)
        self.assertEquals(type(cfg.devices[0]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[1]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[2]),
                          vconfig.LibvirtConfigGuestInterface)
        self.assertEquals(type(cfg.devices[3]),
                          vconfig.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[4]),
                          vconfig.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[5]),
                          vconfig.LibvirtConfigGuestInput)
        self.assertEquals(type(cfg.devices[6]),
                          vconfig.LibvirtConfigGuestGraphics)

        self.assertEquals(type(cfg.clock),
                          vconfig.LibvirtConfigGuestClock)
        self.assertEquals(cfg.clock.offset, "utc")
        self.assertEquals(len(cfg.clock.timers), 2)
        self.assertEquals(type(cfg.clock.timers[0]),
                          vconfig.LibvirtConfigGuestTimer)
        self.assertEquals(type(cfg.clock.timers[1]),
                          vconfig.LibvirtConfigGuestTimer)
        self.assertEquals(cfg.clock.timers[0].name, "pit")
        self.assertEquals(cfg.clock.timers[0].tickpolicy,
                          "delay")
        self.assertEquals(cfg.clock.timers[1].name, "rtc")
        self.assertEquals(cfg.clock.timers[1].tickpolicy,
                          "catchup")

    def test_get_guest_config_with_two_nics(self):
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        cfg = conn.get_guest_config(instance_ref,
                                    _fake_network_info(self.stubs, 2),
                                    None, disk_info)
        self.assertEquals(cfg.acpi, True)
        self.assertEquals(cfg.memory, 1024 * 1024 * 2)
        self.assertEquals(cfg.vcpus, 1)
        self.assertEquals(cfg.os_type, vm_mode.HVM)
        self.assertEquals(cfg.os_boot_dev, "hd")
        self.assertEquals(cfg.os_root, None)
        self.assertEquals(len(cfg.devices), 8)
        self.assertEquals(type(cfg.devices[0]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[1]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[2]),
                          vconfig.LibvirtConfigGuestInterface)
        self.assertEquals(type(cfg.devices[3]),
                          vconfig.LibvirtConfigGuestInterface)
        self.assertEquals(type(cfg.devices[4]),
                          vconfig.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[5]),
                          vconfig.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[6]),
                          vconfig.LibvirtConfigGuestInput)
        self.assertEquals(type(cfg.devices[7]),
                          vconfig.LibvirtConfigGuestGraphics)

    def test_get_guest_config_bug_1118829(self):
        self.flags(libvirt_type='uml')
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        disk_info = {'disk_bus': 'virtio',
                     'cdrom_bus': 'ide',
                     'mapping': {u'vda': {'bus': 'virtio',
                                          'type': 'disk',
                                          'dev': u'vda'},
                                 'root': {'bus': 'virtio',
                                          'type': 'disk',
                                          'dev': 'vda'}}}

        # NOTE(jdg): For this specific test leave this blank
        # This will exercise the failed code path still,
        # and won't require fakes and stubs of the iscsi discovery
        block_device_info = {}
        cfg = conn.get_guest_config(instance_ref, [], None, disk_info,
                                    None, block_device_info)
        instance_ref = db.instance_get(self.context, instance_ref['id'])
        self.assertEquals(instance_ref['root_device_name'], '/dev/vda')

    def test_get_guest_config_with_root_device_name(self):
        self.flags(libvirt_type='uml')
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        block_device_info = {'root_device_name': '/dev/vdb'}
        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref,
                                            block_device_info)
        cfg = conn.get_guest_config(instance_ref, [], None, disk_info,
                                    None, block_device_info)
        self.assertEquals(cfg.acpi, False)
        self.assertEquals(cfg.memory, 1024 * 1024 * 2)
        self.assertEquals(cfg.vcpus, 1)
        self.assertEquals(cfg.os_type, "uml")
        self.assertEquals(cfg.os_boot_dev, None)
        self.assertEquals(cfg.os_root, '/dev/vdb')
        self.assertEquals(len(cfg.devices), 3)
        self.assertEquals(type(cfg.devices[0]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[1]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[2]),
                          vconfig.LibvirtConfigGuestConsole)

    def test_get_guest_config_with_block_device(self):
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        instance_ref = db.instance_create(self.context, self.test_instance)
        conn_info = {'driver_volume_type': 'fake'}
        info = {'block_device_mapping': [
                  {'connection_info': conn_info, 'mount_device': '/dev/vdc'},
                  {'connection_info': conn_info, 'mount_device': '/dev/vdd'}]}

        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref, info)
        cfg = conn.get_guest_config(instance_ref, [], None, disk_info,
                                    None, info)
        self.assertEquals(type(cfg.devices[2]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(cfg.devices[2].target_dev, 'vdc')
        self.assertEquals(type(cfg.devices[3]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(cfg.devices[3].target_dev, 'vdd')

    def test_get_guest_config_with_configdrive(self):
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        # make configdrive.enabled_for() return True
        instance_ref['config_drive'] = 'ANY_ID'

        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        cfg = conn.get_guest_config(instance_ref, [], None, disk_info)

        self.assertEquals(type(cfg.devices[2]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(cfg.devices[2].target_dev, 'vdz')

    def test_get_guest_config_with_vnc(self):
        self.flags(libvirt_type='kvm',
                   vnc_enabled=True,
                   use_usb_tablet=False)
        self.flags(enabled=False, group='spice')

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        cfg = conn.get_guest_config(instance_ref, [], None, disk_info)
        self.assertEquals(len(cfg.devices), 5)
        self.assertEquals(type(cfg.devices[0]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[1]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[2]),
                          vconfig.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[3]),
                          vconfig.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[4]),
                          vconfig.LibvirtConfigGuestGraphics)

        self.assertEquals(cfg.devices[4].type, "vnc")

    def test_get_guest_config_with_vnc_and_tablet(self):
        self.flags(libvirt_type='kvm',
                   vnc_enabled=True,
                   use_usb_tablet=True)
        self.flags(enabled=False, group='spice')

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        cfg = conn.get_guest_config(instance_ref, [], None, disk_info)
        self.assertEquals(len(cfg.devices), 6)
        self.assertEquals(type(cfg.devices[0]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[1]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[2]),
                          vconfig.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[3]),
                          vconfig.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[4]),
                          vconfig.LibvirtConfigGuestInput)
        self.assertEquals(type(cfg.devices[5]),
                          vconfig.LibvirtConfigGuestGraphics)

        self.assertEquals(cfg.devices[4].type, "tablet")
        self.assertEquals(cfg.devices[5].type, "vnc")

    def test_get_guest_config_with_spice_and_tablet(self):
        self.flags(libvirt_type='kvm',
                   vnc_enabled=False,
                   use_usb_tablet=True)
        self.flags(enabled=True,
                   agent_enabled=False,
                   group='spice')

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        cfg = conn.get_guest_config(instance_ref, [], None, disk_info)
        self.assertEquals(len(cfg.devices), 6)
        self.assertEquals(type(cfg.devices[0]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[1]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[2]),
                          vconfig.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[3]),
                          vconfig.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[4]),
                          vconfig.LibvirtConfigGuestInput)
        self.assertEquals(type(cfg.devices[5]),
                          vconfig.LibvirtConfigGuestGraphics)

        self.assertEquals(cfg.devices[4].type, "tablet")
        self.assertEquals(cfg.devices[5].type, "spice")

    def test_get_guest_config_with_spice_and_agent(self):
        self.flags(libvirt_type='kvm',
                   vnc_enabled=False,
                   use_usb_tablet=True)
        self.flags(enabled=True,
                   agent_enabled=True,
                   group='spice')

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        cfg = conn.get_guest_config(instance_ref, [], None, disk_info)
        self.assertEquals(len(cfg.devices), 6)
        self.assertEquals(type(cfg.devices[0]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[1]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[2]),
                          vconfig.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[3]),
                          vconfig.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[4]),
                          vconfig.LibvirtConfigGuestChannel)
        self.assertEquals(type(cfg.devices[5]),
                          vconfig.LibvirtConfigGuestGraphics)

        self.assertEquals(cfg.devices[4].target_name, "com.redhat.spice.0")
        self.assertEquals(cfg.devices[5].type, "spice")

    def test_get_guest_config_with_vnc_and_spice(self):
        self.flags(libvirt_type='kvm',
                   vnc_enabled=True,
                   use_usb_tablet=True)
        self.flags(enabled=True,
                   agent_enabled=True,
                   group='spice')

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        cfg = conn.get_guest_config(instance_ref, [], None, disk_info)
        self.assertEquals(len(cfg.devices), 8)
        self.assertEquals(type(cfg.devices[0]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[1]),
                          vconfig.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[2]),
                          vconfig.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[3]),
                          vconfig.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[4]),
                          vconfig.LibvirtConfigGuestInput)
        self.assertEquals(type(cfg.devices[5]),
                          vconfig.LibvirtConfigGuestChannel)
        self.assertEquals(type(cfg.devices[6]),
                          vconfig.LibvirtConfigGuestGraphics)
        self.assertEquals(type(cfg.devices[7]),
                          vconfig.LibvirtConfigGuestGraphics)

        self.assertEquals(cfg.devices[4].type, "tablet")
        self.assertEquals(cfg.devices[5].target_name, "com.redhat.spice.0")
        self.assertEquals(cfg.devices[6].type, "vnc")
        self.assertEquals(cfg.devices[7].type, "spice")

    def test_get_guest_cpu_config_none(self):
        self.flags(libvirt_cpu_mode="none")
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        conf = conn.get_guest_config(instance_ref,
                                     _fake_network_info(self.stubs, 1),
                                     None, disk_info)
        self.assertEquals(conf.cpu, None)

    def test_get_guest_cpu_config_default_kvm(self):
        self.flags(libvirt_type="kvm",
                   libvirt_cpu_mode=None)

        def get_lib_version_stub(self):
            return (0 * 1000 * 1000) + (9 * 1000) + 11

        self.stubs.Set(libvirt.virConnect,
                       "getLibVersion",
                       get_lib_version_stub)
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        conf = conn.get_guest_config(instance_ref,
                                     _fake_network_info(self.stubs, 1),
                                     None, disk_info)
        self.assertEquals(type(conf.cpu),
                          vconfig.LibvirtConfigGuestCPU)
        self.assertEquals(conf.cpu.mode, "host-model")
        self.assertEquals(conf.cpu.model, None)

    def test_get_guest_cpu_config_default_uml(self):
        self.flags(libvirt_type="uml",
                   libvirt_cpu_mode=None)

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        conf = conn.get_guest_config(instance_ref,
                                     _fake_network_info(self.stubs, 1),
                                     None, disk_info)
        self.assertEquals(conf.cpu, None)

    def test_get_guest_cpu_config_default_lxc(self):
        self.flags(libvirt_type="lxc",
                   libvirt_cpu_mode=None)

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        conf = conn.get_guest_config(instance_ref,
                                     _fake_network_info(self.stubs, 1),
                                     None, disk_info)
        self.assertEquals(conf.cpu, None)

    def test_get_guest_cpu_config_host_passthrough_new(self):
        def get_lib_version_stub(self):
            return (0 * 1000 * 1000) + (9 * 1000) + 11

        self.stubs.Set(libvirt.virConnect,
                       "getLibVersion",
                       get_lib_version_stub)
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        self.flags(libvirt_cpu_mode="host-passthrough")
        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        conf = conn.get_guest_config(instance_ref,
                                     _fake_network_info(self.stubs, 1),
                                     None, disk_info)
        self.assertEquals(type(conf.cpu),
                          vconfig.LibvirtConfigGuestCPU)
        self.assertEquals(conf.cpu.mode, "host-passthrough")
        self.assertEquals(conf.cpu.model, None)

    def test_get_guest_cpu_config_host_model_new(self):
        def get_lib_version_stub(self):
            return (0 * 1000 * 1000) + (9 * 1000) + 11

        self.stubs.Set(libvirt.virConnect,
                       "getLibVersion",
                       get_lib_version_stub)
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        self.flags(libvirt_cpu_mode="host-model")
        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        conf = conn.get_guest_config(instance_ref,
                                     _fake_network_info(self.stubs, 1),
                                     None, disk_info)
        self.assertEquals(type(conf.cpu),
                          vconfig.LibvirtConfigGuestCPU)
        self.assertEquals(conf.cpu.mode, "host-model")
        self.assertEquals(conf.cpu.model, None)

    def test_get_guest_cpu_config_custom_new(self):
        def get_lib_version_stub(self):
            return (0 * 1000 * 1000) + (9 * 1000) + 11

        self.stubs.Set(libvirt.virConnect,
                       "getLibVersion",
                       get_lib_version_stub)
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        self.flags(libvirt_cpu_mode="custom")
        self.flags(libvirt_cpu_model="Penryn")
        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        conf = conn.get_guest_config(instance_ref,
                                     _fake_network_info(self.stubs, 1),
                                     None, disk_info)
        self.assertEquals(type(conf.cpu),
                          vconfig.LibvirtConfigGuestCPU)
        self.assertEquals(conf.cpu.mode, "custom")
        self.assertEquals(conf.cpu.model, "Penryn")

    def test_get_guest_cpu_config_host_passthrough_old(self):
        def get_lib_version_stub(self):
            return (0 * 1000 * 1000) + (9 * 1000) + 7

        self.stubs.Set(libvirt.virConnect, "getLibVersion",
                       get_lib_version_stub)
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        self.flags(libvirt_cpu_mode="host-passthrough")
        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        self.assertRaises(exception.NovaException,
                          conn.get_guest_config,
                          instance_ref,
                          _fake_network_info(self.stubs, 1),
                          None,
                          disk_info)

    def test_get_guest_cpu_config_host_model_old(self):
        def get_lib_version_stub(self):
            return (0 * 1000 * 1000) + (9 * 1000) + 7

        # Ensure we have a predictable host CPU
        def get_host_capabilities_stub(self):
            cpu = vconfig.LibvirtConfigGuestCPU()
            cpu.model = "Opteron_G4"
            cpu.vendor = "AMD"

            cpu.features.append(vconfig.LibvirtConfigGuestCPUFeature("tm2"))
            cpu.features.append(vconfig.LibvirtConfigGuestCPUFeature("ht"))

            caps = vconfig.LibvirtConfigCaps()
            caps.host = vconfig.LibvirtConfigCapsHost()
            caps.host.cpu = cpu
            return caps

        self.stubs.Set(libvirt.virConnect,
                       "getLibVersion",
                       get_lib_version_stub)
        self.stubs.Set(libvirt_driver.LibvirtDriver,
                       "get_host_capabilities",
                       get_host_capabilities_stub)
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        self.flags(libvirt_cpu_mode="host-model")
        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        conf = conn.get_guest_config(instance_ref,
                                     _fake_network_info(self.stubs, 1),
                                     None, disk_info)
        self.assertEquals(type(conf.cpu),
                          vconfig.LibvirtConfigGuestCPU)
        self.assertEquals(conf.cpu.mode, None)
        self.assertEquals(conf.cpu.model, "Opteron_G4")
        self.assertEquals(conf.cpu.vendor, "AMD")
        self.assertEquals(len(conf.cpu.features), 2)
        self.assertEquals(conf.cpu.features[0].name, "tm2")
        self.assertEquals(conf.cpu.features[1].name, "ht")

    def test_get_guest_cpu_config_custom_old(self):
        def get_lib_version_stub(self):
            return (0 * 1000 * 1000) + (9 * 1000) + 7

        self.stubs.Set(libvirt.virConnect,
                       "getLibVersion",
                       get_lib_version_stub)
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        self.flags(libvirt_cpu_mode="custom")
        self.flags(libvirt_cpu_model="Penryn")
        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        conf = conn.get_guest_config(instance_ref,
                                     _fake_network_info(self.stubs, 1),
                                     None, disk_info)
        self.assertEquals(type(conf.cpu),
                          vconfig.LibvirtConfigGuestCPU)
        self.assertEquals(conf.cpu.mode, None)
        self.assertEquals(conf.cpu.model, "Penryn")

    def test_xml_and_uri_no_ramdisk_no_kernel(self):
        instance_data = dict(self.test_instance)
        self._check_xml_and_uri(instance_data,
                                expect_kernel=False, expect_ramdisk=False)

    def test_xml_and_uri_no_ramdisk_no_kernel_xen_hvm(self):
        instance_data = dict(self.test_instance)
        instance_data.update({'vm_mode': vm_mode.HVM})
        self._check_xml_and_uri(instance_data, expect_kernel=False,
                                expect_ramdisk=False, expect_xen_hvm=True)

    def test_xml_and_uri_no_ramdisk_no_kernel_xen_pv(self):
        instance_data = dict(self.test_instance)
        instance_data.update({'vm_mode': vm_mode.XEN})
        self._check_xml_and_uri(instance_data, expect_kernel=False,
                                expect_ramdisk=False, expect_xen_hvm=False,
                                xen_only=True)

    def test_xml_and_uri_no_ramdisk(self):
        instance_data = dict(self.test_instance)
        instance_data['kernel_id'] = 'aki-deadbeef'
        self._check_xml_and_uri(instance_data,
                                expect_kernel=True, expect_ramdisk=False)

    def test_xml_and_uri_no_kernel(self):
        instance_data = dict(self.test_instance)
        instance_data['ramdisk_id'] = 'ari-deadbeef'
        self._check_xml_and_uri(instance_data,
                                expect_kernel=False, expect_ramdisk=False)

    def test_xml_and_uri(self):
        instance_data = dict(self.test_instance)
        instance_data['ramdisk_id'] = 'ari-deadbeef'
        instance_data['kernel_id'] = 'aki-deadbeef'
        self._check_xml_and_uri(instance_data,
                                expect_kernel=True, expect_ramdisk=True)

    def test_xml_and_uri_rescue(self):
        instance_data = dict(self.test_instance)
        instance_data['ramdisk_id'] = 'ari-deadbeef'
        instance_data['kernel_id'] = 'aki-deadbeef'
        self._check_xml_and_uri(instance_data, expect_kernel=True,
                                expect_ramdisk=True, rescue=instance_data)

    def test_xml_and_uri_rescue_no_kernel_no_ramdisk(self):
        instance_data = dict(self.test_instance)
        self._check_xml_and_uri(instance_data, expect_kernel=False,
                                expect_ramdisk=False, rescue=instance_data)

    def test_xml_and_uri_rescue_no_kernel(self):
        instance_data = dict(self.test_instance)
        instance_data['ramdisk_id'] = 'aki-deadbeef'
        self._check_xml_and_uri(instance_data, expect_kernel=False,
                                expect_ramdisk=True, rescue=instance_data)

    def test_xml_and_uri_rescue_no_ramdisk(self):
        instance_data = dict(self.test_instance)
        instance_data['kernel_id'] = 'aki-deadbeef'
        self._check_xml_and_uri(instance_data, expect_kernel=True,
                                expect_ramdisk=False, rescue=instance_data)

    def test_xml_uuid(self):
        self._check_xml_and_uuid({"disk_format": "raw"})

    def test_lxc_container_and_uri(self):
        instance_data = dict(self.test_instance)
        self._check_xml_and_container(instance_data)

    def test_xml_disk_prefix(self):
        instance_data = dict(self.test_instance)
        self._check_xml_and_disk_prefix(instance_data)

    def test_xml_disk_driver(self):
        instance_data = dict(self.test_instance)
        self._check_xml_and_disk_driver(instance_data)

    def test_xml_disk_bus_virtio(self):
        self._check_xml_and_disk_bus({"disk_format": "raw"},
                                     None,
                                     (("disk", "virtio", "vda"),))

    def test_xml_disk_bus_ide(self):
        self._check_xml_and_disk_bus({"disk_format": "iso"},
                                     None,
                                     (("cdrom", "ide", "hda"),))

    def test_xml_disk_bus_ide_and_virtio(self):
        swap = {'device_name': '/dev/vdc',
                'swap_size': 1}
        ephemerals = [{'num': 0,
                       'virtual_name': 'ephemeral0',
                       'device_name': '/dev/vdb',
                       'size': 1}]
        block_device_info = {
                'swap': swap,
                'ephemerals': ephemerals}

        self._check_xml_and_disk_bus({"disk_format": "iso"},
                                     block_device_info,
                                     (("cdrom", "ide", "hda"),
                                      ("disk", "virtio", "vdb"),
                                      ("disk", "virtio", "vdc")))

    def test_list_instances(self):
        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByID = self.fake_lookup
        libvirt_driver.LibvirtDriver._conn.numOfDomains = lambda: 2
        libvirt_driver.LibvirtDriver._conn.listDomainsID = lambda: [0, 1]
        libvirt_driver.LibvirtDriver._conn.listDefinedDomains = lambda: []

        self.mox.ReplayAll()
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instances = conn.list_instances()
        # Only one should be listed, since domain with ID 0 must be skiped
        self.assertEquals(len(instances), 1)

    def test_list_defined_instances(self):
        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByID = self.fake_lookup
        libvirt_driver.LibvirtDriver._conn.numOfDomains = lambda: 1
        libvirt_driver.LibvirtDriver._conn.listDomainsID = lambda: [0]
        libvirt_driver.LibvirtDriver._conn.listDefinedDomains = lambda: [1]

        self.mox.ReplayAll()
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instances = conn.list_instances()
        # Only one defined domain should be listed
        self.assertEquals(len(instances), 1)

    def test_list_instances_when_instance_deleted(self):

        def fake_lookup(instance_name):
            raise libvirt.libvirtError("we deleted an instance!")

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByID = fake_lookup
        libvirt_driver.LibvirtDriver._conn.numOfDomains = lambda: 1
        libvirt_driver.LibvirtDriver._conn.listDomainsID = lambda: [0, 1]
        libvirt_driver.LibvirtDriver._conn.listDefinedDomains = lambda: []

        self.mox.ReplayAll()
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instances = conn.list_instances()
        # None should be listed, since we fake deleted the last one
        self.assertEquals(len(instances), 0)

    def test_get_all_block_devices(self):
        xml = [
            # NOTE(vish): id 0 is skipped
            None,
            """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/1'/>
                        </disk>
                    </devices>
                </domain>
            """,
            """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                        </disk>
                    </devices>
                </domain>
            """,
            """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/3'/>
                        </disk>
                    </devices>
                </domain>
            """,
        ]

        def fake_lookup(id):
            return FakeVirtDomain(xml[id])

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.numOfDomains = lambda: 4
        libvirt_driver.LibvirtDriver._conn.listDomainsID = lambda: range(4)
        libvirt_driver.LibvirtDriver._conn.lookupByID = fake_lookup

        self.mox.ReplayAll()
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        devices = conn.get_all_block_devices()
        self.assertEqual(devices, ['/path/to/dev/1', '/path/to/dev/3'])

    def test_get_disks(self):
        xml = [
            # NOTE(vish): id 0 is skipped
            None,
            """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                            <target dev='vda' bus='virtio'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/1'/>
                            <target dev='vdb' bus='virtio'/>
                        </disk>
                    </devices>
                </domain>
            """,
            """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                            <target dev='vda' bus='virtio'/>
                        </disk>
                    </devices>
                </domain>
            """,
            """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                            <target dev='vda' bus='virtio'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/3'/>
                            <target dev='vdb' bus='virtio'/>
                        </disk>
                    </devices>
                </domain>
            """,
        ]

        def fake_lookup(id):
            return FakeVirtDomain(xml[id])

        def fake_lookup_name(name):
            return FakeVirtDomain(xml[1])

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.numOfDomains = lambda: 4
        libvirt_driver.LibvirtDriver._conn.listDomainsID = lambda: range(4)
        libvirt_driver.LibvirtDriver._conn.lookupByID = fake_lookup
        libvirt_driver.LibvirtDriver._conn.lookupByName = fake_lookup_name
        libvirt_driver.LibvirtDriver._conn.listDefinedDomains = lambda: []

        self.mox.ReplayAll()
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        devices = conn.get_disks(conn.list_instances()[0])
        self.assertEqual(devices, ['vda', 'vdb'])

    def test_snapshot_in_ami_format(self):
        expected_calls = [
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_PENDING_UPLOAD}},
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_UPLOADING,
                  'expected_state': task_states.IMAGE_PENDING_UPLOAD}}]
        func_call_matcher = matchers.FunctionCallMatcher(expected_calls)

        self.flags(libvirt_snapshots_directory='./')

        # Start test
        image_service = nova.tests.image.fake.FakeImageService()

        # Assign different image_ref from nova/images/fakes for testing ami
        test_instance = copy.deepcopy(self.test_instance)
        test_instance["image_ref"] = 'c905cedb-7281-47e4-8a62-f26bc5fc4c77'

        # Assuming that base image already exists in image_service
        instance_ref = db.instance_create(self.context, test_instance)
        properties = {'instance_id': instance_ref['id'],
                      'user_id': str(self.context.user_id)}
        snapshot_name = 'test-snap'
        sent_meta = {'name': snapshot_name, 'is_public': False,
                     'status': 'creating', 'properties': properties}
        # Create new image. It will be updated in snapshot method
        # To work with it from snapshot, the single image_service is needed
        recv_meta = image_service.create(context, sent_meta)

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        self.mox.StubOutWithMock(libvirt_driver.utils, 'execute')
        libvirt_driver.utils.execute = self.fake_execute
        libvirt_driver.libvirt_utils.disk_type = "qcow2"

        self.mox.ReplayAll()

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        conn.snapshot(self.context, instance_ref, recv_meta['id'],
                      func_call_matcher.call)

        snapshot = image_service.show(context, recv_meta['id'])
        self.assertIsNone(func_call_matcher.match())
        self.assertEquals(snapshot['properties']['image_state'], 'available')
        self.assertEquals(snapshot['properties']['image_state'], 'available')
        self.assertEquals(snapshot['status'], 'active')
        self.assertEquals(snapshot['disk_format'], 'ami')
        self.assertEquals(snapshot['name'], snapshot_name)

    def test_lxc_snapshot_in_ami_format(self):
        expected_calls = [
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_PENDING_UPLOAD}},
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_UPLOADING,
                  'expected_state': task_states.IMAGE_PENDING_UPLOAD}}]
        func_call_matcher = matchers.FunctionCallMatcher(expected_calls)

        self.flags(libvirt_snapshots_directory='./',
                   libvirt_type='lxc')

        # Start test
        image_service = nova.tests.image.fake.FakeImageService()

        # Assign different image_ref from nova/images/fakes for testing ami
        test_instance = copy.deepcopy(self.test_instance)
        test_instance["image_ref"] = 'c905cedb-7281-47e4-8a62-f26bc5fc4c77'

        # Assuming that base image already exists in image_service
        instance_ref = db.instance_create(self.context, test_instance)
        properties = {'instance_id': instance_ref['id'],
                      'user_id': str(self.context.user_id)}
        snapshot_name = 'test-snap'
        sent_meta = {'name': snapshot_name, 'is_public': False,
                     'status': 'creating', 'properties': properties}
        # Create new image. It will be updated in snapshot method
        # To work with it from snapshot, the single image_service is needed
        recv_meta = image_service.create(context, sent_meta)

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        self.mox.StubOutWithMock(libvirt_driver.utils, 'execute')
        libvirt_driver.utils.execute = self.fake_execute
        libvirt_driver.libvirt_utils.disk_type = "qcow2"

        self.mox.ReplayAll()

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        conn.snapshot(self.context, instance_ref, recv_meta['id'],
                      func_call_matcher.call)

        snapshot = image_service.show(context, recv_meta['id'])
        self.assertIsNone(func_call_matcher.match())
        self.assertEquals(snapshot['properties']['image_state'], 'available')
        self.assertEquals(snapshot['status'], 'active')
        self.assertEquals(snapshot['disk_format'], 'ami')
        self.assertEquals(snapshot['name'], snapshot_name)

    def test_snapshot_in_raw_format(self):
        expected_calls = [
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_PENDING_UPLOAD}},
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_UPLOADING,
                  'expected_state': task_states.IMAGE_PENDING_UPLOAD}}]
        func_call_matcher = matchers.FunctionCallMatcher(expected_calls)

        self.flags(libvirt_snapshots_directory='./')

        # Start test
        image_service = nova.tests.image.fake.FakeImageService()

        # Assuming that base image already exists in image_service
        instance_ref = db.instance_create(self.context, self.test_instance)
        properties = {'instance_id': instance_ref['id'],
                      'user_id': str(self.context.user_id)}
        snapshot_name = 'test-snap'
        sent_meta = {'name': snapshot_name, 'is_public': False,
                     'status': 'creating', 'properties': properties}
        # Create new image. It will be updated in snapshot method
        # To work with it from snapshot, the single image_service is needed
        recv_meta = image_service.create(context, sent_meta)

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        self.mox.StubOutWithMock(libvirt_driver.utils, 'execute')
        libvirt_driver.utils.execute = self.fake_execute
        self.stubs.Set(libvirt_driver.libvirt_utils, 'disk_type', 'raw')

        def convert_image(source, dest, out_format):
            libvirt_driver.libvirt_utils.files[dest] = ''

        self.stubs.Set(images, 'convert_image', convert_image)

        self.mox.ReplayAll()

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        conn.snapshot(self.context, instance_ref, recv_meta['id'],
                      func_call_matcher.call)

        snapshot = image_service.show(context, recv_meta['id'])
        self.assertIsNone(func_call_matcher.match())
        self.assertEquals(snapshot['properties']['image_state'], 'available')
        self.assertEquals(snapshot['status'], 'active')
        self.assertEquals(snapshot['disk_format'], 'raw')
        self.assertEquals(snapshot['name'], snapshot_name)

    def test_lxc_snapshot_in_raw_format(self):
        expected_calls = [
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_PENDING_UPLOAD}},
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_UPLOADING,
                  'expected_state': task_states.IMAGE_PENDING_UPLOAD}}]
        func_call_matcher = matchers.FunctionCallMatcher(expected_calls)

        self.flags(libvirt_snapshots_directory='./',
                   libvirt_type='lxc')

        # Start test
        image_service = nova.tests.image.fake.FakeImageService()

        # Assuming that base image already exists in image_service
        instance_ref = db.instance_create(self.context, self.test_instance)
        properties = {'instance_id': instance_ref['id'],
                      'user_id': str(self.context.user_id)}
        snapshot_name = 'test-snap'
        sent_meta = {'name': snapshot_name, 'is_public': False,
                     'status': 'creating', 'properties': properties}
        # Create new image. It will be updated in snapshot method
        # To work with it from snapshot, the single image_service is needed
        recv_meta = image_service.create(context, sent_meta)

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        self.mox.StubOutWithMock(libvirt_driver.utils, 'execute')
        libvirt_driver.utils.execute = self.fake_execute
        self.stubs.Set(libvirt_driver.libvirt_utils, 'disk_type', 'raw')

        def convert_image(source, dest, out_format):
            libvirt_driver.libvirt_utils.files[dest] = ''

        self.stubs.Set(images, 'convert_image', convert_image)

        self.mox.ReplayAll()

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        conn.snapshot(self.context, instance_ref, recv_meta['id'],
                      func_call_matcher.call)

        snapshot = image_service.show(context, recv_meta['id'])
        self.assertIsNone(func_call_matcher.match())
        self.assertEquals(snapshot['properties']['image_state'], 'available')
        self.assertEquals(snapshot['status'], 'active')
        self.assertEquals(snapshot['disk_format'], 'raw')
        self.assertEquals(snapshot['name'], snapshot_name)

    def test_snapshot_in_qcow2_format(self):
        expected_calls = [
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_PENDING_UPLOAD}},
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_UPLOADING,
                  'expected_state': task_states.IMAGE_PENDING_UPLOAD}}]
        func_call_matcher = matchers.FunctionCallMatcher(expected_calls)

        self.flags(snapshot_image_format='qcow2',
                   libvirt_snapshots_directory='./')

        # Start test
        image_service = nova.tests.image.fake.FakeImageService()

        # Assuming that base image already exists in image_service
        instance_ref = db.instance_create(self.context, self.test_instance)
        properties = {'instance_id': instance_ref['id'],
                      'user_id': str(self.context.user_id)}
        snapshot_name = 'test-snap'
        sent_meta = {'name': snapshot_name, 'is_public': False,
                     'status': 'creating', 'properties': properties}
        # Create new image. It will be updated in snapshot method
        # To work with it from snapshot, the single image_service is needed
        recv_meta = image_service.create(context, sent_meta)

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        self.mox.StubOutWithMock(libvirt_driver.utils, 'execute')
        libvirt_driver.utils.execute = self.fake_execute
        libvirt_driver.libvirt_utils.disk_type = "qcow2"

        self.mox.ReplayAll()

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        conn.snapshot(self.context, instance_ref, recv_meta['id'],
                      func_call_matcher.call)

        snapshot = image_service.show(context, recv_meta['id'])
        self.assertIsNone(func_call_matcher.match())
        self.assertEquals(snapshot['properties']['image_state'], 'available')
        self.assertEquals(snapshot['status'], 'active')
        self.assertEquals(snapshot['disk_format'], 'qcow2')
        self.assertEquals(snapshot['name'], snapshot_name)

    def test_lxc_snapshot_in_qcow2_format(self):
        expected_calls = [
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_PENDING_UPLOAD}},
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_UPLOADING,
                  'expected_state': task_states.IMAGE_PENDING_UPLOAD}}]
        func_call_matcher = matchers.FunctionCallMatcher(expected_calls)

        self.flags(snapshot_image_format='qcow2',
                   libvirt_snapshots_directory='./',
                   libvirt_type='lxc')

        # Start test
        image_service = nova.tests.image.fake.FakeImageService()

        # Assuming that base image already exists in image_service
        instance_ref = db.instance_create(self.context, self.test_instance)
        properties = {'instance_id': instance_ref['id'],
                      'user_id': str(self.context.user_id)}
        snapshot_name = 'test-snap'
        sent_meta = {'name': snapshot_name, 'is_public': False,
                     'status': 'creating', 'properties': properties}
        # Create new image. It will be updated in snapshot method
        # To work with it from snapshot, the single image_service is needed
        recv_meta = image_service.create(context, sent_meta)

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        self.mox.StubOutWithMock(libvirt_driver.utils, 'execute')
        libvirt_driver.utils.execute = self.fake_execute
        libvirt_driver.libvirt_utils.disk_type = "qcow2"

        self.mox.ReplayAll()

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        conn.snapshot(self.context, instance_ref, recv_meta['id'],
                      func_call_matcher.call)

        snapshot = image_service.show(context, recv_meta['id'])
        self.assertIsNone(func_call_matcher.match())
        self.assertEquals(snapshot['properties']['image_state'], 'available')
        self.assertEquals(snapshot['status'], 'active')
        self.assertEquals(snapshot['disk_format'], 'qcow2')
        self.assertEquals(snapshot['name'], snapshot_name)

    def test_snapshot_no_image_architecture(self):
        expected_calls = [
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_PENDING_UPLOAD}},
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_UPLOADING,
                  'expected_state': task_states.IMAGE_PENDING_UPLOAD}}]
        func_call_matcher = matchers.FunctionCallMatcher(expected_calls)

        self.flags(libvirt_snapshots_directory='./')

        # Start test
        image_service = nova.tests.image.fake.FakeImageService()

        # Assign different image_ref from nova/images/fakes for
        # testing different base image
        test_instance = copy.deepcopy(self.test_instance)
        test_instance["image_ref"] = '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6'

        # Assuming that base image already exists in image_service
        instance_ref = db.instance_create(self.context, test_instance)
        properties = {'instance_id': instance_ref['id'],
                      'user_id': str(self.context.user_id)}
        snapshot_name = 'test-snap'
        sent_meta = {'name': snapshot_name, 'is_public': False,
                     'status': 'creating', 'properties': properties}
        # Create new image. It will be updated in snapshot method
        # To work with it from snapshot, the single image_service is needed
        recv_meta = image_service.create(context, sent_meta)

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        self.mox.StubOutWithMock(libvirt_driver.utils, 'execute')
        libvirt_driver.utils.execute = self.fake_execute

        self.mox.ReplayAll()

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        conn.snapshot(self.context, instance_ref, recv_meta['id'],
                      func_call_matcher.call)

        snapshot = image_service.show(context, recv_meta['id'])
        self.assertIsNone(func_call_matcher.match())
        self.assertEquals(snapshot['properties']['image_state'], 'available')
        self.assertEquals(snapshot['status'], 'active')
        self.assertEquals(snapshot['name'], snapshot_name)

    def test_lxc_snapshot_no_image_architecture(self):
        expected_calls = [
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_PENDING_UPLOAD}},
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_UPLOADING,
                  'expected_state': task_states.IMAGE_PENDING_UPLOAD}}]
        func_call_matcher = matchers.FunctionCallMatcher(expected_calls)

        self.flags(libvirt_snapshots_directory='./',
                   libvirt_type='lxc')

        # Start test
        image_service = nova.tests.image.fake.FakeImageService()

        # Assign different image_ref from nova/images/fakes for
        # testing different base image
        test_instance = copy.deepcopy(self.test_instance)
        test_instance["image_ref"] = '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6'

        # Assuming that base image already exists in image_service
        instance_ref = db.instance_create(self.context, test_instance)
        properties = {'instance_id': instance_ref['id'],
                      'user_id': str(self.context.user_id)}
        snapshot_name = 'test-snap'
        sent_meta = {'name': snapshot_name, 'is_public': False,
                     'status': 'creating', 'properties': properties}
        # Create new image. It will be updated in snapshot method
        # To work with it from snapshot, the single image_service is needed
        recv_meta = image_service.create(context, sent_meta)

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        self.mox.StubOutWithMock(libvirt_driver.utils, 'execute')
        libvirt_driver.utils.execute = self.fake_execute

        self.mox.ReplayAll()

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        conn.snapshot(self.context, instance_ref, recv_meta['id'],
                      func_call_matcher.call)

        snapshot = image_service.show(context, recv_meta['id'])
        self.assertIsNone(func_call_matcher.match())
        self.assertEquals(snapshot['properties']['image_state'], 'available')
        self.assertEquals(snapshot['status'], 'active')
        self.assertEquals(snapshot['name'], snapshot_name)

    def test_snapshot_no_original_image(self):
        expected_calls = [
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_PENDING_UPLOAD}},
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_UPLOADING,
                  'expected_state': task_states.IMAGE_PENDING_UPLOAD}}]
        func_call_matcher = matchers.FunctionCallMatcher(expected_calls)

        self.flags(libvirt_snapshots_directory='./')

        # Start test
        image_service = nova.tests.image.fake.FakeImageService()

        # Assign a non-existent image
        test_instance = copy.deepcopy(self.test_instance)
        test_instance["image_ref"] = '661122aa-1234-dede-fefe-babababababa'

        instance_ref = db.instance_create(self.context, test_instance)
        properties = {'instance_id': instance_ref['id'],
                      'user_id': str(self.context.user_id)}
        snapshot_name = 'test-snap'
        sent_meta = {'name': snapshot_name, 'is_public': False,
                     'status': 'creating', 'properties': properties}
        recv_meta = image_service.create(context, sent_meta)

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        self.mox.StubOutWithMock(libvirt_driver.utils, 'execute')
        libvirt_driver.utils.execute = self.fake_execute

        self.mox.ReplayAll()

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        conn.snapshot(self.context, instance_ref, recv_meta['id'],
                      func_call_matcher.call)

        snapshot = image_service.show(context, recv_meta['id'])
        self.assertIsNone(func_call_matcher.match())
        self.assertEquals(snapshot['properties']['image_state'], 'available')
        self.assertEquals(snapshot['status'], 'active')
        self.assertEquals(snapshot['name'], snapshot_name)

    def test_lxc_snapshot_no_original_image(self):
        expected_calls = [
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_PENDING_UPLOAD}},
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_UPLOADING,
                  'expected_state': task_states.IMAGE_PENDING_UPLOAD}}]
        func_call_matcher = matchers.FunctionCallMatcher(expected_calls)

        self.flags(libvirt_snapshots_directory='./',
                   libvirt_type='lxc')

        # Start test
        image_service = nova.tests.image.fake.FakeImageService()

        # Assign a non-existent image
        test_instance = copy.deepcopy(self.test_instance)
        test_instance["image_ref"] = '661122aa-1234-dede-fefe-babababababa'

        instance_ref = db.instance_create(self.context, test_instance)
        properties = {'instance_id': instance_ref['id'],
                      'user_id': str(self.context.user_id)}
        snapshot_name = 'test-snap'
        sent_meta = {'name': snapshot_name, 'is_public': False,
                     'status': 'creating', 'properties': properties}
        recv_meta = image_service.create(context, sent_meta)

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        self.mox.StubOutWithMock(libvirt_driver.utils, 'execute')
        libvirt_driver.utils.execute = self.fake_execute

        self.mox.ReplayAll()

        conn = libvirt_driver.LibvirtDriver(False)
        conn.snapshot(self.context, instance_ref, recv_meta['id'],
                      func_call_matcher.call)

        snapshot = image_service.show(context, recv_meta['id'])
        self.assertIsNone(func_call_matcher.match())
        self.assertEquals(snapshot['properties']['image_state'], 'available')
        self.assertEquals(snapshot['status'], 'active')
        self.assertEquals(snapshot['name'], snapshot_name)

    def test_attach_invalid_volume_type(self):
        self.create_fake_libvirt_mock()
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        self.mox.ReplayAll()
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.assertRaises(exception.VolumeDriverNotFound,
                          conn.attach_volume,
                          {"driver_volume_type": "badtype"},
                          {"name": "fake-instance"},
                          "/dev/sda")

    def test_multi_nic(self):
        instance_data = dict(self.test_instance)
        network_info = _fake_network_info(self.stubs, 2)
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = db.instance_create(self.context, instance_data)
        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        xml = conn.to_xml(instance_ref, network_info, disk_info)
        tree = etree.fromstring(xml)
        interfaces = tree.findall("./devices/interface")
        self.assertEquals(len(interfaces), 2)
        self.assertEquals(interfaces[0].get('type'), 'bridge')

    def _check_xml_and_container(self, instance):
        user_context = context.RequestContext(self.user_id,
                                              self.project_id)
        instance_ref = db.instance_create(user_context, instance)

        self.flags(libvirt_type='lxc')
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        self.assertEquals(conn.uri(), 'lxc:///')

        network_info = _fake_network_info(self.stubs, 1)
        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        xml = conn.to_xml(instance_ref, network_info, disk_info)
        tree = etree.fromstring(xml)

        check = [
        (lambda t: t.find('.').get('type'), 'lxc'),
        (lambda t: t.find('./os/type').text, 'exe'),
        (lambda t: t.find('./devices/filesystem/target').get('dir'), '/')]

        for i, (check, expected_result) in enumerate(check):
            self.assertEqual(check(tree),
                             expected_result,
                             '%s failed common check %d' % (xml, i))

        target = tree.find('./devices/filesystem/source').get('dir')
        self.assertTrue(len(target) > 0)

    def _check_xml_and_disk_prefix(self, instance):
        user_context = context.RequestContext(self.user_id,
                                              self.project_id)
        instance_ref = db.instance_create(user_context, instance)

        type_disk_map = {
            'qemu': [
               (lambda t: t.find('.').get('type'), 'qemu'),
               (lambda t: t.find('./devices/disk/target').get('dev'), 'vda')],
            'xen': [
               (lambda t: t.find('.').get('type'), 'xen'),
               (lambda t: t.find('./devices/disk/target').get('dev'), 'sda')],
            'kvm': [
               (lambda t: t.find('.').get('type'), 'kvm'),
               (lambda t: t.find('./devices/disk/target').get('dev'), 'vda')],
            'uml': [
               (lambda t: t.find('.').get('type'), 'uml'),
               (lambda t: t.find('./devices/disk/target').get('dev'), 'ubda')]
            }

        for (libvirt_type, checks) in type_disk_map.iteritems():
            self.flags(libvirt_type=libvirt_type)
            conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

            network_info = _fake_network_info(self.stubs, 1)
            disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                                instance_ref)
            xml = conn.to_xml(instance_ref, network_info, disk_info)
            tree = etree.fromstring(xml)

            for i, (check, expected_result) in enumerate(checks):
                self.assertEqual(check(tree),
                                 expected_result,
                                 '%s != %s failed check %d' %
                                 (check(tree), expected_result, i))

    def _check_xml_and_disk_driver(self, image_meta):
        os_open = os.open
        directio_supported = True

        def os_open_stub(path, flags, *args, **kwargs):
            if flags & os.O_DIRECT:
                if not directio_supported:
                    raise OSError(errno.EINVAL,
                                  '%s: %s' % (os.strerror(errno.EINVAL), path))
                flags &= ~os.O_DIRECT
            return os_open(path, flags, *args, **kwargs)

        self.stubs.Set(os, 'open', os_open_stub)

        def connection_supports_direct_io_stub(*args, **kwargs):
            return directio_supported

        self.stubs.Set(libvirt_driver.LibvirtDriver,
            '_supports_direct_io', connection_supports_direct_io_stub)

        user_context = context.RequestContext(self.user_id, self.project_id)
        instance_ref = db.instance_create(user_context, self.test_instance)
        network_info = _fake_network_info(self.stubs, 1)

        drv = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        xml = drv.to_xml(instance_ref, network_info, disk_info, image_meta)
        tree = etree.fromstring(xml)
        disks = tree.findall('./devices/disk/driver')
        for disk in disks:
            self.assertEqual(disk.get("cache"), "none")

        directio_supported = False

        # The O_DIRECT availability is cached on first use in
        # LibvirtDriver, hence we re-create it here
        drv = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        xml = drv.to_xml(instance_ref, network_info, disk_info, image_meta)
        tree = etree.fromstring(xml)
        disks = tree.findall('./devices/disk/driver')
        for disk in disks:
            self.assertEqual(disk.get("cache"), "writethrough")

    def _check_xml_and_disk_bus(self, image_meta,
                                block_device_info, wantConfig):
        user_context = context.RequestContext(self.user_id, self.project_id)
        instance_ref = db.instance_create(user_context, self.test_instance)
        network_info = _fake_network_info(self.stubs, 1)

        drv = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref,
                                            block_device_info,
                                            image_meta)
        xml = drv.to_xml(instance_ref, network_info, disk_info, image_meta,
                         block_device_info=block_device_info)
        tree = etree.fromstring(xml)

        got_disks = tree.findall('./devices/disk')
        got_disk_targets = tree.findall('./devices/disk/target')
        for i in range(len(wantConfig)):
            want_device_type = wantConfig[i][0]
            want_device_bus = wantConfig[i][1]
            want_device_dev = wantConfig[i][2]

            got_device_type = got_disks[i].get('device')
            got_device_bus = got_disk_targets[i].get('bus')
            got_device_dev = got_disk_targets[i].get('dev')

            self.assertEqual(got_device_type, want_device_type)
            self.assertEqual(got_device_bus, want_device_bus)
            self.assertEqual(got_device_dev, want_device_dev)

    def _check_xml_and_uuid(self, image_meta):
        user_context = context.RequestContext(self.user_id, self.project_id)
        instance_ref = db.instance_create(user_context, self.test_instance)
        network_info = _fake_network_info(self.stubs, 1)

        drv = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance_ref)
        xml = drv.to_xml(instance_ref, network_info, disk_info, image_meta)
        tree = etree.fromstring(xml)
        self.assertEqual(tree.find('./uuid').text,
                         instance_ref['uuid'])

    def _check_xml_and_uri(self, instance, expect_ramdisk, expect_kernel,
                           rescue=None, expect_xen_hvm=False, xen_only=False):
        user_context = context.RequestContext(self.user_id, self.project_id)
        instance_ref = db.instance_create(user_context, instance)
        network_ref = db.project_get_networks(context.get_admin_context(),
                                             self.project_id)[0]

        type_uri_map = {'qemu': ('qemu:///system',
                             [(lambda t: t.find('.').get('type'), 'qemu'),
                              (lambda t: t.find('./os/type').text,
                               vm_mode.HVM),
                              (lambda t: t.find('./devices/emulator'), None)]),
                        'kvm': ('qemu:///system',
                             [(lambda t: t.find('.').get('type'), 'kvm'),
                              (lambda t: t.find('./os/type').text,
                               vm_mode.HVM),
                              (lambda t: t.find('./devices/emulator'), None)]),
                        'uml': ('uml:///system',
                             [(lambda t: t.find('.').get('type'), 'uml'),
                              (lambda t: t.find('./os/type').text,
                               vm_mode.UML)]),
                        'xen': ('xen:///',
                             [(lambda t: t.find('.').get('type'), 'xen'),
                              (lambda t: t.find('./os/type').text,
                               vm_mode.XEN)])}

        if expect_xen_hvm or xen_only:
            hypervisors_to_check = ['xen']
        else:
            hypervisors_to_check = ['qemu', 'kvm', 'xen']

        if expect_xen_hvm:
            type_uri_map = {}
            type_uri_map['xen'] = ('xen:///',
                                   [(lambda t: t.find('.').get('type'),
                                       'xen'),
                                    (lambda t: t.find('./os/type').text,
                                        vm_mode.HVM)])

        for hypervisor_type in hypervisors_to_check:
            check_list = type_uri_map[hypervisor_type][1]

            if rescue:
                suffix = '.rescue'
            else:
                suffix = ''
            if expect_kernel:
                check = (lambda t: t.find('./os/kernel').text.split(
                    '/')[1], 'kernel' + suffix)
            else:
                check = (lambda t: t.find('./os/kernel'), None)
            check_list.append(check)

            # Hypervisors that only support vm_mode.HVM should
            # not produce configuration that results in kernel
            # arguments
            if not expect_kernel and hypervisor_type in ['qemu', 'kvm']:
                check = (lambda t: t.find('./os/root'), None)
                check_list.append(check)
                check = (lambda t: t.find('./os/cmdline'), None)
                check_list.append(check)

            if expect_ramdisk:
                check = (lambda t: t.find('./os/initrd').text.split(
                    '/')[1], 'ramdisk' + suffix)
            else:
                check = (lambda t: t.find('./os/initrd'), None)
            check_list.append(check)

            if hypervisor_type in ['qemu', 'kvm']:
                xpath = "./sysinfo/system/entry"
                check = (lambda t: t.findall(xpath)[0].get("name"),
                         "manufacturer")
                check_list.append(check)
                check = (lambda t: t.findall(xpath)[0].text,
                         version.vendor_string())
                check_list.append(check)

                check = (lambda t: t.findall(xpath)[1].get("name"),
                         "product")
                check_list.append(check)
                check = (lambda t: t.findall(xpath)[1].text,
                         version.product_string())
                check_list.append(check)

                check = (lambda t: t.findall(xpath)[2].get("name"),
                         "version")
                check_list.append(check)
                # NOTE(sirp): empty strings don't roundtrip in lxml (they are
                # converted to None), so we need an `or ''` to correct for that
                check = (lambda t: t.findall(xpath)[2].text or '',
                         version.version_string_with_package())
                check_list.append(check)

                check = (lambda t: t.findall(xpath)[3].get("name"),
                         "serial")
                check_list.append(check)
                check = (lambda t: t.findall(xpath)[3].text,
                         "cef19ce0-0ca2-11df-855d-b19fbce37686")
                check_list.append(check)

                check = (lambda t: t.findall(xpath)[4].get("name"),
                         "uuid")
                check_list.append(check)
                check = (lambda t: t.findall(xpath)[4].text,
                         instance['uuid'])
                check_list.append(check)

            if hypervisor_type in ['qemu', 'kvm']:
                check = (lambda t: t.findall('./devices/serial')[0].get(
                        'type'), 'file')
                check_list.append(check)
                check = (lambda t: t.findall('./devices/serial')[1].get(
                        'type'), 'pty')
                check_list.append(check)
                check = (lambda t: t.findall('./devices/serial/source')[0].get(
                        'path').split('/')[1], 'console.log')
                check_list.append(check)
            else:
                check = (lambda t: t.find('./devices/console').get(
                        'type'), 'pty')
                check_list.append(check)

        common_checks = [
            (lambda t: t.find('.').tag, 'domain'),
            (lambda t: t.find('./memory').text, '2097152')]
        if rescue:
            common_checks += [
                (lambda t: t.findall('./devices/disk/source')[0].get(
                    'file').split('/')[1], 'disk.rescue'),
                (lambda t: t.findall('./devices/disk/source')[1].get(
                    'file').split('/')[1], 'disk')]
        else:
            common_checks += [(lambda t: t.findall(
                './devices/disk/source')[0].get('file').split('/')[1],
                               'disk')]
            common_checks += [(lambda t: t.findall(
                './devices/disk/source')[1].get('file').split('/')[1],
                               'disk.local')]

        for (libvirt_type, (expected_uri, checks)) in type_uri_map.iteritems():
            self.flags(libvirt_type=libvirt_type)
            conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

            self.assertEquals(conn.uri(), expected_uri)

            network_info = _fake_network_info(self.stubs, 1)
            disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                                instance_ref,
                                                rescue=rescue)
            xml = conn.to_xml(instance_ref, network_info, disk_info,
                              rescue=rescue)
            tree = etree.fromstring(xml)
            for i, (check, expected_result) in enumerate(checks):
                self.assertEqual(check(tree),
                                 expected_result,
                                 '%s != %s failed check %d' %
                                 (check(tree), expected_result, i))

            for i, (check, expected_result) in enumerate(common_checks):
                self.assertEqual(check(tree),
                                 expected_result,
                                 '%s != %s failed common check %d' %
                                 (check(tree), expected_result, i))

            filterref = './devices/interface/filterref'
            (network, mapping) = network_info[0]
            nic_id = mapping['mac'].replace(':', '')
            fw = firewall.NWFilterFirewall(fake.FakeVirtAPI(), conn)
            instance_filter_name = fw._instance_filter_name(instance_ref,
                                                            nic_id)
            self.assertEqual(tree.find(filterref).get('filter'),
                             instance_filter_name)
        # This test is supposed to make sure we don't
        # override a specifically set uri
        #
        # Deliberately not just assigning this string to CONF.libvirt_uri and
        # checking against that later on. This way we make sure the
        # implementation doesn't fiddle around with the CONF.
        testuri = 'something completely different'
        self.flags(libvirt_uri=testuri)
        for (libvirt_type, (expected_uri, checks)) in type_uri_map.iteritems():
            self.flags(libvirt_type=libvirt_type)
            conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
            self.assertEquals(conn.uri(), testuri)
        db.instance_destroy(user_context, instance_ref['uuid'])

    def test_ensure_filtering_rules_for_instance_timeout(self):
        # ensure_filtering_fules_for_instance() finishes with timeout.
        # Preparing mocks
        def fake_none(self, *args):
            return

        def fake_raise(self):
            raise libvirt.libvirtError('ERR')

        class FakeTime(object):
            def __init__(self):
                self.counter = 0

            def sleep(self, t):
                self.counter += t

        fake_timer = FakeTime()

        # _fake_network_info must be called before create_fake_libvirt_mock(),
        # as _fake_network_info calls importutils.import_class() and
        # create_fake_libvirt_mock() mocks importutils.import_class().
        network_info = _fake_network_info(self.stubs, 1)
        self.create_fake_libvirt_mock()
        instance_ref = db.instance_create(self.context, self.test_instance)

        # Start test
        self.mox.ReplayAll()
        try:
            conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
            self.stubs.Set(conn.firewall_driver,
                           'setup_basic_filtering',
                           fake_none)
            self.stubs.Set(conn.firewall_driver,
                           'prepare_instance_filter',
                           fake_none)
            self.stubs.Set(conn.firewall_driver,
                           'instance_filter_exists',
                           fake_none)
            conn.ensure_filtering_rules_for_instance(instance_ref,
                                                     network_info,
                                                     time_module=fake_timer)
        except exception.NovaException, e:
            msg = ('The firewall filter for %s does not exist' %
                   instance_ref['name'])
            c1 = (0 <= str(e).find(msg))
        self.assertTrue(c1)

        self.assertEqual(29, fake_timer.counter, "Didn't wait the expected "
                                                 "amount of time")

        db.instance_destroy(self.context, instance_ref['uuid'])

    def test_check_can_live_migrate_dest_all_pass_with_block_migration(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest = "fake_host_2"
        src = instance_ref['host']
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        compute_info = {'disk_available_least': 400,
                        'cpu_info': 'asdf',
                        }
        filename = "file"

        self.mox.StubOutWithMock(conn, '_create_shared_storage_test_file')
        self.mox.StubOutWithMock(conn, '_compare_cpu')

        # _check_cpu_match
        conn._compare_cpu("asdf")

        # mounted_on_same_shared_storage
        conn._create_shared_storage_test_file().AndReturn(filename)

        self.mox.ReplayAll()
        return_value = conn.check_can_live_migrate_destination(self.context,
                instance_ref, compute_info, compute_info, True)
        self.assertThat({"filename": "file",
                         'disk_available_mb': 409600,
                         "disk_over_commit": False,
                         "block_migration": True},
                        matchers.DictMatches(return_value))

    def test_check_can_live_migrate_dest_all_pass_no_block_migration(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest = "fake_host_2"
        src = instance_ref['host']
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        compute_info = {'cpu_info': 'asdf'}
        filename = "file"

        self.mox.StubOutWithMock(conn, '_create_shared_storage_test_file')
        self.mox.StubOutWithMock(conn, '_compare_cpu')

        # _check_cpu_match
        conn._compare_cpu("asdf")

        # mounted_on_same_shared_storage
        conn._create_shared_storage_test_file().AndReturn(filename)

        self.mox.ReplayAll()
        return_value = conn.check_can_live_migrate_destination(self.context,
                instance_ref, compute_info, compute_info, False)
        self.assertThat({"filename": "file",
                         "block_migration": False,
                         "disk_over_commit": False,
                         "disk_available_mb": None},
                        matchers.DictMatches(return_value))

    def test_check_can_live_migrate_dest_incompatible_cpu_raises(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest = "fake_host_2"
        src = instance_ref['host']
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        compute_info = {'cpu_info': 'asdf'}

        self.mox.StubOutWithMock(conn, '_compare_cpu')

        conn._compare_cpu("asdf").AndRaise(exception.InvalidCPUInfo(
                                              reason='foo')
                                           )

        self.mox.ReplayAll()
        self.assertRaises(exception.InvalidCPUInfo,
                          conn.check_can_live_migrate_destination,
                          self.context, instance_ref,
                          compute_info, compute_info, False)

    def test_check_can_live_migrate_dest_cleanup_works_correctly(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest_check_data = {"filename": "file",
                           "block_migration": True,
                           "disk_over_commit": False,
                           "disk_available_mb": 1024}
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        self.mox.StubOutWithMock(conn, '_cleanup_shared_storage_test_file')
        conn._cleanup_shared_storage_test_file("file")

        self.mox.ReplayAll()
        conn.check_can_live_migrate_destination_cleanup(self.context,
                                                        dest_check_data)

    def test_check_can_live_migrate_source_works_correctly(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest_check_data = {"filename": "file",
                           "block_migration": True,
                           "disk_over_commit": False,
                           "disk_available_mb": 1024}
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        self.mox.StubOutWithMock(conn, "_check_shared_storage_test_file")
        conn._check_shared_storage_test_file("file").AndReturn(False)

        self.mox.StubOutWithMock(conn, "_assert_dest_node_has_enough_disk")
        conn._assert_dest_node_has_enough_disk(self.context, instance_ref,
                                        dest_check_data['disk_available_mb'],
                                               False)

        self.mox.ReplayAll()
        conn.check_can_live_migrate_source(self.context, instance_ref,
                                           dest_check_data)

    def test_check_can_live_migrate_source_vol_backed_works_correctly(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest_check_data = {"filename": "file",
                           "block_migration": False,
                           "disk_over_commit": False,
                           "disk_available_mb": 1024,
                           "is_volume_backed": True}
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.mox.StubOutWithMock(conn, "_check_shared_storage_test_file")
        conn._check_shared_storage_test_file("file").AndReturn(False)
        self.mox.ReplayAll()
        ret = conn.check_can_live_migrate_source(self.context, instance_ref,
                                                 dest_check_data)
        self.assertTrue(type(ret) == dict)
        self.assertTrue('is_shared_storage' in ret)

    def test_check_can_live_migrate_source_vol_backed_fails(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest_check_data = {"filename": "file",
                           "block_migration": False,
                           "disk_over_commit": False,
                           "disk_available_mb": 1024,
                           "is_volume_backed": False}
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.mox.StubOutWithMock(conn, "_check_shared_storage_test_file")
        conn._check_shared_storage_test_file("file").AndReturn(False)
        self.mox.ReplayAll()
        self.assertRaises(exception.InvalidSharedStorage,
                          conn.check_can_live_migrate_source, self.context,
                          instance_ref, dest_check_data)

    def test_check_can_live_migrate_dest_fail_shared_storage_with_blockm(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest_check_data = {"filename": "file",
                           "block_migration": True,
                           "disk_over_commit": False,
                           'disk_available_mb': 1024}
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        self.mox.StubOutWithMock(conn, "_check_shared_storage_test_file")
        conn._check_shared_storage_test_file("file").AndReturn(True)

        self.mox.ReplayAll()
        self.assertRaises(exception.InvalidLocalStorage,
                          conn.check_can_live_migrate_source,
                          self.context, instance_ref, dest_check_data)

    def test_check_can_live_migrate_no_shared_storage_no_blck_mig_raises(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest_check_data = {"filename": "file",
                           "block_migration": False,
                           "disk_over_commit": False,
                           'disk_available_mb': 1024}
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        self.mox.StubOutWithMock(conn, "_check_shared_storage_test_file")
        conn._check_shared_storage_test_file("file").AndReturn(False)

        self.mox.ReplayAll()
        self.assertRaises(exception.InvalidSharedStorage,
                          conn.check_can_live_migrate_source,
                          self.context, instance_ref, dest_check_data)

    def test_check_can_live_migrate_source_with_dest_not_enough_disk(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest = "fake_host_2"
        src = instance_ref['host']
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        self.mox.StubOutWithMock(conn, "_check_shared_storage_test_file")
        conn._check_shared_storage_test_file("file").AndReturn(False)

        self.mox.StubOutWithMock(conn, "get_instance_disk_info")
        conn.get_instance_disk_info(instance_ref["name"]).AndReturn(
                                            '[{"virt_disk_size":2}]')

        dest_check_data = {"filename": "file",
                           "disk_available_mb": 0,
                           "block_migration": True,
                           "disk_over_commit": False}
        self.mox.ReplayAll()
        self.assertRaises(exception.MigrationError,
                          conn.check_can_live_migrate_source,
                          self.context, instance_ref, dest_check_data)

    def test_live_migration_raises_exception(self):
        # Confirms recover method is called when exceptions are raised.
        # Preparing data
        self.compute = importutils.import_object(CONF.compute_manager)
        instance_dict = {'host': 'fake',
                         'power_state': power_state.RUNNING,
                         'vm_state': vm_states.ACTIVE}
        instance_ref = db.instance_create(self.context, self.test_instance)
        instance_ref = db.instance_update(self.context, instance_ref['uuid'],
                                          instance_dict)

        # Preparing mocks
        vdmock = self.mox.CreateMock(libvirt.virDomain)
        self.mox.StubOutWithMock(vdmock, "migrateToURI")
        _bandwidth = CONF.live_migration_bandwidth
        vdmock.migrateToURI(CONF.live_migration_uri % 'dest',
                            mox.IgnoreArg(),
                            None,
                            _bandwidth).AndRaise(libvirt.libvirtError('ERR'))

        def fake_lookup(instance_name):
            if instance_name == instance_ref['name']:
                return vdmock

        self.create_fake_libvirt_mock(lookupByName=fake_lookup)
        self.mox.StubOutWithMock(self.compute, "_rollback_live_migration")
        self.compute._rollback_live_migration(self.context, instance_ref,
                                              'dest', False)

        #start test
        self.mox.ReplayAll()
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.assertRaises(libvirt.libvirtError,
                      conn._live_migration,
                      self.context, instance_ref, 'dest', False,
                      self.compute._rollback_live_migration)

        instance_ref = db.instance_get(self.context, instance_ref['id'])
        self.assertTrue(instance_ref['vm_state'] == vm_states.ACTIVE)
        self.assertTrue(instance_ref['power_state'] == power_state.RUNNING)

        db.instance_destroy(self.context, instance_ref['uuid'])

    def test_pre_live_migration_works_correctly_mocked(self):
        # Creating testdata
        vol = {'block_device_mapping': [
                  {'connection_info': 'dummy', 'mount_device': '/dev/sda'},
                  {'connection_info': 'dummy', 'mount_device': '/dev/sdb'}]}
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        class FakeNetworkInfo():
            def fixed_ips(self):
                return ["test_ip_addr"]

        inst_ref = {'id': 'foo'}
        c = context.get_admin_context()
        nw_info = FakeNetworkInfo()

        # Creating mocks
        self.mox.StubOutWithMock(driver, "block_device_info_get_mapping")
        driver.block_device_info_get_mapping(vol
            ).AndReturn(vol['block_device_mapping'])
        self.mox.StubOutWithMock(conn, "volume_driver_method")
        for v in vol['block_device_mapping']:
            disk_info = {
                'bus': "scsi",
                'dev': v['mount_device'].rpartition("/")[2],
                'type': "disk"
                }
            conn.volume_driver_method('connect_volume',
                                      v['connection_info'],
                                      disk_info)
        self.mox.StubOutWithMock(conn, 'plug_vifs')
        conn.plug_vifs(mox.IsA(inst_ref), nw_info)

        self.mox.ReplayAll()
        result = conn.pre_live_migration(c, inst_ref, vol, nw_info)
        self.assertEqual(result, None)

    def test_pre_live_migration_vol_backed_works_correctly_mocked(self):
        # Creating testdata, using temp dir.
        with utils.tempdir() as tmpdir:
            self.flags(instances_path=tmpdir)
            vol = {'block_device_mapping': [
                  {'connection_info': 'dummy', 'mount_device': '/dev/sda'},
                  {'connection_info': 'dummy', 'mount_device': '/dev/sdb'}]}
            conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

            class FakeNetworkInfo():
                def fixed_ips(self):
                    return ["test_ip_addr"]
            inst_ref = db.instance_create(self.context, self.test_instance)
            c = context.get_admin_context()
            nw_info = FakeNetworkInfo()
            # Creating mocks
            self.mox.StubOutWithMock(conn, "volume_driver_method")
            for v in vol['block_device_mapping']:
                disk_info = {
                    'bus': "scsi",
                    'dev': v['mount_device'].rpartition("/")[2],
                    'type': "disk"
                    }
                conn.volume_driver_method('connect_volume',
                                          v['connection_info'],
                                          disk_info)
            self.mox.StubOutWithMock(conn, 'plug_vifs')
            conn.plug_vifs(mox.IsA(inst_ref), nw_info)
            self.mox.ReplayAll()
            migrate_data = {'is_shared_storage': False,
                            'is_volume_backed': True,
                            'block_migration': False
                            }
            ret = conn.pre_live_migration(c, inst_ref, vol, nw_info,
                                          migrate_data)
            self.assertEqual(ret, None)
            self.assertTrue(os.path.exists('%s/%s/' % (tmpdir,
                                                       inst_ref['uuid'])))
        db.instance_destroy(self.context, inst_ref['uuid'])

    def test_pre_block_migration_works_correctly(self):
        # Replace instances_path since this testcase creates tmpfile
        with utils.tempdir() as tmpdir:
            self.flags(instances_path=tmpdir)

            # Test data
            instance_ref = db.instance_create(self.context, self.test_instance)
            dummy_info = [{'path': '%s/disk' % tmpdir,
                           'disk_size': 10737418240,
                           'type': 'raw',
                           'backing_file': ''},
                          {'backing_file': 'otherdisk_1234567',
                           'path': '%s/otherdisk' % tmpdir,
                           'virt_disk_size': 10737418240}]
            dummyjson = json.dumps(dummy_info)

            # qemu-img should be mockd since test environment might not have
            # large disk space.
            self.mox.StubOutWithMock(imagebackend.Image, 'cache')
            imagebackend.Image.cache(context=mox.IgnoreArg(),
                                     fetch_func=mox.IgnoreArg(),
                                     filename='otherdisk',
                                     image_id=self.test_instance['image_ref'],
                                     project_id='fake',
                                     size=10737418240L,
                                     user_id=None).AndReturn(None)
            self.mox.ReplayAll()

            conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
            conn.pre_block_migration(self.context, instance_ref,
                                     dummyjson)

            self.assertTrue(os.path.exists('%s/%s/' %
                                           (tmpdir, instance_ref['uuid'])))

        db.instance_destroy(self.context, instance_ref['uuid'])

    def test_get_instance_disk_info_works_correctly(self):
        # Test data
        instance_ref = db.instance_create(self.context, self.test_instance)
        dummyxml = ("<domain type='kvm'><name>instance-0000000a</name>"
                    "<devices>"
                    "<disk type='file'><driver name='qemu' type='raw'/>"
                    "<source file='/test/disk'/>"
                    "<target dev='vda' bus='virtio'/></disk>"
                    "<disk type='file'><driver name='qemu' type='qcow2'/>"
                    "<source file='/test/disk.local'/>"
                    "<target dev='vdb' bus='virtio'/></disk>"
                    "</devices></domain>")

        # Preparing mocks
        vdmock = self.mox.CreateMock(libvirt.virDomain)
        self.mox.StubOutWithMock(vdmock, "XMLDesc")
        vdmock.XMLDesc(0).AndReturn(dummyxml)

        def fake_lookup(instance_name):
            if instance_name == instance_ref['name']:
                return vdmock
        self.create_fake_libvirt_mock(lookupByName=fake_lookup)

        GB = 1024 * 1024 * 1024
        fake_libvirt_utils.disk_sizes['/test/disk'] = 10 * GB
        fake_libvirt_utils.disk_sizes['/test/disk.local'] = 20 * GB
        fake_libvirt_utils.disk_backing_files['/test/disk.local'] = 'file'

        self.mox.StubOutWithMock(os.path, "getsize")
        os.path.getsize('/test/disk').AndReturn((10737418240))
        os.path.getsize('/test/disk.local').AndReturn((21474836480))

        ret = ("image: /test/disk\n"
               "file format: raw\n"
               "virtual size: 20G (21474836480 bytes)\n"
               "disk size: 3.1G\n"
               "cluster_size: 2097152\n"
               "backing file: /test/dummy (actual path: /backing/file)\n")

        self.mox.StubOutWithMock(os.path, "exists")
        os.path.exists('/test/disk.local').AndReturn(True)

        self.mox.StubOutWithMock(utils, "execute")
        utils.execute('env', 'LC_ALL=C', 'LANG=C', 'qemu-img', 'info',
                      '/test/disk.local').AndReturn((ret, ''))

        self.mox.ReplayAll()
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        info = conn.get_instance_disk_info(instance_ref['name'])
        info = jsonutils.loads(info)
        self.assertEquals(info[0]['type'], 'raw')
        self.assertEquals(info[0]['path'], '/test/disk')
        self.assertEquals(info[0]['disk_size'], 10737418240)
        self.assertEquals(info[0]['backing_file'], "")
        self.assertEquals(info[1]['type'], 'qcow2')
        self.assertEquals(info[1]['path'], '/test/disk.local')
        self.assertEquals(info[1]['virt_disk_size'], 21474836480)
        self.assertEquals(info[1]['backing_file'], "file")

        db.instance_destroy(self.context, instance_ref['uuid'])

    def test_spawn_with_network_info(self):
        # Preparing mocks
        def fake_none(*args, **kwargs):
            return

        def fake_getLibVersion():
            return 9007

        def fake_getCapabilities():
            return """
            <capabilities>
                <host>
                    <uuid>cef19ce0-0ca2-11df-855d-b19fbce37686</uuid>
                    <cpu>
                      <arch>x86_64</arch>
                      <model>Penryn</model>
                      <vendor>Intel</vendor>
                      <topology sockets='1' cores='2' threads='1'/>
                      <feature name='xtpr'/>
                    </cpu>
                </host>
            </capabilities>
            """

        # _fake_network_info must be called before create_fake_libvirt_mock(),
        # as _fake_network_info calls importutils.import_class() and
        # create_fake_libvirt_mock() mocks importutils.import_class().
        network_info = _fake_network_info(self.stubs, 1)
        self.create_fake_libvirt_mock(getLibVersion=fake_getLibVersion,
                                      getCapabilities=fake_getCapabilities)

        instance_ref = self.test_instance
        instance_ref['image_ref'] = 123456  # we send an int to test sha1 call
        instance = db.instance_create(self.context, instance_ref)

        # Mock out the get_info method of the LibvirtDriver so that the polling
        # in the spawn method of the LibvirtDriver returns immediately
        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, 'get_info')
        libvirt_driver.LibvirtDriver.get_info(instance
            ).AndReturn({'state': power_state.RUNNING})

        # Start test
        self.mox.ReplayAll()
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(conn.firewall_driver,
                       'setup_basic_filtering',
                       fake_none)
        self.stubs.Set(conn.firewall_driver,
                       'prepare_instance_filter',
                       fake_none)
        self.stubs.Set(imagebackend.Image,
                       'cache',
                       fake_none)

        conn.spawn(self.context, instance, None, [], 'herp',
                       network_info=network_info)

        path = os.path.join(CONF.instances_path, instance['name'])
        if os.path.isdir(path):
            shutil.rmtree(path)

        path = os.path.join(CONF.instances_path, CONF.base_dir_name)
        if os.path.isdir(path):
            shutil.rmtree(os.path.join(CONF.instances_path,
                                       CONF.base_dir_name))

    def test_spawn_without_image_meta(self):
        self.create_image_called = False

        def fake_none(*args, **kwargs):
            return

        def fake_create_image(*args, **kwargs):
            self.create_image_called = True

        def fake_get_info(instance):
            return {'state': power_state.RUNNING}

        instance_ref = self.test_instance
        instance_ref['image_ref'] = 1
        instance = db.instance_create(self.context, instance_ref)

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(conn, 'to_xml', fake_none)
        self.stubs.Set(conn, '_create_image', fake_create_image)
        self.stubs.Set(conn, '_create_domain_and_network', fake_none)
        self.stubs.Set(conn, 'get_info', fake_get_info)

        conn.spawn(self.context, instance, None, [], None)
        self.assertFalse(self.create_image_called)

        conn.spawn(self.context,
                   instance,
                   {'id': instance['image_ref']},
                   [],
                   None)
        self.assertTrue(self.create_image_called)

    def test_create_image_plain(self):
        gotFiles = []

        def fake_image(self, instance, name, image_type=''):
            class FakeImage(imagebackend.Image):
                def __init__(self, instance, name):
                    self.path = os.path.join(instance['name'], name)

                def create_image(self, prepare_template, base,
                                 size, *args, **kwargs):
                    pass

                def cache(self, fetch_func, filename, size=None,
                          *args, **kwargs):
                    gotFiles.append({'filename': filename,
                                     'size': size})

                def snapshot(self, name):
                    pass

            return FakeImage(instance, name)

        def fake_none(*args, **kwargs):
            return

        def fake_get_info(instance):
            return {'state': power_state.RUNNING}

        # Stop 'libvirt_driver._create_image' touching filesystem
        self.stubs.Set(nova.virt.libvirt.imagebackend.Backend, "image",
                       fake_image)

        instance_ref = self.test_instance
        instance_ref['image_ref'] = 1
        instance = db.instance_create(self.context, instance_ref)

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(conn, 'to_xml', fake_none)
        self.stubs.Set(conn, '_create_domain_and_network', fake_none)
        self.stubs.Set(conn, 'get_info', fake_get_info)

        image_meta = {'id': instance['image_ref']}
        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance,
                                            None,
                                            image_meta)
        xml = conn.to_xml(instance, None,
                          disk_info, image_meta)
        conn._create_image(context, instance, xml,
                           disk_info['mapping'])

        wantFiles = [
            {'filename': '356a192b7913b04c54574d18c28d46e6395428ab',
             'size': 10 * 1024 * 1024 * 1024},
            {'filename': 'ephemeral_20_default',
             'size': 20 * 1024 * 1024 * 1024},
            ]
        self.assertEquals(gotFiles, wantFiles)

    def test_create_image_with_swap(self):
        gotFiles = []

        def fake_image(self, instance, name, image_type=''):
            class FakeImage(imagebackend.Image):
                def __init__(self, instance, name):
                    self.path = os.path.join(instance['name'], name)

                def create_image(self, prepare_template, base,
                                 size, *args, **kwargs):
                    pass

                def cache(self, fetch_func, filename, size=None,
                          *args, **kwargs):
                    gotFiles.append({'filename': filename,
                                     'size': size})

                def snapshot(self, name):
                    pass

            return FakeImage(instance, name)

        def fake_none(*args, **kwargs):
            return

        def fake_get_info(instance):
            return {'state': power_state.RUNNING}

        # Stop 'libvirt_driver._create_image' touching filesystem
        self.stubs.Set(nova.virt.libvirt.imagebackend.Backend, "image",
                       fake_image)

        instance_ref = self.test_instance
        instance_ref['image_ref'] = 1
        instance = db.instance_create(self.context, instance_ref)

        # Turn on some swap to exercise that codepath in _create_image
        instance['instance_type']['swap'] = 500

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(conn, 'to_xml', fake_none)
        self.stubs.Set(conn, '_create_domain_and_network', fake_none)
        self.stubs.Set(conn, 'get_info', fake_get_info)

        image_meta = {'id': instance['image_ref']}
        disk_info = blockinfo.get_disk_info(CONF.libvirt_type,
                                            instance,
                                            None,
                                            image_meta)
        xml = conn.to_xml(instance, None,
                          disk_info, image_meta)
        conn._create_image(context, instance, xml,
                           disk_info['mapping'])

        wantFiles = [
            {'filename': '356a192b7913b04c54574d18c28d46e6395428ab',
             'size': 10 * 1024 * 1024 * 1024},
            {'filename': 'ephemeral_20_default',
             'size': 20 * 1024 * 1024 * 1024},
            {'filename': 'swap_500',
             'size': 500 * 1024 * 1024},
            ]
        self.assertEquals(gotFiles, wantFiles)

    def test_get_console_output_file(self):
        fake_libvirt_utils.files['console.log'] = '01234567890'

        with utils.tempdir() as tmpdir:
            self.flags(instances_path=tmpdir)

            instance_ref = self.test_instance
            instance_ref['image_ref'] = 123456
            instance = db.instance_create(self.context, instance_ref)

            console_dir = (os.path.join(tmpdir, instance['name']))
            console_log = '%s/console.log' % (console_dir)
            fake_dom_xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                        </disk>
                        <console type='file'>
                            <source path='%s'/>
                            <target port='0'/>
                        </console>
                    </devices>
                </domain>
            """ % console_log

            def fake_lookup(id):
                return FakeVirtDomain(fake_dom_xml)

            self.create_fake_libvirt_mock()
            libvirt_driver.LibvirtDriver._conn.lookupByName = fake_lookup

            conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

            try:
                prev_max = libvirt_driver.MAX_CONSOLE_BYTES
                libvirt_driver.MAX_CONSOLE_BYTES = 5
                output = conn.get_console_output(instance)
            finally:
                libvirt_driver.MAX_CONSOLE_BYTES = prev_max

            self.assertEquals('67890', output)

    def test_get_console_output_pty(self):
        fake_libvirt_utils.files['pty'] = '01234567890'

        with utils.tempdir() as tmpdir:
            self.flags(instances_path=tmpdir)

            instance_ref = self.test_instance
            instance_ref['image_ref'] = 123456
            instance = db.instance_create(self.context, instance_ref)

            console_dir = (os.path.join(tmpdir, instance['name']))
            pty_file = '%s/fake_pty' % (console_dir)
            fake_dom_xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                        </disk>
                        <console type='pty'>
                            <source path='%s'/>
                            <target port='0'/>
                        </console>
                    </devices>
                </domain>
            """ % pty_file

            def fake_lookup(id):
                return FakeVirtDomain(fake_dom_xml)

            def _fake_flush(self, fake_pty):
                return 'foo'

            def _fake_append_to_file(self, data, fpath):
                return 'pty'

            self.create_fake_libvirt_mock()
            libvirt_driver.LibvirtDriver._conn.lookupByName = fake_lookup
            libvirt_driver.LibvirtDriver._flush_libvirt_console = _fake_flush
            libvirt_driver.LibvirtDriver._append_to_file = _fake_append_to_file

            conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

            try:
                prev_max = libvirt_driver.MAX_CONSOLE_BYTES
                libvirt_driver.MAX_CONSOLE_BYTES = 5
                output = conn.get_console_output(instance)
            finally:
                libvirt_driver.MAX_CONSOLE_BYTES = prev_max

            self.assertEquals('67890', output)

    def test_get_host_ip_addr(self):
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        ip = conn.get_host_ip_addr()
        self.assertEquals(ip, CONF.my_ip)

    def test_broken_connection(self):
        for (error, domain) in (
                (libvirt.VIR_ERR_SYSTEM_ERROR, libvirt.VIR_FROM_REMOTE),
                (libvirt.VIR_ERR_SYSTEM_ERROR, libvirt.VIR_FROM_RPC)):

            conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

            self.mox.StubOutWithMock(conn, "_wrapped_conn")
            self.mox.StubOutWithMock(conn._wrapped_conn, "getLibVersion")
            self.mox.StubOutWithMock(libvirt.libvirtError, "get_error_code")
            self.mox.StubOutWithMock(libvirt.libvirtError, "get_error_domain")

            conn._wrapped_conn.getLibVersion().AndRaise(
                    libvirt.libvirtError("fake failure"))

            libvirt.libvirtError.get_error_code().AndReturn(error)
            libvirt.libvirtError.get_error_domain().AndReturn(domain)

            self.mox.ReplayAll()

            self.assertFalse(conn._test_connection())

            self.mox.UnsetStubs()

    def test_immediate_delete(self):
        def fake_lookup_by_name(instance_name):
            raise exception.InstanceNotFound(instance_id=instance_name)

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(conn, '_lookup_by_name', fake_lookup_by_name)

        instance = db.instance_create(self.context, self.test_instance)
        conn.destroy(instance, {})

    def test_destroy_removes_disk(self):
        instance = {"name": "instancename", "id": "instanceid",
                    "uuid": "875a8070-d0b9-4949-8b31-104d125c9a64"}

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver,
                                 '_undefine_domain')
        libvirt_driver.LibvirtDriver._undefine_domain(instance)
        self.mox.StubOutWithMock(shutil, "rmtree")
        shutil.rmtree(os.path.join(CONF.instances_path, instance['name']))
        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_cleanup_lvm')
        libvirt_driver.LibvirtDriver._cleanup_lvm(instance)

        # Start test
        self.mox.ReplayAll()

        def fake_destroy(instance):
            pass

        def fake_os_path_exists(path):
            return True

        def fake_unplug_vifs(instance, network_info):
            pass

        def fake_unfilter_instance(instance, network_info):
            pass

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        self.stubs.Set(conn, '_destroy', fake_destroy)
        self.stubs.Set(conn, 'unplug_vifs', fake_unplug_vifs)
        self.stubs.Set(conn.firewall_driver,
                       'unfilter_instance', fake_unfilter_instance)
        self.stubs.Set(os.path, 'exists', fake_os_path_exists)
        conn.destroy(instance, [])

    def test_destroy_not_removes_disk(self):
        instance = {"name": "instancename", "id": "instanceid",
                    "uuid": "875a8070-d0b9-4949-8b31-104d125c9a64"}

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver,
                                 '_undefine_domain')
        libvirt_driver.LibvirtDriver._undefine_domain(instance)

        # Start test
        self.mox.ReplayAll()

        def fake_destroy(instance):
            pass

        def fake_os_path_exists(path):
            return True

        def fake_unplug_vifs(instance, network_info):
            pass

        def fake_unfilter_instance(instance, network_info):
            pass

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        self.stubs.Set(conn, '_destroy', fake_destroy)
        self.stubs.Set(conn, 'unplug_vifs', fake_unplug_vifs)
        self.stubs.Set(conn.firewall_driver,
                       'unfilter_instance', fake_unfilter_instance)
        self.stubs.Set(os.path, 'exists', fake_os_path_exists)
        conn.destroy(instance, [], None, False)

    def test_destroy_undefines(self):
        mock = self.mox.CreateMock(libvirt.virDomain)
        mock.ID()
        mock.destroy()
        mock.undefineFlags(1).AndReturn(1)

        self.mox.ReplayAll()

        def fake_lookup_by_name(instance_name):
            return mock

        def fake_get_info(instance_name):
            return {'state': power_state.SHUTDOWN, 'id': -1}

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(conn, '_lookup_by_name', fake_lookup_by_name)
        self.stubs.Set(conn, 'get_info', fake_get_info)
        instance = {"name": "instancename", "id": "instanceid",
                    "uuid": "875a8070-d0b9-4949-8b31-104d125c9a64"}
        conn.destroy(instance, [])

    def test_destroy_undefines_no_undefine_flags(self):
        mock = self.mox.CreateMock(libvirt.virDomain)
        mock.ID()
        mock.destroy()
        mock.undefineFlags(1).AndRaise(libvirt.libvirtError('Err'))
        mock.undefine()

        self.mox.ReplayAll()

        def fake_lookup_by_name(instance_name):
            return mock

        def fake_get_info(instance_name):
            return {'state': power_state.SHUTDOWN, 'id': -1}

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(conn, '_lookup_by_name', fake_lookup_by_name)
        self.stubs.Set(conn, 'get_info', fake_get_info)
        instance = {"name": "instancename", "id": "instanceid",
                    "uuid": "875a8070-d0b9-4949-8b31-104d125c9a64"}
        conn.destroy(instance, [])

    def test_destroy_undefines_no_attribute_with_managed_save(self):
        mock = self.mox.CreateMock(libvirt.virDomain)
        mock.ID()
        mock.destroy()
        mock.undefineFlags(1).AndRaise(AttributeError())
        mock.hasManagedSaveImage(0).AndReturn(True)
        mock.managedSaveRemove(0)
        mock.undefine()

        self.mox.ReplayAll()

        def fake_lookup_by_name(instance_name):
            return mock

        def fake_get_info(instance_name):
            return {'state': power_state.SHUTDOWN, 'id': -1}

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(conn, '_lookup_by_name', fake_lookup_by_name)
        self.stubs.Set(conn, 'get_info', fake_get_info)
        instance = {"name": "instancename", "id": "instanceid",
                    "uuid": "875a8070-d0b9-4949-8b31-104d125c9a64"}
        conn.destroy(instance, [])

    def test_destroy_undefines_no_attribute_no_managed_save(self):
        mock = self.mox.CreateMock(libvirt.virDomain)
        mock.ID()
        mock.destroy()
        mock.undefineFlags(1).AndRaise(AttributeError())
        mock.hasManagedSaveImage(0).AndRaise(AttributeError())
        mock.undefine()

        self.mox.ReplayAll()

        def fake_lookup_by_name(instance_name):
            return mock

        def fake_get_info(instance_name):
            return {'state': power_state.SHUTDOWN, 'id': -1}

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(conn, '_lookup_by_name', fake_lookup_by_name)
        self.stubs.Set(conn, 'get_info', fake_get_info)
        instance = {"name": "instancename", "id": "instanceid",
                    "uuid": "875a8070-d0b9-4949-8b31-104d125c9a64"}
        conn.destroy(instance, [])

    def test_private_destroy_not_found(self):
        mock = self.mox.CreateMock(libvirt.virDomain)
        mock.ID()
        mock.destroy()
        self.mox.ReplayAll()

        def fake_lookup_by_name(instance_name):
            return mock

        def fake_get_info(instance_name):
            raise exception.InstanceNotFound(instance_id=instance_name)

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(conn, '_lookup_by_name', fake_lookup_by_name)
        self.stubs.Set(conn, 'get_info', fake_get_info)
        instance = {"name": "instancename", "id": "instanceid",
                    "uuid": "875a8070-d0b9-4949-8b31-104d125c9a64"}
        # NOTE(vish): verifies destroy doesn't raise if the instance disappears
        conn._destroy(instance)

    def test_disk_over_committed_size_total(self):
        # Ensure destroy calls managedSaveRemove for saved instance.
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        def list_instances():
            return ['fake1', 'fake2']
        self.stubs.Set(conn, 'list_instances', list_instances)

        fake_disks = {'fake1': [{'type': 'qcow2', 'path': '/somepath/disk1',
                                 'virt_disk_size': '10737418240',
                                 'backing_file': '/somepath/disk1',
                                 'disk_size':'83886080'}],
                      'fake2': [{'type': 'raw', 'path': '/somepath/disk2',
                                 'virt_disk_size': '10737418240',
                                 'backing_file': '/somepath/disk2',
                                 'disk_size':'10737418240'}]}

        def get_info(instance_name):
            return jsonutils.dumps(fake_disks.get(instance_name))
        self.stubs.Set(conn, 'get_instance_disk_info', get_info)

        result = conn.get_disk_over_committed_size_total()
        self.assertEqual(result, 10653532160)

    def test_cpu_info(self):
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        def get_host_capabilities_stub(self):
            cpu = vconfig.LibvirtConfigCPU()
            cpu.model = "Opteron_G4"
            cpu.vendor = "AMD"
            cpu.arch = "x86_64"

            cpu.cores = 2
            cpu.threads = 1
            cpu.sockets = 4

            cpu.add_feature(vconfig.LibvirtConfigCPUFeature("extapic"))
            cpu.add_feature(vconfig.LibvirtConfigCPUFeature("3dnow"))

            caps = vconfig.LibvirtConfigCaps()
            caps.host = vconfig.LibvirtConfigCapsHost()
            caps.host.cpu = cpu

            guest = vconfig.LibvirtConfigGuest()
            guest.ostype = vm_mode.HVM
            guest.arch = "x86_64"
            guest.domtype = ["kvm"]
            caps.guests.append(guest)

            guest = vconfig.LibvirtConfigGuest()
            guest.ostype = vm_mode.HVM
            guest.arch = "i686"
            guest.domtype = ["kvm"]
            caps.guests.append(guest)

            return caps

        self.stubs.Set(libvirt_driver.LibvirtDriver,
                       'get_host_capabilities',
                       get_host_capabilities_stub)

        want = {"vendor": "AMD",
                "features": ["extapic", "3dnow"],
                "model": "Opteron_G4",
                "arch": "x86_64",
                "topology": {"cores": 2, "threads": 1, "sockets": 4}}
        got = jsonutils.loads(conn.get_cpu_info())
        self.assertEqual(want, got)

    def test_diagnostic_vcpus_exception(self):
        xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                            <target dev='vda' bus='virtio'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/1'/>
                            <target dev='vdb' bus='virtio'/>
                        </disk>
                        <interface type='network'>
                            <mac address='52:54:00:a4:38:38'/>
                            <source network='default'/>
                            <target dev='vnet0'/>
                        </interface>
                    </devices>
                </domain>
            """

        class DiagFakeDomain(FakeVirtDomain):

            def __init__(self):
                super(DiagFakeDomain, self).__init__(fake_xml=xml)

            def vcpus(self):
                raise libvirt.libvirtError('vcpus missing')

            def blockStats(self, path):
                return (169L, 688640L, 0L, 0L, -1L)

            def interfaceStats(self, path):
                return (4408L, 82L, 0L, 0L, 0L, 0L, 0L, 0L)

            def memoryStats(self):
                return {'actual': 220160L, 'rss': 200164L}

            def maxMemory(self):
                return 280160L

        def fake_lookup_name(name):
            return DiagFakeDomain()

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = fake_lookup_name

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        actual = conn.get_diagnostics({"name": "testvirt"})
        expect = {'vda_read': 688640L,
                  'vda_read_req': 169L,
                  'vda_write': 0L,
                  'vda_write_req': 0L,
                  'vda_errors': -1L,
                  'vdb_read': 688640L,
                  'vdb_read_req': 169L,
                  'vdb_write': 0L,
                  'vdb_write_req': 0L,
                  'vdb_errors': -1L,
                  'memory': 280160L,
                  'memory-actual': 220160L,
                  'memory-rss': 200164L,
                  'vnet0_rx': 4408L,
                  'vnet0_rx_drop': 0L,
                  'vnet0_rx_errors': 0L,
                  'vnet0_rx_packets': 82L,
                  'vnet0_tx': 0L,
                  'vnet0_tx_drop': 0L,
                  'vnet0_tx_errors': 0L,
                  'vnet0_tx_packets': 0L,
                  }
        self.assertEqual(actual, expect)

    def test_diagnostic_blockstats_exception(self):
        xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                            <target dev='vda' bus='virtio'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/1'/>
                            <target dev='vdb' bus='virtio'/>
                        </disk>
                        <interface type='network'>
                            <mac address='52:54:00:a4:38:38'/>
                            <source network='default'/>
                            <target dev='vnet0'/>
                        </interface>
                    </devices>
                </domain>
            """

        class DiagFakeDomain(FakeVirtDomain):

            def __init__(self):
                super(DiagFakeDomain, self).__init__(fake_xml=xml)

            def vcpus(self):
                return ([(0, 1, 15340000000L, 0),
                         (1, 1, 1640000000L, 0),
                         (2, 1, 3040000000L, 0),
                         (3, 1, 1420000000L, 0)],
                        [(True, False),
                         (True, False),
                         (True, False),
                         (True, False)])

            def blockStats(self, path):
                raise libvirt.libvirtError('blockStats missing')

            def interfaceStats(self, path):
                return (4408L, 82L, 0L, 0L, 0L, 0L, 0L, 0L)

            def memoryStats(self):
                return {'actual': 220160L, 'rss': 200164L}

            def maxMemory(self):
                return 280160L

        def fake_lookup_name(name):
            return DiagFakeDomain()

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = fake_lookup_name

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        actual = conn.get_diagnostics({"name": "testvirt"})
        expect = {'cpu0_time': 15340000000L,
                  'cpu1_time': 1640000000L,
                  'cpu2_time': 3040000000L,
                  'cpu3_time': 1420000000L,
                  'memory': 280160L,
                  'memory-actual': 220160L,
                  'memory-rss': 200164L,
                  'vnet0_rx': 4408L,
                  'vnet0_rx_drop': 0L,
                  'vnet0_rx_errors': 0L,
                  'vnet0_rx_packets': 82L,
                  'vnet0_tx': 0L,
                  'vnet0_tx_drop': 0L,
                  'vnet0_tx_errors': 0L,
                  'vnet0_tx_packets': 0L,
                  }
        self.assertEqual(actual, expect)

    def test_diagnostic_interfacestats_exception(self):
        xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                            <target dev='vda' bus='virtio'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/1'/>
                            <target dev='vdb' bus='virtio'/>
                        </disk>
                        <interface type='network'>
                            <mac address='52:54:00:a4:38:38'/>
                            <source network='default'/>
                            <target dev='vnet0'/>
                        </interface>
                    </devices>
                </domain>
            """

        class DiagFakeDomain(FakeVirtDomain):

            def __init__(self):
                super(DiagFakeDomain, self).__init__(fake_xml=xml)

            def vcpus(self):
                return ([(0, 1, 15340000000L, 0),
                         (1, 1, 1640000000L, 0),
                         (2, 1, 3040000000L, 0),
                         (3, 1, 1420000000L, 0)],
                        [(True, False),
                         (True, False),
                         (True, False),
                         (True, False)])

            def blockStats(self, path):
                return (169L, 688640L, 0L, 0L, -1L)

            def interfaceStats(self, path):
                raise libvirt.libvirtError('interfaceStat missing')

            def memoryStats(self):
                return {'actual': 220160L, 'rss': 200164L}

            def maxMemory(self):
                return 280160L

        def fake_lookup_name(name):
            return DiagFakeDomain()

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = fake_lookup_name

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        actual = conn.get_diagnostics({"name": "testvirt"})
        expect = {'cpu0_time': 15340000000L,
                  'cpu1_time': 1640000000L,
                  'cpu2_time': 3040000000L,
                  'cpu3_time': 1420000000L,
                  'vda_read': 688640L,
                  'vda_read_req': 169L,
                  'vda_write': 0L,
                  'vda_write_req': 0L,
                  'vda_errors': -1L,
                  'vdb_read': 688640L,
                  'vdb_read_req': 169L,
                  'vdb_write': 0L,
                  'vdb_write_req': 0L,
                  'vdb_errors': -1L,
                  'memory': 280160L,
                  'memory-actual': 220160L,
                  'memory-rss': 200164L,
                  }
        self.assertEqual(actual, expect)

    def test_diagnostic_memorystats_exception(self):
        xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                            <target dev='vda' bus='virtio'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/1'/>
                            <target dev='vdb' bus='virtio'/>
                        </disk>
                        <interface type='network'>
                            <mac address='52:54:00:a4:38:38'/>
                            <source network='default'/>
                            <target dev='vnet0'/>
                        </interface>
                    </devices>
                </domain>
            """

        class DiagFakeDomain(FakeVirtDomain):

            def __init__(self):
                super(DiagFakeDomain, self).__init__(fake_xml=xml)

            def vcpus(self):
                return ([(0, 1, 15340000000L, 0),
                         (1, 1, 1640000000L, 0),
                         (2, 1, 3040000000L, 0),
                         (3, 1, 1420000000L, 0)],
                        [(True, False),
                         (True, False),
                         (True, False),
                         (True, False)])

            def blockStats(self, path):
                return (169L, 688640L, 0L, 0L, -1L)

            def interfaceStats(self, path):
                return (4408L, 82L, 0L, 0L, 0L, 0L, 0L, 0L)

            def memoryStats(self):
                raise libvirt.libvirtError('memoryStats missing')

            def maxMemory(self):
                return 280160L

        def fake_lookup_name(name):
            return DiagFakeDomain()

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = fake_lookup_name

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        actual = conn.get_diagnostics({"name": "testvirt"})
        expect = {'cpu0_time': 15340000000L,
                  'cpu1_time': 1640000000L,
                  'cpu2_time': 3040000000L,
                  'cpu3_time': 1420000000L,
                  'vda_read': 688640L,
                  'vda_read_req': 169L,
                  'vda_write': 0L,
                  'vda_write_req': 0L,
                  'vda_errors': -1L,
                  'vdb_read': 688640L,
                  'vdb_read_req': 169L,
                  'vdb_write': 0L,
                  'vdb_write_req': 0L,
                  'vdb_errors': -1L,
                  'memory': 280160L,
                  'vnet0_rx': 4408L,
                  'vnet0_rx_drop': 0L,
                  'vnet0_rx_errors': 0L,
                  'vnet0_rx_packets': 82L,
                  'vnet0_tx': 0L,
                  'vnet0_tx_drop': 0L,
                  'vnet0_tx_errors': 0L,
                  'vnet0_tx_packets': 0L,
                  }
        self.assertEqual(actual, expect)

    def test_diagnostic_full(self):
        xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                            <target dev='vda' bus='virtio'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/1'/>
                            <target dev='vdb' bus='virtio'/>
                        </disk>
                        <interface type='network'>
                            <mac address='52:54:00:a4:38:38'/>
                            <source network='default'/>
                            <target dev='vnet0'/>
                        </interface>
                    </devices>
                </domain>
            """

        class DiagFakeDomain(FakeVirtDomain):

            def __init__(self):
                super(DiagFakeDomain, self).__init__(fake_xml=xml)

            def vcpus(self):
                return ([(0, 1, 15340000000L, 0),
                         (1, 1, 1640000000L, 0),
                         (2, 1, 3040000000L, 0),
                         (3, 1, 1420000000L, 0)],
                        [(True, False),
                         (True, False),
                         (True, False),
                         (True, False)])

            def blockStats(self, path):
                return (169L, 688640L, 0L, 0L, -1L)

            def interfaceStats(self, path):
                return (4408L, 82L, 0L, 0L, 0L, 0L, 0L, 0L)

            def memoryStats(self):
                return {'actual': 220160L, 'rss': 200164L}

            def maxMemory(self):
                return 280160L

        def fake_lookup_name(name):
            return DiagFakeDomain()

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = fake_lookup_name

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        actual = conn.get_diagnostics({"name": "testvirt"})
        expect = {'cpu0_time': 15340000000L,
                  'cpu1_time': 1640000000L,
                  'cpu2_time': 3040000000L,
                  'cpu3_time': 1420000000L,
                  'vda_read': 688640L,
                  'vda_read_req': 169L,
                  'vda_write': 0L,
                  'vda_write_req': 0L,
                  'vda_errors': -1L,
                  'vdb_read': 688640L,
                  'vdb_read_req': 169L,
                  'vdb_write': 0L,
                  'vdb_write_req': 0L,
                  'vdb_errors': -1L,
                  'memory': 280160L,
                  'memory-actual': 220160L,
                  'memory-rss': 200164L,
                  'vnet0_rx': 4408L,
                  'vnet0_rx_drop': 0L,
                  'vnet0_rx_errors': 0L,
                  'vnet0_rx_packets': 82L,
                  'vnet0_tx': 0L,
                  'vnet0_tx_drop': 0L,
                  'vnet0_tx_errors': 0L,
                  'vnet0_tx_packets': 0L,
                  }
        self.assertEqual(actual, expect)

    def test_get_instance_capabilities(self):
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        def get_host_capabilities_stub(self):
            caps = vconfig.LibvirtConfigCaps()

            guest = vconfig.LibvirtConfigGuest()
            guest.ostype = 'hvm'
            guest.arch = 'x86_64'
            guest.domtype = ['kvm', 'qemu']
            caps.guests.append(guest)

            guest = vconfig.LibvirtConfigGuest()
            guest.ostype = 'hvm'
            guest.arch = 'i686'
            guest.domtype = ['kvm']
            caps.guests.append(guest)

            return caps

        self.stubs.Set(libvirt_driver.LibvirtDriver,
                       'get_host_capabilities',
                       get_host_capabilities_stub)

        want = [('x86_64', 'kvm', 'hvm'),
                ('x86_64', 'qemu', 'hvm'),
                ('i686', 'kvm', 'hvm')]
        got = conn.get_instance_capabilities()
        self.assertEqual(want, got)

    def test_event_dispatch(self):
        # Validate that the libvirt self-pipe for forwarding
        # events between threads is working sanely
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        got_events = []

        def handler(event):
            got_events.append(event)

        conn.register_event_listener(handler)

        conn._init_events_pipe()

        event1 = virtevent.LifecycleEvent(
            "cef19ce0-0ca2-11df-855d-b19fbce37686",
            virtevent.EVENT_LIFECYCLE_STARTED)
        event2 = virtevent.LifecycleEvent(
            "cef19ce0-0ca2-11df-855d-b19fbce37686",
            virtevent.EVENT_LIFECYCLE_PAUSED)
        conn._queue_event(event1)
        conn._queue_event(event2)
        conn._dispatch_events()

        want_events = [event1, event2]
        self.assertEqual(want_events, got_events)

        event3 = virtevent.LifecycleEvent(
            "cef19ce0-0ca2-11df-855d-b19fbce37686",
            virtevent.EVENT_LIFECYCLE_RESUMED)
        event4 = virtevent.LifecycleEvent(
            "cef19ce0-0ca2-11df-855d-b19fbce37686",
            virtevent.EVENT_LIFECYCLE_STOPPED)

        conn._queue_event(event3)
        conn._queue_event(event4)
        conn._dispatch_events()

        want_events = [event1, event2, event3, event4]
        self.assertEqual(want_events, got_events)

    def test_event_lifecycle(self):
        # Validate that libvirt events are correctly translated
        # to Nova events
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        got_events = []

        def handler(event):
            got_events.append(event)

        conn.register_event_listener(handler)
        conn._init_events_pipe()
        fake_dom_xml = """
                <domain type='kvm'>
                  <uuid>cef19ce0-0ca2-11df-855d-b19fbce37686</uuid>
                  <devices>
                    <disk type='file'>
                      <source file='filename'/>
                    </disk>
                  </devices>
                </domain>
            """
        dom = FakeVirtDomain(fake_dom_xml,
                             "cef19ce0-0ca2-11df-855d-b19fbce37686")

        conn._event_lifecycle_callback(conn._conn,
                                       dom,
                                       libvirt.VIR_DOMAIN_EVENT_STOPPED,
                                       0,
                                       conn)
        conn._dispatch_events()
        self.assertEqual(len(got_events), 1)
        self.assertEqual(type(got_events[0]), virtevent.LifecycleEvent)
        self.assertEqual(got_events[0].uuid,
                         "cef19ce0-0ca2-11df-855d-b19fbce37686")
        self.assertEqual(got_events[0].transition,
                         virtevent.EVENT_LIFECYCLE_STOPPED)

    def test_set_cache_mode(self):
        self.flags(disk_cachemodes=['file=directsync'])
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        fake_conf = FakeConfigGuestDisk()

        fake_conf.source_type = 'file'
        conn.set_cache_mode(fake_conf)
        self.assertEqual(fake_conf.driver_cache, 'directsync')

    def test_set_cache_mode_invalid_mode(self):
        self.flags(disk_cachemodes=['file=FAKE'])
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        fake_conf = FakeConfigGuestDisk()

        fake_conf.source_type = 'file'
        conn.set_cache_mode(fake_conf)
        self.assertEqual(fake_conf.driver_cache, None)

    def test_set_cache_mode_invalid_object(self):
        self.flags(disk_cachemodes=['file=directsync'])
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        fake_conf = FakeConfigGuest()

        fake_conf.driver_cache = 'fake'
        conn.set_cache_mode(fake_conf)
        self.assertEqual(fake_conf.driver_cache, 'fake')


class HostStateTestCase(test.TestCase):

    cpu_info = ('{"vendor": "Intel", "model": "pentium", "arch": "i686", '
                 '"features": ["ssse3", "monitor", "pni", "sse2", "sse", '
                 '"fxsr", "clflush", "pse36", "pat", "cmov", "mca", "pge", '
                 '"mtrr", "sep", "apic"], '
                 '"topology": {"cores": "1", "threads": "1", "sockets": "1"}}')
    instance_caps = [("x86_64", "kvm", "hvm"), ("i686", "kvm", "hvm")]

    class FakeConnection(object):
        """Fake connection object."""

        def get_vcpu_total(self):
            return 1

        def get_vcpu_used(self):
            return 0

        def get_cpu_info(self):
            return HostStateTestCase.cpu_info

        def get_local_gb_info(self):
            return {'total': 100, 'used': 20, 'free': 80}

        def get_memory_mb_total(self):
            return 497

        def get_memory_mb_used(self):
            return 88

        def get_hypervisor_type(self):
            return 'QEMU'

        def get_hypervisor_version(self):
            return 13091

        def get_hypervisor_hostname(self):
            return 'compute1'

        def get_host_uptime(self):
            return ('10:01:16 up  1:36,  6 users,  '
                    'load average: 0.21, 0.16, 0.19')

        def get_disk_available_least(self):
            return 13091

        def get_instance_capabilities(self):
            return HostStateTestCase.instance_caps

    def test_update_status(self):
        hs = libvirt_driver.HostState(self.FakeConnection())
        stats = hs._stats
        self.assertEquals(stats["vcpus"], 1)
        self.assertEquals(stats["vcpus_used"], 0)
        self.assertEquals(stats["cpu_info"],
                {"vendor": "Intel", "model": "pentium", "arch": "i686",
                 "features": ["ssse3", "monitor", "pni", "sse2", "sse",
                              "fxsr", "clflush", "pse36", "pat", "cmov",
                              "mca", "pge", "mtrr", "sep", "apic"],
                 "topology": {"cores": "1", "threads": "1", "sockets": "1"}
                })
        self.assertEquals(stats["disk_total"], 100)
        self.assertEquals(stats["disk_used"], 20)
        self.assertEquals(stats["disk_available"], 80)
        self.assertEquals(stats["host_memory_total"], 497)
        self.assertEquals(stats["host_memory_free"], 409)
        self.assertEquals(stats["hypervisor_type"], 'QEMU')
        self.assertEquals(stats["hypervisor_version"], 13091)
        self.assertEquals(stats["hypervisor_hostname"], 'compute1')


class NWFilterFakes:
    def __init__(self):
        self.filters = {}

    def nwfilterLookupByName(self, name):
        if name in self.filters:
            return self.filters[name]
        raise libvirt.libvirtError('Filter Not Found')

    def filterDefineXMLMock(self, xml):
        class FakeNWFilterInternal:
            def __init__(self, parent, name, xml):
                self.name = name
                self.parent = parent
                self.xml = xml

            def undefine(self):
                del self.parent.filters[self.name]
                pass
        tree = etree.fromstring(xml)
        name = tree.get('name')
        if name not in self.filters:
            self.filters[name] = FakeNWFilterInternal(self, name, xml)
        return True


class IptablesFirewallTestCase(test.TestCase):
    def setUp(self):
        super(IptablesFirewallTestCase, self).setUp()

        self.user_id = 'fake'
        self.project_id = 'fake'
        self.context = context.RequestContext(self.user_id, self.project_id)

        class FakeLibvirtDriver(object):
            def nwfilterDefineXML(*args, **kwargs):
                """setup_basic_rules in nwfilter calls this."""
                pass
        self.fake_libvirt_connection = FakeLibvirtDriver()
        self.fw = firewall.IptablesFirewallDriver(
                      fake.FakeVirtAPI(),
                      get_connection=lambda: self.fake_libvirt_connection)

    in_rules = [
      '# Generated by iptables-save v1.4.10 on Sat Feb 19 00:03:19 2011',
      '*nat',
      ':PREROUTING ACCEPT [1170:189210]',
      ':INPUT ACCEPT [844:71028]',
      ':OUTPUT ACCEPT [5149:405186]',
      ':POSTROUTING ACCEPT [5063:386098]',
      '# Completed on Tue Dec 18 15:50:25 2012',
      '# Generated by iptables-save v1.4.12 on Tue Dec 18 15:50:25 201;',
      '*mangle',
      ':PREROUTING ACCEPT [241:39722]',
      ':INPUT ACCEPT [230:39282]',
      ':FORWARD ACCEPT [0:0]',
      ':OUTPUT ACCEPT [266:26558]',
      ':POSTROUTING ACCEPT [267:26590]',
      '-A POSTROUTING -o virbr0 -p udp -m udp --dport 68 -j CHECKSUM '
      '--checksum-fill',
      'COMMIT',
      '# Completed on Tue Dec 18 15:50:25 2012',
      '# Generated by iptables-save v1.4.4 on Mon Dec  6 11:54:13 2010',
      '*filter',
      ':INPUT ACCEPT [969615:281627771]',
      ':FORWARD ACCEPT [0:0]',
      ':OUTPUT ACCEPT [915599:63811649]',
      ':nova-block-ipv4 - [0:0]',
      '[0:0] -A INPUT -i virbr0 -p tcp -m tcp --dport 67 -j ACCEPT ',
      '[0:0] -A FORWARD -d 192.168.122.0/24 -o virbr0 -m state --state RELATED'
      ',ESTABLISHED -j ACCEPT ',
      '[0:0] -A FORWARD -s 192.168.122.0/24 -i virbr0 -j ACCEPT ',
      '[0:0] -A FORWARD -i virbr0 -o virbr0 -j ACCEPT ',
      '[0:0] -A FORWARD -o virbr0 -j REJECT '
      '--reject-with icmp-port-unreachable ',
      '[0:0] -A FORWARD -i virbr0 -j REJECT '
      '--reject-with icmp-port-unreachable ',
      'COMMIT',
      '# Completed on Mon Dec  6 11:54:13 2010',
    ]

    in6_filter_rules = [
      '# Generated by ip6tables-save v1.4.4 on Tue Jan 18 23:47:56 2011',
      '*filter',
      ':INPUT ACCEPT [349155:75810423]',
      ':FORWARD ACCEPT [0:0]',
      ':OUTPUT ACCEPT [349256:75777230]',
      'COMMIT',
      '# Completed on Tue Jan 18 23:47:56 2011',
    ]

    def _create_instance_ref(self):
        return db.instance_create(self.context,
                                  {'user_id': 'fake',
                                   'project_id': 'fake',
                                   'instance_type_id': 1})

    def test_static_filters(self):
        instance_ref = self._create_instance_ref()
        src_instance_ref = self._create_instance_ref()

        admin_ctxt = context.get_admin_context()
        secgroup = db.security_group_create(admin_ctxt,
                                            {'user_id': 'fake',
                                             'project_id': 'fake',
                                             'name': 'testgroup',
                                             'description': 'test group'})

        src_secgroup = db.security_group_create(admin_ctxt,
                                                {'user_id': 'fake',
                                                 'project_id': 'fake',
                                                 'name': 'testsourcegroup',
                                                 'description': 'src group'})

        db.security_group_rule_create(admin_ctxt,
                                      {'parent_group_id': secgroup['id'],
                                       'protocol': 'icmp',
                                       'from_port': -1,
                                       'to_port': -1,
                                       'cidr': '192.168.11.0/24'})

        db.security_group_rule_create(admin_ctxt,
                                      {'parent_group_id': secgroup['id'],
                                       'protocol': 'icmp',
                                       'from_port': 8,
                                       'to_port': -1,
                                       'cidr': '192.168.11.0/24'})

        db.security_group_rule_create(admin_ctxt,
                                      {'parent_group_id': secgroup['id'],
                                       'protocol': 'tcp',
                                       'from_port': 80,
                                       'to_port': 81,
                                       'cidr': '192.168.10.0/24'})

        db.security_group_rule_create(admin_ctxt,
                                      {'parent_group_id': secgroup['id'],
                                       'protocol': 'tcp',
                                       'from_port': 80,
                                       'to_port': 81,
                                       'group_id': src_secgroup['id']})

        db.security_group_rule_create(admin_ctxt,
                                      {'parent_group_id': secgroup['id'],
                                       'group_id': src_secgroup['id']})

        db.instance_add_security_group(admin_ctxt, instance_ref['uuid'],
                                       secgroup['id'])
        db.instance_add_security_group(admin_ctxt, src_instance_ref['uuid'],
                                       src_secgroup['id'])
        instance_ref = db.instance_get(admin_ctxt, instance_ref['id'])
        src_instance_ref = db.instance_get(admin_ctxt, src_instance_ref['id'])

#        self.fw.add_instance(instance_ref)
        def fake_iptables_execute(*cmd, **kwargs):
            process_input = kwargs.get('process_input', None)
            if cmd == ('ip6tables-save', '-c'):
                return '\n'.join(self.in6_filter_rules), None
            if cmd == ('iptables-save', '-c'):
                return '\n'.join(self.in_rules), None
            if cmd == ('iptables-restore', '-c'):
                lines = process_input.split('\n')
                if '*filter' in lines:
                    self.out_rules = lines
                return '', ''
            if cmd == ('ip6tables-restore', '-c',):
                lines = process_input.split('\n')
                if '*filter' in lines:
                    self.out6_rules = lines
                return '', ''

        network_model = _fake_network_info(self.stubs, 1, spectacular=True)

        from nova.network import linux_net
        linux_net.iptables_manager.execute = fake_iptables_execute

        _fake_stub_out_get_nw_info(self.stubs, lambda *a, **kw: network_model)

        network_info = network_model.legacy()
        self.fw.prepare_instance_filter(instance_ref, network_info)
        self.fw.apply_instance_filter(instance_ref, network_info)

        in_rules = filter(lambda l: not l.startswith('#'),
                          self.in_rules)
        for rule in in_rules:
            if 'nova' not in rule:
                self.assertTrue(rule in self.out_rules,
                                'Rule went missing: %s' % rule)

        instance_chain = None
        for rule in self.out_rules:
            # This is pretty crude, but it'll do for now
            # last two octets change
            if re.search('-d 192.168.[0-9]{1,3}.[0-9]{1,3} -j', rule):
                instance_chain = rule.split(' ')[-1]
                break
        self.assertTrue(instance_chain, "The instance chain wasn't added")

        security_group_chain = None
        for rule in self.out_rules:
            # This is pretty crude, but it'll do for now
            if '-A %s -j' % instance_chain in rule:
                security_group_chain = rule.split(' ')[-1]
                break
        self.assertTrue(security_group_chain,
                        "The security group chain wasn't added")

        regex = re.compile('\[0\:0\] -A .* -j ACCEPT -p icmp '
                           '-s 192.168.11.0/24')
        self.assertTrue(len(filter(regex.match, self.out_rules)) > 0,
                        "ICMP acceptance rule wasn't added")

        regex = re.compile('\[0\:0\] -A .* -j ACCEPT -p icmp -m icmp '
                           '--icmp-type 8 -s 192.168.11.0/24')
        self.assertTrue(len(filter(regex.match, self.out_rules)) > 0,
                        "ICMP Echo Request acceptance rule wasn't added")

        for ip in network_model.fixed_ips():
            if ip['version'] != 4:
                continue
            regex = re.compile('\[0\:0\] -A .* -j ACCEPT -p tcp -m multiport '
                               '--dports 80:81 -s %s' % ip['address'])
            self.assertTrue(len(filter(regex.match, self.out_rules)) > 0,
                            "TCP port 80/81 acceptance rule wasn't added")
            regex = re.compile('\[0\:0\] -A .* -j ACCEPT -s '
                               '%s' % ip['address'])
            self.assertTrue(len(filter(regex.match, self.out_rules)) > 0,
                            "Protocol/port-less acceptance rule wasn't added")

        regex = re.compile('\[0\:0\] -A .* -j ACCEPT -p tcp '
                           '-m multiport --dports 80:81 -s 192.168.10.0/24')
        self.assertTrue(len(filter(regex.match, self.out_rules)) > 0,
                        "TCP port 80/81 acceptance rule wasn't added")
        db.instance_destroy(admin_ctxt, instance_ref['uuid'])

    def test_filters_for_instance_with_ip_v6(self):
        self.flags(use_ipv6=True)
        network_info = _fake_network_info(self.stubs, 1)
        rulesv4, rulesv6 = self.fw._filters_for_instance("fake", network_info)
        self.assertEquals(len(rulesv4), 2)
        self.assertEquals(len(rulesv6), 1)

    def test_filters_for_instance_without_ip_v6(self):
        self.flags(use_ipv6=False)
        network_info = _fake_network_info(self.stubs, 1)
        rulesv4, rulesv6 = self.fw._filters_for_instance("fake", network_info)
        self.assertEquals(len(rulesv4), 2)
        self.assertEquals(len(rulesv6), 0)

    def test_multinic_iptables(self):
        ipv4_rules_per_addr = 1
        ipv4_addr_per_network = 2
        ipv6_rules_per_addr = 1
        ipv6_addr_per_network = 1
        networks_count = 5
        instance_ref = self._create_instance_ref()
        network_info = _fake_network_info(self.stubs, networks_count,
                                                      ipv4_addr_per_network)
        ipv4_len = len(self.fw.iptables.ipv4['filter'].rules)
        ipv6_len = len(self.fw.iptables.ipv6['filter'].rules)
        inst_ipv4, inst_ipv6 = self.fw.instance_rules(instance_ref,
                                                      network_info)
        self.fw.prepare_instance_filter(instance_ref, network_info)
        ipv4 = self.fw.iptables.ipv4['filter'].rules
        ipv6 = self.fw.iptables.ipv6['filter'].rules
        ipv4_network_rules = len(ipv4) - len(inst_ipv4) - ipv4_len
        ipv6_network_rules = len(ipv6) - len(inst_ipv6) - ipv6_len
        # Extra rules are for the DHCP request
        rules = (ipv4_rules_per_addr * ipv4_addr_per_network *
                 networks_count) + 2
        self.assertEquals(ipv4_network_rules, rules)
        self.assertEquals(ipv6_network_rules,
                  ipv6_rules_per_addr * ipv6_addr_per_network * networks_count)

    def test_do_refresh_security_group_rules(self):
        instance_ref = self._create_instance_ref()
        self.mox.StubOutWithMock(self.fw,
                                 'instance_rules')
        self.mox.StubOutWithMock(self.fw,
                                 'add_filters_for_instance',
                                 use_mock_anything=True)

        self.fw.instance_rules(instance_ref,
                               mox.IgnoreArg()).AndReturn((None, None))
        self.fw.add_filters_for_instance(instance_ref, mox.IgnoreArg(),
                                         mox.IgnoreArg())
        self.fw.instance_rules(instance_ref,
                               mox.IgnoreArg()).AndReturn((None, None))
        self.fw.add_filters_for_instance(instance_ref, mox.IgnoreArg(),
                                         mox.IgnoreArg())
        self.mox.ReplayAll()

        self.fw.prepare_instance_filter(instance_ref, mox.IgnoreArg())
        self.fw.instances[instance_ref['id']] = instance_ref
        self.fw.do_refresh_security_group_rules("fake")

    def test_unfilter_instance_undefines_nwfilter(self):
        admin_ctxt = context.get_admin_context()

        fakefilter = NWFilterFakes()
        _xml_mock = fakefilter.filterDefineXMLMock
        self.fw.nwfilter._conn.nwfilterDefineXML = _xml_mock
        _lookup_name = fakefilter.nwfilterLookupByName
        self.fw.nwfilter._conn.nwfilterLookupByName = _lookup_name
        instance_ref = self._create_instance_ref()

        network_info = _fake_network_info(self.stubs, 1)
        self.fw.setup_basic_filtering(instance_ref, network_info)
        self.fw.prepare_instance_filter(instance_ref, network_info)
        self.fw.apply_instance_filter(instance_ref, network_info)
        original_filter_count = len(fakefilter.filters)
        self.fw.unfilter_instance(instance_ref, network_info)

        # should undefine just the instance filter
        self.assertEqual(original_filter_count - len(fakefilter.filters), 1)

        db.instance_destroy(admin_ctxt, instance_ref['uuid'])

    def test_provider_firewall_rules(self):
        # setup basic instance data
        instance_ref = self._create_instance_ref()
        # FRAGILE: peeks at how the firewall names chains
        chain_name = 'inst-%s' % instance_ref['id']

        # create a firewall via setup_basic_filtering like libvirt_conn.spawn
        # should have a chain with 0 rules
        network_info = _fake_network_info(self.stubs, 1)
        self.fw.setup_basic_filtering(instance_ref, network_info)
        self.assertTrue('provider' in self.fw.iptables.ipv4['filter'].chains)
        rules = [rule for rule in self.fw.iptables.ipv4['filter'].rules
                      if rule.chain == 'provider']
        self.assertEqual(0, len(rules))

        admin_ctxt = context.get_admin_context()
        # add a rule and send the update message, check for 1 rule
        provider_fw0 = db.provider_fw_rule_create(admin_ctxt,
                                                  {'protocol': 'tcp',
                                                   'cidr': '10.99.99.99/32',
                                                   'from_port': 1,
                                                   'to_port': 65535})
        self.fw.refresh_provider_fw_rules()
        rules = [rule for rule in self.fw.iptables.ipv4['filter'].rules
                      if rule.chain == 'provider']
        self.assertEqual(1, len(rules))

        # Add another, refresh, and make sure number of rules goes to two
        provider_fw1 = db.provider_fw_rule_create(admin_ctxt,
                                                  {'protocol': 'udp',
                                                   'cidr': '10.99.99.99/32',
                                                   'from_port': 1,
                                                   'to_port': 65535})
        self.fw.refresh_provider_fw_rules()
        rules = [rule for rule in self.fw.iptables.ipv4['filter'].rules
                      if rule.chain == 'provider']
        self.assertEqual(2, len(rules))

        # create the instance filter and make sure it has a jump rule
        self.fw.prepare_instance_filter(instance_ref, network_info)
        self.fw.apply_instance_filter(instance_ref, network_info)
        inst_rules = [rule for rule in self.fw.iptables.ipv4['filter'].rules
                           if rule.chain == chain_name]
        jump_rules = [rule for rule in inst_rules if '-j' in rule.rule]
        provjump_rules = []
        # IptablesTable doesn't make rules unique internally
        for rule in jump_rules:
            if 'provider' in rule.rule and rule not in provjump_rules:
                provjump_rules.append(rule)
        self.assertEqual(1, len(provjump_rules))

        # remove a rule from the db, cast to compute to refresh rule
        db.provider_fw_rule_destroy(admin_ctxt, provider_fw1['id'])
        self.fw.refresh_provider_fw_rules()
        rules = [rule for rule in self.fw.iptables.ipv4['filter'].rules
                      if rule.chain == 'provider']
        self.assertEqual(1, len(rules))


class NWFilterTestCase(test.TestCase):
    def setUp(self):
        super(NWFilterTestCase, self).setUp()

        class Mock(object):
            pass

        self.user_id = 'fake'
        self.project_id = 'fake'
        self.context = context.RequestContext(self.user_id, self.project_id)

        self.fake_libvirt_connection = Mock()

        self.fw = firewall.NWFilterFirewall(fake.FakeVirtAPI(),
                                         lambda: self.fake_libvirt_connection)

    def test_cidr_rule_nwfilter_xml(self):
        cloud_controller = cloud.CloudController()
        cloud_controller.create_security_group(self.context,
                                               'testgroup',
                                               'test group description')
        cloud_controller.authorize_security_group_ingress(self.context,
                                                          'testgroup',
                                                          from_port='80',
                                                          to_port='81',
                                                          ip_protocol='tcp',
                                                          cidr_ip='0.0.0.0/0')

        security_group = db.security_group_get_by_name(self.context,
                                                       'fake',
                                                       'testgroup')
        self.teardown_security_group()

    def teardown_security_group(self):
        cloud_controller = cloud.CloudController()
        cloud_controller.delete_security_group(self.context, 'testgroup')

    def setup_and_return_security_group(self):
        cloud_controller = cloud.CloudController()
        cloud_controller.create_security_group(self.context,
                                               'testgroup',
                                               'test group description')
        cloud_controller.authorize_security_group_ingress(self.context,
                                                          'testgroup',
                                                          from_port='80',
                                                          to_port='81',
                                                          ip_protocol='tcp',
                                                          cidr_ip='0.0.0.0/0')

        return db.security_group_get_by_name(self.context, 'fake', 'testgroup')

    def _create_instance(self):
        return db.instance_create(self.context,
                                  {'user_id': 'fake',
                                   'project_id': 'fake',
                                   'instance_type_id': 1})

    def _create_instance_type(self, params=None):
        """Create a test instance."""
        if not params:
            params = {}

        context = self.context.elevated()
        inst = {}
        inst['name'] = 'm1.small'
        inst['memory_mb'] = '1024'
        inst['vcpus'] = '1'
        inst['root_gb'] = '10'
        inst['ephemeral_gb'] = '20'
        inst['flavorid'] = '1'
        inst['swap'] = '2048'
        inst['rxtx_factor'] = 1
        inst.update(params)
        return db.instance_type_create(context, inst)['id']

    def test_creates_base_rule_first(self):
        # These come pre-defined by libvirt
        self.defined_filters = ['no-mac-spoofing',
                                'no-ip-spoofing',
                                'no-arp-spoofing',
                                'allow-dhcp-server']

        self.recursive_depends = {}
        for f in self.defined_filters:
            self.recursive_depends[f] = []

        def _filterDefineXMLMock(xml):
            dom = minidom.parseString(xml)
            name = dom.firstChild.getAttribute('name')
            self.recursive_depends[name] = []
            for f in dom.getElementsByTagName('filterref'):
                ref = f.getAttribute('filter')
                self.assertTrue(ref in self.defined_filters,
                                ('%s referenced filter that does ' +
                                'not yet exist: %s') % (name, ref))
                dependencies = [ref] + self.recursive_depends[ref]
                self.recursive_depends[name] += dependencies

            self.defined_filters.append(name)
            return True

        self.fake_libvirt_connection.nwfilterDefineXML = _filterDefineXMLMock

        instance_ref = self._create_instance()
        inst_id = instance_ref['id']
        inst_uuid = instance_ref['uuid']

        def _ensure_all_called(mac, allow_dhcp):
            instance_filter = 'nova-instance-%s-%s' % (instance_ref['name'],
                                                   mac.translate(None, ':'))
            requiredlist = ['no-arp-spoofing', 'no-ip-spoofing',
                             'no-mac-spoofing']
            if allow_dhcp:
                requiredlist.append('allow-dhcp-server')
            for required in requiredlist:
                self.assertTrue(required in
                                self.recursive_depends[instance_filter],
                                "Instance's filter does not include %s" %
                                required)

        self.security_group = self.setup_and_return_security_group()

        db.instance_add_security_group(self.context, inst_uuid,
                                       self.security_group['id'])
        instance = db.instance_get(self.context, inst_id)

        network_info = _fake_network_info(self.stubs, 1)
        # since there is one (network_info) there is one vif
        # pass this vif's mac to _ensure_all_called()
        # to set the instance_filter properly
        mac = network_info[0][1]['mac']

        self.fw.setup_basic_filtering(instance, network_info)
        allow_dhcp = False
        for (network, mapping) in network_info:
            if mapping['dhcp_server']:
                allow_dhcp = True
                break
        _ensure_all_called(mac, allow_dhcp)
        db.instance_remove_security_group(self.context, inst_uuid,
                                          self.security_group['id'])
        self.teardown_security_group()
        db.instance_destroy(context.get_admin_context(), instance_ref['uuid'])

    def test_unfilter_instance_undefines_nwfilters(self):
        admin_ctxt = context.get_admin_context()

        fakefilter = NWFilterFakes()
        self.fw._conn.nwfilterDefineXML = fakefilter.filterDefineXMLMock
        self.fw._conn.nwfilterLookupByName = fakefilter.nwfilterLookupByName

        instance_ref = self._create_instance()
        inst_id = instance_ref['id']
        inst_uuid = instance_ref['uuid']

        self.security_group = self.setup_and_return_security_group()

        db.instance_add_security_group(self.context, inst_uuid,
                                       self.security_group['id'])

        instance = db.instance_get(self.context, inst_id)

        network_info = _fake_network_info(self.stubs, 1)
        self.fw.setup_basic_filtering(instance, network_info)
        original_filter_count = len(fakefilter.filters)
        self.fw.unfilter_instance(instance, network_info)
        self.assertEqual(original_filter_count - len(fakefilter.filters), 1)

        db.instance_destroy(admin_ctxt, instance_ref['uuid'])

    def test_nwfilter_parameters(self):
        admin_ctxt = context.get_admin_context()

        fakefilter = NWFilterFakes()
        self.fw._conn.nwfilterDefineXML = fakefilter.filterDefineXMLMock
        self.fw._conn.nwfilterLookupByName = fakefilter.nwfilterLookupByName

        instance_ref = self._create_instance()
        inst_id = instance_ref['id']
        inst_uuid = instance_ref['uuid']

        self.security_group = self.setup_and_return_security_group()

        db.instance_add_security_group(self.context, inst_uuid,
                                       self.security_group['id'])

        instance = db.instance_get(self.context, inst_id)

        network_info = _fake_network_info(self.stubs, 1)
        self.fw.setup_basic_filtering(instance, network_info)

        (network, mapping) = network_info[0]
        nic_id = mapping['mac'].replace(':', '')
        instance_filter_name = self.fw._instance_filter_name(instance, nic_id)
        f = fakefilter.nwfilterLookupByName(instance_filter_name)
        tree = etree.fromstring(f.xml)

        for fref in tree.findall('filterref'):
            parameters = fref.findall('./parameter')
            for parameter in parameters:
                if parameter.get('name') == 'IP':
                    self.assertTrue(_ipv4_like(parameter.get('value'),
                                                             '192.168'))
                elif parameter.get('name') == 'DHCPSERVER':
                    dhcp_server = mapping['dhcp_server']
                    self.assertEqual(parameter.get('value'), dhcp_server)
                elif parameter.get('name') == 'RASERVER':
                    ra_server = mapping.get('gateway_v6') + "/128"
                    self.assertEqual(parameter.get('value'), ra_server)
                elif parameter.get('name') == 'PROJNET':
                    ipv4_cidr = network['cidr']
                    net, mask = netutils.get_net_and_mask(ipv4_cidr)
                    self.assertEqual(parameter.get('value'), net)
                elif parameter.get('name') == 'PROJMASK':
                    ipv4_cidr = network['cidr']
                    net, mask = netutils.get_net_and_mask(ipv4_cidr)
                    self.assertEqual(parameter.get('value'), mask)
                elif parameter.get('name') == 'PROJNET6':
                    ipv6_cidr = network['cidr_v6']
                    net, prefix = netutils.get_net_and_prefixlen(ipv6_cidr)
                    self.assertEqual(parameter.get('value'), net)
                elif parameter.get('name') == 'PROJMASK6':
                    ipv6_cidr = network['cidr_v6']
                    net, prefix = netutils.get_net_and_prefixlen(ipv6_cidr)
                    self.assertEqual(parameter.get('value'), prefix)
                else:
                    raise exception.InvalidParameterValue('unknown parameter '
                                                          'in filter')

        db.instance_destroy(admin_ctxt, instance_ref['uuid'])


class LibvirtUtilsTestCase(test.TestCase):
    def test_get_iscsi_initiator(self):
        self.mox.StubOutWithMock(utils, 'execute')
        initiator = 'fake.initiator.iqn'
        rval = ("junk\nInitiatorName=%s\njunk\n" % initiator, None)
        utils.execute('cat', '/etc/iscsi/initiatorname.iscsi',
                      run_as_root=True).AndReturn(rval)
        # Start test
        self.mox.ReplayAll()
        result = libvirt_utils.get_iscsi_initiator()
        self.assertEqual(initiator, result)

    def test_create_image(self):
        self.mox.StubOutWithMock(utils, 'execute')
        utils.execute('qemu-img', 'create', '-f', 'raw',
                      '/some/path', '10G')
        utils.execute('qemu-img', 'create', '-f', 'qcow2',
                      '/some/stuff', '1234567891234')
        # Start test
        self.mox.ReplayAll()
        libvirt_utils.create_image('raw', '/some/path', '10G')
        libvirt_utils.create_image('qcow2', '/some/stuff', '1234567891234')

    def test_create_cow_image(self):
        self.mox.StubOutWithMock(os.path, 'exists')
        self.mox.StubOutWithMock(utils, 'execute')
        rval = ('', '')
        os.path.exists('/some/path').AndReturn(True)
        utils.execute('env', 'LC_ALL=C', 'LANG=C',
                      'qemu-img', 'info', '/some/path').AndReturn(rval)
        utils.execute('qemu-img', 'create', '-f', 'qcow2',
                      '-o', 'backing_file=/some/path',
                      '/the/new/cow')
        # Start test
        self.mox.ReplayAll()
        libvirt_utils.create_cow_image('/some/path', '/the/new/cow')

    def test_pick_disk_driver_name(self):
        type_map = {'kvm': ([True, 'qemu'], [False, 'qemu'], [None, 'qemu']),
                    'qemu': ([True, 'qemu'], [False, 'qemu'], [None, 'qemu']),
                    'xen': ([True, 'phy'], [False, 'tap'], [None, 'tap']),
                    'uml': ([True, None], [False, None], [None, None]),
                    'lxc': ([True, None], [False, None], [None, None])}

        for (libvirt_type, checks) in type_map.iteritems():
            self.flags(libvirt_type=libvirt_type)
            for (is_block_dev, expected_result) in checks:
                result = libvirt_utils.pick_disk_driver_name(is_block_dev)
                self.assertEquals(result, expected_result)

    def test_get_disk_size(self):
        self.mox.StubOutWithMock(os.path, 'exists')
        self.mox.StubOutWithMock(utils, 'execute')
        os.path.exists('/some/path').AndReturn(True)
        utils.execute('env', 'LC_ALL=C', 'LANG=C', 'qemu-img', 'info',
                      '/some/path').AndReturn(('''image: 00000001
file format: raw
virtual size: 4.4M (4592640 bytes)
disk size: 4.4M''', ''))

        # Start test
        self.mox.ReplayAll()
        self.assertEquals(disk.get_disk_size('/some/path'), 4592640)

    def test_copy_image(self):
        dst_fd, dst_path = tempfile.mkstemp()
        try:
            os.close(dst_fd)

            src_fd, src_path = tempfile.mkstemp()
            try:
                with os.fdopen(src_fd, 'w') as fp:
                    fp.write('canary')

                libvirt_utils.copy_image(src_path, dst_path)
                with open(dst_path, 'r') as fp:
                    self.assertEquals(fp.read(), 'canary')
            finally:
                os.unlink(src_path)
        finally:
            os.unlink(dst_path)

    def test_write_to_file(self):
        dst_fd, dst_path = tempfile.mkstemp()
        try:
            os.close(dst_fd)

            libvirt_utils.write_to_file(dst_path, 'hello')
            with open(dst_path, 'r') as fp:
                self.assertEquals(fp.read(), 'hello')
        finally:
            os.unlink(dst_path)

    def test_write_to_file_with_umask(self):
        dst_fd, dst_path = tempfile.mkstemp()
        try:
            os.close(dst_fd)
            os.unlink(dst_path)

            libvirt_utils.write_to_file(dst_path, 'hello', umask=0277)
            with open(dst_path, 'r') as fp:
                self.assertEquals(fp.read(), 'hello')
            mode = os.stat(dst_path).st_mode
            self.assertEquals(mode & 0277, 0)
        finally:
            os.unlink(dst_path)

    def test_chown(self):
        self.mox.StubOutWithMock(utils, 'execute')
        utils.execute('chown', 'soren', '/some/path', run_as_root=True)
        self.mox.ReplayAll()
        libvirt_utils.chown('/some/path', 'soren')

    def _do_test_extract_snapshot(self, dest_format='raw', out_format='raw'):
        self.mox.StubOutWithMock(utils, 'execute')
        utils.execute('qemu-img', 'convert', '-f', 'qcow2', '-O', out_format,
                      '-s', 'snap1', '/path/to/disk/image', '/extracted/snap')

        # Start test
        self.mox.ReplayAll()
        libvirt_utils.extract_snapshot('/path/to/disk/image', 'qcow2',
                                       'snap1', '/extracted/snap', dest_format)

    def test_extract_snapshot_raw(self):
        self._do_test_extract_snapshot()

    def test_extract_snapshot_iso(self):
        self._do_test_extract_snapshot(dest_format='iso')

    def test_extract_snapshot_qcow2(self):
        self._do_test_extract_snapshot(dest_format='qcow2', out_format='qcow2')

    def test_load_file(self):
        dst_fd, dst_path = tempfile.mkstemp()
        try:
            os.close(dst_fd)

            # We have a test for write_to_file. If that is sound, this suffices
            libvirt_utils.write_to_file(dst_path, 'hello')
            self.assertEquals(libvirt_utils.load_file(dst_path), 'hello')
        finally:
            os.unlink(dst_path)

    def test_file_open(self):
        dst_fd, dst_path = tempfile.mkstemp()
        try:
            os.close(dst_fd)

            # We have a test for write_to_file. If that is sound, this suffices
            libvirt_utils.write_to_file(dst_path, 'hello')
            with libvirt_utils.file_open(dst_path, 'r') as fp:
                self.assertEquals(fp.read(), 'hello')
        finally:
            os.unlink(dst_path)

    def test_get_fs_info(self):

        class FakeStatResult(object):

            def __init__(self):
                self.f_bsize = 4096
                self.f_frsize = 4096
                self.f_blocks = 2000
                self.f_bfree = 1000
                self.f_bavail = 900
                self.f_files = 2000
                self.f_ffree = 1000
                self.f_favail = 900
                self.f_flag = 4096
                self.f_namemax = 255

        self.path = None

        def fake_statvfs(path):
            self.path = path
            return FakeStatResult()

        self.stubs.Set(os, 'statvfs', fake_statvfs)

        fs_info = libvirt_utils.get_fs_info('/some/file/path')
        self.assertEquals('/some/file/path', self.path)
        self.assertEquals(8192000, fs_info['total'])
        self.assertEquals(3686400, fs_info['free'])
        self.assertEquals(4096000, fs_info['used'])

    def test_fetch_image(self):
        self.mox.StubOutWithMock(images, 'fetch_to_raw')

        context = 'opaque context'
        target = '/tmp/targetfile'
        image_id = '4'
        user_id = 'fake'
        project_id = 'fake'
        images.fetch_to_raw(context, image_id, target, user_id, project_id)

        self.mox.ReplayAll()
        libvirt_utils.fetch_image(context, target, image_id,
                                  user_id, project_id)

    def test_fetch_raw_image(self):

        def fake_execute(*cmd, **kwargs):
            self.executes.append(cmd)
            return None, None

        def fake_rename(old, new):
            self.executes.append(('mv', old, new))

        def fake_unlink(path):
            self.executes.append(('rm', path))

        def fake_rm_on_errror(path):
            self.executes.append(('rm', '-f', path))

        def fake_qemu_img_info(path):
            class FakeImgInfo(object):
                pass

            file_format = path.split('.')[-1]
            if file_format == 'part':
                file_format = path.split('.')[-2]
            elif file_format == 'converted':
                file_format = 'raw'
            if 'backing' in path:
                backing_file = 'backing'
            else:
                backing_file = None

            FakeImgInfo.file_format = file_format
            FakeImgInfo.backing_file = backing_file

            return FakeImgInfo()

        self.stubs.Set(utils, 'execute', fake_execute)
        self.stubs.Set(os, 'rename', fake_rename)
        self.stubs.Set(os, 'unlink', fake_unlink)
        self.stubs.Set(images, 'fetch', lambda *_: None)
        self.stubs.Set(images, 'qemu_img_info', fake_qemu_img_info)
        self.stubs.Set(utils, 'delete_if_exists', fake_rm_on_errror)

        context = 'opaque context'
        image_id = '4'
        user_id = 'fake'
        project_id = 'fake'

        target = 't.qcow2'
        self.executes = []
        expected_commands = [('qemu-img', 'convert', '-O', 'raw',
                              't.qcow2.part', 't.qcow2.converted'),
                             ('rm', 't.qcow2.part'),
                             ('mv', 't.qcow2.converted', 't.qcow2')]
        images.fetch_to_raw(context, image_id, target, user_id, project_id)
        self.assertEqual(self.executes, expected_commands)

        target = 't.raw'
        self.executes = []
        expected_commands = [('mv', 't.raw.part', 't.raw')]
        images.fetch_to_raw(context, image_id, target, user_id, project_id)
        self.assertEqual(self.executes, expected_commands)

        target = 'backing.qcow2'
        self.executes = []
        expected_commands = [('rm', '-f', 'backing.qcow2.part')]
        self.assertRaises(exception.ImageUnacceptable,
                          images.fetch_to_raw,
                          context, image_id, target, user_id, project_id)
        self.assertEqual(self.executes, expected_commands)

        del self.executes

    def test_get_disk_backing_file(self):
        with_actual_path = False

        def fake_execute(*args, **kwargs):
            if with_actual_path:
                return ("some: output\n"
                        "backing file: /foo/bar/baz (actual path: /a/b/c)\n"
                        "...: ...\n"), ''
            else:
                return ("some: output\n"
                        "backing file: /foo/bar/baz\n"
                        "...: ...\n"), ''

        def return_true(*args, **kwargs):
            return True

        self.stubs.Set(utils, 'execute', fake_execute)
        self.stubs.Set(os.path, 'exists', return_true)

        out = libvirt_utils.get_disk_backing_file('')
        self.assertEqual(out, 'baz')
        with_actual_path = True
        out = libvirt_utils.get_disk_backing_file('')
        self.assertEqual(out, 'c')


class LibvirtDriverTestCase(test.TestCase):
    """Test for nova.virt.libvirt.libvirt_driver.LibvirtDriver."""
    def setUp(self):
        super(LibvirtDriverTestCase, self).setUp()
        self.libvirtconnection = libvirt_driver.LibvirtDriver(
            fake.FakeVirtAPI(), read_only=True)

    def _create_instance(self, params=None):
        """Create a test instance."""
        if not params:
            params = {}

        inst = {}
        inst['image_ref'] = '1'
        inst['reservation_id'] = 'r-fakeres'
        inst['launch_time'] = '10'
        inst['user_id'] = 'fake'
        inst['project_id'] = 'fake'
        type_id = instance_types.get_instance_type_by_name('m1.tiny')['id']
        inst['instance_type_id'] = type_id
        inst['ami_launch_index'] = 0
        inst['host'] = 'host1'
        inst['root_gb'] = 10
        inst['ephemeral_gb'] = 20
        inst['config_drive'] = 1
        inst['kernel_id'] = 2
        inst['ramdisk_id'] = 3
        inst['config_drive_id'] = 1
        inst['key_data'] = 'ABCDEFG'

        inst.update(params)
        return db.instance_create(context.get_admin_context(), inst)

    def test_migrate_disk_and_power_off_exception(self):
        """Test for nova.virt.libvirt.libvirt_driver.LivirtConnection
        .migrate_disk_and_power_off. """

        self.counter = 0

        def fake_get_instance_disk_info(instance, xml=None):
            return '[]'

        def fake_destroy(instance):
            pass

        def fake_get_host_ip_addr():
            return '10.0.0.1'

        def fake_execute(*args, **kwargs):
            self.counter += 1
            if self.counter == 1:
                assert False, "intentional failure"

        def fake_os_path_exists(path):
            return True

        self.stubs.Set(self.libvirtconnection, 'get_instance_disk_info',
                       fake_get_instance_disk_info)
        self.stubs.Set(self.libvirtconnection, '_destroy', fake_destroy)
        self.stubs.Set(self.libvirtconnection, 'get_host_ip_addr',
                       fake_get_host_ip_addr)
        self.stubs.Set(utils, 'execute', fake_execute)
        self.stubs.Set(os.path, 'exists', fake_os_path_exists)

        ins_ref = self._create_instance()

        self.assertRaises(AssertionError,
                          self.libvirtconnection.migrate_disk_and_power_off,
                          None, ins_ref, '10.0.0.2', None, None)

    def test_migrate_disk_and_power_off(self):
        """Test for nova.virt.libvirt.libvirt_driver.LivirtConnection
        .migrate_disk_and_power_off. """

        disk_info = [{'type': 'qcow2', 'path': '/test/disk',
                      'virt_disk_size': '10737418240',
                      'backing_file': '/base/disk',
                      'disk_size': '83886080'},
                     {'type': 'raw', 'path': '/test/disk.local',
                      'virt_disk_size': '10737418240',
                      'backing_file': '/base/disk.local',
                      'disk_size': '83886080'}]
        disk_info_text = jsonutils.dumps(disk_info)

        def fake_get_instance_disk_info(instance, xml=None):
            return disk_info_text

        def fake_destroy(instance):
            pass

        def fake_get_host_ip_addr():
            return '10.0.0.1'

        def fake_execute(*args, **kwargs):
            pass

        self.stubs.Set(self.libvirtconnection, 'get_instance_disk_info',
                       fake_get_instance_disk_info)
        self.stubs.Set(self.libvirtconnection, '_destroy', fake_destroy)
        self.stubs.Set(self.libvirtconnection, 'get_host_ip_addr',
                       fake_get_host_ip_addr)
        self.stubs.Set(utils, 'execute', fake_execute)

        ins_ref = self._create_instance()
        # dest is different host case
        out = self.libvirtconnection.migrate_disk_and_power_off(
               None, ins_ref, '10.0.0.2', None, None)
        self.assertEquals(out, disk_info_text)

        # dest is same host case
        out = self.libvirtconnection.migrate_disk_and_power_off(
               None, ins_ref, '10.0.0.1', None, None)
        self.assertEquals(out, disk_info_text)

    def test_wait_for_running(self):
        def fake_get_info(instance):
            if instance['name'] == "not_found":
                raise exception.NotFound
            elif instance['name'] == "running":
                return {'state': power_state.RUNNING}
            else:
                return {'state': power_state.SHUTDOWN}

        self.stubs.Set(self.libvirtconnection, 'get_info',
                       fake_get_info)

        # instance not found case
        self.assertRaises(exception.NotFound,
                self.libvirtconnection._wait_for_running,
                    {'name': 'not_found',
                     'uuid': 'not_found_uuid'})

        # instance is running case
        self.assertRaises(utils.LoopingCallDone,
                self.libvirtconnection._wait_for_running,
                    {'name': 'running',
                     'uuid': 'running_uuid'})

        # else case
        self.libvirtconnection._wait_for_running({'name': 'else',
                                                  'uuid': 'other_uuid'})

    def test_finish_migration(self):
        """Test for nova.virt.libvirt.libvirt_driver.LivirtConnection
        .finish_migration. """

        disk_info = [{'type': 'qcow2', 'path': '/test/disk',
                      'local_gb': 10, 'backing_file': '/base/disk'},
                     {'type': 'raw', 'path': '/test/disk.local',
                      'local_gb': 10, 'backing_file': '/base/disk.local'}]
        disk_info_text = jsonutils.dumps(disk_info)

        def fake_can_resize_fs(path, size, use_cow=False):
            return False

        def fake_extend(path, size):
            pass

        def fake_to_xml(instance, network_info, disk_info,
                        image_meta=None, rescue=None,
                        block_device_info=None, write_to_disk=False):
            return ""

        def fake_plug_vifs(instance, network_info):
            pass

        def fake_create_image(context, inst, libvirt_xml,
                              disk_mapping, suffix='',
                              disk_images=None, network_info=None,
                              block_device_info=None):
            pass

        def fake_create_domain(xml, instance=None):
            return None

        def fake_enable_hairpin(instance):
            pass

        def fake_execute(*args, **kwargs):
            pass

        def fake_get_info(instance):
            return {'state': power_state.RUNNING}

        self.flags(use_cow_images=True)
        self.stubs.Set(libvirt_driver.disk, 'extend', fake_extend)
        self.stubs.Set(libvirt_driver.disk, 'can_resize_fs',
                       fake_can_resize_fs)
        self.stubs.Set(self.libvirtconnection, 'to_xml', fake_to_xml)
        self.stubs.Set(self.libvirtconnection, 'plug_vifs', fake_plug_vifs)
        self.stubs.Set(self.libvirtconnection, '_create_image',
                       fake_create_image)
        self.stubs.Set(self.libvirtconnection, '_create_domain',
                       fake_create_domain)
        self.stubs.Set(self.libvirtconnection, '_enable_hairpin',
                       fake_enable_hairpin)
        self.stubs.Set(utils, 'execute', fake_execute)
        fw = base_firewall.NoopFirewallDriver()
        self.stubs.Set(self.libvirtconnection, 'firewall_driver', fw)
        self.stubs.Set(self.libvirtconnection, 'get_info',
                       fake_get_info)

        ins_ref = self._create_instance()

        self.libvirtconnection.finish_migration(
                      context.get_admin_context(), None, ins_ref,
                      disk_info_text, None, None, None)

    def test_finish_revert_migration(self):
        """Test for nova.virt.libvirt.libvirt_driver.LivirtConnection
        .finish_revert_migration. """

        def fake_execute(*args, **kwargs):
            pass

        def fake_plug_vifs(instance, network_info):
            pass

        def fake_create_domain(xml, instance=None):
            return None

        def fake_enable_hairpin(instance):
            pass

        def fake_get_info(instance):
            return {'state': power_state.RUNNING}

        def fake_to_xml(instance, network_info, disk_info,
                        image_meta=None, rescue=None,
                        block_device_info=None):
            return ""

        self.stubs.Set(self.libvirtconnection, 'to_xml', fake_to_xml)
        self.stubs.Set(self.libvirtconnection, 'plug_vifs', fake_plug_vifs)
        self.stubs.Set(utils, 'execute', fake_execute)
        fw = base_firewall.NoopFirewallDriver()
        self.stubs.Set(self.libvirtconnection, 'firewall_driver', fw)
        self.stubs.Set(self.libvirtconnection, '_create_domain',
                       fake_create_domain)
        self.stubs.Set(self.libvirtconnection, '_enable_hairpin',
                       fake_enable_hairpin)
        self.stubs.Set(self.libvirtconnection, 'get_info',
                       fake_get_info)

        with utils.tempdir() as tmpdir:
            self.flags(instances_path=tmpdir)
            ins_ref = self._create_instance()
            os.mkdir(os.path.join(tmpdir, ins_ref['name']))
            libvirt_xml_path = os.path.join(tmpdir,
                                            ins_ref['name'],
                                            'libvirt.xml')
            f = open(libvirt_xml_path, 'w')
            f.close()

            self.libvirtconnection.finish_revert_migration(ins_ref, None)

    def _test_finish_revert_migration_after_crash(self, backup_made, new_made):
        class FakeLoopingCall:
            def start(self, *a, **k):
                return self

            def wait(self):
                return None

        self.mox.StubOutWithMock(libvirt_utils, 'get_instance_path')
        self.mox.StubOutWithMock(os.path, 'exists')
        self.mox.StubOutWithMock(shutil, 'rmtree')
        self.mox.StubOutWithMock(utils, 'execute')

        self.stubs.Set(blockinfo, 'get_disk_info', lambda *a: None)
        self.stubs.Set(self.libvirtconnection, 'to_xml', lambda *a, **k: None)
        self.stubs.Set(self.libvirtconnection, '_create_domain_and_network',
                       lambda *a: None)
        self.stubs.Set(utils, 'FixedIntervalLoopingCall',
                       lambda *a, **k: FakeLoopingCall())

        libvirt_utils.get_instance_path({}).AndReturn('/fake/foo')
        os.path.exists('/fake/foo_resize').AndReturn(backup_made)
        if backup_made:
            os.path.exists('/fake/foo').AndReturn(new_made)
            if new_made:
                shutil.rmtree('/fake/foo')
            utils.execute('mv', '/fake/foo_resize', '/fake/foo')

        self.mox.ReplayAll()

        self.libvirtconnection.finish_revert_migration({}, [])

    def test_finish_revert_migration_after_crash(self):
        self._test_finish_revert_migration_after_crash(True, True)

    def test_finish_revert_migration_after_crash_before_new(self):
        self._test_finish_revert_migration_after_crash(True, False)

    def test_finish_revert_migration_after_crash_before_backup(self):
        self._test_finish_revert_migration_after_crash(False, False)

    def test_cleanup_failed_migration(self):
        self.mox.StubOutWithMock(shutil, 'rmtree')
        shutil.rmtree('/fake/inst')
        self.mox.ReplayAll()
        self.libvirtconnection._cleanup_failed_migration('/fake/inst')

    def test_confirm_migration(self):
        ins_ref = self._create_instance()

        self.mox.StubOutWithMock(self.libvirtconnection, "_cleanup_resize")
        self.libvirtconnection._cleanup_resize(ins_ref,
                             _fake_network_info(self.stubs, 1))

        self.mox.ReplayAll()
        self.libvirtconnection.confirm_migration("migration_ref", ins_ref,
                                            _fake_network_info(self.stubs, 1))

    def test_cleanup_resize_same_host(self):
        ins_ref = self._create_instance({'host': CONF.host})

        def fake_os_path_exists(path):
            return True

        def fake_shutil_rmtree(target):
            pass

        self.stubs.Set(os.path, 'exists', fake_os_path_exists)
        self.stubs.Set(shutil, 'rmtree', fake_shutil_rmtree)

        self.mox.ReplayAll()
        self.libvirtconnection._cleanup_resize(ins_ref,
                                            _fake_network_info(self.stubs, 1))

    def test_cleanup_resize_not_same_host(self):
        host = 'not' + CONF.host
        ins_ref = self._create_instance({'host': host})

        def fake_os_path_exists(path):
            return True

        def fake_shutil_rmtree(target):
            pass

        def fake_undefine_domain(instance):
            pass

        def fake_unplug_vifs(instance, network_info):
            pass

        def fake_unfilter_instance(instance, network_info):
            pass

        self.stubs.Set(os.path, 'exists', fake_os_path_exists)
        self.stubs.Set(shutil, 'rmtree', fake_shutil_rmtree)
        self.stubs.Set(self.libvirtconnection, '_undefine_domain',
                       fake_undefine_domain)
        self.stubs.Set(self.libvirtconnection, 'unplug_vifs',
                       fake_unplug_vifs)
        self.stubs.Set(self.libvirtconnection.firewall_driver,
                       'unfilter_instance', fake_unfilter_instance)

        self.mox.ReplayAll()
        self.libvirtconnection._cleanup_resize(ins_ref,
                                            _fake_network_info(self.stubs, 1))

    def test_get_instance_disk_info_exception(self):
        instance_name = "fake-instance-name"

        class FakeExceptionDomain(FakeVirtDomain):
            def __init__(self):
                super(FakeExceptionDomain, self).__init__()

            def XMLDesc(self, *args):
                raise libvirt.libvirtError("Libvirt error")

        def fake_lookup_by_name(instance_name):
            return FakeExceptionDomain()

        self.stubs.Set(self.libvirtconnection, '_lookup_by_name',
                       fake_lookup_by_name)
        self.assertRaises(exception.InstanceNotFound,
            self.libvirtconnection.get_instance_disk_info,
            instance_name)


class LibvirtVolumeUsageTestCase(test.TestCase):
    """Test for nova.virt.libvirt.libvirt_driver.LibvirtDriver
       .get_all_volume_usage"""

    def setUp(self):
        super(LibvirtVolumeUsageTestCase, self).setUp()
        self.conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.c = context.get_admin_context()

        # creating instance
        inst = {}
        inst['uuid'] = '875a8070-d0b9-4949-8b31-104d125c9a64'
        self.ins_ref = db.instance_create(self.c, inst)

        # verify bootable volume device path also
        self.bdms = [{'volume_id': 1,
                      'device_name': '/dev/vde'},
                     {'volume_id': 2,
                      'device_name': 'vda'}]

    def test_get_all_volume_usage(self):
        def fake_block_stats(instance_name, disk):
            return (169L, 688640L, 0L, 0L, -1L)

        self.stubs.Set(self.conn, 'block_stats', fake_block_stats)
        vol_usage = self.conn.get_all_volume_usage(self.c,
              [dict(instance=self.ins_ref, instance_bdms=self.bdms)])

        expected_usage = [{'volume': 1,
                           'instance': self.ins_ref,
                           'rd_bytes': 688640L, 'wr_req': 0L,
                           'flush_operations': -1L, 'rd_req': 169L,
                           'wr_bytes': 0L},
                           {'volume': 2,
                            'instance': self.ins_ref,
                            'rd_bytes': 688640L, 'wr_req': 0L,
                            'flush_operations': -1L, 'rd_req': 169L,
                            'wr_bytes': 0L}]
        self.assertEqual(vol_usage, expected_usage)

    def test_get_all_volume_usage_device_not_found(self):
        def fake_lookup(instance_name):
            raise libvirt.libvirtError('invalid path')

        self.stubs.Set(self.conn, '_lookup_by_name', fake_lookup)
        vol_usage = self.conn.get_all_volume_usage(self.c,
              [dict(instance=self.ins_ref, instance_bdms=self.bdms)])
        self.assertEqual(vol_usage, [])


class LibvirtNonblockingTestCase(test.TestCase):
    """Test libvirt_nonblocking option."""

    def setUp(self):
        super(LibvirtNonblockingTestCase, self).setUp()
        self.flags(libvirt_nonblocking=True, libvirt_uri="test:///default")

    def test_connection_to_primitive(self):
        # Test bug 962840.
        import nova.virt.libvirt.driver as libvirt_driver
        connection = libvirt_driver.LibvirtDriver('')
        jsonutils.to_primitive(connection._conn, convert_instances=True)
