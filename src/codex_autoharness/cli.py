"""Codex AutoHarness command line; mutation paths share the admission pipeline."""
import argparse
import json
import sys
import uuid
from pathlib import Path

from codex_autoharness import __version__, integration
from codex_autoharness.hook import on_session_start, promoter
from codex_autoharness.lib import layer, ledger, sidecar, skill_import, skill_store
from codex_autoharness.stage_skill import server


def parser():
    """Define maintenance commands and global options for selecting isolated roots."""
    p = argparse.ArgumentParser(prog="codex-autoharness", description="Learn, curate and recall native Codex skills from real sessions.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--project", type=Path, help="Project directory (defaults to the current project); install uses global scope unless supplied")
    p.add_argument("--home", type=Path, help="Explicit home directory for an isolated installation and global layer")
    commands = p.add_subparsers(dest="command", required=True)
    for name, help_text in (("install", "Install six native hooks and $codex-learn; trust them in /hooks"),
                            ("import-skills", "Copy existing global and project Claude skills without overwriting Codex skills"),
                            ("uninstall", "Remove installer-owned hooks and helper; retain learned skills"),
                            ("status", "Show managed skills, roots and pending proposals"),
                            ("doctor", "Check Python, Codex and installation; explain hook trust"),
                            ("index", "Print the grouped managed skill index"),
                            ("spec", "Print the skill admission format"),
                            ("curate", "Consolidate managed skills with a read-only Codex proposer"),
                            ("mcp", "Serve stage_skill over MCP stdio"),
                            ("_hook", "Handle one native Codex hook event from stdin")):
        commands.add_parser(name, help=help_text)
    learn = commands.add_parser("learn", help="Distill a supplied Codex JSONL transcript now")
    learn.add_argument("--transcript", type=Path, required=True, help="Exact session transcript to distill")
    learn.add_argument("--session-id", default="manual")
    stage = commands.add_parser("stage", help="Validate and apply one JSON proposal through the sole writer")
    stage.add_argument("--file", type=Path, help="Proposal JSON file; default stdin")
    stage.add_argument("--queue-only", action="store_true", help="Queue for the next Stop hook instead of applying now")
    for name in ("archive", "restore"):
        cmd = commands.add_parser(name, help=f"{name.title()} one managed skill; preserve user-authored skills")
        cmd.add_argument("name")
        cmd.add_argument("--level", choices=layer.LAYERS, default=layer.PROJECT)
        if name == "restore":
            cmd.add_argument("--snapshot", type=Path, help="Recover this skill from a pre-curation .tar.gz snapshot")
    history = commands.add_parser("history", help="Show run accounts or one managed skill's ledger")
    history.add_argument("name", nargs="?")
    history.add_argument("--level", choices=layer.LAYERS, default=layer.PROJECT)
    history.add_argument("--limit", type=int, default=10)
    use = commands.add_parser("record-use", help="Record an explicit managed skill invocation")
    use.add_argument("name")
    use.add_argument("--level", choices=layer.LAYERS, default=layer.PROJECT)
    return p


def _emit(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
    return 0 if value.get("ok", True) else 1


def _history(args, roots):
    root = roots[args.level]
    if args.name:
        if not sidecar.is_agent_created(args.level, args.name, root):
            raise ValueError("Skill is not managed by Codex AutoHarness")
        return {"ok": True, "name": args.name, "entries": ledger.read(args.level, args.name, root)[-max(0, args.limit):]}
    state = layer.state_dir(args.level, root)
    paths = sorted((state / "runs").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {"ok": True, "runs": [json.loads(p.read_text()) for p in paths[:max(0, args.limit)]]}


def _archive_restore(args, roots):
    root = roots[args.level]
    if args.command == "archive":
        if not sidecar.is_agent_created(args.level, args.name, root):
            raise ValueError("Only managed skills can be archived")
        dest = skill_store.archive(args.level, args.name, root)
    elif args.snapshot:
        dest = skill_store.restore_snapshot(args.level, args.name, args.snapshot, root)
    else:
        layer._check_name(args.name)
        archived = layer.archive_dir(args.level, root) / args.name
        if not archived.is_dir():
            raise ValueError("Archived skill does not exist")
        dest = skill_store.restore(args.level, args.name, root)
    return {"ok": dest is not None, "path": str(dest) if dest else None}


def main(argv=None):
    """Dispatch a CLI command and return its exit status after reporting structured results."""
    args = parser().parse_args(argv)
    roots = integration.roots(project=args.project, home=args.home)
    if hasattr(args, "level"):
        args.level = layer.canonical_layer(args.level, roots)
    try:
        if args.command == "_hook":
            from codex_autoharness.hook import dispatch
            if args.home or args.project:
                try:
                    event = json.load(sys.stdin)
                except (ValueError, TypeError):
                    print("{}")
                    return 0
                if not isinstance(event, dict):
                    print("{}")
                    return 0
                event_roots = integration.roots(project=args.project or event.get("cwd"), home=args.home)
                dispatch._emit(dispatch.dispatch(event, roots=event_roots))
                return 0
            return dispatch.main()
        if args.command in ("install", "uninstall", "status", "doctor"):
            return _emit(getattr(integration, args.command)(project=args.project, home=args.home))
        if args.command == "import-skills":
            imported = skill_import.import_skills(roots)
            ok = all(reason == "destination exists" for result in imported.values() for reason in result["skipped"].values())
            return _emit({"ok": ok, "layers": imported})
        if args.command == "index":
            print(on_session_start.recall_index(roots, cwd=str(args.project or Path.cwd())) or "No managed skills yet.")
            return 0
        if args.command == "spec":
            from codex_autoharness import config
            print(config.FORMAT_SPEC.read_text())
            return 0
        if args.command == "stage":
            from codex_autoharness import config
            proposal = json.loads(args.file.read_text() if args.file else sys.stdin.read())
            run_id = config.INTERACTIVE_RUN_ID if args.queue_only else "manual-" + uuid.uuid4().hex
            result = server.stage(proposal, run_id=run_id, root=roots[layer.PROJECT])
            if not result["ok"] or args.queue_only:
                return _emit({"ok": result["ok"], "errors": result["errors"], "run_id": run_id})
            verdicts = promoter.drain(run_id, roots=roots)
            return _emit({"ok": all(v["ok"] for v in verdicts), "run_id": run_id, "verdicts": verdicts})
        if args.command in ("learn", "curate"):
            from codex_autoharness.hook import capture, spawn
            run_id = "manual-" + uuid.uuid4().hex
            if args.command == "learn":
                if not args.transcript.is_file():
                    raise ValueError("Transcript file does not exist")
                window, end_offset = capture.window(args.transcript)
                settings = capture.model_settings(args.transcript, end_offset=end_offset)
                if "reasoning_effort" in settings and settings["reasoning_effort"] is None:
                    settings["reasoning_effort"] = ""
                verdicts = spawn.run(window, run_id, roots=roots, session_id=args.session_id, **settings)
            else:
                verdicts = spawn.run_curator(run_id, roots=roots)
            return _emit({"ok": all(v["ok"] for v in verdicts), "run_id": run_id, "verdicts": verdicts})
        if args.command in ("archive", "restore"):
            return _emit(_archive_restore(args, roots))
        if args.command == "history":
            return _emit(_history(args, roots))
        if args.command == "record-use":
            if not sidecar.is_agent_created(args.level, args.name, roots[args.level]):
                raise ValueError("Skill is not managed by Codex AutoHarness")
            sidecar.bump_use(args.level, args.name, roots[args.level])
            return _emit({"ok": True, "name": args.name, "level": args.level})
        if args.command == "mcp":
            server.serve(root=roots[layer.PROJECT])
            return 0
    except (OSError, ValueError, RuntimeError) as exc:
        # Errors name the operation without echoing transcript or proposal secrets.
        return _emit({"ok": False, "error": str(exc)})
    return 0
