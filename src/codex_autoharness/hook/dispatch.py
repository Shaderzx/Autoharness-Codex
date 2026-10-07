"""Route native Codex lifecycle hooks into the learning pipeline.

Prompt submissions count opportunities, pre-tool events count activity, and
successful post-tool reads measure managed skill loads. Stop and SessionEnd
launch bounded detached workers. Unknown events are harmless no-ops.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

MIN_PYTHON = (3, 11)  # tomllib (lib/redact.py) entered the stdlib here; README badge and CI matrix pin the same floor


def _below_floor(version):
    return (f"autoharness: needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+, got "
            f"{version[0]}.{version[1]} at {sys.executable}. Every hook is off for this session; "
            f"point the hooks.json commands at a newer interpreter.")


# this guard runs before the package imports on purpose: the chain below is module level end to end
# (dispatch -> promoter -> redact -> tomllib), so on an older interpreter the process dies at import
# and the fail-safe in dispatch() never gets the chance to catch it -- the host shows a generic hook
# error and nothing inside the plugin names the cause. Exit 0: a host hook must not fail the session.
if sys.version_info[:2] < MIN_PYTHON:
    print(_below_floor(sys.version_info), file=sys.stderr)
    raise SystemExit(0)

from codex_autoharness import config  # noqa: E402
from codex_autoharness.hook import (  # noqa: E402
    capture,
    on_session_end,
    on_session_start,
    on_skill_call,
    on_stop,
    promoter,
)
from codex_autoharness.lib import atomic, counters, layer  # noqa: E402
from codex_autoharness.lib.locking import lock_root, lock_roots  # noqa: E402

_SANITIZE = re.compile(r"[^A-Za-z0-9_-]")
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch")


def _roots(roots, event=None):
    resolved = {lyr: layer.default_root(lyr) for lyr in layer.LAYERS}
    if event and event.get("cwd") and not os.environ.get("CODEX_AUTOHARNESS_PROJECT_ROOT"):
        resolved[layer.PROJECT] = layer._main_worktree_root(event["cwd"]) / ".agents"
    resolved.update(roots or {})
    return resolved


def _run_id(result):
    raw = str(result.get("session_id") or "")
    sid = _SANITIZE.sub("", raw)
    if not sid:
        sid = hashlib.sha256(raw.encode()).hexdigest()[:8] if raw else "run"
    return f"{sid}-{result.get('count', 0)}"


def _curate_run_id(event, pcount):
    raw = str(event.get("session_id") or "")
    sid = _SANITIZE.sub("", raw)
    if not sid:
        sid = hashlib.sha256(raw.encode()).hexdigest()[:8] if raw else "run"
    return f"{sid}-c{pcount}"  # keyed on cumulative tool activity


def _is_reflector(event):
    at = str(event.get("agent_type") or "")
    return at == config.REFLECTOR_AGENT or at.endswith("reflector")


def _worker_env():
    env = os.environ.copy()
    source = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (source, env.get("PYTHONPATH"))))
    return env


def _job_arguments(settings=None, end_offset=None):
    arguments = []
    for key in ("model", "model_provider", "reasoning_effort"):
        if key in (settings or {}):
            arguments.extend(("--" + key.replace("_", "-"), settings[key]))
    if end_offset is not None:
        arguments.extend(("--end-offset", str(end_offset)))
    return arguments


def _session_settings(event, root):
    """Snapshot this session's identity; queued jobs never consult latest state."""
    session_id = event.get("session_id")
    path = None
    if isinstance(session_id, str) and session_id:
        key = hashlib.sha256(session_id.encode()).hexdigest()
        path = layer.checked_path(root, "codex-autoharness", "session-models", f"{key}.json")
    end_offset = None
    transcript = event.get("transcript_path")
    current = {}
    if transcript:
        try:
            end_offset = capture.resolve_transcript(transcript).stat().st_size
            current = capture.model_settings(transcript, turn_id=event.get("turn_id"), end_offset=end_offset)
        except OSError:
            pass
    if isinstance(event.get("model"), str) and event["model"].strip():
        current["model"] = event["model"]
    if "reasoning_effort" in current and current["reasoning_effort"] is None:
        current["reasoning_effort"] = ""  # explicit model default, not another profile's configured effort
    with lock_root(root):
        previous = {}
        if path and path.exists():
            try:
                previous = json.loads(path.read_text())
            except (ValueError, OSError):
                pass
        if not isinstance(previous, dict):
            previous = {}
        settings = {key: value for key, value in {**previous, **current}.items()
                    if key in {"model", "model_provider", "reasoning_effort"} and isinstance(value, str)}
        if path and settings and settings != previous:
            atomic.write_text(path, json.dumps(settings))
    return settings, end_offset


def _detached_launch(transcript_path, session_id, run_id, roots, *, settings=None, end_offset=None):
    try:
        subprocess.Popen(  # host-detach: fire-and-forget so the Stop hook returns immediately
            [sys.executable, "-m", "codex_autoharness.hook.spawn",
             str(transcript_path), str(session_id), run_id,
             str(roots[layer.PROJECT]), str(roots[layer.GLOBAL]),
             *_job_arguments(settings, end_offset)],
            start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=_worker_env(),
        )
    except OSError as exc:
        return {"error": f"detached_launch failed: {type(exc).__name__}: {exc}"}
    return None


def _reflect(event, result, roots, launch=None):
    transcript_path = event.get("transcript_path")
    if not transcript_path:
        return
    # The activity count resets, so it cannot distinguish successive windows.
    run_id = f"{_run_id(result)}-{uuid.uuid4().hex[:12]}"
    job = {}
    if event.get("_autoharness_model_settings"):
        job["settings"] = event["_autoharness_model_settings"]
    if event.get("_autoharness_end_offset") is not None:
        job["end_offset"] = event["_autoharness_end_offset"]
    return (launch or _detached_launch)(transcript_path, result.get("session_id", ""), run_id, roots, **job)


def _consolidate_launch(run_id, roots, *, settings=None):
    subprocess.Popen(  # host-detach: same fire-and-forget as reflection; the curator reads the library, not the transcript
        [sys.executable, "-m", "codex_autoharness.hook.spawn", "--curate", run_id,
         str(roots[layer.PROJECT]), str(roots[layer.GLOBAL]), *_job_arguments(settings)],
        start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=_worker_env(),
    )


def _prompt(event, roots):
    """Count actual prompt submissions once, even if the host retries a hook."""
    session = str(event.get("session_id") or "")
    turn = str(event.get("turn_id") or "")
    marker = None
    if session and turn:
        identity = hashlib.sha256(f"{session}\0{turn}".encode()).hexdigest()
        marker = layer.checked_path(roots[layer.PROJECT], "codex-autoharness", "turns", identity)
    with lock_roots(roots):
        if marker and marker.exists():
            return {"counted": False, "count": counters.request_count(layer.PROJECT, roots[layer.PROJECT])}
        for lyr in layer.unique_layers(roots):
            counters.bump_request(lyr, roots[lyr])
        count = counters.request_count(layer.PROJECT, roots[layer.PROJECT])
        if marker:
            atomic.write_text(marker, "1")
    return {"counted": True, "count": count}


def _activity(event, root):
    with lock_root(root):
        sid = event.get("session_id")
        if isinstance(sid, str) and sid:
            try:
                counters.bump_session(sid, root)
            except ValueError:
                return
        path = layer.checked_path(root, "codex-autoharness", "tool_calls")
        count = counters._read_int(path) + 1
        atomic.write_text(path, str(count))


def _curation_count(root):
    threshold = config.CONSOLIDATE_EVERY_N
    if threshold <= 0:
        return None
    with lock_root(root):
        count = counters._read_int(layer.checked_path(root, "codex-autoharness", "tool_calls"))
        watermark = layer.checked_path(root, "codex-autoharness", "last_curated_tool_count")
        if count - counters._read_int(watermark) < threshold:
            return None
        atomic.write_text(watermark, str(count))
        return count


def dispatch(event, *, roots=None, reflect=None, consolidate=None):
    if not isinstance(event, dict):
        return {"ignored": True, "reason": "malformed hook input"}
    name = event.get("hook_event_name")
    if not getattr(config, "ENABLED", True):
        return {"ignored": True, "reason": "disabled"}
    child = bool(os.environ.get(config.CHILD_SESSION_ENV)) or _is_reflector(event)
    if child:
        if name == "PreToolUse" and event.get("tool_name") in _WRITE_TOOLS:
            return {"deny": True, "reason": "reflector may only stage intents, not write files"}
        return {"handled": name, "result": {"triggered": False, "counted": False, "reason": "recursion_guard"}}
    if event.get("agent_id"):
        return {"ignored": True, "reason": "subagent event"}
    fire = reflect or _reflect
    curate = consolidate or _consolidate_launch
    try:
        roots = _roots(roots, event)
        proot = roots.get(layer.PROJECT)
        if name in {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"}:
            settings, end_offset = _session_settings(event, proot)
            event = {**event, "_autoharness_model_settings": settings,
                     "_autoharness_end_offset": end_offset}
        if name == "SessionStart":
            return {"handled": name, "result": on_session_start.on_session_start(event, roots=roots)}
        if name == "UserPromptSubmit":
            result = _prompt(event, roots)
            return {"handled": name, "result": result}
        if name == "Stop":
            promoter.drain(config.INTERACTIVE_RUN_ID, roots=roots)  # /learn and other in-session proposals; no-op when empty
            result = on_stop.on_stop(event, root=proot)
            if result.get("triggered"):
                fire(event, result, roots)
            curation_count = _curation_count(proot)
            if curation_count is not None:
                settings = event.get("_autoharness_model_settings")
                curate(_curate_run_id(event, curation_count), roots,
                       **({"settings": settings} if settings else {}))
            return {"handled": name, "result": result}
        if name == "SessionEnd":
            result = on_session_end.on_session_end(event, root=proot)
            if result.get("triggered"):
                fire(event, result, roots)
            return {"handled": name, "result": result}
        if name == "PreToolUse":
            _activity(event, proot)
            return {"handled": name, "result": {"counted": False}}
        if name == "PostToolUse":
            return {"handled": name, "result": on_skill_call.on_skill_read(event, roots=roots)}
    except Exception as exc:  # fail-safe: a buggy handler must never crash the host hook
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"ignored": True, "reason": f"unrouted event: {name!r}"}


def _emit(verdict):
    if verdict.get("deny"):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": verdict.get("reason", "denied"),
        }}))
        return
    result = verdict.get("result") or {}
    if verdict.get("handled") in ("SessionStart", "UserPromptSubmit") and result.get("context"):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": verdict["handled"],
            "additionalContext": result["context"],
        }}))
        return
    print("{}")  # Stop requires JSON, and no-op hooks must not leak state into context.


def main():
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print("{}")
        return 0  # malformed hook input → ignore, never crash the host
    _emit(dispatch(event))
    return 0


if __name__ == "__main__":
    sys.exit(main())
