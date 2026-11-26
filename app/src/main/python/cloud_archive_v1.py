"""Native-enabled save-before-replace history outbox; never a remote backup acknowledgement.

Only Android Application installs a root. Offline tools/tests retain their original
behaviour. The immutable bytes and source metadata are picked up by CloudRecovery,
then evicted only after a cloud checkpoint and history-index readback succeeds.
"""
import hashlib
import json
import os
import re
import shutil
import tempfile

_root = None
DIRECTORY = 'cloud_history_outbox_v1'
_secret = re.compile(r'(^|[_-])(token|auth_token|access_token|refresh_token|password|secret|api_key|apikey|sandbox_token|authorization|cookie|bearer)([_-]|$)', re.I)


def install(files_dir):
    global _root
    _root = os.path.realpath(files_dir) if files_dir else None


def preserve(path):
    if _root is None or not os.path.isfile(path):
        return
    source = os.path.realpath(path)
    if os.path.commonpath([_root, source]) != _root:
        raise OSError('ARCHIVE_PATH_OUTSIDE_APP')
    relative = os.path.relpath(source, _root).replace(os.sep, '/')
    parts = relative.split('/')
    if parts[0] in ('chaquopy', 'forensics', DIRECTORY) or any(_secret.search(p) for p in parts):
        return
    directory = os.path.join(_root, DIRECTORY)
    os.makedirs(directory, exist_ok=True)
    if shutil.disk_usage(directory).free < os.path.getsize(source) + 4 * 1024 * 1024:
        raise OSError('PENDING_ARCHIVE_DISK_FULL')
    fd, temp = tempfile.mkstemp(prefix='archive_', suffix='.tmp', dir=directory)
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, 'wb') as out, open(source, 'rb') as inp:
            while True:
                block = inp.read(32768)
                if not block:
                    break
                digest.update(block)
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        content_hash = digest.hexdigest()
        name = hashlib.sha256(relative.encode()).hexdigest()[:16] + '_' + content_hash
        payload = os.path.join(directory, name + '.bin')
        if os.path.exists(payload):
            os.unlink(temp)
        else:
            os.replace(temp, payload)
        metadata = os.path.join(directory, name + '.json')
        if not os.path.exists(metadata):
            with open(metadata + '.tmp', 'w') as out:
                json.dump({'source_path': relative, 'sha256': content_hash, 'bytes': os.path.getsize(payload)}, out, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            os.replace(metadata + '.tmp', metadata)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
