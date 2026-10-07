"""Deterministic admission checks over the final skill and support files.

Checks shape, identity, ownership, path containment, completeness, credentials,
trigger budgets and global scope. Static safety patterns are only heuristics."""
import ast
import json
import re

from codex_autoharness import config
from codex_autoharness.lib import layer, redact, skills_guard

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
_PLACEHOLDER = re.compile(r"\b(TODO|FIXME|XXX):|<(?:TODO|FIXME|XXX|TBD|PLACEHOLDER|REPLACE[_ ]?ME|INSERT[_ ]?HERE|FILL[_ ]?IN)>")
_ABS_PATH = re.compile(r"(?:/home/|/Users/|/root/)[^\s`)\]]+|[A-Za-z]:\\[^\s`)\]]+")
_PY_REF = re.compile(r"[\w./-]+\.py")
_SUBFILE_REF = re.compile(r"\b(?:{})/[A-Za-z0-9._/-]+".format("|".join(layer.SUBFILE_DIRS)))
_MODIFY = ("update", "patch", "remove_file", "delete")
# description-as-trigger proxy: a firing description names WHEN to use it ("use when …") or lists a
# literal phrase the user would type (quoted). A bare topic-label has neither and never fires.
_QUOTED_PHRASE = re.compile(r'"[^"]+"|\'[^\']+\'|`[^`]+`')
_WHEN = re.compile(r"(?i)\bwhen")


def _has_cue(text):
    return bool(_WHEN.search(text) or _QUOTED_PHRASE.search(text))


def _frontmatter(body):
    m = _FRONTMATTER.match(body)
    if not m:
        return None
    fm = {}
    for line in m.group(1).splitlines():
        s = line.strip()
        if not s or s.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        v = value.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            if v[0] == '"':
                try:
                    v = json.loads(v)
                except ValueError:
                    v = v[1:-1]
            else:
                v = v[1:-1].replace("''", "'")
        fm[key.strip()] = v
    return fm


def _flat_frontmatter_findings(body):
    """Accept an explicit YAML string subset that every native skill reader can load.

    External indexes retain best-effort parsing in _frontmatter. Admission alone
    requires flat scalar fields; quote ambiguous values with JSON double quotes.
    """
    match = _FRONTMATTER.match(body)
    if not match:
        return [("structure", "missing or invalid frontmatter delimiters")]
    seen = set()
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1].isspace() or ":" not in line:
            return [("structure", "frontmatter must contain flat key: string fields")]
        key, value = line.split(":", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key) or key in seen:
            return [("structure", "frontmatter keys must be unique identifiers")]
        seen.add(key)
        value = value.strip()
        if not value:
            continue  # required fields report their more specific existing error
        if value.startswith('"'):
            try:
                parsed = json.loads(value)
            except ValueError:
                return [("structure", "frontmatter double quotes must form a valid JSON string")]
            if not isinstance(parsed, str):
                return [("structure", "frontmatter values must be strings")]
        elif value.startswith("'"):
            if len(value) < 2 or not value.endswith("'") or "'" in value[1:-1].replace("''", ""):
                return [("structure", "frontmatter single quotes must be balanced; escape apostrophes by doubling")]
        else:
            ambiguous = (value[0] in "{}[]>|&*!%@`\"" or value.startswith(("- ", "? ", ": "))
                         or re.search(r":(?:\s|$)|\s#", value)
                         or value.lower() in {"null", "true", "false", "yes", "no", "on", "off", "~", ".nan", ".inf", "-.inf", "+.inf"}
                         or re.fullmatch(r"[+-]?(?:\d[\d_.eE+-]*|0[xX][0-9a-fA-F]+)", value)
                         or re.match(r"\d{4}-\d\d-\d\d(?:$|[Tt ])", value))
            if ambiguous:
                return [("structure", "quote ambiguous frontmatter values as JSON strings")]
    return []


def _body_lines(body):
    """Non-blank body lines, frontmatter excluded — the altitude proxy."""
    m = _FRONTMATTER.match(body)
    rest = body[m.end():] if m else body
    return sum(1 for ln in rest.splitlines() if ln.strip())


def description_findings(desc):
    """Description is the host's recall key. Over-length truncates; a cue-less label never fires."""
    findings = []
    if len(desc) > config.INDEX_DESC_MAX_CHARS:
        findings.append(("description",
                         f"description is {len(desc)} chars — a new skill must fit the "
                         f"{config.INDEX_DESC_MAX_CHARS}-char index budget (one sentence, trigger "
                         f"first, ends with a period). The session-start index truncates longer "
                         f"descriptions to {config.INDEX_DESC_MAX_CHARS - 3} chars + '...', which "
                         "destroys the routing signal. Move the detail into the body"))
    if not _has_cue(desc):
        findings.append(("trigger",
                         "description has no trigger cue: add 'use when …' and/or a literal phrase "
                         "the user would type — an abstract topic-label never fires"))
    return findings


def _evidence_slice_denied(rel):
    if rel.startswith(layer.EVIDENCE_PREFIX):
        return [("files", f"{rel}: promoter-materialized evidence slices are off-limits to intents")]
    return []


def check_remove_path(rel):
    try:
        layer.check_subfile(rel)
    except ValueError as exc:
        return [("files", str(exc))]
    return _evidence_slice_denied(rel)


def check_files(files):
    if files is None:
        return []
    if not isinstance(files, dict):
        return [("files", "files must be a map of relative path -> content")]
    findings = []
    if len(files) > config.STAGE_MAX_FILES:
        findings.append(("files", f"more than {config.STAGE_MAX_FILES} subfiles"))
    total = 0
    for rel, content in files.items():
        try:
            layer.check_subfile(rel)
        except ValueError as exc:
            findings.append(("files", str(exc)))
            continue
        if not isinstance(content, str):
            findings.append(("files", f"{rel}: content must be a string"))
            continue
        if rel.endswith(".py"):
            try:
                ast.parse(content)
            except (SyntaxError, ValueError):
                findings.append(("structure", f"{rel} is not valid Python"))
        findings += _evidence_slice_denied(rel)
        size = len(content.encode("utf-8"))
        total += size
        if size > config.STAGE_MAX_FILE_BYTES:
            findings.append(("files", f"{rel} exceeds {config.STAGE_MAX_FILE_BYTES} bytes"))
    if total > config.STAGE_MAX_FILES_TOTAL_BYTES:
        findings.append(("files", f"subfiles exceed {config.STAGE_MAX_FILES_TOTAL_BYTES} bytes total"))
    return findings


def _structure(body, base_dir, files=None):
    findings = _flat_frontmatter_findings(body)
    fm = _frontmatter(body)
    if fm is None:
        findings.append(("structure", "missing/invalid frontmatter"))
        return findings
    if not fm.get("name"):
        findings.append(("structure", "missing name"))
    if not fm.get("description"):
        findings.append(("structure", "missing description"))
    if "category" in fm:  # open set; single safe segment; grouping key for the self-injected index only
        cat = fm["category"]
        if "/" in cat or not layer._SAFE_NAME.match(cat or ""):
            findings.append(("category",
                             f"category {cat!r} must be a single safe segment (letters/digits/._-)"))
    if base_dir is not None:
        base = base_dir.resolve()
        for ref in set(_PY_REF.findall(body)):
            f = base_dir / ref
            if not f.resolve().is_relative_to(base):
                findings.append(("structure", f"referenced {ref} escapes the skill directory"))
                continue
            if f.is_file() and ref not in (files or {}):
                try:
                    ast.parse(f.read_text())
                except SyntaxError:
                    findings.append(("structure", f"referenced {ref} has syntax error"))
        for ref in set(_SUBFILE_REF.findall(body)):
            # escaping refs are skipped silently: writes are gated by the
            # promoter's landing check, validation must not probe outside
            if (base_dir / ref).resolve().is_relative_to(base) \
                    and ref not in (files or {}) and not (base_dir / ref).is_file():
                findings.append(("structure", f"referenced {ref} neither carried in intent nor live"))
    for rel in files or {}:
        if isinstance(rel, str) and rel not in body:
            findings.append(("structure", f"carried subfile {rel} not referenced in SKILL.md body"))
    return findings


def structure(body, files=None):
    return _structure(body, None, files)


def validate(intent, body, *, target_is_agent_created=None, repo_name=None, base_dir=None):
    findings = []
    files = intent.get("files")

    if body is not None:
        identity = (_frontmatter(body) or {}).get("name")
        try:
            layer._check_name(identity)
        except ValueError:
            findings.append(("identity", "skill name must be a safe identifier"))
        if intent.get("name") and identity != intent["name"]:
            findings.append(("identity", "frontmatter name must match the target skill"))
        for content in [body, *((files or {}).values() if isinstance(files, dict) else [])]:
            if isinstance(content, str) and redact.contains_secret(content):
                findings.append(("secret", "proposed skill contains a credential pattern"))
                break
        guard = skills_guard.scan(body)
        if guard:
            findings.append(("safety", guard))

        findings += _structure(body, base_dir, files)
        findings += check_files(files)

        if _PLACEHOLDER.search(body):
            findings.append(("completeness", "contains TODO/placeholder"))

        # altitude: create/update author the full body, so this is where rule-level is set. patch is a
        # delta to a live skill -- capping it would strand existing over-long skills (can't even fix them).
        if intent.get("action") in ("create", "update"):
            n = _body_lines(body)
            if n > config.SKILL_BODY_MAX_LINES:
                findings.append(("altitude",
                                 f"body is {n} non-blank lines (> {config.SKILL_BODY_MAX_LINES}); "
                                 "state the rule, move detail to references/"))
            desc = (_frontmatter(body) or {}).get("description")
            if desc:  # absence is already caught by the structure check
                findings += description_findings(desc)

        contents = [v for v in (files or {}).values() if isinstance(v, str)]
        for content in contents:
            guard = skills_guard.scan(content)
            if guard:
                findings.append(("safety", guard))

        if intent.get("level") == "global":
            markers = _ABS_PATH.findall(body)
            for content in contents:
                markers += _ABS_PATH.findall(content)
            if repo_name and (repo_name in body or any(repo_name in c for c in contents)):
                markers.append(repo_name)
            if markers:
                findings.append(("global_repo_agnostic", markers))

    if intent.get("action") == "remove_file":
        findings += check_remove_path(intent.get("path"))

    if not (intent.get("reason") or "").strip() or not (intent.get("evidence") or "").strip():
        findings.append(("led", "intent missing reason/evidence"))

    if intent.get("action") in _MODIFY and target_is_agent_created is not True:
        findings.append(("self_produced", "target live skill not created_by:agent"))

    return {"ok": not findings, "findings": findings}
