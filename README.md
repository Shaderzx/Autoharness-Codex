# Codex AutoHarness

Codex AutoHarness learns reusable skills from Codex sessions, keeps related lessons together, and archives skills that stop getting used. Skills remain ordinary `SKILL.md` files under `.agents/skills`, so Codex can discover them through its native skill system.

Inspired by [Tigerless Labs AutoHarness](https://github.com/tigerless-labs/autoharness).

## Requirements

- Python 3.11 or later as `python3` on `PATH`; no third-party runtime dependencies.
- Codex CLI 0.160.1, authenticated for `codex exec`. This is the tested version; older versions may reject required feature flags.
- macOS or Linux.
- A Codex surface that runs native lifecycle hooks, with this installation trusted through `/hooks`.

The CLI also supports explicit learning from a transcript when hooks are unavailable. Installing hooks does not prove that a particular Codex Desktop, CLI, or Cloud version will run them.

## Install

Clone the repository and enter its directory:

```sh
git clone https://github.com/Shaderzx/Autoharness-Codex.git
cd Autoharness-Codex
```

Then install the plugin:

```sh
codex plugin marketplace add .
codex plugin add codex-autoharness@codex-autoharness-local
```

Restart Codex, open `/hooks`, and review and trust the Codex AutoHarness plugin source. The plugin carries its own hooks source and leaves `~/.codex/hooks.json` unchanged. Codex trusts the source's exact contents, so an update can require renewed approval. Installation never changes hook trust.

The local marketplace makes this checkout installable without a hosted release. Keep it available for future local reinstalls. The plugin includes the `$codex-learn` helper and a portable Python launcher.

For the maintenance CLI, install the Python package in a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/codex-autoharness doctor
```

The CLI can also run directly from the checkout as `python3 bin/codex-autoharness.py`. The command examples below use `codex-autoharness`; substitute `.venv/bin/codex-autoharness` or that direct invocation if it is not on `PATH`.

### Direct hooks installation

For a Codex setup that supports native hooks but does not use plugins:

```sh
.venv/bin/codex-autoharness install
```

This alternative adds entries to `~/.codex/hooks.json`, a launcher and manifest under `~/.codex/codex-autoharness/`, and the `$codex-learn` helper skill under `~/.agents/skills/`. Existing hook entries are preserved. An existing, unrelated `codex-learn` skill causes installation to fail rather than overwrite it. Keep the checkout and virtual environment in place: the launcher points to this interpreter and source directory.

Restart Codex and use `/hooks` to review and trust the changed source, including its preexisting commands. Direct installation changes that source's contents and therefore its trust hash.

For a project-only installation, supply its directory before the subcommand:

```sh
.venv/bin/codex-autoharness --project /path/to/project install
```

Choose one integration path. Installing both plugin and direct hooks, or both direct scopes for the same project, can produce duplicate events. Use `--home /path/to/test-home` for an isolated direct installation during testing.

## Use it

Work in Codex as usual. After a session accumulates 50 tool calls, its next completed turn starts a background reflection. That reflection compares the recent work with existing skills and proposes a new skill, an update, a merge, or nothing. A separate validator applies accepted changes and records rejected ones. Every 250 tool calls, a curator can consolidate the managed library. Session end flushes pending work.

Each background learner and curator uses the **model of the session that triggered it**, including an in-session model switch. Provider identity and reasoning effort follow that session's metadata; credentials come from the matching provider in your Codex configuration. Session-end hooks omit the model, so the worker uses that session's recorded context. A queued job keeps its original model and transcript cutoff even if you switch models afterward.

There is no configured limit on concurrent sessions. Sessions can run inference in parallel, with separate model context and transcript offsets. Short locks protect shared skill writes; overlapping workers cannot silently overwrite a skill changed since their proposals were prepared. Actual throughput remains subject to your machine and model provider.

To save a lesson immediately, invoke `$codex-learn` in Codex. The helper works from the current conversation and submits a proposal through the same validator.

To learn from a particular Codex JSONL session file:

```sh
codex-autoharness --project /path/to/project learn --transcript /path/to/session.jsonl
codex-autoharness --project /path/to/project index
codex-autoharness --project /path/to/project history
```

The transcript command runs synchronously and inherits the last model context recorded in that file. It uses the file you name; it does not search your session history. A standalone `curate` command, which has no triggering session, uses your saved Codex defaults. Defaults also apply when an older transcript contains no model metadata. A run with no worthwhile lesson can correctly produce no skill.

| Command | What it does |
|---|---|
| `status` | Shows installation paths, managed skills, archives, and pending proposal queues. |
| `doctor` | Checks the interpreter and Codex executable and explains the separate hook trust step. |
| `index` | Prints the grouped index of managed project and global skills. |
| `learn --transcript FILE` | Distills the explicitly supplied Codex transcript. |
| `curate` | Runs library consolidation now, with a snapshot before changes. |
| `history` | Shows recent run results, including accepted and rejected proposals. |
| `history NAME --level project` | Shows a managed skill's provenance ledger. |
| `archive NAME --level project` | Moves a managed skill out of active recall. |
| `restore NAME --level project` | Restores an archived managed skill, unless its live name is occupied. |
| `spec` | Prints the authoring and validation contract. |
| `stage --file FILE` | Validates and applies one JSON proposal immediately. |
| `stage --file FILE --queue-only` | Queues the proposal for the next Stop hook. |
| `record-use NAME --level project` | Records an explicit invocation of a managed skill. |
| `mcp` | Serves the optional `stage_skill` MCP tool over standard input/output. |
| `uninstall` | Removes this integration while preserving learned skills and history. |

Global options `--project` and `--home` go before the subcommand. Skill commands accept `--level global` where shown; project is the default. See `codex-autoharness --help` and each command's `--help` for exact arguments.

## What runs automatically

| Codex event | AutoHarness action |
|---|---|
| `SessionStart` | Applies lifecycle decisions, injects a grouped skill index, and reports the previous run's outcome. |
| `UserPromptSubmit` | Counts a request as an opportunity for skills to be used. |
| `PreToolUse` | Counts main-session tool calls toward reflection and curation. |
| `PostToolUse` | Records successful reads of managed `SKILL.md` files as loads and reads of support files as views. |
| `Stop` | Drains interactive proposals and starts any reflection or curation that is due. |
| `SessionEnd` | Flushes pending learning work. |

A successful `SKILL.md` read is a **load proxy**. It does not prove that Codex followed the skill or that the skill improved the result. Tools that hide their file reads may not produce a usable signal. The [parity notes](docs/PARITY.md#usage-measurement) explain the consequence for lifecycle decisions.

The reflector runs as an isolated, read-only `codex exec` process. It receives bounded, redacted transcript material and skill context, then returns structured JSON proposals. A temporary Codex home copies the selected model/provider settings and file-based authentication; host hooks, plugins, shell tools, and MCP servers are excluded. It does not write the skill library. The promoter validates each proposal and is the only code that applies it. Child processes carry a recursion guard so reflection does not trigger more reflection.

Credentials stored only in an OS keyring are not migrated into the isolated proposer. OAuth refreshes update its temporary credential copy and are not saved back to your real Codex home. An authentication failure appears in run history and leaves the saved transcript position unchanged.

## Files and ownership

| Data | Project layer | Global layer |
|---|---|---|
| Active skills | `<project>/.agents/skills/` | `~/.agents/skills/` |
| Archived skills | `<project>/.agents/skills/.archive/` | `~/.agents/skills/.archive/` |
| Counters, queues, run history | `<project>/.agents/codex-autoharness/` | `~/.agents/codex-autoharness/` |

Each managed skill carries ownership metadata and an append-only provenance ledger. The library index and lifecycle pass only manage those skills. Your other skills can supply context for duplicate detection but are not eligible for automatic modification. A create proposal cannot replace an existing directory.

By default the project layer follows the session's working directory. Linked Git worktrees resolve to the main worktree so their learned skills survive worktree removal. Start Codex at the project root, or pass an explicit `--project`, when you want all work in one project library.

The state directory includes request and tool-call counters, transcript offsets, `intents/`, `runs/`, `last_run.json`, and curator `snapshots/`. Snapshots include managed skills only; both layers' snapshots are stored under the project's state directory. Curation stops if a required snapshot cannot be created. Review history before changing a lesson. `archive` and `restore` are the ordinary recovery commands; snapshots are a separate recovery source for a curator run and require selective manual recovery. A snapshot is not a reason to overwrite unrelated current skills.

## Configuration

Set these variables in the environment that starts Codex. Hooks read them when their process starts.

| Variable | Default | Effect |
|---|---|---|
| `CODEX_AUTOHARNESS_ENABLED` | `1` | Set to `0` to disable automatic hook processing without uninstalling. |
| `CODEX_AUTOHARNESS_REFLECT_EVERY_N` | `50` | Main-session tool calls between automatic reflections. |
| `CODEX_AUTOHARNESS_CONSOLIDATE_EVERY_N` | `250` | Main-session tool calls between curator runs; `0` disables automatic curation. |
| `CODEX_AUTOHARNESS_DIGEST_EXCHANGES` | `20` | Older exchanges retained as a short prior-context digest. |
| `CODEX_AUTOHARNESS_INDEX_SUSPENDED` | `0` | Set to `1` to suppress index injection while keeping accounting active. |
| `CODEX_AUTOHARNESS_INDEX_DESC_MAX_CHARS` | `60` | Description budget for each index entry and new skill admission. |
| `CODEX_AUTOHARNESS_SKILL_DESC_MAX_CHARS` | `1024` | Additional hard ceiling for skill descriptions. |
| `CODEX_AUTOHARNESS_SKILL_BODY_MAX_LINES` | `25` | Maximum nonblank body lines for a new or replaced `SKILL.md`. |
| `CODEX_AUTOHARNESS_MATURITY_PROJECT` | `100` | Project requests before a skill reaches graduation review. |
| `CODEX_AUTOHARNESS_MATURITY_GLOBAL` | `300` | Global requests before graduation review. |
| `CODEX_AUTOHARNESS_CAPACITY_PROJECT` | `50` | Mature, used skills admitted before project capacity contention. |
| `CODEX_AUTOHARNESS_CAPACITY_GLOBAL` | `20` | Corresponding global capacity. |
| `CODEX_AUTOHARNESS_GRADUATION_SUSPENDED` | `0` | Set to `1` to suspend zero-use graduation review; capacity contention remains active. |
| `CODEX_AUTOHARNESS_SNAPSHOT_KEEP` | `5` | Curator snapshots retained per layer. |
| `CODEX_AUTOHARNESS_CODEX_BIN` | `codex` | Codex executable used by the background proposer. |
| `CODEX_AUTOHARNESS_TIMEOUT_S` | `180` | Time limit for a proposer process. |
| `CODEX_AUTOHARNESS_NOTIFY` | unset | Set to `desktop` for optional local run notifications. |
| `CODEX_AUTOHARNESS_NOTIFY_CMD` | unset | Optional command receiving run JSON on stdin; parsed as an argument vector, never a shell expression. |
| `CODEX_AUTOHARNESS_NOTIFY_TIMEOUT_S` | `5` | Maximum notification time per channel. |

Advanced deployments can set `CODEX_AUTOHARNESS_PROJECT_ROOT` and `CODEX_AUTOHARNESS_GLOBAL_ROOT` to alternate **`.agents` roots**. These environment values differ from `--project`, which takes a project directory, and `--home`, which takes a home directory. Prefer the CLI options for manual commands.

Skills are protected during probation. At maturity, a skill with neither loads nor views can be archived. Skills with loads compete by loads divided by requests since creation; low-rate entries are archived only when the mature pool exceeds capacity. Probationary skills do not count against these caps, so capacity is not a hard bound on the entire library or its index. These defaults need calibration against real use.

## Trust and limits

Learning sends selected session content to the Codex model provider configured for the proposer and consumes model usage. Pattern-based redaction removes recognized secrets and personal information before handoff, but cannot identify every sensitive fact. Captured records and windows have size limits, so a long session may lose detail. The managed library bundle is capped at 250 KB, the total handoff at 600 KB, and each model response at 12 proposals. Large libraries can have skills omitted from a pass.

A failed or malformed proposer response records an error without queuing changes or advancing the saved transcript position. Inspect `history` for the outcome; an empty successful run means the model found nothing worth keeping.

The validator checks ownership, paths, structure, references, provenance fields, and selected unsafe content. Automatic proposer output must quote evidence found verbatim in the supplied redacted episode or, for curation, the supplied managed library. This verifies the quote's source, not the lesson's truth. The checks do not detect every prompt injection or establish that a script is safe to execute. Learned instructions and support scripts should be reviewed with the same care as any other agent-authored code.

Skills may be created, revised, merged, and archived automatically once trusted hooks run. Filesystem locks and atomic writes reduce races between simultaneous sessions, but an entire multi-proposal run is not a single transaction. Read [PARITY.md](docs/PARITY.md) for unsupported upstream options and measurement limits.

## Uninstall

For the plugin installation:

```sh
codex plugin remove codex-autoharness@codex-autoharness-local
codex plugin marketplace remove codex-autoharness-local
```

For a direct hooks installation:

```sh
codex-autoharness uninstall
```

For a project installation, repeat the same scope: `codex-autoharness --project /path/to/project uninstall`.

Direct uninstall removes matching hook commands, the unchanged installer-owned launcher, and the unchanged `$codex-learn` helper. Locally modified helper files are retained. Both paths preserve learned skills, archives, and learning history. Removing the Python package is a separate step.

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
```

Tests cover the deterministic learning pipeline and Codex integration. Passing fixture-based tests does not prove hook execution in every Codex surface or the usefulness of model-generated skills. See [VERIFICATION.md](docs/VERIFICATION.md) for the checks actually performed for this build.

Licensed under [MIT](LICENSE).
