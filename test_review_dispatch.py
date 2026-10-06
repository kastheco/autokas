"""Review and inline deliveries must share one coding run and post no queued comments."""

import copy
import unittest
from unittest.mock import Mock, patch

import runner
from test_queue_ack import GitHubFake, REPO, Response
from test_runner import section


BOT = {**runner.CONFIG["coderabbit"], "type": "Bot"}
PR = {"number": 42, "state": "open", "base": {"repo": {"full_name": REPO}},
      "head": {"sha": "a" * 40, "ref": "feature/review", "repo": {"full_name": REPO}}}
REVIEW = {"id": 987, "user": BOT, "state": "COMMENTED", "commit_id": "a" * 40,
          "pull_request_url": f"https://api.github.com/repos/{REPO}/pulls/42",
          "html_url": f"https://github.com/{REPO}/pull/42#pullrequestreview-987",
          "body": section("Prompt to fix review comments", "Fix the two inline findings in the parser.")}
COMMENTS = [{"id": n, "user": BOT, "pull_request_review_id": 987,
             "pull_request_url": REVIEW["pull_request_url"],
             "html_url": f"https://github.com/{REPO}/pull/42#discussion_r{n}",
             "body": section("Prompt for AI Agents", prompt)}
            for n, prompt in ((123, "Fix the empty-input parsing boundary."),
                              (124, "Preserve the trailing delimiter."))]


def event(kind, source):
    return {"action": "submitted" if kind == "pull_request_review" else "created",
            "repository": {"full_name": REPO}, "sender": dict(BOT), "pull_request": copy.deepcopy(PR),
            "review" if kind == "pull_request_review" else "comment": copy.deepcopy(source)}


class ClaimStore:
    def __init__(self):
        self.values = {}

    def put(self, key, value, skip_if_exists=False):
        if skip_if_exists and key in self.values:
            return False
        self.values[key] = copy.deepcopy(value)
        return True

    def get(self, key, default=None):
        return copy.deepcopy(self.values.get(key, default))


class ReviewGitHub(GitHubFake):
    def __init__(self):
        super().__init__()
        self.review = copy.deepcopy(REVIEW)
        self.findings = copy.deepcopy(COMMENTS)

    def __call__(self, request, timeout):
        path = request.full_url.removeprefix("https://api.github.com/")
        if request.get_method() == "GET":
            if path == f"repos/{REPO}/pulls/42":
                return Response(PR)
            if path == f"repos/{REPO}/pulls/42/reviews/987":
                return Response(self.review)
            if path.startswith(f"repos/{REPO}/pulls/42/reviews/987/comments?"):
                page = int(path.rsplit("page=", 1)[1])
                return Response(self.findings[(page - 1) * 100:page * 100])
            if path.startswith(f"repos/{REPO}/pulls/comments/"):
                number = int(path.rsplit("/", 1)[1])
                return Response(next(c for c in self.findings if c["id"] == number))
        if request.get_method() == "POST":
            super().__call__(request, timeout)
            self.comments[-1]["id"] = 900 + len(self.comments)
            self.comments[-1]["html_url"] = f"https://github.com/{REPO}/pull/42#discussion_r{self.comments[-1]['id']}"
            self.comments[-1]["in_reply_to_id"] = int(path.split("/")[-2]) if path.endswith("/replies") else None
            return Response(self.comments[-1])
        return super().__call__(request, timeout)


class ReviewDispatchTests(unittest.TestCase):
    def run_events(self, fake, deliveries):
        store = ClaimStore()
        jobs = []
        with patch.object(runner, "CLAIMS", store), patch.object(runner, "github_token", return_value="test"), \
                patch.object(runner.urllib.request, "urlopen", fake), patch.object(runner, "PRWorker") as worker:
            worker.return_value.run.spawn.side_effect = lambda job: (jobs.append(copy.deepcopy(job)) or Mock(object_id="run"))
            for kind, source in deliveries:
                job = runner.event_job(kind, event(kind, source))
                self.assertIsNotNone(job)
                runner.worker.local(job)
        return jobs

    def test_both_event_orders_start_one_job_without_queue_comments(self):
        review = ("pull_request_review", REVIEW)
        first, second = [("pull_request_review_comment", c) for c in COMMENTS]
        for deliveries in ((review, first, second, review), (first, second, review, first)):
            fake = ReviewGitHub()
            with self.subTest(first=deliveries[0][0]):
                jobs = self.run_events(fake, deliveries)
                self.assertEqual(len(jobs), 1)
                self.assertEqual({t["comment"] for t in jobs[0]["targets"]}, {123, 124})
                self.assertEqual(jobs[0]["prompt"], runner.agent_prompt(REVIEW["body"]))
                self.assertEqual(fake.comments, [])

    def test_review_without_inline_findings_keeps_one_whole_review_job(self):
        fake = ReviewGitHub()
        fake.findings = []
        jobs = self.run_events(fake, [("pull_request_review", REVIEW)] * 2)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["targets"], [])
        self.assertEqual(fake.comments, [])

    def test_inline_delivery_can_recover_a_review_without_an_aggregate_prompt(self):
        fake = ReviewGitHub()
        fake.review["body"] = ""
        jobs = self.run_events(fake, [("pull_request_review_comment", c) for c in COMMENTS])
        self.assertEqual(len(jobs), 1)
        for c in COMMENTS:
            self.assertIn(runner.agent_prompt(c["body"]), jobs[0]["prompt"])
        self.assertEqual(fake.comments, [])

    def test_review_comments_are_paginated_without_targeting_other_authors(self):
        fake = ReviewGitHub()
        foreign = [{**copy.deepcopy(COMMENTS[0]), "id": 1000 + n, "user": {"id": 1, "login": "outsider"}}
                   for n in range(99)]
        fake.findings = [fake.findings[0], *foreign, fake.findings[1]]
        jobs = self.run_events(fake, [("pull_request_review", REVIEW)])
        self.assertEqual(len(jobs), 1)
        self.assertEqual({t["comment"] for t in jobs[0]["targets"]}, {123, 124})
        self.assertEqual(fake.comments, [])

    def test_pending_dismissed_or_spoofed_review_cannot_start_from_inline_event(self):
        for change in ({"state": "PENDING"}, {"state": "DISMISSED"}, {"user": {**BOT, "id": 1}}):
            fake = ReviewGitHub()
            fake.review.update(change)
            with self.subTest(change=change):
                self.assertEqual(self.run_events(fake, [("pull_request_review_comment", COMMENTS[0])]), [])
                self.assertEqual(fake.comments, [])


if __name__ == "__main__":
    unittest.main()
