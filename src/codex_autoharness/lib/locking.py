"""Reentrant advisory locks shared by hooks, workers and management commands.

The process mutex also serializes local threads. A transaction taking both roots
must acquire them together through lock_roots, before taking any individual lock.
"""
import fcntl
import os
import threading
from contextlib import ExitStack, contextmanager

from codex_autoharness.lib import layer

_mutex = threading.RLock()
_held = {}


@contextmanager
def lock_root(root):
    root = layer.checked_root(root)
    key = (os.getpid(), str(root))
    with _mutex:
        if key in _held:
            yield
            return
        directory = layer.state_dir(layer.PROJECT, root)
        directory.mkdir(parents=True, exist_ok=True)
        path = layer.checked_path(root, "codex-autoharness", ".lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _held[key] = fd
            try:
                yield
            finally:
                _held.pop(key, None)
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def lock_roots(roots=None):
    roots = roots or {}
    paths = {layer.checked_root(roots.get(lyr) or layer.default_root(lyr))
             for lyr in layer.LAYERS}
    with ExitStack() as stack:
        for path in sorted(paths, key=str):
            stack.enter_context(lock_root(path))
        yield
