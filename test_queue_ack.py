"""Hermetic GitHub queue acknowledgment behavior."""

import io
import json
import unittest
from unittest.mock import Mock, patch

import runner


REPO = "example-org/example-app"


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(json.dumps(data).encode())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class GitHubFake:
    def __init__(self, *, lost_response=False, receipt_user=77, reply_source=None):
        self.calls = []
        self.comments = []
        self.lost_response = lost_response
        self.receipt_user = receipt_user
        self.reply_source = reply_source

    def __call__(self, request, timeout):
        path = request.full_url.removeprefix("https://api.github.com/")
        self.calls.append((request.get_method(), path, timeout))
        if path == f"repos/{REPO}/pulls/42":
            return Response({"number": 42, "head": {"ref": "feature/queue"}})
        if path == "user":
            return Response({"id": 77})
        if request.get_method() == "POST":
            body = json.loads(request.data)["body"]
            self.comments.append({"body": body, "user": {"id": self.receipt_user},
                                  "in_reply_to_id": self.reply_source})
            if self.lost_response:
                raise TimeoutError("response lost after publication")
            return Response({"id": 901})
        if "comments?" in path:
            return Response(self.comments)
        raise AssertionError(path)


def job(kind="pull_request_review_comment"):
    return {"repo": REPO, "pr": 42, "kind": kind, "comment": 123,
            "key": f"{REPO}:{kind}:123:hash", "prompt": "a prompt"}


class QueueAcknowledgmentTests(unittest.TestCase):
    def setUp(self):
        self.claims = set()
        self.claim = Mock()
        self.claim.put.side_effect = lambda key, _value, skip_if_exists: (
            False if key in self.claims else (self.claims.add(key) or True)
        )
        self.claim_patch = patch.object(runner, "CLAIMS", self.claim)
        self.claim_patch.start()
        self.addCleanup(self.claim_patch.stop)
        self.token_patch = patch.object(runner, "github_token", return_value="fake-installation-token")
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)
        self.logs = []
        self.log_patch = patch.object(runner, "log", side_effect=lambda event, **fields: self.logs.append(event))
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)

    def test_review_comment_reply_is_queued_once_after_spawn(self):
        fake = GitHubFake()
        spawn = Mock(return_value=Mock(object_id="call-1"))
        with patch.object(runner.urllib.request, "urlopen", fake), patch.object(runner, "PRWorker") as cls:
            cls.return_value.run.spawn = spawn
            runner.worker.local(job())
            runner.worker.local(job())  # infrastructure redelivery of the dispatcher
        posts = [path for method, path, _ in fake.calls if method == "POST"]
        self.assertEqual(posts, [f"repos/{REPO}/pulls/42/comments/123/replies"])
        self.assertEqual(spawn.call_count, 2)  # ack dedupe does not cancel already-dispatched work
        self.assertIn("ack_already_claimed", self.logs)

    def test_whole_review_and_issue_comment_link_their_exact_sources(self):
        for kind, anchor in (("pull_request_review", "pullrequestreview-123"),
                             ("issue_comment", "issuecomment-123")):
            with self.subTest(kind=kind):
                fake = GitHubFake()
                with patch.object(runner.urllib.request, "urlopen", fake):
                    runner.acknowledge_queued(job(kind))
                self.assertIn(f"https://github.com/{REPO}/pull/42#{anchor}", fake.comments[0]["body"])
                self.assertEqual([path for method, path, _ in fake.calls if method == "POST"],
                                 [f"repos/{REPO}/issues/42/comments"])

    def test_lost_write_response_requires_own_account_and_source_receipt(self):
        for user_id, reply_source, expected in ((77, 123, "ack_reconciled"),
                                                (999, 123, "ack_uncertain"),
                                                (77, 999, "ack_uncertain")):
            with self.subTest(user_id=user_id, reply_source=reply_source):
                self.claims.clear()
                self.logs.clear()
                fake = GitHubFake(lost_response=True, receipt_user=user_id, reply_source=reply_source)
                with patch.object(runner.urllib.request, "urlopen", fake):
                    runner.acknowledge_queued(job())
                    runner.acknowledge_queued(job())
                self.assertIn(expected, self.logs)
                self.assertEqual([method for method, _, _ in fake.calls], ["POST", "GET", "GET"])

    def test_generated_docs_pr_and_unavailable_metadata_do_not_spawn_or_ack(self):
        with patch.object(runner, "PRWorker") as cls, patch.object(runner, "github") as github:
            github.return_value = {"head": {"ref": runner.CONFIG["docs_update"]["branch_prefix"] + "42"}}
            runner.worker.local(job())
            github.side_effect = TimeoutError("metadata unavailable")
            with self.assertRaises(TimeoutError):
                runner.worker.local(job())
            cls.assert_not_called()
        self.assertIn("generated_docs_ignored", self.logs)
        self.assertIn("pr_metadata_unavailable", self.logs)
        self.assertEqual(self.claims, set())

    def test_docs_job_skips_coderabbit_ack_and_failed_spawn_never_acks(self):
        docs = {"repo": REPO, "pr": 42, "kind": "pull_request", "key": "docs-42"}
        with patch.object(runner, "PRWorker") as cls, patch.object(runner, "github") as github:
            cls.return_value.run.spawn.return_value = Mock(object_id="call-docs")
            runner.worker.local(docs)
            github.assert_not_called()
            self.assertEqual(self.claims, set())
            cls.return_value.run.spawn.side_effect = RuntimeError("spawn failed")
            with self.assertRaisesRegex(RuntimeError, "spawn failed"):
                runner.worker.local(job())
            self.assertEqual(self.claims, set())


    def test_uncertain_ack_does_not_cancel_dispatched_agent(self):
        fake = GitHubFake(lost_response=True, receipt_user=999, reply_source=123)
        with patch.object(runner.urllib.request, "urlopen", fake), patch.object(runner, "PRWorker") as cls:
            cls.return_value.run.spawn.return_value = Mock(object_id="call-queued")
            runner.worker.local(job())
            cls.return_value.run.spawn.assert_called_once_with(job())
        self.assertIn("routed", self.logs)
        self.assertIn("ack_uncertain", self.logs)
        self.assertEqual([method for method, _, _ in fake.calls], ["GET", "POST", "GET", "GET"])


if __name__ == "__main__":
    unittest.main()
