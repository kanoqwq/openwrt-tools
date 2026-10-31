#!/usr/bin/env python3
"""Replace only the root APK/metadata in an explicitly selected verified release.

Run through a firmware repository's tools/build-release.py run. Published ZIPs
are replaced atomically after validation; the journal permits interrupted resume.
"""
import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
import zipfile

TOOLS=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TOOLS))
from package_android import APK, METADATA, artifact, sha


def atomic(path,data):
    temporary=path.with_name(path.name+'.android-new')
    with temporary.open('wb') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def record(path,data):
    atomic(path,(json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode())


def refresh_archive(source,target,apk,metadata,verify):
    """Preserve every other member's content and its existing integrity entry."""
    replacements={APK:Path(apk),METADATA:Path(metadata)}
    hashes={name:sha(path) for name,path in replacements.items()}
    with zipfile.ZipFile(source) as original:
        names=original.namelist()
        if len(names)!=len(set(names)) or any(names.count(name)!=1 for name in replacements):
            raise ValueError('Expected exactly one root APK and Android build record')
        if 'files/SHA256SUMS' in names:
            integrity='files/SHA256SUMS';old=original.read(integrity)
            sums={name:h for line in old.decode().splitlines() for h,name in [line.split(None,1)]}
            if set(sums)!={n for n in names if not n.endswith('/') and n!=integrity}:
                raise ValueError('Incomplete E5 input manifest')
            sums.update(hashes)
            updated=''.join(h+'  '+n+'\n' for n,h in sorted(sums.items())).encode()
        elif 'desktop/manifest.json' in names:
            integrity='desktop/manifest.json';old=original.read(integrity);manifest=json.loads(old)
            for name,wanted in hashes.items():
                owners=[key for key in ('magisk','desktop') if name in manifest.get(key,{})]
                if not owners:raise ValueError('Missing MU300 APK integrity entry: '+name)
                for key in owners:manifest[key][name]=wanted
            updated=(json.dumps(manifest,indent=2)+'\n').encode()
        else:raise ValueError('Unknown release integrity layout')
        with zipfile.ZipFile(target,'x') as output:
            output.comment=original.comment
            for member in original.infolist():
                info=copy.copy(member);info._compresslevel=1
                with output.open(info,'w',force_zip64=True) as destination:
                    if member.filename==integrity:destination.write(updated)
                    elif member.filename in replacements:
                        with replacements[member.filename].open('rb') as stream:shutil.copyfileobj(stream,destination,1<<20)
                    else:
                        with original.open(member) as stream:shutil.copyfileobj(stream,destination,1<<20)
    with zipfile.ZipFile(source) as before,zipfile.ZipFile(target) as after:
        if before.namelist()!=after.namelist():raise ValueError('Archive member set/order changed')
        # The only changed checksums are the two APK members. The family's full
        # verifier below hashes every real member against this updated manifest.
        if integrity=='files/SHA256SUMS':
            left={n:h for line in before.read(integrity).decode().splitlines() for h,n in [line.split(None,1)]}
            right={n:h for line in after.read(integrity).decode().splitlines() for h,n in [line.split(None,1)]}
            for name in hashes:left.pop(name);right.pop(name)
            if left!=right:raise ValueError('Non-APK payload integrity changed')
        else:
            left=json.loads(before.read(integrity));right=json.loads(after.read(integrity))
            for values in (left,right):
                for group in ('magisk','desktop'):
                    for name in hashes:values.get(group,{}).pop(name,None)
            if left!=right:raise ValueError('Non-APK MU300 manifest changed')
    verify(target)


def refresh_release(repo,release,family,apk,metadata):
    repo=Path(repo).resolve();raw=Path(release)
    if raw.is_symlink():raise ValueError('Release may not be a symlink')
    release=raw.resolve()
    parent=repo/('out/releases' if family=='e5' else 'release')
    if release.parent!=parent or release.name.startswith('.'):
        raise ValueError('Select a published release inside the selected repository')
    verifier=runpy.run_path(str(repo/'tools/build-release.py'))['verify_archive']
    def verify(path):
        if family=='e5':verifier(path,require_android=True)
        else:verifier(path,require_diff=True,require_android=True)
    run_path=release/'BUILD-RUN.json';run=json.loads(run_path.read_text())
    if run.get('state')!='verified' or not run.get('assets'):raise ValueError('Release is not verified')
    build=json.loads(Path(metadata).read_text());apk_hash=sha(apk);metadata_hash=sha(metadata)
    if build.get('files')!={APK:apk_hash}:raise ValueError('APK artifact metadata differs')
    journal_path=release/'ANDROID-REFRESH.json'
    if journal_path.exists():
        journal=json.loads(journal_path.read_text())
        if journal.get('apk_sha256')!=apk_hash or journal.get('metadata_sha256')!=metadata_hash:
            raise ValueError('Existing Android refresh belongs to another artifact')
    else:
        journal={'format':1,'kind':'android-apk-only','started_utc':datetime.now(timezone.utc).isoformat(),
                 'apk_sha256':apk_hash,'metadata_sha256':metadata_hash,'entries':{}}
        record(journal_path,journal)
    for item in run['assets']:
        name=item['file'];path=release/name
        if Path(name).name!=name or path.is_symlink() or path.resolve().parent!=release:
            raise ValueError('Unsafe archive path')
        temporary=path.with_name(path.name+'.android-refresh.tmp')
        saved=journal['entries'].get(name);current=sha(path)
        if saved and current==saved.get('after_sha256'):
            verify(path)
        else:
            expected=saved['before_sha256'] if saved else item['sha256']
            if current!=expected:raise ValueError('Input archive differs: '+name)
            if saved and saved.get('phase')=='prepared':
                if temporary.is_symlink() or not temporary.is_file() or sha(temporary)!=saved['after_sha256']:
                    raise ValueError('Prepared APK refresh is missing or changed')
                verify(temporary)
            else:
                if temporary.exists():
                    if not saved or saved.get('phase')!='building':raise ValueError('Unowned refresh staging exists')
                    if temporary.is_symlink():raise ValueError('Refresh staging is a symlink')
                    temporary.unlink()  # Only this journal's interrupted ZIP staging.
                verify(path)
                saved={'phase':'building','before_sha256':current,'before_bytes':path.stat().st_size}
                journal['entries'][name]=saved;record(journal_path,journal)
                refresh_archive(path,temporary,apk,metadata,verify)
                saved.update(phase='prepared',after_sha256=sha(temporary),after_bytes=temporary.stat().st_size)
                record(journal_path,journal)
            os.replace(temporary,path)
        item.update(sha256=saved['after_sha256'],bytes=path.stat().st_size,android_apk_sha256=apk_hash)
        atomic(Path(str(path)+'.sha256'),(item['sha256']+'  '+name+'\n').encode())
        run['android_refresh']={'record':journal_path.name,'apk_sha256':apk_hash,'metadata_sha256':metadata_hash}
        record(run_path,run)
        saved['phase']='complete';record(journal_path,journal)
        print('APK verified: '+name,flush=True)
    journal.update(state='complete',finished_utc=datetime.now(timezone.utc).isoformat());record(journal_path,journal)
    print('Android APK refreshed in place: '+str(release),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,required=True)
    parser.add_argument('--release',type=Path,required=True)
    parser.add_argument('--family',choices=('e5','mu300'),required=True)
    args=parser.parse_args()
    apk,metadata=artifact()
    refresh_release(args.repo,args.release,args.family,apk,metadata)
