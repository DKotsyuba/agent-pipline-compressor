#!/usr/bin/env python3
"""Compress Claude Code Bash results through the shared tokenpipe core."""

import importlib.util
import json
import os
import re
import shlex
import sys
from typing import Any, Dict, Optional

sys.dont_write_bytecode = True

from common import TOKENPIPE, _safe_id, command_category, mode, read_event, tool_output


def _load_tokenpipe():
    spec = importlib.util.spec_from_file_location("tokenpipe_claude_core", str(TOKENPIPE))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tokenpipe = _load_tokenpipe()
# Marker and header template are owned by the compressor so the rendered text
# and the cost its net-win gate prices stay identical.
CLAUDE_MARKER = tokenpipe.CLAUDE_RECOVERY_MARKER


def _limit() -> int:
    try:
        value = int(os.environ.get("TOKENPIPE_CLAUDE_MAX_BYTES", str(16 * 1024 * 1024)))
    except ValueError:
        value = 16 * 1024 * 1024
    return max(1024, min(value, 64 * 1024 * 1024))


AUDIT_IGNORED_TOOLS = frozenset(("Read", "Edit", "Write", "NotebookEdit"))


def _audit_tool_name(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "unknown"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:64] or "unknown"


def _audit_request(event: Dict[str, Any], output: str) -> Dict[str, Any]:
    request = {
        "output": output,
        "tool_name": _audit_tool_name(event.get("tool_name")),
        "command_category": command_category(
            (event.get("tool_input") or {}).get("command")
            if isinstance(event.get("tool_input"), dict) else None
        ),
    }
    session_id = _safe_id(event.get("session_id"))
    if session_id:
        request["session_id"] = session_id
    call_id = _safe_id(event.get("tool_use_id") or event.get("tool_call_id"))
    if call_id:
        request["tool_call_id"] = call_id
    return request


def audit(event: Dict[str, Any]) -> None:
    """Record one bounded audit-only metric for a non-Bash Claude tool event.

    Measurement only: the shared core runs with the mode forced to ``audit``,
    so nothing is ever replaced, spooled, or printed for these tools.
    """
    response = event.get("tool_response")
    if isinstance(response, dict) and response.get("isImage") is True:
        return
    output = tool_output(event)
    if not output:
        return
    limit = _limit()
    if len(output) > limit or len(output.encode("utf-8", "replace")) > limit:
        return
    try:
        tokenpipe.process(_audit_request(event, output), "audit")
    except Exception:
        pass


def _request(event: Dict[str, Any], stream: str, output: str) -> Dict[str, Any]:
    call_id = _safe_id(event.get("tool_use_id") or event.get("tool_call_id"))
    request = {
        "output": output,
        "tool_name": "ClaudeBash." + stream,
        "command_category": command_category(
            (event.get("tool_input") or {}).get("command")
            if isinstance(event.get("tool_input"), dict) else None
        ),
    }
    session_id = _safe_id(event.get("session_id"))
    if session_id:
        request["session_id"] = session_id
    if call_id:
        request["tool_call_id"] = call_id + "-" + stream
    return request


def _recovery_context(streams: Dict[str, Dict[str, Any]]) -> str:
    """Render the recovery context shown with a Claude replacement.

    Args:
        streams: Replaced stream name (``stdout``/``stderr``) to the
            :func:`tokenpipe.process` result that produced it. Every present
            entry must carry a ``raw_ref`` path and may carry a
            ``recovery_preview``, whose shape the compressor owns. Not mutated.

    Returns:
        One ``additionalContext`` line naming each stream's recovery reference
        and the command that restores it, followed by one ``show --range``
        preview per stream whose replacement elided a middle section. Streams
        that were not bounded add no preview. The template is owned by
        ``scripts/tokenpipe.py`` so this text and the header cost priced by its
        net-win gate cannot drift apart.
    """
    recover = "/usr/bin/python3 %s show" % shlex.quote(str(TOKENPIPE))
    parts = []
    previews = []
    for stream in ("stdout", "stderr"):
        result = streams.get(stream)
        if not result:
            continue
        parts.append("%s raw_ref=%s" % (stream, result["raw_ref"]))
        preview = result.get("recovery_preview")
        if preview:
            previews.append("%s %s" % (stream, preview))
    return tokenpipe.claude_recovery_header(parts, recover, CLAUDE_MARKER, previews)


def adapt(event: Dict[str, Any], active_mode: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Build one transactional Claude PostToolUse replacement response.

    Args:
        event: Claude Bash hook event. The input mapping is never mutated.
        active_mode: Optional explicit ``audit``, ``safe``, or ``full`` mode.

    Returns:
        A Claude ``hookSpecificOutput`` mapping when every changed stream has a
        recoverable raw file, otherwise ``None`` for exact host passthrough.

    Stdout and stderr are spooled before one shared cleanup transaction. Every
    surviving reference is read back and compared with its exact original; a
    cap, cleanup, or recovery failure removes new refs and fails open for the
    whole response. Images, unsupported shapes, and oversized streams pass
    through without side effects beyond bounded metrics.

    Non-Bash tools take an audit-only path with no replacement branch: their
    textual output feeds exactly one forced-audit metric and this function
    always returns ``None`` for them, so nothing reaches stdout.
    """
    tool_name = event.get("tool_name")
    if tool_name not in (None, "Bash"):
        if tool_name not in AUDIT_IGNORED_TOOLS:
            audit(event)
        return None
    response = event.get("tool_response")
    if not isinstance(response, dict) or response.get("isImage") is True:
        return None
    streams = {}
    deferred_metrics = []
    updated = dict(response)
    limit = _limit()
    selected_mode = active_mode or mode()
    for stream in ("stdout", "stderr"):
        output = response.get(stream)
        if not isinstance(output, str) or not output:
            continue
        if len(output) > limit or len(output.encode("utf-8", "replace")) > limit:
            continue
        try:
            result = tokenpipe.process(
                _request(event, stream, output), selected_mode,
                cleanup=False, record_metric=False,
            )
        except Exception:
            continue
        metric = result.pop("_metric", None)
        if metric:
            deferred_metrics.append((metric, result.get("action") == "replace"))
        if result.get("action") != "replace" or not result.get("raw_ref"):
            continue
        updated[stream] = result["output"]
        streams[stream] = result
    if not streams:
        for metric, _ in deferred_metrics:
            try:
                tokenpipe._append_metric(metric)
            except Exception:
                pass
        return None
    refs = [result["raw_ref"] for result in streams.values()]
    try:
        if not tokenpipe.cleanup_spool(protected=refs):
            raise OSError("raw output exceeds configured spool cap")
        for stream, result in streams.items():
            if tokenpipe.show_raw(result["raw_ref"]) != response[stream]:
                raise OSError("raw output failed recovery validation")
    except Exception:
        for raw_ref in refs:
            try:
                os.unlink(raw_ref)
            except OSError:
                pass
        for metric, was_replacement in deferred_metrics:
            if not was_replacement:
                try:
                    tokenpipe._append_metric(metric)
                except Exception:
                    pass
        return None
    for metric, _ in deferred_metrics:
        try:
            tokenpipe._append_metric(metric)
        except Exception:
            pass
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": updated,
            "additionalContext": _recovery_context(streams),
        }
    }


def main() -> int:
    event = read_event()
    if event is None:
        return 0
    result = adapt(event)
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
