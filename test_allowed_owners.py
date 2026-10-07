"""Only approved repository owners may dispatch or execute paid jobs."""

import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

import runner
from test_pr_review import pr_event, comment_event
from test_runner import review_event, bugbot_comment_event


class OwnerBoundaryTests(unittest.TestCase):
    def test_exact_case_insensitive_owner_matching_and_fail_closed_config(self):
        with patch.dict(runner.CONFIG, allowed_owners=["Example-Org"]):
            self.assertTrue(runner.allowed_repository("EXAMPLE-ORG/app"))
            for repo in ("example-org-evil/app", "evil/example-org", "example-org", "example-org/", "example-org/app/extra", None):
                with self.subTest(repo=repo):
                    self.assertFalse(runner.allowed_repository(repo))
        for owners in ([], None, "example-org", {}):
            with self.subTest(owners=owners), patch.dict(runner.CONFIG, allowed_owners=owners):
                self.assertFalse(runner.allowed_repository("example-org/app"))
        config = dict(runner.CONFIG)
        config.pop("allowed_owners", None)
        with patch.dict(runner.CONFIG, config, clear=True):
            self.assertFalse(runner.allowed_repository("example-org/app"))

    def test_owner_gate_precedes_reviews_findings_commands_and_bypasses(self):
        events = [("pull_request", pr_event("opened")),
                  ("pull_request_review", review_event()),
                  ("pull_request_review_comment", bugbot_comment_event()),
                  comment_event("@autokas fix the parser"),
                  comment_event("!kas fix review 777")]
        for kind, payload in events:
            with self.subTest(kind=kind), patch.dict(runner.CONFIG, allowed_owners=["example-org"]):
                self.assertIsNotNone(runner.event_job(kind, payload))
            with self.subTest(kind=kind), patch.dict(runner.CONFIG, allowed_owners=["other-owner"]):
                self.assertIsNone(runner.event_job(kind, payload))

    def test_removed_owner_cannot_execute_queued_or_direct_jobs(self):
        with patch.dict(runner.CONFIG, allowed_owners=[]), patch.object(runner, "CLAIMS") as claims, \
                patch.object(runner, "github") as github, patch.object(runner, "PRWorker") as pool:
            for mode in ("pr_review", "command", "docs_update", "review_fix_request", "finding"):
                job = {"repo": "example-org/app", "mode": mode}
                runner.dispatch(job)
                runner.worker.local(job)
                runner.pr_review.local(job)
            claims.put.assert_not_called()
            github.assert_not_called()
            pool.assert_not_called()
        with patch.dict(runner.CONFIG, allowed_owners=[]), patch.object(runner, "CLAIMS") as claims:
            runner.PRWorker(pr_key="example-org/app#1").run.local({"repo": "example-org/app"})
            claims.put.assert_not_called()


class SignedWebhookOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_unapproved_installation_never_claims_or_spawns(self):
        raw = json.dumps(pr_event("opened")).encode()
        request = Mock()
        request.body = AsyncMock(return_value=raw)
        request.headers = {"x-github-event": "pull_request", "x-hub-signature-256":
                           "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()}
        with patch.dict(os.environ, GITHUB_WEBHOOK_SECRET="test-secret"), \
                patch.dict(runner.CONFIG, allowed_owners=["other-owner"]), \
                patch.object(runner, "CLAIMS") as claims, patch.object(runner, "worker") as worker:
            response = await runner.webhook.local(request)
            self.assertEqual(json.loads(response.body)["status"], "ignored")
            claims.put.aio.assert_not_called()
            worker.spawn.aio.assert_not_called()

    async def test_approved_installation_can_dispatch(self):
        raw = json.dumps(pr_event("opened")).encode()
        request = Mock()
        request.body = AsyncMock(return_value=raw)
        request.headers = {"x-github-event": "pull_request", "x-hub-signature-256":
                           "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()}
        with patch.dict(os.environ, GITHUB_WEBHOOK_SECRET="test-secret"), \
                patch.dict(runner.CONFIG, allowed_owners=["example-org"]), \
                patch.object(runner, "CLAIMS") as claims, patch.object(runner, "worker") as worker:
            claims.put.aio = AsyncMock(return_value=True)
            worker.spawn.aio = AsyncMock(return_value=Mock(object_id="test-call"))
            response = await runner.webhook.local(request)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(json.loads(response.body)["status"], "accepted")
            worker.spawn.aio.assert_awaited_once()
