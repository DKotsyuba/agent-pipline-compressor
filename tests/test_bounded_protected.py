"""Bounded replacement policy for the protected content categories.

Above ``TOKENPIPE_MIN_TOKENS_ESTIMATE`` the ``code``, ``diff`` and ``config``
categories are head/tail bounded under a ``bounded-<category>`` strategy with a
recoverable raw copy; at or below the threshold, and for ``binary`` at any
size, output stays byte-exact.
"""

import importlib.util
import os
import re
import sys
import tempfile
import unittest


SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "tokenpipe.py")
SPEC = importlib.util.spec_from_file_location("tokenpipe", SCRIPT)
tokenpipe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tokenpipe)

BOUND_MARKER_RE = re.compile(r"\n\.\.\.\[tokenpipe bounded output; omitted (\d+) chars; use raw_ref\]\.\.\.\n")
SMALL_DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n-old\n+new\n"
SMALL_CODE = "\n".join("def small_%d():\n    return %d" % (index, index) for index in range(12)) + "\n"
SMALL_CONFIG = "[service]\nname = lab\n" + "".join("option_%02d = value_%02d\n" % (index, index) for index in range(10))


def big_diff():
    """Build a deterministic unified diff larger than the shown-character cap.

    Returns:
        str: Unified diff of about 12 kB with many hunks, classified as
        ``diff`` and estimated far above the default replacement threshold.
    """
    hunks = "".join(
        "@@ -%d,4 +%d,5 @@ def handler_%03d(request):\n     before %03d\n-    return legacy(request, %03d)\n+    return modern(validate(request), %03d)\n     after %03d\n"
        % (index * 10 + 1, index * 10 + 1, index, index, index, index, index)
        for index in range(70)
    )
    return "diff --git a/service.py b/service.py\n--- a/service.py\n+++ b/service.py\n" + hunks


def big_code():
    """Build source-like output larger than the shown-character cap.

    Returns:
        str: Python-like text classified as ``code`` and estimated above the
        default replacement threshold.
    """
    return "\n".join(
        "def generated_%03d(value):\n    total = value + %d\n    return total" % (index, index)
        for index in range(200)
    ) + "\n"


def big_config():
    """Build configuration-like output larger than the shown-character cap.

    Returns:
        str: ``key = value`` text classified as ``config`` and estimated above
        the default replacement threshold.
    """
    return "[service]\nname = lab\n" + "".join(
        "option_%03d = value_%03d_with_padding_for_size\n" % (index, index) for index in range(250)
    )


class BoundedProtectedTests(unittest.TestCase):
    """Cover the size-dependent protection policy on both processing paths."""

    def setUp(self):
        """Isolate private state and trust the fixture executable directory."""
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
        """Build one bounded hook payload for :func:`tokenpipe.process`.

        Args:
            output (str): Decoded tool output to process.
            **extra (object): Additional payload fields such as
                ``replace_categories`` or ``tool_call_id``.

        Returns:
            dict[str, object]: Payload with session, call, and tool identity.
        """
        value = {
            "output": output,
            "session_id": "session-bounded",
            "tool_call_id": "call-bounded",
            "tool_name": "exec_command",
            "exit_status": 0,
        }
        value.update(extra)
        return value

    def executable(self, name, body):
        """Write one trusted fixture executable that prints fixed output.

        Args:
            name (str): File name, which also selects the native argv category.
            body (str): Python source executed by the fixture interpreter.

        Returns:
            str: Absolute path to the created ``0700`` executable.
        """
        path = os.path.join(self.temp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!%s\n" % sys.executable)
            handle.write(body)
        os.chmod(path, 0o700)
        return path

    def assert_bounded(self, original, shown):
        """Assert that shown keeps the verbatim head and tail of original.

        Args:
            original (str): Exact pre-replacement text.
            shown (str): Replacement text expected to carry one omission marker
                between a verbatim prefix and a verbatim suffix of ``original``.

        Returns:
            None: Deviations are reported as ``unittest`` assertion failures.
        """
        match = BOUND_MARKER_RE.search(shown)
        self.assertIsNotNone(match, "omission marker missing from bounded output")
        head, tail = shown[:match.start()], shown[match.end():]
        self.assertTrue(head and original.startswith(head), "head is not verbatim")
        self.assertTrue(tail and original.endswith(tail), "tail is not verbatim")
        self.assertLess(len(shown), len(original))
        self.assertEqual(int(match.group(1)), len(original) - len(head) - len(tail))

    def test_small_protected_output_stays_byte_exact_below_threshold(self):
        """Protected output under the threshold keeps its exact skip reason."""
        os.environ["TOKENPIPE_MIN_TOKENS_ESTIMATE"] = "100000"
        for sample in (SMALL_DIFF, SMALL_CODE, SMALL_CONFIG):
            result = tokenpipe.process(self.payload(sample), "safe")
            self.assertEqual(result["action"], "passthrough")
            self.assertEqual(result["output"], sample)
            self.assertEqual(result["skip_reason"], "below-threshold")
            self.assertIsNone(result["raw_ref"])

    def test_protected_output_within_the_character_cap_reports_no_savings(self):
        """Bounding that cannot shrink output leaves the original untouched."""
        for sample in (SMALL_DIFF * 20, SMALL_CODE, SMALL_CONFIG):
            result = tokenpipe.process(self.payload(sample), "safe")
            self.assertEqual(result["action"], "passthrough")
            self.assertEqual(result["output"], sample)
            self.assertEqual(result["skip_reason"], "no-savings")
            self.assertIsNone(result["raw_ref"])

    def test_large_protected_output_is_bounded_with_recoverable_raw(self):
        """Large code/diff/config bound down and stay byte-for-byte recoverable."""
        for category, sample in (("diff", big_diff()), ("code", big_code()), ("config", big_config())):
            self.assertEqual(tokenpipe.classify(sample), category)
            payload = self.payload(sample, replace_categories=[category], tool_call_id="call-" + category)
            result = tokenpipe.process(payload, "safe")
            self.assertEqual(result["action"], "replace")
            self.assertEqual(result["strategy"], "bounded-" + category)
            self.assertIsNone(result["skip_reason"])
            self.assertTrue(result["raw_ref"])
            self.assertEqual(tokenpipe.show_raw(result["raw_ref"]), sample)
            self.assert_bounded(sample, result["output"])
            self.assertLess(result["shown_tokens_estimate"], result["original_tokens_estimate"])

    def test_category_gate_suppresses_bounding_but_keeps_counterfactual(self):
        """A gate excluding diff passes output through without a raw copy."""
        sample = big_diff()
        result = tokenpipe.process(self.payload(sample, replace_categories=["json", "log"]), "safe")
        self.assertEqual(result["action"], "passthrough")
        self.assertEqual(result["output"], sample)
        self.assertEqual(result["skip_reason"], "category-gated")
        self.assertIsNone(result["raw_ref"])
        self.assertLess(result["counterfactual_tokens_estimate"], result["original_tokens_estimate"])

    def test_audit_mode_measures_bounding_without_changing_output(self):
        """Audit mode reports the bounded counterfactual and replaces nothing."""
        sample = big_diff()
        result = tokenpipe.process(self.payload(sample), "audit")
        self.assertEqual(result["action"], "passthrough")
        self.assertEqual(result["output"], sample)
        self.assertIsNone(result["raw_ref"])
        self.assertEqual(result["strategy"], "bounded-diff")
        self.assertLess(result["counterfactual_tokens_estimate"], result["original_tokens_estimate"])

    def test_binary_output_is_never_bounded(self):
        """Binary output stays exact at any size, with its passthrough reason."""
        sample = "".join("\x00binary payload %05d\n" % index for index in range(500))
        self.assertEqual(tokenpipe.classify(sample), "binary")
        result = tokenpipe.process(self.payload(sample), "safe")
        self.assertEqual(result["action"], "passthrough")
        self.assertEqual(result["output"], sample)
        self.assertEqual(result["skip_reason"], "binary-passthrough")
        self.assertIsNone(result["raw_ref"])

    def test_native_small_protected_output_keeps_its_passthrough_reason(self):
        """The native path leaves below-threshold protected output exact."""
        os.environ["TOKENPIPE_MIN_TOKENS_ESTIMATE"] = "100000"
        sample = SMALL_DIFF * 20
        rg = self.executable("rg", "print(%r, end='')\n" % sample)
        output, status = tokenpipe.execute_native(
            [rg, "needle"], "search", "safe", "native-session", "native-small"
        )
        self.assertEqual(status, 0)
        self.assertIn(sample, output)
        self.assertNotIn("raw_ref=", output.splitlines()[0])
        self.assertEqual(tokenpipe.load_metrics()[-1]["skip_reason"], "diff-passthrough")

    def test_native_large_protected_output_is_bounded(self):
        """The native path bounds large protected output and spools the raw."""
        sample = big_diff()
        rg = self.executable("rg", "print(%r, end='')\n" % sample)
        output, status = tokenpipe.execute_native(
            [rg, "needle"], "search", "safe", "native-session", "native-large"
        )
        self.assertEqual(status, 0)
        header = output.splitlines()[0]
        self.assertIn("strategy=bounded-diff", header)
        self.assertIn("raw_ref=", header)
        body = tokenpipe.show_raw(header.split("raw_ref=")[1].strip())
        self.assertIn(sample, body)
        self.assert_bounded(body, output[len(header) + 1:])


if __name__ == "__main__":
    unittest.main()
