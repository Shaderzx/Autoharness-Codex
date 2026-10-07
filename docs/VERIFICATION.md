# Build verification

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
