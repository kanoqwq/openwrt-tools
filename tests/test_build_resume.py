"""Batch completion reuses immutable archives; fake parent builder, no hardware."""
from contextlib import nullcontext
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import zipfile

spec=importlib.util.spec_from_file_location('resume_builder',Path(__file__).resolve().parents[1]/'tools/build-release.py')
tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)


class Resume(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);self.repo=root/'firmware';self.pending=self.repo/'out/releases/.building-old-task'
        self.pending.mkdir(parents=True);self.workspace=root/'workspace';self.workspace.mkdir()
        self.args=SimpleNamespace(repository=self.repo,pending=self.pending,workspace=self.workspace,
                                  profiles=['e5','changxing60'],label='resume')
        folder=self.repo/'out/build-logs/old-task';folder.mkdir(parents=True)
        self.state=folder/'status.json';self.state.write_text(json.dumps(dict(state='failed',pending_output=str(self.pending),steps=[dict(stage='e5-package',state='complete')])))
        self.rows={}
        for name in self.args.profiles:
            kernel=root/name;kernel.mkdir();(kernel/'container-support.json').write_text(json.dumps(dict(model=name)))
            self.rows[name]={'kernel':kernel}
        self.source=dict(source_commit='a'*40,source_fingerprint='same',source_dirty=False,private_version='new-version',
                         profile='e5',openwrt='25.12.5',kernel_release='6.18.54')
        self.archive('e5');self.original=(self.pending/'e5-release.zip').read_bytes()
        self.original_time=(self.pending/'e5-release.zip').stat().st_mtime_ns
        self.builds=[];self.fail=False;owner=self
        class Task:
            def __init__(self,*_):self.data={'task':'resume-task'}
            def save(self,**kwargs):self.data.update(kwargs)
            def run(self,label,command):
                owner.builds.append(label)
                if owner.fail:raise OSError('download interrupted')
                owner.assertEqual(command[command.index('--release-version')+1],'old-version')
                owner.archive(label.removesuffix('-package'))
        self.api=dict(OWNER='e5-linux-build-release-v1',layout=lambda *_:({},self.rows),workspace_lock=lambda *_:nullcontext(),
                      Task=Task,input_check=Mock(),stage_path=lambda _,p:self.workspace/p,
                      write_json=lambda p,r:p.write_text(json.dumps(r)),verify_archive=Mock(),
                      digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest(),clean_stage=Mock())
        self.loader=patch.object(tool.runpy,'run_path',side_effect=lambda p: {'identify':lambda *_:dict(self.source)} if str(p).endswith('release-identity.py') else self.api)
        self.loader.start();self.addCleanup(self.loader.stop)

    def archive(self,profile):
        path=self.pending/(profile+'-release.zip')
        record=dict(profile=profile,source_commit='a'*40,source_fingerprint='same',source_dirty=self.source['source_dirty'],
                    private_version='old-version',package_name=profile+'-release')
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('files/RELEASE.json',json.dumps(record))
            z.writestr('files/BUILD.json',json.dumps(dict(kernel_manifest=dict(model=profile))))
        Path(str(path)+'.sha256').write_text(hashlib.sha256(path.read_bytes()).hexdigest()+'  '+path.name)

    def test_only_missing_model_builds_and_existing_bytes_and_mtime_are_preserved(self):
        tool.resume_firmware(self.args)
        final=self.pending.with_name('old-task')
        self.assertEqual(self.builds,['changxing60-package'])
        self.assertEqual((final/'e5-release.zip').read_bytes(),self.original)
        self.assertEqual((final/'e5-release.zip').stat().st_mtime_ns,self.original_time)
        receipt=json.loads((final/'BUILD-RUN.json').read_text())
        self.assertEqual(len(receipt['assets']),2)
        self.assertEqual(receipt['source']['image_version'],'e5-old-version')
        self.assertEqual(receipt['source']['package_name'],'e5-openwrt-25.12.5-old-version-k6.18.54')

    def test_modified_firmware_stops_before_building_or_replacing_existing_archive(self):
        self.source['source_fingerprint']='changed'
        with self.assertRaisesRegex(ValueError,'source changed'):tool.resume_firmware(self.args)
        self.assertFalse(self.builds);self.assertEqual((self.pending/'e5-release.zip').read_bytes(),self.original)

    def test_unchanged_dirty_snapshot_resumes_without_replacing_existing_archive(self):
        self.source['source_dirty']=True
        self.archive('e5')
        original=(self.pending/'e5-release.zip').read_bytes()
        tool.resume_firmware(self.args)
        final=self.pending.with_name('old-task')
        self.assertEqual(self.builds,['changxing60-package'])
        self.assertEqual((final/'e5-release.zip').read_bytes(),original)
        self.assertIs(json.loads((final/'BUILD-RUN.json').read_text())['source']['source_dirty'],True)

    def test_dirty_flag_change_rejects_even_a_matching_fingerprint(self):
        self.source['source_dirty']=True
        with self.assertRaisesRegex(ValueError,'source changed'):tool.resume_firmware(self.args)
        self.assertFalse(self.builds)

    def test_modified_dirty_snapshot_is_rejected(self):
        self.source['source_dirty']=True
        self.archive('e5')
        self.source['source_fingerprint']='changed'
        with self.assertRaisesRegex(ValueError,'source changed'):tool.resume_firmware(self.args)
        self.assertFalse(self.builds)

    def test_verified_archive_survives_a_lost_completion_checkpoint(self):
        self.archive('changxing60')
        tool.resume_firmware(self.args)
        self.assertEqual(self.builds,[])

    def test_checksum_damage_stops_before_missing_models_build(self):
        path=self.pending/'e5-release.zip';path.write_bytes(self.original+b'extra')
        with self.assertRaisesRegex(ValueError,'checksum'):tool.resume_firmware(self.args)
        self.assertFalse(self.builds)

    def test_running_batches_cannot_be_adopted(self):
        r=json.loads(self.state.read_text());r['state']='running';self.state.write_text(json.dumps(r))
        with self.assertRaisesRegex(ValueError,'stopped'):tool.resume_firmware(self.args)
        self.assertFalse(self.builds)

    def test_new_failure_keeps_original_archives_and_pending_output(self):
        self.fail=True
        with self.assertRaisesRegex(OSError,'download'):tool.resume_firmware(self.args)
        self.assertEqual((self.pending/'e5-release.zip').read_bytes(),self.original)
        self.assertFalse(self.pending.with_name('old-task').exists())


if __name__=='__main__':unittest.main()
