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


VERSION = "0.1.0"
ANSI_RE = re.compile(r"\x1b(?:[@-_][0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
SECRET_KEY_RE = re.compile(r"(?i)(token|secret|password|authorization|api[_-]?key|cookie)")
ERROR_RE = re.compile(
    r"(?i)(error|failed|failure|fatal|panic|exception|traceback|assert|timeout|"
    r"segmentation|denied|not found|\b[45]\d\d\b)"
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


def _mkdir_private(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


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
    session = _safe_component(session_id, "unknown-session")
    call = _safe_component(tool_call_id, "call-" + uuid.uuid4().hex[:12])
    directory = os.path.join(root or _raw_root(), session)
    _mkdir_private(directory)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    data = text.encode("utf-8", "replace")
    for _ in range(32):
        path = os.path.join(directory, call + "-" + uuid.uuid4().hex[:12] + ".log")
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                continue
            raise
        try:
            os.fchmod(fd, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("raw spool target is not regular")
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        return path
    raise OSError(errno.EEXIST, "could not allocate unique raw spool file")


def cleanup_spool(now=None, root=None):
    now = time.time() if now is None else now
    ttl = max(0, int(os.environ.get("TOKENPIPE_RAW_TTL_SECONDS", str(7 * 86400))))
    max_bytes = max(0, int(os.environ.get("TOKENPIPE_RAW_MAX_BYTES", str(256 * 1024 * 1024))))
    root = root or _raw_root()
    if not os.path.isdir(root):
        return
    files = []
    for base, dirs, names in os.walk(root, topdown=True, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
        for name in names:
            path = os.path.join(base, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if ttl and now - info.st_mtime > ttl:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            else:
                files.append((info.st_mtime, info.st_size, path))
    total = sum(item[1] for item in files)
    for _, size, path in sorted(files):
        if total <= max_bytes:
            break
        try:
            os.unlink(path)
            total -= size
        except OSError:
            pass


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


def classify(text):
    stripped = text.lstrip()
    if DIFF_RE.search(text):
        return "diff"
    if CODE_RE.search(text) and len(text.splitlines()) > 8:
        return "code"
    if len(CONFIG_RE.findall(text)) >= 3 and len(text.splitlines()) > 4:
        return "config"
    if stripped[:1] in "[{":
        try:
            json.loads(text)
            return "json"
        except (ValueError, TypeError):
            pass
    if ERROR_RE.search(text):
        return "error"
    lines = text.splitlines()
    if len(lines) > 20:
        repeated = len(lines) - len(set(lines))
        if repeated >= max(3, len(lines) // 10) or any(re.match(r"^\s*\d{2}:\d{2}", line) for line in lines[:30]):
            return "log"
    return "plain"


def compress(text, category):
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
    path = path or _metrics_path()
    _mkdir_private(os.path.dirname(path))
    encoded = (json.dumps(metric, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    max_bytes = max(4096, int(os.environ.get("TOKENPIPE_METRICS_MAX_BYTES", str(8 * 1024 * 1024))))
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        if size + len(encoded) > max_bytes:
            # Keep complete recent JSONL rows. Rotation happens under the same
            # advisory lock and never copies prompt/tool contents elsewhere.
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


def process(payload, mode=None):
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
        elif category in ("code", "diff"):
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
    if replace:
        try:
            raw_ref = spool_raw(
                original,
                payload.get("session_id"),
                payload.get("tool_call_id"),
            )
            cleanup_spool()
        except Exception as exc:  # no recoverable raw means no destructive compression
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
    try:
        _append_metric(metric)
    except Exception:
        # Telemetry must never alter tool execution.
        pass
    return {
        "ok": True,
        "action": "replace" if replace else "passthrough",
        "output": shown,
        "mode": mode,
        "strategy": strategy,
        "raw_ref": raw_ref,
        "original_tokens_estimate": original_est,
        "shown_tokens_estimate": shown_est,
        "counterfactual_tokens_estimate": candidate_est,
        "saved_percent": round(saved, 2),
        "skip_reason": skip_reason,
        "compressor_error": compressor_error,
    }


SAFE_EXEC_CATEGORIES = frozenset(("git-read", "search", "filesystem-read", "docker-read"))
FULL_EXEC_CATEGORIES = SAFE_EXEC_CATEGORIES | frozenset(("test", "build", "lint"))
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
    """Return a category only for argv accepted by the native wrapper.

    This is authoritative and intentionally duplicates the hook's conservative
    policy. The wrapper is directly invocable, so it must never rely on the
    PreToolUse hook as its only security boundary.
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
    if head == "find" and any(item in _FIND_MUTATING for item in args):
        return "unknown"
    if head == "docker" and any(item in ("-f", "--follow") for item in args):
        return "unknown"
    return category


def _resolve_trusted_executable(value):
    """Resolve argv[0] through PATH and reject path-spoofed executables."""
    value = str(value or "")
    if not value:
        return None
    basename = os.path.basename(value)
    located = shutil.which(basename)
    if not located:
        return None
    resolved = os.path.realpath(located)
    if os.sep in value:
        supplied = os.path.realpath(os.path.abspath(value))
        if supplied != resolved:
            return None
    try:
        info = os.stat(resolved)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    if info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
        return None
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


def _run_captured(argv):
    """Run one child with disk-backed bounded capture and signal forwarding."""
    capture_home = _runtime_home()
    _mkdir_private(capture_home)
    limit = _capture_limit()
    overflow_event = threading.Event()
    capture_lock = threading.Lock()
    captured = {"bytes": 0}
    with tempfile.TemporaryFile(dir=capture_home) as stdout_file, tempfile.TemporaryFile(dir=capture_home) as stderr_file:
        process = subprocess.Popen(
            argv, shell=False, cwd=None, env=os.environ.copy(),
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
        "content_category": content_category, "mode": mode,
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
    """Execute direct argv without a shell and render one native tool-role result."""
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
    command_argv = list(original_command_argv)
    rtk_path, persisted_rtk_enabled = configured_rtk()
    want_rtk = use_rtk if use_rtk is not None else persisted_rtk_enabled
    rtk_used = bool(want_rtk and category_ok and trusted_rtk_path(rtk_path))
    if rtk_used:
        command_argv = [rtk_path] + command_argv
    try:
        returncode, stdout_bytes, stderr_bytes, capture_overflow = _run_captured(command_argv)
        exit_status = int(returncode)
        stdout = stdout_bytes.decode("utf-8", "replace")
        stderr = stderr_bytes.decode("utf-8", "replace")
        if capture_overflow:
            exit_status = 125
            stderr += (
                "\ntokenpipe: child terminated after captured output exceeded "
                "%d bytes\n" % _capture_limit()
            )
    except PermissionError as exc:
        if exec_fallback:
            os.execvpe(original_command_argv[0], original_command_argv, os.environ.copy())
        exit_status = 127
        stdout = ""
        stderr = "%s: %s" % (type(exc).__name__, exc)
    except OSError as exc:
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
        elif rtk_used:
            # RTK already owns filtering for this command. Do not stack Lite or
            # CCA on its output; RTK savings are reported by `rtk gain` while
            # tokenpipe records adoption and observed output size.
            skip_reason = "rtk-owned-output"
        elif content_category in ("code", "diff", "config"):
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
            cleanup_spool(root=runtime_raw)  # enforce cap including new file
            if not os.path.exists(raw_ref):
                raise OSError(errno.ENOSPC, "raw output exceeds configured spool cap")
        except Exception as exc:
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
    return {
        "token_counts_are_estimates": True,
        "calls": len(rows),
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
            # PostToolUse is observability-only. Safe/full replacement happens
            # exclusively inside `exec`, before the native tool result exists.
            result = process(payload, "audit")
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
            print("Original: %d est. tokens" % report["original_tokens_estimate"])
            print("Shown: %d est. tokens" % report["shown_tokens_estimate"])
            print("Actual saved: %.2f%%" % report["actual_saved_percent_estimate"])
            print("Counterfactual saved: %.2f%%" % report["counterfactual_saved_percent_estimate"])
            for dimension in (
                "day", "session", "command_category", "strategy",
                "content_category", "skip_reason",
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
