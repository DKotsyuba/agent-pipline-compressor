import importlib.util
import json
import os
import tempfile
import unittest


SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "tokenpipe.py")
SPEC = importlib.util.spec_from_file_location("tokenpipe", SCRIPT)
tokenpipe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tokenpipe)

LAB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "benchmarks", "compression_lab.py")
LAB_SPEC = importlib.util.spec_from_file_location("compression_lab", LAB_PATH)
lab = importlib.util.module_from_spec(LAB_SPEC)
LAB_SPEC.loader.exec_module(lab)


def homogeneous(count, keys=("alpha", "beta")):
    """Build a homogeneous object array for folding assertions.

    Args:
        count (int): Number of objects to build.
        keys (tuple[str]): Key names every object exposes.

    Returns:
        list[dict]: ``count`` objects sharing exactly ``keys``, each holding
        distinct integer values derived from its index.
    """
    return [
        {key: index * len(keys) + offset for offset, key in enumerate(keys)}
        for index in range(count)
    ]


class JsonFoldingTests(unittest.TestCase):
    """Regression coverage for homogeneous JSON array folding."""

    def setUp(self):
        """Point tokenpipe private state at an isolated temporary home."""
        self.temp = tempfile.TemporaryDirectory()
        self.old_env = os.environ.copy()
        os.environ["TOKENPIPE_HOME"] = self.temp.name
        os.environ["TOKENPIPE_RUNTIME_HOME"] = os.path.join(self.temp.name, "runtime")
        os.environ["TOKENPIPE_MIN_TOKENS_ESTIMATE"] = "10"

    def tearDown(self):
        """Restore the process environment and remove the temporary home."""
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def payload(self, output, **extra):
        """Build a hook payload describing one tool result.

        Args:
            output (str): Raw tool output text.
            **extra (dict): Additional payload fields overlaid on the base.

        Returns:
            dict: Payload accepted by :func:`tokenpipe.process`.
        """
        value = {
            "output": output,
            "session_id": "session-fold",
            "tool_call_id": "call-fold",
            "tool_name": "exec_command",
            "command": "cat inventory.json",
            "exit_status": 0,
        }
        value.update(extra)
        return value

    def folded(self, value):
        """Sanitize a parsed value through lite_json and re-parse it.

        Args:
            value (object): Parsed JSON value to fold.

        Returns:
            object: The re-parsed sanitized value.
        """
        return json.loads(tokenpipe.lite_json(json.dumps(value)))

    def test_folds_six_homogeneous_objects_but_not_five(self):
        """Homogeneous folding starts at six items; five stay complete."""
        six = self.folded({"items": homogeneous(6)})
        self.assertEqual(len(six["items"]), 4)
        self.assertEqual(set(six["items"][2]), {"__tokenpipe_similar_items__", "keys"})
        five = self.folded({"items": homogeneous(5)})
        self.assertEqual(five["items"], homogeneous(5))

    def test_mixed_key_sets_are_not_folded(self):
        """Non-homogeneous arrays keep the pre-folding list behaviour."""
        mixed = [{"alpha": index, "beta": index} for index in range(6)]
        mixed[3] = {"alpha": 3, "gamma": 3}
        short = self.folded({"items": mixed})
        self.assertEqual(len(short["items"]), 6)
        self.assertNotIn("__tokenpipe_similar_items__", json.dumps(short))
        long_mixed = [
            {"alpha": index, "beta": index} if index % 2 else {"alpha": index, "gamma": index}
            for index in range(40)
        ]
        folded_long = self.folded({"items": long_mixed})
        self.assertNotIn("__tokenpipe_similar_items__", json.dumps(folded_long))
        self.assertIn("__tokenpipe_omitted_items__", json.dumps(folded_long))
        scalars = self.folded({"items": list(range(6))})
        self.assertEqual(scalars["items"], list(range(6)))

    def test_marker_carries_exact_omitted_count_and_sorted_keys(self):
        """The fold marker reports the exact omitted count and sorted keys."""
        items = homogeneous(41, keys=("zulu", "alpha", "mike"))
        folded = self.folded({"items": items})
        marker = folded["items"][2]
        self.assertEqual(marker["__tokenpipe_similar_items__"], 38)
        self.assertEqual(marker["keys"], ["alpha", "mike", "zulu"])
        self.assertEqual(folded["items"][0], items[0])
        self.assertEqual(folded["items"][1], items[1])
        self.assertEqual(folded["items"][3], items[-1])

    def test_nested_arrays_fold_recursively(self):
        """Homogeneous arrays nested inside kept items fold as well."""
        value = {
            "hosts": [
                {
                    "id": index,
                    "checks": [
                        {"name": "disk", "ok": True, "value": step}
                        for step in range(9)
                    ],
                }
                for index in range(10)
            ]
        }
        hosts = self.folded(value)["hosts"]
        self.assertEqual(len(hosts), 4)
        self.assertEqual(hosts[2]["__tokenpipe_similar_items__"], 7)
        self.assertEqual(hosts[2]["keys"], ["checks", "id"])
        checks = hosts[0]["checks"]
        self.assertEqual(len(checks), 4)
        self.assertEqual(
            checks[2], {"__tokenpipe_similar_items__": 6, "keys": ["name", "ok", "value"]}
        )
        self.assertEqual(checks[3]["value"], 8)

    def test_output_is_valid_json_and_shorter(self):
        """Folded output round-trips through json.loads and shrinks the text."""
        raw = json.dumps({"items": homogeneous(60)}, indent=2, sort_keys=True)
        folded_text = tokenpipe.lite_json(raw)
        json.loads(folded_text)
        self.assertLess(len(folded_text), len(raw))
        self.assertLess(
            tokenpipe.estimate_tokens(folded_text), tokenpipe.estimate_tokens(raw)
        )

    def test_process_end_to_end_folds_and_spools_raw(self):
        """A large homogeneous payload is replaced, folded, and recoverable."""
        raw = json.dumps({"schema": "inventory/v1", "hosts": [
            {"host": "node-%03d" % index, "region": "us-east-%d" % (index % 3),
             "checks": [{"name": name, "ok": True, "value": index + step}
                         for step, name in enumerate(("disk", "memory", "cpu", "load", "io", "net", "gpu"))]}
            for index in range(48)]}, indent=2)
        result = tokenpipe.process(self.payload(raw), "safe")
        self.assertEqual(result["action"], "replace")
        self.assertEqual(result["strategy"], "lite-json")
        self.assertIsNotNone(result["raw_ref"])
        json.loads(result["output"])
        self.assertIn("__tokenpipe_similar_items__", result["output"])
        self.assertLess(len(result["output"]), len(raw))
        self.assertEqual(tokenpipe.show_raw(result["raw_ref"]), raw)


class LabJsonRouteTests(unittest.TestCase):
    """Route-to-stage mapping for JSON command-matrix cases."""

    def test_json_suffixed_routes_get_the_json_stage(self):
        """``rtk-json`` and any ``-json`` route must evaluate ``json-lite``."""
        for route in ("json", "rtk-json", "tool-json"):
            case = lab.Case("route-probe", "json", route, 1, 0, "{}")
            stages = lab._applicable_stages(case)
            self.assertIn("json-lite", stages)
            self.assertNotIn("log-lite", stages)

    def test_lab_schema_gate_counts_similar_item_markers(self):
        """Folded markers must still satisfy array-count expectations."""
        case = next(item for item in lab.corpus() if item.case_id == "inventory-json")
        folded = lab.apply_order(case.raw, ("json-lite",))
        self.assertTrue(lab.schema_check(case, folded)["valid"])


if __name__ == "__main__":
    unittest.main()
