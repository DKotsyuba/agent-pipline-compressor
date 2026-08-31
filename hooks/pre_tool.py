#!/usr/bin/env python3
"""Route strict Bash commands through tokenpipe before execution."""

from pathlib import Path
import os
import re
import shlex
import shutil
import sys
from typing import List, Optional

sys.dont_write_bytecode = True

from common import TOKENPIPE, _safe_id, emit, mode, read_event, tool_input, unwrap_shell_command


ENV_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
INTERACTIVE_FLAGS = frozenset((
    "-i", "-w", "--interactive", "--watch", "--watchall", "--watch-all",
    "--follow", "--open", "--ui", "--pdb", "--trace", "--sw",
    "--paginate",
))
MUTATING_FLAGS = frozenset(("--fix", "--fix-only", "--write"))

# Mirror of the wrapper's trusted executable roots. scripts/tokenpipe.py
# _TRUSTED_EXECUTABLE_DIRS is the authority; its full ownership and symlink
# validation remains the enforcement point at execution time.
_TRUSTED_EXECUTABLE_DIRS = (
    "/bin", "/usr/bin", "/usr/sbin", "/sbin", "/usr/local/bin", "/opt/homebrew/bin",
)


def _has_forbidden_syntax(command: str) -> bool:
    # Check operators without relying on shell parsing. Quoted metacharacters are
    # rejected too: conservative false negatives are safer than a surprising
    # rewrite of a compound command.
    if any(char in command for char in "\n\r|;&<>`$*?[]{}"):
        return True
    return any(word.startswith("~") for word in command.split())


def _wrapper_category(words: List[str]) -> Optional[str]:
    if not words:
        return None
    head = Path(words[0]).name.lower()
    args = [word.lower() for word in words[1:]]
    if head == "git":
        return "git-read"
    if head == "rg" or head == "find":
        return "search"
    if head == "ls":
        return "filesystem-read"
    if head == "docker":
        return "docker-read"
    if head in {"pytest", "py.test", "jest", "vitest"}:
        return "test"
    if head == "cargo" and args:
        return {"test": "test", "check": "lint", "clippy": "lint", "build": "build"}.get(args[0])
    if head == "go" and args:
        return {"test": "test", "vet": "lint", "build": "build"}.get(args[0])
    if head in {"ruff", "eslint", "mypy", "pyright", "tsc"}:
        return "lint"
    if head in {"npm", "pnpm", "yarn"} and args:
        action = args[1] if args[0] == "run" and len(args) > 1 else args[0]
        return {"test": "test", "lint": "lint", "typecheck": "lint", "check": "lint", "build": "build"}.get(action)
    return None


def _allowed(words: List[str], active_mode: str) -> bool:
    if not words or ENV_PREFIX.match(words[0]):
        return False
    head = Path(words[0]).name.lower()
    if head in {"rtk", "tokenpipe", "tokenpipe.py"}:
        return False
    lowered = [word.lower() for word in words[1:]]
    if any(
        flag in INTERACTIVE_FLAGS
        or flag.startswith("--watch=")
        or flag.startswith("--follow=")
        for flag in lowered
    ):
        return False
    if any(flag in MUTATING_FLAGS or flag.startswith("--output=") for flag in lowered):
        return False
    if head == "docker" and "-f" in lowered:
        return False

    if head == "git":
        if "--output" in lowered:
            return False
        return bool(lowered) and lowered[0] in {"status", "diff", "log", "show"}
    if head == "docker":
        return bool(lowered) and lowered[0] in {"ps", "logs"}
    if head == "rg":
        return not any(flag == "--pre" or flag.startswith("--pre=") for flag in lowered)
    if head == "find":
        dangerous = {
            "-delete", "-exec", "-execdir", "-ok", "-okdir",
            "-fprint", "-fprint0", "-fprintf", "-fls",
        }
        return not any(flag in dangerous for flag in lowered)
    if head == "ls":
        return True

    # Safe mode is intentionally read-only. Commands below can execute project
    # code or write build/cache artifacts, so they are full-mode only.
    if active_mode != "full":
        return False
    if head in {"pytest", "py.test", "jest", "vitest"}:
        return True
    if head == "cargo":
        return bool(lowered) and lowered[0] in {"test", "check", "clippy", "build"}
    if head == "go":
        return bool(lowered) and lowered[0] in {"test", "vet", "build"}
    if head in {"npm", "pnpm", "yarn"}:
        # Package scripts are restricted to reporting/build tasks. Installation,
        # publishing, and arbitrary user-named scripts are deliberately excluded.
        scripts = {"test", "lint", "build", "typecheck", "check"}
        if not lowered:
            return False
        if lowered[0] in scripts:
            return True
        return len(lowered) > 1 and lowered[0] == "run" and lowered[1] in scripts
    if head in {"ruff", "eslint", "mypy", "pyright", "tsc"}:
        return True
    return False


def _trusted_head(head: str) -> bool:
    # Cheap prefix gate: resolve the head (absolute paths directly, otherwise
    # shutil.which through the hook process PATH) and reject anything outside
    # the trusted roots. The wrapper refuses those with exit 126, so rewriting
    # them would break the command instead of compressing it.
    candidate = head if os.path.isabs(head) else shutil.which(head)
    if not candidate:
        return False
    # Test the PATH-resolved location itself, not its realpath: Homebrew
    # installs are symlinks into Cellar, and the wrapper accepts them by
    # their trusted-directory location. Symlink/ownership scrutiny stays
    # with the wrapper at execution time.
    candidate = os.path.normpath(candidate)
    return any(
        candidate == prefix or candidate.startswith(prefix + os.sep)
        for prefix in _TRUSTED_EXECUTABLE_DIRS
    )


def rewrite(command: str, active_mode: Optional[str] = None,
            session_id: Optional[str] = None,
            tool_call_id: Optional[str] = None) -> Optional[str]:
    active_mode = active_mode or mode()
    if active_mode not in {"safe", "full"}:
        return None
    command = unwrap_shell_command(command)
    if not command.strip() or _has_forbidden_syntax(command):
        return None
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not _allowed(words, active_mode):
        return None
    category = _wrapper_category(words)
    if not category:
        return None
    if not _trusted_head(words[0]):
        # Full-mode heads (cargo, npm, pnpm, yarn, tsc, pyright, venv pytest)
        # usually live under $HOME; passing the command through untouched beats
        # rewriting it into a wrapper that would exit 126.
        return None
    # Resolve the installed plugin's absolute script path inside the hook. The
    # later Bash process does not inherit the hook-only PLUGIN_ROOT variable.
    # Only the already parsed argv is shell-quoted; no opaque/base64 transport is
    # used, keeping approvals and diagnostics human-readable.
    fields = [
        shlex.quote(sys.executable), shlex.quote(str(TOKENPIPE)), "exec",
        "--category", category,
    ]
    if session_id:
        fields += ["--session-id", shlex.quote(session_id)]
    if tool_call_id:
        fields += ["--tool-call-id", shlex.quote(tool_call_id)]
    return " ".join(fields) + " -- " + shlex.join(words)


def main() -> int:
    # Claude Code replaces tool output directly in PostToolUse. Rewriting the
    # command there would duplicate execution policy and lose Claude's native
    # permission semantics.
    if os.environ.get("CLAUDE_PLUGIN_ROOT") and not os.environ.get("PLUGIN_ROOT"):
        return 0
    active_mode = mode()
    if active_mode not in {"safe", "full"}:
        return 0
    event = read_event()
    if event is None:
        return 0
    original_input = tool_input(event)
    command_key = next((key for key in ("command", "cmd")
                        if isinstance(original_input.get(key), str)), None)
    if command_key is None:
        return 0
    command = original_input[command_key]
    session_id = _safe_id(event.get("session_id"))
    tool_call_id = _safe_id(event.get("tool_use_id") or event.get("tool_call_id"))
    updated_command = rewrite(command, active_mode, session_id, tool_call_id)
    if updated_command is None:
        return 0
    # Preserve every Bash input field (cwd, timeout, tty, etc.) and change only
    # the command. This also keeps permission/sandbox metadata intact.
    updated_input = dict(original_input)
    updated_input[command_key] = updated_command
    emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated_input,
        }
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
