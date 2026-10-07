import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_status_distinguishes_cached_and_enabled_plugin(tmp_path):
    from codex_autoharness import integration
    home = tmp_path / "home"
    codex_home = home / ".codex"
    manifest = codex_home / "plugins/cache/codex-autoharness-local/codex-autoharness/0.1.0/.codex-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"name":"codex-autoharness","version":"0.1.0"}')
    config = codex_home / "config.toml"
    config.write_text('[plugins."codex-autoharness@codex-autoharness-local"]\nenabled = true\n')
    result = integration.status(home=home, project=tmp_path / "project")
    assert result["installed"] is True
    assert result["native_hooks_installed"] is False
    assert result["plugin"]["enabled"] is True
    assert result["plugin"]["versions"] == ["0.1.0"]
    config.write_text('[plugins."codex-autoharness@codex-autoharness-local"]\nenabled = false\n')
    assert integration.status(home=home)["plugin"]["enabled"] is False


def test_plugin_marketplace_and_manifest_reference_real_resources():
    manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
    marketplace = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text())
    entry = marketplace["plugins"][0]
    assert entry["name"] == manifest["name"] == "codex-autoharness"
    assert entry["source"] == {"source": "local", "path": "./"}
    assert (ROOT / manifest["hooks"]).is_file()
    assert (ROOT / manifest["skills"] / "learn" / "SKILL.md").is_file()


def test_plugin_hook_and_learn_launcher_work_without_installation(tmp_path):
    plugin = tmp_path / "plugin with spaces"
    for directory in ("src", "bin", "hooks", "skills"):
        shutil.copytree(ROOT / directory, plugin / directory, ignore=shutil.ignore_patterns("__pycache__"))
    project = tmp_path / "project"
    project.mkdir()
    env = {**os.environ, "PLUGIN_ROOT": str(plugin), "PYTHONPATH": "",
           "CODEX_AUTOHARNESS_GLOBAL_ROOT": str(tmp_path / "global"),
           "CODEX_AUTOHARNESS_PROJECT_ROOT": str(project / ".agents"),
           "CODEX_AUTOHARNESS_CHILD_SESSION": "1"}
    hooks = json.loads((plugin / "hooks" / "hooks.json").read_text())
    hook = hooks["hooks"]["SessionStart"][0]["hooks"][0]
    result = subprocess.run(["/bin/sh", "-c", hook["command"]],
                            input=json.dumps({"hook_event_name": "SessionStart", "cwd": str(project),
                                              "session_id": "plugin-smoke"}),
                            text=True, capture_output=True, cwd=project, env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    assert isinstance(json.loads(result.stdout), dict)
    helper = plugin / "skills" / "learn" / "scripts" / "codex-autoharness.py"
    result = subprocess.run([sys.executable, str(helper), "status"], text=True,
                            capture_output=True, cwd=project, env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["layers"]["global"]["skills_dir"] == str(tmp_path / "global" / "skills")
