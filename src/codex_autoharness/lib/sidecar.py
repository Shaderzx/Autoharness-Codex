"""Owned-skill metadata and independently counted uses, views, and patches.

Only the codex-autoharness owner marker authorizes management. Root locks protect
read-modify-write counters; a use after a patch records the reused generation."""
import json

from codex_autoharness.lib import atomic, layer
from codex_autoharness.lib.locking import lock_root

FILENAME = ".sidecar.json"
OWNER = "codex-autoharness"


def path(lyr, name, root=None):
    layer._check_name(name)
    return layer.checked_path(layer._root(lyr, root), "skills", name, FILENAME)


def _migrate(data):
    if "calls" in data:
        data["use"] = data.get("use", 0) + data.pop("calls")
    return data


def read(lyr, name, root=None):
    p = path(lyr, name, root)
    if not p.exists():
        return {}
    return _migrate(json.loads(p.read_text()))


def write(lyr, name, data, root=None):
    with lock_root(layer._root(lyr, root)):
        atomic.write_text(path(lyr, name, root), json.dumps(data, ensure_ascii=False, indent=2))


def create(lyr, name, anchor, root=None):
    data = {"created_by": OWNER, "use": 0, "view": 0, "patch": 0,
            "anchor": int(anchor), "verification": None}
    write(lyr, name, data, root)
    return data


def _bump_locked(lyr, name, key, root=None):
    data = read(lyr, name, root)
    data[key] = data.get(key, 0) + 1
    if key == "use" and data.get("patch", 0) > data.get("reused_gen", 0):
        data["reused_gen"] = data["patch"]  # first use after a patch = reuse-after-improvement
    write(lyr, name, data, root)
    return data[key]


def _bump(lyr, name, key, root=None):
    with lock_root(layer._root(lyr, root)):
        return _bump_locked(lyr, name, key, root)


def bump_use(lyr, name, root=None):
    return _bump(lyr, name, "use", root)


def bump_view(lyr, name, root=None):
    return _bump(lyr, name, "view", root)


def bump_patch(lyr, name, root=None):
    return _bump(lyr, name, "patch", root)


def is_agent_created(lyr, name, root=None):
    try:
        layer._check_name(name)
        layer.checked_path(layer._root(lyr, root), "skills", name, "SKILL.md")
        return read(lyr, name, root).get("created_by") == OWNER
    except (OSError, ValueError, AttributeError):
        return False
