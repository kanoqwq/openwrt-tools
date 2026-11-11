"""Identify running code separately from a saved failed operation and stale APKs."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'mobile/python'))
from android_host.runtime import Provider
from backend import host
from backend.online_shrink import PREPARE_TIMEOUT
from backend.service import Service,plan_install
from backend.state import Store
from mobile.build_info import build_info
from mobile.apk import validate
from test_state import PACKAGE,DEVICE


class BuildIdentity(unittest.TestCase):
    def test_source_hash_changes_even_when_the_version_name_is_unchanged(self):
        a=build_info('a'*40,{'backend/online_shrink.py':'old'})
        b=build_info('a'*40,{'backend/online_shrink.py':'new'})
        self.assertNotEqual(a['source_sha256'],b['source_sha256'])
        self.assertEqual(a,build_info('a'*40,{'backend/online_shrink.py':'old'}))
        with self.assertRaises(ValueError):build_info('not-a-revision',{})

    def test_android_diagnostics_use_actual_timeout_and_apk_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);identity=build_info('a'*40,{'backend':'hash'})
            (root/'mobile-build.json').write_text(json.dumps(identity))
            provider=Provider(root,root,Mock(),Mock(),environment=lambda:dict(version_name='1.0.0',version_code=1000000,
                android_sdk=37,target_sdk=37,local_network_permission_granted=False))
            info=provider.runtime_info()
            self.assertEqual(info['revision'],identity['revision'])
            self.assertEqual(info['source_sha256'],identity['source_sha256'])
            self.assertEqual(info['online_shrink_prepare_timeout_s'],PREPARE_TIMEOUT)
            self.assertEqual(PREPARE_TIMEOUT,888)
            self.assertFalse(info['local_network_permission_granted'])
            (root/'mobile-build.json').unlink()
            self.assertNotIn('revision',provider.runtime_info())

    def test_current_app_does_not_relabel_a_saved_old_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp);r=store.create(PACKAGE,DEVICE.copy(),{'method':'part','size_gib':4},
                plan_install(PACKAGE,DEVICE,{'method':'part','size_gib':4}))
            r['installer_environment']={'platform':'android','revision':'a'*40,'online_shrink_prepare_timeout_s':60}
            store.save(r);provider=Mock();provider.runtime_info.return_value={'platform':'android','revision':'b'*40,'online_shrink_prepare_timeout_s':888}
            service=Service(tmp,store)
            with patch.object(host,'_provider',provider):
                text=service.dispatch({'protocol':1,'command':'diagnostics','params':{'transaction':r['id']}})['text']
            data=json.loads(text.split('\n',1)[1].split('\n\n',1)[0])
            self.assertEqual(data['operation_installer']['online_shrink_prepare_timeout_s'],60)
            self.assertEqual(data['current_installer']['online_shrink_prepare_timeout_s'],888)

    def test_apk_without_current_embedded_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'app.apk';identity=build_info('a'*40,{})
            for content in (None,dict(identity,revision='b'*40)):
                with zipfile.ZipFile(p,'w') as archive:
                    if content is not None:archive.writestr('assets/openwrt-runtime/mobile-build.json',json.dumps(content))
                with self.assertRaisesRegex(ValueError,'build identity'):
                    validate(p,'aarch64',expected_build=identity)


if __name__=='__main__':unittest.main()
