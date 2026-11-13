"""Real release ZIP comparisons and temp-root transactions; no device access."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.ota_engine import canonical
from test_ota import mu_release, package
if os.name=='posix':
    import fcntl
    from builder import Builder
    from engine import Patch, atomic_json


@unittest.skipUnless(os.name=='posix','Device worker and filesystem transaction tests require Linux')
class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name);self.root=self.folder/'device'
        self.root.mkdir();self.builder=Builder(self.root)
        identity=self.root/'etc/mu300/release.json';identity.parent.mkdir()
        identity.write_text(json.dumps({'image_version':'mu300-r1','profile':'mu300','kernel_release':'7.2.8','openwrt':'25.12.5'}))
        for name,data in {'usr/lib/test':b'old','usr/lib/untouched':b'user edit',
                          'etc/config/wireless':b'user password','usr/lib/my-plugin':b'plugin'}.items():
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        self.archives={}
        for key,version,payload in (('old','mu300-r1',b'old'),('new','mu300-r2',b'new')):
            folder=self.folder/key
            mu_release(folder,version,{'usr/lib/test':payload,'usr/lib/untouched':b'original',
                                      'etc/config/wireless':b'factory'})
            archive=self.folder/(key+'.zip')
            with zipfile.ZipFile(archive,'w') as z:
                for p in folder.rglob('*'):
                    if p.is_file():z.write(p,p.relative_to(folder).as_posix())
            self.archives[key]=archive

    def upload(self,*names):
        for name in names:shutil.copyfile(self.archives[name],self.builder.home/(name+'.zip'))

    def run_job(self,mode):
        with (self.builder.state/'job.lock').open('a') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.builder.run(mode,os.dup(held.fileno()))
        return self.builder.status()

    def rollback(self):
        engine=self.builder.engine
        self.assertTrue(engine.rollback_check()['available'])
        with engine.lock():
            journal=engine.state/'journal.json';data=json.loads(journal.read_text())
            data['status']='rolling_back';atomic_json(journal,data);engine.recover()

    def test_both_modes_generate_identical_patch_without_live_snapshot(self):
        self.upload('old','new')
        first=self.run_job('pair')
        self.assertEqual(first['job']['status'],'complete',first)
        self.assertTrue(first['download']);self.assertTrue(first['baseline']['available'])
        self.assertFalse((self.builder.home/'old.zip').exists())
        with Patch(self.builder.output) as result:
            expected=result.manifest
            paths={op['path'] for op in expected['operations']}
            self.assertNotIn('etc/config/wireless',paths)
            self.assertNotIn('usr/lib/my-plugin',paths)
            self.assertNotIn('usr/lib/untouched',paths)
        self.upload('new');second=self.run_job('current')
        self.assertEqual(first['job']['id'],second['job']['id'])
        self.assertEqual(self.builder.inspect_generated()['id'],second['job']['id'])
        self.assertEqual((self.root/'usr/lib/test').read_bytes(),b'old')
        self.assertEqual((self.root/'etc/config/wireless').read_bytes(),b'user password')
        self.assertEqual((self.root/'usr/lib/untouched').read_bytes(),b'user edit')
        with zipfile.ZipFile(self.builder.output) as z:
            self.assertIn('manifest.json',z.namelist())
            self.assertFalse(any(n.endswith(('.exe','.app')) for n in z.namelist()))

    def test_import_baseline_once_and_reject_wrong_current_version(self):
        self.upload('old');result=self.run_job('baseline')
        self.assertTrue(result['baseline']['available'],result)
        self.assertFalse(result['download'])
        saved=self.builder.baseline.read_bytes()
        shutil.copyfile(self.archives['new'],self.builder.home/'old.zip')
        result=self.run_job('baseline')
        self.assertEqual(result['job']['status'],'failed')
        self.assertEqual(self.builder.baseline.read_bytes(),saved)

    def test_apply_and_rollback_advance_and_restore_clean_baseline(self):
        self.upload('old','new');self.run_job('pair')
        before=self.builder.baseline.read_bytes()
        with Patch(self.builder.output) as result:self.builder.engine.apply(result)
        self.assertEqual(json.loads(self.builder.baseline.read_text())['release']['meta']['image_version'],'mu300-r2')
        self.assertEqual((self.root/'usr/lib/test').read_bytes(),b'new')
        self.assertTrue(self.builder.baseline_status()['available'])
        self.rollback()
        self.assertEqual(self.builder.baseline.read_bytes(),before)
        self.assertEqual((self.root/'usr/lib/test').read_bytes(),b'old')
        self.assertEqual((self.root/'usr/lib/my-plugin').read_bytes(),b'plugin')

    def test_first_ota_without_existing_baseline_restores_absence_on_rollback(self):
        self.upload('old','new');self.run_job('pair');self.builder.baseline.unlink()
        with Patch(self.builder.output) as result:self.builder.engine.apply(result)
        self.assertTrue(self.builder.baseline.is_file())
        self.rollback();self.assertFalse(self.builder.baseline.exists())

    def test_baseline_metadata_rejects_wrong_target_without_restricting_live_files(self):
        self.upload('old','new');self.run_job('pair')
        with Patch(self.builder.output) as result:m=copy.deepcopy(result.manifest)
        m['target_version']='mu300-other'
        with self.assertRaisesRegex(ValueError,'index does not match'):
            package(self.folder/'bad.ota',m)
            Patch(self.folder/'bad.ota')
        self.assertEqual((self.root/'usr/lib/untouched').read_bytes(),b'user edit')

    def test_invalid_inputs_and_background_admission_lock(self):
        for mode in ('','../','live','current'):
            with self.assertRaises(ValueError):self.builder.start(mode)
        self.upload('old','new')
        def launch(argv,**kwargs):
            self.assertEqual(argv[2:4],['_worker','pair'])
            self.assertTrue(kwargs['pass_fds'])
            with (self.builder.state/'job.lock').open('a') as other:
                with self.assertRaises(BlockingIOError):fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with patch('builder.subprocess.Popen',side_effect=launch):
            self.assertEqual(self.builder.start('pair')['status'],'running')
        self.assertEqual(self.builder.status()['job']['status'],'failed')
        self.assertIn('interrupted',self.builder.status()['job']['error'])

    def test_real_detached_worker_finishes_after_ack(self):
        self.upload('old','new')
        result=self.builder.start('pair');self.assertEqual(result['status'],'running')
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            result=self.builder.status()
            if result['job']['status']!='running':break
            time.sleep(.05)
        self.assertEqual(result['job']['status'],'complete',result)

    def test_corrupt_upload_and_missing_dependency_preserve_system(self):
        self.upload('old','new');(self.builder.home/'new.zip').write_bytes(b'bad archive')
        self.assertEqual(self.run_job('pair')['job']['status'],'failed')
        self.assertFalse(self.builder.output.exists())
        self.assertFalse(list(self.builder.home.glob('.build-*')))
        self.upload('old','new')
        with patch('builder.prepare_dependencies',side_effect=ValueError('dependency checksum')):
            result=self.run_job('pair')
        self.assertIn('dependency checksum',result['job']['error'])
        self.assertEqual((self.root/'usr/lib/test').read_bytes(),b'old')

    def test_source_bundle_works_without_host_site_packages(self):
        core=Path(__file__).resolve().parents[1]/'runtime/luci-app-unisoc-ota/root/usr/lib/unisoc-ota'
        code='import sys;sys.path.insert(0,sys.argv[1]);from builder import prepare_dependencies;prepare_dependencies();from dissect.extfs import ExtFS;print(ExtFS.__name__)'
        result=subprocess.run([sys.executable,'-S','-c',code,str(core)],text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'ExtFS')

    def test_stage_keeps_zip_bytes_and_complete_device_generator(self):
        import runpy
        repo=Path(__file__).resolve().parents[1]
        staged=self.folder/'staged';staged.mkdir()
        runpy.run_path(str(repo/'stage-ota.py'))['stage'](staged)
        core=staged/'usr/lib/unisoc-ota'
        original=repo/'runtime/luci-app-unisoc-ota/root/usr/lib/unisoc-ota/generator/extfs-sources.zip'
        self.assertEqual((core/'generator/extfs-sources.zip').read_bytes(),original.read_bytes())
        for name in ('builder.py','baseline.py','uploads.py','lz4raw-aarch64.so','build.json'):
            self.assertTrue((core/name).is_file(),name)
        code='import sys;sys.path.insert(0,sys.argv[1]);from generator.ota_runtime import support_sources;s=support_sources();assert "builder.py" in s;assert "generator/extfs-sources.zip" in s'
        result=subprocess.run([sys.executable,'-S','-c',code,str(core)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
