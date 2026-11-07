"""Partition lookup canonicalizes its fake/live root before containment checks."""
import os
from pathlib import Path
import runpy
import tempfile
import unittest

TOP = Path(__file__).resolve().parents[1]
partition = runpy.run_path(str(TOP/'runtime/luci-app-unisoc-ota/root/usr/lib/unisoc-ota/boot.py'))['partition_device']


class PartitionPaths(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base/'root'
        self.by = self.root/'dev/block/by-name'
        self.by.mkdir(parents=True)
        self.boot = self.by/'boot_a'
        self.boot.write_bytes(b'LINUX-BOOT')

    def test_absolute_root_and_relative_root_select_same_device(self):
        self.assertEqual(partition(self.root, 'boot_a'), self.boot.resolve())
        previous = Path.cwd()
        try:
            os.chdir(self.base)
            self.assertEqual(partition(Path('root'), 'boot_a'), self.boot.resolve())
        finally:
            os.chdir(previous)

    def test_root_alias_is_canonicalized_before_comparison(self):
        alias = self.base/'alias'
        try: alias.symlink_to(self.root, target_is_directory=True)
        except OSError: self.skipTest('directory symlink unavailable on host')
        self.assertEqual(partition(alias, 'boot_a'), self.boot.resolve())

    @unittest.skipUnless(os.name == 'nt', 'Windows short-path aliases')
    def test_windows_short_root_selects_the_same_device(self):
        import ctypes
        api = ctypes.windll.kernel32.GetShortPathNameW
        api.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        api.restype = ctypes.c_uint
        output = ctypes.create_unicode_buffer(32768)
        if not api(str(self.root), output, len(output)):
            self.skipTest('short paths unavailable on this volume')
        alias = Path(output.value)
        if alias == self.root.resolve():
            self.skipTest('volume did not produce a distinct short path')
        self.assertEqual(partition(alias, 'boot_a'), self.boot.resolve())

    def test_alias_outside_root_is_still_rejected(self):
        outside = self.base/'outside'
        outside.write_bytes(b'NOT-A-BOOT-TARGET')
        self.boot.unlink()
        try: self.boot.symlink_to(outside)
        except OSError: self.skipTest('file symlink unavailable on host')
        with self.assertRaisesRegex(ValueError, 'escapes'):
            partition(self.root, 'boot_a')
        self.assertEqual(outside.read_bytes(), b'NOT-A-BOOT-TARGET')


if __name__ == '__main__': unittest.main()
