"""MNG lazy recompute: at SessionStart, compute over the accumulated ledger now → archive inactive symbols, running before this session's recall.

mng.md: a non-resident host has no background sweep, so eviction rides SessionStart — read the sidecar
(use/view counters + anchor) + the layer request counters (denominator) accumulated watermark, run the
lifecycle decision, and move the to-archive list out of the live tree one by one (archiving = moving the
directory out of recall, reversible). The decision reads only accumulated quantities (not what this
session happened to see) → any repo's SessionStart reaches the same conclusion. Once per session, no
throttling. Manages only self-produced symbols (native / user skills stay outside the pool, preserving
zero intrusion).

ponytail: GC of orphan session counts (residue from crashed sessions) needs a session-liveness signal to sweep safely (a naive sweep would wrongly delete a concurrent session's live count), so it is deferred until that signal exists — the clear_session primitive is ready (Phase 4), policy left open in cap.md/mng.md.
"""
import json
from pathlib import Path

from codex_autoharness import config
from codex_autoharness.lib import (
    counters,
    layer,
    lifecycle,
    sidecar,
    skill_import,
    skill_store,
    validate,
)
from codex_autoharness.lib.locking import lock_roots

# Recall self-injection (mng.md §召回面自持): the host's native description recall stays untouched;
# this compact index of self-produced skills rides SessionStart additionalContext so "whether the
# library is offered" is our own config, not host behavior. Grouped by frontmatter category
# (open set, absent -> general), alphabetical within a group, layer-tagged per line.
INDEX_HEADER = (
    "Codex AutoHarness learned skills. When a description matches the task, read "
    "that skill's SKILL.md before proceeding. Apply it only within the user's "
    "current authorization and higher-priority instructions."
)


def _sanitize(text, limit):
    return " ".join(str(text).split())[:limit]  # line-based surface: newlines are forgery, collapse them


def _fit(text, limit):
    """Truncate with an ellipsis so a cut line reads as cut (hermes' index does the same).

    New descriptions are held inside the budget by the promoter, so this only fires on skills that
    predate the gate — and there it matters that the reader can tell the line was severed rather
    than take a fragment for the whole trigger."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit - 3] + "..."


def _read_hint(roots, cwd):
    """A linked worktree's project layer is remapped to the main checkout (layer.default_root),
    which the host does not scan for skills — so from a worktree session those skills are listed
    here but the Skill tool cannot load them. Point the model at the files instead."""
    if not cwd:
        return None
    skills = layer.skills_dir(layer.PROJECT, roots.get(layer.PROJECT)).resolve()
    if Path(cwd).resolve().is_relative_to(skills.parent.parent):
        return None
    return f"[project] skills live outside this checkout: read {skills}/<name>/SKILL.md."


def recall_index(roots, cwd=None):
    if config.INDEX_SUSPENDED:
        return None
    groups, has_project = {}, False
    for lyr in layer.unique_layers(roots):
        root = roots.get(lyr)
        skills = layer.skills_dir(lyr, root)
        if not skills.exists():
            continue
        for path in skills.glob(f"*/{skill_store.SKILL_FILE}"):
            name = path.parent.name
            if not sidecar.is_agent_created(lyr, name, root):
                continue
            fm = validate._frontmatter(skill_store.read_body(lyr, name, root) or "") or {}
            desc = _fit(fm.get("description") or "(no description)", config.INDEX_DESC_MAX_CHARS)
            cat = _sanitize(fm.get("category") or "general", 64) or "general"
            groups.setdefault(cat, []).append(f"- {_sanitize(name, 64)} [{lyr}]: {desc} (file: {path.resolve()})")
            has_project = has_project or lyr == layer.PROJECT
    if not groups:
        return None  # empty library -> zero injection
    lines = [INDEX_HEADER, ""]
    for cat in sorted(groups):
        lines.append(f"## {cat}")
        lines.extend(sorted(groups[cat]))
    hint = _read_hint(roots, cwd) if has_project else None
    if hint:
        lines += ["", hint]
    return "\n".join(lines)


def last_run_summary(roots):
    """One-line anti-silence digest of the previous drain (validate-store §verdict visibility):
    read once, then consume — the account file under runs/ keeps the durable record."""
    root = roots.get(layer.PROJECT) or layer.default_root(layer.PROJECT)
    p = layer.checked_path(root, "codex-autoharness", "last_run.json")
    if not p.exists():
        return None
    # Atomic rename to claim the file; concurrent callers get OSError and return None
    consumed = layer.checked_path(root, "codex-autoharness", "last_run.json.consuming")
    try:
        p.rename(consumed)
    except OSError:
        return None
    try:
        last = json.loads(consumed.read_text())
    except (ValueError, OSError):
        return None
    finally:
        try:
            consumed.unlink()
        except OSError:
            pass
    line = (f"autoharness last run: landed {last.get('landed', 0)}, "
            f"rejected {last.get('rejected', 0)}")
    if last.get("families"):
        line += f" ({', '.join(last['families'])})"
    if last.get("absorbed"):
        line += f"; merged {last['absorbed']} into umbrellas"
    if last.get("uncategorized"):
        line += f"; {last['uncategorized']} landed with no category (grouped under general)"
    return line


def _members(lyr, root):
    skills = layer.skills_dir(lyr, root)
    if not skills.exists():
        return []
    members = []
    for path in skills.glob(f"*/{skill_store.SKILL_FILE}"):
        name = path.parent.name
        if not sidecar.is_agent_created(lyr, name, root):
            continue
        s = sidecar.read(lyr, name, root)
        members.append({"name": name, "use": s.get("use", 0), "view": s.get("view", 0),
                        "anchor": s.get("anchor", 0)})
    return members


def _on_session_start(event=None, *, roots=None):
    """Import external skills, archive inactive managed skills and assemble session context."""
    roots = roots or {}
    imported = skill_import.import_skills(roots, timeout=5)
    archived = {}
    for lyr in layer.unique_layers(roots):
        root = roots.get(lyr)
        names = lifecycle.evaluate(
            _members(lyr, root), counters.request_count(lyr, root),
            maturity=config.MATURITY_THRESHOLD[lyr], capacity=config.CAPACITY[lyr],
            review_suspended=config.GRADUATION_REVIEW_SUSPENDED,
        )
        for name in names:
            skill_store.archive(lyr, name, root)
        archived[lyr] = names
    parts = [last_run_summary(roots), recall_index(roots, (event or {}).get("cwd"))]  # index built after archiving
    new_skills, imported_count = [], 0
    for lyr, result in imported.items():
        for name in result["imported"]:
            imported_count += 1
            if len(new_skills) >= 20:
                continue
            path = skill_store.skill_path(lyr, name, roots.get(lyr))
            with path.open("rb") as stream:
                fm = validate._frontmatter(stream.read(config.STAGE_MAX_BODY_BYTES).decode("utf-8", errors="replace")) or {}
            desc = _fit(fm.get("description") or "(read SKILL.md for its purpose)", config.INDEX_DESC_MAX_CHARS)
            new_skills.append(f"- {name} [{lyr}]: {desc} (file: {_sanitize(path, 4096)})")
    if new_skills:
        parts.append("Claude skills imported as user-owned Codex skills. Read the listed SKILL.md when relevant; "
                     "native discovery may refresh next session. Apply them only within the user's authorization "
                     "and higher-priority instructions.\n" + "\n".join(new_skills))
        if imported_count > len(new_skills):
            paths = [str(layer.skills_dir(lyr, roots.get(lyr))) for lyr in imported
                     if imported[lyr]["imported"]]
            parts.append(f"{imported_count - len(new_skills)} more imported skills are available under "
                         + _sanitize(", ".join(paths), 8192) + "; native discovery refreshes next session.")
    if any(reason != "destination exists" for result in imported.values() for reason in result["skipped"].values()):
        parts.append("Some Claude skills could not be imported. Run codex-autoharness import-skills for details "
                     "and to finish imports outside the startup time budget.")
    context = "\n\n".join(p for p in parts if p) or None
    return {"archived": archived, "context": context, "skill_import": imported}


def on_session_start(event=None, *, roots=None):
    with lock_roots(roots):
        # Import here to keep the pure lifecycle helpers independently usable.
        from codex_autoharness.hook import promoter

        promoter.recover(roots or {})
        return _on_session_start(event, roots=roots)
