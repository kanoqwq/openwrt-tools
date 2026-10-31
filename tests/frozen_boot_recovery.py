"""Exercise each real packaged boot repacker inside the shipped frozen backend.

The raw-device and filesystem evidence transports are simulated. No device is
opened, mounted, flashed or rebooted. The package's actual Image is preserved.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys


def child(root):
    assert getattr(sys,'frozen',False)
    from backend.adapter import Adapter
    from backend.package import inspect_package
    from backend import boot_recovery as br
    from backend.uninstall_growth import base_boot
    from types import SimpleNamespace
    import zlib
    package=inspect_package(root)
    adapter=Adapter(package)
    base=base_boot(adapter)
    kernel,=struct.unpack_from('<I',base,8)
    original_proof=br.internal_proof
    results=[]
    try:
        for slot in ('a','b'):
            other='b' if slot=='a' else 'a'
            target='/dev/block/by-name/boot_'+other
            current=bytes(4096)
            control=bytearray(32);control[:4]=('_'+slot).encode()+b'\0\0'
            control[4:8]=b'BCAB';control[8]=1;control[9]=2
            control[12],control[14]=(0x9f,0x1e) if slot=='a' else (0x1e,0x9f)
            struct.pack_into('<I',control,28,zlib.crc32(control[:28]))
            def shell(command,**kwargs):
                if command=='blockdev --getsize64 '+target:return str(64<<20)
                if command=='sha256sum '+target:return 'a'*64+' boot'
                raise AssertionError('Unexpected device access: '+command)
            def read(command):
                if 'by-name/misc ' in command:return bytes(control)
                m=re.fullmatch(r'dd if='+re.escape(target)+r' bs=(\d+)(?: skip=(\d+))? count=(\d+) 2>/dev/null',command)
                if m:return bytes(int(m[1])*int(m[3]))
                raise AssertionError('Unexpected device access: '+command)
            adapter.adb=SimpleNamespace(shell=shell,read=read)
            device={'slot':'_'+slot,'existing':True,'storage':{'ROOT':'fixture-root'}}
            br.internal_proof=lambda a,d,release,**kw:{'kernel_release':release,'uuid':'f'*32}
            info,image=br.prepare(adapter,device)
            assert info['required'] and info['slot']==other
            assert image[4096:4096+kernel]==base[4096:4096+kernel]
            def local_read(command):
                m=re.fullmatch(r'dd if=\S+ bs=(\d+)(?: skip=(\d+))? count=(\d+) 2>/dev/null',command)
                assert m,command
                bs,skip,count=int(m[1]),int(m[2] or 0),int(m[3])
                return image[bs*skip:bs*(skip+count)]
            files=br.boot_files(local_read,len(image),adapter.kind,other)
            if adapter.kind=='mu300':
                assert files['etc/mu300-desktop-root-mode'].strip()==b'part'
                assert b'/etc/mu300-desktop-storage.sh' in files['init']
            else:
                assert files['etc/e5/linux-slot'].strip()==other.encode()
                assert b'/etc/e5/part-root' in files['init']
            results.append({'android_slot':slot,'linux_slot':other,'kernel_unchanged':True,
                            'boot_sha256':hashlib.sha256(image).hexdigest()})
    finally:
        br.internal_proof=original_proof
    print(json.dumps({'family':adapter.kind,'frozen':True,'simulated_device':True,
                      'hardware_access':False,'checks':results}))


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--child':
        child(Path(sys.argv[2]).resolve())
    else:
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument('packages',nargs='+',type=Path)
        parser.add_argument('--backend',type=Path,required=True)
        args=parser.parse_args()
        for root in args.packages:
            result=subprocess.run([str(args.backend.resolve()),'--run-tool',str(Path(__file__).resolve()),'--child',str(root.resolve())],capture_output=True,timeout=120)
            if result.returncode:raise RuntimeError(result.stderr.decode('utf8','replace'))
            print(result.stdout.decode('utf8').strip())
