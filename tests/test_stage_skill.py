"""Proposal admission and the optional MCP transport; neither may write skills."""
import io
import json

import pytest

from codex_autoharness import config
from codex_autoharness.lib import intent_queue, layer
from codex_autoharness.stage_skill import server

BODY = "---\nname: foo\ndescription: Use when formatting dates.\n---\nUse ISO dates.\n"
COMMON = {"name": "foo", "reason": "Date output corrected", "evidence": "Use ISO dates."}


def create(**changes):
    return {**COMMON, "action": "create", "body": BODY, **changes}


def test_staging_preserves_action_payloads_without_writing_skills(tmp_path):
    proposals = [
        create(),
        create(level="global"),
        {**COMMON, "action": "update", "body": BODY + "See references/dates.md\n",
         "files": {"references/dates.md": "Details"}},
        {**COMMON, "action": "patch", "old_string": "ISO", "new_string": ""},
        {**COMMON, "action": "remove_file", "path": "references/dates.md"},
        {**COMMON, "action": "delete", "absorbed_into": "umbrella"},
    ]
    for proposal in proposals:
        result = server.stage(proposal, run_id="actions", root=tmp_path)
        assert result["ok"], result
    queued = [{k: v for k, v in row.items() if k != "_intent_id"}
              for row in intent_queue.read("actions", tmp_path)]
    assert queued == [{**proposals[0], "level": "project"}, *proposals[1:]]
    assert not layer.skills_dir("project", tmp_path).exists()


@pytest.mark.parametrize("proposal", [
    None,
    create(action="unknown"),
    create(name=""),
    create(name="../outside"),
    create(body=1),
    create(body=None),
    create(reason=""),
    create(evidence=""),
    create(extra="unknown"),
    create(level="planetary"),
    create(files=["references/dates.md"]),
    create(old_string="ISO", new_string="UTC"),
    create(path="scripts/check.py"),
    create(absorbed_into="umbrella"),
    {**COMMON, "action": "patch", "old_string": "ISO"},
    {**COMMON, "action": "patch", "old_string": "", "new_string": "UTC"},
    {**COMMON, "action": "patch", "old_string": "ISO", "new_string": "UTC", "body": BODY},
    {**COMMON, "action": "patch", "old_string": "ISO", "new_string": "UTC", "files": {}},
    {**COMMON, "action": "remove_file"},
    {**COMMON, "action": "remove_file", "path": "scripts/check.py", "files": {}},
    {**COMMON, "action": "delete", "body": BODY},
    {**COMMON, "action": "delete", "files": {}},
])
def test_invalid_proposals_never_enter_the_queue(tmp_path, proposal):
    result = server.stage(proposal, run_id="bad", root=tmp_path)
    assert not result["ok"] and any(family == "schema" for family, _ in result["errors"])
    assert intent_queue.read("bad", tmp_path) == []
    assert not layer.skills_dir("project", tmp_path).exists()


def test_content_feedback_and_paths_block_staging(tmp_path):
    for proposal, family in [
        (create(body="No frontmatter"), "structure"),
        (create(body=BODY.replace("Use when formatting dates.", "Use when " + "x" * 100)), "description"),
        (create(files={"references/dates.md": "Unreferenced"}), "structure"),
        (create(files={"references/dates.md": 7}), "files"),
        (create(body=BODY + "See scripts/check.py\n", files={"scripts/check.py": "def broken(:\n"}), "structure"),
    ]:
        result = server.stage(proposal, run_id="bad", root=tmp_path)
        assert not result["ok"] and any(f == family for f, _ in result["errors"]), result
    for path in ("../outside", "/etc/passwd", "scripts/../SKILL.md", "scripts/a\\b", "references//x",
                 "references/.hidden", "SKILL.md", ".sidecar.json", ".ledger.jsonl", "bin/check.py",
                 "references/evidence-forged.md"):
        for proposal in (create(files={path: "text"}), {**COMMON, "action": "remove_file", "path": path}):
            result = server.stage(proposal, run_id="bad", root=tmp_path)
            assert not result["ok"] and any(f == "files" for f, _ in result["errors"]), (path, result)
    assert intent_queue.read("bad", tmp_path) == []
    assert not server.stage(create(), run_id="../escape", root=tmp_path)["ok"]


@pytest.mark.parametrize("setting,limit", [
    ("STAGE_MAX_BODY_BYTES", 10), ("STAGE_MAX_FILES", 1),
    ("STAGE_MAX_FILE_BYTES", 3), ("STAGE_MAX_FILES_TOTAL_BYTES", 10),
])
def test_staging_size_limits_leave_no_queued_work(tmp_path, monkeypatch, setting, limit):
    monkeypatch.setattr(config, setting, limit)
    proposal = create(body=BODY + "See references/one.md and references/two.md\n",
                      files={"references/one.md": "first notes", "references/two.md": "second notes"})
    assert not server.stage(proposal, run_id="large", root=tmp_path)["ok"]
    assert intent_queue.read("large", tmp_path) == []


def test_mcp_rejects_invalid_calls_and_ignores_notifications(tmp_path):
    call = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "stage_skill", "arguments": create()}}
    requests = [call, {**call, "id": 1, "params": {"name": "unknown"}},
                {**call, "id": 2, "params": {"name": "stage_skill", "arguments": create(body=1)}},
                {"jsonrpc": "2.0", "method": "tools/list", "id": 3}]
    output = io.StringIO()
    server.serve(io.StringIO("not-json\n" + "\n".join(map(json.dumps, requests))), output, root=tmp_path)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(replies) == 4
    assert replies[0]["error"]["code"] == -32700
    assert replies[1]["error"]["code"] == -32602
    assert replies[2]["result"]["isError"]
    tool = replies[3]["result"]["tools"][0]
    assert tool["name"] == "stage_skill" and "remove_file" in tool["inputSchema"]["properties"]["action"]["enum"]
    assert intent_queue.orphans(tmp_path) == []
