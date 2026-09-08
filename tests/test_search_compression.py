import importlib.util
import os
import tempfile
import unittest


SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "tokenpipe.py")
SPEC = importlib.util.spec_from_file_location("tokenpipe", SCRIPT)
tokenpipe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tokenpipe)


def match_lines(files=24, per_file=20):
    """Build deterministic ripgrep-style `path:line:text` output.

    Args:
        files (int): Number of distinct file paths, at least 1.
        per_file (int): Matches emitted per file, at least 1.

    Returns:
        str: Newline-terminated output with ``files * per_file`` match lines
        in first-seen file order.
    """
    return "\n".join(
        "src/pkg%02d/module%02d.py:%d:needle %02d-%02d" % (index // 4, index, line * 3 + 1, index, line)
        for index in range(files) for line in range(per_file)
    ) + "\n"


def path_lines(dirs=6, per_dir=12):
    """Build deterministic `find`-style output with one path per line.

    Args:
        dirs (int): Number of distinct directories, at least 1.
        per_dir (int): Entries per directory, at least 1.

    Returns:
        str: Newline-terminated listing of ``dirs * per_dir`` paths sharing a
        ``./src`` prefix.
    """
    return "\n".join(
        "./src/pkg%02d/module%02d.py" % (directory, entry)
        for directory in range(dirs) for entry in range(per_dir)
    ) + "\n"


class SearchShapeTests(unittest.TestCase):
    def test_match_shapes_are_recognized_per_line(self):
        """All three grep result spellings resolve to their file path."""
        self.assertEqual(tokenpipe._match_path("src/app/main.py:12:needle here"), "src/app/main.py")
        self.assertEqual(tokenpipe._match_path("src/app/main.py:needle here"), "src/app/main.py")
        self.assertEqual(tokenpipe._match_path("src/app/main.py-11-context here"), "src/app/main.py")
        self.assertIsNone(tokenpipe._match_path("plain sentence without a path"))
        self.assertIsNone(tokenpipe._match_path("rg: /missing: IO error for operation"))
        self.assertIsNone(tokenpipe._match_path("2026-01-01T00:00:00Z INFO worker heartbeat"))

    def test_match_line_detection_needs_density_and_length(self):
        """Dense match output is detected; short or mixed output is not."""
        self.assertTrue(tokenpipe._is_match_lines(match_lines(4, 5)))
        self.assertFalse(tokenpipe._is_match_lines(match_lines(1, 9)))
        self.assertFalse(tokenpipe._is_match_lines(
            "\n".join("2026-01-01T00:00:0%d INFO beat" % (index % 10) for index in range(40))))
        mixed = "\n".join(
            ("src/a/module.py:%d:needle" % index) if index % 2 else ("free form line %d" % index)
            for index in range(40))
        self.assertFalse(tokenpipe._is_match_lines(mixed))

    def test_path_line_detection_requires_separator_and_length(self):
        """Bare path listings are detected; words and match lines are not."""
        self.assertTrue(tokenpipe._is_path_lines(path_lines(2, 8)))
        self.assertFalse(tokenpipe._is_path_lines(path_lines(1, 6)))
        self.assertFalse(tokenpipe._is_path_lines("\n".join("word%02d" % index for index in range(40))))
        self.assertFalse(tokenpipe._is_path_lines(match_lines(4, 5)))


class GroupMatchesTests(unittest.TestCase):
    def test_single_file_keeps_first_and_last_matches_with_exact_count(self):
        """One file renders a header, three head/tail matches, and one marker."""
        raw = match_lines(1, 14)
        lines = raw.splitlines()
        self.assertEqual(
            tokenpipe.group_matches(raw).splitlines(),
            ["src/pkg00/module00.py (14 matches)"] + lines[:3]
            + ["... 8 more matches in this file"] + lines[-3:],
        )

    def test_file_cap_reports_remaining_files_and_keeps_the_last_line(self):
        """Beyond the cap, files collapse into one marker with exact totals."""
        raw = match_lines(22, 12)
        lines = raw.splitlines()
        grouped = tokenpipe.group_matches(raw).splitlines()
        self.assertEqual(grouped[0], "src/pkg00/module00.py (12 matches)")
        self.assertEqual(grouped[1], lines[0])
        self.assertIn("... 2 more files (24 matches)", grouped)
        self.assertEqual(grouped[-1], lines[-1])
        self.assertEqual(sum(1 for line in grouped if line.endswith("(12 matches)")), 20)
        self.assertLess(len("\n".join(grouped)), len(raw))

    def test_unmatched_lines_are_bounded_and_kept(self):
        """Lines without a match shape survive as their own bounded block."""
        raw = match_lines(1, 30) + "\n".join("rg: /missing-%d: IO error" % index for index in range(8)) + "\n"
        grouped = tokenpipe.group_matches(raw)
        self.assertIn("rg: /missing-0: IO error", grouped)
        self.assertIn("... 2 more unmatched lines", grouped)
        self.assertIn("rg: /missing-7: IO error", grouped)

    def test_grouping_never_expands_and_never_touches_short_output(self):
        """Sparse or short input is returned byte-for-byte unchanged."""
        sparse = "\n".join("src/module%02d.py:%d:needle" % (index, index) for index in range(13)) + "\n"
        small = "\n".join("src/app/main.py:%d:needle small %d" % (index * 4 + 1, index) for index in range(9)) + "\n"
        self.assertEqual(tokenpipe.group_matches(sparse), sparse)
        self.assertEqual(tokenpipe.group_matches(small), small)
        self.assertFalse(tokenpipe._is_match_lines(small))


class FoldPathsTests(unittest.TestCase):
    def test_directory_fold_keeps_head_and_tail_entries(self):
        """A directory renders its count, three head names, and two tail names."""
        raw = path_lines(1, 14)
        lines = raw.splitlines()
        self.assertEqual(
            tokenpipe.fold_paths(raw).splitlines(),
            [lines[0], "./src/pkg00/ (14 entries)", "module00.py", "module01.py", "module02.py",
             "... 9 more entries", "module12.py", "module13.py", lines[-1]],
        )

    def test_directories_beyond_depth_collapse_into_one_line(self):
        """Directories deeper than the limit collapse under their prefix."""
        raw = "\n".join(
            "./src/pkg%d/sub%d/leaf%d/deep%d/module%02d.py" % (pkg, sub, pkg, sub, entry)
            for pkg in range(2) for sub in range(2) for entry in range(8)
        ) + "\n"
        folded = tokenpipe.fold_paths(raw).splitlines()
        self.assertIn("./src/pkg0/sub0/leaf0/… (8 entries)", folded)
        self.assertIn("./src/pkg1/sub1/leaf1/… (8 entries)", folded)
        self.assertEqual(folded[0], raw.splitlines()[0])
        self.assertEqual(folded[-1], raw.splitlines()[-1])
        self.assertLess(len("\n".join(folded)), len(raw))

    def test_folding_returns_exact_input_when_it_cannot_shorten(self):
        """A listing with nothing to omit is returned unchanged."""
        raw = "\n".join("./src/pkg%02d/module.py" % index for index in range(13)) + "\n"
        self.assertEqual(tokenpipe.fold_paths(raw), raw)


class ClassifySearchTests(unittest.TestCase):
    def test_search_is_classified_before_error(self):
        """Matches mentioning failures stay search output, not error output."""
        failures = "\n".join(
            "src/module%02d.py:%d:error handler not found" % (index, index * 3) for index in range(20)
        ) + "\n"
        self.assertTrue(tokenpipe.STRONG_ERROR_RE.search(failures))
        self.assertEqual(tokenpipe.classify(failures), "search")
        self.assertEqual(tokenpipe.classify(path_lines(3, 8)), "search")
        self.assertEqual(tokenpipe.compress(failures, "search")[0], "search-group")
        self.assertEqual(tokenpipe.compress(path_lines(3, 8), "search")[0], "search-fold")

    def test_non_search_output_keeps_its_previous_category(self):
        """Log, error, and short search output are unaffected by the new shapes."""
        io_errors = "\n".join(
            "rg: /missing-%03d: IO error for operation: No such file or directory" % index
            for index in range(40)
        )
        self.assertEqual(tokenpipe.classify(io_errors), "error")
        self.assertEqual(
            tokenpipe.classify("\n".join("00:0%d worker beat" % (index % 10) for index in range(40))), "log")
        small = "\n".join("src/app/main.py:%d:needle small %d" % (index * 4 + 1, index) for index in range(9)) + "\n"
        self.assertNotEqual(tokenpipe.classify(small), "search")


class SearchProcessTests(unittest.TestCase):
    def setUp(self):
        """Point private runtime state at a temporary directory."""
        self.temp = tempfile.TemporaryDirectory()
        self.old_env = os.environ.copy()
        os.environ["TOKENPIPE_HOME"] = self.temp.name
        os.environ["TOKENPIPE_RUNTIME_HOME"] = os.path.join(self.temp.name, "runtime")
        os.environ["TOKENPIPE_MIN_TOKENS_ESTIMATE"] = "10"

    def tearDown(self):
        """Restore the process environment and remove private state."""
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def payload(self, output):
        """Return a bounded post-tool payload for one captured output.

        Args:
            output (str): Captured tool output to compress.

        Returns:
            dict[str, object]: Payload accepted by :func:`tokenpipe.process`.
        """
        return {"output": output, "session_id": "session-search", "tool_call_id": "call-search",
                "tool_name": "exec_command", "command": "rg needle src", "exit_status": 0}

    def test_dense_matches_are_replaced_with_a_recoverable_reference(self):
        """Grouping replaces output only once the raw copy reads back exactly."""
        raw = match_lines(24, 20)
        result = tokenpipe.process(self.payload(raw), "safe")
        self.assertEqual(result["action"], "replace")
        self.assertEqual(result["content_category"], "search")
        self.assertEqual(result["strategy"], "search-group")
        self.assertTrue(result["raw_ref"])
        self.assertLess(len(result["output"]), len(raw))
        self.assertIn("(20 matches)", result["output"])
        self.assertEqual(tokenpipe.show_raw(result["raw_ref"]), raw)

    def test_path_listings_are_replaced_with_a_recoverable_reference(self):
        """Folding replaces output only once the raw copy reads back exactly."""
        raw = path_lines(6, 20)
        result = tokenpipe.process(self.payload(raw), "safe")
        self.assertEqual(result["action"], "replace")
        self.assertEqual(result["strategy"], "search-fold")
        self.assertTrue(result["raw_ref"])
        self.assertLess(len(result["output"]), len(raw))
        self.assertIn("(20 entries)", result["output"])
        self.assertEqual(tokenpipe.show_raw(result["raw_ref"]), raw)

    def test_audit_mode_reports_the_search_counterfactual_without_replacing(self):
        """Audit keeps the original output and still measures the candidate."""
        raw = match_lines(24, 20)
        result = tokenpipe.process(self.payload(raw), "audit")
        self.assertEqual(result["action"], "passthrough")
        self.assertEqual(result["output"], raw)
        self.assertIsNone(result["raw_ref"])
        self.assertLess(result["counterfactual_tokens_estimate"], result["original_tokens_estimate"])


if __name__ == "__main__":
    unittest.main()
