"""Event-started clean-release comparison. No live-root diff or system writes."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import time
import re
import shutil

from engine import Engine, Patch, atomic_json, sync_dir


def read_record(path, limit=65536):
    with Path(path).open('rb') as stream:
        raw=stream.read(limit+1)
    if len(raw)>limit:
        raise ValueError('Generation metadata is too large')
    value=json.loads(raw)
    if not isinstance(value,dict):
        raise ValueError('Invalid generation metadata')
    return value


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
        self.result = self.home/'output.json'

    def baseline_status(self):
        from baseline import current
        try:
            identity = self.engine.identity()
            stamp = self.baseline.lstat()
            if not stat.S_ISREG(stamp.st_mode):
                return {'available':False,'reason':'invalid','error':'Saved baseline is not a regular file'}
            cache = self.home/'baseline-status.json'
            key = [stamp.st_dev,stamp.st_ino,stamp.st_size,stamp.st_mtime_ns,stamp.st_ctime_ns,identity]
            try:
                saved=read_record(cache)
                cached=saved.get('value')
                if (saved.get('key') == key and isinstance(cached,dict) and cached.get('available') is True
                        and cached.get('version')==identity['version'] and cached.get('profile')==identity['profile']):
                    return cached
            except (OSError,ValueError):
                pass  # A disposable cache must never block a valid baseline.
            value = current(self.baseline,identity)
            result = {'available':True,'version':value['release']['meta']['image_version'],
                      'profile':value['release']['meta']['profile']}
            try:
                atomic_json(cache,{'key':key,'value':result})
            except OSError:
                pass  # Baseline validity does not depend on writable cache space.
            return result
        except FileNotFoundError as error:
            return {'available':False,'reason':'missing','error':str(error)}
        except (OSError,ValueError,KeyError) as error:
            reason='mismatch' if str(error).startswith('Saved release baseline does not match') else 'invalid'
            return {'available':False,'reason':reason,'error':str(error)}

    def job_status(self):
        try:
            value=read_record(self.job)
            if value.get('status') not in ('idle','running','complete','failed'):
                raise ValueError('Invalid generation task state')
            return value
        except FileNotFoundError:
            return {'status':'idle'}
        except (OSError,ValueError):
            return {'status':'failed','error':'Generation task record is damaged; upload files and start again'}

    def output_summary(self, stamp=None):
        if stamp is None:
            try: stamp=self.output.lstat()
            except FileNotFoundError: return None
        if not stat.S_ISREG(stamp.st_mode): return None
        try:
            summary=read_record(self.result)
            if summary.get('output_id')!=self.output_id(stamp): summary=None
        except (OSError,ValueError):
            summary=None
        if summary is None:
            # Import a previous version's successful result before its task
            # record is replaced by another generation or baseline repair.
            old=self.job_status()
            summary=old if old.get('status')=='complete' else None
        if (not isinstance(summary,dict) or not isinstance(summary.get('id'),str)
                or not re.fullmatch(r'[a-f0-9]{64}',summary['id'])):
            return None
        if any(not isinstance(summary.get(k),str) or not summary[k] or len(summary[k])>256 for k in ('base_version','target_version')):
            return None
        return {key:summary[key] for key in ('id','base_version','target_version','files','bytes','added','removed','task_id') if key in summary}

    def retain_output(self):
        summary=self.output_summary()
        if summary:
            atomic_json(self.result,dict(summary,output_id=self.output_id(self.output.stat())))

    def clean_interrupted_staging(self):
        # Only directories carrying our own marker are disposable. Do not touch
        # uploads, results, indexes, transaction backups or arbitrary siblings.
        for folder in self.home.glob('.build-*'):
            if folder.is_symlink() or not folder.is_dir(): continue
            try:
                owner=read_record(folder/'owner.json',1024)
                if (owner.get('owner')!='unisoc-ota-builder' or not isinstance(owner.get('task_id'),str)
                        or not re.fullmatch(r'[a-f0-9]{32}',owner['task_id'])): continue
            except (OSError,ValueError):
                continue
            shutil.rmtree(folder)

    def status(self):
        value = self.job_status()
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
        try:
            stamp=self.output.lstat()
            if not stat.S_ISREG(stamp.st_mode): stamp=None
        except FileNotFoundError:
            stamp=None
        available=stamp is not None
        summary=self.output_summary(stamp) if available else None
        return {'job':value,'baseline':self.baseline_status(),'uploads':uploads,
                'has_output':available,'output_bytes':stamp.st_size if stamp else 0,
                'output_id':self.output_id(stamp) if stamp else None,
                'output':summary,'download':available}

    @staticmethod
    def output_id(stamp):
        return hashlib.sha256(str((stamp.st_dev,stamp.st_ino,stamp.st_size,stamp.st_mtime_ns)).encode()).hexdigest()

    def discard_generated(self, expected):
        # The same admission lock covers generation, inspection and application.
        # Only this fixed output is disposable; baseline and recovery stay intact.
        with (self.state/'job.lock').open('a') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            journal=self.state/'journal.json'
            if journal.exists() and json.loads(journal.read_text()).get('status') in ('preparing','applying','rolling_back'):
                raise ValueError('Update recovery is pending')
            if self.output.is_symlink():
                raise ValueError('Unsafe generated patch path')
            if self.output.exists() and self.output_id(self.output.stat())!=expected:
                raise ValueError('Generated patch changed; refresh before deleting it')
            self.output.unlink(missing_ok=True)
            self.result.unlink(missing_ok=True)
            sync_dir(self.home)
        return self.status()

    def start(self, mode):
        if mode not in ('current','pair','baseline'):
            raise ValueError('Invalid patch generation mode')
        with (self.state/'job.lock').open('a') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            recovery=self.engine.status().get('recovery') or {}
            if recovery.get('pending'):
                raise ValueError('Update recovery is pending')
            self.clean_interrupted_staging()
            required = ('old.zip',) if mode=='baseline' else ('new.zip',) if mode=='current' else ('old.zip','new.zip')
            for name in required:
                path=self.home/name
                if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 2 << 30:
                    raise ValueError('Select and upload the required full firmware ZIP: '+name)
            if mode=='current' and not self.baseline_status()['available']:
                raise ValueError('Import the current version full ZIP once to establish its clean baseline')
            self.retain_output()
            task_id=os.urandom(16).hex()
            atomic_json(self.job,{'status':'running','phase':'queued','mode':mode,'task_id':task_id})
            try:
                subprocess.Popen([sys.executable,__file__,'_worker',mode,str(held.fileno()),str(self.engine.root)],
                                 stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                 start_new_session=True,pass_fds=(held.fileno(),))
            except BaseException as error:
                atomic_json(self.job,{'status':'failed','mode':mode,'task_id':task_id,'error':str(error)})
                raise
        return {'status':'running','mode':mode,'phase':'queued','task_id':task_id}

    def run(self, mode, lock_fd):
        # Inherited lock makes generation mutually exclusive with apply/rollback
        # without holding APK/package-manager locks while hashing release files.
        with os.fdopen(lock_fd,'a'):
            task_id=self.job_status().get('task_id') or os.urandom(16).hex()
            try:
                self.retain_output()
                prepare_dependencies()
                from generator.ota_package import Release, analyze
                from generator.release_index import snapshot, read_index
                from baseline import current
                last = 0;last_stage=None
                def progress(event):
                    nonlocal last,last_stage
                    now=time.monotonic()
                    phase=event['phase']
                    key=(event.get('source'),phase,event.get('path') if phase in ('verify','unpack') else None)
                    finished=event.get('total') is not None and event.get('completed')==event['total']
                    if key!=last_stage or now-last>=0.4 or finished:
                        atomic_json(self.job,dict(status='running',mode=mode,task_id=task_id,**event))
                        last=now;last_stage=key
                with tempfile.TemporaryDirectory(prefix='.build-',dir=self.home) as folder:
                    folder=Path(folder)
                    atomic_json(folder/'owner.json',{'owner':'unisoc-ota-builder','task_id':task_id})
                    # Release's E5 scratch image stays beside this uploaded
                    # ZIP on persistent storage, never in RAM-backed /tmp.
                    required=('old.zip',) if mode=='baseline' else ('new.zip',) if mode=='current' else ('old.zip','new.zip')
                    for name in required:
                        path=self.home/name
                        if path.is_symlink() or not path.is_file():
                            raise ValueError('Full firmware upload changed')
                        path.rename(folder/name)
                    if mode=='baseline':
                        with Release(folder/'old.zip',details=progress) as release:
                            progress({'phase':'save'})
                            index=snapshot(release)
                        # This validates only the clean package identity, never
                        # reads live user file contents into the baseline.
                        probe=folder/'baseline.json';atomic_json(probe,index)
                        current(probe,self.engine.identity())
                        atomic_json(self.baseline,index)
                        atomic_json(self.job,{'status':'complete','mode':mode,'task_id':task_id,'baseline_version':index['release']['meta']['image_version']})
                        return
                    if mode=='current':
                        current(self.baseline,self.engine.identity())
                        old=self.baseline
                    else:
                        old=folder/'old.zip'
                    result=analyze(old,folder/'new.zip',folder/'update.ota',
                                   base_index_output=folder/'base.json' if mode=='pair' else None,details=progress)
                    progress({'phase':'validate'})
                    with Patch(folder/'update.ota') as patch:
                        summary={'id':patch.id,'task_id':task_id,'base_version':patch.manifest['base_version'],
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
                    atomic_json(self.result,dict(summary,output_id=self.output_id(self.output.stat())))
                    atomic_json(self.job,dict(status='complete',mode=mode,**summary))
            except BaseException as error:
                atomic_json(self.job,{'status':'failed','mode':mode,'task_id':task_id,'error':str(error)[-2000:]})

    def inspect_generated(self):
        with (self.state/'job.lock').open('a') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            value=self.output_summary()
            if self.output.is_symlink() or not self.output.is_file():
                raise ValueError('Generate a patch before inspecting it')
            with Patch(self.output) as patch:
                if value and patch.id!=value['id']:
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
