"""Regression coverage for masked log-template collapsing in ``lite_log``."""

import importlib.util
import os
import tempfile
import unittest


SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "tokenpipe.py")
SPEC = importlib.util.spec_from_file_location("tokenpipe", SCRIPT)
tokenpipe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tokenpipe)


def legacy_lite_log(text):
    """Reproduce the adjacent-only collapse that preceded template masking.

    Args:
        text (str): Decoded log output, as passed to :func:`tokenpipe.lite_log`.

    Returns:
        str: ANSI-stripped, LF-normalized text with byte-identical adjacent
        lines collapsed and blank runs squeezed, and nothing else removed.

    Used only to show the reduction the released transform achieved on a
    varying-timestamp log; it is test scaffolding, not production behavior.
    """
    text = tokenpipe.ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    output = []
    previous = None
    repeated = 0
    blanks = 0
    for line in [item.rstrip() for item in text.split("\n")]:
        if not line.strip():
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        if line == previous and line.strip():
            repeated += 1
            continue
        if repeated:
            output.append("[previous line repeated %d more times]" % repeated)
        repeated = 0
        output.append(line)
        previous = line
    if repeated:
        output.append("[previous line repeated %d more times]" % repeated)
    return "\n".join(output).strip()


def varying_log(rounds=40):
    """Build a deterministic log whose lines are all byte-distinct.

    Args:
        rounds (int): Number of passes over the message templates; each pass
            emits four lines with fresh timestamps, hex ids, and durations.

    Returns:
        str: Log text with monotonically increasing ``HH:MM:SS.mmm`` stamps, so
        :func:`tokenpipe.classify` reports ``log`` and no two lines are
        byte-identical.
    """
    lines = []
    for index in range(rounds):
        stamp = "12:%02d:%02d.%03d" % (index // 60, index % 60, index)
        lines.append("%s INFO fetched blob %08x in %dms" % (stamp, index * 7919, index % 40))
        lines.append("%s INFO cache hit ratio %d.%d%%" % (stamp, 90 + index % 9, index % 10))
        lines.append("%s DEBUG mapped region 0x%08x size %d.%dMB" % (stamp, index * 4096, index % 7, index % 10))
        lines.append("%s INFO worker %d picked up job 550e8400-e29b-41d4-a716-4466554400%02d"
                     % (stamp, index % 4, index % 100))
    return "\n".join(lines) + "\n"


class LogTemplateKeyTests(unittest.TestCase):
    """Cover which fields ``_log_template_key`` masks and which it must not."""

    def test_volatile_fields_are_masked_in_the_key(self):
        """Timestamps, ids, durations, sizes, and percentages become placeholders."""
        key = tokenpipe._log_template_key(
            "2026-01-01T00:00:03Z INFO job 550e8400-e29b-41d4-a716-446655440000 "
            "blob a3f9c1d4e5b6 took 12ms then 3.4s at 0:00:01 using 12.3MB "
            "cache 45% addr 0x7ffe1234"
        )
        self.assertEqual(
            key,
            "<timestamp> INFO job <uuid> blob <id> took <duration> then <duration> "
            "at <time> using <size> cache <percent> addr <addr>",
        )

    def test_status_codes_and_small_integers_are_not_masked(self):
        """Meaningful numbers survive, so unlike lines keep unlike keys."""
        first = tokenpipe._log_template_key("12:00:01 GET /orders status=200 retries=3 exit=1")
        second = tokenpipe._log_template_key("12:00:02 GET /orders status=500 retries=3 exit=1")
        self.assertEqual(first, "<time> GET /orders status=200 retries=3 exit=1")
        self.assertNotEqual(first, second)

    def test_lines_differing_only_by_status_code_stay_separate(self):
        """A 200 and a 500 line are two templates, never one collapsed line."""
        text = "12:00:01 GET /orders status=200\n12:00:02 GET /orders status=500\n12:00:03 done\n"
        result = tokenpipe.lite_log(text)
        self.assertIn("status=200", result)
        self.assertIn("status=500", result)
        self.assertNotIn("[seen", result)

    def test_masking_never_reaches_the_output_text(self):
        """Emitted lines stay verbatim; only the comparison key is masked."""
        text = "12:00:01 INFO started run 0x7ffe1234 in 12ms\n12:00:02 INFO started run 0xabcd0001 in 15ms\n"
        result = tokenpipe.lite_log(text)
        self.assertNotIn("<timestamp>", result)
        self.assertNotIn("<duration>", result)
        self.assertIn("12:00:01 INFO started run 0x7ffe1234 in 12ms", result)


class LiteLogCollapseTests(unittest.TestCase):
    """Cover which lines ``lite_log`` drops, keeps, and marks."""

    def test_non_adjacent_near_repeats_collapse_with_exact_count(self):
        """Later occurrences drop and the first one records the total count."""
        text = "".join(
            "12:00:%02d INFO polled queue depth 4\n12:00:%02d INFO wrote chunk %08x\n" % (index, index, index)
            for index in range(5)
        )
        lines = tokenpipe.lite_log(text).split("\n")
        self.assertEqual(lines[0], "12:00:00 INFO polled queue depth 4 [seen 5 times]")
        self.assertEqual(lines[1], "12:00:00 INFO wrote chunk 00000000 [seen 5 times]")
        self.assertEqual(lines[2], "12:00:04 INFO wrote chunk 00000004")
        self.assertEqual(len(lines), 3)

    def test_first_occurrence_stays_verbatim(self):
        """The kept line is the untouched original plus the trailing marker."""
        text = "".join("12:00:%02d INFO scanned 0x%04x pages\n" % (index, index) for index in range(4))
        first = tokenpipe.lite_log(text).split("\n")[0]
        self.assertEqual(first, "12:00:00 INFO scanned 0x0000 pages [seen 4 times]")

    def test_error_and_summary_lines_are_never_dropped(self):
        """Failure and summary evidence survives even as an exact near-repeat."""
        text = "".join(
            "12:00:%02d INFO heartbeat %08x\n"
            "12:00:%02d ERROR upload failed after 12ms\n"
            "12:00:%02d 3 tests passed\n" % (index, index, index, index)
            for index in range(3)
        )
        result = tokenpipe.lite_log(text)
        self.assertEqual(result.count("ERROR upload failed"), 3)
        self.assertEqual(result.count("3 tests passed"), 3)
        self.assertEqual(result.count("INFO heartbeat"), 1)

    def test_last_line_is_always_kept(self):
        """A trailing near-repeat is emitted so the tail of the log survives."""
        text = "".join("12:00:%02d INFO tail marker %08x\n" % (index, index) for index in range(3))
        lines = tokenpipe.lite_log(text).split("\n")
        self.assertEqual(lines[0], "12:00:00 INFO tail marker 00000000 [seen 3 times]")
        self.assertEqual(lines[-1], "12:00:02 INFO tail marker 00000002")

    def test_adjacent_repeat_behaviour_is_unchanged(self):
        """Byte-identical neighbours still collapse with the released marker."""
        self.assertEqual(
            tokenpipe.lite_log("same log line\n" * 500),
            "same log line\n[previous line repeated 499 more times]",
        )
        self.assertEqual(tokenpipe.lite_log("alpha\n\n\n\nbeta\n"), "alpha\n\nbeta")

    def test_transform_holds_no_state_across_calls(self):
        """Repeated calls on the same input return the same output."""
        text = varying_log(6)
        self.assertEqual(tokenpipe.lite_log(text), tokenpipe.lite_log(text))


class ProcessLogReductionTests(unittest.TestCase):
    """Cover end-to-end ``process`` savings on a varying-timestamp log."""

    def setUp(self):
        """Redirect private state, spool, and metrics into a temporary home."""
        self.temp = tempfile.TemporaryDirectory()
        self.old_env = os.environ.copy()
        os.environ["TOKENPIPE_HOME"] = self.temp.name
        os.environ["TOKENPIPE_RUNTIME_HOME"] = os.path.join(self.temp.name, "runtime")
        os.environ["TOKENPIPE_MIN_TOKENS_ESTIMATE"] = "10"

    def tearDown(self):
        """Restore the process environment and delete the temporary home."""
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def test_varying_timestamp_log_now_compresses_where_it_previously_could_not(self):
        """End-to-end replacement yields a raw ref and well over 30% savings."""
        raw = varying_log()
        self.assertEqual(tokenpipe.classify(raw), "log")
        normalized = tokenpipe.ANSI_RE.sub("", raw).replace("\r\n", "\n").replace("\r", "\n").strip()
        self.assertEqual(legacy_lite_log(raw), normalized)

        payload = {
            "output": raw,
            "session_id": "session-log-masking",
            "tool_call_id": "call-log-masking",
            "tool_name": "exec_command",
        }
        result = tokenpipe.process(payload, "safe")
        self.assertEqual(result["action"], "replace")
        self.assertEqual(result["strategy"], "lite-log")
        self.assertIsNotNone(result["raw_ref"])
        self.assertEqual(tokenpipe.show_raw(result["raw_ref"]), raw)
        self.assertGreaterEqual(result["saved_percent"], 30.0)
        self.assertLess(len(result["output"]), len(raw) * 0.7)


if __name__ == "__main__":
    unittest.main()
