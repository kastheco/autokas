"""Behavior regressions for CodeRabbit review and comment intake."""

import copy
import unittest
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from runner import CONFIG, agent_prompt, docs_worker, event_job


BOT = {"login": "coderabbitai[bot]", "id": 136622811, "type": "Bot"}
REPO = "example-org/example-app"
PR_URL = f"https://api.github.com/repos/{REPO}/pulls/142"


def section(heading: str, prompt: str) -> str:
    return f"<details>\n<summary>{heading}</summary>\n\n```\n{prompt}\n```\n\n</details>"


def review_event() -> dict:
    return {
        "action": "submitted",
        "repository": {"full_name": REPO},
        "sender": dict(BOT),
        "pull_request": {"number": 142, "base": {"repo": {"full_name": REPO}}},
        "review": {
            "id": 9000000001,
            "user": dict(BOT),
            "state": "commented",
            "pull_request_url": PR_URL,
            "body": section("Prompt for AI Agents", "Move the validation into the shared core function."),
        },
    }


class ReviewIntakeTests(unittest.TestCase):
    def test_submitted_review_becomes_a_scoped_job(self) -> None:
        job = event_job("pull_request_review", review_event())
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual((job["repo"], job["pr"], job["comment"], job["kind"]),
                         (REPO, 142, 9000000001, "pull_request_review"))
        self.assertEqual(job["prompt"], "Move the validation into the shared core function.")
        edited = review_event()
        edited["action"] = "edited"
        self.assertEqual(event_job("pull_request_review", edited), job)

    def test_pending_and_dismissed_reviews_do_not_run(self) -> None:
        for state in ("pending", "dismissed"):
            event = review_event()
            event["review"]["state"] = state
            with self.subTest(state=state):
                self.assertIsNone(event_job("pull_request_review", event))
        for action in ("dismissed", "created"):
            event = review_event()
            event["action"] = action
            with self.subTest(action=action):
                self.assertIsNone(event_job("pull_request_review", event))

    def test_review_identity_and_pr_relationship_are_required(self) -> None:
        base = review_event()
        variants = []
        event = copy.deepcopy(base)
        event["review"]["user"]["id"] = 1
        variants.append(event)
        event = copy.deepcopy(base)
        event["sender"]["id"] = 1
        variants.append(event)
        event = copy.deepcopy(base)
        event["review"]["pull_request_url"] = PR_URL.replace("142", "143")
        variants.append(event)
        event = copy.deepcopy(base)
        event["pull_request"]["base"]["repo"]["full_name"] = "outsider/example-app"
        variants.append(event)
        for event in variants:
            with self.subTest(event=event):
                self.assertIsNone(event_job("pull_request_review", event))

    def test_aggregate_prompt_is_used_when_no_individual_prompt_exists(self) -> None:
        event = review_event()
        event["review"]["body"] = section("Prompt to fix review comments", "Fix the validated scope mismatch.")
        job = event_job("pull_request_review", event)
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job["prompt"], "Fix the validated scope mismatch.")

    def test_aggregate_does_not_duplicate_individual_findings(self) -> None:
        body = section("Prompt for AI Agents", "Fix finding A.")
        body += section("Prompt for AI Agents", "Fix finding B.")
        body += section("Prompt to fix review comments", "Fix finding A and finding B.")
        self.assertEqual(agent_prompt(body), "Fix finding A.\n\nFix finding B.")

    def test_summary_without_fix_prompt_does_not_start_work(self) -> None:
        event = review_event()
        event["review"]["body"] = "No actionable findings."
        self.assertIsNone(event_job("pull_request_review", event))

    def test_new_exact_approval_allows_one_distinct_attempt(self) -> None:
        with patch.dict(CONFIG["owner_approvals"], {}, clear=True):
            original = event_job("pull_request_review", review_event())
            assert original is not None
            CONFIG["owner_approvals"][f"{REPO}#143"] = "Approval for another PR."
            self.assertEqual(event_job("pull_request_review", review_event()), original)
            CONFIG["owner_approvals"][f"{REPO}#142"] = "Approve this scoped refactor."
            approved = event_job("pull_request_review", review_event())
            assert approved is not None
            self.assertNotEqual(approved["key"], original["key"])
            self.assertEqual(approved["prompt"], original["prompt"])
            self.assertEqual(event_job("pull_request_review", review_event()), approved)


class DocsMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.final_head = "b" * 40
        self.current_head = self.final_head
        self.merged_head = None
        self.merge_attempts = 0
        self.move_head = False
        self.lose_response = False
        self.omit_merge_commit = False
        self.job = {
            "repo": "example/docs", "pr": 1, "source_sha": "a" * 40,
            "source_branch": "feature",
        }
        self.api = "repos/example/docs/pulls"
        self.branch = f'{CONFIG["docs_update"]["branch_prefix"]}1-{"a" * 12}'
        self.followup = {
            "number": 2,
            "head": {"repo": {"full_name": "example/docs"},
                     "ref": self.branch, "sha": self.final_head},
            "base": {"repo": {"full_name": "example/docs"}, "ref": "main"},
        }
        patches = [
            patch.dict(CONFIG["docs_update"]["repositories"], {
                "example/docs": {"branch": "main", "folders": ["docs"]},
            }),
            patch("runner.github", side_effect=self.read_github),
            patch("runner.github_request", side_effect=self.write_github),
            patch("runner.subprocess.Popen"),
            patch("runner.os.killpg"),
            patch("runner.log"),
        ]
        mocks = []
        for patcher in patches:
            mocks.append(patcher.start())
            self.addCleanup(patcher.stop)
        mocks[3].return_value.wait.return_value = 0
        self.log = mocks[5]

    def read_github(self, path):
        if path == f"{self.api}/1":
            return {
                "state": "closed", "merged_at": "2026-09-27T00:00:00Z",
                "merge_commit_sha": self.job["source_sha"],
                "base": self.followup["base"], "head": self.followup["head"],
            }
        if path in (f"{self.api}/1/files?per_page=100", f"{self.api}/2/files?per_page=100"):
            if path == f"{self.api}/2/files?per_page=100" and self.move_head:
                self.current_head = "c" * 40
            return [{"filename": "docs/guide.md"}]
        if path == f"{self.api}?state=open&head=example:{self.branch}&base=main":
            return []
        if path == f"{self.api}/2":
            return {
                **self.followup,
                "merged_at": "2026-09-27T00:00:00Z" if self.merged_head else None,
                "merge_commit_sha": "d" * 40 if self.merged_head and not self.omit_merge_commit else None,
            }
        raise AssertionError(f"unexpected GitHub read: {path}")

    def write_github(self, method, path, payload):
        if method == "POST" and path == self.api:
            return copy.deepcopy(self.followup)
        if method == "PUT" and path == f"{self.api}/2/merge":
            self.merge_attempts += 1
            if payload.get("sha", self.current_head) != self.current_head:
                raise HTTPError(path, 409, "Head branch was modified", {}, None)
            self.merged_head = self.current_head
            if self.lose_response:
                raise TimeoutError("merge response lost")
            return {"merged": True}
        raise AssertionError(f"unexpected GitHub write: {method} {path}")

    def run_command(self, args, cwd):
        if args == ["git", "rev-parse", "origin/main"]:
            return "0" * 40
        if args == ["git", "rev-parse", "HEAD"]:
            return self.final_head
        if args[:3] == ["git", "diff", "--name-only"]:
            return "docs/guide.md"
        if args[:2] in (["gh", "auth"], ["git", "clone"], ["git", "fetch"],
                        ["git", "checkout"], ["git", "ls-remote"],
                        ["git", "status"], ["git", "push"]):
            return ""
        raise AssertionError(f"unexpected command: {args}")

    def run_worker(self):
        docs_worker(self.job, self.root, {}, self.run_command,
                    time.monotonic() + 60, self.root / "settings.json")

    def test_changed_head_is_not_merged_or_retried(self) -> None:
        self.move_head = True
        with self.assertRaisesRegex(RuntimeError, "docs pull request merge failed or is uncertain"):
            self.run_worker()
        self.assertIsNone(self.merged_head)
        self.assertEqual(self.merge_attempts, 1)
        self.log.assert_not_called()

    def test_validated_head_is_merged(self) -> None:
        self.run_worker()
        self.assertEqual(self.merged_head, self.final_head)
        self.assertEqual(self.log.call_args.args[0], "docs_update_complete")

    def test_lost_successful_merge_response_is_confirmed_without_retry(self) -> None:
        self.lose_response = True
        self.run_worker()
        self.assertEqual(self.merged_head, self.final_head)
        self.assertEqual(self.merge_attempts, 1)
        self.assertEqual(self.log.call_args.args[0], "docs_update_complete")

    def test_missing_merge_commit_is_not_confirmed(self) -> None:
        self.omit_merge_commit = True
        with self.assertRaisesRegex(RuntimeError, "docs squash merge was not confirmed"):
            self.run_worker()
        self.log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
