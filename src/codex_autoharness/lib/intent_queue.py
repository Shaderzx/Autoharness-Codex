"""Durable proposal queues with per-intent identities.

Queue writes are locked and fsynced. Credential-bearing skill content is rejected
and evidence is redacted before persistence. Drains preserve interrupted work."""
import json
import os
import re
import uuid

from codex_autoharness.lib import atomic, layer, redact
from codex_autoharness.lib.locking import lock_root

_SAFE_RUN = re.compile(r"^[A-Za-z0-9_-]+$")


def _safe_intent(intent):
    row = dict(intent)
    contents = [row.get("body", ""), row.get("old_string", ""), row.get("new_string", "")]
    if isinstance(row.get("files"), dict):
        contents.extend(row["files"].values())
    if any(isinstance(value, str) and redact.contains_secret(value) for value in contents):
        raise ValueError("proposal contains a credential pattern")
    for field in ("reason", "evidence", "name"):
        if isinstance(row.get(field), str):
            row[field] = redact.redact(row[field])
    row["_intent_id"] = uuid.uuid4().hex
    return row


def _path(run_id, root=None):
    if not isinstance(run_id, str) or not _SAFE_RUN.match(run_id):
        raise ValueError(f"unsafe run id: {run_id!r}")
    return layer.checked_path(layer._root(layer.PROJECT, root), "codex-autoharness", "intents", f"{run_id}.jsonl")


def append(run_id, intent, root=None):
    with lock_root(layer._root(layer.PROJECT, root)):
        row = _safe_intent(intent)
        p = _path(run_id, root)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


def append_many(run_id, intents, root=None):
    """Publish a fully validated worker response as one durable queue."""
    with lock_root(layer._root(layer.PROJECT, root)):
        path = _path(run_id, root)
        if path.exists():
            raise ValueError("run queue already exists")
        rows = [_safe_intent(intent) for intent in intents]
        atomic.write_text(path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def read(run_id, root=None):
    p = _path(run_id, root)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def clear(run_id, root=None):
    with lock_root(layer._root(layer.PROJECT, root)):
        _path(run_id, root).unlink(missing_ok=True)


def orphans(root=None):
    d = layer.state_dir(layer.PROJECT, root) / "intents"
    if not d.exists():
        return []
    return sorted(f.stem for f in d.glob("*.jsonl"))
