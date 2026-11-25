"""USB reconnection and task boundaries using simulated ADB, never hardware."""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import usb_config as usb
from backend.adapter import Adapter
from backend.state import RecoveryRequired


class Clock:
    now = 0
    def monotonic(self): return self.now
    def sleep(self, seconds): self.now += seconds


class FakeAdb:
    serial = 'SELECTED'
    su = 'su'
    driver = None
    original = 'rndis,mtp,adb'
    config = 'rndis,adb'
    state = 'rndis,adb'
    boot = 'same-boot'
    root = '0'
    write_error = None

    def __init__(self):
        self.writes = []
        self.calls = []
        self.listings = []
        self.readbacks = []

    def devices(self):
        return self.listings.pop(0) if self.listings else [{'serial': self.serial, 'state': 'device'}]

    def shell(self, script, **kwargs):
        self.calls.append((script, kwargs))
        if script == 'getprop sys.usb.config': return self.original
        if script.startswith('setprop '):
            self.writes.append(script)
            if self.write_error: raise self.write_error
            return ''
        if script == 'id -u': return self.root
        if script.startswith('printf '):
            if self.readbacks:
                value = self.readbacks.pop(0)
                if isinstance(value, Exception): raise value
                return value
            return f'CONFIG={self.config}\nSTATE={self.state}\nBOOT={self.boot}\n'
        raise AssertionError(script)


class UsbConfigTest(unittest.TestCase):
    def run_usb(self, adb, **kwargs):
        clock = Clock()
        with patch.object(usb.time, 'monotonic', clock.monotonic), patch.object(usb.time, 'sleep', clock.sleep):
            return usb.prepare(adb, 'same-boot', timeout=3, **kwargs)

    def test_remove_only_mtp_and_keep_all_other_functions(self):
        for original, target in (
            ('rndis,mtp,adb', 'rndis,adb'), ('mtp,ecm,adb', 'ecm,adb'),
            ('ncm,mtp,adb', 'ncm,adb'), ('diag,serial_cdev,mtp,adb', 'diag,serial_cdev,adb'),
            ('mtp,adb', 'adb'), ('mtp', 'adb'), ('ecm,mtp', 'ecm,adb'),
            (' mtp, ecm, adb\n', 'ecm,adb')):
            with self.subTest(original=original):
                self.assertEqual(usb.without_mtp(original), target)

    def test_no_mtp_does_not_write_or_wait(self):
        for original in ('rndis,adb', 'ecm,adb', 'ncm,diag,adb', 'adb'):
            adb = FakeAdb(); adb.original = original
            self.assertFalse(self.run_usb(adb)['changed'])
            self.assertEqual([s for s, _ in adb.calls], ['getprop sys.usb.config'])

    def test_wireless_and_unselected_connections_are_never_modified(self):
        for serial in (None, '192.0.2.1:5555', '[::1]:5555', 'adb-device._adb-tls-connect._tcp'):
            adb = FakeAdb(); adb.serial = serial; adb.su = None
            self.assertFalse(self.run_usb(adb)['changed'])
            self.assertEqual(adb.calls, [])

    def test_invalid_properties_never_become_shell_text(self):
        for original in ('', 'mtp,,adb', 'mtp;reboot', 'mtp,$(reboot)', 'none,mtp,adb', 'x'*256):
            adb = FakeAdb(); adb.original = original
            with self.subTest(original=original), self.assertRaises(usb.UsbPreparationError):
                self.run_usb(adb)
            self.assertEqual(adb.writes, [])

    def test_root_and_identity_must_be_known_before_writing(self):
        adb = FakeAdb(); adb.su = None
        with self.assertRaises(usb.UsbPreparationError): self.run_usb(adb)
        self.assertEqual(adb.calls, [])
        adb.su = 'su'
        with self.assertRaises(usb.UsbPreparationError): usb.prepare(adb, '')
        self.assertEqual(adb.writes, [])

    def test_expected_disconnect_is_not_retried_and_requires_stable_readback(self):
        for error in (None, ConnectionError('disconnected'), subprocess.TimeoutExpired('setprop', 10)):
            adb = FakeAdb(); adb.write_error = error; notice = Mock()
            adb.listings = [[], [{'serial': 'OTHER', 'state': 'device'}],
                            [{'serial': 'SELECTED', 'state': 'offline'}],
                            [{'serial': 'SELECTED', 'state': 'unauthorized'}]]
            self.assertTrue(self.run_usb(adb, notify=notice)['changed'])
            self.assertEqual(adb.writes, ['setprop sys.usb.config rndis,adb'])
            self.assertEqual([c.args[0]['code'] for c in notice.call_args_list], ['usb_preparing', 'usb_ready'])
            self.assertEqual(sum(s.startswith('printf ') for s, _ in adb.calls), 2)

    def test_temporary_empty_properties_and_connection_errors_recover(self):
        adb = FakeAdb()
        adb.readbacks = [ConnectionError('not ready'), 'CONFIG=\nSTATE=\nBOOT=same-boot\n']
        self.assertTrue(self.run_usb(adb)['changed'])
        self.assertEqual(len(adb.writes), 1)

    def test_vendor_reordered_functions_and_missing_state_property(self):
        adb = FakeAdb(); adb.config = 'adb,rndis'; adb.state = ''
        self.assertTrue(self.run_usb(adb)['changed'])

    def test_another_ready_device_cannot_satisfy_reconnection(self):
        adb = FakeAdb()
        adb.listings = [[{'serial': 'OTHER', 'state': 'device'}] for _ in range(10)]
        with self.assertRaisesRegex(usb.UsbPreparationError, 'timed out'):
            self.run_usb(adb)
        self.assertFalse(any(s.startswith('printf ') for s, _ in adb.calls))

    def test_cancel_before_setprop_changes_nothing(self):
        adb = FakeAdb()
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.run_usb(adb, boundary=Mock(side_effect=[None, RuntimeError('cancelled')]))
        self.assertEqual(adb.writes, [])

    def test_mtp_or_stale_usb_properties_never_block_working_adb(self):
        for change in ({'config': 'rndis,mtp,adb', 'state': 'rndis,mtp,adb'},
                       {'config': 'mtp,adb'}, {'state': 'mtp,adb'}, {'config': 'rndis'},
                       {'config': '', 'state': ''}, {'state': 'none'}):
            adb = FakeAdb(); notice = Mock()
            for key, value in change.items(): setattr(adb, key, value)
            with self.subTest(change=change):
                result = self.run_usb(adb, notify=notice)
                self.assertEqual(result['config'], adb.config)
                self.assertEqual(result['requested_config'], 'rndis,adb')
            self.assertEqual(len(adb.writes), 1)
            self.assertEqual(notice.call_args.args[0]['code'], 'usb_ready')
            self.assertEqual(sum(s.startswith('printf ') for s, _ in adb.calls), 2)

    def test_ecm_ncm_and_vendor_functions_are_preserved_in_the_actual_write(self):
        for network in ('ecm', 'ncm', 'rndis', 'vendor_network'):
            adb = FakeAdb()
            adb.original = network + ',diag,mtp,adb'
            adb.config = adb.state = adb.original  # Vendor ignores setprop; ADB works.
            self.run_usb(adb)
            self.assertEqual(adb.writes, ['setprop sys.usb.config ' + network + ',diag,adb'])

    def test_missing_boot_identity_still_stops_before_continuing(self):
        adb = FakeAdb(); adb.boot = ''
        with self.assertRaisesRegex(usb.UsbPreparationError, 'timed out'):
            self.run_usb(adb)

    def test_changed_boot_or_lost_root_is_not_accepted(self):
        for change in ({'boot': 'different-boot'}, {'root': '2000'}):
            adb = FakeAdb()
            for key, value in change.items(): setattr(adb, key, value)
            with self.subTest(change=change), self.assertRaises(usb.UsbPreparationError): self.run_usb(adb)
            self.assertEqual(len(adb.writes), 1)

    def test_mobile_connection_is_reset_after_the_single_write(self):
        adb = FakeAdb(); adb.driver = Mock()
        self.assertTrue(self.run_usb(adb)['changed'])
        adb.driver.reset_connection.assert_called_once_with()

    def test_cancellation_during_wait_never_reissues_setprop(self):
        adb = FakeAdb(); notice = Mock()
        boundary = Mock(side_effect=[None, None, RuntimeError('cancelled')])
        with self.assertRaisesRegex(RuntimeError, 'cancelled'): self.run_usb(adb, boundary=boundary, notify=notice)
        self.assertEqual(len(adb.writes), 1)
        self.assertEqual(notice.call_args.args[0]['code'], 'usb_prepare_failed')

    def adapter(self):
        a = Adapter.__new__(Adapter)
        a.usb_prepared = set(); a.adb = FakeAdb(); a.notice = Mock()
        a.ctx = Mock(); a.ctx.record = {'device': {'cid': 'storage', 'profile': 'test', 'slot': 'b'}, 'stage': 'backup'}
        return a, dict(a.ctx.record['device'], serial='SELECTED', boot_id='same-boot')

    def test_active_task_prepares_once_per_boot(self):
        a, device = self.adapter()
        with patch.object(usb, 'prepare') as prepare:
            a.prepare_transport(device); a.prepare_transport(device)
            self.assertEqual(prepare.call_count, 1)
            a.prepare_transport(dict(device, boot_id='next-boot'))
            self.assertEqual(prepare.call_count, 2)
            prepare.assert_called_with(a.adb, 'next-boot', notify=a.notice, boundary=a.ctx.boundary)

    def test_identity_change_or_maintenance_writer_never_changes_usb(self):
        a, device = self.adapter()
        with patch.object(usb, 'prepare') as prepare:
            with self.assertRaises(RecoveryRequired): a.prepare_transport(dict(device, cid='other-storage'))
            for stage in ('openwrt_boot', 'verify_capacity', 'uninstall_offline', 'expand_offline', 'verify_android'):
                a.ctx.record['stage'] = stage; a.prepare_transport(device)
            a.ctx.record['stage'] = 'backup'
            for state in ('started', 'gpt-committed'):
                a.ctx.record['online_shrink'] = {'state': state}; a.prepare_transport(device)
            a.ctx.record.pop('online_shrink'); a.ctx.record['unsafe'] = True
            a.prepare_transport(device)
            prepare.assert_not_called()


if __name__ == '__main__':
    unittest.main()
