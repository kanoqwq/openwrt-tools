"""Portable baselines and real patch exports using disposable releases only."""
import copy
import hashlib
import json
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.ota_package import Release, analyze
from backend.ota_desktop import Desktop
from backend.ota_engine import canonical, Patch, Engine
from backend.release_index import FORMAT, read_index, load_index, save_index, snapshot, write_index
from backend.service import Service
from backend.state import Store
from test_ota import mu_release, file


class ReleaseIndexTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.cache=self.root/'app/release-indexes'

    def releases(self):
        for version in range(1,4):
            mu_release(self.root/str(version),'mu300-r'+str(version),
                       {'usr/lib/test':str(version).encode(),
                        'lib/apk/db/installed':b'P:base\nV:1.0\nA:aarch64\nD:libc\n\n'},boot=('boot'+str(version)).encode())
        return [self.root/str(v) for v in range(1,4)]

    def test_old_package_can_be_deleted_and_result_matches_full_release(self):
        a,b,c=self.releases()
        result=analyze(a,b,index_directory=self.cache)
        index=Path(result['index_path'])
        expected=analyze(b,c,self.root/'full.ota')
        shutil.rmtree(a);shutil.rmtree(b)
        actual=analyze(index,c,self.root/'indexed.ota',index_directory=self.cache)
        self.assertEqual(actual['manifest'],expected['manifest'])
        self.assertEqual(actual['payload_bytes'],expected['payload_bytes'])
        with zipfile.ZipFile(self.root/'full.ota') as x,zipfile.ZipFile(self.root/'indexed.ota') as y:
            self.assertEqual({n:x.read(n) for n in x.namelist()},{n:y.read(n) for n in y.namelist()})
        with Patch(self.root/'indexed.ota'):pass
        self.assertTrue(Path(actual['index_path']).is_file())
        self.assertEqual(len(list(self.cache.glob('*.json'))),2)
        self.assertEqual(load_index(index).packages['base']['V'],'1.0')

    def test_cache_is_deduplicated_and_indexes_can_move(self):
        a,b,c=self.releases()
        first=analyze(a,b,index_directory=self.cache)['index_path']
        again=analyze(a,b,index_directory=self.cache)['index_path']
        self.assertEqual(first,again)
        self.assertEqual(len(list(self.cache.iterdir())),1)
        moved=self.root/'移动 目录/renamed.json'
        moved.parent.mkdir();shutil.move(first,moved)
        self.assertTrue(analyze(moved,c)['can_export'])

    def test_index_contains_effective_updater_overlay_and_no_file_contents(self):
        a,b,_=self.releases()
        desc=file(b'new generated engine')
        def runtime(release):
            return {'usr/bin/python3':file(b'python'),'ota/engine.py':desc,
                    **{'ota/'+name:file(name.encode()) for name in ('boot.py','rpc.py','proxy.sh','unisoc-ota.js','errors.js','unisoc-ota.json')}},file(b'busybox'),{}
        # No device/bootstrap execution: retain the real compare() overlay, while
        # skipping runtime manifest checks irrelevant to this synthetic fixture.
        with patch('backend.ota_runtime.build',side_effect=runtime),patch('backend.ota_runtime.bootstrap',return_value=b'bootstrap'),patch('backend.ota_package.validate_manifest'):
            result=analyze(a,b,index_directory=self.cache)
        saved=load_index(result['index_path'])
        self.assertEqual(saved.entries['usr/lib/unisoc-ota/engine.py'],desc)
        self.assertNotIn(b'new generated engine',Path(result['index_path']).read_bytes())
        self.assertEqual(saved.boot_digest,hashlib.sha256(b'boot2').hexdigest())

    def test_descriptors_roundtrip_permissions_links_xattrs_and_e5_identity(self):
        release=SimpleNamespace(family='e5',meta={'profile':'k1','image_version':'k1-r1','kernel_release':'6.18.54-k1','openwrt':'25.12.5'},
            entries={'usr/lib/file':dict(file(),mode=0o4755,xattrs={'user.test':'eA=='}),
                     'usr/lib/link':dict(kind='symlink',mode=0o777,uid=0,gid=0,target='file',xattrs={}),
                     'usr/lib':dict(kind='dir',mode=0o755,uid=0,gid=0,xattrs={})},packages={},boot_digest='a'*64)
        index=save_index(release,self.cache)
        loaded=load_index(index)
        self.assertEqual(loaded.entries,release.entries)
        self.assertEqual(loaded.family,'e5')

    def test_corrupt_unknown_duplicate_and_unsafe_indexes_are_rejected(self):
        a,b,_=self.releases();index=analyze(a,b,index_directory=self.cache)['index_path']
        original=read_index(index)
        for field,value in [('format','unknown'),('version',2),('sha256','0'*64)]:
            bad=copy.deepcopy(original);bad[field]=value
            path=self.root/'bad.json';path.write_bytes(canonical(bad))
            with self.subTest(field=field),self.assertRaises(ValueError):load_index(path)
        for name,desc in [('../etc/shadow',file()),('usr/lib/a',dict(file(),mode=999999)),('usr/lib/b',dict(file(),sha256='bad'))]:
            bad=copy.deepcopy(original);bad['release']['entries'][name]=desc
            bad['sha256']=hashlib.sha256(canonical(bad['release'])).hexdigest()
            path.write_bytes(canonical(bad))
            with self.subTest(name=name),self.assertRaises(ValueError):load_index(path)
        path.write_bytes(b'{"format":"x","format":"y"}')
        with self.assertRaisesRegex(ValueError,'Duplicate'):load_index(path)
        path.write_bytes(b'{"truncated"')
        with self.assertRaises(ValueError):load_index(path)

    def test_failed_analysis_does_not_create_an_index_or_retain_export(self):
        a,b,c=self.releases();desktop=Desktop(self.root,lambda *_:None,self.root/'app')
        result=desktop.dispatch('ota_analyze',{'old':str(a),'new':str(b)})
        export=self.root/'export.json'
        self.assertEqual(desktop.dispatch('ota_export_index',{'output':str(export)})['path'],str(export))
        self.assertEqual(read_index(export),read_index(result['index_path']))
        with self.assertRaisesRegex(ValueError,'already exists'):desktop.dispatch('ota_export_index',{'output':str(export)})
        meta=c/'desktop/release.json';data=json.loads(meta.read_text());data['profile']='other';meta.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'Incompatible'):desktop.dispatch('ota_analyze',{'old':str(b),'new':str(c)})
        with self.assertRaisesRegex(ValueError,'Analyze'):desktop.dispatch('ota_export_index',{'output':str(self.root/'wrong.json')})
        self.assertEqual(len(list(self.cache.glob('*.json'))),1)

    def test_atomic_failure_leaves_no_partial_cache(self):
        a,b,_=self.releases()
        with Release(b) as release:data=snapshot(release)
        with patch('backend.release_index.os.replace',side_effect=OSError('simulated disk error')):
            with self.assertRaises(OSError):write_index(self.cache/'new.json',data)
        self.assertEqual(list(self.cache.iterdir()),[])

    def test_service_reopens_with_saved_index_under_its_own_app_data(self):
        a,b,c=self.releases()
        store=Store(self.root/'service-state')
        service=Service(self.root,store,io.StringIO())
        result=service.dispatch({'protocol':1,'command':'ota_analyze','params':{'old':str(a),'new':str(b)}})
        self.assertEqual(Path(result['index_path']).parent,store.base/'release-indexes')
        shutil.rmtree(a);shutil.rmtree(b)
        reopened=Service(self.root,store,io.StringIO())
        result=reopened.dispatch({'protocol':1,'command':'ota_analyze','params':{'old':result['index_path'],'new':str(c)}})
        self.assertEqual(read_index(result['index_path'])['release']['meta']['image_version'],'mu300-r3')

    def test_portable_cli_saves_index_and_generates_without_old_package(self):
        a,b,c=self.releases()
        tool=Path(__file__).resolve().parents[1]/'ota-tool.py'
        env=dict(os.environ,LOCALAPPDATA=str(self.root/'cache'),HOME=str(self.root/'home'))
        result=subprocess.run([sys.executable,str(tool),'analyze',str(a),str(b)],env=env,capture_output=True,text=True,encoding='utf-8',check=True)
        index=Path(json.loads(result.stdout)['index_path'])
        self.assertTrue(index.is_relative_to(self.root))
        shutil.rmtree(a);shutil.rmtree(b)
        output=self.root/'cli.ota'
        subprocess.run([sys.executable,str(tool),'generate',str(index),str(c),str(output)],env=env,capture_output=True,check=True)
        with Patch(output) as patch:self.assertEqual(patch.manifest['target_version'],'mu300-r3')

    def test_index_cannot_replace_the_new_payload_and_blocked_analysis_saves_nothing(self):
        a,b,_=self.releases()
        result=analyze(a,b,index_directory=self.cache)
        with self.assertRaisesRegex(ValueError,'new release must be a full package'):
            analyze(a,result['index_path'])
        empty=self.root/'blocked-cache'
        result=analyze(b,b,index_directory=empty)
        self.assertFalse(result['can_export'])
        self.assertFalse(empty.exists())

    @unittest.skipUnless(sys.platform=='linux','Real engine permissions/locking require Linux; temporary files only')
    def test_two_real_patch_transactions_with_injected_runtime_use_saved_target(self):
        device=self.root/'device';device.mkdir()
        for version in range(1,4):
            meta=dict(profile='mu300',image_version='mu300-r'+str(version),kernel_release='7.2.8',openwrt='25.12.5')
            files={'usr/lib/test':str(version).encode(),'usr/bin/python3':b'interpreter fixture',
                   'etc/mu300/release.json':canonical(meta)}
            release=self.root/str(version)
            mu_release(release,meta['image_version'],files,boot=b'same boot')
            (release/'payload/busybox').write_bytes(b'busybox fixture, never executed')
            if version==1:
                for directory in ('usr','usr/lib','usr/bin','etc','etc/config','etc/mu300',
                                  'usr/share/rpcd/acl.d','www/luci-static/resources/view/system',
                                  'www/luci-static/resources/unisoc-ota'):
                    (device/directory).mkdir(parents=True,exist_ok=True)
                for name,data in files.items():(device/name).write_bytes(data)
        first=analyze(self.root/'1',self.root/'2',self.root/'one.ota',index_directory=self.cache)
        engine=Engine(device)
        with Patch(self.root/'one.ota') as p:self.assertEqual(engine.apply(p)['status'],'complete')
        shutil.rmtree(self.root/'2')
        second=analyze(first['index_path'],self.root/'3',self.root/'two.ota',index_directory=self.cache)
        self.assertNotIn('usr/lib/unisoc-ota/engine.py',[o['path'] for o in second['manifest']['operations']])
        with Patch(self.root/'two.ota') as p:self.assertEqual(engine.apply(p)['status'],'complete')
        self.assertEqual(engine.identity()['version'],'mu300-r3')
        self.assertEqual((device/'usr/lib/test').read_bytes(),b'3')


if __name__=='__main__':unittest.main()
