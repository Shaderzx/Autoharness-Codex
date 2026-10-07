# AutoHarness feature comparison

This comparison uses Tigerless Labs AutoHarness revision [`99179cda91a4e974825840e7e6a49f0c34fd3c01`](https://github.com/tigerless-labs/autoharness/tree/99179cda91a4e974825840e7e6a49f0c34fd3c01), retrieved on 2026-10-07. It describes implementation scope, not a claim that both hosts behave identically or that learned skills improve task scores.

Codex AutoHarness adapts the upstream storage and maintenance pipeline under its MIT license. The native Codex hook adapter, JSONL parser, proposer process, installation, and CLI are specific to this project. [NOTICE.md](../NOTICE.md) records attribution.

## Feature matrix

| Capability | Tigerless Labs AutoHarness | Codex AutoHarness | Practical difference |
|---|---|---|---|
| Host integration | Claude Code plugin and hooks | Native Codex plugin and lifecycle hooks, with a Python CLI | Local plugin marketplace installation uses its own hooks source. Direct hook installation is also available. Both require `/hooks` trust. |
| Native skill files | `.claude/skills/<name>/SKILL.md` | `.agents/skills/<name>/SKILL.md` | Missing global/project Claude skills are copied automatically at `SessionStart` and for the installed scope during direct installation; `import-skills` also runs on demand. Originals and existing Codex destinations are preserved. |
| Automatic learning | Reflects after a tool-call threshold | Reflects after a tool-call threshold | Both default to 50 main-session tool calls and start learning after the turn ends. |
| Tail flush | Session-end capture | `SessionEnd` flush | Depends on the host actually emitting the event; an abruptly killed host may not flush its tail. |
| Learn on demand | `/learn` plugin skill | `$codex-learn`; `learn --transcript FILE` | The helper distills the current conversation; the CLI reads the exact transcript supplied by the caller. |
| Transcript handoff | Redacted bounded window and prior-context digest | Codex JSONL window and digest with redaction and limits | Different transcript formats require a separate parser. Truncation can remove useful evidence. |
| Compare before creating | Reflector sees existing skills | Reflector receives the skill context | Whether two lessons overlap remains model judgment. The validator prevents overwrites but does not establish semantic uniqueness. |
| Skill changes | Create, update, patch, remove support file, retire | The same proposal actions | Ordered proposals pass through deterministic staging and promotion. A retirement moves a skill to the archive. |
| Merge provenance | Records the surviving umbrella skill | Requires a valid managed umbrella for a merge retirement | An invented merge target is rejected. A recorded merge still needs content review to establish that nothing useful was lost. |
| Support files | `references/`, `templates/`, `scripts/`, `assets/` | Same allowed directories and bounded text files | References must resolve, paths must remain within the skill, and Python support files must parse. |
| Ownership | Manages its self-authored skills | Manages skills carrying its own ownership metadata | Handwritten and separately installed skills are excluded from automatic changes. Existing directory collisions are rejected. |
| Session recall | Category-grouped project/global index | Category-grouped project/global index at `SessionStart` | Added to native discovery. Hook trust and context delivery determine whether the host receives it. |
| Run visibility | Prior run summary and run accounts | Prior run summary, `history`, and `status` | Rejections remain visible rather than being reported as learned skills. |
| Use/view/patch accounting | Explicit Skill invocation, file view, and update counters | Successful `SKILL.md` read proxy, support-file view, and update counters | The most important host difference; see [usage measurement](#usage-measurement). |
| Opportunity-based aging | Requests since creation | `UserPromptSubmit` counts requests | A closed laptop does not age out skills. Counts depend on hook delivery. |
| Probation and graduation | Project/global maturity gates | Same lifecycle calculation | Defaults are 100/300 requests. No-load, no-view skills can be archived after probation. |
| Capacity contention | Rank mature skills by usage rate | Same rate-based policy | Defaults are 50/20 used mature skills, not caps on all live skills. |
| Archive and restore | Directory moves preserve metadata | Explicit `archive` and `restore` CLI commands | A restore refuses an occupied live name. Archived skills stay out of the injected index. |
| Whole-library curator | Rarer consolidation with snapshots | Read-only Codex curator with snapshots | Codex's cadence is measured in tool calls. Upstream documents tool calls but its captured revision increments this curator cadence by turns. |
| Snapshot retention | Five pre-curation snapshots per layer | Configurable snapshot retention, default five; selective recovery with `restore NAME --snapshot FILE` | Recovery restores one skill to an unoccupied live name; archive an existing managed version first. Ordinary `restore` revives an archived skill. |
| Evidence ledger | Append-only per-skill change records | Ledger, redacted evidence references, and run accounts; automatic proposals require verbatim source evidence | Quote matching establishes that the evidence appeared in the supplied source, not that the model's conclusion was correct. Manual staging requires evidence fields but cannot compare them with an independently supplied transcript. |
| Notifications | Optional desktop and external command | Optional desktop and external command | Disabled by default. Configuring an external notifier can disclose run metadata to its destination. |
| Concurrency | Atomic writes and run queues | Atomic writes, process locks, and duplicate-delivery protection | Reduces races between sessions. A multi-intent run can still be partially applied if later intents are rejected. |
| Model authority | Claude proposer stages intents; promoter writes | Isolated read-only `codex exec` returns JSON; promoter writes | The Codex proposer has no configured MCP servers and no skill-library write capability. |
| Session model | Host session supplies model context | Each background learner and curator inherits its triggering session's model, provider identity, and reasoning effort | Job arguments freeze the selection; concurrent sessions and later model switches cannot replace it. No configured concurrent-session limit. |
| Direct staging | Plugin-scoped `stage_skill` MCP | `stage` CLI and optional stdio `mcp` server | The ordinary Codex proposer uses structured output rather than MCP tool calls. |
| Fork carrier | Experimental resume/fork option; bundle default | Bundle only | No resume/fork or warm-prefix-cache optimization. This does not omit the upstream default learning path. |
| Distribution and updates | Claude plugin marketplace | Local Codex plugin marketplace and Python package | No hosted marketplace listing, automatic updater, or remote publication is implied by this build. |
| Performance evidence | Upstream cites broader harness research | No comparative benchmark claimed | Upstream research percentages do not measure this port. |

## Usage measurement

Claude Code exposes an explicit Skill invocation. Codex can consume skills by reading `SKILL.md`, so this implementation counts recognized, successful reads of managed skill files as loads. A support-file read is recorded separately as a view. `record-use` exists for an explicit managed-skill invocation when an integration can identify it directly.

These signals have limits. Reading a file does not establish adherence; following a skill already in context might produce no new read. A tool wrapper can hide a path or read the file while doing unrelated inspection. Tracking therefore supports a useful retention policy, not a claim that a skill worked.

The denominator is the number of requests received after creation. Graduation review can archive a mature skill only when it has neither loads nor views. If a host integration fails to report reads, that can make a useful skill appear unused. Set `CODEX_AUTOHARNESS_GRADUATION_SUSPENDED=1` while diagnosing such a recall or accounting issue; capacity contention remains active.

## Codex adaptation choices

The default learning path uses a fresh Codex process with a bounded input bundle. Model output must match a proposal schema before it reaches the deterministic staging layer. The child does not inherit the user's project tools or MCP servers, and it cannot directly edit the library. This makes the separation between proposing and applying changes explicit in Codex.

The validator rejects unsafe paths, unmanaged targets, invalid structures, missing support files, unsupported proposal shapes, and selected unsafe text. The deterministic checks preserve an admission boundary; they cannot validate every natural-language instruction or prove a lesson useful. Transcript text remains untrusted evidence even when it resembles instructions to the reflector.

Plugin installation keeps hooks in a separate plugin source. The alternative direct installer registers commands alongside existing integrations and records the exact commands and file digests it owns. Direct uninstall removes those entries and unchanged helper files while retaining learned skills, archives, and evidence. Neither path resets another tool's hooks or automatically changes trust decisions. Editing a shared hooks source can invalidate its prior trust, which is why plugin installation is the preferred path.

Claude skill import copies regular files and support directories with executable permissions. It refuses symlinks, special files and unsafe skill names, skips occupied destinations, and excludes ownership/accounting metadata. Imported skills remain outside automatic lifecycle management. Newly imported names and file paths are offered through a bounded `SessionStart` context; restarting Codex refreshes native discovery if its catalog was loaded before the hook. Files are copied without converting Claude-specific instructions, and repeated imports never overwrite the Codex copy.

## Remaining limits

- Hook events and payloads can differ across Codex versions and surfaces. CLI-level compatibility is not evidence that Desktop or Cloud lifecycle events were observed.
- The isolated proposer bridges Codex's file/direct OS keyring credentials and saves same-account OAuth refreshes to the original store after checking the source login and authentication settings. macOS uses the default user keychain; Linux requires `libsecret` and a running Secret Service. The nondefault encrypted keyring backend (`features.secret_auth_storage = true`) and process-local `ephemeral` credentials are not transferable. Learner locks do not coordinate foreground Codex; a foreground write between the final source check and save remains possible.
- Long or unusually formatted transcripts can lose context through record/window limits or unsupported record types.
- Redaction is pattern-based. Custom secrets and sensitive facts can pass through to the configured model provider.
- Model-generated lessons can be wrong, overly broad, redundant, or susceptible to prompt injection despite format and content checks.
- Automatic archive decisions use load proxies and uncalibrated defaults. The library's quality must be judged through actual work.
- The project includes no Claude fork carrier and no benchmark proving equivalent learning quality.

The implementation and tests should be read together with [the build verification record](VERIFICATION.md). A supported code path and a passing simulated event are distinct from a hook and real model observed in a live Codex session.
