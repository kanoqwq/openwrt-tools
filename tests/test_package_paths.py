"""Actual package verification through macOS/Finder-style directory selections."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.package import inspect_package, locate_package


class PackagePathTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def package(self, name, family):
        root = self.base/name
        root.mkdir(parents=True)
        (root/'OpenWrt-Installer.py').write_text('# fixture', encoding='utf-8')
        (root/'OpenWrt-Installer.app/Contents/MacOS').mkdir(parents=True)
        if family == 'e5':
            (root/'files').mkdir()
            (root/'files/device-profile.json').write_text(json.dumps({'profile': 'e5'}))
            payloads = ['files/boot.img', 'files/openwrt.ext4.gz', 'files/ext4-tools.tar']
        else:
            (root/'desktop').mkdir()
            payloads = ['boot-linux.img', 'mu300-openwrt.tar.gz', 'busybox', 'rootfs-critical.sha256', 'rootfs.requirements']
        checks = {}
        for name in payloads:
            data = ('fixture '+name).encode()
            (root/name).write_bytes(data)
            checks[name] = hashlib.sha256(data).hexdigest()
        if family == 'e5':
            (root/'files/SHA256SUMS').write_text(''.join(f'{h}  {name}\n' for name, h in checks.items()))
        else:
            (root/'desktop/manifest.json').write_text(json.dumps({'kernel': '7.2', 'magisk': checks, 'desktop': {}}))
        return root

    def test_actual_hash_verification_from_app_payload_and_wrapper(self):
        for family, subdir in [('e5', 'files'), ('mu300', 'desktop')]:
            root = self.package('下载 安装包 '+family+'/release', family)
            expected = inspect_package(root)
            for selection in [root.parent, root/'OpenWrt-Installer.app', root/'OpenWrt-Installer.app/Contents/MacOS', root/subdir]:
                with self.subTest(selection=str(selection)):
                    self.assertEqual(inspect_package(selection), expected)

    def test_two_releases_require_explicit_selection(self):
        root = self.package('downloads/first', 'mu300')
        self.package('downloads/second', 'e5')
        with self.assertRaisesRegex(ValueError, 'Multiple'):
            inspect_package(root.parent)
        self.assertEqual(inspect_package(root)['adapter'], 'mu300')

    def test_corruption_is_not_hidden_by_path_normalization(self):
        root = self.package('release', 'e5')
        (root/'files/boot.img').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            inspect_package(root/'OpenWrt-Installer.app')

    def test_incomplete_root_never_switches_to_nested_release(self):
        root = self.package('release', 'e5')
        (root/'files/boot.img').unlink()
        self.package('release/other', 'mu300')
        with self.assertRaisesRegex(ValueError, 'Missing or unsafe package file'):
            inspect_package(root)

    def test_zip_missing_and_app_only_are_actionable_errors(self):
        archive = self.base/'package.zip'
        archive.write_bytes(b'fixture')
        for path in [archive, self.base/'missing']:
            with self.assertRaisesRegex(ValueError, 'fully extracted'):
                inspect_package(path)
        app = self.base/'AppTranslocation/random/d/OpenWrt-Installer.app'
        (app/'Contents/MacOS').mkdir(parents=True)
        self.package('Downloads/unrelated', 'mu300')
        with self.assertRaisesRegex(ValueError, 'No extracted'):
            inspect_package(app)

    def test_release_folder_can_itself_be_named_files(self):
        root = self.package('files', 'e5')
        self.assertEqual(locate_package(root), root.resolve())

    @unittest.skipIf(sys.platform == 'win32', 'POSIX symlink fixture')
    def test_macos_directory_alias(self):
        root = self.package('下载 安装包', 'mu300')
        alias = self.base/'alias'
        alias.symlink_to(root, target_is_directory=True)
        self.assertEqual(inspect_package(alias), inspect_package(root))


if __name__ == '__main__':
    unittest.main()
