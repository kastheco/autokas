"""Behavior regressions for consultation completion and private output capture."""

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

import consult


class ConsultationOutputTests(unittest.TestCase):
    def invoke(self, directory: str, events: list[dict | str]) -> tuple[int, str, str]:
        response = MagicMock()
        response.headers.get_content_type.return_value = "text/event-stream"
        response.__enter__.return_value = response
        response.__iter__.return_value = iter(
            ("data: " + (event if isinstance(event, str) else json.dumps(event)) + "\n").encode()
            for event in events
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, JARVIS_RUNNER_TOKEN="private-test-token",
                       JARVIS_CONSULT_URL="https://jarvis.invalid/consult"),
            patch.dict(os.environ, OMP_JOB_REPO="example-org/example-app"),
            patch("sys.argv", ["consult.py", "--request-id", "89c72b1e-56f1-457e-aae8-1677a9cec379"]),
            patch("sys.stdin", io.StringIO("Assess the scoped refactor.")),
            patch("tempfile.tempdir", directory),
            patch("consult.urllib.request.build_opener") as opener,
            redirect_stdout(stdout), redirect_stderr(stderr),
        ):
            opener.return_value.open.return_value = response
            code = consult.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_long_completed_answer_is_private_and_readable_without_stdout_truncation(self) -> None:
        answer = "preserve behavior café\n" * 100 + "private-test-token\nleave execution guards unchanged."
        with tempfile.TemporaryDirectory() as directory:
            code, stdout, stderr = self.invoke(directory, [
                {"type": "text-delta", "delta": answer},
                {"type": "finish", "finishReason": "stop"}, "[DONE]",
            ])
            self.assertEqual((code, stderr), (0, ""))
            self.assertLess(len(stdout), 768)
            receipt = json.loads(stdout)
            advice = Path(receipt["advice_file"])
            self.assertEqual(advice.parent, Path(directory))
            self.assertEqual(advice.read_text(), answer.replace("private-test-token", "[redacted]"))
            self.assertEqual(advice.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("leave execution guards", stdout)

    def test_incomplete_stream_never_publishes_advice_or_a_receipt(self) -> None:
        for ending in ([], [{"type": "finish", "finishReason": "length"}, "[DONE]"], ["[DONE]"]):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory() as directory:
                code, stdout, stderr = self.invoke(directory, [
                    {"type": "text-delta", "delta": "partial advice"}, *ending,
                ])
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("blocked:", stderr)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_other_owners_and_missing_repo_cannot_contact_jarvis(self) -> None:
        for repo in ("", "example-owner-4/app", "example-owner-2/app", "other/example-org",
                     "example-org-else/example-app", "example-org/", "example-org/a/b"):
            with (
                self.subTest(repo=repo),
                patch.dict(os.environ, OMP_JOB_REPO=repo, JARVIS_RUNNER_TOKEN="test",
                           JARVIS_CONSULT_URL="https://jarvis.invalid/consult"),
                patch("sys.argv", ["consult.py", "--request-id", "89c72b1e-56f1-457e-aae8-1677a9cec379"]),
                patch("consult.urllib.request.build_opener") as opener,
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(consult.main(), 1)
                opener.assert_not_called()

if __name__ == "__main__":
    unittest.main()
