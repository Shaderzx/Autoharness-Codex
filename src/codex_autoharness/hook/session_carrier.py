"""Private, bounded history for dedicated learners; never the user's rollout."""
import fcntl
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime

from codex_autoharness.lib import atomic, layer, redact
from codex_autoharness.lib.locking import lock_root

MAX_HISTORY_BYTES = 2_000_000
MAX_CACHED_SESSIONS = 5


@contextmanager
def cache(root, session_id, identity):
    """Lock the private history entry for one host session and routing identity."""
    key = hashlib.sha256(json.dumps([session_id, identity], sort_keys=True).encode()).hexdigest()
    directory = layer.checked_path(root, "codex-autoharness", "learner-sessions")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    path = layer.checked_path(root, "codex-autoharness", "learner-sessions", f"{key}.json")
    lock = layer.checked_path(root, "codex-autoharness", "learner-sessions", f"{key}.lock")
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield path
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _history(text):
    """Convert a native rollout to redacted messages without inherited authority."""
    records = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not records or any(not isinstance(record, dict) for record in records):
        raise ValueError("invalid learner history")
    first = records[0]
    if first.get("type") != "session_meta" or not isinstance(first.get("payload"), dict):
        raise ValueError("missing learner identity")
    identity, created = first["payload"].get("id"), first["payload"].get("timestamp")
    if not isinstance(identity, str) or not isinstance(created, str):
        raise ValueError("invalid learner identity")
    identity = str(uuid.UUID(identity))
    timestamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("invalid learner timestamp")
    # Native legacy history is self-contained; paginated forks can reference a
    # deleted temporary parent rollout. Retain messages, never persisted tools,
    # capability roots, instructions, approval settings or compaction state.
    meta = {"id": identity, "session_id": identity, "timestamp": first["payload"]["timestamp"],
            "cwd": "/", "originator": "codex-autoharness", "cli_version": "",
            "model_provider": None, "base_instructions": None, "history_mode": "legacy",
            "multi_agent_version": "disabled"}
    safe = [{"timestamp": created, "type": "session_meta", "payload": meta}]
    for record in records[1:]:
        item = record.get("payload")
        if (record.get("type") != "response_item" or not isinstance(item, dict)
                or item.get("type") != "message" or item.get("role") not in {"user", "assistant"}):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            raise ValueError("invalid learner message")
        blocks = [{"type": block["type"], "text": redact.redact(block["text"])} for block in content
                  if isinstance(block, dict) and block.get("type") in {"input_text", "output_text"}
                  and isinstance(block.get("text"), str)]
        if blocks:
            safe.append({"timestamp": created, "type": "response_item",
                         "payload": {"type": "message", "role": item["role"], "content": blocks}})
    rendered = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in safe)
    if len(rendered.encode()) > MAX_HISTORY_BYTES:
        raise ValueError("learner history limit")
    relative = (f"sessions/{timestamp:%Y/%m/%d}/"
                f"rollout-{timestamp:%Y-%m-%dT%H-%M-%S}-{identity}.jsonl")
    return identity, relative, rendered


def restore(path, home):
    """A malformed, outdated or oversized cache simply starts a fresh learner."""
    try:
        if path.is_symlink() or path.stat().st_size > MAX_HISTORY_BYTES:
            return None
        identity, relative, rendered = _history(path.read_text(encoding="utf-8"))
        atomic.write_text(layer.checked_path(home, relative), rendered)
        return identity
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None


def save(path, home, root):
    """Caching is optional; discard unsafe history without invalidating a proposal."""
    try:
        candidates = list((home / "sessions").glob("*/*/*/rollout-*.jsonl"))
        if not candidates:
            return
        latest = layer.checked_path(home, max(candidates, key=lambda item: item.stat().st_mtime_ns).relative_to(home))
        if latest.stat().st_size > MAX_HISTORY_BYTES:
            path.unlink(missing_ok=True)
            return
        _, _, rendered = _history(latest.read_text(encoding="utf-8"))
        with lock_root(root):
            atomic.write_text(path, rendered)
            kept = sorted(path.parent.glob("*.json"), key=lambda item: item.stat().st_mtime_ns)
            for old in kept[:-MAX_CACHED_SESSIONS]:
                old.unlink()
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
