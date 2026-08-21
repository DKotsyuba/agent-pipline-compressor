import importlib.util
import json
import os
import stat
import shutil
import tempfile
import time
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import unittest


SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "tokenpipe.py")
SPEC = importlib.util.spec_from_file_location("tokenpipe", SCRIPT)
tokenpipe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tokenpipe)


class TokenpipeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_env = os.environ.copy()
        os.environ["TOKENPIPE_HOME"] = self.temp.name
        os.environ["TOKENPIPE_RUNTIME_HOME"] = os.path.join(self.temp.name, "runtime")
        os.environ["TOKENPIPE_MIN_TOKENS_ESTIMATE"] = "10"
        os.environ["PATH"] = self.temp.name + os.pathsep + self.old_env.get("PATH", "")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def payload(self, output, **extra):
        value = {
            "output": output,
            "session_id": "session-test",
            "tool_call_id": "call-test",
            "tool_name": "exec_command",
            "command": "pytest tests -q --token=DO_NOT_LOG",
            "exit_status": 1,
        }
        value.update(extra)
        return value

    def executable(self, name, body):
        path = os.path.join(self.temp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!%s\n" % sys.executable)
            handle.write(body)
        os.chmod(path, 0o700)
        return path

    def test_small_output_passthrough(self):
        os.environ["TOKENPIPE_MIN_TOKENS_ESTIMATE"] = "100"
        result = tokenpipe.process(self.payload("ok"), "safe")
        self.assertEqual(result["action"], "passthrough")
        self.assertEqual(result["output"], "ok")
        self.assertEqual(result["skip_reason"], "below-threshold")

    def test_audit_measures_without_changing_output(self):
        raw = "same log line\n" * 200
        result = tokenpipe.process(self.payload(raw), "audit")
        self.assertEqual(result["action"], "passthrough")
        self.assertEqual(result["output"], raw)
        self.assertLess(result["counterfactual_tokens_estimate"], result["original_tokens_estimate"])
        self.assertIsNone(result["raw_ref"])

    def test_json_compression_and_recovery(self):
        raw = json.dumps({"items": list(range(200)), "status": "ok"}, indent=4)
        result = tokenpipe.process(self.payload(raw), "safe")
        self.assertEqual(result["action"], "replace")
        self.assertEqual(result["strategy"], "lite-json")
        self.assertEqual(tokenpipe.show_raw(result["raw_ref"]), raw)
        self.assertIn("__tokenpipe_omitted_items__", result["output"])

    def test_error_ranking_preserves_failure(self):
        raw = ("noise\n" * 300) + "\n\nFAILED auth_test.py expected 401 got 200\nTraceback: boom\n" + ("tail\n" * 200)
        result = tokenpipe.process(self.payload(raw), "safe")
        self.assertEqual(result["action"], "replace")
        self.assertIn("FAILED auth_test.py", result["output"])
        self.assertIn("Traceback", result["output"])

    def test_diff_and_code_passthrough(self):
        diff = "diff --git a/a b/a\n--- a/a\n+++ b/a\n" + ("+important\n" * 300)
        result = tokenpipe.process(self.payload(diff), "safe")
        self.assertEqual(result["action"], "passthrough")
        self.assertEqual(result["skip_reason"], "diff-passthrough")
        code = "\n".join(["def function_%d():\n    return %d" % (i, i) for i in range(100)])
        result = tokenpipe.process(self.payload(code), "safe")
        self.assertEqual(result["action"], "passthrough")
        self.assertEqual(result["skip_reason"], "code-passthrough")

    def test_private_permissions_and_show_guard(self):
        raw = json.dumps({"items": list(range(100))}, indent=4)
        result = tokenpipe.process(self.payload(raw), "safe")
        raw_ref = result["raw_ref"]
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(raw_ref)).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(raw_ref).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(tokenpipe._metrics_path()).st_mode), 0o600)
        with self.assertRaises(ValueError):
            tokenpipe.show_raw("/etc/hosts")

    def test_metrics_do_not_contain_command_args_or_content(self):
        secret = "UNIQUE_SECRET_CONTENT"
        raw = (secret + " repeated line\n") * 200
        tokenpipe.process(self.payload(raw), "audit")
        with open(tokenpipe._metrics_path(), "r", encoding="utf-8") as handle:
            telemetry = handle.read()
        self.assertNotIn(secret, telemetry)
        self.assertNotIn("--token", telemetry)
        self.assertIn('"command_category": "test"', telemetry)

    def test_stats_aggregation(self):
        raw = "repeat\n" * 200
        tokenpipe.process(self.payload(raw), "audit")
        tokenpipe.process(self.payload(raw, tool_call_id="second"), "safe")
        report = tokenpipe.aggregate(tokenpipe.load_metrics())
        self.assertEqual(report["calls"], 2)
        self.assertIn("session-test", report["groups"]["session"])
        self.assertIn("test", report["groups"]["command_category"])
        self.assertTrue(report["token_counts_are_estimates"])

    def test_accepts_only_coarse_supplied_command_category(self):
        safe = self.payload("repeat\n" * 200, command="", command_category="build")
        tokenpipe.process(safe, "audit")
        unsafe = self.payload("repeat\n" * 200, command="", command_category="npm build --secret=x")
        tokenpipe.process(unsafe, "audit")
        rows = tokenpipe.load_metrics()
        self.assertEqual(rows[0]["command_category"], "build")
        self.assertEqual(rows[1]["command_category"], "shell-other")

    def test_raw_spool_failure_is_fail_open(self):
        raw = json.dumps({"items": list(range(200))}, indent=4)
        original = tokenpipe.spool_raw
        try:
            def broken(*args, **kwargs):
                raise OSError("disk full")
            tokenpipe.spool_raw = broken
            result = tokenpipe.process(self.payload(raw), "safe")
        finally:
            tokenpipe.spool_raw = original
        self.assertEqual(result["action"], "passthrough")
        self.assertEqual(result["output"], raw)
        self.assertEqual(result["skip_reason"], "raw-spool-error")

    def test_spool_ttl_and_size_cleanup(self):
        first = tokenpipe.spool_raw("a" * 100, "cleanup", "old")
        second = tokenpipe.spool_raw("b" * 100, "cleanup", "new")
        old_time = time.time() - 100
        os.utime(first, (old_time, old_time))
        os.environ["TOKENPIPE_RAW_TTL_SECONDS"] = "50"
        os.environ["TOKENPIPE_RAW_MAX_BYTES"] = "1000"
        tokenpipe.cleanup_spool()
        self.assertFalse(os.path.exists(first))
        self.assertTrue(os.path.exists(second))
        third = tokenpipe.spool_raw("c" * 100, "cleanup", "third")
        os.environ["TOKENPIPE_RAW_TTL_SECONDS"] = "0"
        os.environ["TOKENPIPE_RAW_MAX_BYTES"] = "100"
        tokenpipe.cleanup_spool()
        self.assertEqual(sum(os.path.getsize(os.path.join(base, name))
                             for base, _, names in os.walk(tokenpipe._raw_root())
                             for name in names), 100)

    def test_long_one_line_json_and_error_are_bounded(self):
        os.environ["TOKENPIPE_MAX_SHOWN_CHARS"] = "700"
        raw_json = json.dumps({"huge": "x" * 10000})
        json_result = tokenpipe.process(self.payload(raw_json), "safe")
        self.assertEqual(json_result["action"], "replace")
        self.assertLessEqual(len(json_result["output"]), 700)
        self.assertIn("tokenpipe bounded output", json_result["output"])
        raw_error = "ERROR start " + ("failure-detail-" * 1000) + " end-marker"
        error_result = tokenpipe.process(self.payload(raw_error, tool_call_id="long-error"), "safe")
        self.assertEqual(error_result["action"], "replace")
        self.assertLessEqual(len(error_result["output"]), 700)
        self.assertIn("ERROR start", error_result["output"])
        self.assertIn("end-marker", error_result["output"])

    def test_persistent_mode_is_private_and_used(self):
        self.assertEqual(tokenpipe.configured_mode(), "audit")
        tokenpipe.set_configured_mode("safe")
        self.assertEqual(tokenpipe.configured_mode(), "safe")
        path = tokenpipe._config_path()
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        raw = json.dumps({"items": list(range(200))}, indent=4)
        result = tokenpipe.process(self.payload(raw))
        self.assertEqual(result["mode"], "safe")
        self.assertEqual(result["action"], "replace")

    def test_native_exec_preserves_exit_and_labels_streams(self):
        pytest = self.executable(
            "pytest",
            "import sys\nprint('same line\\n' * 400, end='')\n"
            "print('FAILED important', file=sys.stderr)\nsys.exit(7)\n",
        )
        output, status_code = tokenpipe.execute_native(
            [pytest], "test", "full", "native-session", "native-call"
        )
        self.assertEqual(status_code, 7)
        self.assertTrue(output.startswith("tokenpipe-native-v1"))
        self.assertIn("--- stdout ---", output)
        self.assertIn("--- stderr ---", output)
        self.assertIn("FAILED important", output)
        self.assertIn("raw_ref=", output)
        metric = tokenpipe.load_metrics()[-1]
        self.assertEqual(metric["exit_status"], 7)
        self.assertGreater(metric["native_header_bytes"], 0)
        self.assertEqual(metric["shown_bytes"], len(output.encode("utf-8")))

    def test_native_safe_rejects_test_category_but_full_accepts(self):
        pytest = self.executable("pytest", "print('duplicate\\n' * 500, end='')\n")
        safe, _ = tokenpipe.execute_native([pytest], "test", "safe")
        full, _ = tokenpipe.execute_native([pytest], "test", "full", tool_call_id="full")
        self.assertNotIn("raw_ref=", safe.splitlines()[0])
        self.assertIn("category-not-allowed-in-mode", tokenpipe.load_metrics()[-2]["skip_reason"])
        self.assertIn("raw_ref=", full.splitlines()[0])

    def test_native_unknown_yaml_toml_source_and_plain_passthrough(self):
        samples = [
            "root:\n  child: value\n" * 200,
            "[section]\nkey = value\n" * 200,
            "def thing():\n    return 1\n" * 200,
            "ordinary unique prose %d\n" * 400,
        ]
        for index, sample in enumerate(samples):
            # Unknown argv/category must be refused before the child executes.
            script = self.executable("unknown%d" % index, "print(%r, end='')\n" % sample)
            output, status = tokenpipe.execute_native([script], "unknown", "full", tool_call_id="u%d" % index)
            self.assertEqual(status, 126)
            self.assertNotIn(sample, output)
            self.assertIn("tokenpipe refused wrapper execution", output)
            self.assertNotIn("raw_ref=", output.splitlines()[0])

    def test_native_json_is_compressed_before_stream_labels(self):
        payload = json.dumps({"items": list(range(2000)), "status": "ok"})
        rg = self.executable("rg", "print(%r, end='')\n" % payload)
        output, status = tokenpipe.execute_native(
            [rg], "search", "safe", "json-session", "json-call"
        )
        self.assertEqual(status, 0)
        self.assertIn("strategy=lite-json", output.splitlines()[0])
        self.assertIn("raw_ref=", output.splitlines()[0])
        self.assertIn("__tokenpipe_omitted_items__", output)

    def test_native_allowlisted_config_outputs_remain_exact_passthrough(self):
        for index, sample in enumerate((
            "root:\n  child: value\n" * 300,
            "[section]\nkey = value\n" * 300,
        )):
            rg = self.executable("rg", "print(%r, end='')\n" % sample)
            output, status = tokenpipe.execute_native(
                [rg, "needle"], "search", "safe", tool_call_id="config-%d" % index
            )
            self.assertEqual(status, 0)
            self.assertIn(sample, output)
            self.assertNotIn("raw_ref=", output.splitlines()[0])
            self.assertEqual(tokenpipe.load_metrics()[-1]["skip_reason"], "config-passthrough")

    def test_native_lite_log_path_is_exercised(self):
        rg = self.executable("rg", "print('same log line\\n' * 500, end='')\n")
        output, status = tokenpipe.execute_native([rg, "needle"], "search", "safe")
        self.assertEqual(status, 0)
        self.assertIn("strategy=lite-log", output.splitlines()[0])
        self.assertIn("previous line repeated", output)
        self.assertIn("raw_ref=", output.splitlines()[0])

    def test_native_ignores_environment_mode_without_persisted_mode(self):
        ls_path = shutil.which("ls")
        self.assertIsNotNone(ls_path)
        os.environ["TOKENPIPE_MODE"] = "safe"
        output, status = tokenpipe.execute_native([ls_path, self.temp.name], "filesystem-read")
        self.assertEqual(status, 126)
        self.assertIn("mode-does-not-execute", output)

    def test_native_refuses_mismatch_before_child_execution(self):
        marker = os.path.join(self.temp.name, "must-not-exist")
        rogue = self.executable(
            "rogue",
            "from pathlib import Path\nPath(%r).write_text('executed')\n" % marker,
        )
        output, status_code = tokenpipe.execute_native([rogue], "git-read", "safe")
        self.assertEqual(status_code, 126)
        self.assertIn("tokenpipe refused wrapper execution", output)
        self.assertFalse(os.path.exists(marker))

    def test_native_authoritative_validator_rejects_pre_hook_denials(self):
        denied = [
            ([self.executable("find", "print('executed')\n"), ".", "-fprint", "out"], "search"),
            ([self.executable("git", "print('executed')\n"), "log", "--output", "out"], "git-read"),
            ([self.executable("ruff", "print('executed')\n"), "check", "--fix", "."], "lint"),
            ([self.executable("pytest", "print('executed')\n"), "--pdb"], "test"),
        ]
        for argv, category in denied:
            output, status_code = tokenpipe.execute_native(argv, category, "full")
            self.assertEqual(status_code, 126, argv)
            self.assertIn("tokenpipe refused wrapper execution", output)
            self.assertNotIn("executed", output)

    def test_native_rejects_absolute_path_spoof_not_resolved_from_path(self):
        other = tempfile.TemporaryDirectory()
        try:
            path = os.path.join(other.name, "rg")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("#!%s\nprint('SPOOF_EXECUTED')\n" % sys.executable)
            os.chmod(path, 0o700)
            output, status_code = tokenpipe.execute_native([path, "needle"], "search", "safe")
            self.assertEqual(status_code, 126)
            self.assertNotIn("SPOOF_EXECUTED", output)
            self.assertIn("untrusted-executable", output)
        finally:
            other.cleanup()

    def test_native_capture_limit_terminates_and_reports_child(self):
        pytest = self.executable(
            "pytest",
            "import os\nchunk=b'x'*65536\n"
            "while True: os.write(1, chunk)\n",
        )
        os.environ["TOKENPIPE_CAPTURE_MAX_BYTES"] = str(1024 * 1024)
        output, status_code = tokenpipe.execute_native([pytest], "test", "full")
        self.assertEqual(status_code, 125)
        self.assertIn("captured output exceeded", output)
        self.assertLess(len(output.encode("utf-8")), 1024 * 1024 + 16384)

    def test_cli_forwards_signal_kills_stubborn_child_and_reaps(self):
        pid_path = os.path.join(self.temp.name, "stubborn.pid")
        self.executable(
            "pytest",
            "import os, signal, time\n"
            "open(%r, 'w').write(str(os.getpid()))\n" % pid_path
            + "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "while True: time.sleep(0.1)\n",
        )
        tokenpipe.set_configured_mode("full")
        process = subprocess.Popen(
            [sys.executable, SCRIPT, "exec", "--category", "test", "--", "pytest"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy(),
        )
        deadline = time.time() + 3
        while not os.path.exists(pid_path) and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(os.path.exists(pid_path))
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 143)
        self.assertIn(b"child interrupted and reaped after signal 15", stdout)
        self.assertEqual(stderr, b"")
        with open(pid_path, "r", encoding="utf-8") as handle:
            child_pid = int(handle.read())
        with self.assertRaises(OSError):
            os.kill(child_pid, 0)

    def test_show_rejects_symlink_final_component(self):
        directory = os.path.join(tokenpipe._raw_root(), "session")
        os.makedirs(directory, mode=0o700)
        link = os.path.join(directory, "swap.log")
        os.symlink("/etc/hosts", link)
        with self.assertRaises((OSError, ValueError)):
            tokenpipe.show_raw(link)

    def test_spool_collision_retries_exclusive_create(self):
        original_open = tokenpipe.os.open
        calls = {"count": 0}

        def collide_once(path, flags, mode=0o777):
            if "raw" in path and calls["count"] == 0:
                calls["count"] += 1
                raise OSError(tokenpipe.errno.EEXIST, "collision")
            return original_open(path, flags, mode)

        tokenpipe.os.open = collide_once
        try:
            path = tokenpipe.spool_raw("recoverable", "collision", "call")
        finally:
            tokenpipe.os.open = original_open
        self.assertEqual(tokenpipe.show_raw(path), "recoverable")
        self.assertEqual(calls["count"], 1)

    def test_native_spool_cap_is_enforced_after_write_and_fails_open(self):
        os.environ["TOKENPIPE_RAW_MAX_BYTES"] = "10"
        pytest = self.executable("pytest", "print('repeat\\n' * 500, end='')\n")
        output, _ = tokenpipe.execute_native([pytest], "test", "full", tool_call_id="cap")
        self.assertNotIn("raw_ref=", output.splitlines()[0])
        self.assertIn("repeat", output)
        self.assertEqual(tokenpipe.load_metrics()[-1]["skip_reason"], "raw-spool-error")

    def test_concurrent_metric_appends_remain_valid_jsonl(self):
        count = 40

        def write_one(index):
            tokenpipe.record_skip("search", "parallel-%d" % index, "audit", "parallel", str(index))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write_one, range(count)))
        rows = tokenpipe.load_metrics()
        self.assertEqual(len(rows), count)

    def test_skip_records_audit_overflow_without_content(self):
        tokenpipe.record_skip("search", "audit-output-overflow", "audit", "s", "c")
        metric = tokenpipe.load_metrics()[-1]
        self.assertTrue(metric["audit_overflow"])
        self.assertEqual(metric["original_bytes"], 0)

    def test_rtk_direct_prefix_requires_trusted_absolute_binary(self):
        rtk = self.executable(
            "rtk",
            "import os, sys\nos.execv(sys.argv[1], sys.argv[1:])\n",
        )
        ls_path = "/bin/ls"
        try:
            tokenpipe.set_configured_rtk(rtk)
            output, status_code = tokenpipe.execute_native(
                [ls_path, self.temp.name], "filesystem-read", "safe"
            )
            self.assertEqual(status_code, 0)
            self.assertIn("strategy=rtk-direct", output.splitlines()[0])
            metric = tokenpipe.load_metrics()[-1]
            self.assertTrue(metric["rtk_used"])
            report = tokenpipe.aggregate([metric])
            self.assertEqual(report["groups"]["strategy"]["rtk-direct"]["rtk_calls"], 1)
            os.chmod(rtk, 0o722)
            output, status_code = tokenpipe.execute_native(
                [ls_path, self.temp.name], "filesystem-read", "safe"
            )
            self.assertEqual(status_code, 0)
            self.assertNotIn("strategy=rtk-direct", output.splitlines()[0])
        finally:
            tokenpipe.set_configured_rtk(None)

    def test_metrics_file_is_capped_and_remains_jsonl(self):
        os.environ["TOKENPIPE_METRICS_MAX_BYTES"] = "4096"
        for index in range(150):
            tokenpipe.record_skip("search", "bounded-%d" % index, "audit", "metrics-cap", str(index))
        path = tokenpipe._metrics_path()
        # One final row may put the file slightly over the requested cap.
        self.assertLess(os.path.getsize(path), 5000)
        rows = tokenpipe.load_metrics()
        self.assertGreater(len(rows), 0)
        self.assertLess(len(rows), 150)


if __name__ == "__main__":
    unittest.main()
