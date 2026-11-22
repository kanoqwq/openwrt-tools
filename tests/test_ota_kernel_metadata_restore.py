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


if __name__ == '__main__':
    unittest.main(verbosity=2)
