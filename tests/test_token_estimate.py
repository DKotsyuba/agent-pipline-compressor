"""Character-class token estimator coverage.

Fixtures pin one class each with a tolerance band rather than an exact count:
the ratios are heuristics calibrated against published OpenAI ``o200k``-style
measurements, so an assertion on a single number would only record the current
constants. The bands are wide enough to survive re-calibration and narrow
enough to fail if a class stops being recognized.
"""

import importlib.util
import json
import os
import time
import unittest


SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "tokenpipe.py")
SPEC = importlib.util.spec_from_file_location("tokenpipe", SCRIPT)
tokenpipe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tokenpipe)

PROSE = (
    "The compression pipeline keeps every transform deterministic so that the "
    "same tool output always produces the same shown result. A reviewer can "
    "replay a capture, compare the estimates, and reach the same conclusion "
    "without running a model or reaching the network at any point. "
)
CODE = (
    "def compress(text, category):\n"
    "    if not text or category == 'binary':\n"
    "        return ('passthrough', text)\n"
    "    lines = [line.rstrip() for line in text.splitlines()]\n"
    "    counts = {}\n"
    "    for index, line in enumerate(lines[:512]):\n"
    "        counts[line] = counts.get(line, 0) + 1\n"
    "    return ('lite-log', '\\n'.join(lines))\n"
)
CYRILLIC = (
    "\u041a\u043e\u043d\u0432\u0435\u0439\u0435\u0440 \u0441\u0436\u0430\u0442\u0438\u044f "
    "\u043e\u0441\u0442\u0430\u0451\u0442\u0441\u044f \u0434\u0435\u0442\u0435\u0440\u043c"
    "\u0438\u043d\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u044b\u043c, \u043f\u043e\u044d"
    "\u0442\u043e\u043c\u0443 \u043e\u0434\u0438\u043d \u0438 \u0442\u043e\u0442 \u0436\u0435 "
    "\u0432\u044b\u0432\u043e\u0434 \u0438\u043d\u0441\u0442\u0440\u0443\u043c\u0435\u043d"
    "\u0442\u0430 \u0432\u0441\u0435\u0433\u0434\u0430 \u0434\u0430\u0451\u0442 \u043e\u0434"
    "\u0438\u043d\u0430\u043a\u043e\u0432\u044b\u0439 \u0440\u0435\u0437\u0443\u043b\u044c"
    "\u0442\u0430\u0442. "
)
HEX = "deadbeef0123abcd" * 64


def kilobyte(text):
    """Repeat and cut a fixture to exactly 1024 characters.

    Args:
        text (str): Non-empty seed text to tile.

    Returns:
        str: The seed repeated and truncated to 1024 characters, so every
        fixture is compared at one length regardless of its UTF-8 size.
    """
    return (text * (1024 // len(text) + 1))[:1024]


class TokenEstimateTests(unittest.TestCase):
    """Per-class calibration, boundary, ordering, and cost coverage."""

    def assertBetween(self, value, low, high):
        """Assert an estimate falls inside an inclusive tolerance band.

        Args:
            value (int): Estimated token count under test.
            low (int): Smallest acceptable count, inclusive.
            high (int): Largest acceptable count, inclusive.

        Returns:
            None: Fails the current test when ``value`` is outside the band.
        """
        self.assertTrue(low <= value <= high, "%d not in [%d, %d]" % (value, low, high))

    def test_empty_input_is_zero_and_any_content_is_at_least_one(self):
        """Empty input estimates zero; any non-empty input estimates at least one."""
        self.assertEqual(tokenpipe.estimate_tokens(""), 0)
        self.assertEqual(tokenpipe.estimate_tokens(None), 0)
        self.assertEqual(tokenpipe.estimate_tokens("x"), 1)
        self.assertEqual(tokenpipe.estimate_tokens("7"), 1)
        self.assertEqual(tokenpipe.estimate_tokens(" "), 1)

    def test_english_prose_is_no_longer_over_counted(self):
        """A kilobyte of English prose lands near four characters per token."""
        prose = kilobyte(PROSE)
        estimate = tokenpipe.estimate_tokens(prose)
        self.assertBetween(estimate, 230, 300)
        self.assertLess(estimate, tokenpipe.estimate_tokens_bytes(prose))

    def test_hex_runs_are_no_longer_under_counted(self):
        """A kilobyte of hex costs far more than the UTF-8 byte estimate."""
        estimate = tokenpipe.estimate_tokens(HEX)
        self.assertBetween(estimate, 500, 620)
        self.assertGreater(estimate, 1.7 * tokenpipe.estimate_tokens_bytes(HEX))

    def test_hex_lines_keep_their_class_across_line_breaks(self):
        """Line breaks split hex runs without dropping them to the prose rate."""
        lines = "\n".join([HEX[index * 64:(index + 1) * 64] for index in range(16)])
        self.assertBetween(tokenpipe.estimate_tokens(lines), 500, 640)

    def test_python_code_sits_between_prose_and_hex(self):
        """Punctuation and indentation make code denser than prose."""
        estimate = tokenpipe.estimate_tokens(kilobyte(CODE))
        self.assertBetween(estimate, 300, 420)
        self.assertGreater(estimate, tokenpipe.estimate_tokens(kilobyte(PROSE)))

    def test_cyrillic_costs_about_two_characters_per_token(self):
        """Non-ASCII alphabetic text is roughly twice the cost of prose."""
        self.assertBetween(tokenpipe.estimate_tokens(kilobyte(CYRILLIC)), 450, 560)

    def test_cjk_and_emoji_are_charged_per_character(self):
        """CJK costs one token per character; a non-BMP emoji costs its two code units."""
        self.assertEqual(tokenpipe.estimate_tokens("\u4e2d\u6587\u6d4b\u8bd5"), 4)
        self.assertEqual(tokenpipe.estimate_tokens("\U0001f600"), 2)

    def test_json_blob_is_punctuation_dense(self):
        """A JSON blob costs well under three characters per token."""
        blob = json.dumps([
            {"id": "a1b2c3d4e5f6a7b8", "name": "item-%d" % index, "count": index * 37, "ok": True}
            for index in range(20)
        ])
        estimate = tokenpipe.estimate_tokens(blob)
        self.assertBetween(estimate, 550, 800)
        self.assertGreater(estimate, tokenpipe.estimate_tokens_bytes(blob))

    def test_appending_content_never_lowers_the_estimate(self):
        """The estimate is monotone as content is appended."""
        text = ""
        previous = 0
        for chunk in (PROSE, CODE, CYRILLIC, HEX[:32], "\n\n", "   ", "x", "!", "\U0001f600", PROSE):
            text += chunk
            estimate = tokenpipe.estimate_tokens(text)
            self.assertGreaterEqual(estimate, previous)
            previous = estimate

    def test_ratios_and_pattern_stay_consistent(self):
        """Every regex class has a ratio and every ratio is a positive rate."""
        self.assertEqual(set(tokenpipe.TOKEN_CLASS_RE.groupindex), set(tokenpipe.TOKEN_CLASS_RATIOS))
        self.assertTrue(all(ratio > 0 for ratio in tokenpipe.TOKEN_CLASS_RATIOS.values()))

    def test_pattern_partitions_the_input_exactly_once(self):
        """Concatenating the matched runs reproduces the input."""
        sample = kilobyte(PROSE)[:200] + CODE + kilobyte(CYRILLIC)[:200] + HEX[:64] + "\U0001f600\t"
        runs = "".join(match.group() for match in tokenpipe.TOKEN_CLASS_RE.finditer(sample))
        self.assertEqual(runs, sample)

    def test_byte_estimator_is_still_available_unchanged(self):
        """The superseded estimator keeps the exact ``bytes / 3.5`` formula."""
        self.assertEqual(tokenpipe.estimate_tokens_bytes(""), 0)
        self.assertEqual(tokenpipe.estimate_tokens_bytes("x"), 1)
        self.assertEqual(tokenpipe.estimate_tokens_bytes("a" * 700), 200)
        cyrillic = kilobyte(CYRILLIC)
        self.assertEqual(
            tokenpipe.estimate_tokens_bytes(cyrillic),
            -(-len(cyrillic.encode("utf-8")) * 2 // 7),
        )

    def test_one_hundred_kilobytes_stay_well_inside_the_latency_budget(self):
        """A 100 KB mixed capture estimates in far less than 50 ms."""
        mixed = ((PROSE + CODE + CYRILLIC + HEX[:128] + "\n") * 200)[:100 * 1024]
        self.assertEqual(len(mixed), 100 * 1024)
        best = None
        for _ in range(3):
            started = time.perf_counter()
            tokenpipe.estimate_tokens(mixed)
            elapsed = (time.perf_counter() - started) * 1000.0
            best = elapsed if best is None else min(best, elapsed)
        self.assertLess(best, 50.0, "100 KB estimate took %.2f ms" % best)


if __name__ == "__main__":
    unittest.main()
