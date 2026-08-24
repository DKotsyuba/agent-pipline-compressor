import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(__file__))
LAB = os.path.join(ROOT, "benchmarks", "compression_lab.py")
SPEC = importlib.util.spec_from_file_location("compression_lab", LAB)
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)


class CompressionLabTests(unittest.TestCase):
    def test_synthetic_manifest_and_command_matrix_are_separate(self):
        first = lab.corpus()
        second = lab.corpus()
        self.assertEqual([(item.case_id, item.raw, item.exit_code) for item in first], [(item.case_id, item.raw, item.exit_code) for item in second])
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            first_report = lab.run_lab(enable_rtk=False, root=left)
            second_report = lab.run_lab(enable_rtk=False, root=right)
        self.assertEqual(first_report["manifest_sha256"], second_report["manifest_sha256"])
        self.assertEqual(first_report["manifest"], second_report["manifest"])
        self.assertEqual(first_report["global_winners"]["command_matrix"]["selected"],
                         second_report["global_winners"]["command_matrix"]["selected"])
        self.assertEqual(first_report["synthetic_corpus"]["case_count"], len(lab.corpus()))
        self.assertGreater(first_report["command_matrix"]["declared_case_count"], 10)
        self.assertNotEqual(first_report["synthetic_corpus"]["case_count"], first_report["command_matrix"]["declared_case_count"])

    def test_order_generation_and_local_only_scope(self):
        orders = lab.enumerate_orders(("ansi", "log-lite", "cca", "bound"))
        self.assertIn((), orders)
        self.assertIn(("ansi", "log-lite", "cca", "bound"), orders)
        for order in orders:
            positions = {stage: order.index(stage) for stage in order}
            for before, after in lab.PARTIAL_ORDER:
                if before in positions and after in positions:
                    self.assertLess(positions[before], positions[after])
        with tempfile.TemporaryDirectory() as root:
            report = lab.run_lab(enable_rtk=False, root=root)
        self.assertTrue(all(item["source"] == "local" for record in report["synthetic_cases"] for item in record["candidates"]))
        self.assertEqual(report["pipeline_comparison"]["rejected"][0]["order"], "local->rtk")

    def test_real_rtk_route_argv_and_executed_capability(self):
        cases, _ = lab._command_matrix(tempfile.mkdtemp(), tempfile.mkdtemp())
        pytest_case = next((case for case in cases if case.route == "pytest"), None)
        if pytest_case:
            self.assertEqual(pytest_case.rtk_argv[0:2], ("rtk", "pytest"))
        self.assertTrue(any(case.rtk_argv[0:2] == ("rtk", "log") for case in cases))
        self.assertTrue(any(case.rtk_argv[0:2] == ("rtk", "json") for case in cases))
        with tempfile.TemporaryDirectory() as root:
            report = lab.run_lab(enable_rtk=False, root=root)
        self.assertFalse(report["capabilities"]["rtk"]["executed"])
        self.assertFalse(report["command_cases"][0]["rtk_capture"])

    def test_pytest_module_is_used_without_console_script(self):
        pytest_python = lab._pytest_python()
        if pytest_python is None:
            self.skipTest("pytest module unavailable")
        with tempfile.TemporaryDirectory() as root:
            cases, available = lab._command_matrix(root, root)
        self.assertTrue(available["pytest"])
        self.assertEqual([case.route for case in cases[:2]], ["pytest", "pytest"])
        self.assertEqual(cases[0].argv[0], pytest_python)
        self.assertEqual(cases[0].rtk_argv[:2], ("rtk", "pytest"))

    def test_generic_test_fallback_uses_script_files_not_shell_fragments(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            lab, "_pytest_python", return_value=None
        ):
            cases, available = lab._command_matrix(root, root)
        generic = [case for case in cases if case.route == "generic-test"]
        self.assertFalse(available["pytest"])
        self.assertEqual(len(generic), 2)
        for case in generic:
            self.assertNotIn("-c", case.argv)
            self.assertNotIn("-c", case.rtk_argv)
            self.assertTrue(case.argv[-1].endswith(".py"))

    def test_rtk_log_alternative_markers_accept_raw_and_valid_summary(self):
        command = next(item for item in lab._command_matrix(tempfile.mkdtemp(), tempfile.mkdtemp())[0] if item.case_id == "rtk-log")
        raw = "heartbeat\n" * 120 + "ERROR\n"
        case = lab.Case(command.case_id, command.category, command.route, command.weight, command.exit_code, raw, must_keep=command.must_keep, alternative_markers=command.alternative_markers)
        self.assertEqual(case.must_keep, ("ERROR",))
        self.assertEqual(case.alternative_markers, (("heartbeat", "120 info messages"),))
        self.assertTrue(lab.evaluate_candidate(case, raw, raw, 0, True)["valid"])
        self.assertTrue(lab.evaluate_candidate(case, raw, "120 info messages\nERROR\n", 0, True)["valid"])

    def test_rtk_log_alternative_marker_wrong_count_fails(self):
        command = next(item for item in lab._command_matrix(tempfile.mkdtemp(), tempfile.mkdtemp())[0] if item.case_id == "rtk-log")
        case = lab.Case(command.case_id, command.category, command.route, command.weight, command.exit_code, "heartbeat\nERROR\n", must_keep=command.must_keep, alternative_markers=command.alternative_markers)
        result = lab.evaluate_candidate(case, "heartbeat\nERROR\n", "119 info messages\nERROR\n", 0, True)
        self.assertFalse(result["valid"])
        self.assertIn("markers", result["invalid_reasons"])

    def test_rtk_log_alternative_markers_are_report_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            report = lab.run_lab(enable_rtk=False, root=root)
        metadata = next(item for item in report["manifest"]["command_matrix"]["cases"] if item["id"] == "rtk-log")
        self.assertEqual(metadata["must_keep"], ["ERROR"])
        self.assertEqual(metadata["alternative_markers"], [["heartbeat", "120 info messages"]])

    def test_enabled_rtk_executes_each_declared_route_once(self):
        with tempfile.TemporaryDirectory() as root:
            log_path = os.path.join(root, "rtk-argv.jsonl")
            rtk_path = os.path.join(root, "rtk-shim")
            shim = """#!/usr/bin/env python3
import json, subprocess, sys
log_path = %r
args = sys.argv[1:]
with open(log_path, 'a', encoding='utf-8') as handle:
    handle.write(json.dumps(args) + '\\n')
route, rest = args[0], args[1:]
if route == 'pytest':
    failed = 'test_fail.py' in rest[-1]
    if failed:
        print('30 passed, 2 failed')
        print('test_expected_value: sentinel assertion detail')
        print('test_exception_detail: sentinel exception detail')
    else:
        print('40 passed')
    raise SystemExit(1 if failed else 0)
elif route == 'test':
    command = rest[1:] if rest[:1] == ['--'] else rest
elif route in ('git', 'rg', 'ls', 'find'):
    command = [route] + rest
elif route in ('log', 'json'):
    with open(rest[-1], 'r', encoding='utf-8') as source:
        sys.stdout.write(source.read())
    raise SystemExit(0)
else:
    raise SystemExit(127)
raise SystemExit(subprocess.run(command, check=False).returncode)
""" % log_path
            with open(rtk_path, "w", encoding="utf-8") as handle:
                handle.write(shim)
            os.chmod(rtk_path, 0o700)
            report = lab.run_lab(enable_rtk=True, explicit_rtk=rtk_path, root=root)
            with open(log_path, "r", encoding="utf-8") as handle:
                route_calls = [json.loads(line) for line in handle if line.strip()]
        self.assertTrue(report["capabilities"]["rtk"]["executed"], report["capabilities"]["rtk"])
        self.assertEqual(report["capabilities"]["rtk"]["executed_count"], report["capabilities"]["rtk"]["declared_count"])
        self.assertEqual(report["capabilities"]["rtk"]["declared_count"], report["command_matrix"]["declared_case_count"])
        self.assertEqual(len(route_calls), report["command_matrix"]["declared_case_count"])
        self.assertEqual(report["pipeline_comparison"]["evaluated"], ["local-only", "rtk-only", "rtk->local"])
        self.assertTrue(all(record["rtk_capture"]["executed"] for record in report["command_cases"]))
        self.assertTrue(all(record["rtk_capture"]["overhead_ms"] >= 0 for record in report["command_cases"]))

    def test_command_local_matrix_evaluates_orders_and_can_compress(self):
        with tempfile.TemporaryDirectory() as root:
            report = lab.run_lab(enable_rtk=False, root=root)
        self.assertTrue(any(candidate["source"] == "local" and candidate["order"] != "passthrough"
                            for record in report["command_cases"] for candidate in record["candidates"]))
        self.assertTrue(all(candidate["latency_ms"] >= 0
                            for record in report["command_cases"] for candidate in record["candidates"]
                            if candidate["source"] == "local"))
        self.assertTrue(all(next(candidate for candidate in record["candidates"]
                                 if candidate["id"] == "local:passthrough")["latency_ms"]
                            < record["capture_latency_ms"] for record in report["command_cases"]))
        self.assertTrue(any(record["winner"].startswith("local:") and record["winner"] != "local:passthrough"
                            for record in report["command_cases"]))

    def test_global_policy_is_full_coverage_and_protected_category_is_passthrough(self):
        with tempfile.TemporaryDirectory() as root:
            report = lab.run_lab(enable_rtk=False, root=root)
        for policy in report["global_winners"].values():
            self.assertEqual(policy["coverage"], 1.0)
            self.assertEqual(policy["corpus_coverage"], 1.0)
            self.assertEqual(policy["group_count"], policy["covered_groups"])
        self.assertTrue(any(signature != "local:passthrough" for signature in report["global_winners"]["command_matrix"]["selected"].values()))
        self.assertEqual(report["global_winners"]["synthetic"]["selected"]["protected"], "local:passthrough")
        self.assertEqual(report["global_winners"]["command_matrix"]["dimension"], "route")
        self.assertEqual(report["global_winners"]["command_matrix"]["selected"]["git-diff"], "local:passthrough")

    def test_real_routing_policy_rejects_rtk_without_raw_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            report = lab.run_lab(enable_rtk=True, root=root)
        selected = report["global_winners"]["command_matrix"]["selected"]
        self.assertTrue(all(signature.startswith("local:") for signature in selected.values()))
        changed = [record for record in report["command_cases"]
                   if record["rtk_capture"] and not record["rtk_capture"]["raw_recoverable"]]
        self.assertTrue(changed)
        self.assertTrue(all(not candidate["valid"] and "raw_recoverable" in candidate["invalid_reasons"]
                            for record in changed for candidate in record["candidates"]
                            if candidate["source"].startswith("rtk")))
        self.assertEqual(selected["git-diff"], "local:passthrough")

    def test_signature_coverage_ignores_inapplicable_cases_but_reports_corpus_coverage(self):
        case = lab.Case("coverage", "x", "plain", 1, 0, "x" * 100)
        present = lab.evaluate_candidate(case, case.raw, "x" * 10, 0, True, "local", (), 1)
        absent = lab.evaluate_candidate(case, case.raw, case.raw, 0, True, "other", (), 1)
        records = [{"scope": "synthetic", "category": "x", "weight": 1, "candidates": [present]},
                   {"scope": "synthetic", "category": "x", "weight": 1, "candidates": [absent]}]
        row = next(item for item in lab._aggregate_rows(records, "synthetic") if item["id"] == present["id"])
        self.assertEqual(row["coverage"], 1.0)
        self.assertEqual(row["corpus_coverage"], 0.5)

    def test_recursive_json_types_scalars_and_effective_omitted_count(self):
        case = next(item for item in lab.corpus() if item.case_id == "nested-json")
        parsed = json.loads(case.raw)
        parsed["items"] = parsed["items"][:20] + [{"__tokenpipe_omitted_items__": 55}] + parsed["items"][-5:]
        self.assertTrue(lab.schema_check(case, json.dumps(parsed))["valid"])
        wrong_type = json.loads(case.raw); wrong_type["page"]["number"] = "1"
        wrong_scalar = json.loads(case.raw); wrong_scalar["status"] = "failed"
        wrong_count = json.loads(case.raw); wrong_count["items"] = wrong_count["items"][:2]
        self.assertFalse(lab.schema_check(case, json.dumps(wrong_type))["valid"])
        self.assertFalse(lab.schema_check(case, json.dumps(wrong_scalar))["valid"])
        self.assertFalse(lab.schema_check(case, json.dumps(wrong_count))["valid"])

    def test_secret_like_content_is_model_output_but_not_report_telemetry(self):
        case = next(item for item in lab.corpus() if item.case_id == "adversarial-secret-like")
        result = lab.evaluate_candidate(case, case.raw, case.raw, case.exit_code, True)
        self.assertTrue(result["valid"])
        with tempfile.TemporaryDirectory() as root:
            report = lab.run_lab(enable_rtk=False, root=root)
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("LAB_ONLY_SECRET", serialized)
        self.assertNotIn("LAB_ONLY_PASSWORD", serialized)
        metadata = next(item for item in report["manifest"]["synthetic_corpus"]["cases"] if item["id"] == case.case_id)
        self.assertEqual(metadata["secret_like_count"], 2)
        self.assertEqual(len(metadata["secret_like_sha256"]), 2)

    def test_protected_diff_config_and_raw_recovery_gates(self):
        for case_id in ("git-diff-protected", "protected-config"):
            case = next(item for item in lab.corpus() if item.case_id == case_id)
            exact = lab.evaluate_candidate(case, case.raw, case.raw, case.exit_code, True)
            altered = lab.evaluate_candidate(case, case.raw, case.raw + "changed", case.exit_code, True)
            self.assertTrue(exact["valid"])
            self.assertFalse(altered["valid"])
            self.assertIn("exact_protected", altered["invalid_reasons"])
        case = next(item for item in lab.corpus() if item.case_id == "protected-code")
        self.assertFalse(lab.evaluate_candidate(case, case.raw, case.raw, case.exit_code, False)["valid"])

    def test_exact_gate_rejects_normalization_only_difference(self):
        case = lab.Case("exact", "protected", "config", 1, 0, "value\r\n", exact_policy={"mode": "exact"})
        result = lab.evaluate_candidate(case, "value\r\n", "value\n", 0, True, exact_raw="value\r\n")
        self.assertFalse(result["valid"])
        self.assertIn("exact_protected", result["invalid_reasons"])
        self.assertTrue(lab.evaluate_candidate(case, "value\r\n", "value\r\n", 0, True, exact_raw="value\r\n")["valid"])

    def test_capability_execution_is_command_matrix_only(self):
        discovered = {name: True for name in ("pytest", "rg", "git", "ls", "find", "rtk")}
        with tempfile.TemporaryDirectory() as root, mock.patch.object(lab, "_command_matrix", return_value=([], discovered)):
            report = lab.run_lab(enable_rtk=True, explicit_rtk="/does/not/exist", root=root)
        for name in ("pytest", "unittest", "rg", "git", "ls", "find"):
            self.assertFalse(report["capabilities"][name]["executed"], name)
        self.assertEqual(report["capabilities"]["rtk"]["executed_count"], 0)
        self.assertEqual(report["capabilities"]["rtk"]["declared_count"], 0)

    def test_destructive_compressor_cannot_win_and_must_not_emit_is_not_a_gate(self):
        case = next(item for item in lab.corpus() if item.case_id == "pytest-fail")
        destructive = lab.evaluate_candidate(case, case.raw, "destructively shortened", case.exit_code, True, order=("cca",))
        self.assertFalse(destructive["valid"])
        self.assertEqual(lab._best([destructive]), None)
        adversarial = next(item for item in lab.corpus() if item.case_id == "adversarial-secret-like")
        self.assertNotIn("must_not_emit", lab.evaluate_candidate(adversarial, adversarial.raw, adversarial.raw, 0, True)["gates"])

    def test_aggregate_signature_winner_and_latency_aware_pareto(self):
        case = lab.Case("same", "x", "plain", 1, 0, "x" * 100)
        slow = lab.evaluate_candidate(case, case.raw, "x" * 10, 0, True, "local", (), 500)
        fast = lab.evaluate_candidate(case, case.raw, "x" * 20, 0, True, "rtk", (), 1)
        records = [{"scope": "command-matrix", "category": "x", "weight": 1, "candidates": [slow, fast]}, {"scope": "command-matrix", "category": "x", "weight": 10, "candidates": [slow, fast]}]
        rows = lab._aggregate_rows(records, "command-matrix")
        self.assertEqual({row["id"] for row in rows}, {"local:passthrough", "rtk:passthrough"})
        self.assertEqual(lab._winner_row(rows)["id"], "rtk:passthrough")
        self.assertGreater(rows[0]["latency_penalty"], 0)
        self.assertTrue(all("p95_latency_ms" in row for row in lab._pareto(rows)))

    def test_sub_tolerance_latency_noise_prefers_shorter_equivalent_pipeline(self):
        case = lab.Case("same", "x", "plain", 1, 0, "x" * 100)
        shorter = lab.evaluate_candidate(case, case.raw, "x" * 10, 0, True, "local", ("cca",), 10)
        noisily_faster = lab.evaluate_candidate(case, case.raw, "x" * 10, 0, True, "local", ("ansi", "cca"), 1)
        self.assertEqual(lab._best([shorter, noisily_faster])["id"], "local:cca")
        records = [{"scope": "command-matrix", "category": "x", "weight": 1,
                    "candidates": [shorter, noisily_faster]}]
        self.assertEqual(lab._winner_row(lab._aggregate_rows(records, "command-matrix"))["id"], "local:cca")

    def test_cli_labels_modes_and_json_report(self):
        script = [sys.executable, LAB]
        plain = subprocess.run(script + ["--no-rtk"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertIn("Deterministic synthetic corpus:", plain.stdout)
        self.assertIn("Real command matrix:", plain.stdout)
        payload = subprocess.run(script + ["--no-rtk", "--json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        self.assertEqual(payload.returncode, 0, payload.stderr)
        report = json.loads(payload.stdout)
        self.assertEqual(report["pipeline_comparison"]["skipped"], ["rtk-only", "rtk->local"])
        self.assertIn("normalization", report)


if __name__ == "__main__":
    unittest.main()
