"""Keep untracked, marker-owned runtime files out of project diffs."""
import fcntl
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from codex_autoharness.lib import atomic, layer


def sync(root):
    """Refresh one root's local rules; Git failures must never affect skill storage."""
    try:
        root = layer.checked_root(root)
        if not any((parent / ".git").exists() for parent in (root.parent, *root.parent.parents)):
            return
        proc = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel"],
            cwd=root.parent, capture_output=True, text=True, timeout=5,
        )
        if proc.returncode:
            return
        common, top = map(Path, proc.stdout.splitlines())
        relative = root.relative_to(top.resolve()).as_posix()
        if "\n" in relative or "\r" in relative:
            return
        indexed = subprocess.run(
            ["git", "ls-files", "-z", "--full-name", "--", f":(literal){relative}/skills"],
            cwd=top, capture_output=True, text=True, timeout=5,
        )
        if indexed.returncode:
            return
        tracked = indexed.stdout.split("\0")
        paths = [root / "codex-autoharness"]
        skills = layer.checked_path(root, "skills")
        directories = list(skills.iterdir()) if skills.exists() else []
        archive = layer.checked_path(root, "skills", ".archive")
        if archive.is_dir():
            directories.extend(archive.iterdir())
        for directory in directories:
            if directory.name == ".archive" or not directory.is_dir() or directory.is_symlink():
                continue
            prefix = directory.relative_to(top.resolve()).as_posix() + "/"
            if any(path.startswith(prefix) for path in tracked):
                continue  # a deliberately tracked skill remains entirely visible to its author
            try:
                layer._check_name(directory.name)
                marker = layer.checked_path(root, *directory.relative_to(root).parts, ".sidecar.json")
                body = layer.checked_path(root, *directory.relative_to(root).parts, "SKILL.md")
                data = json.loads(marker.read_text())
                if body.is_file() and isinstance(data, dict) and data.get("created_by") == "codex-autoharness":
                    paths.append(directory)
            except (OSError, ValueError):
                continue
        patterns = ["/" + re.sub(r"([\\*?\[\] #!])", r"\\\1", path.relative_to(top.resolve()).as_posix()) + "/"
                    for path in sorted(paths)]
        key = hashlib.sha256(relative.encode()).hexdigest()[:16]
        start = f"# codex-autoharness {key} begin\n".encode()
        end = f"# codex-autoharness {key} end\n".encode()
        block = start + "\n".join(patterns).encode() + b"\n" + end
        exclude = layer.checked_path(common, "info", "exclude")
        exclude.parent.mkdir(parents=True, exist_ok=True)
        lock = layer.checked_path(common, "info", "codex-autoharness-exclude.lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            existing = exclude.read_bytes() if exclude.exists() else b""
            offset = existing.find(start)
            if offset >= 0:
                finish = existing.find(end, offset + len(start))
                if finish < 0:
                    return  # preserve a malformed or manually edited block
                updated = existing[:offset] + block + existing[finish + len(end):]
            else:
                updated = existing + (b"\n" if existing and not existing.endswith(b"\n") else b"") + block
            if updated != existing:
                mode = exclude.stat().st_mode & 0o777 if exclude.exists() else 0o644
                atomic.write_bytes(exclude, updated)
                exclude.chmod(mode)
        finally:
            os.close(fd)
    except Exception:
        pass
