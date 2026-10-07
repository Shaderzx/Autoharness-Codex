"""Validate proposals and publish only this tool's managed skills.

Root locks serialize workers. Each mutation has a durable backup journal; recovery
restores interrupted writes before replay. A completed ledger intent ID prevents
duplicate publication after an interrupted queue drain. Static content checks are
heuristics, not a sandbox or a proof that generated instructions are trustworthy.
"""
import hashlib
import json
import os
import re
import shutil
import uuid

from codex_autoharness.lib import (
    atomic,
    counters,
    git_exclude,
    intent_queue,
    layer,
    ledger,
    notify,
    redact,
    sidecar,
    skill_store,
    validate,
)
from codex_autoharness.lib.locking import lock_roots

_MODIFY = ("update", "patch", "remove_file", "delete")


def _reject(action, level, findings):
    return {"ok": False, "action": action, "level": level, "findings": findings}


def _resolve_level(intent, roots):
    if intent.get("action") == "create":
        return layer.canonical_layer(intent.get("level", layer.PROJECT), roots)
    return skill_store.find(intent.get("name"), roots)


def _shape(intent, level, root):
    action = intent.get("action")
    if action in ("create", "update"):
        body = intent.get("body")
        if not isinstance(body, str):
            raise ValueError(f"{action} requires body")
        return body
    if action == "patch":
        live = skill_store.read_body(level, intent.get("name"), root)
        if live is None:
            raise ValueError("patch target has no live body")
        return skill_store.apply_delta(live, intent["old_string"], intent["new_string"])
    if action in ("remove_file", "delete"):
        return None
    raise ValueError(f"unknown action: {action!r}")


def _led(intent, evidence_ref):
    entry = {"action": intent.get("action"),
             "reason": redact.redact(intent.get("reason", "")),
             "evidence": evidence_ref}
    if intent.get("_intent_id"):
        entry["intent_id"] = intent["_intent_id"]
    if intent.get("path"):
        entry["path"] = intent["path"]
    if intent.get("absorbed_into"):
        entry["absorbed_into"] = intent["absorbed_into"]  # consolidated vs pruned, distinguishable in the account
    return entry


def _materialize_evidence(level, name, evidence, root):
    text = redact.redact(evidence)
    rel = f"{layer.EVIDENCE_PREFIX}{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}.md"
    p = layer.subfile_path(level, name, rel, root)
    if not p.exists():
        atomic.write_text(p, text)
    return rel


def _land_files(level, name, files, root):
    if not files:
        return
    sdir = layer.symbol_dir(level, name, root).resolve()
    paths = {rel: layer.subfile_path(level, name, rel, root) for rel in sorted(files)}
    for rel, p in paths.items():
        if not p.resolve().is_relative_to(sdir):
            raise ValueError(f"subfile escapes the skill dir: {rel}")
    for rel, p in paths.items():
        atomic.write_text(p, files[rel])


def _remove_subfile(level, name, rel, root):
    p = layer.subfile_path(level, name, rel, root)
    sdir = layer.symbol_dir(level, name, root).resolve()
    if not p.resolve().is_relative_to(sdir):
        raise ValueError(f"subfile escapes the skill dir: {rel}")
    live = skill_store.read_body(level, name, root) or ""
    # Use word-boundary check so scripts/run.py does not match scripts/run.py.bak
    _ref = re.compile(r"(?<![A-Za-z0-9_./-])" + re.escape(rel) + r"(?![A-Za-z0-9_./-])")
    if _ref.search(live):
        raise ValueError(f"{rel} is still referenced by the live SKILL.md (patch the pointer out first)")
    if p.is_file():
        p.unlink()


def _land(action, intent, body, level, name, root):
    if action == "delete":
        evidence_ref = _materialize_evidence(level, name, intent.get("evidence"), root)
        ledger.append(level, name, _led(intent, evidence_ref), root)
        skill_store.archive(level, name, root)
        return
    if action == "remove_file":
        _remove_subfile(level, name, intent["path"], root)
        evidence_ref = _materialize_evidence(level, name, intent.get("evidence"), root)
        ledger.append(level, name, _led(intent, evidence_ref), root)
        return
    _land_files(level, name, intent.get("files"), root)
    evidence_ref = _materialize_evidence(level, name, intent.get("evidence"), root)
    skill_store.write_body(level, name, body, root)
    if action == "create":
        existing = sidecar.read(level, name, root)
        if not existing:
            sidecar.create(level, name, counters.request_count(level, root), root)
        # crash-replay: sidecar already exists — skip create to preserve counters
    else:
        sidecar.bump_patch(level, name, root)  # update/patch: feeds the reuse-after-improvement pair
    ledger.append(level, name, _led(intent, evidence_ref), root)


def promote(intent, *, roots=None, repo_name=None):
    try:
        with lock_roots(roots):
            recover(roots)
            return _promote(intent, roots=roots, repo_name=repo_name)
    except (OSError, ValueError, TypeError) as exc:
        verdict = _reject(intent.get("action") if isinstance(intent, dict) else None, None,
                          [("storage", "managed storage is unavailable or unsafe")])
        verdict["retryable"] = isinstance(exc, OSError)
        return verdict


def _preflight_paths(level, name, root, intent):
    """Reject every redirected path before any skill content is changed."""
    base = layer.symbol_dir(level, name, root)
    if base.exists():
        for directory, dirs, files in os.walk(base, followlinks=False):
            for leaf in dirs + files:
                layer.checked_path(layer._root(level, root), "skills", name,
                                   (os.path.join(directory, leaf))[len(str(base)) + 1:])
    for filename in (skill_store.SKILL_FILE, sidecar.FILENAME, ledger.FILENAME):
        layer.checked_path(layer._root(level, root), "skills", name, filename)
    layer.checked_path(layer._root(level, root), "skills", name, "references")
    for rel in intent.get("files") or {}:
        layer.subfile_path(level, name, rel, root)
    if intent.get("path"):
        layer.subfile_path(level, name, intent["path"], root)
    if intent.get("action") == "delete":
        layer.archive_dir(level, root)


def _already_landed(intent, roots):
    intent_id = intent.get("_intent_id")
    if not intent_id:
        return None
    for lyr in layer.unique_layers(roots):
        root = roots.get(lyr)
        if intent.get("action") != "delete" and sidecar.is_agent_created(lyr, intent.get("name"), root):
            rows = ledger.read(lyr, intent["name"], root)
            if any(row.get("intent_id") == intent_id for row in rows):
                return {"ok": True, "action": intent.get("action"), "level": lyr,
                        "findings": [], "notes": [], "replayed": True}
        archive = layer.archive_dir(lyr, root)
        if archive.exists():
            for directory in archive.iterdir():
                if not directory.name.startswith(intent.get("name", "") + ".") and directory.name != intent.get("name"):
                    continue
                path = layer.checked_path(layer._root(lyr, root), "skills", ".archive", directory.name, ledger.FILENAME)
                if path.is_file():
                    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
                    if any(row.get("intent_id") == intent_id for row in rows):
                        return {"ok": True, "action": intent.get("action"), "level": lyr,
                                "findings": [], "notes": [], "replayed": True}
    return None


def _transaction_paths(root, intent_id):
    if not isinstance(intent_id, str) or not re.fullmatch(r"[a-f0-9]{32}", intent_id):
        raise ValueError("invalid transaction identity")
    base = layer.checked_path(root, "codex-autoharness", "transactions")
    return (layer.checked_path(root, "codex-autoharness", "transactions", intent_id + ".json"),
            layer.checked_path(root, "codex-autoharness", "transactions", intent_id + ".before"), base)


def _finish_transaction(journal, backup):
    # Remove the journal first: once a committed transaction is no longer pending,
    # a crash during backup cleanup cannot make recovery roll it back.
    journal.unlink(missing_ok=True)
    if backup.exists():
        shutil.rmtree(backup)


def _rollback_transaction(record, root, journal, backup):
    name = record["name"]
    target = layer.symbol_dir(record["level"], name, root)
    if record["had_target"]:
        if not backup.is_dir():
            raise ValueError("pending transaction backup is missing")
        _check_backup(root, backup)
    try:
        if target.exists():
            _preflight_paths(record["level"], name, root, {})
            shutil.rmtree(target)
        if record["had_target"]:
            shutil.copytree(backup, target)
        _finish_transaction(journal, backup)
    finally:
        git_exclude.sync(root)


def _check_backup(root, backup):
    for directory, dirs, files in os.walk(backup, followlinks=False):
        for leaf in dirs + files:
            path = layer.checked_path(root, os.path.relpath(os.path.join(directory, leaf), root))
            if not path.is_file() and not path.is_dir():
                raise ValueError("backup contains an unsupported file type")


def recover(roots=None):
    """Restore interrupted mutations; completed ledger IDs keep committed results.

    Callers hold lock_roots. Journal paths never contain a model-supplied path.
    """
    roots = roots or {}
    for level in layer.unique_layers(roots):
        root = layer._root(level, roots.get(level))
        directory = layer.checked_path(root, "codex-autoharness", "transactions")
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            journal, backup, _ = _transaction_paths(root, path.stem)
            record = json.loads(journal.read_text())
            layer._check_name(record.get("name"))
            if record.get("level") != level or record.get("_intent_id") != path.stem or not isinstance(record.get("had_target"), bool):
                raise ValueError("invalid pending transaction")
            try:
                committed = _already_landed(record, roots)
            except json.JSONDecodeError:
                committed = None  # interrupted ledger append: restore the valid backup
            if committed:
                _finish_transaction(journal, backup)
                git_exclude.sync(root)
            else:
                _rollback_transaction(record, root, journal, backup)


def _promote(intent, *, roots=None, repo_name=None):
    roots = roots or {}
    if not isinstance(intent, dict):
        return _reject(None, None, [("schema", "intent must be an object")])
    from codex_autoharness.stage_skill.server import _schema_errors
    errors = _schema_errors({k: v for k, v in intent.items() if k != "_intent_id"})
    if errors:
        return _reject(intent.get("action"), None, errors)
    intent = {**intent, "_intent_id": intent.get("_intent_id") or uuid.uuid4().hex}
    action = intent.get("action")
    name = intent.get("name")
    replay = _already_landed(intent, roots)
    if replay is not None:
        return replay

    try:
        level = _resolve_level(intent, roots)
    except ValueError as exc:
        return _reject(action, None, [("routing", str(exc))])
    if level not in layer.LAYERS:
        return _reject(action, level, [("routing", f"unresolved/illegal level: {level!r}")])

    root = roots.get(level)
    try:
        base_dir = layer.symbol_dir(level, name, root)
        if action == "create" and base_dir.exists():
            return _reject(action, level, [("occupied", "create target already exists")])
        body = _shape(intent, level, root)
    except (ValueError, KeyError) as exc:
        return _reject(action, level, [("shape", str(exc))])

    target_created = sidecar.is_agent_created(level, name, root) if action in _MODIFY else None

    absorbed = intent.get("absorbed_into")
    if action == "delete" and absorbed:
        # fail-closed absorption claim (direction D, Hermes trust-chain tier 1): the umbrella must be
        # a live, self-produced skill distinct from the target — a hallucinated name rejects the intent.
        try:
            alevel = skill_store.find(absorbed, roots)
        except ValueError as exc:
            return _reject(action, level, [("absorbed_into", str(exc))])
        if alevel is None or absorbed == name \
                or not sidecar.is_agent_created(alevel, absorbed, roots.get(alevel)):
            return _reject(action, level,
                           [("absorbed_into", f"umbrella {absorbed!r} is not a live self-produced skill")])

    verdict = validate.validate(
        {**intent, "level": level}, body,
        target_is_agent_created=target_created, repo_name=repo_name, base_dir=base_dir,
    )
    if not verdict["ok"]:
        return _reject(action, level, verdict["findings"])

    try:
        _preflight_paths(level, name, root, intent)
    except ValueError as exc:
        return _reject(action, level, [("landing", str(exc))])
    journal, backup, directory = _transaction_paths(layer._root(level, root), intent["_intent_id"])
    directory.mkdir(parents=True, exist_ok=True)
    had_target = base_dir.exists()
    if backup.exists():
        # An interrupted pre-journal copy never changed the live skill. Rebuild
        # only this intent's verified private backup before starting a new land.
        _check_backup(layer._root(level, root), backup)
        shutil.rmtree(backup)
    if had_target:
        shutil.copytree(base_dir, backup)
        for path in backup.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
    record = {"name": name, "level": level, "action": action,
              "_intent_id": intent["_intent_id"], "had_target": had_target}
    atomic.write_text(journal, json.dumps(record))
    try:
        _land(action, intent, body, level, name, root)
    except (OSError, ValueError, TypeError) as exc:
        _rollback_transaction(record, layer._root(level, root), journal, backup)
        verdict = _reject(action, level, [("landing", "skill write failed; previous tree restored")])
        verdict["retryable"] = isinstance(exc, OSError)
        return verdict
    _finish_transaction(journal, backup)
    return {"ok": True, "action": action, "level": level, "findings": [], "notes": _notes(action, body)}


def _notes(action, body):
    """Fail-open observations on a landed intent: not a rejection, not silence either.

    A missing category only groups the skill badly (the index falls back to `general`), so refusing
    real content over it is out of proportion — the opposite call from the description gate, where a
    cue-less description can never be recalled at all. It still has to reach the account, or the
    library drifts into one flat group with nobody noticing."""
    if action not in ("create", "update"):
        return []
    return [] if (validate._frontmatter(body) or {}).get("category") else ["category"]


def sweep(roots=None):
    roots = roots or {}
    removed = []
    for lyr in layer.unique_layers(roots):
        removed += skill_store.sweep_orphans(lyr, roots.get(lyr))
    return removed


def _account(run_id, intents, verdicts, proot):
    """Run-level verdict account (direction B, anti-silence): the per-symbol LED records only landed
    facts, so a rejected create would vanish without trace — this account is where verdicts live.
    last_run.json feeds the one-line SessionStart summary and is consumed after one injection."""
    rows = [{"action": v.get("action"), "name": redact.redact(str(i.get("name", ""))), "ok": v["ok"],
             "findings": [f[0] for f in v.get("findings", [])], "notes": v.get("notes", [])}
            for i, v in zip(intents, verdicts, strict=True)]
    landed = sum(1 for r in rows if r["ok"])
    absorbed = sum(1 for i, v in zip(intents, verdicts, strict=True)
                   if v["ok"] and i.get("action") == "delete" and i.get("absorbed_into"))
    families = sorted({f for r in rows if not r["ok"] for f in r["findings"]})
    state = layer.state_dir(layer.PROJECT, proot)
    runs = layer.checked_path(layer._root(layer.PROJECT, proot), "codex-autoharness", "runs")
    runs.mkdir(parents=True, exist_ok=True)
    record = {"run_id": run_id, "verdicts": rows}
    atomic.write_text(runs / f"{run_id}.json", json.dumps(record, ensure_ascii=False, indent=2))
    atomic.write_text(state / "last_run.json",
                      json.dumps({"run_id": run_id, "landed": landed,
                                  "rejected": len(rows) - landed, "absorbed": absorbed,
                                  "families": families,
                                  "uncategorized": sum(1 for r in rows if "category" in r["notes"])},
                                 ensure_ascii=False))
    return record


def drain(run_id, *, roots=None, repo_name=None):
    with lock_roots(roots):
        return _drain(run_id, roots=roots, repo_name=repo_name)


def _drain(run_id, *, roots=None, repo_name=None):
    roots = roots or {}
    recover(roots)
    sweep(roots)
    proot = roots.get(layer.PROJECT)
    intents = intent_queue.read(run_id, proot)
    verdicts = []
    failed_names = set()
    for intent in intents:
        if intent.get("action") == "delete" and intent.get("absorbed_into") in failed_names:
            verdict = _reject("delete", None, [("dependency", "absorbing skill failed earlier in this run")])
        else:
            verdict = promote(intent, roots=roots, repo_name=repo_name)
        verdicts.append(verdict)
        if not verdict["ok"]:
            failed_names.add(intent.get("name"))
    record = _account(run_id, intents, verdicts, proot) if intents else None
    if any(verdict.get("retryable") for verdict in verdicts):
        raise OSError("promotion interrupted; durable queue retained for retry")
    intent_queue.clear(run_id, proot)
    if record:
        # after clear, not inside _account: an external process in the land→clear window would
        # widen the crash window where a whole run replays (duplicate LED, re-rejected creates)
        notify.send(record)
    return verdicts
