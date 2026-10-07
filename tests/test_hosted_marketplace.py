"""Exercise native Git marketplace updates offline, without a custom updater."""
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path

import pytest

from codex_autoharness import integration

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "https://github.com/Shaderzx/Autoharness-Codex.git"
MARKETPLACE = integration.PLUGIN_ID.split("@", 1)[1]


@pytest.mark.skipif(not shutil.which("codex"), reason="Codex CLI is not installed")
def test_native_git_marketplace_updates_and_preserves_commit_pins(tmp_path):
    """Verify native updates preserve ref pins, hook trust records, and user files."""
    source, home, work = (tmp_path / name for name in ("source", "codex-home", "work"))
    for directory in (source, home, work):
        directory.mkdir()
    files = {
        ".agents/plugins/marketplace.json": (ROOT / ".agents/plugins/marketplace.json").read_text(),
        ".codex-plugin/plugin.json": json.dumps({"name": "codex-autoharness", "version": "0.1.1",
                                                 "skills": "./skills", "hooks": "./hooks/hooks.json"}),
        "skills/probe/SKILL.md": "---\nname: probe\ndescription: Use when testing native updates.\n---\nfirst\n",
        "hooks/hooks.json": json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "printf '{}'"}]}]}}),
    }
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    # Only this child environment redirects the real HTTPS origin to an offline fixture.
    git_config = tmp_path / "git-config"
    git_config.write_text(f'[url "{source.as_uri()}"]\n insteadOf = {SOURCE}\n')
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in os.environ}
    env.update(CODEX_HOME=str(home), GIT_CONFIG_GLOBAL=str(git_config),
               GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")

    def git(*args):
        """Run a checked Git command against the isolated source fixture."""
        return subprocess.run(["git", "-C", str(source), *args], env=env, capture_output=True,
                              text=True, check=True, timeout=10).stdout.strip()

    def codex(*args, fixture_home=home):
        """Run a native plugin command in the selected fixture configuration home."""
        run = subprocess.run(["codex", *args, "--json"], cwd=work, env={**env, "CODEX_HOME": str(fixture_home)},
                             capture_output=True, text=True, timeout=20)
        assert run.returncode == 0, run.stderr
        return json.loads(run.stdout)

    def commit():
        """Commit fixture changes and return their revision for update and pin checks."""
        git("add", ".")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "fixture")
        return git("rev-parse", "HEAD")

    @contextmanager
    def native(fixture_home=home):
        """Yield an initialized app-server RPC client and close its fixture process."""
        process = subprocess.Popen(["codex", "app-server", "--stdio"], cwd=work,
                                   env={**env, "CODEX_HOME": str(fixture_home)}, text=True,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        messages = queue.Queue()

        def receive():
            """Queue native app-server messages for the bounded RPC response loop."""
            for line in process.stdout:
                messages.put(json.loads(line))

        reader = threading.Thread(target=receive, daemon=True)
        reader.start()

        def rpc(number, method, params):
            """Send one request and return its result while ignoring notifications."""
            process.stdin.write(json.dumps({"id": number, "method": method, "params": params}) + "\n")
            process.stdin.flush()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    message = messages.get(timeout=max(0.1, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if message.get("id") == number:
                    assert "error" not in message, message
                    return message["result"]
            raise AssertionError(f"No response to {method}")

        try:
            rpc(1, "initialize", {"clientInfo": {"name": "marketplace-fixture", "version": "0.1"},
                                  "capabilities": {"experimentalApi": True}})
            process.stdin.write('{"method":"initialized","params":{}}\n')
            process.stdin.flush()
            yield rpc
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=5)
            process.stdin.close()
            process.stdout.close()

    git("init", "-b", "main")
    first_revision = commit()
    added = codex("plugin", "marketplace", "add", SOURCE, "--ref", "main")
    assert added["marketplaceName"] == MARKETPLACE
    installed = codex("plugin", "add", integration.PLUGIN_ID)
    cache = Path(installed["installedPath"])
    assert installed["version"] == "0.1.1"
    # Trust only the temporary fixture, using the same API as /hooks.
    with native() as rpc:
        hooks = rpc(2, "hooks/list", {"cwds": [str(work)]})["data"][0]["hooks"]
        assert len(hooks) == 1 and hooks[0]["trustStatus"] == "untrusted"
        trust = {hooks[0]["key"]: {"trusted_hash": hooks[0]["currentHash"]}}
        rpc(3, "config/batchWrite", {"edits": [{"keyPath": "hooks.state", "value": trust, "mergeStrategy": "upsert"}],
                                    "reloadUserConfig": True})
        assert rpc(4, "hooks/list", {"cwds": [str(work)]})["data"][0]["hooks"][0]["trustStatus"] == "trusted"
    before = tomllib.loads((home / "config.toml").read_text())
    assert before["marketplaces"][MARKETPLACE]["source"] == SOURCE
    direct_hooks = home / "hooks.json"
    direct_hooks.write_text('{"hooks":{}}\n')
    user_skill = work / ".agents/skills/user-owned/SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("---\nname: user-owned\ndescription: User-authored fixture.\n---\nKeep this.\n")
    skill = source / "skills/probe/SKILL.md"
    skill.write_text(skill.read_text().replace("first", "second"))
    hook = source / "hooks/hooks.json"
    hook.write_text(hook.read_text().replace("printf '{}'", "printf '{ }'"))
    commit()  # Same plugin version; startup still refreshes the changed Git revision.
    with native() as rpc:
        rpc(2, "thread/start", {"cwd": str(work)})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (cache / "skills/probe/SKILL.md").exists() and "second" in (cache / "skills/probe/SKILL.md").read_text():
                break
            time.sleep(0.05)
        assert "second" in (cache / "skills/probe/SKILL.md").read_text()
        hooks = rpc(3, "hooks/list", {"cwds": [str(work)]})["data"][0]["hooks"]
        assert hooks[0]["trustStatus"] == "modified"
    after = tomllib.loads((home / "config.toml").read_text())
    assert after["hooks"] == before["hooks"]  # New contents require review; no trust is added.
    assert after["plugins"] == before["plugins"]
    assert after["marketplaces"][MARKETPLACE]["ref"] == "main"
    assert codex("plugin", "marketplace", "upgrade", MARKETPLACE)["upgradedRoots"] == []
    manifest = source / ".codex-plugin/plugin.json"
    manifest.write_text(manifest.read_text().replace("0.1.1", "0.1.2"))
    commit()
    assert codex("plugin", "marketplace", "upgrade", MARKETPLACE)["upgradedRoots"]
    assert (cache.parent / "0.1.2/skills/probe/SKILL.md").read_text() == skill.read_text()
    assert not cache.exists()
    pinned = tmp_path / "pinned-home"
    pinned.mkdir()
    codex("plugin", "marketplace", "add", SOURCE, "--ref", first_revision, fixture_home=pinned)
    pinned_cache = Path(codex("plugin", "add", integration.PLUGIN_ID, fixture_home=pinned)["installedPath"])
    with native(fixture_home=pinned) as rpc:
        rpc(2, "thread/start", {"cwd": str(work)})
        # The metadata reaches the plugin cache only after startup refresh completes.
        refreshed = pinned_cache / ".codex-marketplace-install.json"
        deadline = time.monotonic() + 10
        while not refreshed.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert refreshed.exists(), "Pinned marketplace startup refresh did not complete"
        active = next(plugin for plugin in codex("plugin", "list", "--marketplace", MARKETPLACE,
                                                  fixture_home=pinned)["installed"]
                      if plugin["pluginId"] == integration.PLUGIN_ID)
        pinned_cache = pinned_cache.parent / active["version"]
        assert json.loads((pinned_cache / ".codex-marketplace-install.json").read_text())["revision"] == first_revision
        assert (pinned_cache / "skills/probe/SKILL.md").read_text() == files["skills/probe/SKILL.md"]
        assert tomllib.loads((pinned / "config.toml").read_text())["marketplaces"][MARKETPLACE]["ref"] == first_revision
    codex("plugin", "marketplace", "upgrade", MARKETPLACE, fixture_home=pinned)
    active = next(plugin for plugin in codex("plugin", "list", "--marketplace", MARKETPLACE,
                                              fixture_home=pinned)["installed"]
                  if plugin["pluginId"] == integration.PLUGIN_ID)
    pinned_cache = pinned_cache.parent / active["version"]
    assert json.loads((pinned_cache / ".codex-marketplace-install.json").read_text())["revision"] == first_revision
    assert (pinned_cache / "skills/probe/SKILL.md").read_text() == files["skills/probe/SKILL.md"]
    assert tomllib.loads((pinned / "config.toml").read_text())["marketplaces"][MARKETPLACE]["ref"] == first_revision
    assert direct_hooks.read_text() == '{"hooks":{}}\n'
    assert user_skill.read_text().endswith("Keep this.\n")
