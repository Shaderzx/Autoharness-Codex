"""Install native Codex hooks while preserving other integrations and user skills."""
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from codex_autoharness.lib import atomic, layer, metrics, sidecar, skill_import

EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd")
SKILL_NAME = "codex-learn"
PLUGIN_ID = "codex-autoharness@codex-autoharness-local"


def roots(*, project=None, home=None):
    return {layer.GLOBAL: Path(home).expanduser().resolve() / ".agents" if home else layer.default_root(layer.GLOBAL),
            layer.PROJECT: layer._main_worktree_root(Path(project).expanduser().resolve()) / ".agents" if project else layer.default_root(layer.PROJECT)}


def _paths(project, home):
    base = Path(project or home or Path.home()).expanduser().resolve()
    return (layer.checked_path(base, ".codex", "hooks.json"),
            layer.checked_path(base, ".codex", "codex-autoharness"),
            layer.checked_path(base, ".agents", "skills", SKILL_NAME))


def _read_json(path, default):
    if path.is_symlink():
        raise ValueError(f"Refusing symlinked configuration file {path}")
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise ValueError(f"Cannot read valid JSON from {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def _hooks(path):
    data = _read_json(path, {})
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"Expected hooks object in {path}")
    for event, groups in hooks.items():
        if not isinstance(groups, list) or any(not isinstance(g, dict) or not isinstance(g.get("hooks", []), list) for g in groups):
            raise ValueError(f"Invalid hook groups for {event}")
    return data


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _remove_command(data, command):
    if not command:
        return
    hooks = data.get("hooks", {})
    for event, groups in list(hooks.items()):
        kept = []
        for group in groups:
            handlers = group.get("hooks", [])
            remaining = [h for h in handlers if not (isinstance(h, dict) and h.get("command") == command)]
            if len(remaining) == len(handlers):
                kept.append(group)
            elif remaining:
                kept.append({**group, "hooks": remaining})
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)


def _skill_text(command):
    return (Path(__file__).parent / "learn_skill.md").read_text().replace("codex-autoharness", command)


def install(*, project=None, home=None):
    """Install owned hooks and the learning helper, then import Claude skills for that scope."""
    hooks_path, install_dir, skill_dir = _paths(project, home)
    manifest_path = install_dir / "install.json"
    previous = _read_json(manifest_path, {})
    data = _hooks(hooks_path)
    skill_path = skill_dir / "SKILL.md"
    if skill_path.is_symlink() or (skill_path.exists() and _digest(skill_path.read_text()) != previous.get("skill_sha256")):
        raise ValueError(f"Existing {skill_path} is not an unchanged installer-owned skill")
    original = hooks_path.read_text() if hooks_path.exists() else None
    launcher = install_dir / "launcher.py"
    if launcher.is_symlink() or (launcher.exists() and _digest(launcher.read_text()) != previous.get("launcher_sha256")):
        raise ValueError(f"Existing {launcher} is not an unchanged installer-owned launcher")
    source = str(Path(__file__).resolve().parent.parent)
    launcher_text = ("# Installed by codex-autoharness.\nimport os\nimport sys\n"
                     f"sys.path.insert(0, {source!r})\n"
                     f"os.environ['PYTHONPATH'] = {source!r} + (os.pathsep + os.environ['PYTHONPATH'] if os.environ.get('PYTHONPATH') else '')\n"
                     "from codex_autoharness.cli import main\nraise SystemExit(main())\n")
    argv = [sys.executable, str(launcher)]
    if home:
        argv.extend(["--home", str(Path(home).resolve())])
    if project:
        argv.extend(["--project", str(Path(project).resolve())])
    command = shlex.join([*argv, "_hook"])
    _remove_command(data, previous.get("command"))
    hooks = data.setdefault("hooks", {})
    for event in EVENTS:
        groups = hooks.setdefault(event, [])
        if not any(isinstance(h, dict) and h.get("command") == command for g in groups for h in g.get("hooks", [])):
            groups.append({"hooks": [{"type": "command", "command": command,
                                      "timeout": 3 if event == "SessionEnd" else 30 if event == "Stop" else 10}]})
    rendered = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    skill = _skill_text(shlex.join(argv))
    if (hooks_path.read_text() if hooks_path.exists() else None) != original:
        raise ValueError("Hook configuration changed during installation; retry after the other edit completes")
    atomic.write_text(launcher, launcher_text)
    atomic.write_text(skill_path, skill)
    if rendered != original:
        atomic.write_text(hooks_path, rendered)
    manifest = {"version": 1, "command": command, "skill_sha256": _digest(skill),
                "launcher_sha256": _digest(launcher_text), "hooks_sha256": _digest(rendered),
                "original_hooks": previous.get("original_hooks", original)}
    atomic.write_text(manifest_path, json.dumps(manifest, indent=2) + "\n")
    level = layer.PROJECT if project else layer.GLOBAL
    imported = skill_import.import_layer(level, skill_dir.parent.parent)
    return {"ok": True, "hooks": str(hooks_path), "skill": str(skill_path), "launcher": str(launcher),
            "skill_import": imported,
            "trust_required": True, "next_step": "Restart Codex, open /hooks, review and trust the changed hooks source, including its preexisting hooks. Hook trust is never bypassed."}


def uninstall(*, project=None, home=None):
    hooks_path, install_dir, skill_dir = _paths(project, home)
    manifest_path = install_dir / "install.json"
    manifest = _read_json(manifest_path, {})
    if not manifest:
        return {"ok": True, "removed": [], "retained": [], "reason": "not installed"}
    data = _hooks(hooks_path)
    before = hooks_path.read_text() if hooks_path.exists() else ""
    _remove_command(data, manifest.get("command"))
    if _digest(before) == manifest.get("hooks_sha256"):
        original = manifest.get("original_hooks")
        if original is None:
            hooks_path.unlink(missing_ok=True)
        else:
            atomic.write_text(hooks_path, original)
    elif before and json.loads(before) != data:
        atomic.write_text(hooks_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    removed, retained = [], []
    for path, key in ((skill_dir / "SKILL.md", "skill_sha256"), (install_dir / "launcher.py", "launcher_sha256")):
        if path.exists():
            if not path.is_symlink() and _digest(path.read_text()) == manifest.get(key):
                path.unlink()
                removed.append(str(path))
            else:
                retained.append(str(path))
    manifest_path.unlink(missing_ok=True)
    for directory in (skill_dir, install_dir):
        try:
            directory.rmdir()
        except OSError:
            pass
    return {"ok": True, "removed": removed, "retained": retained,
            "note": "Learned skills, archives and learning history are retained."}


def status(*, project=None, home=None):
    resolved = roots(project=project, home=home)
    layers = {}
    for level in layer.unique_layers(resolved):
        root = resolved[level]
        skill_paths = sorted(layer.skills_dir(level, root).glob("*/SKILL.md"))
        managed = [p.parent.name for p in skill_paths if sidecar.is_agent_created(level, p.parent.name, root)]
        state = layer.state_dir(level, root)
        layers[level] = {"skills_dir": str(layer.skills_dir(level, root)), "state_dir": str(state),
                         "managed_skills": managed, "archived": len(list(layer.archive_dir(level, root).glob("*/SKILL.md"))),
                         "pending_runs": len(list((state / "intents").glob("*.jsonl")))}
    hooks_path, install_dir, _ = _paths(project, home)
    codex_home = Path(home).resolve() / ".codex" if home else Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    cache = codex_home / "plugins" / "cache" / "codex-autoharness-local" / "codex-autoharness"
    versions = []
    for candidate in cache.glob("*/.codex-plugin/plugin.json"):
        try:
            if json.loads(candidate.read_text()).get("name") == "codex-autoharness":
                versions.append(candidate.parents[1].name)
        except (ValueError, OSError, AttributeError):
            continue
    enabled = False
    try:
        with (codex_home / "config.toml").open("rb") as source:
            config = tomllib.load(source)
        enabled = config.get("plugins", {}).get(PLUGIN_ID, {}).get("enabled") is True
    except (ValueError, OSError, AttributeError):
        pass
    native = (install_dir / "install.json").exists()
    plugin = {"installed": bool(versions), "enabled": enabled, "versions": sorted(versions), "id": PLUGIN_ID}
    aliases = {name: layer.canonical_layer(name, resolved) for name in layer.LAYERS
               if layer.canonical_layer(name, resolved) != name}
    return {"ok": True, "installed": native or bool(versions), "native_hooks_installed": native,
            "plugin": plugin, "hooks": str(hooks_path), "layers": layers, "layer_aliases": aliases,
            "metrics": metrics.collect(resolved)}


def doctor(*, project=None, home=None):
    result = status(project=project, home=home)
    codex = shutil.which("codex")
    version = None
    if codex:
        try:
            run = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=5)
            version = run.stdout.strip() if run.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            pass
    result.update(python=sys.version.split()[0], python_supported=sys.version_info >= (3, 11),
                  codex=codex, codex_version=version,
                  hook_trust="Review trust in Codex /hooks; this command does not change trust.")
    result["ok"] = bool(result["python_supported"] and version)
    return result
