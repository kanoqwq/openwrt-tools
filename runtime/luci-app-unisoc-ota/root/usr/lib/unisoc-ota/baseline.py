"""Clean release indexes are metadata, never snapshots of a user's live files."""
import hashlib
import os
from pathlib import Path
import time

from generator.release_index import validate, read_index


def validate_target(index, manifest):
    validate(index)
    release = index['release']
    if (release['family'] != manifest['family'] or
            release['meta']['profile'] != manifest['profile'] or
            release['meta']['image_version'] != manifest['target_version'] or
            release['meta']['kernel_release'] != manifest['kernel_release']):
        raise ValueError('Target release index does not match the patch')


def current(path, identity):
    data = read_index(path)
    release = data['release']
    if (release['family'], release['meta']['profile'], release['meta']['image_version']) != (
            identity['family'], identity['profile'], identity['version']) or release['meta']['kernel_release']!=identity.get('kernel_release'):
        raise ValueError('Saved release baseline does not match the current version')
    return data


def backup(state, folder, journal):
    path = state/'baseline.json'
    journal['baseline_before'] = None
    if path.is_file() and not path.is_symlink():
        raw = path.read_bytes()
        (folder/'baseline.json').write_bytes(raw)
        with (folder/'baseline.json').open('rb') as stream:
            os.fsync(stream.fileno())
        journal['baseline_before'] = hashlib.sha256(raw).hexdigest()


def restore(state, folder, journal):
    if 'baseline_before' not in journal:
        return
    target = state/'baseline.json'
    if journal['baseline_before'] is None:
        target.unlink(missing_ok=True)
        return
    raw = (folder/'baseline.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != journal['baseline_before']:
        raise ValueError('Release baseline recovery checksum mismatch')
    temp = state/('.baseline-'+str(time.time_ns()))
    try:
        with temp.open('xb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,target)
    finally:
        temp.unlink(missing_ok=True)
