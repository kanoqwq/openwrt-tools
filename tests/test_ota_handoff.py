"""Exercise the real shell ID handoff and RPC admission against existing patches."""
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_ota_desktop as fixture
from backend.ota_engine import Patch
from backend import ota_desktop


class Handoff(unittest.TestCase):
    def setUp(self):
        fixture.DesktopTests.setUp(self)

    def staged(self,nonce):
        with patch.object(ota_desktop.secrets,'token_hex',return_value=nonce):
            session=ota_desktop.remote_prepare(self.shell,self.archive)
        script=self.shell.files[session['runtime']+'/bootstrap.sh']
        return session,script

    def test_existing_archive_uses_controller_entry_without_modifying_patch_or_engine(self):
        before=self.archive.read_bytes()
        session,script=self.staged('a'*64)
        with Patch(self.archive) as original:
            old=original.archive.read('blobs/'+original.manifest['bootstrap']['sha256'])
            self.assertNotEqual(script,old)
            self.assertTrue(script.startswith(b'#!/bin/sh\n'))
            self.assertEqual(session['path'],'/etc/unisoc-ota/upload-'+original.id+'.ota')
            self.assertNotEqual(session['runtime'],'/tmp/unisoc-ota-'+original.id)
        self.assertEqual(self.archive.read_bytes(),before)
        self.assertEqual(self.shell.files[session['path']],before)
        self.assertIn(hashlib.sha256(script).hexdigest(),self.shell.execute(self.shell,
            session['runtime']+'/busybox sha256sum '+session['runtime']+'/bootstrap.sh'))

    @unittest.skipUnless(os.name=='posix','actual POSIX shell and rpcd locks')
    def test_actual_shell_handoff_reproduces_old_failure_and_passes_actual_rpc_admission(self):
        session,script=self.staged('a'*64)
        with Patch(self.archive) as original:expected=original.id
        self.assertNotEqual(expected,'a'*64)
        prefix=script.decode().split('bb="$home/busybox"',1)[0]
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory);upload=state/('upload-'+expected+'.ota')
            shutil.copyfile(self.archive,upload)
            original_prefix='set -eu; patch=$1; command=$2; home=$3; id=${home#/tmp/unisoc-ota-}; '
            def evaluate(source,name=upload,command='start',review=None):
                return subprocess.run(['sh','-c',source+'printf "%s" "$id"','handoff-test',str(name),command,session['runtime']]+([review] if review is not None else []),
                                      capture_output=True,text=True,timeout=5)
            old=evaluate(original_prefix);current=evaluate(prefix)
            self.assertEqual(old.stdout,'a'*64)
            self.assertEqual(current.returncode,0,current.stderr)
            self.assertEqual(current.stdout,expected)
            runtime=Path(__file__).resolve().parents[1]/'runtime/luci-app-unisoc-ota/root/usr/lib/unisoc-ota/rpc.py'
            spec=importlib.util.spec_from_file_location('ota_handoff_rpc',runtime)
            rpc=importlib.util.module_from_spec(spec);spec.loader.exec_module(rpc);rpc.STATE=state
            with self.assertRaisesRegex(ValueError,'Upload differs'):rpc.stage_upload(upload,old.stdout)
            self.assertTrue(upload.is_file())
            frozen=rpc.stage_upload(upload,current.stdout)
            with Patch(frozen) as verified:self.assertEqual(verified.id,expected)
            self.assertFalse(upload.exists())
            self.assertFalse(list(state.glob('candidate-*.ota')))
            self.assertNotEqual(evaluate(prefix,name=state/'upload.ota').returncode,0)
            self.assertEqual(evaluate(prefix,name=state/'upload.ota',review=expected).stdout,expected)
            self.assertEqual(evaluate(prefix,name=state/'upload.ota',command='inspect').returncode,0)
            self.assertEqual(evaluate(prefix,name=frozen).stdout,expected)

    def test_same_patch_gets_distinct_sessions_and_same_reviewed_identity(self):
        first,first_script=self.staged('a'*64)
        second,second_script=self.staged('b'*64)
        self.assertNotEqual(first['runtime'],second['runtime'])
        self.assertEqual(first['path'],second['path'])
        self.assertEqual(first_script,second_script)

    def test_apply_passes_the_actual_review_id_as_an_independent_argument(self):
        result=self.app.dispatch('ota_probe',self.params)
        self.app.dispatch('ota_apply',dict(self.params,token=result['token']))
        import shlex
        args=shlex.split(self.shell.commands[-1])
        self.assertEqual(args[4],'start')
        self.assertEqual(args[6],self.shell.patch_id)
        self.assertNotEqual(args[5].removeprefix('/tmp/unisoc-ota-'),args[6])
