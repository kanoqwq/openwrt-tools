"""Real diff application and APK solving when withdrawing generated records."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_ota import mu_release, analyze, Patch, Engine
from backend.ota_engine import package_records


def record(name, version='1.0-r1', deps='', origin='base', files=''):
    checksum = base64.b64encode(hashlib.sha1((name+version+deps+origin).encode()).digest()).decode()
    return (f'C:Q1{checksum}\nP:{name}\nV:{version}\nA:aarch64_generic\nS:0\nI:0\n'
            f'T:Package fixture {name}\no:{origin}\n'+(f'D:{deps}\n' if deps else '')+files)


def files(generated):
    version = '2.0-r1' if generated else '1.0-r1'
    origin = 'unisoc-native-kernel' if generated else 'feeds/base/kernel/linux'
    rows = [record('ubox'), record('kernel', version, 'ubox', origin),
            record('kmod-tun', version, 'kernel='+version, origin), record('dnsmasq-full')]
    result = {
        'usr/bin/python3':b'fixture interpreter',
        'usr/lib/unisoc-ota/engine.py':b'fixture updater',
        'lib/apk/db/installed':('\n\n'.join(rows)+'\n\n').encode(),
        'etc/apk/world':('ubox\nkernel='+version+'\nkmod-tun='+version+'\ndnsmasq-full\n').encode(),
        'etc/config/wireless':b'factory Wi-Fi settings',
        'usr/sbin/dnsmasq':b'existing DNS binary',
        'lib/modules/7.2.8/tun.ko':b'unchanged actual firmware module',
    }
    if generated:
        rows.append(record('kmod-generated-only', version, 'kernel='+version, origin))
        result['lib/apk/db/installed']=('\n\n'.join(rows)+'\n\n').encode()
        result['etc/apk/world']+=b'kmod-generated-only@legacy=2.0-r1\n'
        result['usr/share/unisoc-kernel/package.json']=b'{"format":1,"generated":true}\n'
    return result


@unittest.skipUnless(sys.platform == 'linux', 'POSIX OTA semantics')
class KernelMetadataRestore(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='kernel-record-withdrawal-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.old=files(True); self.new=files(False)
        mu_release(self.root/'old', 'mu300-r1', self.old)
        mu_release(self.root/'new', 'mu300-r2', self.new)
        self.patch=self.root/'withdraw.ota'
        result=analyze(self.root/'old', self.root/'new', self.patch)
        self.assertTrue(result['can_export'], result['blockers'])
        self.live=self.root/'live'
        for name, raw in self.old.items():
            path=self.live/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        identity=self.live/'etc/mu300/release.json';identity.parent.mkdir(parents=True)
        identity.write_bytes((self.root/'old/desktop/release.json').read_bytes())
        db=self.live/'lib/apk/db/installed'
        # This is the reported state: old distribution kmods survived a native
        # record migration, so even unrelated package transactions are broken.
        db.write_bytes(db.read_bytes()+(record('kmod-user','1.0-r1','kernel=1.0-r1','user-build')+
            '\n\n'+record('user-plugin','9.0-r1','kmod-user','user')+'\n\n').encode())
        with (self.live/'etc/apk/world').open('a') as stream:
            stream.write('kmod-user@custom\nuser-plugin\n')
        (self.live/'etc/config/wireless').write_bytes(b'user Wi-Fi password')

    def apply(self):
        with Patch(self.patch) as patch:
            self.assertEqual(Engine(self.live).apply(patch)['status'], 'complete')

    def test_records_world_and_user_data_are_restored_without_removing_modules(self):
        self.apply()
        rows=package_records((self.live/'lib/apk/db/installed').read_bytes())
        self.assertIn('V:1.0-r1',rows['kernel'])
        self.assertIn('D:kernel=1.0-r1',rows['kmod-tun'])
        self.assertNotIn('kmod-generated-only',rows)
        self.assertIn('kmod-user',rows);self.assertIn('V:9.0-r1',rows['user-plugin'])
        world=(self.live/'etc/apk/world').read_text()
        self.assertNotIn('2.0-r1',world)
        self.assertIn('kmod-user@custom\n',world)
        self.assertEqual((self.live/'etc/config/wireless').read_bytes(),b'user Wi-Fi password')
        self.assertEqual((self.live/'usr/sbin/dnsmasq').read_bytes(),self.old['usr/sbin/dnsmasq'])
        self.assertEqual((self.live/'lib/modules/7.2.8/tun.ko').read_bytes(),self.old['lib/modules/7.2.8/tun.ko'])
        self.assertFalse((self.live/'usr/share/unisoc-kernel/package.json').exists())

    def test_rollback_restores_the_exact_previous_metadata(self):
        names=('lib/apk/db/installed','etc/apk/world','usr/share/unisoc-kernel/package.json')
        before={name:(self.live/name).read_bytes() for name in names}
        self.apply()
        script=Path(__file__).resolve().parents[1]/'runtime/luci-app-unisoc-ota/root/usr/lib/unisoc-ota/engine.py'
        subprocess.run([sys.executable,str(script),'rollback','--root',str(self.live)],check=True,
                       stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        for name,data in before.items():self.assertEqual((self.live/name).read_bytes(),data)

    def test_actual_apk_solver_accepts_restored_dependencies(self):
        image=os.environ.get('UNISOC_APK_TEST_IMAGE','u30pro-rootfs:25.12.5')
        if shutil.which('docker') is None:self.skipTest('Existing offline ARM64 APK image required')
        self.apply()
        # Remove the source label only in this private solver copy; it has no
        # source index. The preceding test separately verifies label retention.
        world=self.live/'etc/apk/world';world.write_text(world.read_text().replace('@custom',''))
        cmd=['docker','run','--rm','--network','none','--ulimit','core=0','--platform','linux/arm64',
             '-v',f'{self.live}:{self.live}','--entrypoint','/usr/bin/apk',image,
             '--root',str(self.live),'--arch','aarch64_generic','--no-network',
             'add','--simulate','--scripts=no','--commit-hooks=no','user-plugin']
        result=subprocess.run(cmd,capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def partial_update(self):
        """Reproduce the old applier: restored kernel plus retained own kmods."""
        db=self.live/'lib/apk/db/installed'
        records=package_records(db.read_bytes())
        target=package_records(self.new['lib/apk/db/installed'])
        records.update(target)
        records['kmod-overwritten']=record('kmod-overwritten','1.0-r1','kernel=1.0-r1','feeds/base/kernel/linux')
        records['kmod-private']=record('kmod-private','2.0-r1','kernel=1.0-r1','user-build')
        db.write_text('\n\n'.join(records.values())+'\n\n')
        world=self.live/'etc/apk/world'
        world.write_text('ubox\nkernel=1.0-r1\nkmod-generated-only@legacy=2.0-r1\n'
                         'kmod-overwritten=2.0-r1\nkmod-private@custom=2.0-r1\nuser-plugin\n')
        self.new['lib/apk/db/installed']+=('\n'+records['kmod-overwritten']+'\n\n').encode()
        (self.live/'usr/share/unisoc-kernel/package.json').unlink()

    def make_second_patch(self):
        self.partial_update()
        before=dict(self.new)
        after=dict(before, **{'usr/lib/new-feature':b'new feature'})
        for name,values in (('bridge',before),('next',after)):
            mu_release(self.root/name,'mu300-'+name,values)
            (self.root/name/'payload/busybox').write_bytes(b'fixture static helper')
        self.patch=self.root/'second.ota'
        analyze(self.root/'bridge',self.root/'next',self.patch)
        marker=self.live/'etc/mu300/release.json'
        marker.write_bytes((self.root/'bridge/desktop/release.json').read_bytes())
        return before

    def test_second_update_repairs_partial_state_when_clean_metadata_is_unchanged(self):
        self.make_second_patch()
        with Patch(self.patch) as patch:
            self.assertFalse(any(op.get('merge') in ('apk-db','world') for op in patch.manifest['operations']))
            self.assertIn('ota/package-db',patch.manifest['runtime'])
            self.assertIn('ota/package-world',patch.manifest['runtime'])
            # Execute the engine actually carried by the generated archive.
            ram=self.root/'ram';ram.mkdir()
            for key,desc in patch.manifest['runtime'].items():
                if key.startswith('ota/') and key.endswith('.py'):
                    destination=ram/key[4:];destination.parent.mkdir(parents=True,exist_ok=True)
                    destination.write_bytes(patch.archive.read('blobs/'+desc['sha256']))
        applier=ram/'engine.py'
        result=subprocess.run([sys.executable,str(applier),'apply',str(self.patch),'--root',str(self.live)],
                              capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        rows=package_records((self.live/'lib/apk/db/installed').read_bytes())
        self.assertNotIn('kmod-generated-only',rows)
        self.assertIn('kmod-private',rows)
        self.assertIn('kmod-user',rows)
        world=(self.live/'etc/apk/world').read_text()
        self.assertNotIn('kmod-overwritten=2.0-r1',world)
        self.assertNotIn('kmod-generated-only',world)
        self.assertIn('kmod-private@custom=2.0-r1',world)
        self.assertEqual((self.live/'etc/config/wireless').read_bytes(),b'user Wi-Fi password')
        self.assertEqual((self.live/'lib/modules/7.2.8/tun.ko').read_bytes(),self.old['lib/modules/7.2.8/tun.ko'])

    def test_partial_state_repair_rollback_restores_db_and_tagged_world(self):
        self.make_second_patch()
        names=('lib/apk/db/installed','etc/apk/world')
        before={n:(self.live/n).read_bytes() for n in names}
        self.apply()
        script=Path(__file__).resolve().parents[1]/'runtime/luci-app-unisoc-ota/root/usr/lib/unisoc-ota/engine.py'
        subprocess.run([sys.executable,str(script),'rollback','--root',str(self.live)],check=True,
                       stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        for name,data in before.items():self.assertEqual((self.live/name).read_bytes(),data)

    def test_partial_repair_preserves_unrelated_upgraded_base_package_and_pin(self):
        self.make_second_patch()
        db=self.live/'lib/apk/db/installed'
        rows=package_records(db.read_bytes())
        rows['ubox']=record('ubox','9.0-r1',origin='user-feed')
        db.write_text('\n\n'.join(rows.values())+'\n\n')
        world=self.live/'etc/apk/world'
        world.write_text(world.read_text().replace('ubox\n','ubox@custom=9.0-r1\n'))
        self.apply()
        self.assertIn('V:9.0-r1',package_records(db.read_bytes())['ubox'])
        self.assertIn('ubox@custom=9.0-r1',world.read_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
