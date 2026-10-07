"""Completed clean reviews acknowledge the exact current head, once."""

import unittest
from unittest.mock import Mock, patch

import runner
from test_queue_ack import GitHubFake, REPO, Response


def setUpModule():
    owners = patch.dict(runner.CONFIG, allowed_owners=["example-org", "example"])
    owners.start()
    unittest.addModuleCleanup(owners.stop)


HEAD = "a" * 40
BOT = {**runner.CONFIG["coderabbit"], "type": "Bot"}
BODY = ("<!-- recent_review_start -->\n\n"
        "No actionable comments were generated in the recent review.\n"
        "<details><summary>Commits</summary>\n"
        f"Reviewing files that changed from the base of the PR and between {'b' * 40} and {HEAD}.\n"
        "</details>\n<!-- recent_review_end -->")


def event():
    return {
        "action": "edited", "repository": {"full_name": REPO}, "sender": dict(BOT),
        "issue": {"number": 42, "pull_request": {"url": f"https://api.github.com/repos/{REPO}/pulls/42"}},
        "comment": {"id": 123, "user": dict(BOT), "body": BODY,
                    "issue_url": f"https://api.github.com/repos/{REPO}/issues/42"},
    }


class CleanGitHub(GitHubFake):
    def __init__(self):
        super().__init__()
        self.pr = {"number": 42, "state": "open", "base": {"repo": {"full_name": REPO}},
                   "head": {"sha": HEAD, "ref": "feature/clean", "repo": {"full_name": REPO}}}
        self.source = event()["comment"]

    def __call__(self, request, timeout):
        if request.full_url.endswith(f"repos/{REPO}/pulls/42"):
            return Response(self.pr)
        if request.full_url.endswith(f"repos/{REPO}/issues/comments/123"):
            return Response(self.source)
        return super().__call__(request, timeout)


class CleanReviewTests(unittest.TestCase):
    def test_completed_review_posts_once_without_starting_a_fix_run(self):
        payload = event()
        job = runner.event_job("issue_comment", payload)
        self.assertIsNotNone(job)
        claims = set()
        store = Mock()
        store.put.side_effect = lambda key, value, skip_if_exists=False: (
            False if skip_if_exists and key in claims else (claims.add(key) or True)
        )
        store.get.return_value = None
        fake = CleanGitHub()
        with patch.object(runner, "CLAIMS", store), patch.object(runner, "github_token", return_value="test"), \
                patch.object(runner.urllib.request, "urlopen", fake), patch.object(runner, "PRWorker") as worker:
            runner.worker.local(job)
            payload["comment"]["body"] += "\nwalkthrough updated"
            runner.worker.local(runner.event_job("issue_comment", payload))
            worker.assert_not_called()
        self.assertEqual(len(fake.comments), 1)
        self.assertIn(HEAD, fake.comments[0]["body"])
        self.assertIn(f"https://github.com/{REPO}/pull/42#issuecomment-123", fake.comments[0]["body"])
        self.assertEqual([path for method, path, _ in fake.calls if method == "POST"],
                         [f"repos/{REPO}/issues/42/comments"])

    def test_incomplete_unscoped_and_spoofed_results_do_not_enter_intake(self):
        variants = []
        for body in ("No actionable comments were generated in the recent review.",
                     BODY.replace("<!-- recent_review_end -->", ""),
                     BODY.replace(HEAD, "unknown"),
                     "<!-- review_in_progress_start -->\n" + BODY,
                     BODY.replace("No actionable comments were generated in the recent review.", "Review in progress.")):
            payload = event()
            payload["comment"]["body"] = body
            variants.append(payload)
        for field in ("sender", "comment"):
            payload = event()
            (payload[field]["user"] if field == "comment" else payload[field])["id"] = 1
            variants.append(payload)
        for payload in variants:
            with self.subTest(payload=payload):
                self.assertIsNone(runner.event_job("issue_comment", payload))

    def test_worker_rejects_results_that_became_stale_or_untrusted(self):
        mutations = [
            lambda f: f.pr["head"].update(sha="c" * 40),
            lambda f: f.pr.update(state="closed"),
            lambda f: f.pr["head"]["repo"].update(full_name="outsider/example-app"),
            lambda f: f.source["user"].update(id=1),
            lambda f: f.source.update(body=BODY.replace(HEAD, "c" * 40)),
            lambda f: f.source.update(body="review in progress"),
            lambda f: f.source.update(issue_url=f"https://api.github.com/repos/{REPO}/issues/43"),
        ]
        for mutate in mutations:
            fake = CleanGitHub()
            mutate(fake)
            with self.subTest(mutate=mutate), patch.object(runner, "github_token", return_value="test"), \
                    patch.object(runner.urllib.request, "urlopen", fake), patch.object(runner, "PRWorker") as worker:
                runner.worker.local(runner.event_job("issue_comment", event()))
                worker.assert_not_called()
            self.assertEqual(fake.comments, [])


if __name__ == "__main__":
    unittest.main()
