# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Public-release hardening, onboarding, and automation are being prepared.

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

[Unreleased]: https://github.com/DKotsyuba/agent-pipline-compressor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/DKotsyuba/agent-pipline-compressor/releases/tag/v0.1.0
