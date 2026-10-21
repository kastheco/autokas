"""Finding intake, serialization, reconciliation and GitHub request cost."""

import copy
import hmac
import hashlib
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import unquote

import runner
from test_merge_gate import PendingStore
from test_queue_ack import REPO, Response
from test_review_dispatch import COMMENTS, PR, REVIEW, ReviewGitHub, event
from test_runner import section


class GateGitHub(ReviewGitHub):
    def __init__(self):
        super().__init__()
        self.pr = {**copy.deepcopy(PR), "labels": []}
        self.findings.append({**copy.deepcopy(COMMENTS[0]), "id": 125,
                              "body": section("Prompt for AI Agents", "Handle a third boundary.")})
        self.requests, self.checks, self.failures = [], {}, {}
        self.next_check = 500

    def __call__(self, request, timeout):
        method = request.get_method()
        path = request.full_url.removeprefix("https://api.github.com/")
        self.requests.append((method, path))
        failure = self.failures.pop((method, path), None)
        if failure:
            raise failure
        if path == f"repos/{REPO}/pulls/42" and method == "GET":
            return Response(copy.deepcopy(self.pr))
        if path == "graphql":
            return Response({"data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}})
        if path == f"repos/{REPO}/labels" and method == "POST":
            return Response(json.loads(request.data))
        if path == f"repos/{REPO}/issues/42/labels" and method == "POST":
            names = {item["name"] for item in self.pr["labels"]}
            names.update(json.loads(request.data)["labels"])
            self.pr["labels"] = [{"name": name} for name in sorted(names)]
            return Response(copy.deepcopy(self.pr["labels"]))
        if path.startswith(f"repos/{REPO}/issues/42/labels/") and method == "DELETE":
            name = unquote(path.rsplit("/", 1)[1])
            self.pr["labels"] = [item for item in self.pr["labels"] if item["name"] != name]
            return Response({})
        if path == f"repos/{REPO}/check-runs" and method == "POST":
            self.next_check += 1
            data = json.loads(request.data)
            check = {**data, "id": self.next_check, "app": {"slug": runner.CONFIG["github_app"]["name"]}}
            self.checks[check["id"]] = check
            return Response(copy.deepcopy(check))
        if path.startswith(f"repos/{REPO}/check-runs/") and method == "PATCH":
            check = self.checks[int(path.rsplit("/", 1)[1])]
            check.update(json.loads(request.data))
            return Response(copy.deepcopy(check))
        return super().__call__(request, timeout)


class FixLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.store = PendingStore()
        self.api = GateGitHub()
        self.jobs, self.finalizers = [], []
        for target, value in (("CLAIMS", self.store), ("_known_fix_labels", set())):
            patcher = patch.object(runner, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, kwargs in (("github_token", {"return_value": "test"}),
                               ("log", {})):
            patcher = patch.object(runner, target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        owners = patch.dict(runner.CONFIG, allowed_owners=[REPO.split("/")[0]])
        owners.start()
        self.addCleanup(owners.stop)
        http = patch.object(runner.urllib.request, "urlopen", self.api)
        http.start()
        self.addCleanup(http.stop)
        pool = patch.object(runner, "PRWorker")
        self.pool = pool.start()
        self.addCleanup(pool.stop)
        self.pool.return_value.run.spawn.side_effect = self.capture_job
        spawn = patch.object(runner.worker, "spawn", side_effect=self.spawn)
        spawn.start()
        self.addCleanup(spawn.stop)

    def capture_job(self, job):
        self.jobs.append(copy.deepcopy(job))
        return Mock(object_id="coding-call")

    def spawn(self, job):
        if job.get("mode") == "fix_finalize":
            self.finalizers.append(copy.deepcopy(job))
        else:
            runner.worker.local(copy.deepcopy(job))
        return Mock(object_id="worker-call")

    def deliver(self, kind="pull_request_review", source=None):
        job = runner.event_job(kind, event(kind, source or REVIEW))
        runner.dispatch(job)
        return job

    def finalize(self, job, state="fixed"):
        runner.worker.local({**copy.deepcopy(job), "mode": "fix_finalize", "fix_state": state})

    def test_review_and_three_inline_deliveries_share_one_check_in_18_requests(self):
        self.deliver()
        for finding in self.api.findings:
            self.deliver("pull_request_review_comment", finding)
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(len(self.api.checks), 1)
        self.assertEqual(next(iter(self.api.checks.values()))["status"], "in_progress")
        self.assertEqual(len(self.api.requests), 18)
        self.assertEqual(sum(path == f"repos/{REPO}/issues/42/labels" for _, path in self.api.requests), 1)
        self.assertFalse(any(key.startswith("intake-fix:") for key in self.store.keys()))
        self.assertTrue(runner.pending_fix_jobs(REPO, 42))

    def test_every_failed_post_intake_stage_cleans_up_and_can_be_redelivered(self):
        for stage in ("metadata", "canonicalization", "routing", "spawn"):
            with self.subTest(stage=stage):
                self.store.values.clear()
                self.api = GateGitHub()
                with patch.object(runner.urllib.request, "urlopen", self.api):
                    if stage in {"metadata", "canonicalization"}:
                        path = f"repos/{REPO}/pulls/42" if stage == "metadata" else f"repos/{REPO}/pulls/42/reviews/987"
                        self.api.failures[("GET", path)] = RuntimeError(stage)
                    put = self.store.put
                    routed_failure = [stage == "routing"]
                    def fail_route(key, value, **kwargs):
                        if key.startswith("routed:") and routed_failure[0]:
                            routed_failure[0] = False
                            raise RuntimeError("routing")
                        return put(key, value, **kwargs)
                    self.pool.return_value.run.spawn.side_effect = RuntimeError("spawn") if stage == "spawn" else self.capture_job
                    with patch.object(self.store, "put", side_effect=fail_route):
                        with self.assertRaises(RuntimeError):
                            self.deliver()
                    self.assertFalse(runner.pending_fix_jobs(REPO, 42))
                    self.assertTrue(all(check["status"] == "completed" for check in self.api.checks.values()))
                    self.pool.return_value.run.spawn.side_effect = self.capture_job
                    self.deliver()
                    self.assertTrue(runner.pending_fix_jobs(REPO, 42))

    def test_outdated_canonical_review_releases_intake_without_a_check(self):
        self.api.review["state"] = "DISMISSED"
        self.deliver()
        self.assertFalse(runner.pending_fix_jobs(REPO, 42))
        self.assertEqual(self.api.checks, {})

    def test_failed_label_delete_is_observable_and_reconciled_after_receipt_release(self):
        self.deliver()
        job = self.jobs[-1]
        self.api.failures[("DELETE", f"repos/{REPO}/issues/42/labels/autokas%3Afixing")] = RuntimeError("502")
        self.finalize(job)
        self.assertNotIn(job["fix_receipt"], self.store.values)
        self.assertTrue(runner.pending_fix_jobs(REPO, 42))
        self.assertEqual(len(self.finalizers), 1)
        self.finalize(self.finalizers.pop(), "blocked")
        self.assertFalse(runner.pending_fix_jobs(REPO, 42))
        self.assertEqual(self.api.pr["labels"], [{"name": "autokas:fixed"}])
        self.assertEqual(self.api.checks[501]["conclusion"], "success")

    def test_concurrent_completion_requests_finalize_serially_and_clear_fixing(self):
        self.deliver()
        first = self.jobs[-1]
        second = {**copy.deepcopy(first), "key": "second-review",
                  "fix_receipt": f"pending-fix:{REPO}:42:second-review"}
        self.store.put(second["fix_receipt"], {"head": self.api.pr["head"]["sha"], "job": second})
        requests = []
        with patch.object(runner.worker, "remote", side_effect=lambda job: requests.append(copy.deepcopy(job))):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda job: runner.finalize_fix_work(copy.deepcopy(job), "fixed"), (first, second)))
        for request in requests:
            runner.worker.local(request)
        self.assertEqual(self.api.pr["labels"], [{"name": "autokas:fixed"}])
        self.assertFalse(runner.pending_fix_jobs(REPO, 42))

    def test_last_completion_restores_a_held_review_on_the_new_current_head(self):
        self.deliver()
        job = self.jobs[-1]
        self.api.pr["head"]["sha"] = "c" * 40
        check = runner.start_review_check(REPO, "c" * 40, "current-review")
        runner.finish_review_check(REPO, check, "current-review", "success", "no findings", number=42)
        self.assertEqual(self.api.checks[check]["conclusion"], "failure")
        real_failure = runner.start_review_check(REPO, "c" * 40, "real-finding")
        runner.finish_review_check(REPO, real_failure, "real-finding", "failure", "1 P1", number=42)
        self.finalize(job)
        self.assertEqual(self.api.checks[check]["conclusion"], "success")
        self.assertEqual(self.api.checks[real_failure]["conclusion"], "failure")
        self.assertFalse(runner.pending_fix_jobs(REPO, 42))

    def test_check_patch_failure_retains_reconciliation_then_retries(self):
        self.deliver()
        job = self.jobs[-1]
        self.api.failures[("PATCH", f"repos/{REPO}/check-runs/501")] = RuntimeError("502")
        self.finalize(job)
        self.assertTrue(runner.pending_fix_jobs(REPO, 42))
        self.assertEqual(len(self.finalizers), 1)
        runner.worker.local(self.finalizers.pop())
        self.assertFalse(runner.pending_fix_jobs(REPO, 42))
        self.assertEqual(self.api.checks[501]["conclusion"], "success")


class WebhookIntakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_webhook_has_no_github_calls_and_failed_spawn_can_be_redelivered(self):
        payload = event("pull_request_review", REVIEW)
        raw = json.dumps(payload).encode()
        request = Mock(body=AsyncMock(return_value=raw), headers={"x-github-event": "pull_request_review",
            "x-hub-signature-256": "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()})
        store = PendingStore()
        claims = Mock()
        claims.put.side_effect = store.put
        claims.put.aio = AsyncMock(side_effect=store.put)
        claims.get.aio = AsyncMock(side_effect=store.get)
        claims.pop.aio = AsyncMock(side_effect=store.pop)
        with patch.dict(os.environ, GITHUB_WEBHOOK_SECRET="test-secret"), \
                patch.dict(runner.CONFIG, allowed_owners=[REPO.split("/")[0]]), \
                patch.object(runner, "CLAIMS", claims), patch.object(runner, "github_request") as github, \
                patch.object(runner.worker, "spawn") as spawn:
            spawn.aio = AsyncMock(side_effect=[RuntimeError("spawn unavailable"), Mock(object_id="call")])
            with self.assertRaises(runner.HTTPException):
                await runner.webhook.local(request)
            self.assertEqual(store.values, {})
            # A process can die after claiming but before registering/spawning.
            key = runner.event_job("pull_request_review", payload)["key"]
            store.put(key, "claimed")
            response = await runner.webhook.local(request)
        self.assertEqual(response.status_code, 202)
        github.assert_not_called()
        self.assertEqual(spawn.aio.await_count, 2)


if __name__ == "__main__":
    unittest.main()
