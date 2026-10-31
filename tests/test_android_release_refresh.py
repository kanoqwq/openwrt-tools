"""APK-only published ZIP replacement, integrity and interrupted publication."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch
import zipfile

TOP=Path(__file__).resolve().parents[1]
tool=runpy.run_path(str(TOP/'tools/refresh-android-release.py'))
refresh=tool['refresh_release']


def digest(data):return hashlib.sha256(data).hexdigest()


class AndroidRefresh(unittest.TestCase):
    def fixture(self,base,family):
        repo=base/family;release=repo/('out/releases/batch' if family=='e5' else 'release/batch')
        release.mkdir(parents=True);(repo/'tools').mkdir()
        configured=os.environ.get('OPENWRT_E5_SOURCE' if family=='e5' else 'OPENWRT_MU300_SOURCE')
        if not configured:self.skipTest('Set the explicit firmware-source integration path')
        actual=Path(configured)
        if not (actual/'tools/build-release.py').is_file():self.skipTest('Firmware verifier input is unavailable')
        wrapper="import runpy\nverify_archive=runpy.run_path("+repr(str(actual/'tools/build-release.py'))+")[\"verify_archive\"]\n"
        (repo/'tools/build-release.py').write_text(wrapper)
        def metadata(data):return {'protocol':1,'platform':'android-aarch64','debug':False,'signing':{'fixture':True},'files':{'OpenWrt-Tools.apk':digest(data)}}
        apk=base/(family+'-new.apk');apk.write_bytes(b'new signed fixture')
        meta=base/(family+'-new.json');meta.write_text(json.dumps(metadata(apk.read_bytes())))
        files={'OpenWrt-Tools.apk':b'old apk','docs/GUI-BUILD-android.json':json.dumps(metadata(b'old apk')).encode(),
               'OpenWrt-Diff-Studio.exe':b'windows','OpenWrt-Diff-Studio.py':b'python',
               'OpenWrt-Diff-Studio.app/Contents/MacOS/openwrt-installer':b'mac',
               'tools/backend/openwrt-backend.exe':b'backend','tools/macos/backend/openwrt-backend':b'mac-backend'}
        files['diff-tool.json']=json.dumps({'protocol':1,'files':{n:digest(v) for n,v in files.items() if not n.startswith(('OpenWrt-Tools','docs/'))}}).encode()
        if family=='e5':
            files.update({'files/boot.img':b'boot','files/openwrt.ext4.gz':b'rootfs'})
            files['files/SHA256SUMS']=''.join(digest(v)+'  '+n+'\n' for n,v in sorted(files.items())).encode()
        else:
            files.update({'payload/boot-linux.img':b'boot','payload/mu300-openwrt.tar.gz':b'rootfs'})
            files['desktop/manifest.json']=json.dumps({'format':1,'magisk':{},'desktop':{n:digest(v) for n,v in files.items()},
                'magisk_layout':{'boot-linux.img':'payload/boot-linux.img','mu300-openwrt.tar.gz':'payload/mu300-openwrt.tar.gz'}}).encode()
        archive=release/'model.zip'
        with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED) as z:
            for n,v in files.items():z.writestr(n,v)
        run={'state':'verified','source':{'private_version':'unchanged-firmware'},'assets':[{'profile':'fixture','file':'model.zip','sha256':digest(archive.read_bytes()),'bytes':archive.stat().st_size}]}
        (release/'BUILD-RUN.json').write_text(json.dumps(run))
        return repo,release,archive,apk,meta,files

    def test_both_families_change_only_apk_metadata_and_manifest_and_are_idempotent(self):
        for family in ('e5','mu300'):
            with self.subTest(family=family),tempfile.TemporaryDirectory() as d:
                repo,release,archive,apk,meta,files=self.fixture(Path(d),family)
                refresh(repo,release,family,apk,meta)
                allowed={'OpenWrt-Tools.apk','docs/GUI-BUILD-android.json','files/SHA256SUMS','desktop/manifest.json'}
                with zipfile.ZipFile(archive) as z:
                    self.assertEqual(set(z.namelist()),set(files))
                    for n,data in files.items():
                        if n not in allowed:self.assertEqual(z.read(n),data,n)
                    self.assertEqual(z.read('OpenWrt-Tools.apk'),apk.read_bytes())
                run=json.loads((release/'BUILD-RUN.json').read_text())
                self.assertEqual(run['source'],{'private_version':'unchanged-firmware'})
                self.assertEqual(run['assets'][0]['sha256'],digest(archive.read_bytes()))
                before=archive.read_bytes();refresh(repo,release,family,apk,meta)
                self.assertEqual(archive.read_bytes(),before)
                self.assertFalse(list(release.glob('*.tmp')))

    def test_lost_host_ack_after_archive_replace_settles_metadata_on_resume(self):
        with tempfile.TemporaryDirectory() as d:
            repo,release,archive,apk,meta,_=self.fixture(Path(d),'e5')
            original=refresh.__globals__['record']
            def fail(path,data):
                if path.name=='BUILD-RUN.json' and 'android_refresh' in data:raise OSError('simulated lost acknowledgement')
                return original(path,data)
            with patch.dict(refresh.__globals__,record=fail),self.assertRaisesRegex(OSError,'acknowledgement'):
                refresh(repo,release,'e5',apk,meta)
            replaced=archive.read_bytes()
            refresh(repo,release,'e5',apk,meta)
            self.assertEqual(archive.read_bytes(),replaced)
            self.assertEqual(json.loads((release/'BUILD-RUN.json').read_text())['assets'][0]['sha256'],digest(replaced))

    def test_failed_atomic_switch_keeps_old_zip_and_prepared_copy_is_checked(self):
        with tempfile.TemporaryDirectory() as d:
            repo,release,archive,apk,meta,_=self.fixture(Path(d),'mu300');before=archive.read_bytes()
            original=refresh.__globals__['os'].replace
            def fail(source,target):
                if str(source).endswith('.android-refresh.tmp'):raise OSError('simulated replace failure')
                return original(source,target)
            with patch.object(refresh.__globals__['os'],'replace',side_effect=fail),self.assertRaisesRegex(OSError,'replace failure'):
                refresh(repo,release,'mu300',apk,meta)
            self.assertEqual(archive.read_bytes(),before)
            temporary=archive.with_name(archive.name+'.android-refresh.tmp');prepared=temporary.read_bytes();temporary.write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError,'Prepared APK refresh'):refresh(repo,release,'mu300',apk,meta)
            self.assertEqual(archive.read_bytes(),before)
            temporary.write_bytes(prepared);refresh(repo,release,'mu300',apk,meta)

    def test_bad_source_hash_or_another_apk_cannot_overwrite_release(self):
        with tempfile.TemporaryDirectory() as d:
            repo,release,archive,apk,meta,_=self.fixture(Path(d),'e5');before=archive.read_bytes()
            archive.write_bytes(before+b'changed')
            with self.assertRaisesRegex(ValueError,'Input archive differs'):refresh(repo,release,'e5',apk,meta)
            self.assertEqual(archive.read_bytes(),before+b'changed')
            archive.write_bytes(before);refresh(repo,release,'e5',apk,meta)
            apk.write_bytes(b'different');meta.write_text(json.dumps({'files':{'OpenWrt-Tools.apk':digest(apk.read_bytes())}}))
            with self.assertRaisesRegex(ValueError,'another artifact'):refresh(repo,release,'e5',apk,meta)


if __name__=='__main__':unittest.main()
