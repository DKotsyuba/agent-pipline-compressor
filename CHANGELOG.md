# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Homogeneous JSON array folding: object arrays with six or more items that
  share exactly the same key set collapse to their first two items, a
  `__tokenpipe_similar_items__` marker carrying the exact omitted count and
  the sorted key list, and the last item, applied recursively at any depth.
  Non-homogeneous arrays keep the previous `__tokenpipe_omitted_items__`
  behaviour.

- Cross-call exact-repeat suppression, measurement-first. Every metric row now
  carries `repeat_of_previous`, and `stats` prints a `Repeat outputs` line with
  the tokens a repeat notice would avoid. Identity is a digest of the
  normalized command category, tool name, session id, and the output digest;
  the private `repeat-index.json` beside the raw spool stores only digests,
  byte lengths, timestamps, and recovery paths, inherits the raw-output TTL,
  and never holds command lines or output text. The notice replaces shown
  output only when `TOKENPIPE_REPEAT_REPLACE=1` (or the persisted
  `repeat-replace` setting) is on, the mode is `safe`/`full`, and the earlier
  raw copy still reads back byte-for-byte; every other case, including an
  unreadable index, keeps the previous behavior.
- Log compression collapses non-adjacent near-repeats. Volatile fields
  (timestamps, UUIDs, hex ids of eight or more characters, durations, byte
  sizes, percentages, and memory addresses) are masked to build a comparison
  key only; later lines sharing a key are dropped and the first, verbatim
  occurrence gains a `[seen N times]` marker. Status codes, exit codes, and
  plain integers below eight digits are never masked, and error, summary, and
  final lines are never dropped.
- Compression lab fixtures for a varying-timestamp log and for two cases with
  non-empty interleaved `stderr`, so the log ratios and the stderr path are
  measured honestly.
- Structural compression for search-shaped output, detected as a new `search`
  content category ahead of the error heuristics: `search-group` folds dense
  `path:line:text` matches (ripgrep/grep, including `path:text` and
  `path-line-text` context lines) into one entry per file with its match count
  and the first and last matches verbatim, and `search-fold` folds bare path
  listings (`find`, `git ls-files`) into one entry per directory with its entry
  count and the first and last names. Both keep the original first and last
  lines, mark every omission, leave short or sparse results byte-for-byte
  unchanged, and still require a recoverable `raw_ref` before replacement.
- Compression lab: dense multi-file `rg`, nested `find`, and short-grep
  fixtures plus `search-group`/`search-fold` stages (lab version 2.1.0).
### Changed

- `code`, `diff`, and `config` output above `TOKENPIPE_MIN_TOKENS_ESTIMATE` is
  no longer unconditionally exact passthrough: it is bounded to its verbatim
  head and tail under the `bounded-code`, `bounded-diff`, and `bounded-config`
  strategies, with mandatory raw spooling, byte-for-byte recovery validation,
  and the usual `replace_categories` gate, on both the hook and the native
  paths. Output at or below the threshold, output already within
  `TOKENPIPE_MAX_SHOWN_CHARS`, and `binary` output of any size stay byte-exact.
  The compression lab's protected fixtures follow the same policy and gained an
  oversized unified-diff fixture.

### Fixed

- Concurrent metric appends no longer lose rows to a spurious `ENOENT`.
  On macOS/APFS, simultaneous `openat(dir_fd, "metrics.jsonl",
  O_CREAT | O_APPEND)` calls intermittently fail even though the private
  directory exists; the creating open is now retried up to three more
  times with millisecond backoff. Every other `OSError` still propagates,
  a genuinely missing directory still fails fast, and callers keep their
  fail-open behaviour.

- The compression lab mapped only the literal `json` route to the `json-lite`
  stage, so real `rtk-json` captures scored as 1.000 passthrough; any route
  suffixed `-json` now gets the JSON stage set, and the lab's array-count gate
  understands the new fold marker.
- A compression-lab candidate whose stage raised (such as `json-lite` on a
  non-JSON `rtk-json` capture) reported only `stage-error` and dropped the
  gates it had already failed; stage errors are now appended to the gate
  reasons, so an rtk candidate without raw recovery still reports
  `raw_recoverable`.

- `stats` no longer counts native (RTK) metric rows written by other
  `TOKENPIPE_HOME`s: rows carry a non-reversible `home` tag and the shared
  runtime metrics file is filtered by it. Legacy rows without the tag are kept.
- Hook latency on replacements: the raw-spool retention sweep is amortized to
  once per `TOKENPIPE_CLEANUP_INTERVAL_SECONDS` (default 600) instead of
  walking the whole spool on every replacement; `latency_ms` now uses a
  monotonic clock. Byte-for-byte raw recovery validation still runs every time.

## [0.2.1] - 2026-09-01

### Added

- Claude Code PostToolUse coverage beyond Bash: Grep, WebFetch, and MCP tool
  results are recorded as audit-only metrics (no replacement path exists for
  them); Read/Edit/Write/NotebookEdit are ignored.
- The Codex pre-hook now verifies that a command's executable resolves into
  the wrapper's trusted directories before rewriting; untrusted heads (for
  example toolchains installed under the home directory) pass through
  unwrapped instead of failing with exit 126, making `full` mode safe to
  enable on machines with home-directory toolchains.

### Changed

- Plugin manifests carry the maintainer's real name and canonical profile URL.

## [0.2.0] - 2026-08-31

### Added

- `TOKENPIPE_POST_REPLACE` accepts a comma-separated content-category
  allowlist (for example `error` or `error,log`) in addition to `0`/`1`:
  only output the compressor classifies into a listed category is replaced,
  and everything else keeps the original output with an honest
  `category-gated` metric. Malformed or mixed values fail closed to
  audit-only.
- `python3 scripts/tokenpipe.py post-replace 1|<category list>|off` persists
  the replacement gate in the shared `TOKENPIPE_HOME` config, so every
  runtime using the same home picks it up without per-process environment
  wiring; the `TOKENPIPE_POST_REPLACE` environment variable still overrides
  the persisted value.

### Fixed

- The Codex post hook no longer runs a second audit pass after a
  non-replacing compressor run; each event records exactly one metric, so
  statistics no longer double-count originals or overstate savings.

## [0.1.0] - 2026-08-31

### Added

- Deterministic local auditing and compression for noisy Codex Bash output.
- Claude Code `PostToolUse` integration using the same compression core.
- `audit`, `safe`, and `full` operating modes with strict command routing.
- Recoverable raw-output spooling, bounded retention, and private metrics.
- Conservative JSON, repetitive-log, and ranked error/test/build transforms.
- Optional RTK integration with explicit executable validation.
- A deterministic compression lab and cross-host regression suite.

### Security

- Wrapped commands execute as argv with `shell=False`; compound shell syntax is
  rejected.
- Output replacement fails open unless a private recovery copy is available.
- Runtime processing is local and makes no model, telemetry, or network calls.
- Git reads disable external diff/textconv, fsmonitor hooks, pagers, and
  environment-supplied Git configuration.
- Native executables are accepted only from explicit installed roots; project
  and temporary PATH shims are refused.
- Raw-output and metrics paths use no-follow directory traversal and reject
  symlinked or foreign-owned storage.
- Claude stdout/stderr recovery and metrics commit as one transaction; binary,
  control-heavy, and invalid-UTF-8 output is never compressed.

### Changed

- Claude hook commands use absolute `/usr/bin/python3` instead of a
  machine-specific Python Framework path or a project-controlled PATH lookup.

[Unreleased]: https://github.com/DKotsyuba/agent-pipline-compressor/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/DKotsyuba/agent-pipline-compressor/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/DKotsyuba/agent-pipline-compressor/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/DKotsyuba/agent-pipline-compressor/releases/tag/v0.1.0
