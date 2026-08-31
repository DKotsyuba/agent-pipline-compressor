#!/usr/bin/env python3
"""Deterministic, local-only tool-output compression for Codex.

The module deliberately uses no network, model, or third-party tokenizer. Token
counts are UTF-8 based estimates and are named accordingly in every interface.
"""

from __future__ import print_function

import argparse
import datetime as _dt
import errno
import fcntl
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid


VERSION = "0.2.0"
ANSI_RE = re.compile(r"\x1b(?:[@-_][0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
SECRET_KEY_RE = re.compile(r"(?i)(token|secret|password|authorization|api[_-]?key|cookie)")
ERROR_RE = re.compile(
    r"(?i)(error|failed|failure|fatal|panic|exception|traceback|assert|timeout|"
    r"segmentation|denied|not found|(?:http(?:/[0-9.]+)?\s+|"
    r"status(?:\s+code)?\s*[:=]?\s*|returned?\s+)[45]\d\d\b)"
)
STRONG_ERROR_RE = re.compile(
    r"(?i)(failed|failure|fatal|panic|exception|traceback|assert|timeout|"
    r"segmentation|denied|not found|(?:io|os)\s+error|"
    r"(?:http(?:/[0-9.]+)?\s+|status(?:\s+code)?\s*[:=]?\s*|"
    r"returned?\s+)[45]\d\d\b)"
)
SUMMARY_RE = re.compile(
    r"(?i)(summary|tests? (?:passed|failed)|passed|failed|warnings?|errors?|"
    r"collected|finished|completed|total|result)"
)
DIFF_RE = re.compile(r"(?m)^(diff --git |@@ |\+\+\+ |--- |Index: )")
CODE_RE = re.compile(
    r"(?m)^\s*(?:def |class |function |import |from \S+ import |const |let |var |"
    r"fn |struct |enum |interface |package |#include|using namespace|public class )"
)
CONFIG_RE = re.compile(
    r"(?m)(^\s*\[[A-Za-z0-9_.-]+\]\s*$|^\s*[A-Za-z_][A-Za-z0-9_.-]*\s*=\s*\S+|"
    r"^\s*[A-Za-z_][A-Za-z0-9_.-]*:\s+\S+)"
)


def plugin_version():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".codex-plugin", "plugin.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        value = payload.get("version") if isinstance(payload, dict) else None
        return value if isinstance(value, str) and 0 < len(value) <= 128 else VERSION
    except (OSError, TypeError, ValueError):
        return VERSION


def _home():
    return os.path.abspath(os.path.expanduser(os.environ.get("TOKENPIPE_HOME", "~/.codex/tokenpipe")))


def _raw_root():
    return os.path.join(_home(), "raw")


def _metrics_path():
    return os.path.join(_home(), "metrics.jsonl")


def _runtime_home():
    configured = os.environ.get("TOKENPIPE_RUNTIME_HOME")
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.join(tempfile.gettempdir(), "codex-tokenpipe-%d" % os.getuid())


def _runtime_raw_root():
    return os.path.join(_runtime_home(), "raw")


def _runtime_metrics_path():
    return os.path.join(_runtime_home(), "metrics.jsonl")


def _config_path():
    return os.path.join(_home(), "config.json")


def _open_private_dir(path, create=True):
    """Open a contained private directory without following symlinks.

    Args:
        path (str | os.PathLike): Absolute or user-expandable directory path.
        create (bool): Create missing components with mode ``0700`` when true.

    Returns:
        int: An open directory descriptor owned by the caller.

    Raises:
        OSError: A component is missing, inaccessible, or the platform lacks
            directory-relative no-follow primitives.
        ValueError: A component is not a trusted directory, is writable by
            another user, or the final directory is not private and user-owned.

    The function never follows or chmods an existing symlink. Ancestors may be
    readable by other users, but only root-owned sticky temporary directories
    may be writable by them. The final directory must be owned by this process
    and have no group/other permission bits.
    """
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise OSError(errno.ENOTSUP, "secure directory traversal is unavailable")
    absolute = os.path.abspath(os.path.expanduser(path))
    if sys.platform == "darwin":
        # macOS exposes these root-owned compatibility aliases as symlinks.
        # Normalize only the fixed platform prefixes; never resolve caller-
        # controlled descendants or the final private directory.
        for alias in ("/var", "/tmp"):
            if absolute == alias or absolute.startswith(alias + os.sep):
                absolute = "/private" + absolute
                break
    parts = [part for part in absolute.split(os.sep) if part]
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    current_fd = os.open(os.sep, flags)
    try:
        for index, component in enumerate(parts):
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as exc:
                if not create or exc.errno != errno.ENOENT:
                    raise
                os.mkdir(component, 0o700, dir_fd=current_fd)
                next_fd = os.open(component, flags, dir_fd=current_fd)
            info = os.fstat(next_fd)
            mode = stat.S_IMODE(info.st_mode)
            final = index == len(parts) - 1
            root_sticky = info.st_uid == 0 and bool(mode & stat.S_ISVTX)
            trusted_owner = info.st_uid in (0, os.getuid())
            safe_writes = not (mode & 0o022) or root_sticky
            private_final = not final or (info.st_uid == os.getuid() and not (mode & 0o077))
            if not stat.S_ISDIR(info.st_mode) or not trusted_owner or not safe_writes or not private_final:
                os.close(next_fd)
                raise ValueError("unsafe private directory component")
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _mkdir_private(path):
    """Create and validate a user-owned ``0700`` directory.

    Args:
        path (str | os.PathLike): Directory to create or validate.

    Returns:
        None: The validation descriptor is closed before returning.

    Raises:
        OSError: Secure traversal or creation fails.
        ValueError: Existing ownership, mode, type, or containment is unsafe.
    """
    directory_fd = _open_private_dir(path, create=True)
    os.close(directory_fd)


def estimate_tokens(text):
    """Return a clearly approximate o200k-like token count.

    UTF-8 byte length / 3.5 is intentionally conservative for mixed source code,
    JSON, and English logs. It is not provider usage accounting.
    """
    if not text:
        return 0
    return max(1, int(math.ceil(len(text.encode("utf-8", "replace")) / 3.5)))


def configured_settings():
    """Read bounded private settings, failing safely to an empty object."""
    try:
        with open(_config_path(), "r", encoding="utf-8") as handle:
            if os.fstat(handle.fileno()).st_size > 4096:
                return {}
            value = json.load(handle)
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
    return value if isinstance(value, dict) else {}


def configured_mode():
    """Read the persistent private mode, failing safely to audit."""
    value = configured_settings().get("mode")
    if value not in ("audit", "safe", "full"):
        return "audit"
    return value


def _write_settings(settings):
    encoded = (json.dumps(settings, sort_keys=True) + "\n").encode("utf-8")
    _atomic_private_write(_config_path(), encoded)


def set_configured_mode(mode):
    if mode not in ("audit", "safe", "full"):
        raise ValueError("invalid tokenpipe mode")
    settings = configured_settings()
    settings["mode"] = mode
    _write_settings(settings)
    return mode


def _safe_component(value, fallback):
    value = str(value or fallback)
    value = re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:96]
    return value or fallback


def _atomic_private_write(path, data):
    parent = os.path.dirname(path)
    _mkdir_private(parent)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_all(fd, data):
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise OSError(errno.EIO, "short write")
        written += count


def spool_raw(text, session_id, tool_call_id, root=None):
    """Persist exact tool output in a contained private spool file.

    Args:
        text (str): Raw model-sensitive tool output to store as UTF-8.
        session_id (str | None): Untrusted lifecycle identifier, sanitized for
            the session directory name.
        tool_call_id (str | None): Untrusted lifecycle identifier, sanitized
            for the file prefix.
        root (str | None): Optional private spool root; defaults to user state.

    Returns:
        str: Absolute recovery path for the newly created ``0600`` regular file.

    Raises:
        OSError: Secure allocation, writing, syncing, or uniqueness fails.
        ValueError: Directory/file containment, ownership, type, or links are
            unsafe. No attacker-controlled symlink is followed or chmodded.
    """
    session = _safe_component(session_id, "unknown-session")
    call = _safe_component(tool_call_id, "call-" + uuid.uuid4().hex[:12])
    directory = os.path.join(root or _raw_root(), session)
    directory_fd = _open_private_dir(directory, create=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    data = text.encode("utf-8", "replace")
    try:
        for _ in range(32):
            name = call + "-" + uuid.uuid4().hex[:12] + ".log"
            try:
                fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    continue
                raise
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_nlink != 1):
                    raise ValueError("raw spool target is not a private regular file")
                os.fchmod(fd, 0o600)
                _write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            return os.path.join(directory, name)
        raise OSError(errno.EEXIST, "could not allocate unique raw spool file")
    finally:
        os.close(directory_fd)


def cleanup_spool(now=None, root=None, protected=None):
    """Enforce raw-output TTL and size caps without escaping the spool root.

    Args:
        now (float | None): Unix timestamp used for deterministic TTL checks.
        root (str | None): Private spool root; defaults to user state.
        protected (Iterable[str] | None): Recovery paths that must survive this
            cleanup transaction while unprotected files are reclaimed first.

    Returns:
        bool: True when surviving files fit the configured byte cap. False means
        protected files alone prevent compliance; callers must fail open and
        remove their newly created protected outputs.

    Unsafe roots are treated as absent by raising to the caller; symlinked
    directories and non-regular or foreign-owned entries are never traversed or
    removed. Cleanup has no effect outside the validated root.
    """
    now = time.time() if now is None else now
    ttl = max(0, int(os.environ.get("TOKENPIPE_RAW_TTL_SECONDS", str(7 * 86400))))
    max_bytes = max(0, int(os.environ.get("TOKENPIPE_RAW_MAX_BYTES", str(256 * 1024 * 1024))))
    root = root or _raw_root()
    try:
        root_fd = _open_private_dir(root, create=False)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return True
        raise
    else:
        os.close(root_fd)
    real_root = os.path.realpath(root)
    protected_paths = {os.path.realpath(path) for path in (protected or ())}
    files = []
    for base, dirs, names in os.walk(root, topdown=True, followlinks=False):
        dirs[:] = [
            name for name in dirs
            if not os.path.islink(os.path.join(base, name))
            and os.path.commonpath((real_root, os.path.realpath(os.path.join(base, name)))) == real_root
        ]
        for name in names:
            path = os.path.join(base, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            real_path = os.path.realpath(path)
            if (os.path.commonpath((real_root, real_path)) != real_root
                    or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()):
                continue
            if real_path not in protected_paths and ttl and now - info.st_mtime > ttl:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            else:
                files.append((info.st_mtime, info.st_size, path, real_path))
    total = sum(item[1] for item in files)
    for _, size, path, real_path in sorted(files):
        if total <= max_bytes:
            break
        if real_path in protected_paths:
            continue
        try:
            os.unlink(path)
            total -= size
        except OSError:
            pass
    return total <= max_bytes


def _json_sanitize(value, depth=0):
    if depth > 12:
        return "[depth elided]"
    if isinstance(value, dict):
        items = list(value.items())
        chosen = items[:40]
        result = {}
        for key, item in chosen:
            safe_key = str(key)
            if SECRET_KEY_RE.search(safe_key):
                # Preserve values in model-visible output; this is not telemetry.
                result[safe_key] = _json_sanitize(item, depth + 1)
            else:
                result[safe_key] = _json_sanitize(item, depth + 1)
        if len(items) > len(chosen):
            result["__tokenpipe_omitted_keys__"] = len(items) - len(chosen)
        return result
    if isinstance(value, list):
        if len(value) <= 30:
            return [_json_sanitize(x, depth + 1) for x in value]
        return (
            [_json_sanitize(x, depth + 1) for x in value[:20]]
            + [{"__tokenpipe_omitted_items__": len(value) - 25}]
            + [_json_sanitize(x, depth + 1) for x in value[-5:]]
        )
    if isinstance(value, str) and len(value) > 1200:
        return value[:900] + "\n...[tokenpipe omitted %d chars]...\n" % (len(value) - 1050) + value[-150:]
    return value


def lite_json(text):
    parsed = json.loads(text)
    return json.dumps(_json_sanitize(parsed), ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def lite_log(text):
    text = ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    output = []
    previous = None
    repeated = 0
    blanks = 0

    def flush_repeat():
        if repeated:
            output.append("[previous line repeated %d more times]" % repeated)

    for line in lines:
        if not line.strip():
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        if line == previous and line.strip():
            repeated += 1
            continue
        flush_repeat()
        repeated = 0
        output.append(line)
        previous = line
    flush_repeat()
    return "\n".join(output).strip()


def _blocks(text):
    paragraphs = re.split(r"\n\s*\n", text.strip())
    blocks = []
    for paragraph in paragraphs:
        lines = paragraph.splitlines()
        for start in range(0, len(lines), 40):
            chunk = "\n".join(lines[start:start + 40]).strip()
            if chunk:
                blocks.append(chunk)
    return blocks


def cca_rank(text, target_chars=6000):
    """Conservative deterministic block ranking; no LLM is involved."""
    cleaned = lite_log(text)
    blocks = _blocks(cleaned)
    if not blocks:
        return cleaned
    ranked = []
    count = len(blocks)
    for index, block in enumerate(blocks):
        score = 0
        if ERROR_RE.search(block):
            score += 100
        if SUMMARY_RE.search(block):
            score += 45
        if index == 0:
            score += 35
        if index >= max(0, count - 2):
            score += 40
        score += int(20.0 * index / max(1, count - 1))
        ranked.append((score, index, block))
    selected = []
    used = 0
    for score, index, block in sorted(ranked, key=lambda item: (-item[0], item[1])):
        cost = len(block) + 2
        if selected and used + cost > target_chars:
            continue
        selected.append((index, block))
        used += cost
        if used >= target_chars:
            break
    selected.sort()
    rendered = []
    prior = -1
    for index, block in selected:
        if prior >= 0 and index > prior + 1:
            rendered.append("[tokenpipe omitted %d lower-ranked block(s)]" % (index - prior - 1))
        rendered.append(block)
        prior = index
    if selected and selected[0][0] > 0:
        rendered.insert(0, "[tokenpipe omitted %d lower-ranked block(s)]" % selected[0][0])
    if selected and selected[-1][0] < count - 1:
        rendered.append("[tokenpipe omitted %d lower-ranked block(s)]" % (count - 1 - selected[-1][0]))
    return "\n\n".join(rendered)


def _is_binary_text(text):
    """Return whether text contains binary or control-heavy content.

    Args:
        text (str): Decoded tool output. Ordinary tabs, newlines, carriage
            returns, Unicode, and ANSI terminal escapes remain text.

    Returns:
        bool: True for any NUL byte or when other control characters exceed
        five percent of ANSI-stripped content (with an eight-character floor).

    This conservative trust-boundary check is deterministic and has no side
    effects. It prevents destructive compression of binary diagnostics.
    """
    if "\x00" in text or "\ufffd" in text:
        return True
    visible = ANSI_RE.sub("", text)
    controls = sum(1 for char in visible if ord(char) < 32 and char not in "\t\n\r")
    return controls > max(8, len(visible) // 20)


def classify(text):
    """Classify decoded tool output for deterministic compression policy.

    Args:
        text (str): Complete decoded stream or combined stream body.

    Returns:
        str: One of ``binary``, ``diff``, ``code``, ``json``, ``config``,
        ``error``, ``log``, or ``plain``. Binary detection runs first so later
        format heuristics can never authorize destructive transformation.
    """
    if _is_binary_text(text):
        return "binary"
    stripped = text.lstrip()
    if DIFF_RE.search(text):
        return "diff"
    if CODE_RE.search(text) and len(text.splitlines()) > 8:
        return "code"
    if stripped[:1] in "[{":
        try:
            json.loads(text)
            return "json"
        except (ValueError, TypeError):
            pass
    # Strong runtime failures outrank config-like `tool: message` lines such as
    # ripgrep's repeated `rg: path: IO error ...` stderr.
    if STRONG_ERROR_RE.search(text):
        return "error"
    if len(CONFIG_RE.findall(text)) >= 3 and len(text.splitlines()) > 4:
        return "config"
    if ERROR_RE.search(text):
        return "error"
    lines = text.splitlines()
    if len(lines) > 20:
        repeated = len(lines) - len(set(lines))
        if repeated >= max(3, len(lines) // 10) or any(re.match(r"^\s*\d{2}:\d{2}", line) for line in lines[:30]):
            return "log"
    return "plain"


def compress(text, category):
    """Apply the deterministic transform selected for a content category.

    Args:
        text (str): Complete decoded output to transform.
        category (str): Result from :func:`classify`.

    Returns:
        tuple[str, str]: Strategy label and candidate output. Binary and
        unsupported categories return exact passthrough content.

    Compression is local and side-effect free; callers remain responsible for
    size comparison and recoverable raw spooling before replacement.
    """
    if category == "binary":
        return "passthrough", text
    if category == "json":
        return "lite-json", lite_json(text)
    if category == "log":
        lite = lite_log(text)
        if len(lite) > 8000:
            return "cca-log", cca_rank(lite)
        return "lite-log", lite
    if category in ("error", "plain"):
        return "cca-" + category, cca_rank(text)
    return "passthrough", text


def bound_candidate(text, max_chars=None):
    """Keep replacement output below the hook's inline spill threshold."""
    if max_chars is None:
        max_chars = max(256, int(os.environ.get("TOKENPIPE_MAX_SHOWN_CHARS", "7000")))
    if len(text) <= max_chars:
        return text
    marker_template = "\n...[tokenpipe bounded output; omitted %d chars; use raw_ref]...\n"
    # The omitted count affects marker width. A short fixed-point loop converges
    # even when the number crosses a decimal digit boundary.
    omitted = max(0, len(text) - max_chars)
    for _ in range(4):
        marker = marker_template % omitted
        available = max(2, max_chars - len(marker))
        head = max(1, int(available * 0.60))
        tail = max(1, available - head)
        omitted = max(0, len(text) - head - tail)
    marker = marker_template % omitted
    overflow = max(0, head + len(marker) + tail - max_chars)
    head = max(1, head - overflow)
    return text[:head] + marker + text[-tail:]


def command_category(payload):
    # Never persist full command or arguments. Only coarse allowlisted categories.
    supplied = str(payload.get("command_category") or "").strip().lower()
    allowed = {
        "git", "test", "python", "node", "rust", "go", "docker", "search",
        "filesystem", "build", "lint", "shell-other", "unknown",
    }
    if supplied in allowed:
        return supplied
    command = str(payload.get("command") or "").strip().lower()
    first = command.split(None, 1)[0] if command else ""
    mapping = {
        "git": "git", "pytest": "test", "python": "python", "python3": "python",
        "npm": "node", "pnpm": "node", "yarn": "node", "npx": "node",
        "cargo": "rust", "go": "go", "docker": "docker", "rg": "search",
        "grep": "search", "find": "search", "ls": "filesystem",
    }
    if first in mapping:
        return mapping[first]
    tool = str(payload.get("tool_name") or "unknown").lower()
    if tool in ("bash", "shell", "exec", "exec_command"):
        return "shell-other"
    return re.sub(r"[^a-z0-9_.-]", "_", tool)[:40] or "unknown"


def _extract_output(payload):
    for key in ("output", "tool_output", "result"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def _append_metric(metric, path=None):
    """Append one privacy-bounded metric to a secure rotating JSONL file.

    Args:
        metric (dict[str, object]): Coarse counters and labels that contain no
            command arguments, prompts, or raw tool output.
        path (str | None): Optional metrics path; defaults to private user state.

    Returns:
        None: The row is appended and the descriptor is closed before return.

    Raises:
        OSError: Secure traversal, locking, rotation, or writing fails.
        ValueError: The target is not a single-link user-owned regular file.

    Parent and final symlinks are refused with no chmod, append, or truncation.
    Rotation and append occur under one advisory lock. Callers intentionally
    suppress failures because metrics must never affect tool output.
    """
    path = path or _metrics_path()
    parent = os.path.dirname(path)
    name = os.path.basename(path)
    if not name:
        raise ValueError("metrics path has no file name")
    parent_fd = _open_private_dir(parent, create=True)
    encoded = (json.dumps(metric, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    max_bytes = max(4096, int(os.environ.get("TOKENPIPE_METRICS_MAX_BYTES", str(8 * 1024 * 1024))))
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1):
                raise ValueError("metrics target is not a private regular file")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            size = os.fstat(fd).st_size
            if size + len(encoded) > max_bytes:
                # Keep complete recent JSONL rows. Rotation happens under the
                # same lock and never copies private content elsewhere.
                os.lseek(fd, max(0, size - max_bytes // 2), os.SEEK_SET)
                tail = os.read(fd, max_bytes // 2)
                newline = tail.find(b"\n")
                tail = tail[newline + 1:] if newline >= 0 else b""
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                _write_all(fd, tail)
            _write_all(fd, encoded)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
    finally:
        os.close(parent_fd)


def process(payload, mode=None, cleanup=True, record_metric=True):
    """Process one tool-output payload with recoverable fail-open semantics.

    Args:
        payload (dict[str, object]): Bounded hook payload containing output and
            optional lifecycle/category metadata. It is not mutated.
        mode (str | None): Explicit ``audit``, ``safe``, or ``full`` override.
        cleanup (bool): When true, enforce spool retention immediately. Claude
            multi-stream callers pass false and finalize all refs together.
        record_metric (bool): Append the decision metric immediately. A false
            value defers its bounded metric in the private ``_metric`` result
            field so a multi-stream caller can commit it only after recovery.

    Returns:
        dict[str, object]: Compression decision, shown output, recovery path,
        estimates, and bounded diagnostic metadata.

    Compressor, metric, and spool failures fail open to the exact original
    output. With immediate cleanup, replacement is returned only after the raw
    file survives cleanup and reads back byte-for-byte. Deferred multi-stream
    callers must perform that transaction before exposing the result or metric.
    Metrics never change the decision.
    """
    started = time.time()
    requested_mode = mode or payload.get("mode") or os.environ.get("TOKENPIPE_MODE") or configured_mode()
    mode = requested_mode if requested_mode in ("audit", "safe", "full") else "audit"
    original = _extract_output(payload)
    original_est = estimate_tokens(original)
    threshold = max(1, int(os.environ.get("TOKENPIPE_MIN_TOKENS_ESTIMATE", "1500")))
    category = classify(original)
    strategy = "passthrough"
    candidate = original
    skip_reason = None
    compressor_error = None
    raw_ref = None

    try:
        if original_est < threshold:
            skip_reason = "below-threshold"
        elif category in ("binary", "code", "diff"):
            skip_reason = category + "-passthrough"
        else:
            strategy, candidate = compress(original, category)
            candidate = bound_candidate(candidate)
            if estimate_tokens(candidate) >= original_est:
                candidate = original
                strategy = "passthrough"
                skip_reason = "no-savings"
    except Exception as exc:  # fail open: tool output must survive compressor faults
        candidate = original
        strategy = "passthrough"
        skip_reason = "compressor-error"
        compressor_error = type(exc).__name__

    candidate_est = estimate_tokens(candidate)
    replace = mode != "audit" and candidate != original
    if replace and mode in ("safe", "full"):
        # A caller-provided allowlist restricts which content categories may be
        # replaced; the counterfactual candidate still feeds honest estimates.
        replace_categories = payload.get("replace_categories")
        if isinstance(replace_categories, list) and category not in replace_categories:
            replace = False
            skip_reason = "category-gated"
    if replace:
        try:
            raw_ref = spool_raw(
                original,
                payload.get("session_id"),
                payload.get("tool_call_id"),
            )
            if cleanup:
                if not cleanup_spool(protected=(raw_ref,)):
                    raise OSError(errno.ENOSPC, "raw output exceeds configured spool cap")
                if show_raw(raw_ref) != original:
                    raise OSError(errno.EIO, "raw output failed recovery validation")
        except Exception as exc:  # no recoverable raw means no destructive compression
            if raw_ref:
                try:
                    os.unlink(raw_ref)
                except OSError:
                    pass
                raw_ref = None
            compressor_error = "raw-spool-" + type(exc).__name__
            skip_reason = "raw-spool-error"
            replace = False
            candidate = original

    shown = candidate if replace else original
    shown_est = estimate_tokens(shown)
    saved = 0.0 if not original_est else 100.0 * (original_est - candidate_est) / original_est
    now = _dt.datetime.now(_dt.timezone.utc)
    metric = {
        "timestamp": now.isoformat(),
        "day": now.date().isoformat(),
        "session": _safe_component(payload.get("session_id"), "unknown-session"),
        "tool": _safe_component(payload.get("tool_name"), "unknown"),
        "command_category": command_category(payload),
        "strategy": strategy,
        "content_category": category,
        "plugin_version": plugin_version(),
        "mode": mode,
        "original_bytes": len(original.encode("utf-8", "replace")),
        "shown_bytes": len(shown.encode("utf-8", "replace")),
        "counterfactual_bytes": len(candidate.encode("utf-8", "replace")),
        "original_tokens_estimate": original_est,
        "shown_tokens_estimate": shown_est,
        "counterfactual_tokens_estimate": candidate_est,
        "saved_percent": round(saved, 2),
        "latency_ms": round((time.time() - started) * 1000.0, 3),
        "exit_status": payload.get("exit_status") if isinstance(payload.get("exit_status"), int) else None,
        "skip_reason": skip_reason,
        "raw_ref_present": bool(raw_ref),
        "compressor_error": compressor_error,
        "audit_overflow": mode == "audit" and len(original) > max(
            256, int(os.environ.get("TOKENPIPE_MAX_SHOWN_CHARS", "7000"))
        ),
        "rtk_used": False,
    }
    if record_metric:
        try:
            _append_metric(metric)
        except Exception:
            # Telemetry must never alter tool execution.
            pass
    result = {
        "ok": True,
        "action": "replace" if replace else "passthrough",
        "output": shown,
        "mode": mode,
        "strategy": strategy,
        "content_category": category,
        "raw_ref": raw_ref,
        "original_tokens_estimate": original_est,
        "shown_tokens_estimate": shown_est,
        "counterfactual_tokens_estimate": candidate_est,
        "saved_percent": round(saved, 2),
        "skip_reason": skip_reason,
        "compressor_error": compressor_error,
    }
    if not record_metric:
        result["_metric"] = metric
    return result


SAFE_EXEC_CATEGORIES = frozenset(("git-read", "search", "filesystem-read", "docker-read"))
FULL_EXEC_CATEGORIES = SAFE_EXEC_CATEGORIES | frozenset(("test", "build", "lint"))
# Explicit installed-entry roots. Tests may replace this immutable set in
# process; untrusted child environments cannot extend it.
_TRUSTED_EXECUTABLE_DIRS = frozenset((
    "/bin", "/usr/bin", "/usr/sbin", "/sbin", "/usr/local/bin", "/opt/homebrew/bin",
))
NATIVE_MARKER = "tokenpipe-native-v1"
_INTERACTIVE_FLAGS = frozenset((
    "-i", "-w", "--interactive", "--watch", "--watchall", "--watch-all",
    "--follow", "--open", "--ui", "--pdb", "--trace", "--sw", "--paginate",
))
_MUTATING_FLAGS = frozenset(("--fix", "--fix-only", "--write"))
_FIND_MUTATING = frozenset((
    "-delete", "-exec", "-execdir", "-ok", "-okdir",
    "-fprint", "-fprint0", "-fprintf", "-fls",
))


def _normalize_exec_category(value):
    aliases = {
        "git": "git-read", "filesystem": "filesystem-read", "docker": "docker-read",
        "tests": "test", "testing": "test",
    }
    value = str(value or "unknown").strip().lower()
    return aliases.get(value, value)


def _argv_category(argv):
    if not argv:
        return "unknown"
    head = os.path.basename(argv[0]).lower()
    args = [str(item).lower() for item in argv[1:]]
    if head == "git" and args and args[0] in ("status", "diff", "log", "show"):
        return "git-read"
    if head == "rg" and not any(item == "--pre" or item.startswith("--pre=") for item in args):
        return "search"
    if head == "find" and not any(item in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for item in args):
        return "search"
    if head == "ls":
        return "filesystem-read"
    if head == "docker" and args and args[0] in ("ps", "logs") and "-f" not in args and "--follow" not in args:
        return "docker-read"
    if head in ("pytest", "py.test", "jest", "vitest"):
        return "test"
    if head == "cargo" and args:
        return {"test": "test", "check": "lint", "clippy": "lint", "build": "build"}.get(args[0], "unknown")
    if head == "go" and args:
        return {"test": "test", "vet": "lint", "build": "build"}.get(args[0], "unknown")
    if head in ("ruff", "eslint", "mypy", "pyright", "tsc"):
        return "lint"
    if head in ("npm", "pnpm", "yarn") and args:
        action = args[1] if args[0] == "run" and len(args) > 1 else args[0]
        return {"test": "test", "lint": "lint", "typecheck": "lint", "check": "lint", "build": "build"}.get(action, "unknown")
    return "unknown"


def _strict_argv_category(argv):
    """Authorize one direct argv vector for the native wrapper.

    Args:
        argv (Sequence[str]): Candidate executable and arguments. Values are
            inspected only; the sequence is not mutated.

    Returns:
        str: Coarse category when every command-specific flag is read-only, or
        ``unknown`` when interactive, mutating, output-writing, or Git helper
        configuration could broaden execution.

    This authoritative check intentionally duplicates the hook's conservative
    policy because the wrapper is directly invocable. It performs no I/O and
    never relies on PreToolUse as its only security boundary.
    """
    category = _argv_category(argv)
    if category == "unknown" or not argv:
        return "unknown"
    args = [str(item).lower() for item in argv[1:]]
    if any(
        item in _INTERACTIVE_FLAGS
        or item.startswith("--watch=")
        or item.startswith("--follow=")
        for item in args
    ):
        return "unknown"
    if any(
        item in _MUTATING_FLAGS
        or item == "--output"
        or item.startswith("--output=")
        for item in args
    ):
        return "unknown"
    head = os.path.basename(str(argv[0])).lower()
    if head == "git":
        forbidden_git = {
            "--ext-diff", "--textconv", "--config-env", "--exec-path",
            "--git-dir", "--work-tree", "--namespace", "--no-index",
        }
        if any(
            item in forbidden_git
            or any(item.startswith(prefix + "=") for prefix in forbidden_git)
            for item in args
        ):
            return "unknown"
    if head == "find" and any(item in _FIND_MUTATING for item in args):
        return "unknown"
    if head == "docker" and any(item in ("-f", "--follow") for item in args):
        return "unknown"
    return category


def _resolve_trusted_executable(value):
    """Resolve argv[0] only from trusted installed executable directories.

    Args:
        value (str | os.PathLike | None): Supplied executable name or absolute
            path from validated argv.

    Returns:
        str | None: Canonical executable path, or None when PATH resolution,
        containment, type, ownership, permissions, or executability is unsafe.

    Project/cwd/temp and ad-hoc home PATH entries are rejected even when their
    files are user-owned ``0755``. System, Homebrew, ``/usr/local`` and a
    non-project interpreter installation bin are explicit entry roots. The
    resolved file and ancestors must be root/user-owned and not world-writable.
    """
    value = str(value or "")
    if not value:
        return None
    basename = os.path.basename(value)
    located = shutil.which(basename)
    if not located:
        return None
    located = os.path.abspath(located)
    resolved = os.path.realpath(located)
    if os.sep in value:
        supplied = os.path.realpath(os.path.abspath(value))
        if supplied != resolved:
            return None
    cwd = os.path.realpath(os.getcwd())
    temp_root = os.path.realpath(tempfile.gettempdir())
    home_root = os.path.realpath(os.path.expanduser("~"))
    interpreter_bin = os.path.dirname(os.path.realpath(sys.executable))
    trusted_entries = {os.path.realpath(path) for path in _TRUSTED_EXECUTABLE_DIRS}
    if not any(
        os.path.commonpath((interpreter_bin, root)) == root
        for root in (cwd, temp_root, home_root)
    ):
        trusted_entries.add(interpreter_bin)
    if os.path.realpath(os.path.dirname(located)) not in trusted_entries:
        return None
    try:
        info = os.stat(resolved)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    if info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
        return None
    ancestor = os.path.dirname(resolved)
    while ancestor and ancestor != os.path.dirname(ancestor):
        try:
            parent_info = os.stat(ancestor)
        except OSError:
            return None
        sticky = bool(parent_info.st_mode & stat.S_ISVTX)
        if (not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid not in (0, os.getuid())
                or (parent_info.st_mode & 0o002 and not sticky)):
            return None
        ancestor = os.path.dirname(ancestor)
    return resolved if os.access(resolved, os.X_OK) else None


def _capture_limit():
    try:
        value = int(os.environ.get("TOKENPIPE_CAPTURE_MAX_BYTES", str(64 * 1024 * 1024)))
    except ValueError:
        value = 64 * 1024 * 1024
    return max(1024 * 1024, min(value, 1024 * 1024 * 1024))


def _terminate_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except OSError:
        try:
            process.send_signal(signum)
        except OSError:
            pass


def _run_captured(argv, child_env=None):
    """Run one child with disk-backed bounded capture and signal forwarding."""
    capture_home = _runtime_home()
    _mkdir_private(capture_home)
    limit = _capture_limit()
    overflow_event = threading.Event()
    capture_lock = threading.Lock()
    captured = {"bytes": 0}
    with tempfile.TemporaryFile(dir=capture_home) as stdout_file, tempfile.TemporaryFile(dir=capture_home) as stderr_file:
        process = subprocess.Popen(
            argv, shell=False, cwd=None,
            env=(os.environ if child_env is None else child_env).copy(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        previous = {}
        received = {"signal": None, "deadline": None}

        def drain(source, target):
            while True:
                chunk = source.read(65536)
                if not chunk:
                    break
                with capture_lock:
                    remaining = max(0, limit - captured["bytes"])
                    kept = chunk[:remaining]
                    if kept:
                        _write_all(target.fileno(), kept)
                        captured["bytes"] += len(kept)
                    if len(kept) != len(chunk):
                        overflow_event.set()

        readers = [
            threading.Thread(target=drain, args=(process.stdout, stdout_file), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, stderr_file), daemon=True),
        ]
        for reader in readers:
            reader.start()

        def forward(signum, _frame):
            if received["signal"] is None:
                received["signal"] = signum
                received["deadline"] = time.monotonic() + 1.0
            _terminate_group(process, signum)

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, forward)
        try:
            while process.poll() is None:
                if received["signal"] is not None and time.monotonic() >= received["deadline"]:
                    _terminate_group(process, signal.SIGKILL)
                    break
                if overflow_event.is_set():
                    _terminate_group(process, signal.SIGTERM)
                    try:
                        process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        _terminate_group(process, signal.SIGKILL)
                    break
                time.sleep(0.025)
            returncode = process.wait()
            for reader in readers:
                reader.join(timeout=2.0)
            process.stdout.close()
            process.stderr.close()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(limit + 1)
        stderr = stderr_file.read(limit + 1)
    if received["signal"] is not None:
        returncode = 128 + received["signal"]
        stderr += b"\ntokenpipe: child interrupted and reaped after signal %d\n" % received["signal"]
    return returncode, stdout, stderr, overflow_event.is_set()


def trusted_rtk_path(path):
    if not path or not os.path.isabs(path):
        return False
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid in (0, os.getuid())
        and not (info.st_mode & 0o022)
        and os.access(path, os.X_OK)
    )


def configured_rtk():
    settings = configured_settings()
    path = settings.get("rtk_bin")
    enabled = settings.get("use_rtk") is True
    return (path if isinstance(path, str) else None), enabled


def set_configured_rtk(path):
    settings = configured_settings()
    if path is None:
        settings["use_rtk"] = False
        settings.pop("rtk_bin", None)
    else:
        path = os.path.abspath(os.path.expanduser(path))
        if not trusted_rtk_path(path):
            raise ValueError("RTK path must be an absolute trusted executable")
        settings["use_rtk"] = True
        settings["rtk_bin"] = path
    _write_settings(settings)
    return configured_rtk()


def _native_header(category, exit_status, mode, strategy, raw_ref=None):
    fields = [
        NATIVE_MARKER,
        "category=" + _safe_component(category, "unknown"),
        "exit=%d" % exit_status,
        "mode=" + mode,
        "strategy=" + _safe_component(strategy, "passthrough"),
    ]
    if raw_ref:
        fields.append("raw_ref=" + raw_ref)
    return " ".join(fields) + "\n"


def _label_streams(stdout, stderr):
    return "--- stdout ---\n" + stdout + "\n--- stderr ---\n" + stderr


def _metric_base(payload, mode, category, strategy, content_category, original, shown,
                 counterfactual, exit_status, skip_reason, raw_ref, compressor_error,
                 started, native_header_bytes=0, audit_overflow=False, rtk_used=False):
    now = _dt.datetime.now(_dt.timezone.utc)
    original_est = estimate_tokens(original)
    shown_est = estimate_tokens(shown)
    counter_est = estimate_tokens(counterfactual)
    saved = 0.0 if not original_est else 100.0 * (original_est - counter_est) / original_est
    return {
        "timestamp": now.isoformat(), "day": now.date().isoformat(),
        "session": _safe_component(payload.get("session_id"), "unknown-session"),
        "tool": _safe_component(payload.get("tool_name"), "exec_command"),
        "command_category": category, "strategy": strategy,
        "content_category": content_category, "plugin_version": plugin_version(), "mode": mode,
        "original_bytes": len(original.encode("utf-8", "replace")),
        "shown_bytes": len(shown.encode("utf-8", "replace")),
        "counterfactual_bytes": len(counterfactual.encode("utf-8", "replace")),
        "native_header_bytes": native_header_bytes,
        "original_tokens_estimate": original_est,
        "shown_tokens_estimate": shown_est,
        "counterfactual_tokens_estimate": counter_est,
        "saved_percent": round(saved, 2),
        "latency_ms": round((time.time() - started) * 1000.0, 3),
        "exit_status": exit_status, "skip_reason": skip_reason,
        "raw_ref_present": bool(raw_ref), "compressor_error": compressor_error,
        "audit_overflow": bool(audit_overflow),
        "rtk_used": bool(rtk_used),
    }


def execute_native(argv, category, mode=None, session_id=None, tool_call_id=None,
                   use_rtk=None, exec_fallback=False):
    """Execute validated argv without a shell and render one tool-role result.

    Args:
        argv (Sequence[str]): Direct command vector; argv[0] must resolve from a
            trusted installed directory and match the supplied category.
        category (str): Coarse expected command category from the hook.
        mode (str | None): Persisted mode override.
        session_id (str | None): Sanitized only for metrics/raw file names.
        tool_call_id (str | None): Sanitized only for metrics/raw file names.
        use_rtk (bool | None): Optional explicit RTK routing decision.
        exec_fallback (bool): Replace the wrapper with safe validated argv when
            capture storage is unavailable.

    Returns:
        tuple[str, int]: Model-visible output and normalized child exit status.

    Git reads receive a sanitized config/environment that disables hooks,
    fsmonitor, pagers, external diff, and textconv. Capture/compressor/metric
    failures preserve execution or fail open without broadening argv authority.
    """
    started = time.time()
    mode = mode or configured_mode()
    mode = mode if mode in ("audit", "safe", "full") else "audit"
    supplied_category = _normalize_exec_category(category)
    derived_category = _strict_argv_category(argv)
    resolved_executable = _resolve_trusted_executable(argv[0] if argv else None)
    category_ok = supplied_category == derived_category and supplied_category in FULL_EXEC_CATEGORIES
    allowed_for_mode = supplied_category in (
        SAFE_EXEC_CATEGORIES if mode == "safe" else FULL_EXEC_CATEGORIES
    )
    if mode not in ("safe", "full") or not category_ok or not allowed_for_mode or not resolved_executable:
        reason = (
            "mode-does-not-execute" if mode not in ("safe", "full")
            else "untrusted-executable" if not resolved_executable
            else "category-command-mismatch" if not category_ok
            else "category-not-allowed-in-mode"
        )
        payload = {
            "session_id": session_id, "tool_call_id": tool_call_id,
            "tool_name": "exec_command", "command_category": supplied_category,
        }
        output = _native_header(supplied_category, 126, mode, "refused")
        output += _label_streams("", "tokenpipe refused wrapper execution: " + reason)
        try:
            _append_metric(_metric_base(
                payload, mode, supplied_category, "refused", "unknown",
                output, output, output, 126, reason, None, None, started,
                len(output.encode("utf-8", "replace")),
            ))
        except Exception:
            pass
        return output, 126
    original_command_argv = [resolved_executable] + list(argv[1:])
    child_env = os.environ.copy()
    if supplied_category == "git-read":
        subcommand = str(argv[1]).lower()
        if subcommand in ("diff", "log", "show"):
            original_command_argv[2:2] = ["--no-ext-diff", "--no-textconv"]
        for key in list(child_env):
            if key.startswith("GIT_CONFIG_") or key in {
                "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_EXEC_PATH", "GIT_EXTERNAL_DIFF",
            }:
                child_env.pop(key, None)
        child_env.update({
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.devnull,
            "GIT_CONFIG_KEY_1": "core.fsmonitor",
            "GIT_CONFIG_VALUE_1": "false",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
        })
    command_argv = list(original_command_argv)
    rtk_path, persisted_rtk_enabled = configured_rtk()
    want_rtk = use_rtk if use_rtk is not None else persisted_rtk_enabled
    rtk_used = bool(want_rtk and category_ok and trusted_rtk_path(rtk_path))
    if rtk_used:
        command_argv = [rtk_path, os.path.basename(str(argv[0]))] + command_argv[1:]
    try:
        if rtk_used:
            child_env.setdefault("RTK_DB_PATH", os.path.join(_runtime_home(), "rtk-history.db"))
        returncode, stdout_bytes, stderr_bytes, capture_overflow = _run_captured(
            command_argv, child_env
        )
        exit_status = int(returncode)
        stdout = stdout_bytes.decode("utf-8", "replace")
        stderr = stderr_bytes.decode("utf-8", "replace")
        if capture_overflow:
            exit_status = 125
            stderr += (
                "\ntokenpipe: child terminated after captured output exceeded "
                "%d bytes\n" % _capture_limit()
            )
    except (PermissionError, OSError) as exc:
        if exec_fallback:
            os.execvpe(original_command_argv[0], original_command_argv, child_env)
        exit_status = 127
        stdout = ""
        stderr = "%s: %s" % (type(exc).__name__, exc)
    body = _label_streams(stdout, stderr)
    # Classify child content without our stream labels; `--- stderr ---` would
    # otherwise resemble a unified diff header.
    content_category = classify(stdout + "\n" + stderr)
    strategy = "rtk-direct" if rtk_used else "passthrough"
    candidate = body
    skip_reason = None
    compressor_error = None
    raw_ref = None
    try:
        if capture_overflow:
            strategy = "capture-overflow"
            skip_reason = "capture-overflow"
            candidate = bound_candidate(body)
        elif content_category in ("binary", "code", "diff", "config"):
            skip_reason = content_category + "-passthrough"
        elif estimate_tokens(body) < max(1, int(os.environ.get("TOKENPIPE_MIN_TOKENS_ESTIMATE", "1500"))):
            skip_reason = "below-threshold"
        else:
            if content_category == "json" and not stderr.strip():
                compressed_strategy, compressed_stdout = compress(stdout, content_category)
                candidate = _label_streams(compressed_stdout, stderr)
            else:
                compressed_strategy, candidate = compress(body, content_category)
            strategy = ("rtk-direct+" if rtk_used else "") + compressed_strategy
            candidate = bound_candidate(candidate)
            if estimate_tokens(candidate) >= estimate_tokens(body):
                candidate = body
                strategy = "rtk-direct" if rtk_used else "passthrough"
                skip_reason = "no-savings"
    except Exception as exc:
        candidate = body
        strategy = "rtk-direct" if rtk_used else "passthrough"
        skip_reason = "compressor-error"
        compressor_error = type(exc).__name__
    replace = mode in ("safe", "full") and candidate != body
    payload = {
        "session_id": session_id, "tool_call_id": tool_call_id,
        "tool_name": "exec_command", "command_category": supplied_category,
    }
    if replace:
        try:
            runtime_raw = _runtime_raw_root()
            raw_ref = spool_raw(body, session_id, tool_call_id, runtime_raw)
            if not cleanup_spool(root=runtime_raw, protected=(raw_ref,)):
                raise OSError(errno.ENOSPC, "raw output exceeds configured spool cap")
            if show_raw(raw_ref) != body:
                raise OSError(errno.EIO, "raw output failed recovery validation")
        except Exception as exc:
            if raw_ref:
                try:
                    os.unlink(raw_ref)
                except OSError:
                    pass
            candidate = body
            replace = False
            raw_ref = None
            skip_reason = "raw-spool-error"
            compressor_error = "raw-spool-" + type(exc).__name__
    shown_body = candidate if replace else body
    shown_header = _native_header(supplied_category, exit_status, mode, strategy, raw_ref)
    shown = shown_header + shown_body
    original_header = _native_header(supplied_category, exit_status, mode, "passthrough", None)
    original_native = original_header + body
    counter_header = _native_header(supplied_category, exit_status, mode, strategy, "available-on-compression" if candidate != body else None)
    counter_native = counter_header + candidate
    metric = _metric_base(
        payload, mode, supplied_category, strategy, content_category,
        original_native, shown, counter_native, exit_status, skip_reason, raw_ref,
        compressor_error, started, len(shown_header.encode("utf-8", "replace")),
        mode == "audit" and len(shown) > max(256, int(os.environ.get("TOKENPIPE_MAX_SHOWN_CHARS", "7000"))),
        rtk_used,
    )
    try:
        _append_metric(metric)
    except Exception:
        try:
            _append_metric(metric, _runtime_metrics_path())
        except Exception:
            pass
    return shown, (exit_status if exit_status >= 0 else 128 + abs(exit_status))


def record_skip(category, reason, mode=None, session_id=None, tool_call_id=None):
    started = time.time()
    mode = mode or os.environ.get("TOKENPIPE_MODE") or configured_mode()
    mode = mode if mode in ("audit", "safe", "full") else "audit"
    category = _normalize_exec_category(category)
    payload = {"session_id": session_id, "tool_call_id": tool_call_id, "tool_name": "exec_command"}
    metric = _metric_base(
        payload, mode, category, "passthrough", "unknown", "", "", "", None,
        _safe_component(reason, "skipped"), None, None, started,
        audit_overflow=reason == "audit-output-overflow",
    )
    _append_metric(metric)


def _parse_since(value):
    if not value:
        return None
    match = re.match(r"^(\d+)([hdw])$", value)
    now = _dt.datetime.now(_dt.timezone.utc)
    if match:
        amount = int(match.group(1))
        units = {"h": 3600, "d": 86400, "w": 604800}[match.group(2)]
        return now - _dt.timedelta(seconds=amount * units)
    parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


def load_metrics(since=None, session=None):
    rows = []
    for metrics_path in (_metrics_path(), _runtime_metrics_path()):
        try:
            handle = open(metrics_path, "r", encoding="utf-8")
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                continue
            raise
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    stamp = _dt.datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                except (ValueError, KeyError, TypeError):
                    continue
                if since and stamp < since:
                    continue
                if session and row.get("session") != session:
                    continue
                rows.append(row)
    return rows


def aggregate(rows):
    groups = {
        "day": {}, "session": {}, "command_category": {},
        "strategy": {}, "content_category": {}, "skip_reason": {},
        "mode": {}, "plugin_version": {},
    }
    for dimension in groups:
        for row in rows:
            key = row.get(dimension) or "none"
            group = groups[dimension].setdefault(key, {
                "calls": 0, "original_tokens_estimate": 0,
                "shown_tokens_estimate": 0, "counterfactual_tokens_estimate": 0,
                "errors": 0,
                "rtk_calls": 0,
            })
            group["calls"] += 1
            group["original_tokens_estimate"] += int(row.get("original_tokens_estimate") or 0)
            group["shown_tokens_estimate"] += int(row.get("shown_tokens_estimate") or 0)
            group["counterfactual_tokens_estimate"] += int(row.get("counterfactual_tokens_estimate") or 0)
            group["errors"] += 1 if row.get("compressor_error") else 0
            group["rtk_calls"] += 1 if row.get("rtk_used") else 0
    original = sum(int(row.get("original_tokens_estimate") or 0) for row in rows)
    shown = sum(int(row.get("shown_tokens_estimate") or 0) for row in rows)
    counterfactual = sum(int(row.get("counterfactual_tokens_estimate") or 0) for row in rows)
    native_refused_rows = [
        row for row in rows
        if int(row.get("native_header_bytes") or 0) > 0 and row.get("strategy") == "refused"
    ]
    native_rows = [
        row for row in rows
        if int(row.get("native_header_bytes") or 0) > 0 and row.get("strategy") != "refused"
    ]
    native_original = sum(int(row.get("original_tokens_estimate") or 0) for row in native_rows)
    native_shown = sum(int(row.get("shown_tokens_estimate") or 0) for row in native_rows)
    return {
        "token_counts_are_estimates": True,
        "calls": len(rows),
        "audit_calls": sum(
            1 for row in rows
            if row.get("mode") == "audit" and int(row.get("native_header_bytes") or 0) == 0
        ),
        "native_calls": len(native_rows),
        "native_refused_calls": len(native_refused_rows),
        "rtk_owned_calls": sum(1 for row in native_rows if row.get("rtk_used")),
        "native_call_coverage_percent_estimate": round(100.0 * len(native_rows) / len(rows), 2) if rows else 0.0,
        "native_token_coverage_percent_estimate": round(100.0 * native_original / original, 2) if original else 0.0,
        "native_saved_percent_estimate": round(100.0 * (native_original - native_shown) / native_original, 2) if native_original else 0.0,
        "original_tokens_estimate": original,
        "shown_tokens_estimate": shown,
        "counterfactual_tokens_estimate": counterfactual,
        "actual_saved_percent_estimate": round(100.0 * (original - shown) / original, 2) if original else 0.0,
        "counterfactual_saved_percent_estimate": round(100.0 * (original - counterfactual) / original, 2) if original else 0.0,
        "groups": groups,
    }


def show_raw(path):
    requested_real = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    roots = [os.path.realpath(_raw_root()), os.path.realpath(_runtime_raw_root())]
    root = next((item for item in roots if requested_real.startswith(item + os.sep)), None)
    if root is None:
        raise ValueError("raw_ref is outside tokenpipe raw directories")
    requested = os.path.abspath(os.path.expanduser(path))
    candidate = os.path.join(os.path.realpath(os.path.dirname(requested)), os.path.basename(requested))
    # Lexical containment prevents an attacker from using `..`; O_NOFOLLOW and
    # fstat below close the final-component symlink swap window.
    if candidate != root and not candidate.startswith(root + os.sep):
        raise ValueError("raw_ref is outside tokenpipe raw directory")
    relative = os.path.relpath(candidate, root)
    parts = relative.split(os.sep)
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("invalid raw_ref")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    directory_fd = os.open(root, directory_flags)
    try:
        for component in parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        fd = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("raw_ref is not a regular file")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    post = commands.add_parser("post", help="process one JSON tool-result payload from stdin")
    post.add_argument("--mode", choices=("audit", "safe", "full"))
    stats = commands.add_parser("stats", help="aggregate private metrics")
    stats.add_argument("--json", action="store_true")
    stats.add_argument("--since", help="ISO timestamp/date or duration such as 24h, 7d, 4w")
    stats.add_argument("--session")
    show = commands.add_parser("show", help="print a recoverable raw output")
    show.add_argument("raw_ref")
    mode_cmd = commands.add_parser("mode", help="print or persist audit/safe/full mode")
    mode_cmd.add_argument("value", nargs="?", choices=("audit", "safe", "full"))
    rtk_cmd = commands.add_parser("rtk", help="show, enable, or disable trusted RTK integration")
    rtk_cmd.add_argument("value", nargs="?", help="absolute RTK executable path, or 'off'")
    native = commands.add_parser("exec", help="execute direct argv and emit native compressed output")
    native.add_argument("--category", required=True)
    native.add_argument("--session-id")
    native.add_argument("--tool-call-id")
    native.add_argument("exec_argv", nargs=argparse.REMAINDER)
    skip = commands.add_parser("skip", help="record an output-free audit skip entry")
    skip.add_argument("--category", required=True)
    skip.add_argument("--reason", required=True)
    skip.add_argument("--mode", choices=("audit", "safe", "full"))
    skip.add_argument("--session-id")
    skip.add_argument("--tool-call-id")
    args = parser.parse_args(argv)
    if args.command == "post":
        try:
            payload = json.load(sys.stdin)
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
            result = process(payload, args.mode or "audit")
        except Exception as exc:
            result = {
                "ok": False, "action": "passthrough", "output": "", "mode": args.mode or "audit",
                "strategy": "passthrough", "raw_ref": None, "compressor_error": type(exc).__name__,
                "skip_reason": "invalid-input",
            }
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    if args.command == "stats":
        report = aggregate(load_metrics(_parse_since(args.since), args.session))
        if args.json:
            json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
            sys.stdout.write("\n")
        else:
            print("Token counts are estimates (UTF-8 bytes / 3.5), not provider usage.")
            print("Calls: %d" % report["calls"])
            print("Audit-only calls: %d" % report["audit_calls"])
            print("Native calls: %d (%.2f%% of calls, %.2f%% of estimated tokens)" % (
                report["native_calls"], report["native_call_coverage_percent_estimate"],
                report["native_token_coverage_percent_estimate"],
            ))
            print("Native refused calls: %d" % report["native_refused_calls"])
            print("RTK-owned native calls: %d" % report["rtk_owned_calls"])
            print("Native tokenpipe saved: %.2f%%" % report["native_saved_percent_estimate"])
            print("Original: %d est. tokens" % report["original_tokens_estimate"])
            print("Shown: %d est. tokens" % report["shown_tokens_estimate"])
            print("Tokenpipe-owned saved: %.2f%%" % report["actual_saved_percent_estimate"])
            print("Counterfactual saved: %.2f%%" % report["counterfactual_saved_percent_estimate"])
            if report["rtk_owned_calls"]:
                print("RTK savings are external to these estimates; verify them with `rtk gain`.")
            for dimension in (
                "day", "session", "command_category", "strategy",
                "content_category", "skip_reason", "mode", "plugin_version",
            ):
                print("\n%s:" % dimension)
                for key, value in sorted(report["groups"][dimension].items()):
                    print("  %s: %d calls, %d -> %d est. tokens" % (
                        key, value["calls"], value["original_tokens_estimate"], value["shown_tokens_estimate"]
                    ))
        return 0
    if args.command == "show":
        try:
            sys.stdout.write(show_raw(args.raw_ref))
        except (OSError, ValueError) as exc:
            print("tokenpipe: %s" % exc, file=sys.stderr)
            return 2
        return 0
    if args.command == "mode":
        if args.value:
            set_configured_mode(args.value)
        print(configured_mode())
        return 0
    if args.command == "rtk":
        try:
            if args.value == "off":
                set_configured_rtk(None)
            elif args.value:
                set_configured_rtk(args.value)
            path, enabled = configured_rtk()
            print("enabled {}".format(path) if enabled and path else "disabled")
        except ValueError as exc:
            print("tokenpipe: {}".format(exc), file=sys.stderr)
            return 2
        return 0
    if args.command == "exec":
        child_argv = list(args.exec_argv)
        if child_argv[:1] == ["--"]:
            child_argv = child_argv[1:]
        if not child_argv:
            output = _native_header(args.category, 127, configured_mode(), "passthrough")
            output += _label_streams("", "tokenpipe: missing command argv")
            sys.stdout.write(output)
            return 127
        output, status_code = execute_native(
            child_argv, args.category, None, args.session_id, args.tool_call_id,
            exec_fallback=True,
        )
        sys.stdout.write(output)
        return status_code
    if args.command == "skip":
        try:
            record_skip(args.category, args.reason, args.mode, args.session_id, args.tool_call_id)
        except Exception:
            return 0
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
