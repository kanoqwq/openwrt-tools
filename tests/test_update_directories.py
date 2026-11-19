"""Update directory lifecycle regressions. All roots/devices are temporary."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_ota import package, manifest, file, mu_release
from backend.ota_engine import Engine, Patch, atomic_json
from backend.ota_package import analyze
from backend.ota_bundle import export_bundle


@unittest.skipUnless(os.name=='posix' and os.geteuid()==0,'real Linux ownership/rollback')
class Directories(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'live'
        (self.root/'etc/config').mkdir(parents=True)
        self.user=self.root/'etc/config/wireless';self.user.write_bytes(b'USER WIFI')
        self.identity=self.root/'etc/mu300/release.json';self.identity.parent.mkdir()
        self.identity.write_text(json.dumps(dict(profile='mu300',image_version='mu300-r1',kernel_release='7.2.8')))

    def archive(self,path='usr/lib/removed/app',indexed=False,family='mu300',profile='mu300'):
        if family=='e5':
            self.identity.unlink();self.identity=self.root/'etc/e5/release.json';self.identity.parent.mkdir()
        self.identity.write_text(json.dumps(dict(profile=profile,image_version='mu300-r1',kernel_release='7.2.8')))
        m=dict(format='unisoc-diff-ota-2',family=family,profile=profile,base_version='mu300-r1',
               target_version='mu300-r2',kernel_release='7.2.8',
               operations=[dict(path=path,before=file(b'old'),after=file(b'new'))])
        if indexed:
            from backend.release_index import snapshot
            from types import SimpleNamespace
            entries={str(p):dict(kind='dir',mode=0o750,uid=0,gid=0,xattrs={})
                     for p in Path(path).parents if str(p)!='.'}
            entries[path]=file(b'new')
            release=SimpleNamespace(family=family,meta=dict(profile=profile,image_version='mu300-r2',
                       kernel_release='7.2.8',openwrt='25.12.5'),entries=entries,packages={},boot_digest='a'*64)
            m['release_index']=snapshot(release)
        return package(self.base/'patch.ota',m)

    def test_missing_program_and_web_ancestors_on_all_families(self):
        for family,profile in (('mu300','mu300'),('mu300','u30pro'),('mu300','f50pro'),('e5','e5')):
            for path in ('usr/lib/removed/app','www/luci-static/resources/removed/view.js'):
                with self.subTest(profile=profile,path=path),tempfile.TemporaryDirectory() as d:
                    root=Path(d);identity=root/('etc/'+family+'/release.json');identity.parent.mkdir(parents=True)
                    identity.write_text(json.dumps(dict(profile=profile,image_version='mu300-r1',kernel_release='7.2.8')))
                    m=dict(format='unisoc-diff-ota-2',family=family,profile=profile,base_version='mu300-r1',
                           target_version='mu300-r2',kernel_release='7.2.8',
                           operations=[dict(path=path,before=file(b'old'),after=file(b'new'))])
                    p=package(root/'patch.ota',m)
                    with Patch(p) as patch:Engine(root).apply(patch)
                    self.assertEqual((root/path).read_bytes(),b'new')

    def test_authenticated_target_parent_metadata_is_used_only_when_missing(self):
        with Patch(self.archive(indexed=True)) as patch:Engine(self.root).apply(patch)
        self.assertEqual(stat.S_IMODE((self.root/'usr/lib/removed').stat().st_mode),0o750)
        self.assertEqual(self.user.read_bytes(),b'USER WIFI')

    def test_existing_parent_permissions_and_data_are_kept(self):
        parent=self.root/'usr/lib/removed';parent.mkdir(parents=True);parent.chmod(0o700)
        (parent/'user-data').write_bytes(b'KEEP')
        with Patch(self.archive(indexed=True)) as patch:Engine(self.root).apply(patch)
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode),0o700)
        self.assertEqual((parent/'user-data').read_bytes(),b'KEEP')

    def test_failure_removes_only_new_empty_parents_and_restores_user_state(self):
        def fail(*args):
            if args[-1]=='usr/lib/removed/app':raise RuntimeError('interrupted')
        with Patch(self.archive(indexed=True)) as patch:
            with self.assertRaisesRegex(RuntimeError,'interrupted'):Engine(self.root).apply(patch,fail)
        self.assertFalse((self.root/'usr').exists())
        self.assertEqual(self.user.read_bytes(),b'USER WIFI')

    def test_rollback_keeps_user_files_added_to_recreated_directories(self):
        engine=Engine(self.root)
        with Patch(self.archive(indexed=True)) as patch:engine.apply(patch)
        user=self.root/'usr/lib/removed/user-data';user.write_bytes(b'KEEP')
        self.assertTrue(engine.rollback_check()['available'])
        journal=engine.state/'journal.json';record=json.loads(journal.read_text())
        record['status']='rolling_back';atomic_json(journal,record)
        with engine.lock():engine.recover()
        self.assertEqual(user.read_bytes(),b'KEEP')
        self.assertFalse((user.parent/'app').exists())
        self.assertEqual(self.user.read_bytes(),b'USER WIFI')

    def test_parent_file_or_symlink_is_not_deleted_or_followed(self):
        parent=self.root/'usr';parent.write_bytes(b'USER FILE')
        with Patch(self.archive()) as patch:
            with self.assertRaises((ValueError,OSError)):Engine(self.root).apply(patch)
        self.assertEqual(parent.read_bytes(),b'USER FILE')
        parent.unlink();outside=self.base/'outside';outside.mkdir();parent.symlink_to(outside,target_is_directory=True)
        with Patch(self.archive()) as patch:
            with self.assertRaises(ValueError):Engine(self.root).apply(patch)
        self.assertEqual(list(outside.iterdir()),[])

    def test_package_database_parent_can_be_missing(self):
        raw=b'P:demo\nV:2\nA:aarch64\n\n';m=manifest([dict(path='lib/apk/db/installed',
            before=file(b'P:demo\nV:1\n\n'),after=file(raw),merge='apk-db')])
        m.update(family='mu300',profile='mu300',base_version='mu300-r1',target_version='mu300-r2',kernel_release='7.2.8')
        p=package(self.base/'db.ota',m,{hashlib.sha256(raw).hexdigest():raw})
        with Patch(p) as patch:Engine(self.root).apply(patch)
        self.assertIn(b'V:2',(self.root/'lib/apk/db/installed').read_bytes())

    def test_atomic_owned_records_create_their_working_parent(self):
        path=self.base/'missing/work/state.json';atomic_json(path,{'ok':True})
        self.assertEqual(json.loads(path.read_text()),{'ok':True})

    def test_cached_builder_recreates_removed_home(self):
        from builder import Builder
        b=Builder(self.root);b.baseline.write_bytes(b'keep this baseline')
        b.home.rmdir();b.status()
        self.assertTrue(b.home.is_dir());self.assertEqual(b.baseline.read_bytes(),b'keep this baseline')

    def test_cached_builder_recreates_entire_state_without_fabricating_a_baseline(self):
        from builder import Builder
        b=Builder(self.root);shutil.rmtree(b.state)
        status=b.status()
        self.assertTrue(b.home.is_dir());self.assertFalse(status['baseline']['available'])
        self.assertFalse(b.baseline.exists())

    def test_patch_and_bundle_exports_create_missing_output_directories(self):
        mu_release(self.base/'old','mu300-r1',{'usr/lib/test':b'old'})
        mu_release(self.base/'new','mu300-r2',{'usr/lib/test':b'new'})
        output=self.base/'removed/deep/update.ota'
        self.assertTrue(analyze(self.base/'old',self.base/'new',output)['can_export'])
        runtime=self.base/'runtime';runtime.mkdir()
        script=runtime/'OpenWrt-Diff-Studio.py';script.write_bytes(b'print("fixture")')
        (runtime/'diff-tool.json').write_text(json.dumps(dict(protocol=1,files={script.name:hashlib.sha256(script.read_bytes()).hexdigest()})))
        dest=self.base/'other/missing/bundle.zip';export_bundle(output,dest,runtime)
        self.assertTrue(dest.is_file())


if __name__=='__main__':unittest.main()
