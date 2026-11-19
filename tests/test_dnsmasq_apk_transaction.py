"""Real APK transaction against an explicit metadata-only device snapshot.

No service scripts, network, hardware or full rootfs copies are used. The
snapshot/binary remain private local diagnostics, never test source inputs
for a firmware build.
"""
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from backend.ota_engine import package_records, package_versions, shadowed_dnsmasq, full_dnsmasq_world

SNAPSHOT = os.environ.get('DNSMASQ_APK_SNAPSHOT')
BINARY = os.environ.get('DNSMASQ_APK_BINARY')
APKS = os.environ.get('DNSMASQ_WIFI_APKS')
APK = os.environ.get('DNSMASQ_APK_TOOL')


@unittest.skipUnless(all((SNAPSHOT, BINARY, APKS, APK)), 'explicit APK regression inputs required')
class RealApkTransaction(unittest.TestCase):
    def seed(self, root):
        with tarfile.open(SNAPSHOT) as archive:
            for member in archive:
                if not member.isfile() or not (member.name.startswith('lib/apk/db/')
                                               or member.name == 'etc/apk/world'):
                    continue
                path = root/member.name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.extractfile(member).read())
        binary = root/'usr/sbin/dnsmasq';binary.parent.mkdir(parents=True)
        binary.write_bytes(Path(BINARY).read_bytes());binary.chmod(0o755)
        config = root/'etc/config/dhcp';config.parent.mkdir(parents=True)
        config.write_bytes(b'private DHCP ranges and static leases')
        service=root/'etc/init.d/dnsmasq';service.parent.mkdir(parents=True)
        service.write_bytes(b'#!/bin/sh\n# existing DNS/DHCP service\n');service.chmod(0o755)
        enabled=root/'etc/rc.d/S19dnsmasq';enabled.parent.mkdir(parents=True)
        enabled.symlink_to('../init.d/dnsmasq')

    def command(self, root, simulate=False):
        files = sorted(Path(APKS).glob('*.apk'))
        self.assertEqual(len(files), 3)
        architectures = {r['A'] for r in package_versions((root/'lib/apk/db/installed').read_bytes()).values()
                         if r['A'] and r['A'] not in ('all', 'noarch')}
        self.assertEqual(len(architectures), 1)
        command = [APK, '--root', str(root), '--arch', architectures.pop(), '--allow-untrusted',
                   '--no-network', '--no-scripts', '--no-commit-hooks']
        if simulate:command.append('--simulate')
        return subprocess.run(command+['add', *map(str, files)],
                              capture_output=True, text=True, timeout=60)

    def test_repaired_metadata_keeps_dns_program_through_actual_wifi_apk_install(self):
        with tempfile.TemporaryDirectory(prefix='dnsmasq-apk-test-') as folder:
            root=Path(folder);self.seed(root)
            before=self.command(root, True)
            self.assertEqual(before.returncode, 0, before.stdout+before.stderr)
            self.assertIn('Purging dnsmasq ', before.stdout)
            db=root/'lib/apk/db/installed';records=package_records(db.read_bytes())
            replaced=shadowed_dnsmasq(records)
            self.assertEqual(replaced, {'dnsmasq'})
            db.write_bytes(('\n\n'.join(v.rstrip('\n') for k,v in records.items()
                                        if k not in replaced)+'\n\n').encode())
            world=root/'etc/apk/world';world.write_bytes(full_dnsmasq_world(world.read_bytes()))
            plan=self.command(root, True)
            self.assertEqual(plan.returncode, 0, plan.stdout+plan.stderr)
            self.assertNotIn('Purging dnsmasq', plan.stdout)
            result=self.command(root)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertNotIn('Purging dnsmasq', result.stdout)
            self.assertEqual((root/'usr/sbin/dnsmasq').read_bytes(), Path(BINARY).read_bytes())
            self.assertTrue((root/'usr/sbin/dnsmasq').stat().st_mode & 0o111)
            self.assertEqual((root/'etc/config/dhcp').read_bytes(), b'private DHCP ranges and static leases')
            self.assertEqual((root/'etc/init.d/dnsmasq').read_bytes(), b'#!/bin/sh\n# existing DNS/DHCP service\n')
            self.assertEqual(os.readlink(root/'etc/rc.d/S19dnsmasq'), '../init.d/dnsmasq')
            after=package_records(db.read_bytes())
            self.assertNotIn('dnsmasq', after)
            self.assertIn('dnsmasq-full', after)
            self.assertEqual(set(after), set(records)-{'dnsmasq'})
            print(json.dumps({'real_apk_install':'pass', 'dns_binary_preserved':True,
                              'dhcp_config_preserved':True, 'other_packages_preserved':True}))


if __name__ == '__main__':
    unittest.main(verbosity=2)
