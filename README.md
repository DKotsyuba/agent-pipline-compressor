# Codex Token Pipeline

Local, deterministic Bash-output auditing and compression for Codex Desktop and
CLI. The plugin does not change the model provider, proxy provider credentials,
call a model, register an MCP server, or set Codex `tool_output_token_limit`.

## Modes

- `audit` (default): PostToolUse records counterfactual savings; model-visible output is unchanged.
- `safe`: PreToolUse wraps only strict read-only commands (`git` reads, `rg`, safe `find`/`ls`, `docker ps/logs`) in the native tokenpipe executor.
- `full`: safe mode plus selected test/build/lint commands. Enable only after checking approval UX in your Codex environment.

PostToolUse is observation-only in every mode. It never copies tool output into
hook `additionalContext` or developer context. Active compression happens
inside the pre-execution wrapper, so compressed text remains ordinary Bash
tool-role output.

Change mode from the versioned source checkout:

```bash
python3 ~/plugins/codex-token-pipeline/scripts/tokenpipe.py mode audit
python3 ~/plugins/codex-token-pipeline/scripts/tokenpipe.py mode safe
python3 ~/plugins/codex-token-pipeline/scripts/tokenpipe.py mode full
```

Start a new Codex task after changing plugin installation or hook definitions.
Mode changes are read per hook call and do not require reinstalling the plugin.

Optional RTK integration is configured separately with a trusted absolute binary:

```bash
python3 ~/plugins/codex-token-pipeline/scripts/tokenpipe.py rtk /absolute/path/to/rtk
python3 ~/plugins/codex-token-pipeline/scripts/tokenpipe.py rtk off
```

RTK owns output filtering when selected; Lite/CCA are not stacked after it.
Tokenpipe therefore cannot measure RTK's raw-to-filtered savings or provide a
tokenpipe `raw_ref` for them. Keep RTK disabled when you want measurable local
compression and recovery; enable it only when `rtk gain` independently shows a
benefit on your workload.

## Statistics

```bash
python3 ~/plugins/codex-token-pipeline/scripts/tokenpipe.py stats
python3 ~/plugins/codex-token-pipeline/scripts/tokenpipe.py stats --since 24h
python3 ~/plugins/codex-token-pipeline/scripts/tokenpipe.py stats --json
```

Stats separate audit-only observations from native wrapped calls, report native
call/token coverage, and label RTK-owned calls. "Tokenpipe-owned" savings do not
include savings that an external RTK process may have applied before tokenpipe
sees the output.

Metrics normally live in `~/.codex/tokenpipe/metrics.jsonl`; when the Bash
sandbox cannot write there, native-wrapper metrics fall back to the private
runtime directory under `$TMPDIR`. The `stats` command reads both locations.
Events include mode, coarse command
category, strategy, content category, RTK adoption, estimated before/after
tokens, header bytes, latency, skip reason, and errors. Metrics never contain
prompts, command arguments, or tool output. Counts are estimates, not provider
usage. The metrics file rotates at 8 MiB by default.

Audit copies at most 1 MiB of one tool result into the local compressor child.
Larger results produce a metadata-only `audit-output-overflow` event.

Compressed native-wrapper calls store recoverable original output in a private
runtime directory under `$TMPDIR/codex-tokenpipe-<uid>/raw/`. This keeps capture
inside the normal workspace-write sandbox instead of requesting access to the
Codex home. The default retention is seven days with a 256 MiB total cap, though
the operating system may sweep temporary files sooner. Read a returned reference with:

```bash
python3 ~/plugins/codex-token-pipeline/scripts/tokenpipe.py show <raw_ref>
```

## Design

- The wrapper revalidates mode, category, command, flags, and executable before starting a child.
- Commands are executed as argv with `shell=False`; shell expansion and compound syntax are rejected.
- Executables must match PATH resolution; the resolved file must be executable,
  owned by the current user or root, and not group/world-writable.
- Small output, source/config output, unknown plain text, and diffs pass through.
- Recognized JSON and repetitive logs use deterministic Lite transforms.
- Recognized large error/test/build output uses conservative CCA-style block ranking.
- Compression is fail-open; if raw recovery cannot be written, output is not replaced.
- Stdout and stderr are captured in private disk-backed files, then returned with labels and the real exit status.
- If even the runtime temp directory is unavailable (for example a strict
  read-only sandbox), the wrapper replaces itself with the original validated
  command and performs no compression.
- Cancellation is forwarded to the child process group with bounded SIGKILL fallback and reap.
- `decision:block`, PostToolUse replacement, model calls, and provider proxies are not used.
- RTK requires explicit persisted configuration and is validated again before execution.

Codex currently cannot replace model-visible output from `PostToolUse`, so
non-wrapped commands remain audit-only. `safe` deliberately rejects compound
shell syntax; use one eligible read-only command per tool call when active
compression matters. Arbitrary pipeline execution is outside this plugin's
`shell=False` security boundary.

The CCA-style algorithm is an independent implementation inspired by the
recovery and conservative-selection ideas in
[`command-compressor-agent`](https://github.com/linger-alpha/command-compressor-agent).
No CCA source code is vendored. Optional RTK integration targets
[`rtk-ai/rtk`](https://github.com/rtk-ai/rtk).

## Privacy

Everything stays local. No telemetry or network calls are implemented. Raw
outputs may contain whatever the original command printed, so the raw directory
is private and should be treated as sensitive.

## Safety notes

- Hook definitions require Codex review/trust after installation or changes.
- `safe` has passed static adversarial review. `full` should remain disabled
  until one live approval-semantics test confirms the rewritten wrapper does not
  broaden the expected approval flow.
- A command not recognized by the wrapper executes normally through native
  Codex Bash; it is never routed through tokenpipe.
