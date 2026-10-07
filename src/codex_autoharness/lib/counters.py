"""Tool-call counters, request opportunities and transcript watermarks.

Read-modify-write mutations share the root advisory lock with worker promotion.
"""
import re

from codex_autoharness.lib import atomic, layer
from codex_autoharness.lib.locking import lock_root

_SAFE_SESSION = re.compile(r"^[A-Za-z0-9_-]+$")


def _read_int(p):
    if p.is_symlink():
        raise ValueError("counter must not be a symlink")
    try:
        return int(p.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def _bump(p, delta=1):
    """Read-modify-write under an exclusive lock so concurrent hook processes
    cannot both read the same value and lose an increment."""
    with lock_root(p.parent.parent):
        value = _read_int(p) + delta
        atomic.write_text(p, str(value))
    return value


def request_count(lyr, root=None):
    return _read_int(layer.state_dir(lyr, root) / "requests")


def bump_request(lyr, root=None):
    return _bump(layer.state_dir(lyr, root) / "requests")


def _session_path(session_id, root=None):
    if not isinstance(session_id, str) or not _SAFE_SESSION.match(session_id):
        raise ValueError(f"unsafe session id: {session_id!r}")
    return layer.state_dir(layer.PROJECT, root) / f"session-{session_id}"


def session_count(session_id, root=None):
    return _read_int(_session_path(session_id, root))


def bump_session(session_id, root=None):
    return _bump(_session_path(session_id, root))


def reset_session(session_id, root=None):
    with lock_root(layer._root(layer.PROJECT, root)):
        atomic.write_text(_session_path(session_id, root), "0")


def clear_session(session_id, root=None):
    with lock_root(layer._root(layer.PROJECT, root)):
        _session_path(session_id, root).unlink(missing_ok=True)


def _offset_path(session_id, root=None):
    if not isinstance(session_id, str) or not _SAFE_SESSION.match(session_id):
        raise ValueError(f"unsafe session id: {session_id!r}")
    return layer.state_dir(layer.PROJECT, root) / f"offset-{session_id}"


def session_offset(session_id, root=None):
    return _read_int(_offset_path(session_id, root))


def write_session_offset(session_id, offset, root=None):
    with lock_root(layer._root(layer.PROJECT, root)):
        atomic.write_text(_offset_path(session_id, root), str(max(0, int(offset))))
