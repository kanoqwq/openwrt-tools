#!/usr/bin/python3
"""Fixed RPC surface. Detached worker survives browser/SSH disconnection."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import re
import shutil
import time

from engine import Engine, Patch, atomic_json, prepare_runtime
from boot import arm_next_linux
from uploads import copy_payload, ota_payload

ROOT = os.environ.get('UNISOC_OTA_ROOT', '')
ENGINE = str(Path(__file__).with_name('engine.py'))
STATE = Path(ROOT+'/etc/unisoc-ota')
UPLOAD = STATE / 'upload.ota'
JOB = STATE / 'job.json'
METHODS = {'status': {}, 'inspect': {}, 'start': {'id':''}, 'rollback_check': {}, 'rollback': {}, 'discard_recovery': {}, 'reboot': {},
           'build_status': {}, 'generate': {'mode':''}, 'inspect_generated': {}, 'start_generated': {'id':''},
           'inspect_begin': {'source':''}, 'inspect_status': {'ticket':''}, 'discard_generated': {'id':''}}


def inspection_summary(result):
    """Bound ubus replies, not verification. Large release indexes stay local.

    ubusd disconnects a client which sends a message over its 1 MiB limit;
    returning a full clean-release index can therefore remove rpcd's session
    object as well as the OTA object. Legacy LuCI needs operations.length only.
    CLI/desktop inspection continues to receive the full original result.
    """
    manifest = result['manifest']
    summary = {key:result[key] for key in ('id','applicable','required_bytes','free_bytes','boot_work') if key in result}
    summary['manifest'] = {key:str(manifest[key])[:1024] for key in
                           ('family','profile','base_version','target_version','kernel_release') if key in manifest}
    summary['manifest']['operations'] = [None] * len(manifest['operations'])
    if manifest.get('boot'): summary['manifest']['boot'] = True
    for key in ('conflicts','package_conflicts'):
        items = result.get(key, [])
        summary[key] = [str(item)[:2048] for item in items[:20]]
        summary[key+'_count'] = len(items)
    summary['preserved_count'] = len(result.get('preserved', []))
    return summary


def inspection_status(ticket=''):
    if not isinstance(ticket, str) or ticket and not re.fullmatch(r'[a-f0-9]{32}', ticket):
        raise ValueError('Invalid inspection ticket')
    record = STATE/'inspection.json'
    value = json.loads(record.read_text()) if record.exists() else {'status':'idle'}
    if ticket and value.get('ticket') != ticket:
        raise ValueError('Inspection was replaced; inspect this patch again')
    if value.get('status') == 'running':
        with (STATE/'job.lock').open('a') as held:
            try:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                value.update(status='failed', error='Patch inspection was interrupted; no update started')
                atomic_json(record, value)
    return value


def begin_inspection(source):
    if source not in ('upload', 'generated'):
        raise ValueError('Invalid inspection source')
    path = UPLOAD if source == 'upload' else STATE/'builder/update.ota'
    with (STATE/'job.lock').open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = STATE/'journal.json'
        if journal.exists() and json.loads(journal.read_text()).get('status') in ('preparing','applying','rolling_back'):
            raise ValueError('Update recovery is pending')
        if path.is_symlink() or not path.is_file():
            raise ValueError('Upload or generate a patch before inspecting it')
        expected = None
        if source == 'generated':
            job = json.loads((STATE/'builder/job.json').read_text())
            if job.get('status') != 'complete' or not re.fullmatch(r'[a-f0-9]{64}', job.get('id','')):
                raise ValueError('Generate a patch before inspecting it')
            expected = job['id']
        ticket = os.urandom(16).hex()
        value = dict(status='running', ticket=ticket, source=source, phase='queued')
        atomic_json(STATE/'inspection.json', value)
        try:
            # Return before any unpacking, blob hashing, or live file checks.
            # These can exceed both the HTTP and rpcd executable timeouts.
            subprocess.Popen([sys.executable, __file__, '_inspect', source, ticket,
                              expected or '', str(held.fileno())], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, pass_fds=(held.fileno(),))
        except BaseException as error:
            atomic_json(STATE/'inspection.json', dict(value, status='failed', error=str(error)))
            raise
        return value


def inspect_worker(source, ticket, expected, lock_fd):
    with os.fdopen(lock_fd, 'a'):
        value = dict(status='running', source=source, ticket=ticket)
        record = STATE/'inspection.json'
        path = UPLOAD if source == 'upload' else STATE/'builder/update.ota'
        try:
            before = path.stat()
            atomic_json(record, dict(value, phase='verifying'))
            with ota_payload(path) as payload, Patch(payload) as patch:
                if expected and patch.id != expected:
                    raise ValueError('Generated patch changed; generate it again')
                atomic_json(record, dict(value, phase='checking'))
                result = inspection_summary(Engine().inspect(patch))
            after = path.stat()
            if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
                raise ValueError('Upload changed during inspection; inspect this patch again')
            atomic_json(record, dict(value, status='complete', result=result))
        except BaseException as error:
            atomic_json(record, dict(value, status='failed', error=str(error)[-2000:]))


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() or None
    except OSError:
        return None


def mounted(path, table='/proc/self/mountinfo'):
    """Bind mounts can share st_dev with their parent; read the kernel table."""
    wanted = str(Path(path).resolve())
    with open(table) as stream:
        for line in stream:
            fields = line.split()
            if len(fields) < 6: continue
            point = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4])
            if point == wanted: return True
    return False


def rpc_object_id(prefix):
    try:
        result = subprocess.run(prefix+['ubus', '-v', 'list', 'unisoc-ota'],
                                capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired:
        return None
    match = re.search(r"^'unisoc-ota' @([0-9a-fA-F]+)\s*$", result.stdout, re.M)
    return match[1].lower() if not result.returncode and match else None


def activate_rpc():
    """Reload executable plugins through rpcd's session-preserving SIGHUP."""
    root = Path(ROOT or '/')
    if not (root/'etc/init.d/rpcd').is_file():
        return {'status': 'deferred', 'error': 'rpcd service is unavailable'}
    prefix = ['/ota/busybox', 'chroot', str(root)] if os.environ.get('UNISOC_OTA_RAM') else []
    try:
        if prefix:
            # A bind of / does not include its tmpfs/proc submounts. rc.common
            # needs the real /var -> /tmp locks and ubus socket, not empty
            # directories from the disk root. PID 1 remains the host init in
            # this RAM chroot (no PID namespace is created).
            for name in ('tmp', 'proc'):
                destination = str(root/name)
                if not mounted(destination):
                    (root/name).mkdir(exist_ok=True)
                    subprocess.run(['/ota/busybox', 'mount', '--bind',
                                    '/proc/1/root/'+name, destination], check=True,
                                   capture_output=True, text=True, timeout=5)
        before = rpc_object_id(prefix)
        reload = subprocess.run(prefix+['/etc/init.d/rpcd', 'reload'], capture_output=True, text=True, timeout=15)
        if reload.returncode:
            raise ValueError((reload.stderr or reload.stdout)[-1000:])
        deadline = time.monotonic()+5
        while True:
            # procd acknowledges the signal asynchronously. An immediate
            # successful call can still come from the old, soon-to-disappear
            # object; require the replacement registration before health-checking.
            current = rpc_object_id(prefix)
            if current and current != before:
                try:
                    probe = subprocess.run(prefix+['/bin/sh', '-c', 'ubus call unisoc-ota status'], capture_output=True, text=True, timeout=3)
                except subprocess.TimeoutExpired:
                    probe = None
                if probe is not None and not probe.returncode:
                    data = json.loads(probe.stdout)
                    if 'identity' in data and not data.get('error') and rpc_object_id(prefix) == current:
                        return {'status': 'ready'}
            if time.monotonic() >= deadline:
                raise ValueError('unisoc-ota RPC did not register after reload')
            time.sleep(.1)
    except Exception as error:
        # Files are already committed. A reload error must not be reported as
        # a failed filesystem transaction or trigger an unrelated rollback.
        return {'status': 'warning', 'error': str(error)}


def stage_upload(path, expected_id, consume=True):
    """Freeze the reviewed content once, under the inherited admission lock."""
    candidate = STATE / ('candidate-' + str(time.time_ns()) + '.ota')
    source = Path(path)
    before = source.stat()
    try:
        copy_payload(source, candidate)
        with Patch(candidate) as patch:
            if patch.id != expected_id:
                raise ValueError('Upload differs from the patch you reviewed; inspect again')
        frozen = STATE / ('reviewed-' + expected_id + '.ota')
        os.replace(candidate, frozen)
        after = source.stat()
        if consume and (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size) == (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size):
            source.unlink()
        return frozen
    finally:
        candidate.unlink(missing_ok=True)


def host_path(path):
    """Convert a path in the current RAM chroot to its real-root pathname."""
    return '/' + Path(path).relative_to(ROOT or '/').as_posix()


def prepare_apply(path, expected_id, lock_fd):
    """Prepare asynchronously, then exec the isolated worker without dropping its lock.

    Bootstrap's inspect command is supported by existing patches and performs
    no writes. Enter _worker directly afterwards: the reviewed ID is already
    verified, so it cannot be lost by re-entering CLI start with upload.ota.
    """
    os.set_inheritable(lock_fd, True)
    def phase(name):
        atomic_json(JOB, dict(status='running', command='apply', patch_id=expected_id, phase=name))
    try:
        phase('staging')
        frozen = stage_upload(path, expected_id, consume=Path(path).parent != STATE/'builder')
        phase('runtime')
        # A bind of /target excludes the host's tmpfs submount. Use PID 1's
        # real tmp directly, including on the very first desktop bootstrap.
        ram = bool(os.environ.get('UNISOC_OTA_RAM'))
        runtime_root = '/proc/1/root' if ram else ROOT
        with Patch(frozen) as patch:
            home = prepare_runtime(patch, root=runtime_root)
        prefix = ['/ota/busybox', 'chroot', '/proc/1/root'] if ram else []
        real_home = '/' + home.relative_to(runtime_root or '/').as_posix()
        real_patch = host_path(frozen)
        result = subprocess.run(prefix + [real_home+'/busybox', 'sh', real_home+'/bootstrap.sh',
                                real_patch, 'inspect', real_home], capture_output=True, text=True)
        if result.returncode:
            raise ValueError(result.stderr or result.stdout)
        check = json.loads(result.stdout)
        if not check.get('applicable'):
            raise ValueError('File conflict or insufficient space')
        # Only publish the fully prepared runtime. All subsequent status calls
        # use it, while this same job lock survives the final exec/chroot.
        (STATE/'active-runtime').write_text(real_home+'/root\n')
        phase('applying')
        env = dict(os.environ, UNISOC_OTA_ROOT='/target', UNISOC_OTA_RAM='/',
                   UNISOC_OTA_LIVE='1', PYTHONHOME='/usr')
        argv = prefix + [real_home+'/busybox', 'chroot', real_home+'/root',
                         '/usr/bin/python3', '/ota/rpc.py', '_worker', 'apply', '/target'+real_patch]
        os.execve(argv[0], argv, env)
    except Exception as error:
        atomic_json(JOB, dict(status='failed', command='apply', patch_id=expected_id,
                             result={'error': str(error)}))


def worker(command, path=None, expected_id=None):
    if path:
        # No bulk copy, runtime extraction or filesystem inspection in the
        # HTTP request. The detached child inherits the lock before we reply.
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (STATE/'job.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            atomic_json(JOB, dict(status='running', command=command,
                                  patch_id=expected_id, phase='queued'))
            try:
                subprocess.Popen([sys.executable, __file__, '_prepare_apply', str(path),
                                  expected_id, str(lock.fileno())], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, pass_fds=(lock.fileno(),))
            except Exception as error:
                atomic_json(JOB, dict(status='failed', command=command, patch_id=expected_id,
                                     result={'error': str(error)}))
                raise
        return {'status': 'running', 'patch_id': expected_id, 'phase': 'queued'}
    if command == 'rollback' and not os.environ.get('UNISOC_OTA_RAM'):
        import tempfile
        journal = json.loads((STATE/'journal.json').read_text())
        if journal.get('recovery_deleted'):
            raise ValueError('Recovery copy was deleted')
        runtime = journal.get('runtime', 'runtime')
        if runtime != 'runtime' and not re.fullmatch(r'backup-[0-9]+/runtime', runtime):
            raise ValueError('Invalid transaction recovery runtime')
        source = STATE/runtime
        if not (source/'ota/busybox').is_file():
            raise ValueError('Missing transaction recovery runtime')
        home = Path(tempfile.mkdtemp(prefix='unisoc-ota-rollback-'))
        shutil.copytree(source, home/'root', symlinks=True)
        bb = str(home/'root/ota/busybox')
        for name, mounted in (('target', '/'), ('dev', '/dev'), ('target/dev', '/dev'), ('target/run', '/run'), ('sys', '/sys'), ('target/sys', '/sys')):
            dest = home/'root'/name; dest.mkdir(parents=True, exist_ok=True)
            subprocess.run([bb, 'mount', '--bind', mounted, str(dest)], check=True)
        proc = home/'root/proc'; proc.mkdir(exist_ok=True)
        subprocess.run([bb, 'mount', '-t', 'proc', 'proc', str(proc)], check=True)
        env = dict(os.environ, UNISOC_OTA_ROOT='/target', UNISOC_OTA_RAM='/', UNISOC_OTA_LIVE='1', PYTHONHOME='/usr')
        result = subprocess.run([bb, 'chroot', str(home/'root'), '/usr/bin/python3', '/ota/rpc.py', 'rollback'],
                                env=env, capture_output=True, text=True)
        if result.returncode: raise ValueError(result.stderr or result.stdout)
        return json.loads(result.stdout)
    STATE.mkdir(mode=0o700, exist_ok=True)
    with (STATE / 'job.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Parent holds the lock until a durable running marker is written, then
        # detaches a process which inherits it. Its close releases the lock.
        atomic_json(JOB, {'status': 'running', 'command': command, 'patch_id': None})
        subprocess.Popen([sys.executable, __file__, '_worker', command, path or ''],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True,
                         pass_fds=(lock.fileno(),))
    return {'status': 'running'}


def dispatch(method, path=None, review_id=None, mode=None):
    STATE.mkdir(mode=0o700, exist_ok=True)
    if method == 'inspect_begin': return begin_inspection(mode)
    if method == 'inspect_status': return inspection_status('' if review_id is None else review_id)
    if method in ('build_status','generate','inspect_generated','start_generated','discard_generated'):
        from builder import Builder
        builder=Builder(ROOT or '/')
        if method=='build_status': return builder.status()
        if method=='generate': return builder.start(mode)
        if method=='discard_generated':
            if not isinstance(review_id,str) or not re.fullmatch(r'[a-f0-9]{64}',review_id):
                raise ValueError('Refresh before deleting the generated patch')
            return builder.discard_generated(review_id)
        if method=='inspect_generated': return builder.inspect_generated()
        if not isinstance(review_id,str) or not re.fullmatch(r'[a-f0-9]{64}',review_id):
            raise ValueError('Inspect this patch before applying')
        return worker('apply',str(builder.output),review_id)
    if method == 'status':
        result = Engine().status()
        result['job'] = json.loads(JOB.read_text()) if JOB.exists() else {'status': 'idle'}
        generation=STATE/'builder/job.json'
        try:
            generating=generation.is_file() and json.loads(generation.read_text()).get('status')=='running'
        except (OSError,ValueError):
            generating=False
        if result['job']['status'] == 'complete' and not generating:
            with (STATE/'job.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    # The worker has published completion but still owns its
                    # final cleanup. Do not race it with an automatic reboot.
                    result['job'] = dict(result['job'], status='running')
                    return result
        if result['job']['status'] == 'running':
            with (STATE / 'job.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    transaction = result.get('transaction') or {}
                    complete = (transaction.get('status') == 'complete' and transaction.get('id') == result['job'].get('patch_id')) if result['job'].get('command') == 'apply' else transaction.get('status') == 'rolled_back'
                    result['job'] = dict(result['job'], status='complete' if complete else 'failed',
                                         result={'error': 'Interrupted transaction; recovery status: ' + transaction.get('status', 'unknown')} if not complete else {})
                    atomic_json(JOB, result['job'])
        completed = result['job'].get('completed_boot_id')
        current = boot_id()
        # Legacy records have no boot ID: report completion without guessing
        # that another reboot is needed from a persistent success flag.
        result['reboot_required'] = (completed == current) if result['job']['status'] == 'complete' and completed and current else None
        return result
    if method == 'inspect':
        with ota_payload(UPLOAD) as payload, Patch(payload) as patch:
            return Engine().inspect(patch)
    if method == 'start':
        path = Path(path) if path else UPLOAD
        # CLI may select only a pre-reviewed, content-addressed upload on the
        # same persistent filesystem. rpcd callers cannot supply arbitrary paths.
        if path.parent != STATE or path.is_symlink() or path.suffix != '.ota':
            raise ValueError('Invalid OTA upload path')
        if review_id is None:
            matched = re.fullmatch(r'upload-([a-f0-9]{64})\.ota', path.name)
            review_id = matched[1] if matched else None
        if not isinstance(review_id,str) or not re.fullmatch(r'[a-f0-9]{64}',review_id):
            raise ValueError('Inspect this patch before applying')
        return worker('apply', str(path), review_id)
    if method == 'rollback':
        return worker('rollback')
    if method == 'rollback_check':
        return Engine().rollback_check()
    if method == 'discard_recovery':
        with (STATE/'job.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return Engine().discard_recovery()
    if method == 'reboot':
        boot_lock=Path(ROOT or '/')/'run/openwrt-default-boot.lock'
        boot_lock.parent.mkdir(parents=True,exist_ok=True)
        with (STATE / 'job.lock').open('a') as lock, boot_lock.open('a') as selection:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(selection, fcntl.LOCK_EX | fcntl.LOCK_NB)
            journal = STATE / 'journal.json'
            if journal.exists() and json.loads(journal.read_text())['status'] in ('applying', 'rolling_back'):
                raise ValueError('Update recovery is pending')
            arm_next_linux(Path(ROOT or '/'),Engine().identity()['family'])
            reboot = ['/ota/busybox', 'reboot'] if os.environ.get('UNISOC_OTA_RAM') else ['/sbin/reboot']
            # Let rpcd deliver the acknowledgement before networking stops.
            # The child retains the job lock so another update cannot start
            # during this short acknowledgement window. It syncs before reboot.
            code = 'import os,sys,time; time.sleep(2); os.sync(); os.execv(sys.argv[1],sys.argv[1:])'
            subprocess.Popen([sys.executable, '-c', code, *reboot], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, pass_fds=(lock.fileno(),selection.fileno()))
        return {'status': 'rebooting'}
    raise ValueError('Unknown OTA command')


def main():
    if sys.argv[1:2] == ['_inspect']:
        source, ticket, expected, lock_fd = sys.argv[2:6]
        inspect_worker(source, ticket, expected, int(lock_fd))
        return
    if sys.argv[1:2] == ['_prepare_apply']:
        path, expected_id, lock_fd = sys.argv[2:5]
        prepare_apply(path, expected_id, int(lock_fd))
        return
    if sys.argv[1:2] == ['_worker']:
        command, path = sys.argv[2:4]
        args = [sys.executable, ENGINE, command] + ([path] if path else [])
        result = subprocess.run(args, capture_output=True, text=True)
        try:
            detail = json.loads(result.stdout)
        except ValueError:
            detail = {'error': (result.stderr or result.stdout)[-4000:]}
        if result.returncode == 0:
            detail['activation'] = activate_rpc()
        atomic_json(JOB, dict(status='failed' if result.returncode else 'complete',
                              command=command, completed_boot_id=boot_id() if not result.returncode else None,
                              result=detail))
        if path and result.returncode == 0:
            uploaded = Path(path)
            if uploaded.parent == STATE and re.fullmatch(r'reviewed-[a-f0-9]{64}\.ota', uploaded.name):
                uploaded.unlink(missing_ok=True)
        return
    if sys.argv[1:] == ['list']:
        print(json.dumps(METHODS))
        return
    if sys.argv[1:2] == ['call']:
        # Standard rpcd plugins receive arguments via stdin. No dynamic command.
        args = json.loads(sys.stdin.read(4096) or '{}')
        if not isinstance(args, dict):
            raise ValueError('RPC arguments must be an object')
        # rpcd injects the authenticated session for LuCI/HTTP calls. It is
        # transport metadata, not a method argument. ACLs are enforced by rpcd.
        args.pop('ubus_rpc_session', None)
        method, path = sys.argv[2], None
        if method == 'inspect_begin' and set(args) == {'source'}:
            print(json.dumps(dispatch(method, mode=args['source']))); return
        if method == 'inspect_status' and set(args) == {'ticket'}:
            print(json.dumps(dispatch(method, review_id=args['ticket']))); return
        if method in ('start','start_generated','discard_generated') and set(args) == {'id'}:
            review_id = args['id']
        elif method == 'generate' and set(args) == {'mode'}:
            print(json.dumps(dispatch(method,mode=args['mode'])))
            return
        elif args:
            raise ValueError('Unexpected RPC arguments')
        else:
            review_id = None
    else:
        method = sys.argv[1]
        path = sys.argv[2] if len(sys.argv) == 3 or method == 'start' and len(sys.argv) == 4 else None
        review_id = sys.argv[3] if method == 'start' and len(sys.argv) == 4 else None
    result = dispatch(method, path, review_id)
    if sys.argv[1:2] == ['call'] and method in ('inspect','inspect_generated') and 'manifest' in result:
        result = inspection_summary(result)
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(json.dumps({'error': str(e)}))
        sys.exit(1)
