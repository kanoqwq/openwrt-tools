"""Reproduce the small source-only ext4 reader used by on-device generation.

Uses explicitly pinned, already installed distributions; never downloads wheels
or copies native host binaries into firmware.
"""
from importlib.metadata import distribution
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import zipfile

TOP = Path(__file__).resolve().parents[1]
DEST = TOP/'runtime/luci-app-unisoc-ota/root/usr/lib/unisoc-ota/generator'
PINNED = {'dissect.extfs':'3.15','dissect.cstruct':'4.7','dissect.util':'3.24'}


def build():
    files = {}
    origins = {}
    for name, version in PINNED.items():
        package = distribution(name)
        if package.version != version:
            raise ValueError('Install the pinned source dependency: '+name+'=='+version)
        origins[name] = {'version':version,'license':package.metadata.get('License-Expression'),
                         'project_urls':package.metadata.get_all('Project-URL') or []}
        for relative in package.files or ():
            path = PurePosixPath(str(relative).replace('\\','/'))
            if path.parts[0] == 'dissect' and path.suffix in ('.py','.pyi'):
                files[str(path)] = package.locate_file(relative).read_bytes()
            elif 'licenses' in path.parts:
                files['licenses/'+name+'/'+path.name] = package.locate_file(relative).read_bytes()
    directories = {str(parent)+'/' for name in files for parent in PurePosixPath(name).parents if str(parent)!='.'}
    archive = DEST/'extfs-sources.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as output:
        for name in sorted(directories)+sorted(files):
            info = zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0));info.create_system=3
            info.external_attr=((stat.S_IFDIR|0o755) if name.endswith('/') else (stat.S_IFREG|0o644))<<16
            info.compress_type=zipfile.ZIP_DEFLATED
            output.writestr(info,files.get(name,b''))
    receipt={'packages':origins,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
             'files':{name:hashlib.sha256(data).hexdigest() for name,data in sorted(files.items())}}
    (DEST/'extfs-sources.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print('Source-only ext4 dependency:',archive.stat().st_size,'bytes;',len(files),'source/license files')


if __name__=='__main__':build()
