"""Observe successful managed skill reads; SKILL.md loads are the usage proxy.

Codex has no Skill tool. A SKILL.md read increments ``use``; a supporting file
read increments ``view``. Neither proves adherence to a skill. Unknown command
syntax is ignored rather than treating an incidental path as a read.
"""
import ast
import json
import os
import re
import shlex
from pathlib import Path

from codex_autoharness import config
from codex_autoharness.lib import layer, sidecar, skill_store
from codex_autoharness.lib.locking import lock_roots

_READERS = {"cat", "head", "tail", "sed", "rg", "grep", "less", "more"}
_SHELLS = {"Bash", "exec_command", "shell", "ctx_shell"}
_NESTED = re.compile(r"(?:tools\.)?([\w]+)\(\s*\{([^{}]*)\}", re.S)
_LITERAL = r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''


def _skill_name(event):
    nested = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    for src in (event, nested):
        for key in ("skill_name", "skill", "name"):
            value = src.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _success(response):
    if isinstance(response, str):
        try:
            return _success(json.loads(response))
        except (ValueError, TypeError):
            return not re.search(r"(?:exited with code|exit(?:_code| code|:))\s*[:=]?\s*[1-9]\d*|\bERROR:", response, re.I)
    if isinstance(response, dict):
        if response.get("isError") or response.get("error"):
            return False
        for key in ("exit_code", "exitCode", "returncode"):
            if response.get(key) not in (None, 0, "0"):
                return False
        if response.get("session_id") and response.get("exit_code") is None:
            return False
        return all(_success(value) for key, value in response.items()
                   if key in ("content", "structuredContent", "output", "text"))
    if isinstance(response, list):
        return all(_success(item) for item in response)
    return response is not None


def _shell_paths(command, cwd):
    if not isinstance(command, str):
        return
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return
    segment = []
    for token in [*tokens, ";"]:
        if token not in (";", "&&", "||", "|"):
            segment.append(token)
            continue
        if not segment:
            continue
        verb = Path(segment[0]).name
        if verb == "cd" and len(segment) == 2:
            candidate = Path(segment[1]).expanduser()
            cwd = candidate if candidate.is_absolute() else Path(cwd) / candidate
        elif verb in _READERS and not (verb == "sed" and any(s.startswith("-i") for s in segment[1:])):
            # File names used as patterns or redirection destinations are not
            # read evidence. Leave complicated shell syntax uncounted.
            if any(">" in token or token in ("<<", "<<<") for token in segment):
                segment = []
                continue
            arguments = segment[1:]
            if verb in ("rg", "grep") and any(token in ("--files", "-l", "--files-with-matches", "-q", "--quiet") for token in arguments):
                segment = []
                continue
            skip_expression = verb in ("rg", "grep", "sed") and not any(token in ("-e", "--regexp", "-f", "--file") for token in arguments)
            for candidate in arguments:
                if candidate.startswith("-") or any(c in candidate for c in ("$", "`", "*", "?", "\n")):
                    continue
                if skip_expression:
                    skip_expression = False
                    continue
                yield candidate, cwd
        elif verb == "lean-ctx" and "-c" in segment:
            index = segment.index("-c") + 1
            if index < len(segment):
                yield from _shell_paths(segment[index], cwd)
        segment = []


def _literal_field(body, key):
    literal = r"(\[[^\[\]]*\])" if key == "paths" else _LITERAL
    match = re.search(r"\b" + re.escape(key) + r"\s*:\s*" + literal, body)
    if match:
        try:
            return ast.literal_eval(match.group(1))
        except (SyntaxError, ValueError):
            pass
    return None


def _read_paths(event):
    tool = str(event.get("tool_name") or "").split(".")[-1]
    raw_input = event.get("tool_input")
    args = raw_input if isinstance(raw_input, dict) else {}
    cwd = args.get("workdir") or args.get("cwd") or event.get("cwd") or os.getcwd()
    if tool == "Read" or tool.endswith("ctx_read") or tool in ("read_file", "read_text_file"):
        path = args.get("file_path") or args.get("path") or event.get("file_path")
        paths = [path, *(args.get("paths") if isinstance(args.get("paths"), list) else [])]
        for path in paths:
            if isinstance(path, str):
                yield path, cwd
    elif tool in _SHELLS or tool.endswith("ctx_shell"):
        yield from _shell_paths(args.get("command") or args.get("cmd"), cwd)
    elif tool == "exec":
        code = raw_input if isinstance(raw_input, str) else args.get("code") or args.get("source")
        if isinstance(code, str):
            # ponytail: only literal arguments are observed; dynamic JavaScript
            # needs the host's nested tool events for accurate attribution.
            for nested_tool, body in _NESTED.findall(code):
                nested_args = {key: value for key in ("path", "paths", "file_path", "command", "cmd", "cwd", "workdir")
                               if (value := _literal_field(body, key)) is not None}
                yield from _read_paths({"tool_name": nested_tool, "tool_input": nested_args, "cwd": cwd})


def _identity(file_path, cwd, roots):
    target = Path(file_path).expanduser()
    target = (target if target.is_absolute() else Path(cwd) / target).resolve()
    if not target.is_file():
        return None
    for lyr in layer.unique_layers(roots):
        base = layer.skills_dir(lyr, roots.get(lyr)).resolve()
        try:
            rel = target.relative_to(base)
        except ValueError:
            continue
        if len(rel.parts) > 1 and rel.parts[0] != ".archive":
            return lyr, rel.parts[0], "use" if rel.parts[1:] == ("SKILL.md",) else "view"
    return None


def _name_from_read_path(event, roots):
    for path, cwd in _read_paths(event):
        identity = _identity(path, cwd, roots)
        if identity:
            return identity[1]
    return None


def _count(name, roots, kind, lyr=None):
    if not name:
        return {"counted": False, "reason": "no_skill"}
    try:
        lyr = lyr or skill_store.find(name, roots)
    except ValueError:
        return {"counted": False, "reason": "bad_or_ambiguous"}
    if lyr is None:
        return {"counted": False, "reason": "not_managed"}
    root = roots.get(lyr)
    if not sidecar.is_agent_created(lyr, name, root):
        return {"counted": False, "reason": "not_agent_created"}
    bump = sidecar.bump_use if kind == "use" else sidecar.bump_view
    count = bump(lyr, name, root)
    return {"counted": True, "kind": kind, "level": lyr, "name": name, "count": count}


def on_skill_call(event, *, roots=None):
    if os.environ.get(config.CHILD_SESSION_ENV):
        return {"counted": False, "reason": "recursion_guard"}
    roots = roots or {}
    with lock_roots(roots):
        return _count(_skill_name(event), roots, "use")


def on_skill_read(event, *, roots=None):
    if os.environ.get(config.CHILD_SESSION_ENV):
        return {"counted": False, "reason": "recursion_guard"}
    if not _success(event.get("tool_response")):
        return {"counted": False, "reason": "unconfirmed_read"}
    roots = roots or {}
    results, seen = [], set()
    with lock_roots(roots):
        for path, cwd in _read_paths(event):
            identity = _identity(path, cwd, roots)
            if identity and identity not in seen:
                seen.add(identity)
                lyr, name, kind = identity
                results.append(_count(name, roots, kind, lyr))
    if len(results) == 1:
        return results[0]
    return {"counted": any(item["counted"] for item in results), "reads": results,
            "reason": "observed_reads" if results else "no_skill"}
