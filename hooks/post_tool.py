#!/usr/bin/env python3
"""Observe Bash output without ever changing model-visible tool results."""

import importlib.util
import os
import sys
from typing import FrozenSet, Optional, Tuple

sys.dont_write_bytecode = True

from common import (NATIVE_MARKER, TOKENPIPE, emit, exit_status, mode,
                    post_replace_value, read_event, run_audit,
                    run_post_process, run_skip, tool_output)


def audit_limit() -> int:
    try:
        value = int(os.environ.get("TOKENPIPE_AUDIT_MAX_BYTES", str(1024 * 1024)))
    except ValueError:
        value = 1024 * 1024
    return max(1024, min(value, 16 * 1024 * 1024))


def is_native_output(output: str) -> bool:
    if output.startswith(NATIVE_MARKER):
        return True
    # Unified exec wraps the actual command output in a deterministic envelope.
    # Restrict the secondary check to that envelope shape; a generic substring
    # match would let arbitrary stdout spoof native-pipeline telemetry.
    if not output.startswith("Chunk ID:"):
        return False
    marker = "\nFinal output:\n" + NATIVE_MARKER
    return marker in output.replace("\r\n", "\n")


def post_replace_gate() -> Tuple[bool, Optional[FrozenSet[str]]]:
    """Parse TOKENPIPE_POST_REPLACE into (replace enabled, category allowlist).

    A None allowlist means every content category is eligible ("1"). Unset,
    "0", or empty values disable replacement; a comma-separated list restricts
    replacement to the named categories. Case and surrounding whitespace are
    ignored, unrecognized tokens never match, and ambiguous mixes of switch
    values with categories fail closed to audit-only.
    """
    raw = post_replace_value()
    if raw is None:
        return False, None
    tokens = frozenset(
        token for token in (part.strip().lower() for part in raw.split(",")) if token
    )
    if not tokens or tokens == frozenset(("0",)):
        return False, None
    if tokens == frozenset(("1",)):
        return True, None
    if "0" in tokens or "1" in tokens:
        return False, None
    return True, tokens


def recovery_header(active_mode: str, strategy: Optional[str],
                    status: Optional[int], raw_ref: str) -> Optional[str]:
    """Render the replacement header from the compressor's shared template.

    The template lives in ``scripts/tokenpipe.py`` so the header this hook
    shows and the header cost the net-win gate prices cannot drift apart. The
    compressor module is loaded lazily, so audit-only events never pay for it.

    Args:
        active_mode: Resolved ``safe`` or ``full`` mode for this event.
        strategy: Compression strategy reported by the compressor; ``None`` or
            an empty value renders ``unknown``.
        status: Child exit status, or ``None`` to omit the field.
        raw_ref: Absolute recovery path returned with the replacement.

    Returns:
        The single header line without a trailing newline, or ``None`` when the
        compressor module cannot be loaded. A ``None`` result makes the caller
        fail open and leave the exact original output visible.
    """
    try:
        spec = importlib.util.spec_from_file_location("tokenpipe_post_core", str(TOKENPIPE))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.post_recovery_header(active_mode, strategy, status, raw_ref)
    except Exception:
        return None


def main() -> int:
    if os.environ.get("CLAUDE_PLUGIN_ROOT") and not os.environ.get("PLUGIN_ROOT"):
        from claude_post_tool import main as claude_main
        return claude_main()
    event = read_event()
    if event is None:
        return 0
    output = tool_output(event)
    if output is None:
        return 0
    # `tokenpipe exec` already recorded the native command once. Its stable
    # marker starts at byte zero and prevents duplicate PostToolUse telemetry.
    if is_native_output(output):
        return 0
    limit = audit_limit()
    # One Unicode code point occupies at least one UTF-8 byte, so the cheap
    # character check avoids encoding/copying obviously oversized output.
    if len(output) > limit:
        run_skip(event, "audit-output-overflow")
        return 0
    if len(output.encode("utf-8", "replace")) > limit:
        run_skip(event, "audit-output-overflow")
        return 0
    active_mode = mode()
    replace_enabled, categories = post_replace_gate()
    if replace_enabled and active_mode in {"safe", "full"}:
        result = run_post_process(event, output, active_mode, categories)
        if result is not None:
            # The compressor already recorded exactly one honest metric for this
            # event; a follow-up audit pass would double-count it.
            if result.get("action") == "replace" and result.get("raw_ref"):
                header = recovery_header(
                    active_mode, result.get("strategy"), exit_status(event),
                    result["raw_ref"],
                )
                if header is not None:
                    # The compressor owns the preview shape; the hook only places it.
                    preview = result.get("recovery_preview")
                    if preview:
                        header += "; " + preview
                    emit({"decision": "block", "reason": header + "\n" + result["output"]})
            return 0
    # Always force audit: PostToolUse is observation-only in every configured
    # ordinary Codex mode and deliberately emits no stdout whatsoever.
    run_audit(event, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
