"""Verify the signed mobile host is carried once at the release root."""
import hashlib
import io
import json
import runpy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import package_android as tool


class AndroidPackaging(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.artifact=self.root/'artifact';self.artifact.mkdir()
        self.raw=b'signed ARM64 fixture';(self.artifact/tool.APK).write_bytes(self.raw)
        self.build={'protocol':1,'platform':'android-aarch64','debug':False,'signing':{'alias':'fixture'},
                    'source':{'shared':'current','mobile':'current'},'validation':{'abi':'arm64-v8a'},
                    'files':{tool.APK:hashlib.sha256(self.raw).hexdigest()}}
        self.save()
        original=tool.runpy.run_path
        def inputs(path):
            if str(path).endswith('build-android.py'):return {'sources':lambda:self.build['source']}
            if str(path).endswith('mobile/apk.py') or str(path).endswith(r'mobile\apk.py'):return {'validate':lambda *_:self.build['validation']}
            return original(path)
        mocking=patch.object(tool.runpy,'run_path',side_effect=inputs);mocking.start();self.addCleanup(mocking.stop)

    def save(self):
        (self.artifact/'OpenWrt-Tools-build.json').write_text(json.dumps(self.build))

    def test_attachment_and_zip_inventory_keep_one_apk_at_the_root(self):
        package=self.root/'release';package.mkdir()
        tool.attach(package,self.artifact)
        self.assertEqual((package/tool.APK).read_bytes(),self.raw)
        self.assertEqual(len(list(package.rglob('*.apk'))),1)
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as archive:
            for path in package.rglob('*'):
                if path.is_file():archive.write(path,path.relative_to(package).as_posix())
        with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:tool.verify_archive(archive)

    def test_debug_wrong_abi_unsigned_or_corrupt_apks_are_not_published(self):
        for field,value in [('debug',True),('platform','android-x86_64'),('signing',{})]:
            old=self.build[field];self.build[field]=value;self.save()
            with self.subTest(field=field),self.assertRaises(ValueError):tool.artifact(self.artifact)
            self.build[field]=old
        self.save();(self.artifact/tool.APK).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'checksum'):tool.artifact(self.artifact)

    def test_stale_source_and_nested_duplicate_apks_are_refused(self):
        with patch.object(tool.runpy,'run_path',return_value={'sources':lambda:{'shared':'old'}}):
            with self.assertRaisesRegex(ValueError,'stale'):tool.artifact(self.artifact)
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as archive:
            archive.writestr(tool.APK,self.raw);archive.writestr('extras/'+tool.APK,self.raw)
            archive.writestr(tool.METADATA,json.dumps(self.build))
        with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive,self.assertRaisesRegex(ValueError,'one'):
            tool.verify_archive(archive)


class PortableAndroidFingerprint(unittest.TestCase):
    def test_checkout_line_endings_and_generated_rules_do_not_mask_real_source_changes(self):
        source=runpy.run_path(str(ROOT/'build-android.py'))['sources']
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            names=('build-android.py','release-index.py','backend/main.py','mobile/App.kt',
                   'mobile/gen/gradlew.bat','mobile/gen/proguard-rules.pro',
                   'ui/package.json','ui/package-lock.json','ui/vite.config.ts','ui/index.html',
                   'ui/diff.html','ui/ota.html','ui/release-index.html')
            for name in names:
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'first\r\nsecond\r\n')
            with patch.dict(source.__globals__,HERE=root,MOBILE=root/'mobile'),patch.object(runpy,'run_path',return_value={'sources':lambda:{}}):
                first=source()
                for name in names:(root/name).write_bytes((root/name).read_bytes().replace(b'\r\n',b'\n'))
                (root/'mobile/gen/proguard-tauri.pro').write_text('generated rules')
                (root/'mobile/gen/.gitignore').write_text('local metadata')
                (root/'mobile/gen/.editorconfig').write_text('local metadata')
                self.assertEqual(first,source())
                (root/'mobile/App.kt').write_text('real production change')
                self.assertNotEqual(first,source())


if __name__=='__main__':unittest.main()
