"""Read either a verified OTA or the single OTA inside a distribution ZIP.

Only the selected member is copied. Host executables and unrelated archive
paths are never extracted. The caller retains the original upload until the
normal content-addressed review/apply handoff has completed.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile

from engine import FORMAT, MAX_BYTES, MAX_FILES, MAX_MANIFEST


@contextmanager
def payload_stream(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('Invalid or excessive OTA upload')
    with path.open('rb') as source, zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES + 1 or len({i.filename for i in infos}) != len(infos):
            raise ValueError('Duplicate/excessive ZIP entries')
        manifest = next((i for i in infos if i.filename == 'manifest.json'), None)
        direct = False
        if manifest is not None and manifest.file_size <= MAX_MANIFEST:
            try:
                direct = json.loads(archive.read(manifest)).get('format') == FORMAT
            except (ValueError, AttributeError, UnicodeError):
                pass
        if direct:
            source.seek(0)
            yield source, os.fstat(source.fileno()).st_size, None
            return
        matches = [i for i in infos if not i.is_dir() and i.filename.lower().endswith('.ota')]
        if not matches:
            raise ValueError('ZIP contains no OTA file; use full firmware ZIPs in Generate patch')
        if len(matches) != 1:
            raise ValueError('ZIP contains multiple OTA files; select one OTA file')
        member = matches[0]
        name = PurePosixPath(member.filename)
        if (name.is_absolute() or '..' in name.parts or '\\' in member.filename or
                ':' in member.filename or stat.S_ISLNK(member.external_attr >> 16) or
                member.flag_bits & 1 or not 0 < member.file_size <= MAX_BYTES):
            raise ValueError('Unsafe, encrypted or excessive OTA member')
        with archive.open(member) as stream:
            yield stream, member.file_size, member.filename


def copy_payload(path, destination):
    destination = Path(destination)
    created = False
    try:
        with payload_stream(path) as (source, length, member):
            if shutil.disk_usage(destination.parent).free < length + (16 << 20):
                raise ValueError('Insufficient OTA staging space')
            with destination.open('xb') as target:
                created = True
                left = length
                while left:
                    block = source.read(min(left, 1 << 20))
                    if not block:
                        raise ValueError('Truncated OTA upload')
                    target.write(block)
                    left -= len(block)
                if source.read(1):
                    raise ValueError('OTA upload size changed')
                target.flush()
                os.fsync(target.fileno())
        return member
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


@contextmanager
def ota_payload(path):
    # A direct OTA needs no additional copy; wrapper ZIPs require a seekable
    # inner archive for the existing verifier, with bounded persistent storage.
    with payload_stream(path) as (_, _, member):
        pass
    if member is None:
        yield Path(path)
        return
    descriptor, name = tempfile.mkstemp(prefix='.import-ota-', suffix='.ota', dir=Path(path).parent)
    os.close(descriptor)
    temporary = Path(name)
    temporary.unlink()
    try:
        copy_payload(path, temporary)
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)
