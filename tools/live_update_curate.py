"""Verify real Codex patching and curation using synthetic temporary skills.

Run: python3 tools/live_update_curate.py --report docs/live-update-curate.json
Invokes the configured Codex provider twice and may incur model charges.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_autoharness.hook import spawn  # noqa: E402
from codex_autoharness.lib import layer, ledger, sidecar, skill_store  # noqa: E402

BODY = """---
name: subprocess-bytes
description: Use when subprocess output contains arbitrary bytes.
category: debugging
---
Capture subprocess stdout as bytes. Decode with replacement only when displaying to a human; preserve bytes for further processing.
"""
NARROW_BODY = """---
name: subprocess-stderr-bytes
description: Use when subprocess stderr contains arbitrary bytes.
category: debugging
---
Capture subprocess stderr as bytes too; invalid UTF-8 can occur on either output stream. Keep the original bytes when processing output.
"""
EPISODE = """user: Improve our existing subprocess-bytes skill: capture both stdout and stderr as bytes because either stream can contain invalid UTF-8. Decode only at the display boundary.
assistant: Verified with a subprocess that writes b'\\xff' to both stdout and stderr; capture_output=True, text=False preserved both streams exactly.
user: This belongs in the existing managed skill, so patch it instead of creating another skill.
"""


def _seed(roots, name, body):
    skill_store.write_body(layer.PROJECT, name, body, roots[layer.PROJECT])
    sidecar.create(layer.PROJECT, name, 0, roots[layer.PROJECT])


def verify():
    report = {"real_codex": True, "synthetic_input": True, "isolated_skill_roots": True}
    with tempfile.TemporaryDirectory(prefix="codex-autoharness-live-update-") as tmp:
        roots = {layer.PROJECT: Path(tmp) / "project", layer.GLOBAL: Path(tmp) / "global"}
        _seed(roots, "subprocess-bytes", BODY)
        verdicts = spawn.run(EPISODE, "live-update-smoke", roots=roots, timeout_s=120)
        after = skill_store.read_body(layer.PROJECT, "subprocess-bytes", roots[layer.PROJECT])
        report["update"] = {
            "input": "Add byte-safe stderr handling to existing subprocess-bytes skill.",
            "all_verdicts_ok": bool(verdicts and all(v["ok"] for v in verdicts)),
            "body_changed": after != BODY,
            "preserves_stdout_and_stderr": "stdout" in after and "stderr" in after,
            "ledger_entries": len(ledger.read(layer.PROJECT, "subprocess-bytes", roots[layer.PROJECT])),
        }
    with tempfile.TemporaryDirectory(prefix="codex-autoharness-live-curate-") as tmp:
        roots = {layer.PROJECT: Path(tmp) / "project", layer.GLOBAL: Path(tmp) / "global"}
        _seed(roots, "subprocess-bytes", BODY)
        _seed(roots, "subprocess-stderr-bytes", NARROW_BODY)
        verdicts = spawn.run_curator("live-curate-smoke", roots=roots, timeout_s=120)
        live = [p for level in layer.LAYERS for p in layer.skills_dir(level, roots[level]).glob("*/SKILL.md")]
        body = live[0].read_text() if len(live) == 1 else ""
        snapshots = list((layer.state_dir(layer.PROJECT, roots[layer.PROJECT]) / "snapshots").glob("*.tar.gz"))
        archived = [p for level in layer.LAYERS for p in layer.archive_dir(level, roots[level]).glob("*/SKILL.md")]
        report["curation"] = {
            "input": "Merge managed subprocess-bytes and subprocess-stderr-bytes skills.",
            "all_verdicts_ok": bool(verdicts and all(v["ok"] for v in verdicts)),
            "live_skill_count": len(live),
            "preserves_stdout_and_stderr": "stdout" in body and "stderr" in body,
            "managed_snapshot_count": len(snapshots),
            "archived_skill_count": len(archived),
        }
    update, curate = report["update"], report["curation"]
    report["passed"] = bool(
        update["all_verdicts_ok"] and update["body_changed"] and update["preserves_stdout_and_stderr"]
        and update["ledger_entries"] and curate["all_verdicts_ok"] and curate["live_skill_count"] == 1
        and curate["preserves_stdout_and_stderr"] and curate["managed_snapshot_count"] == 1
        and curate["archived_skill_count"] == 1
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = verify()
    rendered = json.dumps(report, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered)
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
