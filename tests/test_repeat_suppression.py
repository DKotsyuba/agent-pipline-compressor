"""Cross-call exact-repeat suppression: measurement, gating, and privacy."""

import importlib.util
import json
import os
import stat
import tempfile
import time
import unittest


SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "tokenpipe.py"
)
SPEC = importlib.util.spec_from_file_location("tokenpipe_repeat_under_test", SCRIPT)
tokenpipe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tokenpipe)


class RepeatSuppressionTests(unittest.TestCase):
    """Behavior of the private repeat index and the notice replacement gate."""

    def setUp(self):
        """Point every private root at a throwaway home for this test only."""
        self.temp = tempfile.TemporaryDirectory()
        self.old_env = os.environ.copy()
        os.environ["TOKENPIPE_HOME"] = self.temp.name
        os.environ["TOKENPIPE_RUNTIME_HOME"] = os.path.join(self.temp.name, "runtime")
        os.environ["TOKENPIPE_MIN_TOKENS_ESTIMATE"] = "10"

    def tearDown(self):
        """Restore the process environment and delete the throwaway home."""
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def payload(self, output, **extra):
        """Build one bounded hook payload with a stable repeat identity.

        Args:
            output (str): Tool output text placed in the payload.
            **extra (object): Field overrides such as ``session_id``.

        Returns:
            dict[str, object]: A fresh payload; callers may mutate it freely.
        """
        value = {
            "output": output,
            "session_id": "session-repeat",
            "tool_call_id": "call-one",
            "tool_name": "exec_command",
            "command": "pytest tests -q",
            "exit_status": 0,
        }
        value.update(extra)
        return value

    def sample(self, marker="same log line"):
        """Return a compressible log body large enough to pass the threshold.

        Args:
            marker (str): Line body repeated to build the output.

        Returns:
            str: Deterministic multi-line log text.
        """
        return (marker + "\n") * 200

    def read_index(self):
        """Return the parsed private repeat index for the active home.

        Returns:
            dict[str, object]: Parsed index contents.

        Raises:
            OSError: The index has not been written yet.
        """
        with open(tokenpipe._repeat_index_path(), "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_repeat_is_flagged_but_output_is_unchanged_while_gate_is_off(self):
        raw = self.sample()
        first = tokenpipe.process(self.payload(raw), "audit")
        second = tokenpipe.process(self.payload(raw, tool_call_id="call-two"), "audit")
        self.assertEqual(second["output"], raw)
        self.assertEqual(second["action"], "passthrough")
        self.assertNotIn("identical to previous output", second["output"])
        rows = tokenpipe.load_metrics()
        self.assertFalse(rows[0]["repeat_of_previous"])
        self.assertTrue(rows[1]["repeat_of_previous"])
        # The counterfactual measures the notice the mechanism would have shown,
        # not the compressed candidate the first call was estimated against.
        self.assertEqual(
            second["counterfactual_tokens_estimate"],
            tokenpipe.estimate_tokens(
                tokenpipe._repeat_notice(len(raw.encode("utf-8")), None)),
        )
        self.assertLess(
            second["counterfactual_tokens_estimate"], second["original_tokens_estimate"]
        )
        self.assertNotEqual(
            second["counterfactual_tokens_estimate"], first["counterfactual_tokens_estimate"]
        )

    def test_gate_off_keeps_ordinary_compression_for_a_repeat(self):
        raw = self.sample()
        tokenpipe.process(self.payload(raw), "safe")
        second = tokenpipe.process(self.payload(raw, tool_call_id="call-two"), "safe")
        self.assertEqual(second["action"], "replace")
        self.assertEqual(second["strategy"], "lite-log")
        self.assertNotIn("identical to previous output", second["output"])
        self.assertTrue(tokenpipe.load_metrics()[-1]["repeat_of_previous"])

    def test_gate_on_in_safe_mode_shows_notice_with_recoverable_raw_ref(self):
        os.environ["TOKENPIPE_REPEAT_REPLACE"] = "1"
        raw = self.sample()
        first = tokenpipe.process(self.payload(raw), "safe")
        self.assertEqual(first["action"], "replace")
        second = tokenpipe.process(self.payload(raw, tool_call_id="call-two"), "safe")
        self.assertEqual(second["action"], "replace")
        self.assertEqual(second["strategy"], "repeat-notice")
        self.assertEqual(
            second["output"],
            "[tokenpipe] identical to previous output (%d bytes); raw_ref=%s"
            % (len(raw.encode("utf-8")), second["raw_ref"]),
        )
        self.assertEqual(tokenpipe.show_raw(second["raw_ref"]), raw)
        self.assertNotEqual(second["raw_ref"], first["raw_ref"])
        self.assertLess(second["shown_tokens_estimate"], second["original_tokens_estimate"])

    def test_gate_on_never_replaces_in_audit_mode(self):
        os.environ["TOKENPIPE_REPEAT_REPLACE"] = "1"
        raw = self.sample()
        tokenpipe.process(self.payload(raw), "audit")
        second = tokenpipe.process(self.payload(raw, tool_call_id="call-two"), "audit")
        self.assertEqual(second["output"], raw)
        self.assertTrue(tokenpipe.load_metrics()[-1]["repeat_of_previous"])

    def test_persisted_gate_is_private_and_enables_replacement(self):
        self.assertFalse(tokenpipe.configured_repeat_replace())
        tokenpipe.set_repeat_replace("1")
        self.assertTrue(tokenpipe.configured_repeat_replace())
        self.assertEqual(stat.S_IMODE(os.stat(tokenpipe._config_path()).st_mode), 0o600)
        raw = self.sample()
        tokenpipe.process(self.payload(raw), "safe")
        second = tokenpipe.process(self.payload(raw, tool_call_id="call-two"), "safe")
        self.assertEqual(second["strategy"], "repeat-notice")
        tokenpipe.set_repeat_replace("off")
        self.assertFalse(tokenpipe.configured_repeat_replace())

    def test_changed_bytes_receive_full_processing(self):
        os.environ["TOKENPIPE_REPEAT_REPLACE"] = "1"
        tokenpipe.process(self.payload(self.sample()), "safe")
        second = tokenpipe.process(
            self.payload(self.sample("other log line"), tool_call_id="call-two"), "safe")
        self.assertEqual(second["strategy"], "lite-log")
        self.assertIn("other log line", second["output"])
        self.assertFalse(tokenpipe.load_metrics()[-1]["repeat_of_previous"])

    def test_a_different_session_is_not_a_repeat(self):
        os.environ["TOKENPIPE_REPEAT_REPLACE"] = "1"
        raw = self.sample()
        tokenpipe.process(self.payload(raw), "safe")
        second = tokenpipe.process(self.payload(raw, session_id="other-session"), "safe")
        self.assertEqual(second["strategy"], "lite-log")
        self.assertFalse(tokenpipe.load_metrics()[-1]["repeat_of_previous"])

    def test_missing_and_corrupt_index_fail_open(self):
        os.environ["TOKENPIPE_REPEAT_REPLACE"] = "1"
        raw = self.sample()
        # Missing index: the first call must behave exactly as before.
        first = tokenpipe.process(self.payload(raw), "safe")
        self.assertEqual(first["strategy"], "lite-log")
        with open(tokenpipe._repeat_index_path(), "w", encoding="utf-8") as handle:
            handle.write('{"broken": ')
        second = tokenpipe.process(self.payload(raw, tool_call_id="call-two"), "safe")
        self.assertEqual(second["strategy"], "lite-log")
        self.assertFalse(tokenpipe.load_metrics()[-1]["repeat_of_previous"])
        # An index that is not a JSON object is ignored just as safely.
        with open(tokenpipe._repeat_index_path(), "w", encoding="utf-8") as handle:
            handle.write("[]")
        third = tokenpipe.process(self.payload(raw, tool_call_id="call-three"), "safe")
        self.assertEqual(third["strategy"], "lite-log")
        self.assertFalse(tokenpipe.load_metrics()[-1]["repeat_of_previous"])

    def test_unrecoverable_previous_raw_falls_back_to_compression(self):
        os.environ["TOKENPIPE_REPEAT_REPLACE"] = "1"
        raw = self.sample()
        first = tokenpipe.process(self.payload(raw), "safe")
        os.unlink(first["raw_ref"])
        second = tokenpipe.process(self.payload(raw, tool_call_id="call-two"), "safe")
        self.assertEqual(second["strategy"], "lite-log")
        self.assertTrue(tokenpipe.load_metrics()[-1]["repeat_of_previous"])

    def test_expired_index_entries_are_ignored_and_pruned(self):
        os.environ["TOKENPIPE_REPEAT_REPLACE"] = "1"
        raw = self.sample()
        tokenpipe.process(self.payload(raw), "safe")
        index = self.read_index()
        self.assertEqual(len(index), 1)
        identity = next(iter(index))
        index[identity]["timestamp"] = time.time() - 100
        with open(tokenpipe._repeat_index_path(), "w", encoding="utf-8") as handle:
            json.dump(index, handle)
        os.environ["TOKENPIPE_RAW_TTL_SECONDS"] = "50"
        second = tokenpipe.process(self.payload(raw, tool_call_id="call-two"), "safe")
        self.assertEqual(second["strategy"], "lite-log")
        self.assertFalse(tokenpipe.load_metrics()[-1]["repeat_of_previous"])
        # The stale entry is replaced by the fresh one rather than accumulating.
        self.assertEqual(len(self.read_index()), 1)

    def test_index_is_private_and_free_of_raw_output(self):
        sentinel = "UNIQUE_REPEAT_SENTINEL"
        raw = self.sample(sentinel + " detail")
        tokenpipe.process(self.payload(raw), "safe")
        path = tokenpipe._repeat_index_path()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        with open(path, "r", encoding="utf-8") as handle:
            stored = handle.read()
        self.assertNotIn(sentinel, stored)
        self.assertNotIn("pytest tests", stored)
        entry = next(iter(self.read_index().values()))
        self.assertEqual(sorted(entry), ["bytes", "raw_ref", "timestamp"])
        self.assertEqual(entry["bytes"], len(raw.encode("utf-8")))

    def test_stats_summarize_repeat_events(self):
        raw = self.sample()
        tokenpipe.process(self.payload(raw), "audit")
        tokenpipe.process(self.payload(raw, tool_call_id="call-two"), "audit")
        report = tokenpipe.aggregate(tokenpipe.load_metrics())
        self.assertEqual(report["repeat_calls"], 1)
        self.assertGreater(report["repeat_saved_tokens_estimate"], 0)


if __name__ == "__main__":
    unittest.main()
