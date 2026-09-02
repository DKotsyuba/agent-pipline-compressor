# Agent Pipline Compressor

[![CI](https://github.com/DKotsyuba/agent-pipline-compressor/actions/workflows/ci.yml/badge.svg)](https://github.com/DKotsyuba/agent-pipline-compressor/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/DKotsyuba/agent-pipline-compressor?display_name=tag)](https://github.com/DKotsyuba/agent-pipline-compressor/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Agent Pipline Compressor** is a local, deterministic tool-output compressor for [Codex](https://github.com/openai/codex) and Claude Code. It measures noisy Bash output in `audit` mode and can selectively compress eligible output while keeping a recoverable original copy. Runtime code is Python standard library only: it makes no model, provider, telemetry, or network calls.

This is an early `0.1.0` release. Start in `audit`, then opt into `safe` only after checking the hook trust/approval experience on your host. `full` remains cautionary until its live approval semantics have been tested.

## Five-minute start

### Prerequisites

- Codex or Claude Code with its plugin support enabled.
- Python 3.8+ at `/usr/bin/python3` for hooks, plus `python3` on `PATH` for the
  source administration commands below (tested CI versions are listed below).
- A newly started task/session after installing, updating, or changing hooks.

Install for one host only, or both if you use both hosts.

### Codex

```bash
codex plugin marketplace add DKotsyuba/agent-pipline-compressor
codex plugin add agent-pipline-compressor@agent-pipline-compressor
```

### Claude Code

```bash
claude plugin marketplace add DKotsyuba/agent-pipline-compressor --scope user
claude plugin install agent-pipline-compressor@tokenpipe-local --scope user
```

Restart or start a new task/session. The plugin installs above do not create a
working source checkout: clone the repository for the administration commands
below (or use a verified release checkout). Both host installs share the
persisted `TOKENPIPE_HOME` configuration unless it is overridden.

```bash
git clone https://github.com/DKotsyuba/agent-pipline-compressor.git
cd agent-pipline-compressor
```

Then choose the conservative mode, run a single eligible read-only command, and
inspect local results:

```bash
python3 scripts/tokenpipe.py mode safe
git status
python3 scripts/tokenpipe.py stats --since 24h
```

The `mode` and `stats` commands are run from a source checkout. To inspect an original output after a compressed result prints `raw_ref=...`, run:

```bash
python3 scripts/tokenpipe.py show <raw_ref>
```

## Install, update, and uninstall

### Codex

```bash
# Update the marketplace, then reinstall/add the plugin if the CLI asks for it.
codex plugin marketplace upgrade agent-pipline-compressor
codex plugin add agent-pipline-compressor@agent-pipline-compressor

# Uninstall the plugin. Remove the marketplace only after this step if desired.
codex plugin remove agent-pipline-compressor@agent-pipline-compressor
```

### Claude Code

```bash
# Refresh the marketplace, then reinstall the plugin.
claude plugin marketplace update tokenpipe-local
claude plugin install agent-pipline-compressor@tokenpipe-local --scope user

# Remove the installed plugin.
claude plugin uninstall agent-pipline-compressor@tokenpipe-local --scope user
```

If a CLI version presents a different update prompt, follow that prompt and start a new session afterwards. Installation changes host state, so it is not part of this repository's automated test suite.

### Source checkout / development fallback

```bash
git clone https://github.com/DKotsyuba/agent-pipline-compressor.git
cd agent-pipline-compressor
python3 -m unittest discover -s tests -v
python3 scripts/tokenpipe.py --version
```

For an isolated Claude print-mode run from this checkout:

```bash
claude --bare --plugin-dir . -p "..."
```

## Support

| Host | Supported path | Notes |
| --- | --- | --- |
| Codex | Marketplace plugin | Native compression is applied by the pre-execution wrapper for eligible commands. Other Bash output is audited. |
| Claude Code | GitHub marketplace plugin | The hook can replace eligible text `stdout`/`stderr`; changed streams include recovery references. |
| Python | 3.8 minimum | Current source and tests are compatible with Python 3.8; see the CI workflow for the release-tested matrix. |
| Platform | macOS/Linux convention | Hook execution requires `/usr/bin/python3`; nonstandard layouts need explicit packaging support. |

## Modes and configuration

| Mode | Default | Behavior |
| --- | --- | --- |
| `audit` | yes | Records counterfactual estimates; tool output is unchanged. |
| `safe` | no | Wraps only strict read-only command categories. |
| `full` | no | Adds selected test/build/lint commands; use only after approval behavior is validated locally. |

Persist a mode with `python3 scripts/tokenpipe.py mode audit|safe|full`. `TOKENPIPE_MODE` can override the mode for hook processing. The Codex post-replacement gate can likewise be persisted with `python3 scripts/tokenpipe.py post-replace 1|<category list>|off`, so every runtime sharing the same `TOKENPIPE_HOME` picks it up without per-process environment wiring; `TOKENPIPE_POST_REPLACE` overrides it. The following environment variables are optional and intentionally local:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TOKENPIPE_HOME` | `~/.codex/tokenpipe` | Private settings, raw-output and metrics root. |
| `TOKENPIPE_RUNTIME_HOME` | system temp `codex-tokenpipe-<uid>` | Private runtime output used by the native wrapper. |
| `TOKENPIPE_MIN_TOKENS_ESTIMATE` | `1500` | Minimum estimated input size before compression is considered. |
| `TOKENPIPE_MAX_SHOWN_CHARS` | `7000` | Global ceiling for shown compressed text; every per-category budget is clamped to it. |
| `TOKENPIPE_BUDGET_<CATEGORY>` | `error`/`code`/`diff` `7000`, `log`/`json` `6000`, `plain`/`search`/`config` `5000` | Shown-character budget for one content category, e.g. `TOKENPIPE_BUDGET_ERROR=4000`. Clamped to `[256, TOKENPIPE_MAX_SHOWN_CHARS]`; an unparsable value keeps the default. |
| `TOKENPIPE_CAPTURE_MAX_BYTES` | `64 MiB` | Native child-output capture limit. |
| `TOKENPIPE_RAW_TTL_SECONDS` | `7 days` | Raw-output retention target. |
| `TOKENPIPE_RAW_MAX_BYTES` | `256 MiB` | Raw-output storage cap. |
| `TOKENPIPE_METRICS_MAX_BYTES` | `8 MiB` | Metrics-file rotation cap. |
| `TOKENPIPE_CLEANUP_INTERVAL_SECONDS` | `600` | Minimum seconds between raw-spool retention sweeps; `0` sweeps on every replacement. |
| `TOKENPIPE_HOOK_TIMEOUT_SEC` | `30` | Hook post-processing timeout. |
| `TOKENPIPE_POST_REPLACE` | `0` | Codex post-output replacement gate: `0`/absent audits only; `1` replaces any eligible content category; a comma-separated category list (e.g. `error,log`) replaces only output the compressor classifies into a listed category, and any other value audits only. The environment variable overrides the persisted `post-replace` setting. |
| `TOKENPIPE_REPEAT_REPLACE` | `0` | Exact-repeat suppression gate: `0`/absent measures repeats only; `1` lets output byte-identical to the previous output of the same identity be shown as a short `raw_ref` notice, and only in `safe`/`full` mode when the earlier raw copy still reads back byte-for-byte. Persist it with `python3 scripts/tokenpipe.py repeat-replace 1\|off`; the environment variable overrides the persisted setting. |
| `TOKENPIPE_AUDIT_MAX_BYTES` | `1 MiB` | Largest single Codex audit output copied to the compressor; larger output is recorded as metadata only. |
| `TOKENPIPE_CLAUDE_MAX_BYTES` | `16 MiB` | Largest Claude text stream considered by its post-tool hook. |
| `RTK_DB_PATH` | private runtime DB | Optional RTK history location; an explicit value takes precedence. |

### Optional RTK

RTK is disabled unless explicitly configured with a trusted absolute executable:

```bash
python3 scripts/tokenpipe.py rtk /absolute/path/to/rtk
python3 scripts/tokenpipe.py rtk off
```

When active, RTK owns filtering. Tokenpipe does not stack its own Lite/CCA transforms, cannot measure RTK's raw-to-filtered savings, and cannot provide a Tokenpipe `raw_ref` for that stage. Keep it off when recoverability and Tokenpipe-owned measurements matter.

## Recovery, statistics, and privacy

Search-shaped output is compressed structurally: the `search-group` strategy prints each matching file once with its match count and the first and last matches verbatim, and the `search-fold` strategy prints each directory once with its entry count and the first and last entries. Both mark every omission, keep the original first and last lines, leave short or sparse results untouched, and rely on `show <raw_ref>` for the complete result.

Replacement must also pay for itself. A replacement ships with the recovery header the host renders above it, so the estimated saving is compared against the compressed output *plus* that header; when the saving does not exceed it, the exact original output is returned with `skip_reason=net-loss` and nothing is spooled. The metric row still records the compressed candidate as the counterfactual, so `stats` keeps reporting the potential the header cost cancelled out. The native wrapper applies the same rule to the recovery field its own header would gain.

When bounding elides a middle section, the recovery header states how many characters were omitted and the exact command that prints them back, so no separate lookup is needed:

```bash
python3 scripts/tokenpipe.py show <raw_ref> --range <start>:<end>
```

`--range` takes character offsets into the decoded original as a half-open `START:END` span, prints exactly those characters, and rejects a malformed range with exit status `2`. Without `--range`, `show` prints the whole original.

Replacement is allowed only after raw output is securely spooled; a spool error leaves the original output unchanged. Raw files are private runtime state (`0700` directories and `0600` files), may contain secrets from commands, and are subject to retention and size caps. Treat any `raw_ref` as sensitive.

Output that looks like a credential is refused before anything is stored. Immediately before spooling, both the hook path and the native wrapper scan at most the first 256 KiB of the output for PEM private key blocks, AWS access keys, GitHub tokens, OpenAI/Anthropic-style keys, Slack tokens, JWTs, `Authorization: Bearer` headers, and a key word such as `password`, `secret`, `api_key`, or `token` directly assigned a value (prose that merely mentions those words does not match). On a match the output is returned byte-identical: this is a refusal, not a redaction, so nothing is rewritten, no raw file is written, no repeat-index entry is recorded, and the metric row carries only the skip reason `secret-guard` — never any matched text. The scan bound is deliberate: a credential appearing only after the first 256 KiB of a very large output is not detected, and output already spooled by earlier runs is unaffected.

`stats` reads private metrics and reports estimates by mode, command category, strategy, and plugin version. Metrics omit prompts, command arguments, and tool output. They are not provider billing/usage measurements. The `Repeat outputs` line counts calls whose output was byte-identical to the previous output of the same identity and the estimated tokens a repeat notice would have avoided, whether or not `TOKENPIPE_REPEAT_REPLACE` is on.

Token counts come from the character-class estimator named in the `Estimator: class-ratio v1` line of the summary. It splits text into runs of prose, code punctuation, whitespace, digits, hex/base64 identifiers, non-Latin alphabets, CJK, and symbols, and charges each run its own characters-per-token rate, so hashes and ids are no longer under-counted and English prose is no longer over-counted. The caveat: those rates are heuristics calibrated against published OpenAI `o200k`-style measurements, not against the Claude tokenizer or any provider's billing, and a single estimate can still be off by tens of percent on unusual output. Rows written before this release were measured with the previous UTF-8-bytes/3.5 formula and are not rewritten, so a `--since` window that spans the upgrade mixes both. Thresholds such as `TOKENPIPE_MIN_TOKENS_ESTIMATE` keep their numeric defaults, so the size of output they admit shifts with the estimator.

```bash
python3 scripts/tokenpipe.py stats
python3 scripts/tokenpipe.py stats --since 24h
python3 scripts/tokenpipe.py stats --json
```

## How it works

```text
Host hook
  -> validate mode and eligible argv
  -> run original command without a shell
  -> capture output privately
  -> deterministic classification / conservative compression
  -> spool original successfully
  -> emit compressed output + raw_ref (otherwise emit original output)
```

Protected categories are bounded, not exempt: `code`, `diff`, and `config` output above `TOKENPIPE_MIN_TOKENS_ESTIMATE` is replaced by its verbatim head and tail around an omission marker (strategy `bounded-code`, `bounded-diff`, `bounded-config`) with the exact original spooled and reachable through `raw_ref`. Output at or below that threshold, and binary output at any size, stays byte-exact and is never spooled.

Compression is strategy-per-category and deterministic: JSON output folds arrays of six or more objects that all share exactly the same key set down to their first two items, a `__tokenpipe_similar_items__` marker with the exact omitted count and sorted key list, and the last item, recursively at any depth, while non-homogeneous arrays keep the head/tail truncation markers.
Log-classified output collapses repeats: byte-identical neighbours become a `[previous line repeated N more times]` marker, and a line that matches an earlier one after volatile fields (timestamps, UUIDs, long hex ids, durations, byte sizes, percentages, and addresses) are masked for comparison only is dropped in favour of its first, verbatim occurrence, which gains a trailing `[seen N times]` marker — error and summary lines and the last line are always kept, and status codes, exit codes, and small integers are never masked.

Key files:

- [`scripts/tokenpipe.py`](scripts/tokenpipe.py) — compressor, storage, statistics, CLI, and native wrapper.
- [`hooks/pre_tool.py`](hooks/pre_tool.py) — Codex eligible-command rewrite.
- [`hooks/post_tool.py`](hooks/post_tool.py) and [`hooks/claude_post_tool.py`](hooks/claude_post_tool.py) — post-tool hooks.
- [`hooks/common.py`](hooks/common.py) — hook protocol and fail-open boundary.
- [`hooks/hooks.json`](hooks/hooks.json) — Claude Code hook declarations.
- [`benchmarks/compression_lab.py`](benchmarks/compression_lab.py) — deterministic compression lab.

## Security model and limitations

- Commands are passed as argv with `shell=False`; compound shell syntax and untrusted executable paths are rejected from wrapper execution.
- Unrecognized commands run through the host normally. Failure to compress, store raw output, or access runtime storage fails open.
- No model or network call is implemented in runtime code. The plugin does not change model providers, credentials, proxies, or Codex token limits.
- Compressed output may omit context. Use `show <raw_ref>` before making a decision that depends on omitted details.
- The OS can clear temporary directories earlier than the configured retention.
- Hook/plugin installation requires the host's own review/trust process.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No change in output | Confirm a new session was started, inspect `python3 scripts/tokenpipe.py mode`, and remember `audit` never replaces output. |
| A command was not wrapped | `safe` accepts only narrow read-only argv; avoid pipes, redirects, shell operators, and compound commands. |
| No `raw_ref` | Small/protected/unknown output can pass through; any raw-spool failure also deliberately leaves output untouched. |
| Cannot read a reference | Use the same local user/runtime state; temporary storage may have been cleaned. |
| Python/hook failure | Confirm `/usr/bin/python3` exists and `python3` is on `PATH`, reinstall/update the plugin, then start a new session. |
| Need details | Run `python3 scripts/tokenpipe.py stats --json` and open a [bug report](https://github.com/DKotsyuba/agent-pipline-compressor/issues/new?template=bug_report.md). Do not attach raw sensitive output. |

## Development

No dependency installation is required for the project tests:

```bash
python3 -m unittest discover -s tests -v
python3 benchmarks/compression_lab.py --no-rtk
python3 scripts/tokenpipe.py --version
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution flow and [AGENTS.md](AGENTS.md) for repository engineering guidance.

## Contributing, security, and license

Contributions are welcome: read [CONTRIBUTING.md](CONTRIBUTING.md), report security vulnerabilities privately through [GitHub Security Advisories](https://github.com/DKotsyuba/agent-pipline-compressor/security/advisories/new), and review [SECURITY.md](SECURITY.md). Released under the [MIT License](LICENSE).
