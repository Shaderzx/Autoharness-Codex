"""skill CRUD: atomic SKILL.md write + two-layer find (ambiguity error when the same name spans both layers) + apply delta + archive + orphan .tmp sweep.

Persistence uses atomic (same-dir temp + os.replace) in this one place, so live is never half-written.
find extends Hermes's `_find_skill` to the union of the global+project layers; the same name across
both layers → error (promoter uses this to disambiguate the layer when resolving update/delete).
apply_delta requires old_string to match uniquely (rejects both not-found and multiple-match
ambiguity), a deterministic rebuild. archive atomically moves symbol_dir into `.archive` (preserving
LED/sidecar); landing a delete and MNG (Phase 6) eviction share this one path.
"""
import json
import os
import time

from codex_autoharness.lib import atomic, layer, sidecar
from codex_autoharness.lib.locking import lock_root

SKILL_FILE = "SKILL.md"


def _collision_safe_dest(dest):
    if not dest.exists() and not dest.is_symlink():
        return dest
    suffix = time.strftime("%Y%m%dT%H%M%S")
    candidate = dest.with_name(f"{dest.name}.{suffix}")
    counter = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = dest.with_name(f"{dest.name}.{suffix}.{counter}")
        counter += 1
    return candidate


def skill_path(lyr, name, root=None):
    layer._check_name(name)
    return layer.checked_path(layer._root(lyr, root), "skills", name, SKILL_FILE)


def write_body(lyr, name, body, root=None):
    with lock_root(layer._root(lyr, root)):
        atomic.write_text(skill_path(lyr, name, root), body)


def read_body(lyr, name, root=None):
    p = skill_path(lyr, name, root)
    return p.read_text() if p.exists() else None


def exists(lyr, name, root=None):
    return skill_path(lyr, name, root).exists()


def find(name, roots=None):
    roots = roots or {}
    hits = [lyr for lyr in layer.unique_layers(roots) if exists(lyr, name, roots.get(lyr))]
    if len(hits) > 1:
        raise ValueError(f"ambiguous skill {name!r} present in layers {hits}")
    return hits[0] if hits else None


def apply_delta(body, old_string, new_string):
    if not isinstance(old_string, str) or not old_string or not isinstance(new_string, str):
        raise ValueError("patch requires nonempty old_string and string new_string")
    count = body.count(old_string)
    if count == 0:
        raise ValueError("delta old_string not found in live body")
    if count > 1:
        raise ValueError("delta old_string is ambiguous (multiple matches)")
    return body.replace(old_string, new_string, 1)


def remove(lyr, name, root=None):
    return archive(lyr, name, root)


def archive(lyr, name, root=None):
    with lock_root(layer._root(lyr, root)):
        return _archive(lyr, name, root)


def _archive(lyr, name, root=None):
    sdir = layer.symbol_dir(lyr, name, root)
    if not sdir.exists():
        return None
    if not sidecar.is_agent_created(lyr, name, root):
        raise ValueError("cannot archive an unmanaged skill")
    dest = layer.archive_dir(lyr, root) / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest = _collision_safe_dest(dest)
    os.replace(sdir, dest)
    return dest


def restore(lyr, name, root=None):
    with lock_root(layer._root(lyr, root)):
        return _restore(lyr, name, root)


def _restore(lyr, name, root=None):
    layer._check_name(name)
    src = layer.checked_path(layer._root(lyr, root), "skills", ".archive", name)
    if not src.exists():
        return None
    marker = layer.checked_path(layer._root(lyr, root), "skills", ".archive", name, sidecar.FILENAME)
    try:
        metadata = json.loads(marker.read_text())
    except (OSError, ValueError):
        raise ValueError("cannot restore an unmanaged archive") from None
    if not isinstance(metadata, dict) or metadata.get("created_by") != sidecar.OWNER:
        raise ValueError("cannot restore an unmanaged archive")
    body_path = layer.checked_path(layer._root(lyr, root), "skills", ".archive", name, SKILL_FILE)
    from codex_autoharness.lib import validate
    original = (validate._frontmatter(body_path.read_text()) or {}).get("name")
    dest = layer.symbol_dir(lyr, original, root)
    if dest.exists():
        raise ValueError("restore target already exists; archive it first")
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)
    return dest


def sweep_orphans(lyr, root=None):
    with lock_root(layer._root(lyr, root)):
        return _sweep_orphans(lyr, root)


def _sweep_orphans(lyr, root=None):
    skills = layer.skills_dir(lyr, root)
    if not skills.exists():
        return []
    removed = []
    for directory in skills.iterdir():
        if not sidecar.is_agent_created(lyr, directory.name, root):
            continue
        for tmp in directory.rglob(".codex-autoharness-*.tmp"):
            if tmp.is_symlink():
                continue
            checked = layer.checked_path(layer._root(lyr, root), "skills", directory.name, tmp.relative_to(directory))
            checked.unlink()
            removed.append(checked)
    return removed
