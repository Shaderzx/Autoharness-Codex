# Build verification

## GitHub-hosted marketplace and native updates

Verified on 2026-10-07 with Codex CLI 0.160.1. An isolated configuration home
successfully installed the real public repository using
`codex plugin marketplace add Shaderzx/Autoharness-Codex --ref main` and
`codex plugin add codex-autoharness@codex-autoharness-local`. Codex recorded the
canonical HTTPS Git origin and installed version 0.1.1. Explicit native
`marketplace upgrade` also succeeded. User configuration and hook trust were
not changed by these smoke checks.

The retained offline integration test runs actual Codex against a temporary Git
repository, with an origin rewrite confined to its child environment. It verifies:

- Plugin startup automatically refreshes a moving Git ref, including content
  changes with the same plugin version.
- A changed hooks source has `modified` trust status; existing trust records
  remain unchanged until the user reviews the source.
- Explicit upgrade is a no-op at the current revision and activates a new
  plugin version when the remote manifest version changes.
- A full commit SHA remains pinned through automatic startup refresh and
  explicit upgrades.

Run `python3 -m pytest -q tests/test_hosted_marketplace.py`. The marketplace
fixture needs no network access, model provider, or user credentials. The test
skips if Codex is absent.
Automatic updates are provided by Codex's existing
[plugin startup task](https://github.com/openai/codex/blob/rust-v0.160.1/codex-rs/core-plugins/src/manager.rs#L2812).
There is no custom updater, release scheduler, or public plugin directory listing.

## Optional isolated learner sessions

Verified on 2026-10-07 with Codex CLI 0.160.1. `resume` and `fork` are opt-in;
the default remains a fresh bounded bundle. The executable local-provider test
in `tests/test_codex_request_catalog.py` runs three generations for each mode,
deletes every prior temporary home, and transfers only a sanitized learner
rollout without SQLite state. Every request retains prior learner messages,
uses the current model, provider and reasoning selection, and exposes no tools.

Native source checks informed the cache format: `codex exec fork` can create
reference-backed paginated history, and persisted metadata can restore tools or
capability roots. The cache therefore uses Codex's self-contained legacy history
format, retains only user/assistant text, drops stored instructions and settings,
and explicitly preserves disabled multi-agent capability. The relevant source is
[the 0.160.1 protocol](https://github.com/openai/codex/blob/rust-v0.160.1/codex-rs/protocol/src/protocol.rs)
and [thread processor](https://github.com/openai/codex/blob/rust-v0.160.1/codex-rs/app-server/src/request_processors/thread_processor.rs).

Regression checks cover private permissions, routing/session separation,
redaction, history-size and retention caps, malformed caches, no-output fallback,
and rejection of evidence available only in prior history. The full suite passed
**584 tests**; scoped Ruff and `git diff --check` passed. These checks use synthetic
messages and a loopback provider. They do not measure provider-side caching,
learning quality, or compatibility with other Codex versions.

After merging the snapshot-recovery, Claude-import and test-consolidation changes
from `main`, the retained suite passed **290 tests**, Ruff passed for `src`,
`tests` and `tools`, and `git diff --check` passed. Removed redundant tests were
kept removed; the learner-session tests extend the retained baseline.

## Test suite cleanup

Verified on 2026-10-07: **243 passed**, no failures or skips; Ruff passed.
The baseline was 574 tests in 42 modules. The retained suite has 24 modules and
3,543 Python lines, down from 6,359. Runtime code is unchanged.

Removed duplicate helper/hook/dispatcher checks, assertions about constants or
documentation wording, and a regex test that reimplemented production logic.
Retained the CLI and native Codex flows, proposer isolation, concurrent workers,
interrupted-write recovery, ownership and path checks, redaction, and snapshot
recovery including directory permissions. Remaining content, staging, configuration,
and metrics checks share fewer setups.

For comparison, statement coverage measured in the pytest process changed from
90.2% to 88.6%, and branch coverage from 83.9% to 80.4%. These measurements exclude
subprocess execution; they are not proof of equivalent assertion coverage.
Coverage was run through an ephemeral `uvx` environment, with no new project
dependencies or test harness.

## 0.1.1 — session model inheritance

Verified on 2026-10-07 with Codex CLI 0.160.1. Background learners and curators
inherit the triggering session's model, provider identity, and reasoning effort.
Each job freezes these settings and its transcript cutoff. SessionEnd, which
omits the native model field, uses its own session context. Standalone transcript
learning reads that transcript's recorded model context.

Focused regression and integration checks used the existing tests:

- A native Codex thread started on a model different from its saved default,
  then switched models and reasoning effort. Both curators and the final
  SessionEnd learner received the correct triggering model.
- An actual Codex request sent the session-selected model and high reasoning
  to a local provider, while still exposing zero tools.
- Three detached learners sharing a project and global library ran inference
  concurrently, kept distinct models and transcript offsets, and saved their
  respective skills. A rendezvous inside the existing fake executable would
  fail if inference were globally serialized.
- An older queued transcript window excluded later model turns and did not
  rewind an offset already consumed by another worker.

There is no configured limit on concurrent sessions. Short locks protect shared
files, and a per-session lock protects that session's transcript offset. Machine
resources and provider capacity still determine available throughput.

All existing regression checks passed (569), Ruff passed, and the 0.1.1 wheel
built. This update adds four focused cases and extends existing checks; no new
test framework or test harness was introduced.

## 0.1.0 — initial release

Verified on 2026-10-07 with Codex CLI 0.160.1 and Python 3.14.7 on macOS.
Release: **0.1.0**. Completed before the 11:31 Asia/Dubai deadline.

## Product checks

| Path | Observed result |
|---|---|
| Real Codex reflection | Learned a subprocess-output skill from synthetic session evidence, wrote its ownership metadata and ledger, and included it in grouped recall. |
| Real Codex improvement | Patched an existing managed skill to preserve stdout and stderr bytes; the change passed admission and produced a ledger entry. |
| Real Codex consolidation | Combined two related managed skills, retained both lessons, archived the sibling, and saved a pre-curation snapshot. |
| Native hook delivery | Actual Codex app-server emitted all six lifecycle events. Session-start context reached the model, prompt and skill-use counters changed, and SessionEnd launched the detached proposer after transcript archival. |
| Native trust | An isolated fixture used the same `hooks/list` and `config/batchWrite` trust flow as `/hooks`; no trust-bypass flag was used. |
| Proposer isolation | The request emitted by actual Codex contained `tools: []`. No inherited shell, MCP, browser, or other model tool was exposed. |
| Native skill discovery | Codex discovered a skill with a quoted YAML description correctly. Its scanner excludes hidden archive directories. |
| Concurrent updates | Changes to an affected skill during model inference reject the stale proposal instead of overwriting newer content. |
| Ownership and recovery | Handwritten skills remain protected. Unsafe paths and fabricated automatic evidence are rejected. Interrupted promotions recover, transient storage failures retain queued work, and archives restore their original skill identity. |
| Distribution | The wheel built and installed into an isolated directory; its CLI and bundled authoring spec ran successfully outside the checkout. |

Real model calls used synthetic inputs and temporary skill directories. They did
not modify the user's learned skills. The native hook test used a local provider
fixture; it verifies engine integration, while the separate real model checks
verify learning output. These checks do not measure long-term skill usefulness
or prove behavior on every Codex Desktop or Cloud version.

Evidence: [creation result](live-smoke.json) and
[update/consolidation result](live-update-curate.json).

## Local installation

Installed plugin:

```text
codex-autoharness@codex-autoharness-local
~/.codex/plugins/cache/codex-autoharness-local/codex-autoharness/0.1.0/
```

The `codex-autoharness` maintenance command is available in `~/.local/bin`.
Its `doctor` command reports the plugin installed and enabled with a supported
Python interpreter and Codex executable. All 40 packaged runtime/configuration
files were compared with the checkout: none were missing or different.

Only the new marketplace and plugin configuration entries were added. Existing
`~/.codex/hooks.json` contents and other configuration settings were unchanged.
A read-only native `hooks/list` check found all six plugin hooks **untrusted**.
Automatic operation therefore awaits the user's review in `/hooks` after
restarting Codex. Actual user hook trust was not changed.

## Regression and packaging checks

The retained upstream regression checks and Codex-specific feature/integration
checks passed: **565 passed**, no failures or skips. Ruff passed. Runtime source
also parsed under Python 3.11 syntax rules; execution on Python 3.11 was not
performed on this machine.

Commands used:

```sh
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q --disable-warnings
python3 -m ruff check src tests tools
python3 -m pip wheel --no-deps --wheel-dir dist .
codex-autoharness --version
codex-autoharness doctor
```

The wheel is `dist/codex_autoharness-0.1.0-py3-none-any.whl`.
Its SHA-256 is
`83f6ea90dcd27eb0347b9c1b11b02e48bf0e17e2dda394596695c35d91cdabc5`.

No remote repository, hosted marketplace, or public release was created.
