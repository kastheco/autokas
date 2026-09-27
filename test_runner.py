"""Behavior regressions for CodeRabbit review and comment intake."""

import copy
import unittest

from runner import agent_prompt, event_job


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


if __name__ == "__main__":
    unittest.main()
