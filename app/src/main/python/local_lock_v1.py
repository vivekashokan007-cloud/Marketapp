"""Exclusive inter-thread + inter-process lock for local app-private stores (Delivery A R5 corrections).

POSIX advisory lock (fcntl.flock) on a lock file, plus a process-local threading.Lock so threads of one
process also serialize (flock is per open-file-description).  Bounded wait; never blocks forever.
"""
from __future__ import annotations

import os
import threading
import time

try:
    import fcntl  # available on Linux/Android (Chaquopy)
except Exception:  # pragma: no cover - non-POSIX
    fcntl = None

_THREAD_LOCKS = {}
_GUARD = threading.Lock()


class LockTimeout(Exception):
    pass


def _thread_lock(path):
    with _GUARD:
        lk = _THREAD_LOCKS.get(path)
        if lk is None:
            lk = _THREAD_LOCKS[path] = threading.Lock()
        return lk


class FileLock:
    def __init__(self, path, timeout_s=5.0, poll_s=0.01):
        self.path = os.path.abspath(path)
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self._fd = None
        self._tl = _thread_lock(self.path)

    def __enter__(self):
        deadline = time.monotonic() + self.timeout_s
        if not self._tl.acquire(timeout=max(0.0, self.timeout_s)):
            raise LockTimeout(self.path)
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            if fcntl is None:
                return self
            while True:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockTimeout(self.path)
                    time.sleep(self.poll_s)
        except BaseException:
            self._release()
            raise

    def _release(self):
        try:
            if self._fd is not None:
                if fcntl is not None:
                    try:
                        fcntl.flock(self._fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(self._fd)
        finally:
            self._fd = None
            try:
                self._tl.release()
            except RuntimeError:
                pass

    def __exit__(self, *exc):
        self._release()
        return False
