"""Boot-only recovery: real ramdisk/ext4 parsers, simulated device writes."""
import hashlib
import io
from pathlib import Path
import re
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import boot_recovery as br
from backend.boot_image import boot_installed_e5, boot_installed_mu300
from backend.state import Context, Store, RecoveryRequired
from backend.uninstall_boot import overlay
from test_state import PACKAGE, DEVICE
from test_uninstall_evidence import image, marker_files


RELEASE = '7.2.8'


def boot(family='mu300', route='part', slot='b', release=RELEASE):
    kernel = (b'ARM64 fixture\0Linux version '+release.encode()+b' (fixture)\0').ljust(4096, b'\0')
    files = {'init': b'#!/bin/sh\n. /etc/mu300-desktop-storage.sh\n' if family == 'mu300' else b'#!/bin/sh\ncat /etc/e5/part-root\n'}
    if family == 'mu300':
        if route == 'tf':
            files = {'init': b'#!/bin/sh\n# TF only\n', 'etc/mu300-root-target':b'sd\n'}
        else:
            files['etc/mu300-desktop-root-mode'] = route.encode()+b'\n'
    else:
        files['etc/e5/linux-slot'] = slot.encode()+b'\n'
    ram = overlay(files)
    data = bytearray(64 << 20); data[:8] = b'ANDROID!'
    struct.pack_into('<II', data, 8, len(kernel), len(ram))
    struct.pack_into('<I', data, 20, 1584); struct.pack_into('<I', data, 40, 4)
    command = b'loglevel=5' if family == 'mu300' else b'loglevel=5 e5.part_v=2'
    data[44:44+len(command)] = command
    data[4096:8192] = kernel; data[8192:8192+len(ram)] = ram
    end = (8192+len(ram)+4095)&~4095
    data[-64:-60] = b'AVBf'; struct.pack_into('>QQQ',data,len(data)-52,end,end,4096)
    return bytes(data)


class Fixture:
    def __init__(self, family='mu300', route='tf', slot='a', broken=False, release=RELEASE):
        self.family, self.slot = family, slot
        self.other = 'b' if slot == 'a' else 'a'
        self.target = '/dev/block/by-name/boot_'+self.other
        self.original = bytes(64 << 20) if broken else boot(family,route,self.other)
        self.current = self.original
        self.packaged = boot(family,'tf' if family == 'mu300' else 'part',self.other)
        self.rebuilt = boot(family,'part',self.other)
        self.files = marker_files(family)
        prefix = 'openwrt/' if family == 'mu300' else ''
        self.files[prefix+'lib/modules/'+release+'/sipc-core.ko'] = b'\x7fELF\0vermagic='+release.encode()+b' SMP preempt modversions aarch64\0'
        self.fs = image(self.files)
        self.commands, self.reads, self.remote, self.writes = [], [], {}, 0
        self.busy = False; self.interrupt = None; self.bad_upload = False
        self.device = {**DEVICE, 'slot':'_'+slot, 'existing':True,
                       'storage':{'ROOT':'mmcblk0p76','ROOT_SIZE':len(self.fs)//512,'ROOT_START':8192,'DISK':'mmcblk0'}}
        self.adb = SimpleNamespace(shell=Mock(side_effect=self.shell), read=Mock(side_effect=self.read),
                                   call=Mock(side_effect=self.call), backup_partition=Mock(side_effect=self.backup), cleanup_backup=Mock())
        builder = SimpleNamespace(build=Mock(return_value=self.rebuilt))
        self.adapter = SimpleNamespace(kind=family, root=Path('.'), adb=self.adb,
            legacy=SimpleNamespace(module=Mock(return_value=builder), slot_api=Mock(return_value={'bind_boot':Mock(return_value=self.rebuilt)})), stage=Mock())

    def read(self, command):
        self.reads.append(command)
        m = re.fullmatch(r'dd if=(\S+) bs=(\d+)(?: skip=(\d+))? count=(\d+) 2>/dev/null',command)
        if not m: raise AssertionError(command)
        path, size, skip, count = m[1], int(m[2]), int(m[3] or 0), int(m[4])
        source = self.current if path == self.target else self.fs if path.endswith(('mmcblk0p76','mmcblk1p1')) else bytes(4096)
        return source[size*skip:size*(skip+count)]

    def shell(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith('blockdev '): return str(64 << 20)
        if command.startswith('sha256sum '):
            name = command.split()[1]
            data = self.current if name == self.target else self.remote[name]
            return hashlib.sha256(data).hexdigest()+' file'
        if 'echo IDLE' in command: return 'IDLE'
        if command.startswith('for p in /proc/'): return 'BUSY' if self.busy else ''
        if command.startswith(('umask 077;', 'rm -f ')): return ''
        if command.startswith('sh /data/local/tmp/openwrt-boot-repair-'):
            self.writes += 1
            script = self.remote[command[3:]].decode()
            self.current = next(v for n,v in self.remote.items() if n.endswith('/boot.img'))
            assert 'of='+self.target in script and 'ro.boot.slot_suffix' in script
            if self.interrupt:
                mode, self.interrupt = self.interrupt, None
                if mode == 'partial': self.current = self.current[:4096]+bytes((64 << 20)-4096)
                raise ConnectionError('ADB lost after write started')
            return 'BOOT-REPAIR-VERIFIED\n'
        raise AssertionError(command)

    def call(self, *args, **kwargs):
        assert args[0] == 'push'
        self.remote[args[2]] = Path(args[1]).read_bytes()
        if self.bad_upload and args[2].endswith('/boot.img'): self.remote[args[2]] = b'corrupt'

    def backup(self, name, destination, context):
        Path(destination).write_bytes(self.current if name.startswith('boot_') else bytes(4096))

    def plan(self, **kwargs):
        with patch.object(br, 'base_boot', return_value=self.packaged):
            return br.prepare(self.adapter, self.device, **kwargs)

    def context(self, directory, info, new):
        store = Store(directory)
        r = store.create({**PACKAGE,'adapter':self.family},self.device,{'method':'boot'},
                         {'method':'boot','target':'misc','openwrt_bytes':0,'boot_check':info})
        ctx = Context(store,r,lambda *_:None);br.persist(ctx,info,new)
        self.adapter.ctx = ctx
        return ctx


class BootRecovery(unittest.TestCase):
    def test_wrong_tf_image_and_blank_boot_offer_repair_for_both_slots_and_families(self):
        for family, broken in (('mu300',False),('mu300',True),('e5',True)):
            for slot in ('a','b'):
                with self.subTest(family=family,slot=slot,broken=broken):
                    f = Fixture(family,slot=slot,broken=broken)
                    info,new = f.plan()
                    self.assertTrue(info['required']);self.assertEqual(info['slot'],f.other)
                    self.assertEqual(info['root']['kernel_release'],RELEASE)
                    self.assertEqual(new,f.rebuilt)
                    self.assertFalse(f.adb.call.called);self.assertEqual(f.writes,0)
                    self.assertTrue(all('of=' not in cmd and 'mount -' not in cmd and 'e2fsck ' not in cmd for cmd in f.reads+f.commands))

    def test_healthy_internal_tf_legacy_and_image_routes_keep_misc_only(self):
        for family,route,tf in (('e5','part',False),('mu300','part',False),('mu300','tf',True),('mu300','legacy',False),('mu300','data-image',False)):
            with self.subTest(family=family,route=route):
                f = Fixture(family,route);f.device['tf'] = '/dev/block/mmcblk1p1' if tf else None
                info,new = f.plan()
                self.assertFalse(info['required']);self.assertIsNone(new)
                self.assertFalse(any('mmcblk0p76' in cmd for cmd in f.reads))

    def test_explicit_rebuild_handles_failure_not_visible_in_the_header(self):
        f = Fixture(route='part');info,new = f.plan(force=True)
        self.assertTrue(info['required']);self.assertIsNotNone(new)

    def test_tf_repair_uses_sd_route_and_keeps_the_existing_card(self):
        for family in ('mu300','e5'):
            with self.subTest(family=family):
                f=Fixture(family,broken=True)
                f.device.update(existing=False,tf='/dev/block/mmcblk1p1')
                f.device['storage']['ROOT']=None
                info,new=f.plan()
                self.assertTrue(info['required']);self.assertEqual(info['layout'],'sd')
                self.assertEqual(info['root']['node'],'mmcblk1p1')
                self.assertEqual(f.writes,0)
                if family=='mu300':
                    self.assertEqual(f.adapter.legacy.module.return_value.build.call_args.args[2],'sd')

    def test_staged_binary_checksum_failure_is_not_boot_damage(self):
        f=Fixture()
        read=f.adb.read.side_effect
        def invalid(command):
            if 'skip=2 ' in command:raise ValueError('Binary download checksum mismatch')
            return read(command)
        f.adb.read.side_effect=invalid
        with self.assertRaises(ConnectionError):f.plan()
        f.adapter.legacy.module.assert_not_called()

    def test_wrong_module_version_and_foreign_root_stop_before_any_writes(self):
        f = Fixture(release='6.18.54')
        with self.assertRaisesRegex(RecoveryRequired,'内核模块版本'):f.plan()
        f = Fixture();f.files['openwrt/etc/mu300/desktop-mode'] = b'sd';f.fs = image(f.files)
        with self.assertRaisesRegex(RecoveryRequired,'布局'):f.plan()
        self.assertEqual(f.writes,0)

    def test_transport_errors_are_not_misclassified_as_boot_damage(self):
        f = Fixture();f.adb.read.side_effect = ConnectionError('ADB unavailable')
        with self.assertRaises(ConnectionError):f.plan()
        f.adapter.legacy.module.assert_not_called()

    def test_success_only_backs_up_and_writes_inactive_boot(self):
        for family,slot in (('mu300','a'),('mu300','b'),('e5','b')):
            with self.subTest(family=family,slot=slot),tempfile.TemporaryDirectory() as temp:
                f = Fixture(family,slot=slot,broken=True);info,new = f.plan();ctx=f.context(temp,info,new)
                br.repair(f.adapter,f.device,info)
                self.assertEqual(f.writes,1);self.assertEqual(f.current,f.rebuilt)
                self.assertEqual([c.args[0] for c in f.adb.backup_partition.call_args_list],['boot_'+f.other,'misc'])
                self.assertEqual(ctx.record['effects']['boot-repair-write']['state'],'done')
                self.assertTrue((ctx.directory/'boot-repair-backup'/('boot_'+f.other+'.img')).is_file())
                staging=[c for c in f.adb.shell.call_args_list if c.args[0].startswith('umask 077; mkdir -p ')]
                self.assertEqual(len(staging),1)
                self.assertFalse(staging[0].kwargs.get('root',False),'ADB uploads need a shell-owned private directory')

    def test_lost_reply_and_partial_write_resume_without_installing_or_erasing_root(self):
        for mode,expected in (('complete',1),('partial',2)):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as temp:
                f=Fixture();info,new=f.plan();ctx=f.context(temp,info,new);f.interrupt=mode
                with self.assertRaises(ConnectionError):br.repair(f.adapter,f.device,info)
                # Recreate the persisted coordinator as after restarting the app.
                f.adapter.ctx=Context(ctx.store,ctx.store.load(ctx.record['id']),lambda *_:None)
                br.repair(f.adapter,f.device,info)
                self.assertEqual(f.writes,expected);self.assertEqual(f.current,f.rebuilt)

    def test_active_writer_corrupt_upload_and_missing_backup_never_reach_dd(self):
        for failure in ('active','upload','backup'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as temp:
                f=Fixture();info,new=f.plan();ctx=f.context(temp,info,new)
                f.busy=failure=='active';f.bad_upload=failure=='upload'
                if failure=='backup':f.adb.backup_partition.side_effect=lambda *args:None
                with self.assertRaises((ValueError,RecoveryRequired,FileNotFoundError)):br.repair(f.adapter,f.device,info)
                self.assertEqual(f.writes,0);self.assertEqual(f.current,f.original)

    def test_changed_root_cannot_use_a_previously_confirmed_boot_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            f=Fixture();info,new=f.plan();f.context(temp,info,new)
            changed=bytearray(f.fs);changed[1128:1144]=b'V'*16;f.fs=bytes(changed)
            with self.assertRaisesRegex(RecoveryRequired,'身份'):br.repair(f.adapter,f.device,info)
            f.adb.backup_partition.assert_not_called()

    def test_mu300_chain_must_pass_before_the_repair_callback_and_misc(self):
        from test_mu300_boot_switch import AndroidFixture, MU300Switch
        for slot in ('a','b'):
            with self.subTest(slot=slot):
                android=AndroidFixture(slot)
                valid=android.header;android.header=bytes(4096)
                def repair():
                    android.calls.append(('boot-repair',))
                    android.header=valid
                result=boot_installed_mu300(MU300Switch().api(),android,Path('/fixture'),repair=repair)
                self.assertEqual(result.state,'success')
                names=[c[0] for c in android.calls]
                self.assertLess(names.index('chain'),names.index('boot-repair'))
                self.assertLess(names.index('boot-repair'),names.index('misc'))
                android=AndroidFixture(slot);android.chain_failed=True
                callback=Mock()
                with self.assertRaises(ValueError):boot_installed_mu300(MU300Switch().api(),android,Path('/fixture'),repair=callback)
                callback.assert_not_called()

    def test_e5_chain_must_pass_before_repair_then_reinspect(self):
        from test_boot_switch import fixture
        header,tail,size=fixture()
        api={name:Mock() for name in ('steps','find_adb','android_checks','bootchain','arm_slot_b','say','adb')}
        api['current_slots']=Mock(return_value=('b','a'));api['device_size']=Mock(return_value=size)
        callback=Mock()
        boot_installed_e5(api,Mock(side_effect=[header,tail]),repair=callback)
        callback.assert_called_once();api['bootchain'].assert_called_once_with('check')
        api['arm_slot_b'].assert_called_once()
        api['bootchain'].side_effect=ValueError('chain invalid');callback.reset_mock();api['arm_slot_b'].reset_mock()
        with self.assertRaises(ValueError):boot_installed_e5(api,Mock(),repair=callback)
        callback.assert_not_called();api['arm_slot_b'].assert_not_called()


if __name__ == '__main__': unittest.main()
