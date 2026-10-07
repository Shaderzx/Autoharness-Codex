"""Append-only per-skill provenance, serialized with the owning root lock.

Each completed intent has a unique identity, making queue replay idempotent.
Evidence entries refer to promoter-generated redacted support files."""
import json
import os

from codex_autoharness.lib import layer
from codex_autoharness.lib.locking import lock_root

FILENAME = ".ledger.jsonl"


def path(lyr, name, root=None):
    layer._check_name(name)
    return layer.checked_path(layer._root(lyr, root), "skills", name, FILENAME)


def append(lyr, name, entry, root=None):
    with lock_root(layer._root(lyr, root)):
        p = path(lyr, name, root)
        p.parent.mkdir(parents=True, exist_ok=True)
        if entry.get("intent_id") and any(row.get("intent_id") == entry["intent_id"] for row in read(lyr, name, root)):
            return
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


def read(lyr, name, root=None, *, archived=False):
    """archived=True reads the ledger that travelled with the symbol into .archive/ (whole-dir
    rename carries it), so retirement provenance stays readable after eviction."""
    layer._check_name(name)
    p = (layer.checked_path(layer._root(lyr, root), "skills", ".archive", name, FILENAME)
         if archived else path(lyr, name, root))
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
