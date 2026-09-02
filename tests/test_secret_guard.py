"""Secret guard: credential-shaped output is refused before raw spooling.

Every credential-looking string in this file is synthetic and matches only the
shape of a real credential; none is or was valid anywhere. Each one is also
assembled at run time by :func:`shape`, so no scanner-matching literal is
stored in the repository.
"""

import importlib.util
import os
import sys
import tempfile
import unittest


SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "tokenpipe.py")
SPEC = importlib.util.spec_from_file_location("tokenpipe_secret_guard_under_test", SCRIPT)
tokenpipe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tokenpipe)

def shape(*parts):
    """Join fragments into one credential-shaped fixture at run time.

    Secret scanners such as GitHub push protection reject a push when any file
    contains text matching a credential shape, even a synthetic one that never
    was valid. Every fixture is therefore assembled here from fragments that
    are individually innocent, so the repository holds no scanner-matching
    literal while the tests still exercise each pattern in ``SECRET_RES``.

    Args:
        *parts (str): Fragments concatenated in order.

    Returns:
        str: The assembled fixture value.
    """
    return "".join(parts)


# One synthetic value per guarded pattern family, keyed by family name.
SECRET_SAMPLES = {
    "pem": shape(
        "-----", "BEGIN ", "RSA ", "PRIVATE ", "KEY", "-----\n",
        "MIIEexampleexample\n",
        "-----", "END ", "RSA ", "PRIVATE ", "KEY", "-----\n",
    ),
    "aws": shape("provider key ", "AK", "IA", "ZZZZEXAMPLE99999", " loaded\n"),
    "github": shape("remote token ", "gh", "p_", "E" * 24, "\n"),
    "openai": shape("client key ", "sk", "-", "E" * 32, "\n"),
    "slack": shape("hook ", "xox", "b-", "1111111111-EXAMPLEEXAMPLE", "\n"),
    "jwt": shape(
        "session ",
        "ey", "JhbGciOiJIUzI1NiJ9", ".",
        "ey", "JzdWIiOiJleGFtcGxlIn0", ".",
        "EXAMPLEsignature_value\n",
    ),
    "bearer": shape("Authorization: ", "Bearer ", "E" * 24, "\n"),
    "assignment": "api_key = EXAMPLEvalue123\n",
}
# Prose that names a key word without assigning a value must never trigger.
PROSE_SAMPLES = (
    "The password was rotated last week and the token is no longer valid.\n",
    "Ask the operator for a password, then paste the token into the form.\n",
    "secret santa assignments are published; cookie recipes follow.\n",
)
# Distinctive fragment asserted absent from the metrics file.
SENTINEL = shape("AK", "IA", "ZZZZSENTINEL9999")


def noisy_log(extra=""):
    """Build compressible log output, optionally carrying one secret line.

    Args:
        extra (str): Text appended after the repeated lines, such as a
            synthetic credential. Empty by default.

    Returns:
        str: Deterministic multi-line text whose repeated lines make it
        compressible and whose estimate exceeds the test threshold.
    """
    return "worker 12 processed batch\n" * 300 + extra


class SecretPatternTests(unittest.TestCase):
    """Detection behavior of :func:`tokenpipe._looks_like_secret` itself."""

    def test_each_pattern_family_triggers(self):
        """Every documented credential shape is recognized."""
        for family, sample in SECRET_SAMPLES.items():
            with self.subTest(family=family):
                self.assertTrue(tokenpipe._looks_like_secret(sample))

    def test_prose_without_an_assigned_value_does_not_trigger(self):
        """Mentioning `password` or `token` in prose is not a credential."""
        for sample in PROSE_SAMPLES:
            with self.subTest(sample=sample[:24]):
                self.assertFalse(tokenpipe._looks_like_secret(sample))

    def test_ordinary_output_and_empty_input_do_not_trigger(self):
        """Plain output, empty text, and non-strings are never guarded."""
        self.assertFalse(tokenpipe._looks_like_secret(noisy_log()))
        self.assertFalse(tokenpipe._looks_like_secret(""))
        self.assertFalse(tokenpipe._looks_like_secret(None))

    def test_json_token_member_with_a_long_value_triggers(self):
        """A JSON blob whose `token` key holds a long value is guarded."""
        blob = '{"user": "lab", "token": "EXAMPLEtokenvalue0123", "ok": true}'
        self.assertTrue(tokenpipe._looks_like_secret(blob))
        self.assertFalse(tokenpipe._looks_like_secret('{"user": "lab", "ok": true}'))

    def test_scan_is_bounded_to_the_first_window(self):
        """A credential past the scan bound is deliberately not detected.

        The bound is the accepted cost of a cheap guard: scanning only the
        first ``SECRET_SCAN_CHARS`` characters keeps very large output cheap,
        at the price of missing a credential that appears only after it.
        """
        padding = "x" * tokenpipe.SECRET_SCAN_CHARS
        late = padding + "\n" + SECRET_SAMPLES["aws"] + "y" * (1024 * 1024)
        self.assertGreater(len(late), 1024 * 1024)
        self.assertFalse(tokenpipe._looks_like_secret(late))
        self.assertTrue(tokenpipe._looks_like_secret(SECRET_SAMPLES["aws"] + padding))


class SecretGuardProcessTests(unittest.TestCase):
    """The guard as it behaves on the payload and native processing paths."""

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
        """Build one bounded hook payload whose category is allowed to replace.

        Args:
            output (str): Decoded tool output to process.
            **extra (object): Additional payload fields such as
                ``tool_call_id``.

        Returns:
            dict[str, object]: Payload allowing replacement of exactly the
            classified category of ``output``.
        """
        value = {
            "output": output,
            "session_id": "session-secret",
            "tool_call_id": "call-secret",
            "tool_name": "exec_command",
            "replace_categories": [tokenpipe.classify(output)],
            "exit_status": 0,
        }
        value.update(extra)
        return value

    def spooled_files(self):
        """List every raw file currently stored under both spool roots.

        Returns:
            list[str]: Absolute paths of spooled raw files; the cleanup marker
            and the repeat index are not spooled output and are excluded.
        """
        found = []
        for root in (tokenpipe._raw_root(), tokenpipe._runtime_raw_root()):
            for directory, _, names in os.walk(root):
                found.extend(
                    os.path.join(directory, name) for name in names
                    if name.endswith(".log")
                )
        return found

    def metrics_text(self):
        """Return the concatenated raw text of every metrics file written.

        Returns:
            str: File contents, or an empty string when nothing was recorded.
        """
        text = ""
        for path in (tokenpipe._metrics_path(), tokenpipe._runtime_metrics_path()):
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    text += handle.read()
        return text

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

    def test_control_output_is_replaced_and_spooled(self):
        """Without a credential the same shape is compressed and spooled."""
        sample = noisy_log()
        result = tokenpipe.process(self.payload(sample), "safe")
        self.assertEqual(result["action"], "replace")
        self.assertTrue(result["raw_ref"])
        self.assertIsNone(result["skip_reason"])
        self.assertEqual(len(self.spooled_files()), 1)

    def test_guarded_output_is_byte_identical_passthrough(self):
        """A credential forces exact output, no spool file, and the reason."""
        sample = noisy_log(SECRET_SAMPLES["github"])
        result = tokenpipe.process(self.payload(sample), "safe")
        self.assertEqual(result["output"], sample)
        self.assertEqual(result["action"], "passthrough")
        self.assertEqual(result["strategy"], "passthrough")
        self.assertEqual(result["skip_reason"], "secret-guard")
        self.assertIsNone(result["raw_ref"])
        self.assertEqual(self.spooled_files(), [])
        self.assertEqual(tokenpipe.load_metrics()[-1]["skip_reason"], "secret-guard")

    def test_guarded_output_records_no_repeat_index_entry(self):
        """The repeat index stores a raw_ref, so guarded output stays out."""
        tokenpipe.process(self.payload(noisy_log(SECRET_SAMPLES["pem"])), "safe")
        self.assertFalse(os.path.exists(tokenpipe._repeat_index_path()))
        tokenpipe.process(self.payload(noisy_log()), "safe")
        self.assertTrue(os.path.exists(tokenpipe._repeat_index_path()))

    def test_metric_row_carries_no_fragment_of_the_secret(self):
        """Only the skip reason is recorded; no matched text reaches metrics."""
        sample = noisy_log("provider key %s loaded\n" % SENTINEL)
        tokenpipe.process(self.payload(sample), "safe")
        text = self.metrics_text()
        self.assertIn("secret-guard", text)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn("SENTINEL", text)
        self.assertNotIn(SENTINEL[:4], text)

    def test_native_path_refuses_to_spool_credential_output(self):
        """`execute_native` applies the same refusal before its own spool."""
        sample = noisy_log(SECRET_SAMPLES["slack"])
        rg = self.executable("rg", "print(%r, end='')\n" % sample)
        output, status = tokenpipe.execute_native(
            [rg, "needle"], "search", "safe", "native-session", "native-secret"
        )
        self.assertEqual(status, 0)
        self.assertIn(sample, output)
        self.assertIn("strategy=passthrough", output.splitlines()[0])
        self.assertNotIn("raw_ref=", output.splitlines()[0])
        self.assertEqual(self.spooled_files(), [])
        self.assertEqual(tokenpipe.load_metrics()[-1]["skip_reason"], "secret-guard")

    def test_native_control_output_is_replaced_and_spooled(self):
        """The same native command without a credential still compresses."""
        rg = self.executable("rg", "print(%r, end='')\n" % noisy_log())
        output, status = tokenpipe.execute_native(
            [rg, "needle"], "search", "safe", "native-session", "native-control"
        )
        self.assertEqual(status, 0)
        self.assertIn("raw_ref=", output.splitlines()[0])
        self.assertEqual(len(self.spooled_files()), 1)


if __name__ == "__main__":
    unittest.main()
