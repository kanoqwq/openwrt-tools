"""Event-started clean-release comparison. No live-root diff or system writes."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from engine import Engine, Patch, atomic_json


class Builder:
    def __init__(self, root=None):
        self.engine = Engine(root)
        self.state = self.engine.state
        self.home = self.engine.path('etc/unisoc-ota/builder')
        if self.home.is_symlink():
            raise ValueError('Unsafe patch generation directory')
        self.home.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.job = self.home/'job.json'
        self.output = self.home/'update.ota'
        self.baseline = self.state/'baseline.json'

    def baseline_status(self):
        from baseline import current
        try:
            identity = self.engine.identity()
            stamp = self.baseline.stat()
            cache = self.home/'baseline-status.json'
            key = [stamp.st_ino,stamp.st_size,stamp.st_mtime_ns,identity]
            if cache.is_file():
                saved=json.loads(cache.read_text())
                if saved.get('key') == key:
                    return saved['value']
            value = current(self.baseline,identity)
            result = {'available':True,'version':value['release']['meta']['image_version'],
                      'profile':value['release']['meta']['profile']}
            atomic_json(cache,{'key':key,'value':result})
            return result
        except (OSError,ValueError,KeyError) as error:
            return {'available':False,'error':str(error)}

    def status(self):
        value = json.loads(self.job.read_text()) if self.job.exists() else {'status':'idle'}
        if value.get('status') == 'running':
            with (self.state/'job.lock').open('a') as held:
                try:
                    fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    value.update(status='failed',error='Patch generation was interrupted')
                    atomic_json(self.job,value)
        uploads={name: (self.home/name).stat().st_size for name in ('old.zip','new.zip')
                 if (self.home/name).is_file() and not (self.home/name).is_symlink()}
        return {'job':value,'baseline':self.baseline_status(),'uploads':uploads,
                'download':bool(value.get('status')=='complete' and value.get('id') and self.output.is_file())}

    def start(self, mode):
        if mode not in ('current','pair','baseline'):
            raise ValueError('Invalid patch generation mode')
        with (self.state/'job.lock').open('a') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            recovery=self.engine.status().get('recovery') or {}
            if recovery.get('pending'):
                raise ValueError('Update recovery is pending')
            required = ('old.zip',) if mode=='baseline' else ('new.zip',) if mode=='current' else ('old.zip','new.zip')
            for name in required:
                path=self.home/name
                if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 2 << 30:
                    raise ValueError('Select and upload the required full firmware ZIP: '+name)
            if mode=='current' and not self.baseline_status()['available']:
                raise ValueError('Import the current version full ZIP once to establish its clean baseline')
            atomic_json(self.job,{'status':'running','phase':'queued','mode':mode})
            try:
                subprocess.Popen([sys.executable,__file__,'_worker',mode,str(held.fileno()),str(self.engine.root)],
                                 stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                 start_new_session=True,pass_fds=(held.fileno(),))
            except BaseException as error:
                atomic_json(self.job,{'status':'failed','mode':mode,'error':str(error)})
                raise
        return {'status':'running','mode':mode,'phase':'queued'}

    def run(self, mode, lock_fd):
        # Inherited lock makes generation mutually exclusive with apply/rollback
        # without holding APK/package-manager locks while hashing release files.
        with os.fdopen(lock_fd,'a'):
            try:
                prepare_dependencies()
                from generator.ota_package import Release, analyze
                from generator.release_index import snapshot, read_index
                from baseline import current
                last = 0
                def progress(stage,path):
                    nonlocal last
                    now=time.monotonic()
                    if now-last>=0.4:
                        atomic_json(self.job,{'status':'running','mode':mode,'phase':stage,'path':path})
                        last=now
                with tempfile.TemporaryDirectory(prefix='.build-',dir=self.home) as folder:
                    folder=Path(folder)
                    # Release's E5 scratch image stays beside this uploaded
                    # ZIP on persistent storage, never in RAM-backed /tmp.
                    required=('old.zip',) if mode=='baseline' else ('new.zip',) if mode=='current' else ('old.zip','new.zip')
                    for name in required:
                        path=self.home/name
                        if path.is_symlink() or not path.is_file():
                            raise ValueError('Full firmware upload changed')
                        path.rename(folder/name)
                    if mode=='baseline':
                        with Release(folder/'old.zip',progress) as release:
                            index=snapshot(release)
                        # This validates only the clean package identity, never
                        # reads live user file contents into the baseline.
                        probe=folder/'baseline.json';atomic_json(probe,index)
                        current(probe,self.engine.identity())
                        atomic_json(self.baseline,index)
                        atomic_json(self.job,{'status':'complete','mode':mode,'baseline_version':index['release']['meta']['image_version']})
                        return
                    if mode=='current':
                        current(self.baseline,self.engine.identity())
                        old=self.baseline
                    else:
                        old=folder/'old.zip'
                    result=analyze(old,folder/'new.zip',folder/'update.ota',progress,
                                   base_index_output=folder/'base.json' if mode=='pair' else None)
                    with Patch(folder/'update.ota') as patch:
                        summary={'id':patch.id,'base_version':patch.manifest['base_version'],
                                 'target_version':patch.manifest['target_version'],'files':result['files'],
                                 'bytes':result['archive_bytes'],'added':result['added'],'removed':result['removed']}
                    if mode=='pair':
                        try:
                            index=current(folder/'base.json',self.engine.identity())
                        except ValueError:
                            pass # Any compatible old/new pair may still be exported.
                        else:
                            atomic_json(self.baseline,index)
                    os.replace(folder/'update.ota',self.output)
                    atomic_json(self.job,dict(status='complete',mode=mode,**summary))
            except BaseException as error:
                atomic_json(self.job,{'status':'failed','mode':mode,'error':str(error)[-2000:]})

    def inspect_generated(self):
        with (self.state/'job.lock').open('a') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            value=json.loads(self.job.read_text())
            if value.get('status')!='complete' or not value.get('id'):
                raise ValueError('Generate a patch before inspecting it')
            with Patch(self.output) as patch:
                if patch.id!=value['id']:
                    raise ValueError('Generated patch changed; generate it again')
                return self.engine.inspect(patch)


def prepare_dependencies():
    # Source-only pinned dependencies; never use pip or load a host ELF on-device.
    home=Path(__file__).with_name('generator')
    archive=home/'extfs-sources.zip'
    expected=json.loads((home/'extfs-sources.json').read_text())['sha256']
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=expected:
        raise ValueError('Patch generator ext4 reader checksum mismatch')
    sys.path.insert(0,str(archive))


if __name__=='__main__':
    if len(sys.argv)!=5 or sys.argv[1]!='_worker':
        raise SystemExit('Invalid generator worker invocation')
    Builder(sys.argv[4]).run(sys.argv[2],int(sys.argv[3]))
