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

    def test_disposable_cache_corruption_cannot_disable_valid_baseline(self):
        self.upload('old');self.run_job('baseline')
        cache=self.builder.home/'baseline-status.json'
        for raw in ('{','null','[]','{"value":null}','x'*70000):
            cache.write_text(raw)
            self.assertTrue(self.builder.baseline_status()['available'],raw[:30])
        saved=json.loads(cache.read_text());saved['value']=None;cache.write_text(json.dumps(saved))
        self.assertTrue(self.builder.baseline_status()['available'])
        cache.unlink()
        with patch('builder.atomic_json',side_effect=PermissionError('cache read only')):
            self.assertTrue(self.builder.baseline_status()['available'])

    def test_cached_baseline_does_not_hide_same_length_corruption_with_preserved_mtime(self):
        self.upload('old');self.run_job('baseline')
        self.assertTrue(self.builder.baseline_status()['available'])
        stamp=self.builder.baseline.stat();raw=self.builder.baseline.read_bytes()
        broken=raw.replace(b'"sha256":"',b'"sha256":"x',1)[:-1]
        self.assertEqual(len(broken),len(raw))
        self.builder.baseline.write_bytes(broken);os.utime(self.builder.baseline,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        result=self.builder.baseline_status();self.assertFalse(result['available']);self.assertEqual(result['reason'],'invalid')

    def test_missing_corrupt_or_wrong_version_baseline_can_be_reinitialized(self):
        for raw in (None,b'',b'null',b'{"format":"broken"}'):
            with self.subTest(raw=raw):
                if raw is None:self.builder.baseline.unlink(missing_ok=True)
                else:self.builder.baseline.write_bytes(raw)
                result=self.builder.status();self.assertFalse(result['baseline']['available'])
                self.upload('old');result=self.run_job('baseline')
                self.assertTrue(result['baseline']['available'],result)
        value=json.loads(self.builder.baseline.read_text());value['release']['meta']['image_version']='mu300-other'
        import hashlib
        value['sha256']=hashlib.sha256(canonical(value['release'])).hexdigest()
        atomic_json(self.builder.baseline,value)
        self.assertEqual(self.builder.baseline_status()['reason'],'mismatch')
        self.upload('old');self.assertTrue(self.run_job('baseline')['baseline']['available'])

    def test_corrupt_task_record_does_not_block_new_task_or_upload_status(self):
        for raw in ('{','null','[]','{"status":"unknown"}','x'*70000):
            self.builder.job.write_text(raw);self.upload('old')
            status=self.builder.status();self.assertEqual(status['job']['status'],'failed')
            self.assertIn('old.zip',status['uploads'])
            with patch('builder.subprocess.Popen'):
                ack=self.builder.start('baseline')
                self.assertEqual(ack['status'],'running')
                self.assertEqual(len(ack['task_id']),32)
            result=self.run_job('baseline');self.assertEqual(result['job']['status'],'complete',result)

    def test_failed_generation_and_baseline_repair_keep_previous_download_and_review(self):
        self.upload('old','new');good=self.run_job('pair')
        original=self.builder.output.read_bytes()
        self.upload('old');baseline=self.run_job('baseline')
        self.assertTrue(baseline['download']);self.assertEqual(baseline['output']['id'],good['job']['id'])
        self.assertEqual(self.builder.inspect_generated()['id'],good['job']['id'])
        self.upload('new');(self.builder.home/'new.zip').write_bytes(b'bad upload')
        failure=self.run_job('current')
        self.assertEqual(failure['job']['status'],'failed');self.assertTrue(failure['download'])
        self.assertEqual(failure['output']['base_version'],'mu300-r1')
        self.assertEqual(self.builder.output.read_bytes(),original)
        self.assertEqual(self.builder.inspect_generated()['id'],good['job']['id'])

    def test_cleanup_only_removes_owned_interrupted_scratch_under_admission_lock(self):
        owned=self.builder.home/'.build-owned';owned.mkdir()
        atomic_json(owned/'owner.json',{'owner':'unisoc-ota-builder','task_id':'a'*32})
        (owned/'partial.img').write_bytes(b'partial image')
        unowned=self.builder.home/'.build-keep';unowned.mkdir();(unowned/'private').write_bytes(b'keep')
        linked=self.builder.home/'.build-link';linked.symlink_to(unowned,target_is_directory=True)
        with (self.builder.state/'job.lock').open('a') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):self.builder.start('baseline')
            self.assertTrue(owned.exists())
        with self.assertRaisesRegex(ValueError,'required full'):self.builder.start('baseline')
        self.assertFalse(owned.exists());self.assertEqual((unowned/'private').read_bytes(),b'keep');self.assertTrue(linked.is_symlink())

    def test_each_admitted_task_has_distinct_identity(self):
        self.upload('old')
        with patch('builder.subprocess.Popen'):
            first=self.builder.start('baseline');second=self.builder.start('baseline')
        self.assertNotEqual(first['task_id'],second['task_id'])

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

    def test_delete_generated_frees_only_output_and_keeps_baseline_uploads_and_recovery(self):
        self.upload('old','new');ready=self.run_job('pair')
        baseline=self.builder.baseline.read_bytes();version=(self.root/'etc/mu300/release.json').read_bytes()
        recovery=self.builder.state/'backup-123';recovery.mkdir();(recovery/'keep').write_bytes(b'recovery')
        self.upload('new')
        self.assertTrue(ready['has_output']);self.assertEqual(ready['output_bytes'],self.builder.output.stat().st_size)
        deleted=self.builder.discard_generated(ready['output_id'])
        self.assertFalse(self.builder.output.exists());self.assertFalse(deleted['has_output']);self.assertFalse(deleted['download'])
        self.assertEqual(deleted['output_bytes'],0);self.assertIsNone(deleted['output_id'])
        self.assertEqual(deleted['job']['id'],ready['job']['id'])
        self.assertEqual(self.builder.baseline.read_bytes(),baseline);self.assertTrue(deleted['baseline']['available'])
        self.assertTrue((self.builder.home/'new.zip').exists());self.assertEqual((recovery/'keep').read_bytes(),b'recovery')
        self.assertEqual((self.root/'etc/mu300/release.json').read_bytes(),version)
        self.assertEqual((self.root/'etc/config/wireless').read_bytes(),b'user password')
        self.assertFalse(self.builder.discard_generated(ready['output_id'])['has_output'],'Repeat deletion should be harmless')

    def test_delete_is_locked_against_generation_inspection_apply_and_pending_recovery(self):
        self.builder.output.write_bytes(b'output');token=self.builder.status()['output_id']
        with (self.builder.state/'job.lock').open('a') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):self.builder.discard_generated(token)
        self.assertEqual(self.builder.output.read_bytes(),b'output')
        (self.builder.state/'journal.json').write_text(json.dumps({'status':'applying'}))
        with self.assertRaisesRegex(ValueError,'recovery'):self.builder.discard_generated(token)
        self.assertTrue(self.builder.output.exists())

    def test_failed_generation_retained_output_remains_deletable_and_stale_confirmation_is_rejected(self):
        self.builder.output.write_bytes(b'previous output')
        atomic_json(self.builder.job,{'status':'failed','error':'bad newer upload'})
        ready=self.builder.status();self.assertTrue(ready['download']);self.assertTrue(ready['has_output'])
        other=self.builder.home/'other';other.write_bytes(b'new output');os.replace(other,self.builder.output)
        with self.assertRaisesRegex(ValueError,'changed'):self.builder.discard_generated(ready['output_id'])
        self.assertEqual(self.builder.output.read_bytes(),b'new output')
        token=self.builder.status()['output_id'];self.builder.discard_generated(token)
        self.assertFalse(self.builder.output.exists())

    def test_lost_result_metadata_keeps_download_and_full_readonly_verification(self):
        self.upload('old','new');ready=self.run_job('pair')
        self.builder.result.write_text('null');self.builder.job.write_text('{')
        status=self.builder.status();self.assertTrue(status['download']);self.assertIsNone(status['output'])
        self.assertEqual(self.builder.inspect_generated()['id'],ready['job']['id'])
        atomic_json(self.builder.result,{'id':int('1'*64),'base_version':'mu300-r1','target_version':'mu300-r2',
                                        'output_id':self.builder.status()['output_id']})
        self.assertIsNone(self.builder.output_summary())
        self.assertEqual(self.builder.inspect_generated()['id'],ready['job']['id'])
        self.assertEqual((self.root/'usr/lib/test').read_bytes(),b'old')
        self.builder.output.write_bytes(b'incomplete archive')
        with self.assertRaises(zipfile.BadZipFile):self.builder.inspect_generated()

    def test_delete_does_not_follow_output_symlink(self):
        target=self.folder/'private';target.write_bytes(b'keep')
        self.builder.output.symlink_to(target)
        self.assertFalse(self.builder.status()['has_output'])
        with self.assertRaisesRegex(ValueError,'Unsafe'):self.builder.discard_generated('b'*64)
        self.assertEqual(target.read_bytes(),b'keep')

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

    def test_native_generator_inventory_excludes_bytecode_and_directories(self):
        from build import generator_data_args
        args=generator_data_args()
        self.assertTrue(args)
        for flag,item in zip(args[::2],args[1::2]):
            self.assertEqual(flag,'--add-data')
            source,destination=item.rsplit(os.pathsep,1)
            path=Path(source)
            self.assertTrue(path.is_file(),source)
            self.assertNotIn('__pycache__',path.parts)
            self.assertNotEqual(path.suffix,'.pyc')
            self.assertEqual(destination,'generator')


if __name__=='__main__':unittest.main()
