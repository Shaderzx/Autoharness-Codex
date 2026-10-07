"""CAP handoff window: a raw byte slice of the host transcript since the last reflection — zero parsing.

cap.md + docs/plans/raw-capture.md: the transcript is the host's internal event log, not a chat log;
any role/text extraction here is a format assumption that live windows proved wrong (meta records as
empty roles, tool results as pseudo-user turns). So capture does not interpret the format at all —
the reflector (an LLM) reads raw JSONL fine. capture only moves bytes: slice from the session's byte
watermark to EOF, clip oversized records and the window total (tool dumps / base64 must not blow the
child context), pass the egress red line (redact), and hand the text plus the new watermark back to
the caller. **Never write back to the host raw log** (it is not ours). A missing / stale / negative
watermark (compaction rewrote the file) fails safe to a full re-read bounded by the window cap.
"""
import json
import os
from pathlib import Path

from codex_autoharness import config
from codex_autoharness.lib import redact

TRUNCATION_MARK = "...[truncated]"


def resolve_transcript(transcript_path):
    """Resolve the exact archived counterpart after native SessionEnd moves it.

    Codex's archive operation runs after its synchronous hook returns, so a
    detached worker can receive the former location. Never search unrelated
    sessions or guess by a partial session id.
    """
    path = Path(transcript_path)
    if path.exists():
        return path
    codex_directory = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    try:
        relative = path.resolve().relative_to(codex_directory / "sessions")
    except ValueError:
        return path
    if len(relative.parts) != 4 or not all(part.isdigit() for part in relative.parts[:3]):
        return path
    if not relative.name.startswith("rollout-") or not relative.name.endswith(".jsonl"):
        return path
    archived = codex_directory / "archived_sessions" / relative.name
    return archived if archived.is_file() and not archived.is_symlink() else path


def _clip(line, cap):
    """Clip *line* so its UTF-8 encoding stays within *cap* bytes."""
    encoded = line.encode("utf-8")
    if len(encoded) <= cap:
        return line
    truncated = encoded[:cap].decode("utf-8", errors="ignore")
    return truncated + TRUNCATION_MARK


def _open_transcript(path):
    try:
        return open(path, "rb")
    except FileNotFoundError:
        # Archive can move the file between resolve_transcript() and open().
        return open(resolve_transcript(path), "rb")


def model_settings(transcript_path, *, turn_id=None, end_offset=None):
    """Read routing metadata without returning conversation content.

    A requested turn must match exactly: a later turn's model must never replace
    the model that triggered a worker. SessionEnd has no model/turn id, so its
    caller can use the last recorded turn. Explicit null reasoning is preserved
    to distinguish the session default from an inherited config override.
    """
    if not transcript_path:
        return {}
    settings = {}
    try:
        stream = _open_transcript(resolve_transcript(transcript_path))
    except OSError:
        return settings
    with stream:
        limit = stream.seek(0, 2)
        if end_offset is not None:
            limit = min(limit, max(0, int(end_offset)))
        stream.seek(0)
        for line in stream:
            if stream.tell() > limit:
                break
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                continue
            payload = record["payload"]
            if record.get("type") == "session_meta":
                provider = payload.get("model_provider")
                if isinstance(provider, str) and provider:
                    settings["model_provider"] = provider
            elif record.get("type") == "turn_context" and (turn_id is None or payload.get("turn_id") == turn_id):
                mode = payload.get("collaboration_mode")
                nested = mode.get("settings", {}) if isinstance(mode, dict) else {}
                nested = nested if isinstance(nested, dict) else {}
                model = payload.get("model") or nested.get("model")
                if isinstance(model, str) and model:
                    settings["model"] = model
                effort = next((source[key] for source, key in
                               ((payload, "effort"), (payload, "reasoning_effort"), (nested, "reasoning_effort"))
                               if key in source), None)
                if effort is None or isinstance(effort, str):
                    settings["reasoning_effort"] = effort
    return settings


def window(transcript_path, offset=0, *, max_record_bytes=None, max_window_bytes=None,
           rules_path=None, end_offset=None):
    record_cap = max_record_bytes or config.CAPTURE_MAX_RECORD_BYTES
    window_cap = max_window_bytes or config.CAPTURE_MAX_WINDOW_BYTES
    path = resolve_transcript(transcript_path)
    if not path.exists():
        return "", 0
    # Seek past the offset rather than reading the file to slice it away: a hook
    # runs per turn, and a long session's transcript is megabytes of history this
    # call already knows it does not need.
    with _open_transcript(path) as f:
        file_size = f.seek(0, 2)
        if not 0 <= offset <= file_size:
            offset = 0
        new_offset = file_size if end_offset is None else min(file_size, max(0, int(end_offset)))
        if offset >= new_offset:
            # Another queued job may have consumed this older snapshot already.
            return "", offset
        f.seek(offset)
        tail = f.read(new_offset - offset)
    # A native rollout may still be writing its last record. Do not commit a
    # watermark through an incomplete JSON value (including a partial secret).
    if tail and not tail.endswith(b"\n"):
        complete = tail.rfind(b"\n") + 1
        try:
            json.loads(tail[complete:])
        except (ValueError, UnicodeError):
            new_offset -= len(tail) - complete
            tail = tail[:complete]
    # Redact before clipping: truncation can discard a secret's recognizable
    # prefix while retaining sensitive bytes that follow it.
    safe_tail = redact.redact(tail.decode("utf-8", errors="replace"), rules_path)
    lines = [_clip(line, record_cap) for line in safe_tail.splitlines()]
    kept, total = [], 0
    for line in reversed(lines):
        total += len(line.encode("utf-8")) + 1
        if total > window_cap:
            kept.append(TRUNCATION_MARK)
            break
        kept.append(line)
    return "\n".join(reversed(kept)), new_offset


def _digest_record(line, max_chars):
    record = json.loads(line)
    if not isinstance(record, dict):
        return None
    if record.get("type") == "response_item":
        payload = record.get("payload") or {}
        kind = payload.get("type")
        if kind in ("function_call", "custom_tool_call"):
            return "assistant", f"[tool: {payload.get('name', '?')}]"
        if kind != "message" or payload.get("role") not in ("user", "assistant"):
            return None
        role, content = payload["role"], payload.get("content", [])
    elif record.get("type") in ("user", "assistant"):
        role, content = record["type"], record["message"]["content"]
    else:
        # Codex event_msg entries duplicate response_item messages. Reading both
        # inflates the conversation and can make an exchange appear repeated.
        return None
    if isinstance(content, str):
        parts = [content]
    else:
        parts = []
        for block in content:
            kind = block.get("type")
            if kind in ("text", "input_text", "output_text") and block.get("text"):
                parts.append(block["text"])
            elif kind == "tool_use":
                parts.append(f"[tool: {block.get('name', '?')}]")
    if not parts:
        return None
    text = " ".join(p.strip() for p in parts if p.strip())
    if len(text) > max_chars:
        text = text[:max_chars] + TRUNCATION_MARK
    return role, text


def digest(transcript_path, end_offset, *, max_exchanges=None, max_record_chars=None,
           max_digest_bytes=None, rules_path=None):
    exchanges = max_exchanges or config.DIGEST_EXCHANGES
    record_chars = max_record_chars or config.DIGEST_MAX_RECORD_CHARS
    digest_cap = max_digest_bytes or config.DIGEST_MAX_BYTES
    path = resolve_transcript(transcript_path)
    if not path.exists() or end_offset <= 0:
        return ""
    with _open_transcript(path) as f:
        data = f.read(end_offset)
    kept, total, users_seen = [], 0, 0
    safe_data = redact.redact(data.decode("utf-8", errors="replace"), rules_path)
    for line in reversed(safe_data.splitlines()):
        try:
            entry = _digest_record(line, record_chars)
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            continue
        if entry is None:
            continue
        role, text = entry
        rendered = f"{role}: {text}"
        total += len(rendered) + 1
        if total > digest_cap:
            kept.append(TRUNCATION_MARK)
            break
        kept.append(rendered)
        if role == "user":
            users_seen += 1
            if users_seen >= exchanges:
                break
    return redact.redact("\n".join(reversed(kept)), rules_path) if kept else ""
