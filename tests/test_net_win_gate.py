"""Net-win gate: a replacement must beat the recovery header it ships with.

Both processing paths compare the compressed candidate against the original
*plus* the header the host renders above a replacement. When the saving does
not exceed that header the exact original is returned under a ``net-loss`` skip
reason and nothing is spooled, while the metric counterfactual still reports
the compression that was measured but not taken.
"""

import importlib.util
import os
import sys
import tempfile
import unittest


SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "tokenpipe.py"
)
SPEC = importlib.util.spec_from_file_location("tokenpipe_net_win_under_test", SCRIPT)
tokenpipe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tokenpipe)


def marginal_sample():
    """Build output whose compression saves only a modest number of tokens.

    Returns:
        str: Plain text of 120 unique lines with trailing whitespace, so the
        compressor strips padding without collapsing the body.
    """
    return "".join("event %d completed ok   \n" % index for index in range(120))


def compressible_sample():
    """Build output whose compression saves far more than any header.

    Returns:
        str: Plain text of 200 ANSI-decorated lines; escape removal alone is a
        large, deterministic saving.
    """
    return "".join("\x1b[32mok\x1b[0m item %d\n" % index for index in range(200))


def repeated_sample():
    """Build output that collapses to a couple of lines.

    Returns:
        str: 200 identical log lines, whose compressed candidate is smaller
        than a repeat notice.
    """
    return "same log line\n" * 200


def varied_sample():
    """Build output that stays large after compression.

    Returns:
        str: 200 distinct log lines, whose compressed candidate is larger than
        a repeat notice.
    """
    return "".join(
        "2024-01-01T00:00:%02d worker-%d handled request %d\n" % (index % 60, index, index * 7)
        for index in range(200)
    )


class NetWinGateTests(unittest.TestCase):
    """Cover the header-aware replacement decision on both processing paths."""

    def setUp(self):
        """Point private state at a throwaway home and trust fixture binaries."""
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
        """Restore the environment and executable trust before cleanup."""
        os.environ.clear()
        os.environ.update(self.old_env)
        tokenpipe._TRUSTED_EXECUTABLE_DIRS = self.old_trusted_dirs
        self.temp.cleanup()

    def payload(self, output, **extra):
        """Build one bounded hook payload for :func:`tokenpipe.process`.

        Args:
            output (str): Decoded tool output to process.
            **extra (object): Field overrides such as ``tool_call_id``.

        Returns:
            dict[str, object]: A fresh payload; callers may mutate it freely.
        """
        value = {
            "output": output,
            "session_id": "session-net-win",
            "tool_call_id": "call-one",
            "tool_name": "exec_command",
            "command": "pytest tests -q",
            "exit_status": 0,
        }
        value.update(extra)
        return value

    def patch_overhead(self, tokens):
        """Pin the header cost so a decision boundary is exact on any machine.

        Args:
            tokens (int): Header estimate every gate call should observe. The
                real estimate varies with the private spool path length, which
                differs per checkout.

        Returns:
            None: The real estimator is restored during test cleanup.
        """
        original = tokenpipe._replacement_overhead_estimate
        tokenpipe._replacement_overhead_estimate = lambda placeholder=None: tokens
        self.addCleanup(setattr, tokenpipe, "_replacement_overhead_estimate", original)

    def patch_placeholder(self, reference):
        """Pin the recovery path the native header cost is priced against.

        Args:
            reference (str): Placeholder path returned to every caller.

        Returns:
            None: The real placeholder builder is restored during cleanup.
        """
        original = tokenpipe._raw_ref_placeholder
        tokenpipe._raw_ref_placeholder = lambda root=None: reference
        self.addCleanup(setattr, tokenpipe, "_raw_ref_placeholder", original)

    def candidate_estimate(self, sample):
        """Return the token estimate of the candidate the compressor produces.

        Args:
            sample (str): Output the gate will be asked about.

        Returns:
            int: ``estimate_tokens`` of the bounded compressed candidate, that
            is exactly the value :func:`tokenpipe.process` compares.
        """
        _, candidate = tokenpipe.compress(sample, tokenpipe.classify(sample))
        return tokenpipe.estimate_tokens(tokenpipe.bound_candidate(candidate))

    def outpriced_placeholder(self, sample):
        """Return a recovery reference the native header can never earn back.

        Args:
            sample (str): Body the native path will compress.

        Returns:
            str: Placeholder path grown until the reference alone widens the
            native header by more tokens than compressing ``sample`` could ever
            save, so the net-loss branch is taken under any token estimator
            rather than under a hand-tuned fixture size.
        """
        budget = 2 * tokenpipe.estimate_tokens(sample)
        baseline = tokenpipe.estimate_tokens(
            tokenpipe._native_header("search", 0, "safe", "passthrough", None)
        )
        reference = "/" + "x" * 256
        while tokenpipe.estimate_tokens(tokenpipe._native_header(
            "search", 0, "safe", "passthrough", reference
        )) - baseline < budget:
            reference += "x" * len(reference)
        return reference

    def raw_files(self):
        """List every file spooled under the private raw roots.

        Returns:
            list[str]: Absolute paths, empty when nothing was spooled.
        """
        found = []
        for root in (tokenpipe._raw_root(), tokenpipe._runtime_raw_root()):
            for parent, _, names in os.walk(root):
                found.extend(os.path.join(parent, name) for name in names)
        return found

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

    def test_header_overhead_is_priced_from_the_shared_templates(self):
        """The estimate is the larger of the two headers the hosts render."""
        reference = tokenpipe._raw_ref_placeholder()
        self.assertTrue(reference.startswith(tokenpipe._raw_root()))
        codex = tokenpipe.post_recovery_header("audit", "passthrough", 0, reference)
        claude = tokenpipe.claude_recovery_header(
            ["stdout raw_ref=" + reference],
            "/usr/bin/python3 %s show" % os.path.abspath(tokenpipe.__file__),
        )
        self.assertEqual(
            tokenpipe._replacement_overhead_estimate(),
            max(tokenpipe.estimate_tokens(codex), tokenpipe.estimate_tokens(claude)),
        )
        self.assertGreater(tokenpipe._replacement_overhead_estimate(), 0)

    def test_saving_smaller_than_the_header_is_a_net_loss(self):
        """A saving the header cancels out returns the exact original."""
        sample = marginal_sample()
        candidate_est = self.candidate_estimate(sample)
        original_est = tokenpipe.estimate_tokens(sample)
        self.assertLess(candidate_est, original_est)
        self.patch_overhead(original_est - candidate_est + 1)
        result = tokenpipe.process(self.payload(sample), "safe")
        self.assertEqual(result["action"], "passthrough")
        self.assertEqual(result["output"], sample)
        self.assertEqual(result["strategy"], "passthrough")
        self.assertEqual(result["skip_reason"], "net-loss")
        self.assertIsNone(result["raw_ref"])
        self.assertEqual(self.raw_files(), [])
        metric = tokenpipe.load_metrics()[-1]
        self.assertEqual(metric["skip_reason"], "net-loss")
        self.assertEqual(metric["counterfactual_tokens_estimate"], candidate_est)
        self.assertLess(
            metric["counterfactual_tokens_estimate"], metric["original_tokens_estimate"]
        )

    def test_break_even_saving_is_a_net_loss(self):
        """A saving exactly equal to the header is not a win."""
        sample = marginal_sample()
        candidate_est = self.candidate_estimate(sample)
        self.patch_overhead(tokenpipe.estimate_tokens(sample) - candidate_est)
        result = tokenpipe.process(self.payload(sample), "safe")
        self.assertEqual(result["action"], "passthrough")
        self.assertEqual(result["output"], sample)
        self.assertEqual(result["skip_reason"], "net-loss")
        self.assertEqual(self.raw_files(), [])

    def test_one_token_of_headroom_is_still_a_win(self):
        """Just past break-even the replacement is emitted as before."""
        sample = marginal_sample()
        candidate_est = self.candidate_estimate(sample)
        self.patch_overhead(tokenpipe.estimate_tokens(sample) - candidate_est - 1)
        result = tokenpipe.process(self.payload(sample), "safe")
        self.assertEqual(result["action"], "replace")
        self.assertIsNone(result["skip_reason"])
        self.assertTrue(result["raw_ref"])
        self.assertEqual(tokenpipe.show_raw(result["raw_ref"]), sample)

    def test_clear_win_is_replaced_with_a_recoverable_raw_ref(self):
        """The real header cost never blocks a substantial saving."""
        sample = compressible_sample()
        result = tokenpipe.process(self.payload(sample), "safe")
        self.assertEqual(result["action"], "replace")
        self.assertIsNone(result["skip_reason"])
        self.assertTrue(result["raw_ref"])
        self.assertEqual(tokenpipe.show_raw(result["raw_ref"]), sample)
        self.assertLess(
            result["shown_tokens_estimate"] + tokenpipe._replacement_overhead_estimate(),
            result["original_tokens_estimate"],
        )

    def test_native_net_loss_keeps_the_exact_body(self):
        """The native path prices the recovery field its own header gains."""
        sample = compressible_sample()
        # The native header grows by the recovery reference; an implausibly long
        # reference makes that growth outweigh any saving.
        self.patch_placeholder(self.outpriced_placeholder(sample))
        rg = self.executable("rg", "print(%r, end='')\n" % sample)
        output, status = tokenpipe.execute_native(
            [rg, "needle"], "search", "safe", "native-session", "native-net-loss"
        )
        self.assertEqual(status, 0)
        self.assertIn(sample, output)
        self.assertNotIn("raw_ref=", output.splitlines()[0])
        self.assertEqual(self.raw_files(), [])
        metric = tokenpipe.load_metrics()[-1]
        self.assertEqual(metric["skip_reason"], "net-loss")
        self.assertLess(metric["counterfactual_bytes"], metric["original_bytes"])

    def test_native_clear_win_is_still_replaced(self):
        """With the real header cost the native path keeps compressing."""
        sample = compressible_sample()
        rg = self.executable("rg", "print(%r, end='')\n" % sample)
        output, status = tokenpipe.execute_native(
            [rg, "needle"], "search", "safe", "native-session", "native-win"
        )
        self.assertEqual(status, 0)
        header = output.splitlines()[0]
        self.assertIn("raw_ref=", header)
        self.assertIn(sample, tokenpipe.show_raw(header.split("raw_ref=")[1].strip()))
        self.assertNotEqual(tokenpipe.load_metrics()[-1]["skip_reason"], "net-loss")

    def test_repeat_notice_loses_to_a_smaller_candidate(self):
        """A notice larger than the candidate never becomes the shown output."""
        os.environ["TOKENPIPE_REPEAT_REPLACE"] = "1"
        sample = repeated_sample()
        first = tokenpipe.process(self.payload(sample), "safe")
        second = tokenpipe.process(self.payload(sample, tool_call_id="call-two"), "safe")
        notice_est = tokenpipe.estimate_tokens(
            tokenpipe._repeat_notice(len(sample.encode("utf-8")), first["raw_ref"]))
        self.assertGreaterEqual(notice_est, second["shown_tokens_estimate"])
        self.assertEqual(second["action"], "replace")
        self.assertEqual(second["strategy"], "lite-log")
        self.assertNotIn("identical to previous output", second["output"])
        self.assertTrue(tokenpipe.load_metrics()[-1]["repeat_of_previous"])

    def test_repeat_notice_wins_when_it_is_smaller_than_the_candidate(self):
        """A notice smaller than the candidate replaces the output."""
        os.environ["TOKENPIPE_REPEAT_REPLACE"] = "1"
        sample = varied_sample()
        tokenpipe.process(self.payload(sample), "safe")
        second = tokenpipe.process(self.payload(sample, tool_call_id="call-two"), "safe")
        self.assertEqual(second["action"], "replace")
        self.assertEqual(second["strategy"], "repeat-notice")
        self.assertIn("identical to previous output", second["output"])
        self.assertEqual(tokenpipe.show_raw(second["raw_ref"]), sample)


if __name__ == "__main__":
    unittest.main()
