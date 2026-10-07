"""Exercise real Codex reflection against synthetic data in temporary skill roots.

Run: python3 tools/live_smoke.py --report docs/live-smoke.json
This invokes the configured Codex model and may incur provider charges.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_autoharness.hook import on_session_start, spawn  # noqa: E402
from codex_autoharness.lib import layer, ledger, sidecar  # noqa: E402

EPISODE = """user: We fixed a Python subprocess crash when a command wrote arbitrary bytes.
assistant: subprocess.run(..., text=True) decoded stdout automatically and raised UnicodeDecodeError on b'\\xff'.
user: The successful fix was to capture stdout as bytes, then decode with errors='replace' only for display. Never use lossy decoding for binary data that will be processed further.
assistant: Verified: capture_output=True with text=False preserves b'\\xff' exactly; stdout.decode('utf-8', errors='replace') displays a replacement character without crashing.
user: Please save this reusable lesson as a skill for future subprocess-output debugging.
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = {"real_codex": True, "synthetic_input": True, "isolated_skill_roots": True}
    with tempfile.TemporaryDirectory(prefix="codex-autoharness-live-test-") as tmp:
        roots = {layer.PROJECT: Path(tmp) / "project", layer.GLOBAL: Path(tmp) / "global"}
        verdicts = spawn.run(EPISODE, "live-learning-smoke", roots=roots, timeout_s=150)
        report["verdicts"] = verdicts
        skills = []
        for level, root in roots.items():
            for path in layer.skills_dir(level, root).glob("*/SKILL.md"):
                name = path.parent.name
                skills.append({"name": name, "level": level,
                               "managed": sidecar.is_agent_created(level, name, root),
                               "ledger_entries": len(ledger.read(level, name, root)),
                               "body": path.read_text()})
        report["skills"] = skills
        report["recall_index"] = on_session_start.recall_index(roots)
        report["passed"] = bool(skills and all(s["managed"] and s["ledger_entries"] for s in skills)
                                and report["recall_index"] and all(v["ok"] for v in verdicts))
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered)
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
