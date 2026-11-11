"""Non-secret source identity embedded in the APK and checked after assembly."""
import hashlib
import json


def build_info(revision, sources):
    if len(revision)!=40 or any(c not in '0123456789abcdef' for c in revision):
        raise ValueError('Android build requires a Git revision')
    encoded=json.dumps(sources,sort_keys=True,separators=(',',':')).encode()
    return {'revision':revision,'source_sha256':hashlib.sha256(encoded).hexdigest()}
