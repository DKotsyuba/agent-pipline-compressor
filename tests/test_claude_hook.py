import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "hooks"
sys.path.insert(0, str(HOOKS))
claude_post = importlib.import_module("claude_post_tool")
post_tool = importlib.import_module("post_tool")


class ClaudeHookTests(unittest.TestCase):
    def setUp(self):
        """Isolate user and runtime state for each Claude hook transaction."""
        self.temp = tempfile.TemporaryDirectory()
        self.old_env = os.environ.copy()
        os.environ["TOKENPIPE_HOME"] = self.temp.name
        os.environ["TOKENPIPE_RUNTIME_HOME"] = os.path.join(self.temp.name, "runtime")
        os.environ["TOKENPIPE_MIN_TOKENS_ESTIMATE"] = "10"
        os.environ["TOKENPIPE_MODE"] = "safe"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def event(self, stdout="", stderr="", **response_fields):
        response = {
            "stdout": stdout,
            "stderr": stderr,
            "interrupted": False,
            "isImage": False,
        }
        response.update(response_fields)
        return {
            "session_id": "claude-session",
            "tool_use_id": "toolu_123",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -q"},
            "tool_response": response,
        }

    def non_bash_event(self, tool_name, response, **fields):
        event = {
            "session_id": "claude-session",
            "tool_use_id": "toolu_123",
            "hook_event_name": "PostToolUse",
            "tool_name": tool_name,
            "tool_input": {},
            "tool_response": response,
        }
        event.update(fields)
        return event

    def run_hook_main(self, event):
        os.environ["CLAUDE_PLUGIN_ROOT"] = str(ROOT)
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(event))
        visible = io.StringIO()
        try:
            with contextlib.redirect_stdout(visible):
                post_tool.main()
        finally:
            sys.stdin = old_stdin
        return visible.getvalue()

    def test_replaces_stdout_preserves_shape_and_recovers_raw(self):
        raw = "same line\n" * 400
        event = self.event(raw, "warning\n", duration_ms=17)
        result = claude_post.adapt(event)
        specific = result["hookSpecificOutput"]
        updated = specific["updatedToolOutput"]
        self.assertLess(len(updated["stdout"]), len(raw))
        self.assertEqual(updated["stderr"], "warning\n")
        self.assertEqual(updated["interrupted"], False)
        self.assertEqual(updated["isImage"], False)
        self.assertEqual(updated["duration_ms"], 17)
        self.assertNotIn("permissionDecision", json.dumps(result))
        context = specific["additionalContext"]
        self.assertIn("tokenpipe-claude-v1", context)
        raw_ref = context.split("stdout raw_ref=", 1)[1].split(";", 1)[0]
        self.assertEqual(claude_post.tokenpipe.show_raw(raw_ref), raw)

    def test_each_changed_stream_has_its_own_recovery_ref(self):
        stdout = "out repeat\n" * 400
        stderr = "ERROR repeated\n" * 400
        result = claude_post.adapt(self.event(stdout, stderr))["hookSpecificOutput"]
        context = result["additionalContext"]
        stdout_ref = context.split("stdout raw_ref=", 1)[1].split(",", 1)[0]
        stderr_ref = context.split("stderr raw_ref=", 1)[1].split(";", 1)[0]
        self.assertNotEqual(stdout_ref, stderr_ref)
        self.assertEqual(claude_post.tokenpipe.show_raw(stdout_ref), stdout)
        self.assertEqual(claude_post.tokenpipe.show_raw(stderr_ref), stderr)

    def test_two_stream_cap_failure_is_transactional_passthrough(self):
        """Both Claude streams fail open when protected raws exceed one cap."""
        os.environ["TOKENPIPE_RAW_MAX_BYTES"] = "12000"
        stdout = "out repeat\n" * 800
        stderr = "ERROR repeated\n" * 800
        self.assertIsNone(claude_post.adapt(self.event(stdout, stderr)))
        raw_root = claude_post.tokenpipe._raw_root()
        remaining = [
            name for base, _, names in os.walk(raw_root) for name in names
        ] if os.path.isdir(raw_root) else []
        self.assertEqual(remaining, [])
        self.assertEqual(claude_post.tokenpipe.load_metrics(), [])

    def test_small_binary_unknown_and_audit_are_exact_passthrough(self):
        """Unsupported, binary, small, and audit streams pass through exactly."""
        self.assertIsNone(claude_post.adapt(self.event("ok\n")))
        self.assertIsNone(claude_post.adapt(self.event("\x00BIN\x01\n" * 400)))
        self.assertIsNone(claude_post.adapt(self.event("same\n" * 400, isImage=True)))
        malformed = self.event("same\n" * 400)
        malformed["tool_response"] = "not-an-object"
        self.assertIsNone(claude_post.adapt(malformed))
        self.assertIsNone(claude_post.adapt(self.event("same\n" * 400), "audit"))

    def test_raw_spool_failure_is_fail_open(self):
        original = claude_post.tokenpipe.spool_raw
        claude_post.tokenpipe.spool_raw = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("full"))
        try:
            self.assertIsNone(claude_post.adapt(self.event("same\n" * 400)))
        finally:
            claude_post.tokenpipe.spool_raw = original

    def test_shared_post_hook_dispatches_only_in_claude_environment(self):
        os.environ["CLAUDE_PLUGIN_ROOT"] = str(ROOT)
        event = self.event("same line\n" * 400)
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(event))
        visible = io.StringIO()
        try:
            with contextlib.redirect_stdout(visible):
                post_tool.main()
        finally:
            sys.stdin = old_stdin
        result = json.loads(visible.getvalue())
        self.assertIn("updatedToolOutput", result["hookSpecificOutput"])

    def test_grep_event_records_one_audit_metric_and_no_stdout(self):
        payload = "match: same line\n" * 400
        event = self.non_bash_event("Grep", {"content": payload})
        self.assertEqual(self.run_hook_main(event), "")
        metrics = claude_post.tokenpipe.load_metrics()
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0]["mode"], "audit")
        self.assertEqual(metrics[0]["tool"], "Grep")
        self.assertEqual(metrics[0]["original_bytes"], len(payload.encode("utf-8")))
        self.assertFalse(metrics[0]["raw_ref_present"])

    def test_webfetch_event_records_one_audit_metric_and_no_stdout(self):
        payload = "fetched page body\n" * 400
        event = self.non_bash_event("WebFetch", payload)
        self.assertEqual(self.run_hook_main(event), "")
        metrics = claude_post.tokenpipe.load_metrics()
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0]["mode"], "audit")
        self.assertEqual(metrics[0]["tool"], "WebFetch")
        self.assertEqual(metrics[0]["original_bytes"], len(payload.encode("utf-8")))

    def test_mcp_event_extracts_text_blocks_into_one_audit_metric(self):
        first = "first block line\n" * 200
        last = "last block line\n" * 200
        blocks = [
            {"type": "text", "text": first},
            {"type": "image", "data": "not-text"},
            {"type": "text", "text": last},
        ]
        event = self.non_bash_event("mcp__server__tool", blocks)
        self.assertEqual(self.run_hook_main(event), "")
        metrics = claude_post.tokenpipe.load_metrics()
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0]["mode"], "audit")
        self.assertEqual(metrics[0]["tool"], "mcp__server__tool")
        expected = "\n".join((first, last))
        self.assertEqual(metrics[0]["original_bytes"], len(expected.encode("utf-8")))

    def test_long_mcp_tool_name_is_capped_in_metric_label(self):
        name = "mcp__" + "server-" * 20 + "tool"
        event = self.non_bash_event(name, "payload\n" * 100)
        self.assertEqual(self.run_hook_main(event), "")
        metrics = claude_post.tokenpipe.load_metrics()
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0]["tool"], name[:64])

    def test_read_event_is_ignored_without_metric_or_output(self):
        event = self.non_bash_event("Read", {"content": "file body\n" * 400})
        self.assertIsNone(claude_post.adapt(event))
        self.assertEqual(self.run_hook_main(event), "")
        self.assertEqual(claude_post.tokenpipe.load_metrics(), [])

    def test_non_bash_event_in_safe_mode_still_forces_audit(self):
        payload = "safe mode payload\n" * 400
        event = self.non_bash_event("Grep", {"content": payload})
        self.assertIsNone(claude_post.adapt(event, "safe"))
        self.assertEqual(self.run_hook_main(event), "")
        metrics = claude_post.tokenpipe.load_metrics()
        self.assertEqual(len(metrics), 2)
        self.assertEqual([row["mode"] for row in metrics], ["audit", "audit"])
        self.assertFalse(any(row["raw_ref_present"] for row in metrics))


if __name__ == "__main__":
    unittest.main()
