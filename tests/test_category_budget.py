"""Per-category shown budgets and the ``show --range`` recovery preview.

Bounding uses one budget per content category instead of a single global
number: :data:`tokenpipe.SHOWN_BUDGET_CHARS` holds the defaults,
``TOKENPIPE_BUDGET_<CATEGORY>`` overrides one category, and
``TOKENPIPE_MAX_SHOWN_CHARS`` remains the ceiling every budget is clamped to.
When bounding elides a middle section, the recovery preview names the omitted
character count and the exact ``show <raw_ref> --range START:END`` command that
prints it back.
"""

import contextlib
import importlib.util
import io
import os
import re
import tempfile
import unittest


SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "tokenpipe.py")
SPEC = importlib.util.spec_from_file_location("tokenpipe", SCRIPT)
tokenpipe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tokenpipe)

PREVIEW_RE = re.compile(
    r"omitted (\d+) chars; fetch the elided middle with: (.+) --range (\d+):(\d+)$")


def big_code():
    """Build source-like output far larger than any category budget.

    Returns:
        str: Python-like text of about 20 kB, classified as ``code`` so the
        compressor keeps it verbatim and only bounding shortens it.
    """
    return "\n".join(
        "def generated_%03d(value):\n    total = value + %d\n    return total" % (index, index)
        for index in range(300)
    ) + "\n"


class CategoryBudgetTests(unittest.TestCase):
    """Cover budget resolution, its overrides, and the recovery preview."""

    def setUp(self):
        """Isolate private state and clear inherited budget overrides."""
        self.temp = tempfile.TemporaryDirectory()
        self.old_env = os.environ.copy()
        for name in list(os.environ):
            if name.startswith("TOKENPIPE_BUDGET_") or name == "TOKENPIPE_MAX_SHOWN_CHARS":
                del os.environ[name]
        os.environ["TOKENPIPE_HOME"] = self.temp.name
        os.environ["TOKENPIPE_RUNTIME_HOME"] = os.path.join(self.temp.name, "runtime")
        os.environ["TOKENPIPE_MIN_TOKENS_ESTIMATE"] = "10"

    def tearDown(self):
        """Restore the process environment before removing private state."""
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def payload(self, output, **extra):
        """Build one bounded hook payload for :func:`tokenpipe.process`.

        Args:
            output (str): Decoded tool output to process.
            **extra (object): Additional payload fields such as ``tool_call_id``.

        Returns:
            dict[str, object]: Payload with session, call, and tool identity.
        """
        value = {
            "output": output,
            "session_id": "session-budget",
            "tool_call_id": "call-budget",
            "tool_name": "exec_command",
            "exit_status": 0,
        }
        value.update(extra)
        return value

    def run_cli(self, argv):
        """Run one CLI invocation and capture both of its streams.

        Args:
            argv (list[str]): Argument vector without the program name.

        Returns:
            tuple[int, str, str]: Exit status, captured stdout, captured stderr.
        """
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = tokenpipe.main(argv)
        return status, out.getvalue(), err.getvalue()

    def test_default_table_stays_within_the_documented_host_cap(self):
        """Every default budget fits the tightest documented host output cap."""
        self.assertTrue(tokenpipe.SHOWN_BUDGET_CHARS)
        for category, budget in tokenpipe.SHOWN_BUDGET_CHARS.items():
            self.assertGreaterEqual(budget, 256, category)
            self.assertLessEqual(budget, 7000, category)

    def test_bound_candidate_uses_the_budget_of_its_category(self):
        """Each category bounds to its own table entry, not one global number."""
        text = "x" * 40000
        for category, budget in tokenpipe.SHOWN_BUDGET_CHARS.items():
            bounded = tokenpipe.bound_candidate(text, category=category)
            self.assertLessEqual(len(bounded), budget, category)
            self.assertGreater(len(bounded), budget - 200, category)
        self.assertLess(
            len(tokenpipe.bound_candidate(text, category="plain")),
            len(tokenpipe.bound_candidate(text, category="error")),
        )

    def test_missing_category_keeps_the_previous_global_behaviour(self):
        """Without a category the global cap and explicit budgets still rule."""
        text = "y" * 40000
        self.assertEqual(tokenpipe.shown_budget(), 7000)
        self.assertEqual(tokenpipe.shown_budget("no-such-category"), 7000)
        self.assertLessEqual(len(tokenpipe.bound_candidate(text)), 7000)
        self.assertGreater(len(tokenpipe.bound_candidate(text)), 6800)
        explicit = tokenpipe.bound_candidate(text, max_chars=1400)
        self.assertLessEqual(len(explicit), 1400)
        self.assertGreater(len(explicit), 1200)
        # An explicit budget still wins over the category table.
        self.assertEqual(explicit, tokenpipe.bound_candidate(text, 1400, category="error"))

    def test_env_override_applies_and_is_clamped_both_ways(self):
        """One category is overridable and clamped to [256, global cap]."""
        os.environ["TOKENPIPE_BUDGET_ERROR"] = "4000"
        self.assertEqual(tokenpipe.shown_budget("error"), 4000)
        self.assertEqual(tokenpipe.shown_budget("log"), 6000)
        self.assertLessEqual(len(tokenpipe.bound_candidate("z" * 40000, category="error")), 4000)
        os.environ["TOKENPIPE_BUDGET_ERROR"] = "10"
        self.assertEqual(tokenpipe.shown_budget("error"), 256)
        os.environ["TOKENPIPE_BUDGET_ERROR"] = "999999"
        self.assertEqual(tokenpipe.shown_budget("error"), 7000)
        os.environ["TOKENPIPE_BUDGET_ERROR"] = "not-a-number"
        self.assertEqual(tokenpipe.shown_budget("error"), 7000)

    def test_global_cap_still_wins(self):
        """The global ceiling clamps both defaults and category overrides."""
        os.environ["TOKENPIPE_MAX_SHOWN_CHARS"] = "1200"
        self.assertEqual(tokenpipe.shown_budget("error"), 1200)
        self.assertEqual(tokenpipe.shown_budget("plain"), 1200)
        os.environ["TOKENPIPE_BUDGET_ERROR"] = "5000"
        self.assertEqual(tokenpipe.shown_budget("error"), 1200)
        self.assertLessEqual(len(tokenpipe.bound_candidate("q" * 40000, category="error")), 1200)
        os.environ["TOKENPIPE_MAX_SHOWN_CHARS"] = "unusable"
        self.assertEqual(tokenpipe.shown_budget("plain"), 5000)

    def test_process_bounds_by_category_and_records_budget_chars(self):
        """The metric row reports the budget that bounded the shown output."""
        result = tokenpipe.process(self.payload(big_code()), "safe", record_metric=False)
        self.assertEqual(result["action"], "replace")
        self.assertEqual(result["content_category"], "code")
        self.assertEqual(result["_metric"]["budget_chars"], 7000)
        self.assertLessEqual(len(result["output"]), 7000)
        os.environ["TOKENPIPE_BUDGET_CODE"] = "2000"
        narrowed = tokenpipe.process(
            self.payload(big_code(), tool_call_id="call-narrow"), "safe", record_metric=False)
        self.assertEqual(narrowed["_metric"]["budget_chars"], 2000)
        self.assertLessEqual(len(narrowed["output"]), 2000)

    def test_preview_reports_the_exact_omitted_span(self):
        """The preview count and range describe the middle that was elided."""
        original = big_code()
        result = tokenpipe.process(self.payload(original), "safe", record_metric=False)
        shown, raw_ref = result["output"], result["raw_ref"]
        match = PREVIEW_RE.search(result["recovery_preview"])
        self.assertIsNotNone(match, result["recovery_preview"])
        omitted, start, end = int(match.group(1)), int(match.group(3)), int(match.group(4))
        self.assertEqual(omitted, end - start)
        marker = tokenpipe.BOUND_MARKER_RE.search(shown)
        self.assertIsNotNone(marker)
        self.assertEqual(shown[:marker.start()] + shown[marker.end():],
                         original[:start] + original[end:])
        self.assertIn(" show " + raw_ref + " --range %d:%d" % (start, end),
                      result["recovery_preview"])
        status, out, _ = self.run_cli(["show", raw_ref, "--range", "%d:%d" % (start, end)])
        self.assertEqual(status, 0)
        self.assertEqual(out, original[start:end])

    def test_preview_is_empty_without_bounding_or_recovery(self):
        """Unbounded output, or output with no raw copy, previews nothing."""
        self.assertEqual(tokenpipe.recovery_preview("abc", "abc", "/tmp/raw.log"), "")
        bounded = tokenpipe.bound_candidate("w" * 40000, category="plain")
        self.assertEqual(tokenpipe.recovery_preview(bounded, "w" * 40000, None), "")
        # A marker that only occurs inside the content describes no real span.
        text = "head" + tokenpipe.BOUND_MARKER_TEMPLATE % 12 + "tail"
        self.assertEqual(tokenpipe.recovery_preview(text, text + "!", "/tmp/raw.log"), "")

    def test_show_range_returns_exact_characters(self):
        """``show --range`` prints exactly the requested half-open slice."""
        original = "".join("line %04d of recoverable output\n" % index for index in range(200))
        raw_ref = tokenpipe.spool_raw(original, "session-budget", "call-show")
        status, out, _ = self.run_cli(["show", raw_ref, "--range", "100:250"])
        self.assertEqual(status, 0)
        self.assertEqual(out, original[100:250])
        status, out, _ = self.run_cli(["show", raw_ref])
        self.assertEqual(status, 0)
        self.assertEqual(out, original)
        # Offsets past the end clamp exactly like a Python slice.
        status, out, _ = self.run_cli(
            ["show", raw_ref, "--range", "%d:%d" % (len(original) - 5, len(original) + 500)])
        self.assertEqual(status, 0)
        self.assertEqual(out, original[-5:])

    def test_show_range_rejects_malformed_ranges(self):
        """Malformed ranges fail with a clear message and exit status 2."""
        raw_ref = tokenpipe.spool_raw("recoverable", "session-budget", "call-bad")
        for value in ("250:100", "100:100", "100", "100:", "a:b", "1:2:3", "", "-5:10"):
            status, out, err = self.run_cli(["show", raw_ref, "--range=" + value])
            self.assertEqual(status, 2, value)
            self.assertEqual(out, "", value)
            self.assertIn("--range", err)

    def test_native_header_carries_the_shared_preview(self):
        """The native header appends the preview after its marker fields."""
        header = tokenpipe._native_header("search", 0, "safe", "bounded-code", "/tmp/raw.log", "")
        self.assertTrue(header.endswith("raw_ref=/tmp/raw.log\n"))
        preview = "omitted 5 chars; fetch the elided middle with: p show /tmp/raw.log --range 1:6"
        with_preview = tokenpipe._native_header(
            "search", 0, "safe", "bounded-code", "/tmp/raw.log", preview)
        self.assertEqual(with_preview, header[:-1] + "; " + preview + "\n")

    def test_host_headers_carry_the_shared_preview(self):
        """Both host renderers, not the hooks, append the recovery preview."""
        preview = "omitted 5 chars; fetch the elided middle with: p show /tmp/raw.log --range 1:6"
        codex = tokenpipe.post_recovery_header("safe", "bounded-code", 0, "/tmp/raw.log")
        self.assertTrue(codex.endswith("raw_ref=/tmp/raw.log"))
        self.assertEqual(
            tokenpipe.post_recovery_header("safe", "bounded-code", 0, "/tmp/raw.log", preview),
            codex + "; " + preview)
        claude = tokenpipe.claude_recovery_header(["stdout raw_ref=/tmp/raw.log"], "p show")
        self.assertEqual(
            tokenpipe.claude_recovery_header(
                ["stdout raw_ref=/tmp/raw.log"], "p show",
                previews=["stdout " + preview, ""]),
            claude + "; stdout " + preview)


if __name__ == "__main__":
    unittest.main()
