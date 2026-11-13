"""Protocol 2 migration, ownership preservation and kernel transaction tests.

All block-device writes use regular files under a temporary root. These are not
claims of device power-cut or modem validation.
"""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys
import mmap
import tracemalloc
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from test_ota import file, manifest, package
from backend.ota_engine import Engine, Patch, descriptor, canonical
from boot import bind, layout, archives, entries, overlay, target, Binder


def boot_image(slot='a', kernel=b'kernel-before', family='e5', payload=0, extra=None):
    init = b'#!/bin/sh\n    log "stage=switch-root root=$rootdir init=$init"\n    exec switch_root /newroot $init\n'
    files = {'init': (init, stat.S_IFREG|0o755),
             'etc/e5/linux-slot': (slot.encode()+b'\n', stat.S_IFREG|0o644),
             'etc/misc-bc-slot-b-trial.bin': (b'_'+slot.encode()+b'\0'*30, stat.S_IFREG|0o644)}
    files.update(extra or {})
    if payload: files['linux-modules/large.ko'] = (b'x'*payload, stat.S_IFREG|0o644)
    ram = overlay(files)
    header = bytearray(4096); header[:8] = b'ANDROID!'
    struct.pack_into('<II', header, 8, len(kernel), len(ram)); struct.pack_into('<I', header, 40, 4)
    command = b'root=installed-device custom=keep'
    header[44:44+len(command)] = command
    body = bytes(header)+kernel.ljust((len(kernel)+4095)&~4095, b'\0')+ram
    body = body.ljust((len(body)+4095)&~4095, b'\0'); avb = len(body)
    body += b'test-avb'
    footer = bytearray(64); footer[:4] = b'AVBf'
    struct.pack_into('>QQQ', footer, 12, avb, avb, 8)
    return body.ljust((64 << 20)-64, b'\0')+footer


def joined_cpio_boot(image, late, block_size=4096, padding=512):
    """Model lz4 -dc | lz4: multiple newc archives in one legacy frame."""
    import boot
    start, size, avb, avb_size, _ = layout(image, 'mu300')
    chunks = list(archives(image[start:start+size]))
    chunks += list(archives(overlay(late)))
    raw = b'\0'*padding + b''.join(chunk.ljust((len(chunk)+padding-1)//padding*padding, b'\0')
                                             for chunk in chunks) + b'\0'*padding
    ram = bytearray(boot.MAGIC)
    for offset in range(0, len(raw), block_size):
        data = raw[offset:offset+block_size]
        token = bytearray([min(len(data), 15) << 4])
        if len(data) >= 15:
            length = len(data)-15
            token.extend(b'\xff'*(length//255)); token.append(length%255)
        token.extend(data)
        ram.extend(struct.pack('<I', len(token))); ram.extend(token)
    header = bytearray(image[:4096]); struct.pack_into('<I', header, 12, len(ram))
    body = bytes(header)+image[4096:start]+bytes(ram)
    body = body.ljust((len(body)+4095)&~4095, b'\0'); new_avb = len(body)
    body += image[avb:avb+avb_size]
    footer = bytearray(image[-64:]); struct.pack_into('>QQQ', footer, 12, new_avb, new_avb, avb_size)
    return body.ljust((64 << 20)-64, b'\0')+footer


class BootTests(unittest.TestCase):
    def test_joined_archives_preserve_late_storage_and_init_across_blocks(self):
        import boot
        name = 'etc/mu300-desktop-root-mode'
        initial = boot_image(family='mu300', extra={
            'etc/mu300-root-target': (b'sd\n', stat.S_IFREG|0o644)})
        late_init = b'#!/bin/sh\n# installed final init\n    log "stage=switch-root root=$rootdir init=$init"\n'
        late = {name:(b'part\n', stat.S_IFREG|0o644),
                'init':(late_init, stat.S_IFREG|0o755)}
        for block_size in (7, 113, 4096):
            with self.subTest(block_size=block_size):
                current = joined_cpio_boot(initial, late, block_size)
                found = boot.metadata(current, 'mu300', lambda _:True)
                self.assertEqual(found[name][0], b'part\n')
                self.assertEqual(found['init'][0], late_init)
                start,size,*_ = layout(current, 'mu300')
                decoded = {n:data for raw in archives(current[start:start+size])
                           for n,data,_ in entries(raw)}
                self.assertEqual(decoded[name], b'part\n')
                self.assertEqual(decoded['init'], late_init)

    def test_binding_uses_late_roles_in_merged_frames_for_both_families(self):
        import boot
        for family,name,value in (
                ('mu300','etc/mu300-desktop-root-mode',b'part\n'),
                ('e5','etc/e5/boot-request',b'e5.root=part\n')):
            current = joined_cpio_boot(boot_image('a',family=family),
                                      {name:(value,stat.S_IFREG|0o644)}, padding=8192)
            incoming = boot_image('b',kernel=b'new-kernel',family=family)
            updated = bind(incoming,current,family)
            self.assertEqual(boot.metadata(updated,family,lambda n:n==name)[name][0],value)
            self.assertEqual(updated[4096:4106],b'new-kernel')
            self.assertEqual(updated[44:1580],current[44:1580])
            self.assertEqual(bind(updated,updated,family),updated)

    def test_joined_archive_does_not_hide_malformed_nonzero_tail(self):
        import boot
        raw = next(archives(overlay({'x':(b'data',stat.S_IFREG|0o644)})))
        with self.assertRaises(ValueError): list(entries(raw+b'\0'*512+b'not-a-cpio'))
        stream = boot._BlockStream(iter((b'\0'*4096,b'070701')))
        with self.assertRaises(ValueError): stream.cpio_header()

    def test_e5_partition_request_survives_guard_and_repeated_updates_without_cmdline_help(self):
        import boot
        name = 'etc/e5/boot-request'
        for slot in ('a', 'b'):
            current = bytearray(boot_image(slot, extra={name: (b'e5.root=part\n', stat.S_IFREG|0o644)}))
            current[44:1580] = b'e5.part_v=2 e5.root=part'.ljust(1536, b'\0')
            current = bytes(current)
            guarded = bind(current, current, 'e5')
            self.assertEqual(boot.metadata(guarded, 'e5', lambda n:n == name)[name][0], b'e5.root=part\n')
            sizes = []
            for number in range(3):
                incoming = boot_image('b' if slot == 'a' else 'a', kernel=b'new-kernel-'+str(number).encode())
                result = bind(incoming, current, 'e5')
                preserved = boot.metadata(result, 'e5', lambda n:n in (name, 'etc/e5/linux-slot'))
                self.assertEqual(preserved[name][0], b'e5.root=part\n')
                self.assertEqual(preserved['etc/e5/linux-slot'][0], slot.encode()+b'\n')
                self.assertEqual(result[44:1580], current[44:1580])
                self.assertEqual(result[64:1580].split(b'\0', 1)[0], b'part')
                sizes.append(layout(result, 'e5')[1])
                self.assertEqual(bind(result, result, 'e5'), result)
                current = result
            self.assertEqual(len(set(sizes)), 1)

    def test_e5_without_request_does_not_gain_a_partition_override(self):
        import boot
        for family in ('e5', 'mu300'):
            current = boot_image(family=family)
            result = bind(boot_image(kernel=b'new-kernel', family=family), current, family)
            self.assertNotIn('etc/e5/boot-request', boot.metadata(result, family, lambda n:n == 'etc/e5/boot-request'))
            self.assertEqual(result[44:1580], current[44:1580])

    def test_e5_one_shot_or_invalid_requests_are_rejected_before_writes(self):
        name = 'etc/e5/boot-request'
        for value, mode in ((b'e5.part_prepare=token:cid:plan\n', stat.S_IFREG|0o644),
                            (b'\ne5.root=part\n', stat.S_IFREG|0o644),
                            (b'e5.root=part;reboot\n', stat.S_IFREG|0o644),
                            (b'e5.root=part\ne5.root=sd\n', stat.S_IFREG|0o644),
                            (b'e5.root=part', stat.S_IFLNK|0o777)):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'not a persistent partition request'):
                Binder(boot_image(extra={name: (value, mode)}), 'e5')
        incoming = boot_image(extra={name: (b'e5.root=part\n', stat.S_IFREG|0o644)})
        with self.assertRaisesRegex(ValueError, 'installation-specific request'):
            bind(incoming, boot_image(), 'e5')

    def test_file_binding_matches_byte_api_without_loading_full_image(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory)/'old'; new = Path(directory)/'new'
            old.write_bytes(boot_image('a')); new.write_bytes(boot_image('b', b'new-kernel'))
            expected = bind(new.read_bytes(), old.read_bytes(), 'e5')
            with old.open('rb') as stream, mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ) as current:
                Binder(current,'e5').prepare_file(new)
            actual = new.read_bytes(); *_,limit = layout(actual,'e5')
            self.assertEqual(actual[:limit],expected[:limit]); self.assertEqual(actual[-64:],expected[-64:])

    def test_large_module_payload_is_skipped_with_bounded_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'boot'
            path.write_bytes(boot_image(payload=48 << 20))
            with path.open('rb') as stream, mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ) as image:
                tracemalloc.start()
                try:
                    binder = Binder(image,'e5')
                    _,peak = tracemalloc.get_traced_memory()
                    self.assertIn('etc/e5/linux-slot',binder.roles)
                    self.assertLess(peak,48 << 20,'A whole decoded ramdisk was retained')
                finally: tracemalloc.stop()

    def test_light_python_without_ctypes_can_decode(self):
        import boot
        import builtins
        from types import SimpleNamespace
        original = builtins.__import__
        def importing(name, *args, **kwargs):
            if name == 'ctypes': raise ImportError('ctypes omitted by python3-light')
            return original(name, *args, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory)/'lz4raw-aarch64.so'; helper.touch()
            with patch.object(boot, '_native_decode', None), patch.object(boot.Path, 'with_name', return_value=helper), patch.object(builtins, '__import__', side_effect=importing), patch.object(boot.os, 'uname', return_value=SimpleNamespace(sysname='Linux', machine='aarch64'), create=True):
                self.assertEqual(boot.block_decode(b'\x50hello'), b'hello')

    def test_foreign_arm64_helper_never_reaches_host_loader(self):
        import boot
        import ctypes
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory)/'lz4raw-aarch64.so'
            helper.write_bytes(b'\x7fELF\x02')
            for system, machine in (('Windows','AMD64'), ('Darwin','arm64'),
                                    ('Linux','x86_64'), ('Windows','ARM64')):
                with self.subTest(system=system, machine=machine), \
                     patch.object(boot, '_native_decode', None), \
                     patch.object(boot.Path, 'with_name', return_value=helper), \
                     patch.object(boot.os, 'uname', return_value=SimpleNamespace(sysname=system, machine=machine), create=True), \
                     patch.object(ctypes, 'CDLL', side_effect=AssertionError('foreign binary loaded')) as loader:
                    self.assertEqual(boot.block_decode(b'\x50hello'), b'hello')
                    loader.assert_not_called()

    def test_linux_arm64_still_uses_packaged_native_decoder(self):
        import boot
        import ctypes
        from types import SimpleNamespace
        from unittest.mock import Mock
        library = Mock()
        def decode(data, size, buffer, capacity):
            ctypes.memmove(buffer, b'hello', 5)
            return 5
        library.unisoc_lz4_decode.side_effect = decode
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory)/'lz4raw-aarch64.so'
            helper.write_bytes(b'\x7fELF\x02')
            with patch.object(boot, '_native_decode', None), \
                 patch.object(boot.Path, 'with_name', return_value=helper), \
                 patch.object(boot.os, 'uname', return_value=SimpleNamespace(sysname='Linux', machine='aarch64'), create=True), \
                 patch.object(ctypes, 'CDLL', return_value=library) as loader:
                self.assertEqual(boot.block_decode(b'\x50hello'), b'hello')
                loader.assert_called_once_with(str(helper))

    def test_kernel_and_slot_bindings_and_diagnostic_bytes(self):
        current = bytearray(boot_image('a')); current[56 << 20:(56 << 20)+8] = b'LIVE-LOG'
        incoming = boot_image('b', b'new-kernel')
        result = bind(incoming, bytes(current), 'e5')
        self.assertEqual(result[44:1580], current[44:1580])
        self.assertEqual(result[4096:4106], b'new-kernel')
        self.assertEqual(result[56 << 20:(56 << 20)+8], b'LIVE-LOG')
        start, size, *_ = layout(result, 'e5')
        contents = {}
        for archive in archives(result[start:start+size]):
            contents.update({n:c for n,c,_ in entries(archive)})
        self.assertEqual(contents['etc/e5/linux-slot'], b'a\n')
        self.assertEqual(contents['etc/misc-bc-slot-b-trial.bin'][:2], b'_a')
        self.assertIn(b'unisoc-ota-recovery-v2', contents['init'])
        self.assertLess(contents['init'].index(b'unisoc-ota-recovery-v2'), contents['init'].index(b'exec switch_root'))
        self.assertEqual(bind(result, result, 'e5'), result)

    def test_mu300_storage_and_identity_survive_tf_release_boot(self):
        for slot in ('a', 'b'):
            for mode in (b'part\n', b'sd\n', b'legacy\n', None):
                extra = {'etc/mu300-desktop-identity': (b'{"device":"original"}\n', stat.S_IFREG|0o600)}
                if mode is not None:
                    extra['etc/mu300-desktop-root-mode'] = (mode, stat.S_IFREG|0o644)
                    extra['etc/mu300-root-target'] = (b'sd\n', stat.S_IFREG|0o644)
                current = boot_image(slot, family='mu300', extra=extra)
                incoming = boot_image('b' if slot == 'a' else 'a', family='mu300', extra={
                    'etc/mu300-root-target': (b'sd\n', stat.S_IFREG|0o644),
                    'etc/mu300-desktop-root-mode': (b'sd\n', stat.S_IFREG|0o644),
                    'etc/mu300-desktop-identity': (b'wrong-device', stat.S_IFREG|0o644)})
                result = bind(incoming, current, 'mu300')
                start, size, *_ = layout(result, 'mu300')
                contents = {}
                for archive in archives(result[start:start+size]):
                    contents.update({n:c for n,c,_ in entries(archive)})
                self.assertEqual(contents['etc/mu300-desktop-root-mode'], mode or b'')
                self.assertEqual(contents['etc/mu300-root-target'], b'sd\n' if mode is not None else b'')
                self.assertEqual(contents['etc/mu300-desktop-identity'], extra['etc/mu300-desktop-identity'][0])
                self.assertEqual(contents['etc/e5/linux-slot'], slot.encode()+b'\n')

    def test_malformed_boot_refused(self):
        with self.assertRaises(ValueError): bind(b'bad', boot_image(), 'e5')


@unittest.skipUnless(sys.platform == 'linux', 'Linux filesystem transaction tests')
class FullUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('etc/e5', 'usr/lib', 'lib/apk/db', 'bin', 'lib/modules/new', 'dev/block/by-name'):
            (self.root/name).mkdir(parents=True, exist_ok=True)
        (self.root/'etc/e5/release.json').write_text(json.dumps(dict(profile='e5', image_version='e5-r1', kernel_release='old-kernel')))
        (self.root/'etc/e5/linux-slot').write_text('a\n')
        (self.root/'dev/block/by-name/boot_a').write_bytes(boot_image('a'))
        (self.root/'dev/block/by-name/boot_b').write_bytes(b'ANDROID-DO-NOT-WRITE')
        self.engine = Engine(self.root)

    def test_e5_partition_request_survives_real_transaction_and_rollback_in_both_slots(self):
        import boot
        import subprocess
        request = 'etc/e5/boot-request'
        for slot in ('a', 'b'):
            with self.subTest(slot=slot):
                linux = self.root/('dev/block/by-name/boot_'+slot)
                android = self.root/('dev/block/by-name/boot_'+('b' if slot == 'a' else 'a'))
                original = boot_image(slot, extra={request:(b'e5.root=part\n', stat.S_IFREG|0o644)})
                linux.write_bytes(original); android.write_bytes(b'ANDROID-DO-NOT-WRITE')
                (self.root/'etc/e5/linux-slot').write_text(slot+'\n')
                (self.root/'usr/lib/test').write_bytes(b'old-file')
                update = self.update([('usr/lib/test', b'new-file', None)], boot=True)
                with Patch(update) as package:
                    self.engine.apply(package)
                self.assertEqual(boot.metadata(linux.read_bytes(), 'e5', lambda n:n == request)[request][0], b'e5.root=part\n')
                self.assertEqual(android.read_bytes(), b'ANDROID-DO-NOT-WRITE')
                result = subprocess.run([sys.executable, str(Path(boot.__file__).with_name('engine.py')),
                                         'rollback', '--root', str(self.root)], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                self.assertEqual(linux.read_bytes(), original)
                self.assertEqual(android.read_bytes(), b'ANDROID-DO-NOT-WRITE')
                self.assertEqual((self.root/'usr/lib/test').read_bytes(), b'old-file')

    def update(self, updates, boot=False, base='e5-r1', target_version='e5-r2'):
        m = manifest(); m['operations'] = []; blobs = {}
        m.update(base_version=base, target_version=target_version)
        for name, value, merge in updates:
            before = descriptor(self.root/name)
            desc = file(value) if value is not None else None
            op = dict(path=name, before=before, after=desc)
            if merge: op['merge'] = merge
            m['operations'].append(op)
            if value is not None: blobs[desc['sha256']] = value
        if boot:
            content = boot_image('b', b'new-kernel')
            h = hashlib.sha256(content).hexdigest(); blobs[h] = content
            m['boot'] = dict(sha256=h, size=len(content))
            m['kernel_release'] = 'new-kernel'
        path = self.root/'patch.ota'; package(path, m, blobs)
        return path

    def test_packages_plugins_configs_data_self_update_and_rollback(self):
        old = b'P:base\nV:1\nA:aarch64\nF:bin\nR:busybox\n\nP:user-plugin\nV:99\nF:usr/lib\nR:user-plugin\n\n'
        new = b'P:base\nV:2\nA:aarch64\nF:bin\nR:busybox\n\nP:new-dependency\nV:1\n\nP:user-plugin\nV:2\nF:usr/lib\nR:user-plugin\nR:new-plugin-helper\n\n'
        (self.root/'lib/apk/db/installed').write_bytes(old)
        (self.root/'usr/lib/user-plugin').write_bytes(b'user-binary')
        (self.root/'bin/busybox').write_bytes(b'old-busybox')
        (self.root/'etc/shadow').write_bytes(b'root:my-password:0::::::\n')
        (self.root/'etc/config').mkdir(); (self.root/'etc/config/wireless').write_bytes(b'my-ssid-and-password')
        (self.root/'root').mkdir(); (self.root/'root/data.db').write_bytes(b'my-data')
        p = self.update([('lib/apk/db/installed', new, 'apk-db'),
                         ('bin/busybox', b'new-busybox', None),
                         ('usr/lib/user-plugin', b'package-collision', None),
                         ('usr/lib/new-plugin-helper', b'belongs-to-other-plugin-version', None),
                         ('usr/lib/engine.py', b'new-updater', None),
                         ('etc/shadow', b'root:default:0::::::\nnewuser:!:0::::::\n', 'accounts')])
        # Record the base package universe: the extra user plugin is not owned
        # by the full release and must keep both its database row and its files.
        import zipfile
        with Patch(p) as patch:
            patch.manifest['base_packages'] = {'base': {'V':'1'}}
            patch.manifest['target_packages'] = {'base': {'V':'2'}, 'new-dependency': {'V':'1'}}
            result = self.engine.apply(patch)
        self.assertEqual(result['status'], 'complete')
        db = (self.root/'lib/apk/db/installed').read_bytes()
        self.assertIn(b'P:base\nV:2', db); self.assertIn(b'P:user-plugin\nV:99', db)
        self.assertIn(b'P:new-dependency', db)
        self.assertEqual((self.root/'usr/lib/user-plugin').read_bytes(), b'user-binary')
        self.assertFalse((self.root/'usr/lib/new-plugin-helper').exists())
        self.assertEqual((self.root/'bin/busybox').read_bytes(), b'new-busybox')
        self.assertTrue((self.root/'etc/shadow').read_bytes().startswith(b'root:my-password:'))
        self.assertIn(b'newuser:!', (self.root/'etc/shadow').read_bytes())
        self.assertEqual((self.root/'etc/config/wireless').read_bytes(), b'my-ssid-and-password')
        self.assertEqual((self.root/'root/data.db').read_bytes(), b'my-data')
        journal = self.engine.state/'journal.json'; data = json.loads(journal.read_text())
        data['status'] = 'rolling_back'; journal.write_bytes(canonical(data))
        self.engine.recover()
        self.assertEqual((self.root/'lib/apk/db/installed').read_bytes(), old)
        self.assertEqual((self.root/'bin/busybox').read_bytes(), b'old-busybox')
        self.assertFalse((self.root/'usr/lib/engine.py').exists())

    def test_linux_a_updated_android_b_untouched_with_modules(self):
        (self.root/'lib/modules/new/wifi.ko').write_bytes(b'old-module')
        p = self.update([('lib/modules/new/wifi.ko', b'new-module', None)], boot=True)
        original = (self.root/'dev/block/by-name/boot_a').read_bytes()
        with Patch(p) as patch: self.engine.apply(patch)
        self.assertEqual((self.root/'dev/block/by-name/boot_b').read_bytes(), b'ANDROID-DO-NOT-WRITE')
        self.assertEqual(self.engine.identity()['kernel_release'], 'new-kernel')
        self.assertIn(b'new-kernel', (self.root/'dev/block/by-name/boot_a').read_bytes()[:8192])
        journal = self.engine.state/'journal.json'; data = json.loads(journal.read_text())
        data['status'] = 'rolling_back'; journal.write_bytes(canonical(data)); self.engine.recover()
        self.assertEqual((self.root/'dev/block/by-name/boot_a').read_bytes(), original)
        self.assertEqual((self.root/'lib/modules/new/wifi.ko').read_bytes(), b'old-module')

    def test_partial_module_write_restores_kernel_and_root(self):
        (self.root/'usr/lib/test').write_bytes(b'old-file')
        p = self.update([('usr/lib/test', b'new-file', None)], boot=True)
        original = (self.root/'dev/block/by-name/boot_a').read_bytes()
        def fail(*args): raise RuntimeError('simulated interruption after root write')
        with Patch(p) as patch:
            with self.assertRaises(RuntimeError): self.engine.apply(patch, fail)
        self.assertEqual((self.root/'usr/lib/test').read_bytes(), b'old-file')
        self.assertEqual((self.root/'dev/block/by-name/boot_a').read_bytes(), original)
        self.assertEqual((self.root/'dev/block/by-name/boot_b').read_bytes(), b'ANDROID-DO-NOT-WRITE')

    def test_file_only_update_installs_guard_once_then_does_not_reflash(self):
        import boot
        (self.root/'usr/lib/test').write_bytes(b'old')
        inspect=self.engine.inspect
        def live_inspect(patch):
            result=inspect(patch);result['boot_work']=True;return result
        first=self.update([('usr/lib/test',b'new',None)])
        with patch.object(self.engine,'inspect',side_effect=live_inspect), patch.object(boot,'write_file',wraps=boot.write_file) as writes:
            with Patch(first) as package:self.engine.apply(package)
            self.assertEqual(writes.call_count,1)
        second=self.update([('usr/lib/test',b'newer',None)],base='e5-r2',target_version='e5-r3')
        with patch.object(self.engine,'inspect',side_effect=live_inspect), patch.object(boot,'write_file',wraps=boot.write_file) as writes:
            with Patch(second) as package:self.engine.apply(package)
            self.assertEqual(writes.call_count,0)

    def test_manual_rollback_rejects_subsequently_modified_boot(self):
        (self.root/'usr/lib/test').write_bytes(b'old')
        p=self.update([('usr/lib/test',b'new',None)],boot=True)
        with Patch(p) as patch:self.engine.apply(patch)
        with (self.root/'dev/block/by-name/boot_a').open('r+b') as image:
            image.seek(4096);image.write(b'changed')
        import subprocess
        engine=Path(__file__).resolve().parents[1]/'runtime/luci-app-unisoc-ota/root/usr/lib/unisoc-ota/engine.py'
        result=subprocess.run([sys.executable,str(engine),'rollback','--root',str(self.root)],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0);self.assertIn('changed=boot',result.stdout)
        self.assertEqual((self.root/'usr/lib/test').read_bytes(),b'new')
