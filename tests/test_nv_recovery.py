"""An NV fallback is a persisted user decision, never an implicit yes."""
import io
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.adapter import Adapter
from backend.service import Service, plan_install
from backend.state import Context, Store, NVConfirmationRequired
from test_state import PACKAGE, DEVICE


class NVRecovery(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)
        self.record=self.store.create(PACKAGE,DEVICE.copy(),{'method':'part','size_gib':4},
            plan_install(PACKAGE,DEVICE,{'method':'part','size_gib':4}))
        self.ctx=Context(self.store,self.record,lambda *a:None)
        self.adapter=Adapter.__new__(Adapter);self.adapter.ctx=self.ctx

    def test_prompt_and_progress_survive_restart_without_manual_export(self):
        with self.assertRaises(NVConfirmationRequired):self.adapter.confirm_nv_recovery('checksum failed')
        record=self.store.load(self.record['id'])
        self.assertEqual(record['status'],'waiting_user')
        self.assertEqual(record['stage'],'nv_recovery')
        self.assertEqual(record['nv_recovery']['state'],'awaiting')
        self.assertFalse(record['unsafe'])
        fresh=Adapter.__new__(Adapter);fresh.ctx=Context(self.store,record,lambda *a:None)
        record['nv_recovery']['state']='confirmed'
        self.assertTrue(fresh.confirm_nv_recovery('retry'))

    def test_retry_without_yes_or_matching_nonce_cannot_accept(self):
        with self.assertRaises(NVConfirmationRequired):self.adapter.confirm_nv_recovery('checksum failed')
        service=Service('.',self.store,io.StringIO())
        nonce=self.record['nv_recovery']['nonce']
        for answer,token in [('',nonce),('YES',nonce),('yes','wrong')]:
            with self.assertRaisesRegex(ValueError,'Confirm original-NV'):
                service.dispatch({'protocol':1,'command':'retry','params':{'transaction':self.record['id'],
                    'nv_confirmation':answer,'nv_nonce':token}})
            self.assertEqual(self.store.load(self.record['id'])['nv_recovery']['state'],'awaiting')

    def test_verified_consent_is_bound_to_persisted_transaction(self):
        with self.assertRaises(NVConfirmationRequired):self.adapter.confirm_nv_recovery('checksum failed')
        service=Service('.',self.store,io.StringIO())
        adapter=Mock();adapter.password=''
        with patch('backend.service.inspect_package',return_value=PACKAGE),patch.object(service,'adapter',return_value=adapter),patch('backend.service.threading.Thread'):
            service.dispatch({'protocol':1,'command':'retry','params':{'transaction':self.record['id'],
                'nv_confirmation':'yes','nv_nonce':self.record['nv_recovery']['nonce']}})
        saved=self.store.load(self.record['id'])
        self.assertEqual(saved['nv_recovery']['state'],'confirmed')
        self.assertIn('confirmed_at',saved['nv_recovery'])


if __name__=='__main__':unittest.main()
