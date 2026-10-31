"""Local DLL failures must never follow a first device write."""
import builtins
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.service import Service, plan_install
from backend.ssh_runtime import HostRuntimeError, load_ssh
from backend.state import Context, Store
from test_state import PACKAGE, DEVICE


class SSHRuntimeTests(unittest.TestCase):
    def test_missing_extension_has_actionable_local_error_and_original_cause(self):
        original = builtins.__import__
        def imports(name, *args, **kwargs):
            if name == 'paramiko':
                raise ImportError('DLL load failed: filename too long')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=imports):
            with self.assertRaises(HostRuntimeError) as caught:
                load_ssh()
        self.assertIsInstance(caught.exception.__cause__, ImportError)
        self.assertIn('控制端', str(caught.exception))
        self.assertIn('filename too long', str(caught.exception))

    def test_native_cipher_load_is_checked_without_any_connection(self):
        with patch('paramiko.SSHClient') as client:
            self.assertEqual(load_ssh().__name__, 'paramiko')
            client.assert_not_called()
        with patch('cryptography.hazmat.primitives.ciphers.Cipher', side_effect=OSError('native library unavailable')):
            with self.assertRaises(HostRuntimeError):
                load_ssh()

    def test_install_boot_and_update_fail_before_effects_in_both_families(self):
        for family in ('e5', 'mu300'):
            for method in ('tf', 'part', 'reinstall', 'image', 'boot', 'update'):
                with self.subTest(family=family, method=method), tempfile.TemporaryDirectory() as temp:
                    store = Store(temp)
                    # Use a valid base plan; avoid unrelated geometry requirements
                    # for TF/reinstall. No transport or planner may run here.
                    plan = plan_install(PACKAGE, DEVICE, {'method': 'part', 'size_gib': 4})
                    plan.update(method=method, shrink_strategy='online-f2fs')
                    record = store.create({**PACKAGE, 'adapter': family}, DEVICE.copy(), {'method': method}, plan)
                    service = Service('.', store, io.StringIO())
                    adapter = SimpleNamespace(password='', probe=Mock(), execute=Mock())
                    with patch('backend.service.load_ssh', side_effect=HostRuntimeError('Local dependency unavailable')), \
                            patch('backend.service.OnlineShrink') as shrink:
                        service.run(Context(store, record, service.emit), adapter)
                    adapter.probe.assert_not_called()
                    adapter.execute.assert_not_called()
                    shrink.assert_not_called()
                    self.assertEqual(record['effects'], {})
                    self.assertEqual(record['error']['code'], 'host_runtime_unavailable')
                    self.assertEqual(record['status'], 'failed')

    def test_retry_after_verified_misc_only_verifies_without_replaying_writes(self):
        for family in ('e5', 'mu300'):
            with self.subTest(family=family), tempfile.TemporaryDirectory() as temp:
                store = Store(temp)
                plan = plan_install(PACKAGE, DEVICE, {'method': 'boot'})
                record = store.create({**PACKAGE, 'adapter': family}, DEVICE.copy(), {'method': 'boot'}, plan)
                record.update(stage='openwrt_boot', effects={'misc-trial': {'state': 'done'}})
                service = Service('.', store, io.StringIO())
                adapter = SimpleNamespace(password='', probe=Mock(), execute=Mock())
                with patch('backend.service.load_ssh', side_effect=HostRuntimeError('DLL unavailable')):
                    service.run(Context(store, record, service.emit), adapter)
                restored = store.load(record['id'])
                with patch.object(service, 'wait_openwrt') as verify:
                    service.run(Context(store, restored, service.emit), adapter)
                verify.assert_called_once()
                adapter.probe.assert_not_called()
                adapter.execute.assert_not_called()
                self.assertEqual(restored['effects'], {'misc-trial': {'state': 'done'}})

    def test_android_uninstall_does_not_require_ssh(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            record = store.create(PACKAGE, DEVICE.copy(), {'method': 'uninstall'}, {'method': 'uninstall'})
            service = Service('.', store, io.StringIO())
            adapter = SimpleNamespace(password='')
            with patch('backend.service.load_ssh', side_effect=AssertionError('Unneeded SSH dependency')), \
                    patch('backend.service.Uninstaller') as uninstall:
                service.run(Context(store, record, service.emit), adapter)
            uninstall.return_value.run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
