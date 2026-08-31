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
from unittest import mock


SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "tokenpipe.py")
SPEC = importlib.util.spec_from_file_location("tokenpipe", SCRIPT)
tokenpipe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tokenpipe)


class TokenpipeTests(unittest.TestCase):
    def setUp(self):
        """Create private state and an explicitly trusted fixture bin directory."""
        self.temp = tempfile.TemporaryDirectory()
        self.old_env = os.environ.copy()
        self.old_trusted_dirs = tokenpipe._TRUSTED_EXECUTABLE_DIRS
        tokenpipe._TRUSTED_EXECUTABLE_DIRS = frozenset(
            tuple(self.old_trusted_dirs) + (self.temp.name,)
        )
        os.environ["TOKENPIPE_HOME"] = self.temp.name
        os.environ["TOKENPIPE_RUNTIME_HOME"] = os.path.join(self.temp.name, "runtime")
        os.environ["TOKENPIPE_MIN_TOKENS_ESTIMATE"] = "10"
        os.environ["PATH"] = self.temp.name + os.pathsep + self.old_env.get("PATH", "")

    def tearDown(self):
        """Restore process environment and executable trust before cleanup."""
        os.environ.clear()
        os.environ.update(self.old_env)
        tokenpipe._TRUSTED_EXECUTABLE_DIRS = self.old_trusted_dirs
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

    def test_replace_categories_gate_suppresses_spool_not_counterfactual(self):
        raw = "same log line\n" * 200
        payload = self.payload(raw)
        payload["replace_categories"] = ["json", "error"]
        result = tokenpipe.process(payload, "safe")
        self.assertEqual(result["action"], "passthrough")
        self.assertEqual(result["output"], raw)
        self.assertEqual(result["skip_reason"], "category-gated")
        self.assertIsNone(result["raw_ref"])
        self.assertLess(result["counterfactual_tokens_estimate"], result["original_tokens_estimate"])
        self.assertEqual(result["shown_tokens_estimate"], result["original_tokens_estimate"])

    def test_replace_categories_allow_replaces_matching_category(self):
        raw = "same log line\n" * 200
        payload = self.payload(raw)
        payload["replace_categories"] = ["log"]
        result = tokenpipe.process(payload, "safe")
        self.assertEqual(result["action"], "replace")
        self.assertIsNotNone(result["raw_ref"])
        self.assertLess(result["shown_tokens_estimate"], result["original_tokens_estimate"])

    def test_no_replace_categories_replaces_as_before(self):
        raw = "same log line\n" * 200
        result = tokenpipe.process(self.payload(raw), "safe")
        self.assertEqual(result["action"], "replace")
        self.assertIsNotNone(result["raw_ref"])
        self.assertLess(result["shown_tokens_estimate"], result["original_tokens_estimate"])

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

    def test_binary_and_control_heavy_output_is_exact_passthrough(self):
        """NUL/control-heavy output must never be transformed or spooled."""
        for raw in (
            ("\x00BIN\xff\n" * 400),
            ("\x01\x02\x03payload" * 200),
            ("\ufffdbad\n" * 1000),
        ):
            result = tokenpipe.process(self.payload(raw), "safe")
            self.assertEqual(result["action"], "passthrough")
            self.assertEqual(result["output"], raw)
            self.assertIsNone(result["raw_ref"])

    def test_native_invalid_utf8_is_not_compressed_or_spooled(self):
        """Lossy decode replacement markers force exact-policy passthrough."""
        pytest = self.executable(
            "pytest", "import os\nos.write(1, b'\\xffbad\\n' * 1000)\n"
        )
        output, status_code = tokenpipe.execute_native([pytest], "test", "full")
        self.assertEqual(status_code, 0)
        self.assertIn("strategy=passthrough", output.splitlines()[0])
        self.assertNotIn("raw_ref=", output.splitlines()[0])
        self.assertIn("\ufffdbad", output)

    def test_error_ranking_preserves_failure(self):
        raw = ("noise\n" * 300) + "\n\nFAILED auth_test.py expected 401 got 200\nTraceback: boom\n" + ("tail\n" * 200)
        result = tokenpipe.process(self.payload(raw), "safe")
        self.assertEqual(result["action"], "replace")
        self.assertIn("FAILED auth_test.py", result["output"])
        self.assertIn("Traceback", result["output"])

    def test_classify_search_line_numbers_and_rg_io_errors(self):
        numbered = "\n".join(
            "%d:module%04d needle unique payload" % (line, line)
            for line in range(1, 1000)
        )
        self.assertEqual(tokenpipe.classify(numbered), "plain")
        io_errors = "\n".join(
            "rg: /missing-%03d: IO error for operation: No such file or directory" % line
            for line in range(300)
        )
        self.assertEqual(tokenpipe.classify(io_errors), "error")
        failed_json = json.dumps({"status": "failed", "items": list(range(200))})
        self.assertEqual(tokenpipe.classify(failed_json), "json")

    def test_plugin_version_fails_open_for_non_object_manifest(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="[]")):
            self.assertEqual(tokenpipe.plugin_version(), tokenpipe.VERSION)

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
        self.assertEqual(report["groups"]["mode"]["audit"]["calls"], 1)
        self.assertEqual(report["groups"]["mode"]["safe"]["calls"], 1)
        self.assertEqual(report["audit_calls"], 1)
        self.assertEqual(report["native_calls"], 0)
        self.assertIn(tokenpipe.plugin_version(), report["groups"]["plugin_version"])
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

    def test_refused_audit_exec_does_not_inflate_native_coverage(self):
        ls_path = shutil.which("ls")
        output, status = tokenpipe.execute_native(
            [ls_path, self.temp.name], "filesystem-read", "audit"
        )
        self.assertEqual(status, 126)
        self.assertIn("strategy=refused", output.splitlines()[0])
        report = tokenpipe.aggregate([tokenpipe.load_metrics()[-1]])
        self.assertEqual(report["audit_calls"], 0)
        self.assertEqual(report["native_calls"], 0)
        self.assertEqual(report["native_refused_calls"], 1)
        self.assertEqual(report["native_call_coverage_percent_estimate"], 0.0)

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

    def test_native_cli_falls_back_to_validated_original_on_capture_oserror(self):
        ls_path = shutil.which("ls")
        self.assertIsNotNone(ls_path)

        class FallbackCalled(Exception):
            pass

        with mock.patch.object(tokenpipe, "_run_captured", side_effect=FileNotFoundError("no temp")), \
                mock.patch.object(tokenpipe.os, "execvpe", side_effect=FallbackCalled) as fallback:
            with self.assertRaises(FallbackCalled):
                tokenpipe.execute_native(
                    [ls_path, self.temp.name], "filesystem-read", "safe", exec_fallback=True
                )
        fallback.assert_called_once_with(ls_path, [ls_path, self.temp.name], mock.ANY)

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

    def test_native_rejects_project_path_shim(self):
        """A project-controlled PATH executable must not run even when 0755."""
        project_bin = tempfile.TemporaryDirectory(dir=os.getcwd())
        marker = os.path.join(project_bin.name, "marker")
        try:
            shim = os.path.join(project_bin.name, "rg")
            with open(shim, "w", encoding="utf-8") as handle:
                handle.write("#!%s\nopen(%r, 'w').write('ran')\n" % (sys.executable, marker))
            os.chmod(shim, 0o755)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = project_bin.name + os.pathsep + old_path
            output, status_code = tokenpipe.execute_native(["rg", "needle"], "search", "safe")
            self.assertEqual(status_code, 126)
            self.assertFalse(os.path.exists(marker))
            self.assertIn("untrusted-executable", output)
        finally:
            project_bin.cleanup()

    def test_safe_git_disables_external_diff_and_fsmonitor_helpers(self):
        """Safe Git reads must not invoke repository-configured executables."""
        git = shutil.which("git")
        if not git:
            self.skipTest("git unavailable")
        repo = tempfile.TemporaryDirectory()
        marker = os.path.join(repo.name, "helper-ran")
        helper = os.path.join(repo.name, "helper")
        with open(helper, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nprintf ran > %s\nexit 0\n" % marker)
        os.chmod(helper, 0o700)
        subprocess.run([git, "init", "-q", repo.name], check=True)
        subprocess.run([git, "-C", repo.name, "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run([git, "-C", repo.name, "config", "user.name", "test"], check=True)
        path = os.path.join(repo.name, "file.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("one\n")
        subprocess.run([git, "-C", repo.name, "add", "file.txt"], check=True)
        subprocess.run([git, "-C", repo.name, "commit", "-qm", "base"], check=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("two\n")
        subprocess.run([git, "-C", repo.name, "config", "diff.external", helper], check=True)
        subprocess.run([git, "-C", repo.name, "config", "core.fsmonitor", helper], check=True)
        previous = os.getcwd()
        try:
            os.chdir(repo.name)
            output, status_code = tokenpipe.execute_native(["git", "diff"], "git-read", "safe")
            self.assertEqual(status_code, 0, output)
            self.assertFalse(os.path.exists(marker))
            output, status_code = tokenpipe.execute_native(["git", "status", "--short"], "git-read", "safe")
            self.assertEqual(status_code, 0, output)
            self.assertFalse(os.path.exists(marker))
            refused, refused_status = tokenpipe.execute_native(
                ["git", "diff", "--ext-diff"], "git-read", "safe"
            )
            self.assertEqual(refused_status, 126)
            self.assertIn("category-command-mismatch", refused)
        finally:
            os.chdir(previous)
            repo.cleanup()

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
        """CLI main forwards termination and reaps a trusted fixture process."""
        pid_path = os.path.join(self.temp.name, "stubborn.pid")
        self.executable(
            "pytest",
            "import os, signal, time\n"
            "open(%r, 'w').write(str(os.getpid()))\n" % pid_path
            + "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "while True: time.sleep(0.1)\n",
        )
        driver = os.path.join(self.temp.name, "tokenpipe-driver.py")
        with open(driver, "w", encoding="utf-8") as handle:
            handle.write(
                "import importlib.util, sys\n"
                "spec = importlib.util.spec_from_file_location('tokenpipe_driver_core', %r)\n"
                "module = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(module)\n"
                "module._TRUSTED_EXECUTABLE_DIRS = frozenset((%r,))\n"
                "sys.argv = [%r] + sys.argv[1:]\n"
                "raise SystemExit(module.main())\n" % (SCRIPT, self.temp.name, SCRIPT)
            )
        tokenpipe.set_configured_mode("full")
        process = subprocess.Popen(
            [sys.executable, driver, "exec", "--category", "test", "--", "pytest"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy(),
        )
        deadline = time.time() + 10
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

    def test_spool_rejects_intermediate_session_symlink(self):
        """Raw spooling must fail open without writing through a session link."""
        raw_root = tokenpipe._raw_root()
        tokenpipe._mkdir_private(raw_root)
        outside = tempfile.TemporaryDirectory()
        try:
            os.symlink(outside.name, os.path.join(raw_root, "linked-session"))
            raw = json.dumps({"items": list(range(300))}, indent=2)
            payload = self.payload(raw)
            payload["session_id"] = "linked-session"
            result = tokenpipe.process(payload, "safe")
            self.assertEqual(result["action"], "passthrough")
            self.assertEqual(result["output"], raw)
            self.assertEqual(os.listdir(outside.name), [])
        finally:
            outside.cleanup()

    def test_spool_collision_retries_exclusive_create(self):
        original_open = tokenpipe.os.open
        calls = {"count": 0}

        def collide_once(path, flags, mode=0o777, *, dir_fd=None):
            """Inject one file-allocation collision while preserving dirfd opens."""
            if flags & os.O_CREAT and calls["count"] == 0:
                calls["count"] += 1
                raise OSError(tokenpipe.errno.EEXIST, "collision")
            return original_open(path, flags, mode, dir_fd=dir_fd)

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
        db_path_seen = os.path.join(self.temp.name, "rtk-db-path-seen")
        command_seen = os.path.join(self.temp.name, "rtk-command-seen")
        rtk = self.executable(
            "rtk",
            "import os, sys\nopen(%r, 'w').write(os.environ.get('RTK_DB_PATH', ''))\n"
            "open(%r, 'w').write(sys.argv[1])\nos.execvp(sys.argv[1], sys.argv[1:])\n"
            % (db_path_seen, command_seen),
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
            self.assertEqual(report["native_calls"], 1)
            self.assertEqual(report["rtk_owned_calls"], 1)
            self.assertEqual(report["native_call_coverage_percent_estimate"], 100.0)
            with open(command_seen, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "ls")
            with open(db_path_seen, "r", encoding="utf-8") as handle:
                self.assertEqual(
                    handle.read(), os.path.join(tokenpipe._runtime_home(), "rtk-history.db")
                )
            custom_db = os.path.join(self.temp.name, "custom-rtk.db")
            os.environ["RTK_DB_PATH"] = custom_db
            tokenpipe.execute_native([ls_path, self.temp.name], "filesystem-read", "safe")
            with open(db_path_seen, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), custom_db)
            os.chmod(rtk, 0o722)
            output, status_code = tokenpipe.execute_native(
                [ls_path, self.temp.name], "filesystem-read", "safe"
            )
            self.assertEqual(status_code, 0)
            self.assertNotIn("strategy=rtk-direct", output.splitlines()[0])
        finally:
            tokenpipe.set_configured_rtk(None)

    def test_rtk_no_savings_output_falls_through_to_deterministic_compression(self):
        command_seen = os.path.join(self.temp.name, "rtk-fallback-command-seen")
        rtk = self.executable(
            "rtk",
            "import os, sys\nopen(%r, 'w').write(sys.argv[1])\n"
            "os.execvp(sys.argv[1], sys.argv[1:])\n" % command_seen,
        )
        rg_target = self.executable("rg-real", "print('same log line\\n' * 500, end='')\n")
        rg = os.path.join(self.temp.name, "rg")
        os.symlink(rg_target, rg)
        try:
            tokenpipe.set_configured_rtk(rtk)
            output, status_code = tokenpipe.execute_native(
                [rg, "needle"], "search", "safe", "rtk-session", "rtk-fallback"
            )
            self.assertEqual(status_code, 0)
            self.assertIn("strategy=rtk-direct_lite-log", output.splitlines()[0])
            self.assertIn("raw_ref=", output.splitlines()[0])
            self.assertIn("previous line repeated", output)
            with open(command_seen, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "rg")
            metric = tokenpipe.load_metrics()[-1]
            self.assertTrue(metric["rtk_used"])
            self.assertEqual(metric["strategy"], "rtk-direct+lite-log")
            self.assertGreater(metric["original_bytes"], metric["shown_bytes"])
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

    def test_metrics_symlink_victim_is_unchanged_and_processing_fails_open(self):
        """Metrics symlinks must not alter victim content, mode, or tool output."""
        victim_dir = tempfile.TemporaryDirectory()
        try:
            victim = os.path.join(victim_dir.name, "victim")
            with open(victim, "w", encoding="utf-8") as handle:
                handle.write("original")
            os.chmod(victim, 0o644)
            os.symlink(victim, tokenpipe._metrics_path())
            with self.assertRaises(OSError):
                tokenpipe._append_metric({"safe": True})
            result = tokenpipe.process(self.payload("small exact output"), "safe")
            self.assertEqual(result["output"], "small exact output")
            with open(victim, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "original")
            self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), 0o644)
        finally:
            victim_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
