#!/usr/bin/env python3
"""Observe Bash output without ever changing model-visible tool results."""

import os
import sys

sys.dont_write_bytecode = True

from common import NATIVE_MARKER, read_event, run_audit, run_skip, tool_output


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


def main() -> int:
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
    # Always force audit: PostToolUse is observation-only in every configured
    # mode and deliberately emits no stdout whatsoever.
    run_audit(event, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
