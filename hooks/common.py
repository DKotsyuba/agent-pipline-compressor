#!/usr/bin/env python3
"""Shared, fail-open helpers for Agent Pipline Compressor hooks.

The hook layer deliberately does not persist prompts, commands, or raw output.
Persistence and aggregate accounting belong to ``scripts/tokenpipe.py``.
"""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any, Dict, Iterable, Optional


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
TOKENPIPE = PLUGIN_ROOT / "scripts" / "tokenpipe.py"
VALID_MODES = frozenset(("audit", "safe", "full"))
NATIVE_MARKER = "tokenpipe-native-v1"


def mode() -> str:
    configured = os.environ.get("TOKENPIPE_MODE")
    if configured is not None:
        value = configured.strip().lower()
        return value if value in VALID_MODES else "audit"
    config_home = Path(
        os.path.expanduser(os.environ.get("TOKENPIPE_HOME", "~/.codex/tokenpipe"))
    )
    try:
        config_path = config_home / "config.json"
        if config_path.stat().st_size > 4096:
            return "audit"
        with config_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        value = payload.get("mode") if isinstance(payload, dict) else None
        return value if value in VALID_MODES else "audit"
    except (OSError, TypeError, ValueError):
        return "audit"


def read_event() -> Optional[Dict[str, Any]]:
    try:
        value = json.load(sys.stdin)
    except (OSError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def emit(value: Dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from _strings(item)
        return
    if isinstance(value, dict):
        # Prefer the conventional text-bearing fields and avoid duplicating a
        # response by recursively walking metadata alongside its content.
        if "stdout" in value or "stderr" in value:
            for key in ("stdout", "stderr"):
                if key in value:
                    yield from _strings(value[key])
            return
        for key in ("aggregatedOutput", "output", "text", "content", "result"):
            if key in value:
                yield from _strings(value[key])
                return


def tool_output(event: Dict[str, Any]) -> Optional[str]:
    response = event.get("tool_response")
    if response is None:
        response = event.get("tool_output")
    if response is None:
        return None
    if isinstance(response, str):
        return response
    parts = [part for part in _strings(response) if part]
    return "\n".join(parts) if parts else None


def tool_input(event: Dict[str, Any]) -> Dict[str, Any]:
    value = event.get("tool_input")
    return value if isinstance(value, dict) else {}


def command_from(event: Dict[str, Any]) -> Optional[str]:
    value = tool_input(event)
    for key in ("command", "cmd"):
        command = value.get(key)
        if isinstance(command, str):
            return command
    return None


def unwrap_shell_command(command: str) -> str:
    """Unwrap Codex unified exec's exact `/bin/bash -c <command>` envelope."""
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return command
    if (len(words) == 3 and Path(words[0]).name in {"bash", "sh", "zsh"}
            and os.path.isabs(words[0]) and words[1] == "-c"):
        return words[2]
    return command


def command_category(command: Optional[str]) -> str:
    """Return a coarse non-secret category, never the command or its arguments."""
    if not command:
        return "unknown"
    command = unwrap_shell_command(command)
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return "unknown"
    if not words:
        return "unknown"
    head = Path(words[0]).name.lower()
    if head == "git" and len(words) > 1:
        sub = words[1].lower()
        return "git"
    if head in {"pytest", "py.test", "jest", "vitest"}:
        return "test"
    if head == "cargo" and len(words) > 1:
        return "rust"
    if head == "go" and len(words) > 1:
        return "go"
    if head in {"npm", "pnpm", "yarn"}:
        return "node"
    if head in {"ruff", "eslint", "mypy", "pyright", "tsc"}:
        return "lint"
    if head == "docker" and len(words) > 1:
        return "docker"
    if head == "rg":
        return "search"
    if head in {"find", "ls"}:
        return "filesystem"
    return "shell-other"


def exit_status(event: Dict[str, Any]) -> Optional[int]:
    response = event.get("tool_response")
    candidates = (response, event)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("exit_code", "exitCode", "status_code", "returncode"):
            value = candidate.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def _safe_id(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    # Lifecycle identifiers are useful for per-session statistics, but accept
    # only their expected inert alphabet and never forward arbitrary text.
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", value):
        return None
    return value


def post_request(event: Dict[str, Any], output: str) -> Dict[str, Any]:
    request: Dict[str, Any] = {
        "output": output,
        "tool_name": "Bash",
        "command_category": command_category(command_from(event)),
    }
    for source, target in (
        ("session_id", "session_id"),
        ("turn_id", "turn_id"),
        ("tool_use_id", "tool_call_id"),
        ("tool_call_id", "tool_call_id"),
    ):
        value = _safe_id(event.get(source))
        if value and target not in request:
            request[target] = value
    status = exit_status(event)
    if status is not None:
        request["exit_status"] = status
    return request


def _hook_timeout() -> float:
    try:
        timeout = float(os.environ.get("TOKENPIPE_HOOK_TIMEOUT_SEC", "30"))
    except ValueError:
        timeout = 30.0
    return max(1.0, min(timeout, 60.0))


def run_audit(event: Dict[str, Any], output: str) -> None:
    """Record a bounded observation without exposing child output to the hook."""
    if not TOKENPIPE.is_file():
        return
    try:
        subprocess.run(
            [sys.executable, str(TOKENPIPE), "post", "--mode", "audit"],
            input=json.dumps(post_request(event, output), ensure_ascii=False),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_hook_timeout(),
            check=False,
            env=os.environ.copy(),
        )
    except (OSError, subprocess.SubprocessError):
        return


def run_post_process(event: Dict[str, Any], output: str, active_mode: str,
                     categories: Optional[frozenset] = None) -> Optional[Dict[str, Any]]:
    """Run the shared compressor and return only its bounded structured result."""
    if active_mode not in {"safe", "full"} or not TOKENPIPE.is_file():
        return None
    request = post_request(event, output)
    if categories is not None:
        request["replace_categories"] = sorted(categories)
    try:
        completed = subprocess.run(
            [sys.executable, str(TOKENPIPE), "post", "--mode", active_mode],
            input=json.dumps(request, ensure_ascii=False),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_hook_timeout(),
            check=False,
            env=os.environ.copy(),
        )
        if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
            return None
        value = json.loads(completed.stdout)
        return value if isinstance(value, dict) else None
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        return None


def run_skip(event: Dict[str, Any], reason: str) -> None:
    """Record trusted metadata only; no tool output crosses this boundary."""
    if not TOKENPIPE.is_file():
        return
    category = command_category(command_from(event))
    try:
        subprocess.run(
            [
                sys.executable, str(TOKENPIPE), "skip",
                "--category", category,
                "--reason", reason,
                "--mode", "audit",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_hook_timeout(),
            check=False,
            env=os.environ.copy(),
        )
    except (OSError, subprocess.SubprocessError):
        return
