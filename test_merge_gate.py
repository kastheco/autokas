"""A clean incremental review can't outrun unpublished review fixes."""

import copy
import unittest
from unittest.mock import Mock, patch

import runner
from test_clean_review import HEAD
from test_queue_ack import REPO
from test_review_dispatch import ClaimStore, REVIEW
from test_runner import section


class PendingStore(ClaimStore):
    def keys(self):
        return list(self.values)

    def pop(self, key, default=None):
        return self.values.pop(key, default)


class MergeGateTests(unittest.TestCase):
    def setUp(self):
        self.store = PendingStore()
        self.pr = {"number": 42, "state": "open", "labels": [],
                   "base": {"repo": {"full_name": REPO}},
                   "head": {"sha": HEAD, "ref": "feature/gate", "repo": {"full_name": REPO}}}
        claims = patch.object(runner, "CLAIMS", self.store)
        claims.start()
        self.addCleanup(claims.stop)

    def finish(self, *, labels=(), pending=False, unresolved=False, lookup_error=None, conclusion="success"):
        self.pr["labels"] = [{"name": name} for name in labels]
        if pending:
            self.store.put(f"pending-fix:{REPO}:42:queued", {})
        with patch.object(runner, "github", return_value=self.pr), \
                patch.object(runner, "unresolved_review_findings", return_value=unresolved,
                             side_effect=lookup_error), \
                patch.object(runner, "github_request") as request:
            runner.finish_review_check(REPO, 88, "review", conclusion, "no findings", number=42)
        return request.call_args.args[2]

    def test_success_is_held_for_label_queued_job_and_open_finding(self):
        for kwargs in ({"labels": ["autokas:fixing"]}, {"pending": True}, {"unresolved": True},
                       {"lookup_error": RuntimeError("unavailable")}):
            with self.subTest(kwargs=kwargs):
                self.store.values.clear()
                result = self.finish(**kwargs)
                self.assertEqual(result["conclusion"], "failure")
                self.assertIn("pending", result["output"]["title"])

    def test_success_only_after_work_and_threads_are_clear(self):
        self.assertEqual(self.finish()["conclusion"], "success")

    def test_skipped_and_neutral_cannot_bypass_pending_fixes(self):
        for conclusion in ("skipped", "neutral"):
            with self.subTest(conclusion=conclusion):
                result = self.finish(pending=True, conclusion=conclusion)
                self.assertEqual(result["conclusion"], "failure")
                self.assertIn("pending", result["output"]["title"])


    def test_one_completed_job_does_not_clear_another_queued_job(self):
        jobs = [{"repo": REPO, "pr": 42, "key": str(i),
                 "fix_receipt": f"pending-fix:{REPO}:42:{i}"} for i in range(2)]
        for job in jobs:
            self.store.put(job["fix_receipt"], {"check": 88, "head": HEAD})
        with patch.object(runner, "github", return_value=self.pr), \
                patch.object(runner, "set_fix_label") as label, \
                patch.object(runner, "finish_review_check") as finish:
            runner.finish_fix_work(jobs[0], "fixed")
            self.assertEqual(label.call_args.args[2], "fixing")
            self.assertTrue(runner.pending_fix_jobs(REPO, 42))
            runner.finish_fix_work(jobs[1], "blocked")
            self.assertEqual(label.call_args.args[2], "blocked")
            self.assertEqual(finish.call_args.args[3], "failure")
        self.assertFalse(runner.pending_fix_jobs(REPO, 42))


    def test_resolved_threads_and_non_actionable_or_human_threads_do_not_hold_gate(self):
        def thread(body, *, resolved=False, login=None, actor="Bot"):
            return {"isResolved": resolved, "comments": {"nodes": [{"body": body,
                    "author": {"__typename": actor, "login": login or runner.CONFIG["coderabbit"]["login"]}}]}}

        body = section("Prompt for AI Agents", "Fix the parser boundary.")
        pages = [
            {"nodes": [thread(body, resolved=True), thread("thanks"), thread(body, actor="User")],
             "pageInfo": {"hasNextPage": True, "endCursor": "next"}},
            {"nodes": [thread(body, login=runner.CONFIG["coderabbit"]["login"].removesuffix("[bot]"))],
             "pageInfo": {"hasNextPage": False, "endCursor": None}},
        ]
        responses = [{"data": {"repository": {"pullRequest": {"reviewThreads": page}}}} for page in pages]
        with patch.object(runner, "github_request", side_effect=responses) as request:
            self.assertTrue(runner.unresolved_review_findings(REPO, 42))
        self.assertEqual(request.call_args.args[2]["variables"]["cursor"], "next")
        pages[1]["nodes"][0]["isResolved"] = True
        with patch.object(runner, "github_request", side_effect=copy.deepcopy(responses)):
            self.assertFalse(runner.unresolved_review_findings(REPO, 42))

    def test_clean_ack_is_explicitly_range_only_with_other_findings_open(self):
        job = {"repo": REPO, "pr": 42, "key": "clean", "comment": REVIEW["id"],
               "kind": "issue_comment", "mode": "clean_review", "head": HEAD}
        with patch.object(runner, "github_request", return_value={"id": 1, "html_url": "https://example.test/1"}) as request:
            runner.acknowledge_review(job)
        body = request.call_args.args[2]["body"]
        self.assertNotIn("✅ clean", body)
        self.assertIn("only that commit range", body)
        self.assertIn("not other open findings or fixes", body)


if __name__ == "__main__":
    unittest.main()
