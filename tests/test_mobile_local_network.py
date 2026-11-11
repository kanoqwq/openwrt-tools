"""Permission admission runs before network work, without gating USB/local tools."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'mobile/python'))
from android_host.runtime import Runtime,Provider
from android_host.adb import Registry


class LocalNetwork(unittest.TestCase):
    def runtime(self,serial='USB-SERIAL',method='part'):
        r=Runtime.__new__(Runtime)
        r.provider=SimpleNamespace(registry=SimpleNamespace(tcp={}))
        r.service=SimpleNamespace(store=Mock())
        r.service.store.load.return_value={'choices':{'method':method},'device':{'serial':serial}}
        return r

    def test_wireless_adb_ssh_and_ota_admission(self):
        r=self.runtime()
        for cmd in ('adb_connect','probe_update','verify_openwrt','ota_probe','ota_apply','ota_status','ota_reboot'):
            with self.subTest(cmd=cmd):self.assertTrue(r.requires_network(cmd,'{}'))
        self.assertTrue(r.requires_network('probe',json.dumps({'serial':'192.168.77.1:5555'})))
        self.assertTrue(r.requires_network('plan',json.dumps({'choices':{'method':'update'}})))

    def test_usb_and_local_analysis_do_not_request_lan_permission(self):
        r=self.runtime()
        for cmd in ('hello','devices','diagnostics','records','package','export','import','ota_analyze','ota_export','mobile_index'):
            with self.subTest(cmd=cmd):self.assertFalse(r.requires_network(cmd,'{}'))
        for cmd in ('probe','plan'):
            self.assertFalse(r.requires_network(cmd,json.dumps({'serial':'USB-SERIAL','choices':{'method':'part'}})))
        for cmd in ('start','resume','retry','expand_reset','online_fallback'):
            self.assertFalse(r.requires_network(cmd,'{"transaction":"test"}'))

    def test_resume_uses_selected_serial_and_saved_transport(self):
        for cmd in ('start','resume','retry'):
            with self.subTest(cmd=cmd):
                self.assertTrue(self.runtime('192.168.77.1:5555').requires_network(cmd,'{"transaction":"test"}'))
                self.assertTrue(self.runtime(method='update').requires_network(cmd,'{"transaction":"test"}'))
                self.assertFalse(self.runtime('192.168.77.1:5555').requires_network(cmd,'{"transaction":"test","serial":"USB-SERIAL"}'))
        r=self.runtime();r.provider.registry.tcp['192.168.77.1:5555']=('192.168.77.1',5555)
        self.assertTrue(r.requires_network('devices','{}'))

    def test_denial_stops_before_dns_socket_creation_or_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            denied=Mock(side_effect=PermissionError('local network denied'))
            provider=Provider(tmp,tmp,Mock(),Mock(),require_network=denied)
            with patch('android_host.network.connect') as connect:
                for endpoint in (('192.168.77.1',5555),('device.local',22)):
                    with self.assertRaises(PermissionError):provider.tcp_connection(endpoint,20)
                connect.assert_not_called()
            self.assertEqual(denied.call_count,2)

    def test_revocation_is_checked_on_every_new_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            permission=Mock(side_effect=[None,PermissionError('revoked')])
            registry=Registry(Mock(),tmp,require_network=permission)
            with patch('android_host.network.connect',return_value='socket') as connect:
                self.assertEqual(registry.tcp_connection(('192.168.77.1',22),20),'socket')
                with self.assertRaises(PermissionError):registry.tcp_connection(('192.168.77.1',22),20)
                self.assertEqual(connect.call_count,1)


if __name__=='__main__':unittest.main()
