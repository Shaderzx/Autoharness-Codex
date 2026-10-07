"""Copy Claude skills into native Codex discovery paths without taking ownership."""
import ctypes
import errno
import os
import shutil
import stat
import sys
import tempfile
import time
from contextlib import contextmanager

from codex_autoharness.lib import layer, ledger, sidecar, skill_store
from codex_autoharness.lib.locking import lock_root


@contextmanager
def _directory(path, *, dir_fd=None):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        yield fd
    finally:
        os.close(fd)


def _check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("startup import time budget exceeded; run import-skills to finish")


def _copy_tree(source, target, *, top=True, deadline=None):
    for name in sorted(os.listdir(source)):
        _check_deadline(deadline)
        if top and name in (sidecar.FILENAME, ledger.FILENAME):
            continue  # Host-specific ownership and accounting never transfer.
        info = os.stat(name, dir_fd=source, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            os.mkdir(name, mode=0o700, dir_fd=target)
            with _directory(name, dir_fd=source) as src, _directory(name, dir_fd=target) as dst:
                _copy_tree(src, dst, top=False, deadline=deadline)
                os.fchmod(dst, info.st_mode & 0o777)
        elif stat.S_ISREG(info.st_mode):
            # NONBLOCK prevents a swapped FIFO from hanging the startup hook.
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source)
            with os.fdopen(fd, "rb") as src:
                mode = os.fstat(src.fileno()).st_mode
                if not stat.S_ISREG(mode):
                    raise ValueError("skill contains a non-regular file")
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=target)
                with os.fdopen(fd, "wb") as dst:
                    while chunk := src.read(1024 * 1024):
                        _check_deadline(deadline)
                        dst.write(chunk)
                    os.fchmod(dst.fileno(), mode & 0o777)
        else:
            raise ValueError("skill contains a symlink or non-regular file")


def _publish(source_fd, name, target_fd):
    """Atomically publish a directory without replacing even an empty user directory."""
    native = ctypes.CDLL(None, use_errno=True)
    # macOS RENAME_EXCL=4; Linux RENAME_NOREPLACE=1.
    function, flag = ("renameatx_np", 4) if sys.platform == "darwin" else ("renameat2", 1)
    rename = getattr(native, function, None)
    if rename is None:
        raise OSError(errno.ENOTSUP, "atomic no-replace import is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    encoded = os.fsencode(name)
    if rename(source_fd, encoded, target_fd, encoded, flag) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def import_layer(lyr, root=None, *, deadline=None):
    root = layer._root(lyr, root)
    result = {"imported": [], "skipped": {}}
    try:
        source = layer.checked_path(root.parent, ".claude", "skills")
        if not source.exists():
            return result
        if not source.is_dir():
            raise ValueError("Claude skills path is not a directory")
        with _directory(source.parent) as parent, _directory(source.name, dir_fd=parent) as src:
            with lock_root(root):
                target = layer.skills_dir(lyr, root)
                target.mkdir(parents=True, exist_ok=True)
                staging = layer.checked_path(root, "codex-autoharness", "imports")
                staging.mkdir(parents=True, exist_ok=True)
                # This root lock excludes every cooperating importer, so leftover stages are interrupted copies.
                for leftover in staging.glob(".claude-import-*"):
                    if leftover.is_dir() and not leftover.is_symlink():
                        shutil.rmtree(leftover)
                with _directory(target) as dst:
                    for name in sorted(os.listdir(src)):
                        try:
                            _check_deadline(deadline)
                            layer._check_name(name)
                            try:
                                os.stat(name, dir_fd=dst, follow_symlinks=False)
                            except FileNotFoundError:
                                pass
                            else:
                                result["skipped"][name] = "destination exists"
                                continue
                            with _directory(name, dir_fd=src) as skill:
                                info = os.stat(skill_store.SKILL_FILE, dir_fd=skill, follow_symlinks=False)
                                if not stat.S_ISREG(info.st_mode):
                                    raise ValueError("SKILL.md is not a regular file")
                                # A killed hook leaves only a hidden staging tree; the next import retries.
                                with tempfile.TemporaryDirectory(prefix=".claude-import-", dir=staging) as stage:
                                    with _directory(stage) as staged:
                                        os.mkdir(name, mode=0o700, dir_fd=staged)
                                        with _directory(name, dir_fd=staged) as copied:
                                            _copy_tree(skill, copied, deadline=deadline)
                                            os.stat(skill_store.SKILL_FILE, dir_fd=copied, follow_symlinks=False)
                                        _publish(staged, name, dst)
                            result["imported"].append(name)
                        except (OSError, ValueError) as exc:
                            result["skipped"][name] = "destination exists" if isinstance(exc, FileExistsError) else str(exc)
    except (OSError, ValueError) as exc:
        result["skipped"]["."] = str(exc)
    return result


def import_skills(roots=None, *, timeout=None):
    roots = roots or {}
    deadline = time.monotonic() + timeout if timeout is not None else None
    return {lyr: import_layer(lyr, roots.get(lyr), deadline=deadline) for lyr in layer.unique_layers(roots)}
