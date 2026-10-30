"""Cross-host failure cases found during the installer workflow audit.

No ADB executable, USB interface, SSH connection or block device is used.
"""
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'mobile/python'))
sys.path.insert(0,str(ROOT/'mobile/vendor/adb_shell-0.4.4-py3-none-any.whl'))
from backend.service import Service
from backend.state import Store
from android_host.adb import Registry
from test_state import PACKAGE,DEVICE


class AuditRegressions(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.store=Store(self.base)
        self.service=Service('.',self.store,io.StringIO())
        self.record=self.store.create(PACKAGE,DEVICE,{'method':'boot'},{'method':'boot'})
        self.directory=self.store.directory(self.record['id'])
        (self.directory/'boot.bin').write_bytes(b'original partition fixture')

    def export(self,path):
        return self.service.dispatch({'protocol':1,'command':'export','params':{'transaction':self.record['id'],'path':str(path)}})

    def test_one_damaged_record_cannot_disable_home_or_record_manager(self):
        for damaged in ([],{}, {'version':1,'id':'c'*32}, {**self.record,'id':'c'*32,'updated':'not-a-time'},
                        {**self.record,'id':'c'*32,'package':{}}):
            directory=self.store.directory('c'*32);directory.mkdir(exist_ok=True)
            (directory/'transaction.json').write_text(json.dumps(damaged))
            for command in ('hello','list','records'):
                with self.subTest(record=damaged,command=command):
                    result=self.service.dispatch({'protocol':1,'command':command})
                    rows=result['transactions'] if command=='hello' else result
                    self.assertEqual(len(rows),1)

    def test_export_must_not_archive_its_own_output(self):
        destination=self.directory/'nested'/'backup.zip';destination.parent.mkdir()
        # Catch self-inclusion before zipfile could read its own growing output.
        original=zipfile.ZipFile.write
        def write(archive,path,*args,**kwargs):
            self.assertNotEqual(Path(path).resolve(),destination.resolve(),'export archives itself')
            return original(archive,path,*args,**kwargs)
        with patch.object(zipfile.ZipFile,'write',write):
            with self.assertRaisesRegex(ValueError,'outside|记录目录'):
                self.export(destination)
        self.assertFalse(destination.exists())

    def test_failed_export_does_not_leave_an_unusable_zip_or_replace_existing_file(self):
        destination=self.base/'backup.zip'
        with patch.object(zipfile.ZipFile,'write',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.export(destination)
        self.assertFalse(destination.exists())
        destination.write_bytes(b'existing user file')
        with self.assertRaises(ValueError):self.export(destination)
        self.assertEqual(destination.read_bytes(),b'existing user file')

    def test_export_keeps_both_family_records_and_binary_backups(self):
        for family in ('e5','mu300'):
            self.record['package']={**PACKAGE,'adapter':family};self.store.save(self.record)
            result=self.export(self.base/(family+'.zip'))
            with zipfile.ZipFile(result['path']) as archive:
                self.assertEqual(archive.read('boot.bin'),b'original partition fixture')
                self.assertEqual(json.loads(archive.read('transaction.json'))['package']['adapter'],family)

    def test_an_in_progress_write_does_not_export_a_false_recovery_snapshot(self):
        destination=self.base/'backup.zip';self.record['unsafe']=True;self.store.save(self.record)
        with self.assertRaisesRegex(ValueError,'write|写入'):self.export(destination)
        self.assertFalse(destination.exists())

    def test_export_does_not_publish_a_checkpoint_from_before_new_device_writes(self):
        destination=self.base/'backup.zip';original=zipfile.ZipFile.write
        def write(archive,path,*args,**kwargs):
            self.record['effects']['next-write']={'state':'done'};self.store.save(self.record)
            return original(archive,path,*args,**kwargs)
        with patch.object(zipfile.ZipFile,'write',write):
            with self.assertRaisesRegex(ValueError,'advanced|推进'):self.export(destination)
        self.assertFalse(destination.exists())

    def test_usb_detach_releases_cached_client_without_closing_wireless_connection(self):
        usb=Mock();usb.enumerate.return_value='[]'
        registry=Registry(usb,self.base/'keys')
        gone=Mock();wireless=Mock()
        registry.clients={'usb:gone':gone,'127.0.0.1:5555':wireless}
        registry.tcp={'127.0.0.1:5555':('127.0.0.1',5555)}
        rows=registry.devices()
        gone.close.assert_called_once();wireless.close.assert_not_called()
        self.assertNotIn('usb:gone',registry.clients)
        self.assertEqual([row['serial'] for row in rows],['127.0.0.1:5555'])

    def test_a_new_usb_attachment_reauthenticates_instead_of_reusing_a_dead_session(self):
        usb=Mock();usb.enumerate.return_value='[]';registry=Registry(usb,self.base/'keys')
        old=Mock();registry.clients['usb:again']=old
        registry.devices()
        usb.enumerate.return_value=json.dumps([{'serial':'usb:again','permission':True}])
        with patch.object(registry,'client',return_value=Mock()) as establish:
            self.assertEqual(registry.devices()[0]['state'],'device')
        self.assertNotIn('usb:again',registry.clients)
        establish.assert_called_once_with('usb:again')

    def test_invalid_protocol_message_does_not_kill_the_cli_server(self):
        requests=[[],{'protocol':1,'command':None},{'protocol':1,'command':'hello','id':3}]
        result=subprocess.run([sys.executable,str(ROOT/'backend_entry.py'),'--package-root','.',
                               '--data-dir',str(self.base/'server')],
                              input=''.join(json.dumps(row)+'\n' for row in requests),
                              capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        replies=[json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row['data']['ok'] for row in replies],[False,False,True])
        self.assertEqual(replies[-1]['id'],3)

    def test_a_failed_package_selection_cannot_leave_the_previous_package_armed(self):
        self.service.package=PACKAGE
        with self.assertRaises(ValueError):
            self.service.dispatch({'protocol':1,'command':'package','params':{'path':str(self.base/'missing')}})
        self.assertIsNone(self.service.package)


if __name__=='__main__':unittest.main()
