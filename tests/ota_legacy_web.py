"""Generate with current code, inspect with the actual pre-fix engine, test ubus.

Local integration requires the historical source ref supplied by --legacy-ref.
It never loads that engine into a device or makes it a production build input.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from test_ota import mu_release
from backend.ota_engine import Engine, Patch
from backend.ota_package import analyze


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--legacy-ref',required=True)
    args=parser.parse_args()
    repo=Path(__file__).resolve().parents[1]
    source=subprocess.check_output(['git','-c','safe.directory='+str(repo),'show',args.legacy_ref+':runtime/luci-app-unisoc-ota/root/usr/lib/unisoc-ota/engine.py'],cwd=repo)
    with tempfile.TemporaryDirectory(prefix='ota-legacy-web-') as folder:
        root=Path(folder); old=root/'legacy.py';old.write_bytes(source)
        spec=importlib.util.spec_from_file_location('legacy_engine',old)
        legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
        files={'usr/share/fixture/'+str(i):b'unchanged' for i in range(10000)}
        files.update({'usr/lib/unisoc-ota/engine.py':b'embedded target engine',
                      'usr/bin/python3':b'fixture interpreter','usr/lib/test':b'old'})
        mu_release(root/'old','mu300-r1',files)
        mu_release(root/'new','mu300-r2',dict(files,**{'usr/lib/test':b'new'}))
        (root/'new/payload/busybox').write_bytes(b'fixture static helper')
        ota=root/'update.ota';analyze(root/'old',root/'new',ota)
        device=root/'device'
        (device/'usr/lib').mkdir(parents=True);(device/'usr/lib/test').write_bytes(b'old')
        (device/'etc/mu300').mkdir(parents=True)
        (device/'etc/mu300/release.json').write_text((root/'old/desktop/release.json').read_text())
        (device/'etc/config').mkdir();(device/'etc/config/wireless').write_bytes(b'user wifi settings')
        (device/'etc/shadow').write_bytes(b'user account remains')
        with Patch(ota) as current,legacy.Patch(ota) as previous:
            assert current.id==previous.id
            assert 'release_index' not in previous.manifest
            assert len(json.dumps(current.release_index))>1<<20
            result=legacy.Engine(device).inspect(previous)
            assert result['applicable'],result
            assert len(json.dumps(result))<128<<10
            Engine(device).apply(current)
        assert (device/'usr/lib/test').read_bytes()==b'new'
        baseline=json.loads((device/'etc/unisoc-ota/baseline.json').read_text())
        assert baseline['release']['meta']['image_version']=='mu300-r2'
        subprocess.run([sys.executable,str(repo/'runtime/luci-app-unisoc-ota/root/usr/lib/unisoc-ota/engine.py'),'rollback'],
                       env=dict(os.environ,UNISOC_OTA_ROOT=str(device)),check=True,capture_output=True,text=True)
        assert (device/'usr/lib/test').read_bytes()==b'old'
        assert not (device/'etc/unisoc-ota/baseline.json').exists()
        assert (device/'etc/config/wireless').read_bytes()==b'user wifi settings'
        assert (device/'etc/shadow').read_bytes()==b'user account remains'
        print('PASS actual legacy engine reads current OTA; 1+ MiB target baseline stays out of its RPC reply',flush=True)
        print('PASS apply/rollback preserve baseline semantics, wireless settings and accounts',flush=True)
        from ota_rpc_bus import main as check_bus
        check_bus(result)


if __name__=='__main__':main()
