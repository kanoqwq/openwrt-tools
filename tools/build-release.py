#!/usr/bin/env python3
"""Run a native installer build with the shared workspace lock and durable logs."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    temp.write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
    temp.replace(path)


def resume_firmware(args):
    """Complete an E5 batch in place, using its unchanged firmware checkout.

    The controller can be updated independently of the firmware source. Existing
    archives are immutable inputs, not templates to repackage or copy elsewhere.
    """
    repo=args.repository.resolve(); pending=args.pending.resolve()
    parent=repo/'out/releases'
    if args.pending.is_symlink() or pending.parent!=parent or not pending.name.startswith('.building-'):
        raise ValueError('Select an owned pending E5 release under out/releases')
    previous=pending.name.removeprefix('.building-')
    if not re.fullmatch(r'[A-Za-z0-9-]+',previous):raise ValueError('Invalid pending task name')
    state=json.loads((repo/'out/build-logs'/previous/'status.json').read_text())
    if state.get('state')!='failed' or Path(state.get('pending_output','')).resolve()!=pending:
        raise ValueError('Only a stopped failed batch can be resumed')
    api=runpy.run_path(str(repo/'tools/build-release.py'))
    if api.get('OWNER')!='e5-linux-build-release-v1':raise ValueError('Unsupported firmware build entry')
    common,rows=api['layout'](repo/'tools/build-layout.json',args.workspace.resolve(),args.profiles)
    identify=runpy.run_path(str(repo/'tools/release-identity.py'))['identify']
    with api['workspace_lock'](args.workspace.resolve()):
        task=api['Task'](args.workspace.resolve(),args.label)
        try:
            api['input_check'](common,rows)
            before=identify(repo,'e5','6.18.54')
            assets=[]; version=None; found=set()
            for path in sorted(pending.glob('*.zip')):
                if path.is_symlink():raise ValueError('Symlink archive is not an owned output')
                with zipfile.ZipFile(path) as archive:
                    record=json.loads(archive.read('files/RELEASE.json'))
                    build=json.loads(archive.read('files/BUILD.json'))
                profile=record['profile']
                if profile not in rows or profile in found:
                    raise ValueError('Archive is not a unique selected model: '+profile)
                if (record['source_commit']!=before['source_commit'] or record['source_fingerprint']!=before['source_fingerprint'] or
                        record.get('source_dirty') is not before['source_dirty'] or path.name!=record['package_name']+'.zip'):
                    raise ValueError('Firmware source changed since this archive was built: '+profile)
                if version is not None and version!=record['private_version']:raise ValueError('Mixed pending release versions')
                version=record['private_version']
                if build['kernel_manifest']!=json.loads((rows[profile]['kernel']/'container-support.json').read_text()):
                    raise ValueError('Kernel/module cohort changed: '+profile)
                checksum=api['digest'](path)
                if path.with_name(path.name+'.sha256').read_text().split()!=[checksum,path.name]:
                    raise ValueError('Completed archive checksum changed: '+profile)
                api['verify_archive'](path,require_android=True)
                assets.append(dict(profile=profile,file=path.name,bytes=path.stat().st_size,sha256=checksum))
                found.add(profile);print('Reusing verified archive: '+profile,flush=True)
            if not assets:raise ValueError('Pending batch has no verified archives')
            task.save(pending_output=str(pending),resumed_from=previous,assets=assets)
            for profile,row in rows.items():
                if profile in found:continue
                stage=api['stage_path'](args.workspace.resolve(),profile)
                if stage.exists():raise ValueError('Inspect retained staging, then clean-staging: '+profile)
                stage.mkdir(parents=True)
                api['write_json'](stage/'.build-owner.json',dict(owner=api['OWNER'],profile=profile))
                command=[sys.executable,repo/'openwrt/build-profile-tf.py','--profile',profile,
                         '--work',stage/'assembly','--output',pending,'--release-version',version]
                for key,path in {**common,**row}.items():command+=['--'+key.replace('_','-'),path]
                task.run(profile+'-package',command)
                matches=list(pending.glob(profile+'-*.zip'))
                if len(matches)!=1:raise ValueError('Expected one archive for '+profile)
                path=matches[0];api['verify_archive'](path,require_android=True)
                assets.append(dict(profile=profile,file=path.name,bytes=path.stat().st_size,sha256=api['digest'](path)))
                task.save(assets=assets);api['clean_stage'](args.workspace.resolve(),profile)
            if identify(repo,'e5','6.18.54')['source_fingerprint']!=before['source_fingerprint']:
                raise ValueError('Firmware sources changed during resumed assembly')
            destination=parent/previous
            if destination.exists():raise ValueError('Published destination already exists')
            before.update(private_version=version,
                          image_version=before['profile']+'-'+version,
                          package_name=f"{before['profile']}-openwrt-{before['openwrt']}-{version}-k{before['kernel_release']}")
            api['write_json'](pending/'BUILD-RUN.json',{**task.data,'state':'verified','source':before,'assets':assets})
            pending.rename(destination)
            task.save(state='complete',output=str(destination),assets=assets)
            print('Verified release: '+str(destination),flush=True)
        except BaseException as error:
            task.save(state='failed',error=str(error));raise
    return 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='action',required=True)
    runner=commands.add_parser('run')
    runner.add_argument('--workspace',type=Path,required=True)
    runner.add_argument('--label',required=True)
    runner.add_argument('command',nargs=argparse.REMAINDER)
    resume=commands.add_parser('resume-firmware')
    resume.add_argument('--repository',type=Path,required=True)
    resume.add_argument('--pending',type=Path,required=True)
    resume.add_argument('--workspace',type=Path,required=True)
    resume.add_argument('--profiles',nargs='+',required=True)
    resume.add_argument('--label',default='resume-firmware')
    args=parser.parse_args()
    if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}',args.label):raise ValueError('Invalid task label')
    if args.action=='resume-firmware':return resume_firmware(args)
    command=args.command
    if command[:1]==['--']:command=command[1:]
    if not command:raise ValueError('Build command is required')
    if os.name!='posix':raise ValueError('Use the existing WSL workspace runner for Windows native builds')
    if not args.workspace.is_dir():raise ValueError('Existing build workspace is missing; do not create a duplicate workspace')
    import fcntl
    locks=args.workspace/'locks';locks.mkdir(exist_ok=True)
    fd=os.open(locks/'openwrt-release.lock',os.O_RDONLY|os.O_CREAT,0o644)
    with os.fdopen(fd,'rb') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as error:raise ValueError('Another build owns the shared workspace') from error
        task=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+args.label+'-'+str(os.getpid())
        directory=ROOT/'out/build-logs'/task;directory.mkdir(parents=True)
        state={'task':task,'status':'running','command':command,'log':str(directory/'command.log')}
        destination=ROOT/'out/build-state/latest.json';atomic(destination,state)
        print('command: '+state['log'],flush=True)
        try:
            with (directory/'command.log').open('wb') as output:
                result=subprocess.run(command,cwd=ROOT,stdout=output,stderr=subprocess.STDOUT,check=False)
            state.update(status='complete' if result.returncode==0 else 'failed',exit_code=result.returncode)
            return result.returncode
        except BaseException as error:
            state.update(status='failed',error=type(error).__name__+': '+str(error))
            raise
        finally:
            atomic(destination,state);atomic(directory/'status.json',state)
            fcntl.flock(lock,fcntl.LOCK_UN)


if __name__=='__main__':sys.exit(main())
