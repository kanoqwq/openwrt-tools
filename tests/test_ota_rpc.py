"""rpcd HTTP session metadata and post-commit plugin registration regressions."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(os.name == 'posix', 'rpcd uses Linux locks')
class RPCTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parents[1]/'runtime/luci-app-unisoc-ota/root/usr/lib/unisoc-ota'
        sys.path.insert(0, str(directory)); self.addCleanup(lambda: sys.path.remove(str(directory)))
        spec = importlib.util.spec_from_file_location('ota_rpc_regression', directory/'rpc.py')
        self.rpc = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.rpc)

    def call(self, method, arguments):
        with patch.object(sys, 'argv', ['rpc.py','call',method]), patch.object(sys, 'stdin', io.StringIO(json.dumps(arguments))), redirect_stdout(io.StringIO()):
            self.rpc.main()

    def test_upload_preflight_recreates_missing_state_and_builder_directories(self):
        for family,profile in [('mu300','mu300'),('mu300','u30pro'),('e5','e5')]:
            with self.subTest(profile=profile),tempfile.TemporaryDirectory() as d:
                root=Path(d);release=root/'etc'/family/'release.json';release.parent.mkdir(parents=True)
                release.write_text(json.dumps({'image_version':'test-r1','profile':profile,
                                               'kernel_release':'test-kernel','openwrt':'25.12.5'}))
                self.rpc.ROOT=d;self.rpc.STATE=root/'etc/unisoc-ota'
                self.assertFalse(self.rpc.STATE.exists())
                reply=self.rpc.dispatch('build_status')
                self.assertEqual(reply['job']['status'],'idle')
                home=self.rpc.STATE/'builder'
                self.assertEqual(home.stat().st_mode & 0o777,0o700)
                # Same open as CGI: no implicit mkdir and no firmware changes.
                target=home/'old.zip';target.write_bytes(b'PK test upload');target.unlink();home.rmdir()
                self.rpc.dispatch('build_status')
                self.assertTrue(home.is_dir())
                self.assertEqual(json.loads(release.read_text())['image_version'],'test-r1')

    def test_authenticated_metadata_is_accepted_for_every_method(self):
        for method in self.rpc.METHODS:
            args = {'ubus_rpc_session': 'a'*32}
            if method in ('start','start_generated','discard_generated'): args['id'] = 'b'*64
            if method == 'generate':args['mode']='pair'
            if method == 'inspect_begin':args['source']='upload'
            if method == 'inspect_status':args['ticket']='c'*32
            with patch.object(self.rpc, 'dispatch', return_value={}) as dispatch:
                self.call(method, args)
                if method=='generate':dispatch.assert_called_once_with(method,mode='pair')
                elif method=='inspect_begin':dispatch.assert_called_once_with(method,mode='upload')
                elif method=='inspect_status':dispatch.assert_called_once_with(method,review_id='c'*32)
                else:dispatch.assert_called_once_with(method, None, 'b'*64 if method in ('start','start_generated','discard_generated') else None)

    def test_large_inspection_rpc_reply_excludes_release_index_and_runtime(self):
        from test_ota import manifest
        m=manifest();m.update(release_index={'files':'x'*(2<<20)},runtime={'files':'x'*(1<<20)})
        result={'id':'a'*64,'manifest':m,'applicable':True,'required_bytes':123,'free_bytes':456,
                'preserved':['etc/shadow'],'conflicts':[],'boot_work':True}
        original=json.dumps(result)
        with patch.object(self.rpc,'dispatch',return_value=result), patch.object(sys,'argv',['rpc.py','call','inspect']), \
             patch.object(sys,'stdin',io.StringIO('{}')),redirect_stdout(io.StringIO()) as out:
            self.rpc.main()
        reply=json.loads(out.getvalue())
        self.assertLess(len(out.getvalue()),4096)
        self.assertNotIn('release_index',reply['manifest']);self.assertNotIn('runtime',reply['manifest'])
        self.assertEqual(reply['manifest']['operations'],[None]);self.assertEqual(reply['id'],result['id'])
        self.assertEqual(reply['preserved_count'],1);self.assertTrue(reply['applicable'])
        self.assertEqual(json.dumps(result),original,'Summary changed verified manifest')
        with patch.object(self.rpc,'dispatch',return_value=result),patch.object(sys,'argv',['rpc.py','inspect']),redirect_stdout(io.StringIO()) as cli:
            self.rpc.main()
        self.assertEqual(json.loads(cli.getvalue()),result,'Desktop/CLI lost the complete manifest')

    def test_inspection_ack_does_no_bulk_work_and_inherits_admission_lock(self):
        import fcntl
        with tempfile.TemporaryDirectory() as d:
            self.rpc.STATE=Path(d);self.rpc.UPLOAD=Path(d)/'upload.ota';self.rpc.UPLOAD.write_bytes(b'not read in request')
            def launch(argv,**kwargs):
                self.assertEqual(argv[2:4],['_inspect','upload'])
                with (Path(d)/'job.lock').open('a') as held:
                    with self.assertRaises(BlockingIOError):fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertTrue(kwargs['pass_fds']);self.assertTrue(kwargs['start_new_session'])
            with patch.object(self.rpc,'Patch') as content,patch.object(self.rpc,'ota_payload') as payload, \
                 patch.object(self.rpc.subprocess,'Popen',side_effect=launch):
                reply=self.rpc.dispatch('inspect_begin',mode='upload')
                content.assert_not_called();payload.assert_not_called()
            self.assertEqual(reply['status'],'running');self.assertEqual(len(reply['ticket']),32)
            self.assertFalse((Path(d)/'job.json').exists());self.assertFalse((Path(d)/'journal.json').exists())
            self.assertEqual(self.rpc.inspection_status(reply['ticket'])['status'],'failed','Dead worker was not detected')

    def test_inspection_rejects_arbitrary_sources_tickets_and_pending_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            self.rpc.STATE=Path(d);self.rpc.UPLOAD=Path(d)/'upload.ota';self.rpc.UPLOAD.touch()
            for source in ('/etc/shadow','../upload.ota',None):
                with self.assertRaises(ValueError):self.rpc.begin_inspection(source)
            for ticket in ('../../etc/shadow',9,'b'*64):
                with self.assertRaises(ValueError):self.rpc.inspection_status(ticket)
            (Path(d)/'journal.json').write_text(json.dumps({'status':'applying'}))
            with patch.object(self.rpc.subprocess,'Popen') as launch:
                with self.assertRaisesRegex(ValueError,'recovery'):self.rpc.begin_inspection('upload')
                launch.assert_not_called()

    def test_detached_inspection_checks_content_and_keeps_accounts_unchanged(self):
        import fcntl
        from test_ota import package
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.rpc.STATE=root/'etc/unisoc-ota';self.rpc.STATE.mkdir(parents=True)
            self.rpc.UPLOAD=package(self.rpc.STATE/'upload.ota')
            accounts=root/'etc/shadow';accounts.write_bytes(b'private password remains')
            expected={'id':'a'*64,'manifest':{'base_version':'r1','target_version':'r2','operations':[]},'applicable':True}
            with (self.rpc.STATE/'job.lock').open('a') as held,patch.object(self.rpc,'Engine') as engine:
                engine.return_value.inspect.return_value=expected
                fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.rpc.inspect_worker('upload','c'*32,'',os.dup(held.fileno()))
            value=self.rpc.inspection_status('c'*32)
            self.assertEqual(value['status'],'complete');self.assertEqual(value['result']['id'],'a'*64)
            self.assertTrue(self.rpc.UPLOAD.exists());self.assertEqual(accounts.read_bytes(),b'private password remains')
            self.assertFalse((self.rpc.STATE/'journal.json').exists())
            with (self.rpc.STATE/'job.lock').open('a') as held,patch.object(self.rpc,'Engine') as engine:
                fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.rpc.inspect_worker('upload','d'*32,'b'*64,os.dup(held.fileno()))
                engine.return_value.inspect.assert_not_called()
            self.assertEqual(self.rpc.inspection_status('d'*32)['status'],'failed')
            with self.assertRaisesRegex(ValueError,'replaced'):self.rpc.inspection_status('c'*32)

    def test_real_detached_inspection_finishes_without_an_apply_transaction(self):
        import subprocess,time
        from test_ota import package
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);state=root/'etc/unisoc-ota';state.mkdir(parents=True)
            identity=root/'etc/e5/release.json';identity.parent.mkdir()
            identity.write_text(json.dumps({'profile':'e5','image_version':'e5-r1','kernel_release':'6.18.54-e5-test'}))
            (root/'usr/lib').mkdir(parents=True);(root/'usr/lib/test').write_bytes(b'old')
            (root/'etc/shadow').write_bytes(b'private-account')
            package(state/'upload.ota')
            env=dict(os.environ,UNISOC_OTA_ROOT=d)
            argv=[sys.executable,self.rpc.__file__]
            begin=subprocess.run(argv+['call','inspect_begin'],input='{"source":"upload"}',env=env,capture_output=True,text=True,timeout=10)
            ack=json.loads(begin.stdout);self.assertEqual(ack['status'],'running',begin.stderr)
            for _ in range(100):
                value=json.loads((state/'inspection.json').read_text())
                if value['status']!='running':break
                time.sleep(.05)
            self.assertEqual(value['status'],'complete',value)
            self.assertEqual(value['ticket'],ack['ticket']);self.assertTrue(value['result']['applicable'])
            self.assertEqual((root/'usr/lib/test').read_bytes(),b'old')
            self.assertEqual((root/'etc/shadow').read_bytes(),b'private-account')
            self.assertFalse((state/'journal.json').exists());self.assertFalse((state/'job.json').exists())

    def test_other_arguments_still_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unexpected RPC'):
            self.call('status', {'ubus_rpc_session':'a'*32,'command':'not-allowed'})
        with self.assertRaisesRegex(ValueError, 'must be an object'):
            self.call('status', ['not-an-object'])
        for method in ('generate','start_generated','inspect_generated','build_status'):
            with self.subTest(method=method),self.assertRaisesRegex(ValueError,'Unexpected RPC'):
                self.call(method,{'path':'/etc/shadow','mode':'pair'})

    def test_generated_apply_preserves_original_download_and_review_id(self):
        from test_ota import package
        with tempfile.TemporaryDirectory() as d:
            self.rpc.STATE=Path(d);home=Path(d)/'builder';home.mkdir()
            source=package(home/'update.ota')
            with self.rpc.Patch(source) as content:review=content.id
            staged=self.rpc.stage_upload(source,review,consume=False)
            self.assertTrue(source.is_file());self.assertTrue(staged.is_file())
            with self.assertRaisesRegex(ValueError,'differs'):
                self.rpc.stage_upload(source,'b'*64,consume=False)
            self.assertTrue(source.is_file());self.assertFalse(list(Path(d).glob('candidate-*')))

    def test_generation_lock_does_not_misreport_old_completed_update_as_running(self):
        import fcntl
        with tempfile.TemporaryDirectory() as d:
            self.rpc.STATE=Path(d);self.rpc.JOB=Path(d)/'job.json'
            self.rpc.JOB.write_text(json.dumps({'status':'complete'}))
            (Path(d)/'builder').mkdir();(Path(d)/'builder/job.json').write_text(json.dumps({'status':'running'}))
            with (Path(d)/'job.lock').open('a') as held,patch.object(self.rpc,'Engine') as engine:
                engine.return_value.status.return_value={'identity':{'version':'r1'}}
                fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertEqual(self.rpc.dispatch('status')['job']['status'],'complete')

    def test_corrupt_generation_record_cannot_break_system_patch_status(self):
        with tempfile.TemporaryDirectory() as d:
            self.rpc.STATE=Path(d);self.rpc.JOB=Path(d)/'job.json'
            (Path(d)/'builder').mkdir()
            for raw in ('{','null','[]','x'*70000):
                (Path(d)/'builder/job.json').write_text(raw)
                with patch.object(self.rpc,'Engine') as engine:
                    engine.return_value.status.return_value={'identity':{'version':'r1'}}
                    self.assertEqual(self.rpc.dispatch('status')['identity']['version'],'r1')

    def test_corrupt_inspection_record_is_retryable_and_does_not_change_transactions(self):
        with tempfile.TemporaryDirectory() as d:
            self.rpc.STATE=Path(d)
            for raw in ('{','null','[]','{"status":"unknown"}','{"status":"running"}'):
                (Path(d)/'inspection.json').write_text(raw)
                result=self.rpc.inspection_status('a'*32)
                self.assertEqual(result['status'],'failed');self.assertEqual(result['ticket'],'a'*32)
                self.assertFalse((Path(d)/'job.json').exists());self.assertFalse((Path(d)/'journal.json').exists())

    def test_inspection_lock_is_not_reported_as_another_update(self):
        import fcntl
        with tempfile.TemporaryDirectory() as d:
            self.rpc.STATE=Path(d);self.rpc.JOB=Path(d)/'job.json'
            self.rpc.JOB.write_text(json.dumps({'status':'complete'}))
            (Path(d)/'inspection.json').write_text(json.dumps({'status':'running','ticket':'a'*32}))
            with (Path(d)/'job.lock').open('a') as held,patch.object(self.rpc,'Engine') as engine:
                engine.return_value.status.return_value={'identity':{'version':'r1'}}
                fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertEqual(self.rpc.dispatch('status')['job']['status'],'complete')

    def test_corrupt_update_task_record_preserves_authoritative_pending_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            self.rpc.STATE=Path(d);self.rpc.JOB=Path(d)/'job.json'
            for raw in ('{','null','[]','{"status":9}'):
                self.rpc.JOB.write_text(raw)
                with patch.object(self.rpc,'Engine') as engine:
                    engine.return_value.status.return_value={'identity':{'version':'r1'},'transaction':{'status':'applying'},'recovery':{'pending':True}}
                    value=self.rpc.dispatch('status')
                    self.assertEqual(value['job']['status'],'failed');self.assertTrue(value['recovery']['pending'])
                    self.assertEqual(value['transaction']['status'],'applying')
                    self.assertEqual(self.rpc.JOB.read_text(),raw)

    def test_bootstrap_passes_review_id_for_generic_web_upload(self):
        with patch.object(sys,'argv',['rpc.py','start','/target/etc/unisoc-ota/upload.ota','b'*64]), \
             patch.object(self.rpc,'dispatch',return_value={}) as dispatch, redirect_stdout(io.StringIO()):
            self.rpc.main()
        dispatch.assert_called_once_with('start','/target/etc/unisoc-ota/upload.ota','b'*64)

    def test_start_acknowledges_before_any_bulk_preparation_and_holds_lock(self):
        import fcntl
        with tempfile.TemporaryDirectory() as d:
            self.rpc.STATE=Path(d);self.rpc.JOB=Path(d)/'job.json'
            def launch(argv,**kwargs):
                state=json.loads(self.rpc.JOB.read_text())
                self.assertEqual((state['status'],state['phase'],state['patch_id']),('running','queued','b'*64))
                self.assertEqual(argv[2],'_prepare_apply')
                self.assertTrue(kwargs['pass_fds'])
                with (Path(d)/'job.lock').open('a') as other:
                    with self.assertRaises(BlockingIOError):fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
                return Mock()
            with patch.object(self.rpc.subprocess,'Popen',side_effect=launch), \
                 patch.object(self.rpc,'Patch',side_effect=AssertionError('HTTP must not inspect/copy the archive')):
                result=self.rpc.worker('apply',str(Path(d)/'upload.ota'),'b'*64)
            self.assertEqual(result,{'status':'running','patch_id':'b'*64,'phase':'queued'})

    def test_spawn_failure_cannot_leave_a_phantom_running_job(self):
        with tempfile.TemporaryDirectory() as d:
            self.rpc.STATE=Path(d);self.rpc.JOB=Path(d)/'job.json'
            with patch.object(self.rpc.subprocess,'Popen',side_effect=OSError('fixture spawn failure')):
                with self.assertRaises(OSError):self.rpc.worker('apply',str(Path(d)/'upload.ota'),'b'*64)
            state=json.loads(self.rpc.JOB.read_text())
            self.assertEqual(state['status'],'failed')
            self.assertIn('spawn failure',state['result']['error'])

    def test_first_web_start_prepares_then_enters_worker_without_recursive_start(self):
        import fcntl
        for ram in ('','/'):
            with self.subTest(ram=ram), tempfile.TemporaryDirectory() as d:
                root=Path(d);self.rpc.ROOT=d;self.rpc.STATE=root/'etc/unisoc-ota';self.rpc.STATE.mkdir(parents=True)
                self.rpc.JOB=self.rpc.STATE/'job.json';upload=self.rpc.STATE/'upload.ota'
                # This fixture mocks the protocol verifier below, but upload
                # staging still validates a real ZIP/container structure.
                import zipfile
                with zipfile.ZipFile(upload,'w') as zipped:
                    zipped.writestr('manifest.json',json.dumps({'format':'unisoc-diff-ota-2'}))
                home=Path('/proc/1/root' if ram else d)/'tmp'/('unisoc-ota-'+'b'*64)
                with (self.rpc.STATE/'job.lock').open('a') as held, \
                     patch.dict(os.environ,{'UNISOC_OTA_RAM':ram}), \
                     patch.object(self.rpc,'Patch') as archive, \
                     patch.object(self.rpc,'prepare_runtime',return_value=home) as prepare, \
                     patch.object(self.rpc.subprocess,'run',return_value=Mock(returncode=0,stdout=json.dumps({'id':'b'*64,'applicable':True}),stderr='')) as run, \
                     patch.object(self.rpc.os,'execve',side_effect=SystemExit) as execute:
                    archive.return_value.__enter__.return_value.id='b'*64
                    fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    with self.assertRaises(SystemExit):self.rpc.prepare_apply(str(upload),'b'*64,held.fileno())
                    prepare.assert_called_once()
                    self.assertEqual(prepare.call_args.kwargs,{'root':'/proc/1/root' if ram else d})
                    setup=run.call_args.args[0]
                    self.assertEqual(setup[-2:],[ 'inspect','/tmp/unisoc-ota-'+'b'*64])
                    argv=execute.call_args.args[1]
                    self.assertEqual(argv[-4:],['/ota/rpc.py','_worker','apply','/target/etc/unisoc-ota/reviewed-'+'b'*64+'.ota'])
                    self.assertEqual(execute.call_args.args[2]['UNISOC_OTA_ROOT'],'/target')
                    if ram:self.assertEqual(setup[:3],['/ota/busybox','chroot','/proc/1/root'])
                self.assertFalse(upload.exists())
                self.assertEqual((self.rpc.STATE/'active-runtime').read_text(),'/tmp/unisoc-ota-'+'b'*64+'/root\n')
                self.assertEqual(json.loads(self.rpc.JOB.read_text())['phase'],'applying')
                self.assertFalse(list(self.rpc.STATE.glob('candidate-*')))

    def test_changed_upload_fails_before_runtime_or_system_writes(self):
        with tempfile.TemporaryDirectory() as d:
            self.rpc.STATE=Path(d);self.rpc.JOB=Path(d)/'job.json';upload=Path(d)/'upload.ota';upload.write_bytes(b'changed')
            with (Path(d)/'job.lock').open('a') as held, patch.object(self.rpc,'Patch') as archive, \
                 patch.object(self.rpc,'prepare_runtime') as prepare, patch.object(self.rpc.os,'execve') as execute:
                archive.return_value.__enter__.return_value.id='c'*64
                self.rpc.prepare_apply(str(upload),'b'*64,held.fileno())
            self.assertEqual(json.loads(self.rpc.JOB.read_text())['status'],'failed')
            self.assertTrue(upload.exists())
            self.assertFalse(list(Path(d).glob('candidate-*')))
            prepare.assert_not_called();execute.assert_not_called()

    def test_reload_uses_stock_service_and_health_checks_before_success(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); service=root/'etc/init.d/rpcd'; service.parent.mkdir(parents=True); service.touch()
            self.rpc.ROOT = d
            result = Mock(returncode=0, stdout=json.dumps({'identity':{'version':'test'}}), stderr='')
            with patch.dict(os.environ, {'UNISOC_OTA_RAM':'/'}), patch.object(self.rpc,'mounted',return_value=True), patch.object(self.rpc,'rpc_object_id',side_effect=['old','new','new']), patch.object(self.rpc.subprocess,'run',return_value=result) as run:
                self.assertEqual(self.rpc.activate_rpc(), {'status':'ready'})
                self.assertEqual(run.call_args_list[0].args[0], ['/ota/busybox','chroot',d,'/etc/init.d/rpcd','reload'])
                self.assertEqual(run.call_args_list[1].args[0][-1], 'ubus call unisoc-ota status')

    def test_ram_activation_binds_host_service_runtime_before_reload(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); service=root/'etc/init.d/rpcd'; service.parent.mkdir(parents=True); service.touch(); self.rpc.ROOT=d
            missing=Mock(returncode=1); ok=Mock(returncode=0,stdout='{"identity":{}}',stderr='')
            with patch.dict(os.environ, {'UNISOC_OTA_RAM':'/'}), patch.object(self.rpc,'mounted',return_value=False), patch.object(self.rpc,'rpc_object_id',side_effect=['old','new','new']), patch.object(self.rpc.subprocess,'run',side_effect=[ok,ok,ok,ok]) as run:
                self.assertEqual(self.rpc.activate_rpc(), {'status':'ready'})
                self.assertEqual(run.call_args_list[0].args[0], ['/ota/busybox','mount','--bind','/proc/1/root/tmp',d+'/tmp'])
                self.assertEqual(run.call_args_list[1].args[0], ['/ota/busybox','mount','--bind','/proc/1/root/proc',d+'/proc'])
                self.assertEqual(run.call_args_list[2].args[0][-2:], ['/etc/init.d/rpcd','reload'])

    def test_mount_table_decodes_paths_and_matches_exact_bind_mount(self):
        with tempfile.TemporaryDirectory() as d:
            table = Path(d)/'mountinfo'
            table.write_text('31 20 0:1 / /target rw - ext4 /dev/root rw\n'
                             '32 31 0:2 / /target/tmp rw - tmpfs tmpfs rw\n'
                             r'33 31 0:2 / /target/with\040space rw - tmpfs tmpfs rw'+'\n')
            self.assertTrue(self.rpc.mounted('/target/tmp', table))
            self.assertTrue(self.rpc.mounted('/target/with space', table))
            self.assertFalse(self.rpc.mounted('/target/tm', table))
            self.assertFalse(self.rpc.mounted('/target/tmp/child', table))

    def test_reload_failure_is_warning_not_filesystem_failure(self):
        with tempfile.TemporaryDirectory() as d:
            service=Path(d)/'etc/init.d/rpcd'; service.parent.mkdir(parents=True); service.touch(); self.rpc.ROOT=d
            with patch.object(self.rpc,'rpc_object_id',return_value='old'), patch.object(self.rpc.subprocess,'run',return_value=Mock(returncode=1,stdout='',stderr='reload failed')):
                self.assertEqual(self.rpc.activate_rpc()['status'], 'warning')

    def test_fixed_signature_does_not_need_python_or_live_runtime(self):
        import subprocess
        proxy=Path(self.rpc.__file__).with_name('proxy.sh')
        with tempfile.TemporaryDirectory() as empty:
            result=subprocess.run(['/bin/sh',str(proxy),'list'],env={'PATH':empty},
                                  capture_output=True,text=True,timeout=3,check=True)
            self.assertEqual(json.loads(result.stdout),self.rpc.METHODS)
            self.assertEqual(result.stderr,'')
            extra=subprocess.run(['/bin/sh',str(proxy),'list','unexpected'],env={'PATH':empty},
                                 capture_output=True,text=True,timeout=3)
            self.assertNotEqual(extra.returncode,0,'Malformed invocation obtained a valid registration signature')

    def test_old_object_cannot_pass_post_reload_activation(self):
        with tempfile.TemporaryDirectory() as d:
            service=Path(d)/'etc/init.d/rpcd';service.parent.mkdir(parents=True);service.touch();self.rpc.ROOT=d
            ok=Mock(returncode=0,stdout='{"identity":{"version":"new"}}',stderr='')
            with patch.object(self.rpc,'rpc_object_id',side_effect=['old','old',None,'new','new']) as generation, \
                 patch.object(self.rpc.subprocess,'run',return_value=ok) as run, \
                 patch.object(self.rpc.time,'monotonic',side_effect=[0,0.1,0.2]), \
                 patch.object(self.rpc.time,'sleep'):
                self.assertEqual(self.rpc.activate_rpc(),{'status':'ready'})
            self.assertEqual(generation.call_count,5)
            self.assertEqual(len(run.call_args_list),2,'Health check ran against the old registration')

    def test_another_reload_during_health_check_is_not_ready(self):
        with tempfile.TemporaryDirectory() as d:
            service=Path(d)/'etc/init.d/rpcd';service.parent.mkdir(parents=True);service.touch();self.rpc.ROOT=d
            ok=Mock(returncode=0,stdout='{"identity":{}}',stderr='')
            with patch.object(self.rpc,'rpc_object_id',side_effect=['old','new','changed',None]), \
                 patch.object(self.rpc.subprocess,'run',return_value=ok), \
                 patch.object(self.rpc.time,'monotonic',side_effect=[0,1,6]), \
                 patch.object(self.rpc.time,'sleep'):
                self.assertEqual(self.rpc.activate_rpc()['status'],'warning')

    def test_object_lookup_requires_the_exact_name_and_success(self):
        for output,code,expected in [("'unisoc-ota' @Ab12\n",0,'ab12'),
                                     ("'other-ota' @ab12\n",0,None),
                                     ("'unisoc-ota' @ab12\n",1,None),('',0,None)]:
            with self.subTest(output=output,code=code), patch.object(self.rpc.subprocess,'run',return_value=Mock(returncode=code,stdout=output)):
                self.assertEqual(self.rpc.rpc_object_id([]),expected)
        with patch.object(self.rpc.subprocess,'run',side_effect=self.rpc.subprocess.TimeoutExpired('ubus',3)):
            self.assertIsNone(self.rpc.rpc_object_id([]))

    def test_worker_publishes_complete_after_activation(self):
        events=[]
        result=Mock(returncode=0,stdout='{"status":"complete"}',stderr='')
        with patch.object(sys,'argv',['rpc.py','_worker','apply','']), patch.object(self.rpc.subprocess,'run',return_value=result), patch.object(self.rpc,'activate_rpc',side_effect=lambda:(events.append('reload') or {'status':'ready'})), patch.object(self.rpc,'atomic_json',side_effect=lambda path,data:events.append(data)):
            self.rpc.main()
        self.assertEqual(events[0], 'reload')
        self.assertEqual(events[1]['status'], 'complete')
        self.assertEqual(events[1]['result']['activation'], {'status':'ready'})

    def test_failed_worker_does_not_activate_partial_files(self):
        result=Mock(returncode=1,stdout='{"error":"write failed"}',stderr='')
        with patch.object(sys,'argv',['rpc.py','_worker','apply','']), patch.object(self.rpc.subprocess,'run',return_value=result), patch.object(self.rpc,'activate_rpc') as activate, patch.object(self.rpc,'atomic_json') as publish:
            self.rpc.main()
        activate.assert_not_called()
        self.assertEqual(publish.call_args.args[1]['status'], 'failed')

    def test_completion_waits_for_worker_lock_before_reboot_is_offered(self):
        import fcntl
        with tempfile.TemporaryDirectory() as d:
            self.rpc.STATE=Path(d); self.rpc.JOB=Path(d)/'job.json'
            self.rpc.JOB.write_text('{"status":"complete"}')
            with patch.object(self.rpc,'Engine') as engine:
                engine.return_value.status.return_value={'identity':{'version':'test'}}
                with (Path(d)/'job.lock').open('a') as held:
                    fcntl.flock(held, fcntl.LOCK_EX|fcntl.LOCK_NB)
                    self.assertEqual(self.rpc.dispatch('status')['job']['status'], 'running')
                self.assertEqual(self.rpc.dispatch('status')['job']['status'], 'complete')

    def test_reconciled_completion_retains_the_requested_patch_identity(self):
        with tempfile.TemporaryDirectory() as d:
            self.rpc.STATE=Path(d);self.rpc.JOB=Path(d)/'job.json'
            self.rpc.JOB.write_text(json.dumps({'status':'running','command':'apply','patch_id':'b'*64,'phase':'applying'}))
            with patch.object(self.rpc,'Engine') as engine:
                engine.return_value.status.return_value={'transaction':{'status':'complete','id':'b'*64}}
                result=self.rpc.dispatch('status')['job']
            self.assertEqual((result['status'],result['command'],result['patch_id']),('complete','apply','b'*64))

    def test_reboot_ack_window_retains_job_lock_until_child_exits(self):
        import fcntl
        import subprocess
        import shutil
        with tempfile.TemporaryDirectory() as d:
            self.rpc.STATE=Path(d)
            self.rpc.ROOT=d
            launch=subprocess.Popen
            children=[]
            def harmless(command, **kwargs):
                command=list(command); command[-1]=shutil.which('true')
                # This test verifies inherited locks, not a host-wide flush.
                # Keep production sync intact without syncing every WSL/9p
                # disk from an otherwise harmless unit-test subprocess.
                self.assertIn('os.sync()',command[2])
                command[2]=command[2].replace('os.sync()','None')
                kwargs['stderr']=subprocess.PIPE
                child=launch(command,**kwargs);children.append(child);return child
            try:
                with patch.dict(os.environ,{'UNISOC_OTA_RAM':''}), patch.object(self.rpc.subprocess,'Popen',side_effect=harmless), \
                     patch.object(self.rpc,'arm_next_linux'), patch.object(self.rpc,'Engine') as engine:
                    engine.return_value.identity.return_value={'family':'e5'}
                    self.assertEqual(self.rpc.dispatch('reboot')['status'],'rebooting')
                with (Path(d)/'job.lock').open('a') as held:
                    with self.assertRaises(BlockingIOError): fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    _, stderr = children[0].communicate(timeout=10)
                    self.assertEqual(children[0].returncode,0,stderr.decode())
                    fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:
                for child in children:
                    if child.poll() is None: child.terminate();child.wait()

    def test_reboot_prompt_compares_boot_id_without_clearing_undo_history(self):
        with tempfile.TemporaryDirectory() as d:
            self.rpc.STATE=Path(d); self.rpc.JOB=Path(d)/'job.json'
            with patch.object(self.rpc,'Engine') as engine, patch.object(self.rpc,'boot_id',return_value='boot-now'):
                engine.return_value.status.return_value={'identity':{'version':'r2'},'transaction':{'status':'complete'}}
                for completed, expected in (('boot-now',True),('boot-before',False),(None,None)):
                    record={'status':'complete','result':{}}
                    if completed: record['completed_boot_id']=completed
                    self.rpc.JOB.write_text(json.dumps(record))
                    status=self.rpc.dispatch('status')
                    self.assertIs(status['reboot_required'],expected)
                    self.assertEqual(status['transaction']['status'],'complete')
                    self.assertEqual(json.loads(self.rpc.JOB.read_text()),record)

    def test_failed_boot_selection_cancels_reboot_and_releases_both_locks(self):
        import fcntl
        with tempfile.TemporaryDirectory() as d:
            self.rpc.STATE=Path(d);self.rpc.ROOT=d
            with patch.object(self.rpc,'Engine') as engine, patch.object(self.rpc,'arm_next_linux',side_effect=ValueError('bad boot control')), patch.object(self.rpc.subprocess,'Popen') as launch:
                engine.return_value.identity.return_value={'family':'mu300'}
                with self.assertRaisesRegex(ValueError,'bad boot control'):self.rpc.dispatch('reboot')
                launch.assert_not_called()
            for path in (Path(d)/'job.lock',Path(d)/'run/openwrt-default-boot.lock'):
                with path.open('a') as held:fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)

    def test_discard_cannot_race_a_worker_or_reboot(self):
        import fcntl
        with tempfile.TemporaryDirectory() as d:
            self.rpc.STATE=Path(d)
            with patch.object(self.rpc,'Engine') as engine:
                with (Path(d)/'job.lock').open('a') as held:
                    fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    with self.assertRaises(BlockingIOError):self.rpc.dispatch('discard_recovery')
                engine.return_value.discard_recovery.assert_not_called()
                self.rpc.dispatch('discard_recovery')
                engine.return_value.discard_recovery.assert_called_once()
