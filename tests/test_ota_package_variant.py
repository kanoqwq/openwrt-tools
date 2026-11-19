"""A firmware package variant can change without resetting DHCP or user APKs."""
import json
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

from test_ota import mu_release, analyze, Patch, Engine
from backend.ota_engine import canonical, package_records, package_replacements, shadowed_dnsmasq


def record(name, version='1', provides=''):
    return (f'P:{name}\nV:{version}\nA:aarch64\nD:libc\np:{provides}\n'
            f'F:usr/sbin\nR:dnsmasq\nF:lib/apk/packages\nR:{name}.conffiles\n')


def release(path, version, full=False, revision='1', extra=None):
    name = 'dnsmasq-full' if full else 'dnsmasq'
    files = {
        'usr/sbin/dnsmasq': (name+'-'+revision).encode(),
        'usr/bin/python3': b'fixture interpreter',
        'usr/lib/unisoc-ota/engine.py': b'embedded updater',
        'lib/apk/db/installed': record(name, revision, 'dnsmasq=1' if full else '').encode(),
        'lib/apk/packages/'+name+'.conffiles': b'/etc/config/dhcp\n/etc/dnsmasq.conf\n',
        'etc/apk/world': (name+'\n').encode(),
        'etc/config/dhcp': b'factory DHCP settings',
        'etc/dnsmasq.conf': b'factory DNS settings',
    }
    files.update(extra or {})
    mu_release(path, version, files)
    (path/'payload/busybox').write_bytes(b'fixture helper')
    return files


@unittest.skipUnless(sys.platform == 'linux', 'POSIX OTA filesystem semantics')
class PackageVariant(unittest.TestCase):
    def test_existing_duplicate_is_repaired_even_without_database_changes_in_patch(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            old=release(base/'old', 'mu300-r1', True)
            release(base/'new', 'mu300-r2', True, extra={'usr/lib/wifi-update':b'new Wi-Fi component'})
            live=base/'live'
            for name,raw in old.items():
                path=live/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
            identity=live/'etc/mu300/release.json';identity.parent.mkdir(parents=True)
            identity.write_bytes((base/'old/desktop/release.json').read_bytes())
            db=live/'lib/apk/db/installed'
            db.write_bytes(db.read_bytes()+b'\n'+record('dnsmasq').encode())
            legacy=live/'lib/apk/packages/dnsmasq.conffiles';legacy.write_bytes(b'legacy metadata')
            world=live/'etc/apk/world';world.write_text('dnsmasq\ndnsmasq-full@user-feed\nuser-plugin\n')
            config=live/'etc/config/dhcp';config.write_bytes(b'custom DHCP leases and resolver')
            output=base/'update.ota';result=analyze(base/'old',base/'new',output)
            self.assertNotIn('lib/apk/db/installed',{op['path'] for op in result['manifest']['operations']})
            with Patch(output) as patch:Engine(live).apply(patch)
            self.assertNotIn('dnsmasq',package_records(db.read_bytes()))
            self.assertIn('dnsmasq-full',package_records(db.read_bytes()))
            self.assertEqual(world.read_text(),'dnsmasq-full@user-feed\nuser-plugin\n')
            self.assertFalse(legacy.exists())
            self.assertEqual((live/'usr/sbin/dnsmasq').read_bytes(),b'dnsmasq-full-1')
            self.assertEqual(config.read_bytes(),b'custom DHCP leases and resolver')

    def test_duplicate_user_full_keeps_its_program_and_full_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);old=release(base/'old','mu300-r1')
            release(base/'new','mu300-r2',True)
            live=base/'live'
            for name,raw in old.items():
                path=live/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
            identity=live/'etc/mu300/release.json';identity.parent.mkdir(parents=True)
            identity.write_bytes((base/'old/desktop/release.json').read_bytes())
            db=live/'lib/apk/db/installed'
            full=record('dnsmasq-full','9','dnsmasq=9')
            db.write_bytes(db.read_bytes()+b'\n'+full.encode())
            (live/'usr/sbin/dnsmasq').write_bytes(b'user full program')
            metadata=live/'lib/apk/packages/dnsmasq-full.conffiles';metadata.write_bytes(b'user full metadata')
            output=base/'update.ota';analyze(base/'old',base/'new',output)
            with Patch(output) as patch:Engine(live).apply(patch)
            packages=package_records(db.read_bytes())
            self.assertEqual(set(packages),{'dnsmasq-full'})
            self.assertIn('V:9',packages['dnsmasq-full'])
            self.assertEqual((live/'usr/sbin/dnsmasq').read_bytes(),b'user full program')
            self.assertEqual(metadata.read_bytes(),b'user full metadata')
            self.assertEqual((live/'etc/apk/world').read_text(),'dnsmasq-full\n')

    def test_shadowed_provider_requires_declared_alias_and_complete_payload_ownership(self):
        basic=record('dnsmasq')+'F:lib/upgrade/keep.d\nR:dnsmasq\n'
        full=record('dnsmasq-full',provides='dnsmasq=2')+'F:lib/upgrade/keep.d\nR:dnsmasq-full\n'
        self.assertEqual(shadowed_dnsmasq({'dnsmasq':basic,'dnsmasq-full':full}),{'dnsmasq'})
        self.assertEqual(shadowed_dnsmasq({'dnsmasq':basic,'dnsmasq-full':full},{'dnsmasq':basic}),set())
        self.assertEqual(shadowed_dnsmasq({'dnsmasq':basic,'dnsmasq-full':record('dnsmasq-full')}),set())
        self.assertEqual(shadowed_dnsmasq({'dnsmasq':basic+'F:usr/bin\nR:user-command\n','dnsmasq-full':full}),set())

    def test_failed_metadata_repair_restores_records_world_and_keeps_dns_available(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);old=release(base/'old','mu300-r1',True)
            release(base/'new','mu300-r2',True,extra={'usr/lib/wifi-update':b'changed'})
            live=base/'live'
            for name,raw in old.items():
                path=live/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
            identity=live/'etc/mu300/release.json';identity.parent.mkdir(parents=True)
            identity.write_bytes((base/'old/desktop/release.json').read_bytes())
            db=live/'lib/apk/db/installed';db.write_bytes(db.read_bytes()+b'\n'+record('dnsmasq').encode())
            world=live/'etc/apk/world';world.write_bytes(b'dnsmasq\ndnsmasq-full\n')
            before_db,before_world=db.read_bytes(),world.read_bytes()
            output=base/'update.ota';analyze(base/'old',base/'new',output)
            def interrupt(done,total,name):
                if name=='lib/apk/db/installed':raise RuntimeError('simulated update interruption')
            with Patch(output) as patch, self.assertRaisesRegex(RuntimeError,'simulated update interruption'):
                Engine(live).apply(patch,progress=interrupt)
            self.assertEqual(db.read_bytes(),before_db)
            self.assertEqual(world.read_bytes(),before_world)
            self.assertEqual((live/'usr/sbin/dnsmasq').read_bytes(),b'dnsmasq-full-1')
            self.assertEqual((live/'etc/config/dhcp').read_bytes(),old['etc/config/dhcp'])

    def test_runtime_baseline_rejects_duplicate_wrong_target_and_nonfile(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            release(base/'old', 'mu300-r1')
            release(base/'new', 'mu300-r2', True)
            output=base/'patch.ota'; analyze(base/'old', base/'new', output)
            with zipfile.ZipFile(output) as archive:
                contents={name:archive.read(name) for name in archive.namelist()}
            for case in ('duplicate', 'target', 'nonfile'):
                payload=dict(contents)
                manifest=json.loads(payload['manifest.json'])
                desc=manifest['runtime']['ota/release-index.json']
                index=json.loads(payload['blobs/'+desc['sha256']])
                if case=='duplicate': manifest['release_index']=index
                elif case=='nonfile':
                    manifest['runtime']['ota/release-index.json']={'kind':'symlink','mode':0o777,'uid':0,'gid':0,'target':'other'}
                    # Keep no unreferenced blob, so the reserved-type check is exercised.
                    del payload['blobs/'+desc['sha256']]
                else:
                    index['release']['meta']['image_version']='mu300-wrong'
                    data=canonical(index);digest=hashlib.sha256(data).hexdigest()
                    del payload['blobs/'+desc['sha256']]
                    manifest['runtime']['ota/release-index.json']=dict(desc, size=len(data),sha256=digest)
                    payload['blobs/'+digest]=data
                payload['manifest.json']=canonical(manifest)
                bad=base/(case+'.ota')
                with zipfile.ZipFile(bad,'w') as archive:
                    for name,data in payload.items(): archive.writestr(name,data)
                with self.subTest(case=case), self.assertRaises(ValueError): Patch(bad)

    def test_existing_user_full_variant_keeps_its_binary_database_and_config(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            old=release(base/'old', 'mu300-r1')
            release(base/'new', 'mu300-r2', True)
            live=base/'live'
            for name,data in old.items():
                path=live/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
            (live/'etc/mu300').mkdir()
            (live/'etc/mu300/release.json').write_bytes((base/'old/desktop/release.json').read_bytes())
            installed=record('dnsmasq-full', '9', 'dnsmasq=9').encode()
            (live/'lib/apk/db/installed').write_bytes(installed)
            (live/'usr/sbin/dnsmasq').write_bytes(b'user-installed full binary')
            metadata=live/'lib/apk/packages/dnsmasq-full.conffiles'
            metadata.write_bytes(b'user full metadata')
            (live/'lib/apk/packages/dnsmasq.conffiles').unlink()
            config=live/'etc/config/dhcp'
            config.write_bytes(b'OpenClash managed resolver and DHCP')
            (live/'etc/apk/world').write_text('dnsmasq-full\nopenclash\n')
            output=base/'patch.ota'; analyze(base/'old', base/'new', output)
            with Patch(output) as patch: Engine(live).apply(patch)
            self.assertEqual((live/'usr/sbin/dnsmasq').read_bytes(), b'user-installed full binary')
            self.assertIn('V:9', package_records((live/'lib/apk/db/installed').read_bytes())['dnsmasq-full'])
            self.assertEqual(metadata.read_bytes(), b'user full metadata')
            self.assertEqual(config.read_bytes(), b'OpenClash managed resolver and DHCP')

    def test_replacement_and_subsequent_update_preserve_config_and_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            old = release(base/'old', 'mu300-r1')
            release(base/'new', 'mu300-r2', True)
            release(base/'next', 'mu300-r3', True, '2')
            live = base/'live'
            for name, data in old.items():
                p = live/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
            meta = live/'etc/mu300/release.json'; meta.parent.mkdir(parents=True)
            meta.write_bytes((base/'old/desktop/release.json').read_bytes())
            preserved = {'etc/config/dhcp': b'user DNS with static leases',
                         'etc/dnsmasq.conf': b'user overrides',
                         'etc/dnsmasq.d/user.conf': b'address=/lan.example/192.168.9.1',
                         'etc/shadow': b'user password'}
            for name, data in preserved.items():
                p=live/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
            db=live/'lib/apk/db/installed'
            db.write_bytes(db.read_bytes()+b'\nP:user-plugin\nV:9\n\n')
            (live/'etc/apk/world').write_text('dnsmasq\nuser-plugin\n')
            engine = Engine(live)
            for number, (a, b) in enumerate((('old','new'), ('new','next'))):
                output = base/('patch%d.ota' % number)
                result = analyze(base/a, base/b, output)
                self.assertTrue(result['can_export'], result['blockers'])
                with Patch(output) as patch:
                    self.assertIn('ota/apk/dnsmasq-full.conffiles', patch.manifest['runtime'])
                    engine.apply(patch)
                packages = package_records(db.read_bytes())
                self.assertNotIn('dnsmasq', packages)
                self.assertIn('dnsmasq-full', packages)
                self.assertIn('user-plugin', packages)
                self.assertEqual((live/'usr/sbin/dnsmasq').read_bytes(), b'dnsmasq-full-'+str(number+1).encode())
                self.assertFalse((live/'lib/apk/packages/dnsmasq.conffiles').exists())
                self.assertTrue((live/'lib/apk/packages/dnsmasq-full.conffiles').is_file())
                self.assertEqual((live/'etc/apk/world').read_text(), 'user-plugin\ndnsmasq-full\n')
                for name, data in preserved.items(): self.assertEqual((live/name).read_bytes(), data)
            # Rollback restores the preceding full build and its package records.
            import subprocess
            script=Path(__file__).resolve().parents[1]/'runtime/luci-app-unisoc-ota/root/usr/lib/unisoc-ota/engine.py'
            subprocess.run([sys.executable, str(script), 'rollback'],
                           env=dict(os.environ, UNISOC_OTA_ROOT=str(live)), check=True, capture_output=True)
            self.assertEqual((live/'usr/sbin/dnsmasq').read_bytes(), b'dnsmasq-full-1')
            self.assertIn('V:1', package_records(db.read_bytes())['dnsmasq-full'])

    def test_only_declared_overlapping_base_variants_are_replaced(self):
        installed={'dnsmasq':record('dnsmasq'), 'user-plugin':record('user-plugin')}
        target={'dnsmasq-full':record('dnsmasq-full', provides='dnsmasq=1 user-plugin=1')}
        self.assertEqual(package_replacements(installed, target, {'dnsmasq'}), {'dnsmasq'})
        self.assertEqual(package_replacements(installed, target, set()), set())
        self.assertEqual(package_replacements(installed, {'other':record('other')}, {'dnsmasq'}), set())
        target={'virtual':record('virtual', provides='dnsmasq=1').replace('R:dnsmasq\n', 'R:other\n')}
        self.assertEqual(package_replacements(installed, target, {'dnsmasq'}), set())


if __name__ == '__main__': unittest.main(verbosity=2)
