"""Review regressions exercise real process death at promotion boundaries."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codex_autoharness.hook import promoter
from codex_autoharness.lib import intent_queue, ledger, sidecar, skill_store

BODY = "---\nname: dates\ndescription: Use when formatting dates.\ncategory: testing\n---\nUse ISO dates.\n"


def _roots(tmp_path):
    return {"project": tmp_path / "project", "global": tmp_path / "global"}


def _proposal(**changes):
    return {"action": "create", "name": "dates", "level": "project", "body": BODY,
            "reason": "Date output was corrected", "evidence": "Use ISO dates.", **changes}


def _kill_during_land(roots, seam):
    code = """
import os, sys
from pathlib import Path
from codex_autoharness.hook import promoter
from codex_autoharness.lib import sidecar, ledger
roots = {"project": Path(sys.argv[1]), "global": Path(sys.argv[2])}
def crash(*args, **kwargs):
    os._exit(37)
if sys.argv[3] == "owner":
    sidecar.create = crash
elif sys.argv[3] == "ledger":
    ledger.append = crash
elif sys.argv[3] == "partial-ledger":
    def crash_ledger(level, name, entry, root):
        with ledger.path(level, name, root).open("a") as stream:
            stream.write('{"action":"patch",')
            stream.flush()
            os.fsync(stream.fileno())
        os._exit(37)
    ledger.append = crash_ledger
elif sys.argv[3] == "backup":
    original_copytree = promoter.shutil.copytree
    def crash_copytree(source, target, *args, **kwargs):
        Path(target).mkdir(parents=True)
        (Path(target) / "partial").write_text("unfinished backup")
        os._exit(37)
    promoter.shutil.copytree = crash_copytree
promoter.drain("crash-review", roots=roots)
"""
    env = dict(os.environ)
    source = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [source, env.get("PYTHONPATH")]))
    result = subprocess.run([sys.executable, "-c", code, str(roots["project"]),
                             str(roots["global"]), seam], env=env, timeout=10,
                            capture_output=True, text=True)
    assert result.returncode == 37, result.stderr


def test_create_recovers_after_process_dies_before_owner_stamp(tmp_path):
    roots = _roots(tmp_path)
    intent_queue.append("crash-review", _proposal(), roots["project"])
    _kill_during_land(roots, "owner")
    result = promoter.drain("crash-review", roots=roots)
    assert result[0]["ok"], result
    assert sidecar.is_agent_created("project", "dates", roots["project"])
    assert skill_store.read_body("project", "dates", roots["project"]) == BODY
    assert len(ledger.read("project", "dates", roots["project"])) == 1
    assert intent_queue.read("crash-review", roots["project"]) == []


@pytest.mark.parametrize("seam", ["ledger", "partial-ledger"])
def test_patch_recovers_after_process_dies_before_ledger_commit(tmp_path, seam):
    roots = _roots(tmp_path)
    assert promoter.promote(_proposal(), roots=roots)["ok"]
    patch = {"action": "patch", "name": "dates", "old_string": "Use ISO dates.",
             "new_string": "Use UTC dates.", "reason": "Prefer UTC date output",
             "evidence": "Use UTC dates."}
    intent_queue.append("crash-review", patch, roots["project"])
    _kill_during_land(roots, seam)
    result = promoter.drain("crash-review", roots=roots)
    assert result[0]["ok"], result
    assert skill_store.read_body("project", "dates", roots["project"]) == BODY.replace("ISO", "UTC")
    assert len(ledger.read("project", "dates", roots["project"])) == 2
    assert intent_queue.read("crash-review", roots["project"]) == []


@pytest.mark.parametrize("action", ["create", "update", "patch"])
def test_skill_cannot_change_identity_through_frontmatter(tmp_path, action):
    roots = _roots(tmp_path)
    before = None
    if action != "create":
        assert promoter.promote(_proposal(), roots=roots)["ok"]
        before = skill_store.read_body("project", "dates", roots["project"])
    if action == "patch":
        proposal = {"action": "patch", "name": "dates", "old_string": "name: dates",
                    "new_string": "name: other", "reason": "Change identity",
                    "evidence": "Use ISO dates."}
    else:
        proposal = _proposal(action=action, body=BODY.replace("name: dates", "name: other"))
    result = promoter.promote(proposal, roots=roots)
    assert not result["ok"], result
    assert skill_store.read_body("project", "dates", roots["project"]) == before


def test_update_recovers_after_process_dies_while_copying_backup(tmp_path):
    roots = _roots(tmp_path)
    assert promoter.promote(_proposal(), roots=roots)["ok"]
    update = _proposal(action="update", body=BODY.replace("ISO", "UTC"))
    intent_queue.append("crash-review", update, roots["project"])
    _kill_during_land(roots, "backup")
    result = promoter.drain("crash-review", roots=roots)
    assert result[0]["ok"], result
    assert skill_store.read_body("project", "dates", roots["project"]) == update["body"]
    assert len(ledger.read("project", "dates", roots["project"])) == 2


def test_drain_keeps_intent_after_transient_storage_failure(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    intent_queue.append("crash-review", _proposal(), roots["project"])
    original = promoter._land
    def disk_full(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(promoter, "_land", disk_full)
    try:
        promoter.drain("crash-review", roots=roots)
    except OSError:
        pass
    assert len(intent_queue.read("crash-review", roots["project"])) == 1
    monkeypatch.setattr(promoter, "_land", original)
    result = promoter.drain("crash-review", roots=roots)
    assert result[0]["ok"], result
    assert sidecar.is_agent_created("project", "dates", roots["project"])
