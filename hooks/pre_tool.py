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


def _git_subcommand_index(args: List[str]) -> Optional[int]:
    """Return a supported Git subcommand index after safe global flags.

    Args:
        args (List[str]): Arguments after ``git``.

    Returns:
        Optional[int]: Read-only subcommand index, or ``None`` when unsupported.
            Git's ``-c`` option is always unsupported because it can select
            executable configuration outside the wrapper's environment.
    """
    index = 0
    while index < len(args):
        item = args[index]
        if item == "--no-pager":
            index += 1
        elif item == "-C":
            if index + 1 >= len(args):
                return None
            index += 2
        elif item.startswith("--git-dir=") or item.startswith("--work-tree="):
            index += 1
        elif item.startswith("-"):
            return None
        else:
            return index if item in {"status", "diff", "log", "show"} else None
    return None


def _read_file_args(args: List[str], command: str) -> bool:
    """Return whether filesystem-read arguments contain a plain file path.

    Args:
        args (List[str]): Lower-cased arguments after a file-reading command.
        command (str): The file-reading executable name.

    Returns:
        bool: ``True`` for one or more non-stdin file operands.
    """
    files = []
    options_end = False
    index = 0
    while index < len(args):
        item = args[index]
        if not options_end and item == "--":
            options_end = True
            index += 1
            continue
        if not options_end and item.startswith("-"):
            if command in {"head", "tail"} and item in {"-n", "-c"} and index + 1 < len(args):
                index += 2
            else:
                index += 1
            continue
        if not item or item == "-" or item.startswith("-"):
            return False
        files.append(item)
        index += 1
    return bool(files)


def _grep_has_file(args: List[str]) -> bool:
    """Return whether grep has a file operand and does not read stdin only.

    Args:
        args (List[str]): Lower-cased arguments after ``grep``.

    Returns:
        bool: ``True`` when grep has a pattern and a plain file operand.
    """
    positional = []
    pattern_option = False
    index = 0
    while index < len(args):
        item = args[index]
        if item == "--":
            positional.extend(args[index + 1:])
            break
        if item in {"-e", "-f"}:
            pattern_option = True
            if index + 1 >= len(args):
                return False
            index += 2
        elif item.startswith("-"):
            index += 1
        else:
            positional.append(item)
            index += 1
    return len(positional) >= (1 if pattern_option else 2) and positional[-1] != "-" and not positional[-1].startswith("-")


def _jq_read_only(args: List[str]) -> bool:
    """Return whether jq has one filter and one or more file operands.

    Args:
        args (List[str]): Lower-cased arguments after ``jq``.

    Returns:
        bool: ``True`` only for file-backed read-only jq usage.
    """
    positional = []
    index = 0
    while index < len(args):
        item = args[index]
        if item in {"--rawfile", "--slurpfile", "-f", "--from-file"} or item.startswith("--arg"):
            return False
        if item == "--":
            positional.extend(args[index + 1:])
            break
        if item.startswith("-"):
            index += 1
            continue
        positional.append(item)
        index += 1
    return len(positional) >= 2 and all(item != "-" and not item.startswith("-") for item in positional[1:])


def _python_pytest(words: List[str]) -> bool:
    """Return whether words invoke pytest through a Python interpreter.

    Args:
        words (List[str]): Complete executable and argument vector.

    Returns:
        bool: ``True`` for ``python[3[.N]] -m pytest``.
    """
    head = Path(words[0]).name.lower() if words else ""
    return (
        (head == "python" or head == "python3" or
         (head.startswith("python3.") and head[8:].isdigit()))
        and len(words) >= 3
        and words[1].lower() == "-m"
        and words[2].lower() == "pytest"
    )


def _wrapper_category(words: List[str]) -> Optional[str]:
    """Classify a command eligible for the native wrapper.

    Args:
        words (List[str]): Shell-split executable and arguments.

    Returns:
        Optional[str]: Conservative wrapper category, or ``None`` when the
        command is unsupported or stdin-only.
    """
    if not words:
        return None
    head = Path(words[0]).name.lower()
    args = [word.lower() for word in words[1:]]
    if head == "git" and _git_subcommand_index(words[1:]) is not None:
        return "git-read"
    if head == "rg" or (head == "grep" and _grep_has_file(args)) or head == "find":
        return "search"
    if head in {"cat", "head", "tail", "wc"} and _read_file_args(args, head):
        return "filesystem-read"
    if head == "jq" and _jq_read_only(args):
        return "filesystem-read"
    if head == "ls":
        return "filesystem-read"
    if head == "docker" and args and (args[0] in {"ps", "logs", "images"} or args[:2] == ["compose", "ps"]):
        return "docker-read"
    if head == "gh" and args in (
        ["pr", "list"], ["pr", "view"], ["pr", "checks"], ["pr", "status"],
        ["issue", "list"], ["issue", "view"], ["run", "list"], ["run", "view"],
    ):
        return "gh-read"
    if head in {"pytest", "py.test", "jest", "vitest"} or _python_pytest(words):
        return "test"
    if head == "uv" and len(args) >= 2 and args[0] == "run" and (
        args[1] == "pytest" or args[1:4] == ["python", "-m", "pytest"]
    ):
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
    """Check shell words against the hook's conservative execution policy.

    Args:
        words (List[str]): Shell-split executable and arguments.
        active_mode (str): ``safe`` or ``full`` policy mode.

    Returns:
        bool: Whether the hook may rewrite this command into the wrapper.
    """
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
    if head == "git" and any(flag in {"-o", "--output"} or flag.startswith("--output=") for flag in lowered):
        return False

    if head == "git":
        return _git_subcommand_index(words[1:]) is not None
    if head == "docker":
        if "-f" in lowered:
            return False
        return bool(lowered) and (lowered[0] in {"ps", "logs", "images"} or lowered[:2] == ["compose", "ps"])
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

    if head in {"cat", "head", "tail", "wc", "jq", "grep", "gh"}:
        if head in {"head", "tail"} and any(flag in {"-f", "-F"} for flag in lowered):
            return False
        return _wrapper_category(words) is not None

    # Safe mode is intentionally read-only. Commands below can execute project
    # code or write build/cache artifacts, so they are full-mode only.
    if active_mode != "full":
        return False
    if head in {"pytest", "py.test", "jest", "vitest"} or _python_pytest(words):
        return True
    if head == "uv" and len(lowered) >= 2 and lowered[0] == "run" and (
        lowered[1] == "pytest" or lowered[1:4] == ["python", "-m", "pytest"]
    ):
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
