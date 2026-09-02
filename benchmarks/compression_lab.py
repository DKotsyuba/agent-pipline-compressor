#!/usr/bin/env python3
"""Deterministic local-stage and real-command compression benchmark."""

from __future__ import print_function

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKENPIPE_PATH = os.path.join(ROOT, "scripts", "tokenpipe.py")
SPEC = importlib.util.spec_from_file_location("tokenpipe", TOKENPIPE_PATH)
tokenpipe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tokenpipe)

LAB_VERSION = "2.0.0"
MAX_SELECTION_LATENCY_MS = 250.0
STAGES = ("ansi", "json-lite", "log-lite", "cca", "bound")
PARTIAL_ORDER = (("ansi", "json-lite"), ("ansi", "log-lite"), ("ansi", "cca"),
                 ("ansi", "bound"), ("json-lite", "bound"), ("log-lite", "cca"),
                 ("log-lite", "bound"), ("cca", "bound"))
NORMALIZATION_RULES = (
    "ANSI escape sequences are removed",
    "CRLF and bare CR are normalized to LF",
    "fixture-root absolute paths are replaced with <fixture-root>",
    "declared test durations are replaced with <duration>",
)


@dataclass(frozen=True)
class Case:
    case_id: str
    category: str
    route: str
    weight: int
    exit_code: int
    stdout: str
    stderr: str = ""
    must_keep: tuple = ()
    alternative_markers: tuple = ()
    secret_like: tuple = ()
    exact_policy: dict = None
    json_keys: tuple = ()
    json_expectations: tuple = ()

    def __post_init__(self):
        if self.exact_policy is None:
            object.__setattr__(self, "exact_policy", {"mode": "compressible"})

    @property
    def raw(self):
        return self.stdout + self.stderr

    def metadata(self, raw_hash):
        # This is not a redactor. Keep only non-sensitive telemetry.
        return {"id": self.case_id, "category": self.category, "route": self.route,
                "weight": self.weight, "exit_code": self.exit_code,
                "must_keep": list(self.must_keep), "secret_like_count": len(self.secret_like),
                "alternative_markers": [list(group) for group in self.alternative_markers],
                "secret_like_sha256": [sha256(item) for item in self.secret_like],
                "exact_policy": self.exact_policy, "json_keys": list(self.json_keys),
                "json_expectations": [dict(item) for item in self.json_expectations],
                "raw_sha256": raw_hash, "deterministic_hash": raw_hash}


@dataclass(frozen=True)
class CommandCase:
    case_id: str
    category: str
    route: str
    weight: int
    argv: tuple
    rtk_argv: tuple
    cwd: str
    exit_code: int
    must_keep: tuple = ()
    exact_policy: dict = None
    json_expectations: tuple = ()
    normalization_root: str = ""
    alternative_markers: tuple = ()

    def __post_init__(self):
        if self.exact_policy is None:
            object.__setattr__(self, "exact_policy", {"mode": "compressible"})


def _repeat(line, count):
    return line * count


def _varying_log(rounds=31):
    """Render a log that repeats ten templates but never a byte-identical line.

    Args:
        rounds (int): Number of passes over the templates. Each pass emits ten
            lines plus, every tenth pass, one ERROR line.

    Returns:
        str: Newline-terminated log text with monotonically increasing ISO
        timestamps and varying hex ids, uuids, durations, byte sizes, and
        percentages, so adjacent-repeat collapsing alone can save nothing.
        Three ERROR lines are included and must survive any transform.
    """
    lines = []
    for index in range(rounds):
        stamp = "2026-01-01T00:%02d:%02dZ" % (index // 60, index % 60)
        lines.append("%s INFO worker heartbeat interval 5s" % stamp)
        lines.append("%s INFO fetched object %012x in %dms" % (stamp, index * 7919, 3 + index % 40))
        lines.append("%s INFO cache hit ratio %d.%d%%" % (stamp, 90 + index % 9, index % 10))
        lines.append("%s DEBUG mapped segment 0x%08x size %d.%dMB" % (stamp, index * 4096, 1 + index % 7, index % 10))
        lines.append("%s INFO job 550e8400-e29b-41d4-a716-4466554400%02d accepted" % (stamp, index % 100))
        lines.append("%s DEBUG flushed buffer %dKiB in %d.%ds" % (stamp, 4 + index % 60, index % 3, index % 10))
        lines.append("%s INFO peer handshake %016x established" % (stamp, index * 104729))
        lines.append("%s DEBUG gc pause %dms heap %d.%dMB" % (stamp, index % 25, 100 + index % 40, index % 10))
        lines.append("%s INFO replicated shard %010x to node-3 in %dms" % (stamp, index * 31337, 5 + index % 20))
        lines.append("%s DEBUG queue depth 4 backlog %dms" % (stamp, index % 90))
        if index % 10 == 9:
            lines.append("%s ERROR upload rejected by node-3 status=503" % stamp)
    return "\n".join(lines) + "\n"


def _varying_log_streams(rounds=31):
    """Split :func:`_varying_log` across both captured streams.

    Args:
        rounds (int): Number of passes over the templates, as for
            :func:`_varying_log`.

    Returns:
        tuple[str, str]: Stdout text carrying the INFO lines and stderr text
        carrying the DEBUG and ERROR lines, so a fixture built from them
        exercises the stderr capture path instead of declaring ``stderr=""``.
    """
    out, err = [], []
    for line in _varying_log(rounds).splitlines():
        (err if (" DEBUG " in line or " ERROR " in line) else out).append(line)
    return "\n".join(out) + "\n", "\n".join(err) + "\n"


def corpus():
    """Synthetic text corpus; no executable command cases are in this list."""
    pytest_pass = "============================= test session starts =============================\ncollected 3 items\n\ntests/test_math.py ...                                             [100%]\n\n3 passed in 0.01s\n"
    pytest_fail = "============================= test session starts =============================\ncollected 3 items\n\ntests/test_math.py ..F                                             [100%]\n=================================== FAILURES ===================================\nFAILED tests/test_math.py::test_total - AssertionError: 2 != 3\nE       AssertionError: expected total\n=========================== short test summary info ============================\n1 failed, 2 passed in 0.02s\n"
    unittest_pass = "test_add (tests.TestMath) ... ok\ntest_total (tests.TestMath) ... ok\n----------------------------------------------------------------------\nRan 2 tests in 0.001s\n\nOK\n"
    unittest_fail = "test_add (tests.TestMath) ... ok\ntest_total (tests.TestMath) ... FAIL\n======================================================================\nFAIL: test_total (tests.TestMath)\n----------------------------------------------------------------------\nTraceback (most recent call last):\n  File \"tests.py\", line 14, in test_total\nAssertionError: 2 != 3\n----------------------------------------------------------------------\nRan 2 tests in 0.001s\n\nFAILED (failures=1)\n"
    git_status = "On branch main\nChanges not staged for commit:\n  modified: scripts/tokenpipe.py\n  modified: tests/test_tokenpipe.py\nno changes added to commit\n"
    git_log = "\n".join("commit %040d\nAuthor: Lab User <lab@example.invalid>\nDate:   2026-01-%02d 00:00:00 +0000\n\n    deterministic fixture commit %03d" % (i, (i % 28) + 1, i) for i in range(1, 90)) + "\n"
    git_diff = "diff --git a/example.py b/example.py\nindex 1111111..2222222 100644\n--- a/example.py\n+++ b/example.py\n@@ -1,3 +1,43 @@\n def value():\n-    return 1\n+    return 2\n" + _repeat("+    # protected diff detail %03d\n" % 0, 40)
    rg_sparse = "\n".join("src/module%02d.py:%d:needle unique-%02d" % (i, i * 7, i) for i in range(1, 24)) + "\n"
    rg_dense = "\n".join("src/generated.py:%d:needle repeated payload" % i for i in range(1, 700)) + "\n"
    rg_io = "\n".join("rg: /missing-%03d: IO error for operation: No such file or directory" % i for i in range(1, 180)) + "\n"
    ls_output = "total 24\n-rw-r--r-- 1 lab lab  120 README.md\ndrwxr-xr-x 2 lab lab   64 src\n-rw-r--r-- 1 lab lab 4096 data.json\n"
    find_output = "\n".join("./src/module%02d.py" % i for i in range(1, 80)) + "\n"
    repetitive_log = _repeat("2026-01-01T00:00:00Z INFO worker heartbeat\n", 500)
    mixed_log = _repeat("INFO worker started\n", 100) + "WARN retrying request id=42\n" + _repeat("INFO worker finished\n", 90) + "ERROR request failed status=503\nTraceback: timeout\n"
    nested_json = json.dumps({"status": "ok", "items": [{"id": i, "meta": {"active": True, "labels": ["lab", "fixture"]}} for i in range(80)], "page": {"number": 1, "next": None}}, indent=2, sort_keys=True)
    error_json = json.dumps({"status": "failed", "error": {"type": "AssertionError", "message": "expected total", "trace": ["setup", "assert", "teardown"]}, "keys": ["status", "error"]}, indent=2, sort_keys=True)
    inventory_json = json.dumps({"schema": "inventory/v1", "hosts": [
        {"host": "node-%03d" % i, "region": "us-east-%d" % (i % 3), "uptime_s": i * 60,
         "checks": [{"name": name, "ok": (i + j) % 5 != 0, "value": (i * 7 + j * 3) % 97}
                    for j, name in enumerate(("disk", "memory", "cpu", "load", "io", "net", "gpu"))]}
        for i in range(48)]}, indent=2, sort_keys=True)
    ansi_progress = "\x1b[2K\rprogress 10%\x1b[2K\rprogress 50%\x1b[2K\rprogress 100%\ncompleted successfully\n"
    protected_code = "\n".join("def protected_%03d(value):\n    return value + %d" % (i, i) for i in range(20)) + "\n"
    protected_config = "[service]\nname = lab\nmode = protected\n" + "\n".join("option_%02d = value_%02d" % (i, i) for i in range(20)) + "\n"
    adversarial = _repeat("ordinary diagnostic line\n", 35) + "\nAPI_KEY=LAB_ONLY_SECRET\npassword=LAB_ONLY_PASSWORD\n\n" + _repeat("ordinary tail line\n", 35) + "adversarial failure marker\n"
    varying_log = _varying_log()
    varying_stdout, varying_stderr = _varying_log_streams()
    pytest_stderr_stdout = "============================= test session starts =============================\ncollected 2 items\n\ntests/test_io.py .F                                                [100%]\n=================================== FAILURES ===================================\nFAILED tests/test_io.py::test_write - OSError: disk quota exceeded\n=========================== short test summary info ============================\n1 failed, 1 passed in 0.03s\n"
    pytest_stderr_stderr = _repeat("WARNING: ssl module is compiled with an unsupported LibreSSL build\n", 30) + "Traceback (most recent call last):\n  File \"tests/test_io.py\", line 8, in test_write\nOSError: disk quota exceeded\n"
    return [
        Case("pytest-pass", "tests", "pytest", 3, 0, pytest_pass, must_keep=("3 passed",)),
        Case("pytest-fail", "tests", "pytest", 4, 1, pytest_fail, must_keep=("FAILED", "1 failed", "AssertionError")),
        Case("unittest-pass", "tests", "unittest", 3, 0, unittest_pass, must_keep=("Ran 2 tests", "OK")),
        Case("unittest-fail", "tests", "unittest", 4, 1, unittest_fail, must_keep=("FAIL", "FAILED", "AssertionError")),
        Case("git-status", "git", "git-status", 2, 0, git_status, must_keep=("Changes not staged",)),
        Case("git-log", "git", "git-log", 2, 0, git_log, must_keep=("deterministic fixture commit",)),
        Case("git-diff-protected", "protected", "git-diff", 5, 0, git_diff, must_keep=("diff --git", "return 2"), exact_policy={"mode": "exact", "reason": "diff is protected"}),
        Case("rg-sparse", "search", "rg-sparse", 1, 0, rg_sparse, must_keep=("needle unique-23",)),
        Case("rg-dense", "search", "rg-dense", 2, 0, rg_dense, must_keep=("needle repeated payload",)),
        Case("rg-io-errors", "search", "rg-io-errors", 4, 2, rg_io, must_keep=("IO error", "No such file or directory")),
        Case("ls-output", "filesystem", "ls", 1, 0, ls_output, must_keep=("README.md", "data.json")),
        Case("find-output", "filesystem", "find", 1, 0, find_output, must_keep=("./src/module79.py",)),
        Case("repetitive-log", "logs", "log", 2, 0, repetitive_log, must_keep=("heartbeat",)),
        Case("mixed-log", "logs", "log", 4, 1, mixed_log, must_keep=("ERROR", "Traceback", "status=503")),
        Case("nested-json", "json", "json", 3, 0, nested_json, must_keep=("status",), json_keys=("items", "page", "status"), json_expectations=(
            {"path": ("status",), "type": "string", "value": "ok"}, {"path": ("items",), "type": "array", "count": 80},
            {"path": ("items", 0, "id"), "type": "integer", "value": 0}, {"path": ("items", 0, "meta", "active"), "type": "boolean", "value": True},
            {"path": ("page", "number"), "type": "integer", "value": 1}, {"path": ("page", "next"), "type": "null", "value": None},
        )),
        Case("error-json", "json", "json", 5, 1, error_json, must_keep=("failed", "AssertionError"), json_keys=("error", "keys", "status"), json_expectations=(
            {"path": ("status",), "type": "string", "value": "failed"}, {"path": ("error",), "type": "object"},
            {"path": ("error", "type"), "type": "string", "value": "AssertionError"}, {"path": ("error", "trace"), "type": "array", "count": 3},
        )),
        Case("inventory-json", "json", "json", 3, 0, inventory_json, must_keep=("inventory/v1",), json_keys=("hosts", "schema"), json_expectations=(
            {"path": ("schema",), "type": "string", "value": "inventory/v1"}, {"path": ("hosts",), "type": "array", "count": 48},
            {"path": ("hosts", 0, "host"), "type": "string", "value": "node-000"}, {"path": ("hosts", 0, "region"), "type": "string", "value": "us-east-0"},
            {"path": ("hosts", 0, "checks"), "type": "array", "count": 7}, {"path": ("hosts", 0, "checks", 0, "name"), "type": "string", "value": "disk"},
        )),
        Case("ansi-progress", "logs", "log", 1, 0, ansi_progress, must_keep=("completed successfully",)), Case("small-output", "plain", "small", 1, 0, "ok\n"),
        Case("protected-code", "protected", "code", 5, 0, protected_code, must_keep=("def protected_019",), exact_policy={"mode": "exact", "reason": "source code is protected"}),
        Case("protected-config", "protected", "config", 5, 0, protected_config, must_keep=("mode = protected",), exact_policy={"mode": "exact", "reason": "configuration is protected"}),
        Case("adversarial-secret-like", "adversarial", "plain", 5, 0, adversarial, must_keep=("adversarial failure marker",), secret_like=("API_KEY=LAB_ONLY_SECRET", "password=LAB_ONLY_PASSWORD")),
        Case("varying-timestamp-log", "logs", "log", 4, 0, varying_log, must_keep=("worker heartbeat", "ERROR upload rejected", "status=503")),
        Case("varying-timestamp-log-stderr", "logs", "log", 4, 1, varying_stdout, stderr=varying_stderr, must_keep=("worker heartbeat", "ERROR upload rejected", "gc pause")),
        Case("pytest-fail-stderr", "tests", "pytest", 4, 1, pytest_stderr_stdout, stderr=pytest_stderr_stderr, must_keep=("FAILED", "1 failed", "disk quota exceeded", "WARNING")),
    ]


def sha256(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _applicable_stages(case):
    """Return the local stages a case's route is allowed to evaluate.

    Args:
        case (Case or CommandCase): Case whose ``route`` string selects the
            applicable stage set.

    Returns:
        tuple[str]: Stage names forming a sub-order of ``STAGES``. JSON routes
        (``json`` and any route suffixed ``-json``, such as ``rtk-json``) get
        ``json-lite``; textual log routes get ``log-lite``; protected
        diff/code/config routes keep only non-destructive stages.
    """
    if case.route == "json" or case.route.endswith("-json"):
        return ("ansi", "json-lite", "bound")
    if case.route in ("log", "rg-io-errors", "git-log", "pytest", "unittest", "generic-test", "rtk-log"):
        return ("ansi", "log-lite", "cca", "bound")
    if case.route in ("git-diff", "code", "config"):
        return ("ansi", "cca", "bound")
    return ("ansi", "cca", "bound")


def enumerate_orders(stages):
    stages = tuple(stage for stage in STAGES if stage in tuple(stages))
    before = {stage: set() for stage in stages}
    for left, right in PARTIAL_ORDER:
        if left in before and right in before:
            before[right].add(left)
    found, visited = set(), set()

    def visit(prefix, remaining):
        state = (tuple(prefix), tuple(remaining))
        if state in visited:
            return
        visited.add(state)
        found.add(tuple(prefix))
        for stage in remaining:
            if not (before[stage] & set(remaining)):
                next_remaining = tuple(item for item in remaining if item != stage)
                visit(prefix + [stage], next_remaining)
                visit(prefix, next_remaining)

    visit([], stages)
    return sorted(found, key=lambda item: (len(item), item))


def order_name(order):
    return ">".join(order) if order else "passthrough"


def pipeline_signature(source, order=()):
    return "%s:%s" % (source, order_name(order))


def _capture(argv, env=None, cwd=None):
    started = time.perf_counter()
    completed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=20, env=env, cwd=cwd)
    elapsed = round((time.perf_counter() - started) * 1000.0, 3)
    return completed.stdout.decode("utf-8", "replace") + completed.stderr.decode("utf-8", "replace"), completed.returncode, elapsed


def _write_fixture(case, fixture_dir):
    path = os.path.join(fixture_dir, case.case_id + ".py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("import sys\nsys.stdout.write(%r)\nsys.stderr.write(%r)\nsys.exit(%d)\n" % (case.stdout, case.stderr, case.exit_code))
    os.chmod(path, 0o700)
    return [sys.executable, path]


def _rtk_path(explicit=None, allow=True):
    if not allow:
        return None, "skipped (disabled)"
    candidate = explicit or os.environ.get("TOKENPIPE_LAB_RTK")
    if candidate is None:
        configured, enabled = tokenpipe.configured_rtk()
        candidate = configured if enabled else None
    if candidate and tokenpipe.trusted_rtk_path(candidate):
        return candidate, "available"
    return None, "skipped (unavailable)"


def _stage_apply(text, stage):
    if stage == "ansi":
        return tokenpipe.ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    if stage == "json-lite":
        return tokenpipe.lite_json(text)
    if stage == "log-lite":
        return tokenpipe.lite_log(text)
    if stage == "cca":
        return tokenpipe.cca_rank(text, target_chars=1400)
    if stage == "bound":
        return tokenpipe.bound_candidate(text, max_chars=1400)
    raise ValueError("unknown stage: %s" % stage)


def apply_order(text, order):
    for stage in order:
        text = _stage_apply(text, stage)
    return text


def _json_path(value, path):
    for part in path:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and isinstance(part, int) and 0 <= part < len(value):
            value = value[part]
        else:
            return False, None
    return True, value


def _json_type(value):
    if value is None: return "null"
    if isinstance(value, bool): return "boolean"
    if isinstance(value, int): return "integer"
    if isinstance(value, float): return "number"
    if isinstance(value, str): return "string"
    if isinstance(value, list): return "array"
    if isinstance(value, dict): return "object"
    return "unknown"


def _marker_omitted_count(item):
    """Return the omitted-item count a tokenpipe fold marker declares.

    Args:
        item (object): Candidate JSON value of any type.

    Returns:
        int or None: The declared omitted count when ``item`` is exactly a
        ``__tokenpipe_omitted_items__`` or ``__tokenpipe_similar_items__``
        marker object; ``None`` when ``item`` is ordinary array content.
    """
    if not isinstance(item, dict):
        return None
    if set(item) == {"__tokenpipe_omitted_items__"} and isinstance(item["__tokenpipe_omitted_items__"], int):
        return item["__tokenpipe_omitted_items__"]
    if set(item) == {"__tokenpipe_similar_items__", "keys"} and isinstance(item["__tokenpipe_similar_items__"], int):
        return item["__tokenpipe_similar_items__"]
    return None


def _effective_array_count(value):
    """Count the items a sanitized JSON array represents.

    Args:
        value (object): Candidate JSON value of any type.

    Returns:
        int or None: Effective item count with fold markers expanded to their
        declared omitted counts; ``None`` when ``value`` is not a list.
    """
    if not isinstance(value, list):
        return None
    total = 0
    for item in value:
        omitted = _marker_omitted_count(item)
        total += omitted if omitted is not None else 1
    return total


def schema_check(case, candidate):
    expectations = case.json_expectations or tuple({"path": (key,), "type": "unknown"} for key in case.json_keys)
    if not expectations:
        return {"valid": True, "failures": [], "expectations": []}
    try:
        value = json.loads(candidate)
    except (TypeError, ValueError) as exc:
        return {"valid": False, "failures": [{"reason": "invalid-json", "error": type(exc).__name__}], "expectations": [dict(item) for item in expectations]}
    failures = []
    for expectation in expectations:
        path = tuple(expectation["path"])
        present, actual = _json_path(value, path)
        if not present:
            failures.append({"path": list(path), "reason": "missing"})
            continue
        if expectation.get("type") not in (None, "unknown") and _json_type(actual) != expectation["type"]:
            failures.append({"path": list(path), "reason": "type", "expected": expectation["type"], "actual": _json_type(actual)})
            continue
        if "value" in expectation and actual != expectation["value"]:
            failures.append({"path": list(path), "reason": "value"})
        if "count" in expectation and _effective_array_count(actual) != expectation["count"]:
            failures.append({"path": list(path), "reason": "count", "expected": expectation["count"], "actual": _effective_array_count(actual)})
    return {"valid": not failures, "failures": failures, "expectations": [dict(item) for item in expectations]}


def _markers_valid(case, candidate):
    return (all(marker in candidate for marker in case.must_keep) and
            all(any(marker in candidate for marker in group) for group in case.alternative_markers))


def _normalize(text, fixture_root=""):
    normalized = tokenpipe.ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    if fixture_root:
        normalized = normalized.replace(os.path.abspath(fixture_root).rstrip(os.sep), "<fixture-root>")
    normalized = re.sub(r"\bin \d+(?:\.\d+)?s\b", "in <duration>s", normalized)
    return re.sub(r"\b\d+(?:\.\d+)?\s*ms\b", "<duration>ms", normalized)


def evaluate_candidate(case, raw, candidate, observed_exit, raw_recoverable, source="local", order=(), latency_ms=0.0, case_id=None, normalization=None, exact_raw=None):
    schema = schema_check(case, candidate)
    reasons = []
    gates = {"exit": observed_exit == case.exit_code, "markers": _markers_valid(case, candidate),
             "json_schema": schema["valid"], "exact_protected": case.exact_policy.get("mode") != "exact" or candidate == (raw if exact_raw is None else exact_raw),
             "non_expansion": len(candidate.encode("utf-8", "replace")) <= len(raw.encode("utf-8", "replace")), "raw_recoverable": bool(raw_recoverable)}
    reasons.extend(gate for gate, passed in gates.items() if not passed)
    raw_bytes = len(raw.encode("utf-8", "replace")); candidate_bytes = len(candidate.encode("utf-8", "replace"))
    raw_tokens = tokenpipe.estimate_tokens(raw); candidate_tokens = tokenpipe.estimate_tokens(candidate)
    ratio = 1.0 if not raw_bytes else candidate_bytes / float(raw_bytes)
    token_ratio = 1.0 if not raw_tokens else candidate_tokens / float(raw_tokens)
    savings = max(0.0, 1.0 - ratio); token_savings = max(0.0, 1.0 - token_ratio)
    coverage = 0.0 if not case.must_keep else sum(marker in candidate for marker in case.must_keep) / float(len(case.must_keep))
    penalty = min(0.25, max(0.0, latency_ms) / 1000.0)
    score = 0.55 * token_savings + 0.30 * savings + 0.15 * coverage - penalty
    signature = pipeline_signature(source, order)
    return {"id": signature, "signature": {"source": source, "order": order_name(order)}, "case_id": case_id,
            "source": source, "order": order_name(order), "valid": not reasons, "invalid_reasons": reasons, "gates": gates,
            "schema": schema, "bytes": candidate_bytes, "tokens": candidate_tokens, "normalized_bytes": candidate_bytes,
            "normalized_tokens": candidate_tokens, "raw_bytes": raw_bytes, "raw_tokens": raw_tokens,
            "output_ratio": round(ratio, 6), "token_ratio": round(token_ratio, 6), "savings": round(savings, 6),
            "token_savings": round(token_savings, 6), "marker_coverage": round(coverage, 6), "sha256": sha256(candidate),
            "latency_ms": latency_ms, "latency_penalty": round(penalty, 6), "selection_score": round(score, 6),
            "normalization": normalization or []}


def _candidate(case, raw, source, order, raw_recoverable, observed_exit, case_id=None, normalization=None, base_latency_ms=0.0, exact_raw=None):
    started = time.perf_counter()
    try:
        candidate = apply_order(raw, order)
    except Exception as exc:
        result = evaluate_candidate(case, raw, raw, observed_exit, raw_recoverable, source, order, base_latency_ms, case_id, normalization, exact_raw)
        result.update(valid=False, invalid_reasons=result["invalid_reasons"] + ["stage-error", type(exc).__name__])
        return result
    return evaluate_candidate(case, raw, candidate, observed_exit, raw_recoverable, source, order,
                              round(base_latency_ms + (time.perf_counter() - started) * 1000.0, 3), case_id, normalization, exact_raw)


def _best(candidates):
    valid = [item for item in candidates if item["valid"]]
    if not valid:
        return None
    within_guard = [item for item in valid if item["latency_ms"] <= MAX_SELECTION_LATENCY_MS]
    pool = within_guard or valid
    return max(pool, key=lambda item: (item["token_savings"], item["savings"], -_stage_count(item),
                                      -item["bytes"], item["source"] == "local", item["id"]))


def _stage_count(item):
    order = item["signature"]["order"]
    return 0 if order == "passthrough" else len(order.split(">"))


def _percentile(values, percentile):
    if not values: return 0.0
    ordered = sorted(values)
    return ordered[int(round((len(ordered) - 1) * percentile))]


def _aggregate_rows(records, scope, group=None, group_field="category"):
    selected = [record for record in records if record["scope"] == scope and (group is None or record[group_field] == group)]
    corpus_weight = sum(record["weight"] for record in selected)
    grouped = {}
    for record in selected:
        for item in record["candidates"]:
            slot = grouped.setdefault(item["id"], {"item": item, "cases": 0, "valid_cases": 0, "weight": 0, "valid_weight": 0, "ratios": [], "savings": [], "latencies": [], "bytes": [], "tokens": []})
            slot["cases"] += 1; slot["weight"] += record["weight"]
            if item["valid"]:
                slot["valid_cases"] += 1; slot["valid_weight"] += record["weight"]
                slot["ratios"].append((record["weight"], item["output_ratio"])); slot["savings"].append((record["weight"], item["savings"]))
                slot["latencies"].append(item["latency_ms"]); slot["bytes"].append((record["weight"], item["normalized_bytes"])); slot["tokens"].append((record["weight"], item["normalized_tokens"]))
    rows = []
    for signature, slot in sorted(grouped.items()):
        def weighted(values):
            denominator = sum(weight for weight, _ in values)
            return sum(weight * value for weight, value in values) / float(denominator) if denominator else 0.0
        median = statistics.median(slot["latencies"]) if slot["latencies"] else 0.0
        p95 = _percentile(slot["latencies"], 0.95)
        ratio = weighted(slot["ratios"]); savings = weighted(slot["savings"]); penalty = min(0.25, p95 / 1000.0)
        eligible_weight = slot["weight"]
        rows.append({"id": signature, "signature": slot["item"]["signature"], "scope": scope, "group": group, "group_field": group_field,
                     "cases": slot["cases"], "valid_cases": slot["valid_cases"], "eligible_weight": eligible_weight,
                     "coverage": round(slot["valid_weight"] / float(eligible_weight), 6) if eligible_weight else 0.0,
                     "corpus_coverage": round(slot["valid_weight"] / float(corpus_weight), 6) if corpus_weight else 0.0,
                     "normalized_output_ratio": round(ratio, 6), "normalized_savings": round(savings, 6),
                     "weighted_normalized_bytes": round(weighted(slot["bytes"]), 3), "weighted_normalized_tokens": round(weighted(slot["tokens"]), 3),
                     "median_latency_ms": round(median, 3), "p95_latency_ms": round(p95, 3), "latency_penalty": round(penalty, 6), "latency_aware_score": round(savings - penalty, 6)})
    return rows


def _winner_row(rows, coverage_field="coverage"):
    eligible = [row for row in rows if row[coverage_field] >= 1.0]
    if not eligible:
        return None
    within_guard = [row for row in eligible if row["p95_latency_ms"] <= MAX_SELECTION_LATENCY_MS]
    pool = within_guard or eligible
    return max(pool, key=lambda row: (row["normalized_savings"], -row["weighted_normalized_tokens"],
                                     -_stage_count(row), row["signature"]["source"] == "local",
                                     row["id"]))


def _routing_policy(records, scope):
    scoped = [record for record in records if record["scope"] == scope]
    group_field = "route" if scope == "command-matrix" else "category"
    groups = sorted(set(record[group_field] for record in scoped))
    selected = {}
    winners = {}
    for group in groups:
        rows = _aggregate_rows(records, scope, group, group_field)
        winner = _winner_row(rows, "corpus_coverage")
        winners[group] = winner
        if winner:
            selected[group] = winner["id"]
    chosen = []
    for record in scoped:
        signature = selected.get(record[group_field])
        item = next((candidate for candidate in record["candidates"] if candidate["id"] == signature and candidate["valid"]), None)
        if item:
            chosen.append((record, item))

    def weighted(field, values):
        denominator = sum(record["weight"] for record, _ in values)
        return sum(record["weight"] * item[field] for record, item in values) / float(denominator) if denominator else 0.0

    corpus_weight = sum(record["weight"] for record in scoped)
    chosen_weight = sum(record["weight"] for record, _ in chosen)
    latencies = [item["latency_ms"] for _, item in chosen]
    return {"selected": selected, "dimension": group_field, "group_count": len(groups), "covered_groups": len(selected),
            "normalized_output_ratio": round(weighted("output_ratio", chosen), 6),
            "normalized_savings": round(weighted("savings", chosen), 6),
            "weighted_normalized_bytes": round(weighted("normalized_bytes", chosen), 3),
            "weighted_normalized_tokens": round(weighted("normalized_tokens", chosen), 3),
            "median_latency_ms": round(statistics.median(latencies), 3) if latencies else 0.0,
            "p95_latency_ms": round(_percentile(latencies, 0.95), 3),
            "coverage": round(chosen_weight / float(corpus_weight), 6) if corpus_weight else 0.0,
            "corpus_coverage": round(chosen_weight / float(corpus_weight), 6) if corpus_weight else 0.0}


def _pareto(rows):
    """Pareto frontier over normalized aggregate pipeline rows, including latency."""
    rows = [row for row in rows if row.get("coverage", 0) > 0]
    frontier = []
    for row in rows:
        dominated = any(other is not row and other["normalized_output_ratio"] <= row["normalized_output_ratio"] and other["p95_latency_ms"] <= row["p95_latency_ms"] and other["coverage"] >= row["coverage"] and (other["normalized_output_ratio"] < row["normalized_output_ratio"] or other["p95_latency_ms"] < row["p95_latency_ms"] or other["coverage"] > row["coverage"]) for other in rows)
        if not dominated: frontier.append(row)
    return sorted(frontier, key=lambda item: item["id"])


def _write(path, text):
    with open(path, "w", encoding="utf-8") as handle: handle.write(text)


def _git_setup(path):
    os.makedirs(path); env = os.environ.copy(); env.update({"GIT_AUTHOR_NAME": "Lab User", "GIT_AUTHOR_EMAIL": "lab@example.invalid", "GIT_COMMITTER_NAME": "Lab User", "GIT_COMMITTER_EMAIL": "lab@example.invalid", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000"})
    subprocess.run(["git", "init", "-q", path], check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for key, value in (("user.name", "Lab User"), ("user.email", "lab@example.invalid"), ("commit.gpgSign", "false")): subprocess.run(["git", "-C", path, "config", key, value], check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _write(os.path.join(path, "tracked.txt"), "one\n")
    subprocess.run(["git", "-C", path, "add", "tracked.txt"], check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "-C", path, "commit", "-q", "-m", "fixture commit"], check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _write(os.path.join(path, "tracked.txt"), "one\ntwo\n")


def _pytest_python():
    seen = set()
    for candidate in (sys.executable, shutil.which("python"), shutil.which("python3")):
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        result = subprocess.run(
            [candidate, "-c", "import pytest"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=10,
        )
        if result.returncode == 0:
            return candidate
    return None


def _command_matrix(root, fixture_dir):
    command_root = os.path.join(fixture_dir, "commands"); os.makedirs(command_root)
    available = {name: bool(shutil.which(name)) for name in ("rg", "git", "ls", "find", "rtk")}
    pytest_python = _pytest_python()
    available["pytest"] = pytest_python is not None
    git_root = os.path.join(command_root, "git-repo"); _git_setup(git_root)
    test_pass, test_fail = os.path.join(command_root, "test_pass.py"), os.path.join(command_root, "test_fail.py")
    _write(test_pass, "import pytest\n@pytest.mark.parametrize('value', range(40))\ndef test_success(value):\n    assert value >= 0\n")
    _write(test_fail, "import pytest\n@pytest.mark.parametrize('value', range(30))\ndef test_many_pass(value):\n    assert value >= 0\ndef test_expected_value():\n    assert 41 == 42, 'sentinel assertion detail'\ndef test_exception_detail():\n    raise ValueError('sentinel exception detail')\n")
    generic_pass, generic_fail = os.path.join(command_root, "generic_pass.py"), os.path.join(command_root, "generic_fail.py")
    _write(generic_pass, "print('generic test passed')\n")
    _write(generic_fail, "import sys\nprint('generic test failed')\nsys.exit(1)\n")
    unittest_pass, unittest_fail = os.path.join(command_root, "unittest_pass.py"), os.path.join(command_root, "unittest_fail.py")
    _write(unittest_pass, "import unittest\nclass TestCase(unittest.TestCase):\n    def test_success(self): self.assertTrue(True)\nif __name__ == '__main__': unittest.main()\n")
    _write(unittest_fail, "import unittest\nclass TestCase(unittest.TestCase):\n    def test_failure(self): self.assertEqual(2, 3)\nif __name__ == '__main__': unittest.main()\n")
    log_path = os.path.join(command_root, "fixture.log"); _write(log_path, _repeat("2026-01-01T00:00:00Z INFO worker heartbeat\n", 120) + "ERROR request failed status=503\n")
    json_case = next(item for item in corpus() if item.case_id == "nested-json"); json_path = os.path.join(command_root, "fixture.json"); _write(json_path, json_case.raw + "\n")
    search_root = os.path.join(command_root, "search"); os.makedirs(search_root)
    _write(os.path.join(search_root, "sparse.txt"), "\n".join("needle sparse-%02d" % i for i in range(1, 8)) + "\n")
    _write(os.path.join(search_root, "dense.txt"), "\n".join("needle repeated payload %03d" % i for i in range(1, 120)) + "\n")
    for index in range(3): _write(os.path.join(search_root, "module%02d.py" % index), "needle module\n")
    _write(os.path.join(command_root, "README.md"), "README\n"); _write(os.path.join(command_root, "data.json"), "{}\n")
    cases = []
    if available["pytest"]:
        common = ("-vv", "-p", "no:cacheprovider")
        cases.append(CommandCase("pytest-pass", "tests", "pytest", 3, (pytest_python, "-m", "pytest") + common + (test_pass,), ("rtk", "pytest") + common + (test_pass,), command_root, 0, ("40 passed",), normalization_root=command_root))
        cases.append(CommandCase("pytest-fail", "tests", "pytest", 4, (pytest_python, "-m", "pytest") + common + (test_fail,), ("rtk", "pytest") + common + (test_fail,), command_root, 1, ("test_expected_value", "test_exception_detail", "sentinel assertion detail", "sentinel exception detail"), normalization_root=command_root))
    else:
        for case_id, path, code, marker in (("generic-test-pass", generic_pass, 0, "generic test passed"), ("generic-test-fail", generic_fail, 1, "generic test failed")):
            cases.append(CommandCase(case_id, "tests", "generic-test", 3 if not code else 4, (sys.executable, path), ("rtk", "test", sys.executable, path), command_root, code, (marker,), normalization_root=command_root))
    for case_id, path, code, marker in (("unittest-pass", unittest_pass, 0, "OK"), ("unittest-fail", unittest_fail, 1, "FAILED")):
        cases.append(CommandCase(case_id, "tests", "unittest", 3 if not code else 4, (sys.executable, path), ("rtk", "test", sys.executable, path), command_root, code, (marker,), normalization_root=command_root))
    if available["git"]:
        git_bin = shutil.which("git")
        for case_id, route, args, rtk_args, code, markers, policy in (("git-status", "git-status", ("status", "--short"), ("status", "--short"), 0, (" M tracked.txt",), {"mode": "compressible"}), ("git-log", "git-log", ("log", "--format=commit %H%nAuthor: %an <%ae>%nDate: %ad%n%n    %s", "--date=iso-strict", "-1"), ("log", "--format=commit %H%nAuthor: %an <%ae>%nDate: %ad%n%n    %s", "--date=iso-strict", "-1"), 0, ("fixture commit",), {"mode": "compressible"}), ("git-diff-protected", "git-diff", ("diff", "--no-ext-diff", "--binary"), ("diff", "--no-ext-diff", "--binary"), 0, ("diff --git", "tracked.txt"), {"mode": "exact", "reason": "diff is protected"})):
            cases.append(CommandCase(case_id, "protected" if policy["mode"] == "exact" else "git", route, 5 if policy["mode"] == "exact" else 2, (git_bin,) + args, ("rtk", "git") + rtk_args, git_root, code, markers, policy, normalization_root=command_root))
    if available["rg"]:
        rg_bin = shutil.which("rg")
        cases += [CommandCase("rg-sparse", "search", "rg-sparse", 1, (rg_bin, "needle", os.path.join(search_root, "sparse.txt")), ("rtk", "rg", "needle", os.path.join(search_root, "sparse.txt")), command_root, 0, ("sparse-07",), normalization_root=command_root), CommandCase("rg-dense", "search", "rg-dense", 2, (rg_bin, "needle", os.path.join(search_root, "dense.txt")), ("rtk", "rg", "needle", os.path.join(search_root, "dense.txt")), command_root, 0, ("repeated payload",), normalization_root=command_root), CommandCase("rg-io-errors", "search", "rg-io-errors", 4, (rg_bin, "needle", os.path.join(search_root, "missing.txt")), ("rtk", "rg", "needle", os.path.join(search_root, "missing.txt")), command_root, 2, ("No such file or directory",), normalization_root=command_root)]
    if available["ls"]:
        ls_bin = shutil.which("ls"); cases.append(CommandCase("ls-output", "filesystem", "ls", 1, (ls_bin, "-1", command_root), ("rtk", "ls", "-1", command_root), command_root, 0, ("README.md", "data.json"), normalization_root=command_root))
    if available["find"]:
        find_bin = shutil.which("find"); cases.append(CommandCase("find-output", "filesystem", "find", 1, (find_bin, command_root, "-maxdepth", "1", "-type", "f", "-print"), ("rtk", "find", command_root, "-maxdepth", "1", "-type", "f", "-print"), command_root, 0, ("README.md", "fixture.json"), normalization_root=command_root))
    cat = shutil.which("cat") or "/bin/cat"
    cases += [CommandCase("rtk-log", "logs", "rtk-log", 2, (cat, log_path), ("rtk", "log", log_path), command_root, 0, ("ERROR",), alternative_markers=(("heartbeat", "120 info messages"),), normalization_root=command_root), CommandCase("rtk-json", "json", "rtk-json", 3, (cat, json_path), ("rtk", "json", json_path), command_root, 0, ("status",), json_expectations=json_case.json_expectations, normalization_root=command_root)]
    return cases, available


def _manifest_argv(argv, root):
    return [str(item).replace(os.path.abspath(root), "<fixture-root>") for item in argv]


def _run_in_root(root, enable_rtk=True, explicit_rtk=None):
    fixture_dir = os.path.join(root, "fixtures"); raw_dir = tokenpipe._runtime_raw_root(); os.makedirs(fixture_dir); os.makedirs(raw_dir)
    rtk, rtk_status = _rtk_path(explicit_rtk, enable_rtk); env = os.environ.copy(); env["RTK_DB_PATH"] = os.path.join(root, "rtk-history.db"); env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    records, orders_seen, invalid = [], set(), []
    for case in corpus():
        raw, exit_code, capture_latency = _capture(_write_fixture(case, fixture_dir)); ref = tokenpipe.spool_raw(raw, "compression-lab", case.case_id, raw_dir); recoverable = tokenpipe.show_raw(ref) == raw
        orders = enumerate_orders(_applicable_stages(case)); orders_seen.update(order_name(order) for order in orders); candidates = [_candidate(case, raw, "local", order, recoverable, exit_code, case.case_id) for order in orders]
        best = _best(candidates); record = case.metadata(sha256(raw)); record.update(scope="synthetic", observed_exit_code=exit_code, raw_bytes=len(raw.encode()), raw_tokens=tokenpipe.estimate_tokens(raw), capture_latency_ms=capture_latency, raw_recoverable=recoverable, candidate_order_count=len(orders), candidate_orders=[order_name(order) for order in orders], winner=best["id"] if best else None, candidates=candidates); records.append(record)
        invalid += [(record["id"], item) for item in candidates if not item["valid"]]
    command_cases, discovered = _command_matrix(root, fixture_dir)
    for command in command_cases:
        raw, exit_code, capture_latency = _capture(command.argv, env=env, cwd=command.cwd); ref = tokenpipe.spool_raw(raw, "compression-lab-command", command.case_id, raw_dir); recoverable = tokenpipe.show_raw(ref) == raw
        normalized_raw = _normalize(raw, command.normalization_root); normalization = list(NORMALIZATION_RULES)
        quality_raw = raw if command.exact_policy.get("mode") == "exact" else normalized_raw
        quality_case = Case(command.case_id, command.category, command.route, command.weight, command.exit_code, quality_raw, must_keep=command.must_keep, alternative_markers=command.alternative_markers, exact_policy=command.exact_policy, json_expectations=command.json_expectations)
        candidates = [_candidate(quality_case, quality_raw, "local", order, recoverable, exit_code, command.case_id, normalization, 0.0, raw)
                      for order in enumerate_orders(_applicable_stages(quality_case))]
        rtk_capture = None
        if rtk:
            rtk_argv = tuple(rtk if item == "rtk" else item for item in command.rtk_argv); rtk_raw, rtk_exit, rtk_latency = _capture(rtk_argv, env=env, cwd=command.cwd); normalized_rtk = _normalize(rtk_raw, command.normalization_root)
            rtk_quality = rtk_raw if command.exact_policy.get("mode") == "exact" else normalized_rtk
            executed = rtk_exit == command.exit_code and _markers_valid(command, rtk_quality)
            rtk_recoverable = rtk_quality == quality_raw
            rtk_overhead = max(0.0, rtk_latency - capture_latency)
            rtk_capture = {"argv": _manifest_argv(command.rtk_argv, root), "exit_code": rtk_exit, "bytes": len(rtk_quality.encode()), "tokens": tokenpipe.estimate_tokens(rtk_quality), "sha256": sha256(rtk_quality), "latency_ms": rtk_latency, "overhead_ms": round(rtk_overhead, 3), "executed": executed, "raw_recoverable": rtk_recoverable}
            candidates.append(evaluate_candidate(quality_case, quality_raw, rtk_quality, rtk_exit, rtk_recoverable, "rtk", (), rtk_overhead, command.case_id, normalization, raw))
            for order in enumerate_orders(_applicable_stages(quality_case)):
                if not order: continue
                started = time.perf_counter()
                try:
                    candidate = apply_order(rtk_quality, order); latency = rtk_overhead + round((time.perf_counter() - started) * 1000.0, 3)
                    candidates.append(evaluate_candidate(quality_case, quality_raw, candidate, rtk_exit, rtk_recoverable, "rtk->local", order, latency, command.case_id, normalization, raw))
                except Exception as exc:
                    failed = evaluate_candidate(quality_case, quality_raw, rtk_quality, rtk_exit, rtk_recoverable, "rtk->local", order, rtk_overhead, command.case_id, normalization, raw); failed.update(valid=False, invalid_reasons=failed["invalid_reasons"] + ["stage-error", type(exc).__name__]); candidates.append(failed)
        best = _best(candidates); record = {"id": command.case_id, "scope": "command-matrix", "category": command.category, "route": command.route, "weight": command.weight, "exit_code": command.exit_code, "observed_exit_code": exit_code, "raw_sha256": sha256(normalized_raw), "raw_bytes": len(normalized_raw.encode()), "raw_tokens": tokenpipe.estimate_tokens(normalized_raw), "capture_latency_ms": capture_latency, "raw_recoverable": recoverable, "normalization": {"rules": normalization, "raw_changed": raw != normalized_raw}, "rtk_capture": rtk_capture, "winner": best["id"] if best else None, "candidates": candidates}; records.append(record); invalid += [(record["id"], item) for item in candidates if not item["valid"]]
    manifest = {"synthetic_corpus": {"cases": [{key: value for key, value in record.items() if key in ("id", "category", "route", "weight", "exit_code", "must_keep", "alternative_markers", "secret_like_count", "secret_like_sha256", "exact_policy", "json_keys", "json_expectations", "raw_sha256")} for record in records if record["scope"] == "synthetic"], "partial_order": [list(item) for item in PARTIAL_ORDER], "orders": sorted(orders_seen)}, "command_matrix": {"cases": [{"id": case.case_id, "category": case.category, "route": case.route, "weight": case.weight, "argv": _manifest_argv(case.argv, root), "rtk_argv": _manifest_argv(case.rtk_argv, root), "exit_code": case.exit_code, "must_keep": list(case.must_keep), "alternative_markers": [list(group) for group in case.alternative_markers], "exact_policy": case.exact_policy, "json_expectations": [dict(item) for item in case.json_expectations]} for case in command_cases], "discovered": discovered}}
    synthetic_rows = _aggregate_rows(records, "synthetic"); command_rows = _aggregate_rows(records, "command-matrix")
    per_content = {}
    for category in sorted(set(record["category"] for record in records)):
        scope = "command-matrix" if any(record["scope"] == "command-matrix" and record["category"] == category for record in records) else "synthetic"; rows = _aggregate_rows(records, scope, category); winner = _winner_row(rows); per_content[category] = {"scope": scope, "winner": winner["id"] if winner else None, "rows": rows}
    valid_count = sum(item["valid"] for record in records for item in record["candidates"]); all_candidates = [item for record in records for item in record["candidates"]]
    rtk_declared = len(command_cases) if rtk else 0
    command_records = [record for record in records if record["scope"] == "command-matrix"]
    rtk_executed = sum(bool((record.get("rtk_capture") or {}).get("executed")) for record in command_records)
    capabilities = {"stdlib": {"discovered": True, "executed": True, "status": "available"}, "unittest": {"discovered": True, "executed": any(record["id"].startswith("unittest-") and record["observed_exit_code"] == record["exit_code"] for record in command_records), "status": "available"}, "pytest": {"discovered": discovered["pytest"], "executed": any(record["id"].startswith("pytest-") and record["observed_exit_code"] == record["exit_code"] for record in command_records), "status": "available" if discovered["pytest"] else "skipped (unavailable)"}, "rg": {"discovered": discovered["rg"], "executed": any(record["id"].startswith("rg-") and record["observed_exit_code"] == record["exit_code"] for record in command_records), "status": "available" if discovered["rg"] else "skipped (unavailable)"}, "git": {"discovered": discovered["git"], "executed": any(record["id"].startswith("git-") and record["observed_exit_code"] == record["exit_code"] for record in command_records), "status": "available" if discovered["git"] else "skipped (unavailable)"}, "ls": {"discovered": discovered["ls"], "executed": any(record["id"] == "ls-output" and record["observed_exit_code"] == record["exit_code"] for record in command_records), "status": "available" if discovered["ls"] else "skipped (unavailable)"}, "find": {"discovered": discovered["find"], "executed": any(record["id"] == "find-output" and record["observed_exit_code"] == record["exit_code"] for record in command_records), "status": "available" if discovered["find"] else "skipped (unavailable)"}, "rtk": {"discovered": discovered["rtk"], "executed": bool(rtk and rtk_executed == rtk_declared), "executed_count": rtk_executed, "declared_count": rtk_declared, "status": rtk_status}}
    policies = {"synthetic": _routing_policy(records, "synthetic"), "command_matrix": _routing_policy(records, "command-matrix")}
    return {"schema_version": 2, "lab_version": LAB_VERSION, "manifest_sha256": canonical_hash(manifest), "manifest": manifest, "synthetic_corpus": {"case_count": sum(record["scope"] == "synthetic" for record in records), "raw_sha256": canonical_hash([record["raw_sha256"] for record in records if record["scope"] == "synthetic"]), "candidate_orders": len(orders_seen)}, "command_matrix": {"declared_case_count": len(command_cases), "executed_case_count": sum(record["observed_exit_code"] == record["exit_code"] and record["raw_recoverable"] for record in records if record["scope"] == "command-matrix"), "real_routes": [record["route"] for record in records if record["scope"] == "command-matrix"], "rtk_routes": [record["rtk_capture"]["argv"] for record in records if record.get("rtk_capture")]}, "normalization": {"rules": list(NORMALIZATION_RULES), "applied_capture_count": len(command_cases) * len(NORMALIZATION_RULES), "rule_counts": dict((rule, len(command_cases)) for rule in NORMALIZATION_RULES)}, "pipeline_comparison": {"evaluated": ["local-only"] + (["rtk-only", "rtk->local"] if rtk else []), "skipped": [] if rtk else ["rtk-only", "rtk->local"], "rejected": [{"order": "local->rtk", "reason": "would re-execute a stateful command"}]}, "capabilities": capabilities, "aggregate": {"cases": len(records), "candidate_orders": len(orders_seen), "candidates": len(all_candidates), "valid_candidates": valid_count, "invalid_candidates": len(invalid), "raw_bytes": sum(record["raw_bytes"] for record in records), "raw_tokens": sum(record["raw_tokens"] for record in records), "synthetic_cases": sum(record["scope"] == "synthetic" for record in records), "command_cases": len(command_cases), "total_weight": sum(record["weight"] for record in records)}, "pipeline_aggregates": {"synthetic": synthetic_rows, "command_matrix": command_rows}, "global_winners": policies, "per_content_winners": per_content, "pareto_frontier": _pareto(synthetic_rows + command_rows), "invalid_candidates": [{"case_id": case_id, "id": item["id"], "source": item["source"], "order": item["order"], "invalid_reasons": item["invalid_reasons"], "gates": item["gates"]} for case_id, item in invalid], "synthetic_cases": [record for record in records if record["scope"] == "synthetic"], "command_cases": [record for record in records if record["scope"] == "command-matrix"]}


def run_lab(enable_rtk=False, explicit_rtk=None, root=None):
    old_runtime = os.environ.get("TOKENPIPE_RUNTIME_HOME"); cleanup = None
    if root is None: cleanup = tempfile.TemporaryDirectory(prefix="tokenpipe-compression-lab-"); root = cleanup.name
    os.environ["TOKENPIPE_RUNTIME_HOME"] = os.path.join(root, "runtime")
    try: return _run_in_root(root, enable_rtk, explicit_rtk)
    finally:
        if old_runtime is None: os.environ.pop("TOKENPIPE_RUNTIME_HOME", None)
        else: os.environ["TOKENPIPE_RUNTIME_HOME"] = old_runtime
        if cleanup is not None: cleanup.cleanup()


def markdown_summary(report):
    aggregate = report["aggregate"]; caps = ", ".join("%s=%s (discovered=%s, executed=%s)" % (key, value["status"], value["discovered"], value["executed"]) for key, value in sorted(report["capabilities"].items()))
    lines = ["Tokenpipe compression lab %s" % report["lab_version"], "Manifest: %s" % report["manifest_sha256"], "Deterministic synthetic corpus: %d cases | %d candidate orders" % (report["synthetic_corpus"]["case_count"], report["synthetic_corpus"]["candidate_orders"]), "Real command matrix: %d declared / %d executed cases | routes: %s" % (report["command_matrix"]["declared_case_count"], report["command_matrix"]["executed_case_count"], ", ".join(report["command_matrix"]["real_routes"])), "Candidates: %d total | %d valid / %d invalid" % (aggregate["candidates"], aggregate["valid_candidates"], aggregate["invalid_candidates"]), "Comparisons: %s | skipped: %s | rejected: local->rtk" % (", ".join(report["pipeline_comparison"]["evaluated"]), ", ".join(report["pipeline_comparison"]["skipped"]) or "none"), "Normalization: %d captures | %s" % (report["normalization"]["applied_capture_count"], "; ".join(report["normalization"]["rules"])), "Capabilities: %s" % caps, "Global policies:"]
    for scope, policy in sorted(report["global_winners"].items()):
        lines.append("  %s: selected=%s | savings=%.4f | ratio=%.4f | median/p95 latency=%.3f/%.3f ms | coverage=%.4f (corpus=%.4f)" % (scope, json.dumps(policy["selected"], sort_keys=True), policy["normalized_savings"], policy["normalized_output_ratio"], policy["median_latency_ms"], policy["p95_latency_ms"], policy["coverage"], policy["corpus_coverage"]))
    lines.append("Pareto frontier: %d aggregate pipeline rows" % len(report["pareto_frontier"]))
    lines.extend("Per-content winner %-12s %s" % (key, value["winner"] or "none") for key, value in sorted(report["per_content_winners"].items()))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--json", action="store_true"); parser.add_argument("--rtk"); parser.add_argument("--no-rtk", action="store_true")
    args = parser.parse_args(argv); report = run_lab(enable_rtk=not args.no_rtk, explicit_rtk=args.rtk)
    if args.json: json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True); sys.stdout.write("\n")
    else: print(markdown_summary(report))
    return 0


if __name__ == "__main__": sys.exit(main())
