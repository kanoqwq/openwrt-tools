"""Execute the repair shell in private fixtures; macOS tools are protocol doubles."""
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.macos_launch import APPS, attach, content


@unittest.skipUnless(sys.platform == 'linux', 'POSIX permissions and private user xattrs')
class MacLauncher(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mac-launcher-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = self.directory / "解压目录 with 'quotes' $(ignored)"
        self.root.mkdir()
        self.tools = self.root / 'tools/macos'
        self.tools.mkdir(parents=True)
        self.log = self.directory / 'calls.jsonl'
        self.env = dict(os.environ, MAC_TEST_LOG=str(self.log))
        self.stub = self.directory / 'mac-tools.py'
        self.stub.write_text('''#!''' + sys.executable + '''
import json,os,plistlib,sys
from pathlib import Path
tool,*args=sys.argv[1:]
with open(os.environ['MAC_TEST_LOG'],'a') as log:
    log.write(json.dumps([tool,*args])+ '\\n')
if tool=='uname':
    print(os.environ.get('MAC_TEST_PLATFORM','Darwin'))
elif tool=='plist':
    print(plistlib.loads(Path(args[-1]).read_bytes())['CFBundleExecutable'])
elif tool=='xattr':
    action,attribute,name=args
    assert attribute=='com.apple.quarantine'
    if action=='-p':
        try:sys.stdout.buffer.write(os.getxattr(name,'user.quarantine'))
        except OSError:sys.exit(1)
    else:
        assert action=='-d'
        if os.environ.get('MAC_TEST_DELETE_FAIL'):sys.exit(1)
        os.removexattr(name,'user.quarantine')
elif tool=='open':
    assert not os.getxattr(args[0],'user.keep')==b''
else:raise AssertionError(tool)
''')
        self.stub.chmod(0o755)

    def package(self, app, adb=True):
        bundle = self.root / app
        entry = bundle / 'Contents/MacOS/openwrt-installer'
        entry.parent.mkdir(parents=True)
        entry.write_bytes(b'app signed bytes')
        (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': entry.name}))
        backend = self.tools / 'backend/openwrt-backend'
        backend.parent.mkdir(parents=True, exist_ok=True)
        backend.write_bytes(b'backend signed bytes')
        library = backend.parent / '_internal/libpython.dylib'
        library.parent.mkdir(exist_ok=True)
        library.write_bytes(b'library signed bytes')
        if adb:
            (self.tools / 'adb').write_bytes(b'adb signed bytes')
        for path in (bundle, self.tools, *bundle.rglob('*'), *self.tools.rglob('*')):
            os.setxattr(path, 'user.quarantine', b'quarantine')
            os.setxattr(path, 'user.keep', b'preserved')
            if path.is_file():
                path.chmod(0o644)
        return bundle

    def run_launcher(self, app):
        name = attach(self.root, app)
        script = (self.root / name).read_text()
        for original, tool in (('/usr/bin/uname', 'uname'), ('/usr/bin/xattr', 'xattr'),
                               ('/usr/libexec/PlistBuddy', 'plist'), ('/usr/bin/open', 'open')):
            script = script.replace(original, str(self.stub) + ' ' + tool)
        (self.root / name).write_text(script)
        return subprocess.run(['sh', str(self.root / name)], cwd='/', env=self.env,
                              capture_output=True, text=True, timeout=15)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_all_entries_fix_nested_quarantine_and_permissions_idempotently(self):
        for app in APPS:
            with self.subTest(app=app):
                bundle = self.package(app, adb=app == 'OpenWrt-Installer.app')
                before = {path: path.read_bytes() for path in (*bundle.rglob('*'), *self.tools.rglob('*')) if path.is_file()}
                outside = self.root / 'user-data'
                outside.write_bytes(b'leave me alone')
                os.setxattr(outside, 'user.quarantine', b'preserve this quarantine')
                for _ in range(2):
                    result = self.run_launcher(app)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for path, raw in before.items():
                    self.assertEqual(path.read_bytes(), raw)
                    self.assertNotIn('user.quarantine', os.listxattr(path))
                    self.assertEqual(os.getxattr(path, 'user.keep'), b'preserved')
                self.assertEqual(os.getxattr(outside, 'user.quarantine'), b'preserve this quarantine')
                for path in (bundle / 'Contents/MacOS/openwrt-installer', self.tools / 'backend/openwrt-backend'):
                    self.assertTrue(stat.S_IMODE(path.stat().st_mode) & stat.S_IXUSR)
                self.assertEqual([args[1] for args in self.calls() if args[0] == 'open'][-2:], [str(bundle)] * 2)

    def test_missing_runtime_never_starts_permission_repair(self):
        self.package('OpenWrt-Installer.app')
        (self.tools / 'backend/openwrt-backend').unlink()
        result = self.run_launcher('OpenWrt-Installer.app')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(args[0] in ('xattr', 'open') for args in self.calls()))

    def test_redirected_tree_is_rejected_without_modifying_external_files(self):
        self.package('OpenWrt-Installer.app')
        outside = self.directory / 'outside'
        outside.write_bytes(b'outside')
        os.setxattr(outside, 'user.quarantine', b'untouched')
        (self.tools / 'backend/redirected').symlink_to(outside)
        result = self.run_launcher('OpenWrt-Installer.app')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(os.getxattr(outside, 'user.quarantine'), b'untouched')
        self.assertFalse(any(args[0] in ('xattr', 'open') for args in self.calls()))

    def test_failed_attribute_removal_does_not_claim_success_or_open_app(self):
        self.package('OpenWrt-Installer.app')
        self.env['MAC_TEST_DELETE_FAIL'] = '1'
        result = self.run_launcher('OpenWrt-Installer.app')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(args[0] == 'open' for args in self.calls()))

    def test_platform_and_bundle_entry_must_be_valid(self):
        bundle = self.package('OpenWrt-Installer.app')
        self.env['MAC_TEST_PLATFORM'] = 'Linux'
        self.assertNotEqual(self.run_launcher('OpenWrt-Installer.app').returncode, 0)
        self.env.pop('MAC_TEST_PLATFORM')
        (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': '../../../outside'}))
        self.assertNotEqual(self.run_launcher('OpenWrt-Installer.app').returncode, 0)
        self.assertFalse(any(args[0] in ('xattr', 'open') for args in self.calls()))

    def test_only_known_entry_names_are_generated_and_no_global_security_changes(self):
        with self.assertRaises(ValueError):
            content('../../outside.app')
        text = content('OpenWrt-Installer.app').decode()
        for command in ('sudo', 'spctl', 'codesign', 'xattr -c'):
            self.assertNotIn(command, text)
        self.assertIn('com.apple.quarantine', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
