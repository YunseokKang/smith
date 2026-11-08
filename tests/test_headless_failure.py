import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from smith import headless

SCHEMA = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}


def _fail():
    raise PermissionError(13, "Access is denied: C:\private\path")


class DescribeFailureTests(unittest.TestCase):
    def test_a_failure_is_described_without_its_message(self):
        self.assertEqual(headless.describe_failure(headless.HeadlessError("exit-1", "error_max_turns")),
                         "exit-1: error_max_turns")
        try:
            _fail()
        except PermissionError as error:
            text = headless.describe_failure(error)
        self.assertRegex(text, r"^PermissionError \(errno 13\) at test_headless_failure\.py:\d+$")
        self.assertNotIn("private", text)       # Messages can carry paths, payload text or secrets.


class CleanupTests(unittest.TestCase):
    def test_a_locked_workdir_does_not_discard_a_valid_answer(self):
        envelope = {"subtype": "success", "is_error": False, "structured_output": {"answer": "ok"}, "total_cost_usd": 0.1}
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(envelope), ""))
        def locked(name, onexc):    # What rmtree does on Windows when a child process holds the directory.
            onexc(lambda path: _fail(), name, PermissionError(13, "in use"))
        with mock.patch("shutil.rmtree", side_effect=locked):
            output, cost = headless.run("hi", system_prompt="s", schema=SCHEMA, executable=Path("claude.exe"), model=None,
                                        budget_usd="1.00", timeout_seconds=5, runner=runner)
        self.assertEqual((output, cost), ({"answer": "ok"}, "0.1"))


if __name__ == "__main__":
    unittest.main()
