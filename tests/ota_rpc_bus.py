"""Real rpcd/ubus oversized-reply regression in the existing offline image.

No device access, host port, network or image download. Only this test's own
container is removed. Requires mu300-openwrt-base:25.12.5 already present.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def run(*args, timeout=15):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def main():
    repo=Path(__file__).resolve().parents[1]
    core=repo/'runtime/luci-app-unisoc-ota/root/usr/lib/unisoc-ota'
    sys.path.insert(0,str(core))
    spec=importlib.util.spec_from_file_location('ota_bus_rpc',core/'rpc.py')
    rpc=importlib.util.module_from_spec(spec);spec.loader.exec_module(rpc)
    # A clean release index exceeds the bus limit even with only seven changes.
    result={'id':'a'*64,'applicable':True,'conflicts':[],
            'manifest':{'base_version':'r1','target_version':'r2','operations':[{}]*7,
                        'release_index':{'files':'x'*1471143}}}
    image='mu300-openwrt-base:25.12.5'
    assert run('docker','image','inspect',image).returncode==0,'Existing test image is unavailable'
    with tempfile.TemporaryDirectory(prefix='ota-rpc-size-') as temp:
        temp=Path(temp)
        (temp/'large.json').write_text(json.dumps(result))
        (temp/'fixed.json').write_text(json.dumps(rpc.inspection_summary(result)))
        (temp/'plugin').write_text('''#!/bin/sh
if [ "$1" = list ]; then echo '{"inspect":{}}'; exit; fi
case "$1:$2" in call:inspect) cat /fixture/$(cat /tmp/response).json;; esac
''')
        (temp/'plugin').chmod(0o755)
        startup='''set -eu
mkdir -p /var/run/ubus /usr/libexec/rpcd
cp /fixture/plugin /usr/libexec/rpcd/ota-size-fixture
chmod 755 /usr/libexec/rpcd/ota-size-fixture
echo large >/tmp/response
ubusd >/tmp/bus.log 2>&1 &
while [ ! -S /var/run/ubus/ubus.sock ]; do sleep 1; done
/sbin/rpcd >/tmp/rpc.log 2>&1 &
exec sleep 120
'''
        child=run('docker','run','-d','--pull=never','--network=none','--platform=linux/arm64',
                  '--mount',f'type=bind,src={temp},dst=/fixture,readonly',
                  '--entrypoint','/bin/sh',image,'-c',startup)
        assert child.returncode==0,child.stderr
        container=child.stdout.strip()
        def command(*args):return run('docker','exec',container,*args)
        def wait_session():
            for _ in range(40):
                if command('ubus','list','session').stdout.strip()=='session':return
                time.sleep(.1)
            logs=run('docker','logs',container)
            detail=command('cat','/tmp/rpc.log')
            raise AssertionError('Fixture rpcd did not register session: '+logs.stdout+logs.stderr+detail.stdout+detail.stderr)
        try:
            wait_session()
            old=command('ubus','-t','3','call','ota-size-fixture','inspect')
            assert old.returncode!=0,'Oversized reply unexpectedly succeeded'
            assert not command('ubus','list','session').stdout.strip(),'Original bug did not remove login object'
            print('REPRODUCED: oversized inspection reply removes rpcd session object',flush=True)
            assert command('sh','-c','killall rpcd; echo fixed >/tmp/response; /sbin/rpcd >/tmp/rpc-fixed.log 2>&1 &').returncode==0
            wait_session()
            for _ in range(3):
                fixed=command('ubus','-t','3','call','ota-size-fixture','inspect')
                assert fixed.returncode==0,fixed.stderr
                value=json.loads(fixed.stdout)
                assert value['id']==result['id'] and len(value['manifest']['operations'])==7
                assert command('ubus','list','session').stdout.strip()=='session'
            print('PASS: compact inspection replies preserve rpcd/login; full verification data is unchanged')
        finally:
            removed=run('docker','rm','-f',container)
            assert removed.returncode==0,removed.stderr


if __name__=='__main__':main()
