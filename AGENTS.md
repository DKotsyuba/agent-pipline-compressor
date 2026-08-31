# Repository guidance

`agent-pipline-compressor` is a Python-standard-library-only plugin for Codex and Claude Code. Treat the public spelling `Pipline` as identity; rename it only through an explicitly scoped migration.

## Scope and architecture

- `scripts/tokenpipe.py` owns deterministic transforms, raw-output storage, private metrics, CLI commands, and native argv execution.
- `hooks/` adapts host hook events. Keep it thin: no persistence of prompts, command arguments, or raw output there.
- `benchmarks/compression_lab.py` evaluates deterministic policies.
- `tests/` uses `unittest`; add focused regression coverage with behavior changes.
- Plugin manifests and hook declarations are part of the public contract. Keep their version and host paths aligned with the runtime.

## Constraints

- Keep runtime dependency-free and compatible with Python 3.8 and the CI Python matrix. Do not add a package manager or third-party import without an explicit project decision.
- Every named first-party declaration needs an attached Python docstring or language-native documentation comment: this includes internal/private/local functions, classes/types, and constants where the language supports attached documentation. Exempt generated and vendor code; apply the rule only to the declarations touched by the change unless the task explicitly expands scope.
- In dynamically typed Python, docs state parameter and return types as well as semantics. Where relevant, document side effects, errors, security invariants, and resource lifetime. Update docs with the implementation.
- Make minimal diffs and preserve unrelated working-tree changes.

## Safety and privacy invariants

- Runtime is local only: do not add model, telemetry, provider, or network calls.
- Compression must be deterministic and fail open.
- Never replace output until the exact raw output is recoverable; on any spool or validation failure return the original result.
- Preserve argv execution without a shell. Do not broaden wrapper acceptance of shell operators, redirects, or untrusted executable paths casually.
- Raw output, recovery references, private directories, and runtime state can be sensitive. Do not log, commit, paste into issues, or add to metrics any prompts, command arguments, credentials, or raw tool output.
- Treat `full` as cautionary until live host approval semantics are covered by a deliberate test; do not imply it is safer than `safe`.

## Validation

Run the narrowest relevant checks, then the full suite before a release-facing change:

```bash
python3 -m unittest discover -s tests -v
python3 benchmarks/compression_lab.py --no-rtk
python3 scripts/tokenpipe.py --version
git diff --check
```

For manifests, also parse JSON with `python3 -m json.tool <file>` and confirm all plugin versions equal `VERSION`. For hook changes, test both host event paths and retain the fail-open behavior.

## Versioning, releases, and documentation

- `VERSION` is the SemVer source of truth. Keep runtime/manifests/release metadata aligned in the same change.
- Tags are `vMAJOR.MINOR.PATCH`. Maintain `CHANGELOG.md` using Keep a Changelog; do not invent release entries.
- Update README configuration, support, privacy, and recovery guidance whenever user-visible behavior changes.
- Use Conventional Commit-style messages as a recommendation, not as a promise of automatic semantic version bumps.

## Acceptance checklist

- [ ] Focused and full tests pass.
- [ ] Raw recovery / fail-open behavior remains covered.
- [ ] No secret, raw output, or user-local path entered source, docs, or tests.
- [ ] Versions, changelog, manifests, and public docs agree.
- [ ] `git diff --check` is clean and unrelated edits are untouched.
