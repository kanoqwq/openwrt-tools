"""Generate and apply real defaults-only patches against temporary filesystems."""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_ota import mu_release, analyze, Engine, Patch


def release(root, version, bluetooth=False, parent_only=False):
    files={'usr/lib/test':b'unchanged'}
    if bluetooth:files.update({'etc/bluetooth/input.conf':b'[General]\n',
                               'etc/unisoc-bluetooth/platform':b'mu300-pcie\n',
                               'etc/unisoc-bluetooth/devices/lab/settings.json':b'{}\n'})
    mu_release(root,version,files)
    packed=root/'payload/mu300-openwrt.tar.gz'
    with tarfile.open(packed) as archive:
        entries=[(node,archive.extractfile(node).read() if node.isfile() else None) for node in archive]
    with tarfile.open(packed,'w:gz') as archive:
        for node,data in entries:archive.addfile(node,io.BytesIO(data) if data is not None else None)
        if bluetooth or parent_only:
            node=tarfile.TarInfo('etc/bluetooth');node.type=tarfile.DIRTYPE;node.mode=0o700;archive.addfile(node)
        if bluetooth:
            for name in ('etc/unisoc-bluetooth','etc/unisoc-bluetooth/devices','etc/unisoc-bluetooth/devices/lab'):
                node=tarfile.TarInfo(name);node.type=tarfile.DIRTYPE;node.mode=0o700;archive.addfile(node)
    manifest=root/'desktop/manifest.json';value=json.loads(manifest.read_text())
    value['magisk']['payload/mu300-openwrt.tar.gz']=hashlib.sha256(packed.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(value))


@unittest.skipUnless(os.name=='posix' and os.geteuid()==0,'POSIX root required for actual OTA ownership changes')
class ConfigDirectories(unittest.TestCase):
    def fixture(self, directory, old_parent=False, profile='mu300'):
        root=Path(directory);live=root/'live'
        for name in ('etc/mu300','etc/config','usr/lib'):(live/name).mkdir(parents=True,exist_ok=True)
        (live/'etc/mu300/release.json').write_text(json.dumps(dict(profile=profile,image_version='mu300-r1',kernel_release='7.2.8')))
        (live/'etc/config/wireless').write_bytes(b'user password and channel')
        (live/'usr/lib/test').write_bytes(b'unchanged')
        release(root/'old','mu300-r1',parent_only=old_parent)
        release(root/'new','mu300-r2',bluetooth=True)
        for name in ('old','new'):
            meta=root/name/'desktop/release.json';value=json.loads(meta.read_text())
            value['profile']=profile;meta.write_text(json.dumps(value))
        output=root/'patch.ota';result=analyze(root/'old',root/'new',output)
        self.assertTrue(result['can_export'],result['blockers'])
        return live,output

    def test_first_install_and_missing_baseline_parent(self):
        for old_parent in (False,True):
            with self.subTest(old_parent=old_parent),tempfile.TemporaryDirectory() as directory:
                live,output=self.fixture(directory,old_parent)
                with Patch(output) as patch:
                    for name in ('etc/bluetooth','etc/unisoc-bluetooth','etc/unisoc-bluetooth/devices/lab'):
                        parent=next(op for op in patch.manifest['operations'] if op['path']==name)
                        self.assertEqual(parent['merge'],'default')
                    Engine(live).apply(patch)
                self.assertEqual((live/'etc/bluetooth/input.conf').read_bytes(),b'[General]\n')
                self.assertEqual((live/'etc/unisoc-bluetooth/platform').read_bytes(),b'mu300-pcie\n')
                self.assertEqual((live/'etc/unisoc-bluetooth/devices/lab/settings.json').read_bytes(),b'{}\n')
                self.assertEqual(stat.S_IMODE((live/'etc/bluetooth').stat().st_mode),0o700)
                self.assertEqual((live/'etc/config/wireless').read_bytes(),b'user password and channel')

    def test_existing_config_permissions_and_pairing_data_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            live,output=self.fixture(directory)
            folder=live/'etc/bluetooth';folder.mkdir(mode=0o750)
            (folder/'input.conf').write_bytes(b'user config');(folder/'pairing').write_bytes(b'user pairing')
            gateway=live/'etc/unisoc-bluetooth';gateway.mkdir(mode=0o700)
            (gateway/'platform').write_bytes(b'u30pro-aic-uart\n')
            with Patch(output) as patch:Engine(live).apply(patch)
            self.assertEqual((folder/'input.conf').read_bytes(),b'user config')
            self.assertEqual((folder/'pairing').read_bytes(),b'user pairing')
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode),0o750)
            self.assertEqual((gateway/'platform').read_bytes(),b'u30pro-aic-uart\n')
            self.assertEqual(stat.S_IMODE(gateway.stat().st_mode),0o700)

    def test_pro_profiles_receive_all_new_default_directories(self):
        for profile in ('u30pro','f50pro'):
            with self.subTest(profile=profile),tempfile.TemporaryDirectory() as directory:
                live,output=self.fixture(directory,profile=profile)
                with Patch(output) as patch:Engine(live).apply(patch)
                self.assertEqual((live/'etc/bluetooth/input.conf').read_bytes(),b'[General]\n')
                self.assertEqual((live/'etc/unisoc-bluetooth/platform').read_bytes(),b'mu300-pcie\n')

    def test_failure_rolls_back_new_defaults_and_empty_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            live,output=self.fixture(directory)
            def fail(*_):
                if (live/'etc/unisoc-bluetooth/devices/lab/settings.json').exists():raise RuntimeError('fixture interruption')
            with Patch(output) as patch:
                with self.assertRaisesRegex(RuntimeError,'fixture interruption'):Engine(live).apply(patch,fail)
            self.assertFalse((live/'etc/bluetooth').exists())
            self.assertFalse((live/'etc/unisoc-bluetooth').exists())
            self.assertEqual((live/'etc/config/wireless').read_bytes(),b'user password and channel')

    def test_symlink_parent_is_not_followed_or_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            live,output=self.fixture(directory);outside=Path(directory)/'outside';outside.mkdir()
            (live/'etc/bluetooth').symlink_to(outside,target_is_directory=True)
            with Patch(output) as patch:
                with self.assertRaises(ValueError):Engine(live).apply(patch)
            self.assertTrue((live/'etc/bluetooth').is_symlink())
            self.assertEqual(list(outside.iterdir()),[])


if __name__=='__main__':unittest.main()
