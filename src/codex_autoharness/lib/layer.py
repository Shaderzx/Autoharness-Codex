"""Resolves a layer name into its on-disk location — the one place where a layer surfaces as a path.

The rest of the code is layer-agnostic and passes layer in as an argument. Unknown layers / unsafe
symbol names are always rejected (fail-safe, deny-by-default), because this is the chokepoint that
builds filesystem paths: privilege escalation / path traversal must be stopped here.

The project layer's identity is the session cwd — except inside a linked git worktree, where it is
remapped to the main worktree root, so counters / intents / skills from all worktrees of one repo
land in one place and survive worktree removal. Only the linked-worktree case is remapped (git-dir
differs from git-common-dir): plain repos, repo subdirectories, and non-git directories keep cwd
verbatim, so a nested project can never be attributed to an enclosing repo. Any git failure falls
back to cwd (fail-safe).
"""
import os
import re
import subprocess
from functools import cache
from pathlib import Path

GLOBAL = "global"
PROJECT = "project"
LAYERS = (GLOBAL, PROJECT)

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _check_layer(layer):
    if layer not in LAYERS:
        raise ValueError("unknown layer (expected global or project)")


def _check_name(name):
    if not isinstance(name, str) or ".." in name or not _SAFE_NAME.match(name):
        raise ValueError("unsafe symbol name")


def _main_worktree_root(cwd):
    return _main_worktree_root_resolved(str(Path(cwd).resolve()))


@cache
def _main_worktree_root_resolved(cwd):
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-dir", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        lines = proc.stdout.splitlines()
        if proc.returncode != 0 or len(lines) < 2:
            return Path(cwd)
        git_dir, common_dir = ((Path(cwd) / line).resolve() for line in lines[:2])
        if git_dir != common_dir:
            return common_dir.parent
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd)


def default_root(layer):
    _check_layer(layer)
    override = os.environ.get(f"CODEX_AUTOHARNESS_{layer.upper()}_ROOT")
    if override:
        return Path(override).expanduser().absolute()
    if layer == GLOBAL:
        return Path.home() / ".agents"
    return _main_worktree_root(str(Path.cwd())) / ".agents"


def checked_root(root):
    """Canonicalize ancestors while refusing a redirected owned root itself."""
    root = Path(root).expanduser().absolute()
    if root.is_symlink():
        raise ValueError("managed root must not be a symlink")
    return root.parent.resolve() / root.name


def checked_path(root, *parts):
    root = checked_root(root)
    target = root
    for part in parts:
        for segment in Path(part).parts:
            if segment in ("..", ".") or Path(segment).is_absolute():
                raise ValueError("path escapes managed root")
            target = target / segment
            if target.is_symlink():
                raise ValueError("managed path must not contain a symlink")
    if not target.resolve().is_relative_to(root):
        raise ValueError("path escapes managed root")
    return target


def _root(layer, root):
    _check_layer(layer)
    return checked_root(root if root is not None else default_root(layer))


def unique_layers(roots=None):
    """A session in HOME shares one physical .agents root: treat it as global."""
    roots = roots or {}
    seen = set()
    result = []
    for lyr in LAYERS:
        resolved = _root(lyr, roots.get(lyr))
        if resolved not in seen:
            seen.add(resolved)
            result.append(lyr)
    return tuple(result)


def canonical_layer(lyr, roots=None):
    _check_layer(lyr)
    roots = roots or {}
    if lyr == PROJECT and unique_layers(roots) == (GLOBAL,):
        return GLOBAL
    return lyr


def skills_dir(layer, root=None):
    return checked_path(_root(layer, root), "skills")


def archive_dir(layer, root=None):
    return checked_path(_root(layer, root), "skills", ".archive")


def state_dir(layer, root=None):
    return checked_path(_root(layer, root), "codex-autoharness")


def symbol_dir(layer, name, root=None):
    _check_name(name)
    return checked_path(_root(layer, root), "skills", name)


SUBFILE_DIRS = ("scripts", "templates", "assets", "references")
EVIDENCE_PREFIX = "references/evidence-"


def check_subfile(rel):
    if not isinstance(rel, str) or not rel or ".." in rel or "\\" in rel:
        raise ValueError("unsafe subfile path")
    segments = rel.split("/")
    if len(segments) < 2 or segments[0] not in SUBFILE_DIRS:
        raise ValueError(f"subfile path must sit under one of {SUBFILE_DIRS}")
    for segment in segments:
        _check_name(segment)


def subfile_path(layer, name, rel, root=None):
    check_subfile(rel)
    _check_name(name)
    return checked_path(_root(layer, root), "skills", name, rel)
