"""Behavior regressions for CodeRabbit review and comment intake."""

import copy
import hashlib
import json
import os
from io import StringIO
import subprocess
from email.message import Message
import unittest
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import Mock, patch

from runner import CONFIG, POLICY, PRWorker, agent_prompt, bugbot_prompt, command_publication, docs_worker, event_job, review_job, upstack

def setUpModule():
    owners = patch.dict(CONFIG, allowed_owners=["example-org", "example"])
    owners.start()
    unittest.addModuleCleanup(owners.stop)



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


BUGBOT = {"login": "cursor[bot]", "id": 206951365, "type": "Bot"}
BUGBOT_BODY = (
    "### Feedback split ignores boundaries\n\n**Medium Severity**\n\n"
    "<!-- DESCRIPTION START -->\nDetection requires a bounded `@sentry` mention, but extraction still splits "
    "on every substring.\n<!-- DESCRIPTION END -->\n\n"
    "<!-- BUGBOT_BUG_ID: 9afa9cea-d9bd-4ee0-b100-7c46c28a7006 -->\n\n"
    "<!-- LOCATIONS START\nsrc/seer/webhooks.py#L39-L42\nsrc/seer/webhooks.py#L29-L33\nLOCATIONS END -->\n"
    "<details>\n<summary>Additional Locations (1)</summary>\n\n- [`src/seer/webhooks.py#L29-L33`](https://github.com/o/r/blob/x/a.py#L29-L33)\n\n</details>\n\n"
    '<div><a href="https://cursor.com/open?link=TOKEN" target="_blank"><img alt="Fix in Cursor"></a></div>\n\n'
    "<sup>Reviewed by [Cursor Bugbot](https://cursor.com/bugbot) for commit abc.</sup>"
)


def bugbot_comment_event() -> dict:
    return {
        "action": "created",
        "repository": {"full_name": REPO},
        "sender": dict(BUGBOT),
        "pull_request": {"number": 142, "base": {"repo": {"full_name": REPO}}},
        "comment": {
            "id": 9000000002,
            "user": dict(BUGBOT),
            "pull_request_url": PR_URL,
            "body": BUGBOT_BODY,
        },
    }


class BugbotIntakeTests(unittest.TestCase):
    def test_inline_finding_becomes_a_job_without_cursor_links(self) -> None:
        job = event_job("pull_request_review_comment", bugbot_comment_event())
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual((job["reviewer"], job["comment"], job["kind"]),
                         ("bugbot", 9000000002, "pull_request_review_comment"))
        self.assertTrue(job["prompt"].startswith("Feedback split ignores boundaries\nSeverity: Medium\n"
                                                  "Locations: src/seer/webhooks.py#L39-L42, src/seer/webhooks.py#L29-L33"))
        self.assertIn("extraction still splits", job["prompt"])
        self.assertNotIn("cursor.com", job["prompt"])
        self.assertNotIn("Additional Locations", job["prompt"])

    def test_review_summary_does_not_start_work(self) -> None:
        event = review_event()
        event["sender"] = dict(BUGBOT)
        event["review"]["user"] = dict(BUGBOT)
        event["review"]["body"] = "<!-- BUGBOT_REVIEW -->\nCursor Bugbot has reviewed your changes and found 1 potential issue."
        self.assertIsNone(event_job("pull_request_review", event))

    def test_marked_findings_in_review_summaries_and_issue_comments_do_not_start_work(self) -> None:
        summary = review_event()
        summary["sender"] = dict(BUGBOT)
        summary["review"]["user"] = dict(BUGBOT)
        summary["review"]["body"] = "<!-- BUGBOT_REVIEW -->\n" + BUGBOT_BODY
        issue = bugbot_comment_event()
        issue["issue"] = {"number": 142, "pull_request": {"url": PR_URL}}
        issue["comment"]["issue_url"] = f"https://api.github.com/repos/{REPO}/issues/142"
        for kind, event, actions in (("pull_request_review", summary, ("submitted", "edited")),
                                     ("issue_comment", issue, ("created", "edited"))):
            for action in actions:
                event["action"] = action
                with self.subTest(kind=kind, action=action):
                    self.assertIsNone(event_job(kind, event))

    def test_bugbot_identity_is_exact_and_not_interchangeable(self) -> None:
        for sender, author in ((BUGBOT, BOT), (BOT, BUGBOT), ({**BUGBOT, "id": 1}, BUGBOT)):
            event = bugbot_comment_event()
            event["sender"], event["comment"]["user"] = dict(sender), dict(author)
            with self.subTest(sender=sender["login"], author=author["login"], sender_id=sender["id"]):
                self.assertIsNone(event_job("pull_request_review_comment", event))

    def test_each_reviewer_only_uses_its_own_finding_format(self) -> None:
        event = bugbot_comment_event()
        event["comment"]["body"] = section("Prompt for AI Agents", "Fix it.")
        self.assertIsNone(event_job("pull_request_review_comment", event))
        event = review_event()
        event["review"]["body"] = BUGBOT_BODY
        self.assertIsNone(event_job("pull_request_review", event))

    def test_ignore_marker_and_missing_description_block_bugbot_work(self) -> None:
        event = bugbot_comment_event()
        event["pull_request"]["body"] = "wip @autokas ignore"
        self.assertIsNone(event_job("pull_request_review_comment", event))
        stripped = BUGBOT_BODY.replace("<!-- DESCRIPTION START -->", "").replace("<!-- DESCRIPTION END -->", "")
        self.assertEqual(bugbot_prompt(stripped), "")

    def test_review_batch_carries_the_reviewer_to_the_worker(self) -> None:
        inline = {"id": 9000000002, "user": dict(BUGBOT), "pull_request_review_id": 77, "body": BUGBOT_BODY,
                  "pull_request_url": PR_URL, "html_url": "https://github.com/x#discussion_r1"}
        review = {"user": dict(BUGBOT), "state": "commented", "pull_request_url": PR_URL,
                  "body": "<!-- BUGBOT_REVIEW -->", "html_url": "https://github.com/x#pullrequestreview-77"}
        pr = {"number": 142, "base": {"repo": {"full_name": REPO}}}
        responses = {f"repos/{REPO}/pulls/comments/9000000002": inline,
                     f"repos/{REPO}/pulls/142/reviews/77": review,
                     f"repos/{REPO}/pulls/142/reviews/77/comments?per_page=100&page=1": [inline]}
        job = event_job("pull_request_review_comment", bugbot_comment_event())
        assert job is not None
        with patch("runner.github", side_effect=lambda path: responses[path]):
            canonical = review_job(job, pr)
        self.assertIsNotNone(canonical)
        assert canonical is not None
        self.assertEqual(canonical["reviewer"], "bugbot")
        self.assertEqual([target["comment"] for target in canonical["targets"]], [9000000002])

    def test_parent_marked_finding_cannot_replace_collected_inline_prompts(self) -> None:
        event = bugbot_comment_event()
        first = {**event["comment"], "pull_request_review_id": 77,
                 "html_url": "https://github.com/x#discussion_r1"}
        second = {**first, "id": first["id"] + 1,
                  "body": BUGBOT_BODY.replace("Feedback split ignores boundaries", "Second inline finding"),
                  "html_url": "https://github.com/x#discussion_r2"}
        review = {"user": dict(BUGBOT), "state": "commented", "pull_request_url": PR_URL,
                  "body": BUGBOT_BODY.replace("Feedback split ignores boundaries", "Parent-only finding"),
                  "html_url": "https://github.com/x#pullrequestreview-77"}
        responses = {f"repos/{REPO}/pulls/comments/{first['id']}": first,
                     f"repos/{REPO}/pulls/142/reviews/77": review,
                     f"repos/{REPO}/pulls/142/reviews/77/comments?per_page=100&page=1": [second, first]}
        job = event_job("pull_request_review_comment", event)
        assert job is not None
        with patch("runner.github", side_effect=lambda path: responses[path]):
            canonical = review_job(job, event["pull_request"])
        self.assertIsNotNone(canonical)
        assert canonical is not None
        self.assertEqual(canonical["prompt"], "\n\n".join(bugbot_prompt(c["body"]) for c in (first, second)))
        self.assertNotIn("Parent-only finding", canonical["prompt"])


SECURITY_BODY = (
    "<!-- CURSOR_AUTOMATION_ID: 00000000-0000-0000-0000-000000000000 | RUN_ID: bc-00000000 -->\n"
    "🔒 **Agentic Security Review**\nSeverity: HIGH\n\n"
    "`upstack()` trusts any PR based on the job branch as a restack target.\n\n"
    "**Impact:** the App can be directed to push a protected branch.\n\n"
    '<div><a href="https://cursor.com/open?link=TOKEN" target="_blank"><img alt="Fix in Cursor"></a></div>\n\n'
    "<sup>Reviewed by [Cursor Security Reviewer](https://cursor.com/docs/security-review) for commit abc. "
    "Configure [here](https://www.cursor.com/dashboard/security-agents/x).</sup>"
)


class CursorSecurityIntakeTests(unittest.TestCase):
    def security_event(self, body: str = SECURITY_BODY) -> dict:
        event = bugbot_comment_event()
        event["comment"]["body"] = body
        return event

    def test_inline_security_finding_becomes_a_job_without_cursor_links(self) -> None:
        job = event_job("pull_request_review_comment", self.security_event())
        assert job is not None
        self.assertEqual((job["reviewer"], job["kind"]), ("bugbot", "pull_request_review_comment"))
        self.assertEqual(job["prompt"], "Cursor security review\nSeverity: HIGH\n\n"
                                        "`upstack()` trusts any PR based on the job branch as a restack target.\n\n"
                                        "**Impact:** the App can be directed to push a protected branch.")

    def test_security_summaries_other_accounts_and_unmarked_text_start_nothing(self) -> None:
        summary = review_event()
        summary["sender"] = summary["review"]["user"] = dict(BUGBOT)
        summary["review"]["body"] = "<!-- CURSOR_AUTOMATION_ID: x | RUN_ID: y -->\nSecurity review found one high-severity issue."
        self.assertIsNone(event_job("pull_request_review", summary))
        impostor = self.security_event()
        impostor["sender"] = impostor["comment"]["user"] = {**BUGBOT, "id": 1}
        self.assertIsNone(event_job("pull_request_review_comment", impostor))
        unmarked = SECURITY_BODY.split("\n", 1)[1]
        self.assertIsNone(event_job("pull_request_review_comment", self.security_event(unmarked)))
        empty = SECURITY_BODY.split("Severity: HIGH\n\n")[0] + "Severity: HIGH\n\n<sup>Reviewed.</sup>"
        self.assertIsNone(event_job("pull_request_review_comment", self.security_event(empty)))

    def test_security_and_bugbot_findings_in_one_review_batch_together(self) -> None:
        bugbot = {"id": 9000000002, "user": dict(BUGBOT), "pull_request_review_id": 77, "body": BUGBOT_BODY,
                  "pull_request_url": PR_URL, "html_url": "https://github.com/x#discussion_r1"}
        security = {**bugbot, "id": 9000000003, "body": SECURITY_BODY, "html_url": "https://github.com/x#discussion_r2"}
        review = {"user": dict(BUGBOT), "state": "commented", "pull_request_url": PR_URL,
                  "body": "<!-- CURSOR_AUTOMATION_ID: x -->\nSecurity review found one issue.",
                  "html_url": "https://github.com/x#pullrequestreview-77"}
        responses = {f"repos/{REPO}/pulls/comments/9000000003": security,
                     f"repos/{REPO}/pulls/142/reviews/77": review,
                     f"repos/{REPO}/pulls/142/reviews/77/comments?per_page=100&page=1": [security, bugbot]}
        job = event_job("pull_request_review_comment", {**self.security_event(), "comment": security})
        assert job is not None
        with patch("runner.github", side_effect=lambda path: responses[path]):
            canonical = review_job(job, {"number": 142, "base": {"repo": {"full_name": REPO}}})
        assert canonical is not None
        self.assertEqual([target["comment"] for target in canonical["targets"]], [9000000002, 9000000003])
        self.assertIn("Cursor security review", canonical["prompt"])
        self.assertNotIn("cursor.com", canonical["prompt"])


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


    def test_installed_repository_requires_an_approved_owner(self) -> None:
        event = review_event()
        event["repository"]["full_name"] = "unlisted-owner/unlisted-repository"
        event["pull_request"]["base"]["repo"]["full_name"] = "unlisted-owner/unlisted-repository"
        event["review"]["pull_request_url"] = "https://api.github.com/repos/unlisted-owner/unlisted-repository/pulls/142"
        self.assertIsNone(event_job("pull_request_review", event))
        with patch.dict(CONFIG, allowed_owners=["unlisted-owner"]):
            job = event_job("pull_request_review", event)
        self.assertEqual(job["repo"], "unlisted-owner/unlisted-repository")

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

    def test_generated_docs_findings_never_become_jobs(self) -> None:
        for kind in ("pull_request_review", "pull_request_review_comment", "issue_comment"):
            event = review_event()
            pr = event["pull_request"]
            pr["body"] = "ordinary PR"
            pr["head"] = {"ref": "fix/ordinary"}
            if kind != "pull_request_review":
                event["action"] = "created"
                event["comment"] = event.pop("review")
            if kind == "issue_comment":
                event["issue"] = event.pop("pull_request")
                pr.pop("head")
                pr["pull_request"] = {"url": PR_URL}
                event["comment"]["issue_url"] = PR_URL.replace("/pulls/", "/issues/")
            with self.subTest(kind=kind):
                self.assertIsNotNone(event_job(kind, event))
                pr["body"] = "<!-- omp-runner:docs-update -->\n@coderabbitai ignore"
                self.assertIsNone(event_job(kind, event))
                if kind != "issue_comment":
                    pr["body"] = "older generated docs PR without a marker"
                    pr["head"]["ref"] = CONFIG["docs_update"]["branch_prefix"] + "142-abc"
                    self.assertIsNone(event_job(kind, event))

    def test_ignore_marker_blocks_automatic_work_but_not_commands(self) -> None:
        for kind in ("pull_request_review", "pull_request_review_comment", "issue_comment"):
            event = review_event()
            pr = event["pull_request"]
            pr["body"] = "ordinary PR"
            pr["head"] = {"ref": "fix/ordinary"}
            if kind != "pull_request_review":
                event["action"] = "created"
                event["comment"] = event.pop("review")
            if kind == "issue_comment":
                event["issue"] = event.pop("pull_request")
                pr.pop("head")
                pr["pull_request"] = {"url": PR_URL}
                event["comment"]["issue_url"] = PR_URL.replace("/pulls/", "/issues/")
            with self.subTest(kind=kind):
                self.assertIsNotNone(event_job(kind, event))
                pr["body"] = "wip\n@autokas ignore"
                self.assertIsNone(event_job(kind, event))
                pr["body"] = "<!-- autokas:ignore -->"
                self.assertIsNone(event_job(kind, event))

        payload = {
            "action": "created", "repository": {"full_name": REPO},
            "issue": {"number": 142, "pull_request": {"url": PR_URL}, "body": "autokas:ignore"},
            "comment": {"id": 555, "body": "@autokas fix the typo",
                        "user": {"login": "kas", "type": "User"},
                        "html_url": "https://github.com/example-org/example-app/pull/142#issuecomment-555"},
        }
        job = event_job("issue_comment", payload)
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job["mode"], "command")

        with patch.dict(CONFIG["docs_update"]["repositories"], {REPO: {"branch": "main", "folders": ["docs"]}}):
            event = {"action": "closed", "repository": {"full_name": REPO}, "pull_request": {
                "number": 142, "merged": True, "merge_commit_sha": "a" * 40,
                "base": {"ref": "main", "repo": {"full_name": REPO}},
                "head": {"ref": "fix/ordinary", "repo": {"full_name": REPO}},
            }}
            self.assertIsNotNone(event_job("pull_request", event))
            event["pull_request"]["body"] = "autokas:ignore"
            self.assertIsNone(event_job("pull_request", event))

    def test_generated_docs_merge_does_not_schedule_another_update(self) -> None:
        repositories = {REPO: {"branch": "main", "folders": ["docs"]}}
        with patch.dict(CONFIG["docs_update"]["repositories"], repositories):
            self.check_generated_docs_merge()

    def check_generated_docs_merge(self) -> None:
        event = {"action": "closed", "repository": {"full_name": REPO}, "pull_request": {
            "number": 142, "merged": True, "merge_commit_sha": "a" * 40,
            "base": {"ref": "main", "repo": {"full_name": REPO}},
            "head": {"ref": "fix/ordinary", "repo": {"full_name": REPO}},
        }}
        self.assertIsNotNone(event_job("pull_request", event))
        event["pull_request"]["body"] = "<!-- omp-runner:docs-update -->"
        self.assertIsNone(event_job("pull_request", event))
        event["pull_request"].pop("body")
        event["pull_request"]["head"]["ref"] = CONFIG["docs_update"]["branch_prefix"] + "142-abc"
        self.assertIsNone(event_job("pull_request", event))




class CommandPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.job = {"repo": REPO, "pr": 142, "target": "pr", "key": f"{REPO}:command:555"}
        self.record = {"state": "executing", "starting_head": "a" * 40, "branch": "feature/command"}
        self.head = "b" * 40
        self.trailer = "Autokas-Command: " + hashlib.sha256(self.job["key"].encode()).hexdigest()
        self.pr = {"head": {"sha": self.head, "ref": self.record["branch"], "repo": {"full_name": REPO}}}
        self.pages = [[self.commit(self.head, "fix: apply command\n\n" + self.trailer)]]
        self.claims = Mock()
        self.claims.get.side_effect = lambda key, default=None: self.record if key == "command:" + self.job["key"] else default
        for patcher in (patch("runner.CLAIMS", self.claims), patch("runner.github", side_effect=self.read_github)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def commit(self, sha: str, message: str, login: str | None = None) -> dict:
        return {"sha": sha, "committer": {"login": login or CONFIG["git_author"]["name"]},
                "commit": {"message": message}}

    def read_github(self, path: str):
        if path == f"repos/{REPO}/pulls/142":
            return self.pr
        if path == f"repos/{REPO}/git/ref/heads/autokas/issue-142":
            return {"object": {"sha": self.head}}
        prefix = f"repos/{REPO}/commits?sha={self.pr['head']['sha']}&per_page=100&page="
        if path.startswith(prefix):
            return self.pages[int(path.removeprefix(prefix)) - 1]
        raise AssertionError(path)

    def test_published_command_is_found_beneath_a_later_unrelated_commit(self) -> None:
        published = "c" * 40
        self.pages = [[self.commit(self.head, "chore: unrelated change"),
                       self.commit(published, "fix: command\n\n" + self.trailer)]]
        result = command_publication(self.job)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["commit"], published)

    def test_unrelated_changed_or_unchanged_heads_do_not_authorize_replay(self) -> None:
        for sha in (self.head, self.record["starting_head"]):
            with self.subTest(head=sha):
                self.pr["head"]["sha"] = sha
                self.pages = [[self.commit(sha, "chore: unrelated change")]]
                self.assertEqual(command_publication(self.job)["status"], "uncertain")

    def test_receipts_require_this_command_and_the_bot_identity(self) -> None:
        other = "Autokas-Command: " + hashlib.sha256((self.job["key"] + "other").encode()).hexdigest()
        for message, login in ((other, CONFIG["git_author"]["name"]), (self.trailer, "someone-else")):
            with self.subTest(message=message, login=login):
                self.pages = [[self.commit(self.head, "fix: change\n\n" + message, login)]]
                self.assertEqual(command_publication(self.job)["status"], "uncertain")

    def test_recorded_publication_must_still_be_reachable(self) -> None:
        self.record["published_head"] = self.head
        self.pages = [[self.commit(self.head, "fix: command without a trailer")]]
        self.assertEqual(command_publication(self.job)["status"], "published")
        self.pages = [[self.commit("c" * 40, "chore: different history")]]
        self.assertEqual(command_publication(self.job)["status"], "uncertain")

    def test_published_issue_command_uses_its_deterministic_branch(self) -> None:
        self.job["target"] = "issue"
        self.record["branch"] = "autokas/issue-142"
        self.assertEqual(command_publication(self.job)["status"], "published")

    def test_changed_branch_or_repository_leaves_publication_uncertain(self) -> None:
        for field, value in (("ref", "different-branch"), ("repo", {"full_name": "someone/fork"})):
            with self.subTest(field=field):
                original = self.pr["head"][field]
                self.pr["head"][field] = value
                self.assertEqual(command_publication(self.job)["status"], "uncertain")
                self.pr["head"][field] = original

    def test_pre_launch_preemption_can_retry_but_missing_records_cannot(self) -> None:
        self.record = {"state": "preparing"}
        self.assertIsNone(command_publication(self.job))
        self.record = None
        self.assertEqual(command_publication(self.job)["status"], "uncertain")

    def test_successful_no_change_command_is_not_executed_again(self) -> None:
        self.record["state"] = "completed"
        self.pr["head"]["sha"] = self.record["starting_head"]
        self.pages = [[self.commit(self.record["starting_head"], "base")]]
        self.assertEqual(command_publication(self.job)["status"], "completed")

    def test_pagination_finds_the_receipt_but_stops_at_the_starting_head(self) -> None:
        self.pages = [[self.commit(f"{index:040x}", "chore: unrelated") for index in range(100)], self.pages[0]]
        self.assertEqual(command_publication(self.job)["status"], "published")
        self.pages[0][0] = self.commit(self.record["starting_head"], "base")
        self.assertEqual(command_publication(self.job)["status"], "uncertain")

    def test_retry_stops_at_existing_remote_head_before_old_receipts(self) -> None:
        self.record["remote_start"] = "c" * 40
        self.pages = [[self.commit(self.head, "chore: unrelated"),
                       self.commit(self.record["remote_start"], "fix: old command\n\n" + self.trailer)]]
        self.assertEqual(command_publication(self.job)["status"], "uncertain")

    def test_completed_reused_branch_does_not_claim_old_receipt(self) -> None:
        self.record.update(state="completed", remote_start=self.head)
        self.assertEqual(command_publication(self.job)["status"], "completed")

    def test_retry_finds_new_receipt_above_existing_remote_head(self) -> None:
        self.record["remote_start"] = "c" * 40
        self.pages[0].append(self.commit(self.record["remote_start"], "old task"))
        result = command_publication(self.job)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["commit"], self.head)

    def test_lookup_failure_never_falls_back_to_execution(self) -> None:
        with patch("runner.github", side_effect=TimeoutError("lost read response")):
            result = command_publication(self.job)
        self.assertEqual(result["status"], "uncertain")
        self.assertIn("TimeoutError", result["reason"])


class CommandInitializationTests(unittest.TestCase):
    class Interrupted(Exception):
        pass

    def setUp(self) -> None:
        self.job = {"mode": "command", "repo": REPO, "pr": 142, "key": f"{REPO}:command:555",
                    "kind": "issue_comment", "comment": 555, "author": "kas", "target": "pr",
                    "source_url": f"https://github.com/{REPO}/pull/142#issuecomment-555",
                    "prompt": "fix the command launch regression"}
        self.temporary_directory = tempfile.TemporaryDirectory
        self.subprocess_run = subprocess.run
        self.subprocess_popen = subprocess.Popen
        temporary = self.temporary_directory()
        self.addCleanup(temporary.cleanup)
        self.upstream = Path(temporary.name)
        self.git_env = {"PATH": os.defpath, "HOME": temporary.name, "GIT_CONFIG_NOSYSTEM": "1"}
        self.branch = "feature/command"
        self.git(["git", "init", "-b", self.branch])
        self.git(["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
                  "commit", "--allow-empty", "-m", "base"])
        self.head = self.git(["git", "rev-parse", "HEAD"]).stdout.strip()
        self.git(["git", "update-ref", "refs/pull/142/head", self.head])
        self.values = {}
        self.interrupt_after_start = False
        claims = Mock()
        claims.put.side_effect = self.put
        claims.get.side_effect = lambda key, default=None: copy.deepcopy(self.values.get(key, default))
        for patcher in (patch("runner.CLAIMS", claims), patch("runner.log"),
                        patch("runner.github", side_effect=AssertionError("unexpected GitHub read")),
                        patch("runner.tempfile.TemporaryDirectory", side_effect=self.Interrupted)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def put(self, key, value, skip_if_exists=False):
        if skip_if_exists and key in self.values:
            return False
        self.values[key] = copy.deepcopy(value)
        if self.interrupt_after_start and key == "started:" + self.job["key"]:
            self.interrupt_after_start = False
            raise self.Interrupted
        return True

    def deliver(self) -> None:
        with self.assertRaises(self.Interrupted):
            PRWorker(pr_key=f"{REPO}#142").run.local(self.job)

    def git(self, args, **kwargs):
        return self.subprocess_run(args, cwd=kwargs.get("cwd", self.upstream),
                                   env=self.git_env, capture_output=True, text=True, check=True)

    def launch(self, agent=None) -> dict:
        """Reach omp with a local checkout or an empty reporting-only directory.

        With an agent callback, omp "exits 0" after the callback edits the checkout."""
        observed = {}

        def read_github(path):
            if path == f"repos/{REPO}/pulls/142":
                return {"state": "open", "body": getattr(self, "pr_body", ""), "base": {"ref": "main", "repo": {"full_name": REPO}},
                        "head": {"sha": getattr(self, "remote_head", self.head), "ref": self.branch, "repo": {"full_name": REPO}}}
            if path == f"repos/{REPO}/commits?sha={self.head}&per_page=100&page=1":
                return [{"sha": self.head, "commit": {"message": "base"}}]
            if path.startswith(f"repos/{REPO}/pulls?state=open&per_page=100&page=1&base="):
                return getattr(self, "stack", {}).get(path.rsplit("base=", 1)[1], [])
            if path == f"repos/{REPO}":
                return {"default_branch": "main"}
            if path.startswith(f"repos/{REPO}/branches/"):
                return {"protected": False}
            if path.startswith(f"repos/{REPO}/compare/"):
                return {"merge_base_commit": {"sha": "f" * 40}, "ahead_by": 1}
            raise AssertionError(f"unexpected GitHub read: {path}")

        def run(args, **kwargs):
            if args == ["gh", "auth", "setup-git"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:3] == ["git", "clone", "--no-checkout"]:
                args = [*args[:3], str(self.upstream), args[-1]]
            elif args[:2] not in (["git", "fetch"], ["git", "checkout"], ["git", "rev-parse"]):
                raise AssertionError(f"unexpected command: {args}")
            return self.git(args, **kwargs)

        def popen(args, *, cwd, **kwargs):
            if args[0] == "git":
                return self.subprocess_popen(args, cwd=cwd, **kwargs)
            self.assertEqual(args[0], "omp")
            observed["prompt"] = Path(args[-1].removeprefix("@")).read_text()
            policy = Path(args[args.index("--append-system-prompt") + 1]).read_text()
            observed["context"] = json.loads(policy.split("\nTrusted job context:\n", 1)[1])
            observed["has_checkout"] = (cwd / ".git").is_dir()
            observed["files"] = {path.name for path in cwd.iterdir()}
            if observed["has_checkout"]:
                observed["head"] = self.git(["git", "rev-parse", "HEAD"], cwd=cwd).stdout.strip()
                observed["branch"] = self.git(["git", "branch", "--show-current"], cwd=cwd).stdout.strip()
            if agent:
                agent(cwd)
                return Mock(pid=-1, **{"wait.return_value": 0})
            raise self.Interrupted

        model = CONFIG["model"].split("/", 1)[1]
        with (patch("runner.tempfile.TemporaryDirectory", self.temporary_directory),
              patch.dict("runner.os.environ", {"PATH": os.defpath, "BUN_INSTALL": "/unused",
                                               "CLI_PROXY_API_KEY": "disposable-proxy-key"}, clear=True),
              patch.dict(CONFIG, {"jarvis_owner": ""}),
              patch("runner.github_token", return_value="disposable-token"),
              patch("runner.github", side_effect=read_github),
              patch("runner.urllib.request.urlopen", return_value=StringIO(json.dumps({"data": [{"id": model}]}))),
              patch("runner.subprocess.run", side_effect=run),
              patch("runner.subprocess.Popen", side_effect=popen),
              patch("runner.os.killpg")):
            if agent:
                PRWorker(pr_key=f"{REPO}#142").run.local(self.job)
            else:
                self.deliver()
        return observed

    def assert_execution_launch(self, observed) -> None:
        self.assertTrue(observed["has_checkout"])
        self.assertEqual((observed["head"], observed["branch"]), (self.head, self.branch))
        self.assertNotIn("command_resume", observed["context"])
        self.assertIn(self.job["prompt"], observed["prompt"])
        self.assertIn(self.job["author"], observed["prompt"])
        self.assertNotIn("reporting-only", observed["prompt"])
        self.assertEqual(self.values["command:" + self.job["key"]],
                         {"state": "executing", "starting_head": self.head, "branch": self.branch})

    def assert_reporting_launch(self, observed, status) -> None:
        self.assertFalse(observed["has_checkout"])
        self.assertEqual(observed["files"], set())
        self.assertEqual(observed["context"]["command_resume"]["status"], status)
        self.assertIn("reporting-only", observed["prompt"])
        self.assertIn(self.job["prompt"], observed["prompt"])

    def test_preemption_after_start_claim_allows_command_preparation_on_redelivery(self) -> None:
        self.interrupt_after_start = True
        self.deliver()
        self.assertNotIn("command:" + self.job["key"], self.values)
        self.deliver()
        self.assertIsNone(command_publication(self.job))
        self.assert_execution_launch(self.launch())

    def test_explicit_command_on_generated_docs_pr_reaches_its_head(self) -> None:
        self.pr_body = "<!-- omp-runner:docs-update -->"
        self.assert_execution_launch(self.launch())

    def test_launch_gives_the_agent_the_branches_stacked_above_its_pr(self) -> None:
        child = {"number": 143, "head": {"ref": "feature/child", "sha": "c" * 40, "repo": {"full_name": REPO}}}
        self.stack = {"feature%2Fcommand": [child]}
        self.assertEqual(self.launch()["context"]["upstack"],
                         [{"pr": 143, "branch": "feature/child", "parent": "feature/command"}])
        policy = POLICY
        self.assertIn("Leave every upstack branch unchanged: never create merge commits", policy)
        self.assertIn("restack by its owner", policy)
        self.assertNotIn("chore(stack): merge", policy)
        self.assertNotIn("Report which branches you updated", policy)

    def test_legacy_start_without_execution_record_stays_reporting_only(self) -> None:
        self.values["started:" + self.job["key"]] = "started"
        self.deliver()
        self.assertEqual(command_publication(self.job)["status"], "uncertain")
        self.assertNotIn("command:" + self.job["key"], self.values)
        self.assert_reporting_launch(self.launch(), "uncertain")
        self.assertNotIn("command:" + self.job["key"], self.values)

    def test_redelivery_preserves_existing_execution_records(self) -> None:
        self.interrupt_after_start = True
        self.deliver()
        for state in ("preparing", "executing", "completed"):
            with self.subTest(state=state):
                record = {"state": state, "starting_head": self.head, "branch": self.branch}
                self.values["command:" + self.job["key"]] = record
                self.deliver()
                self.assertEqual(self.values["command:" + self.job["key"]], record)
                result = command_publication(self.job)
                if state == "preparing":
                    self.assertIsNone(result)
                else:
                    self.assertEqual(result["status"], "uncertain")
                observed = self.launch()
                if state == "preparing":
                    self.assert_execution_launch(observed)
                else:
                    self.assert_reporting_launch(observed, "completed" if state == "completed" else "uncertain")
                    self.assertEqual(self.values["command:" + self.job["key"]], record)


class StackTests(unittest.TestCase):
    @staticmethod
    def pr(number: int, branch: str, repo: str | None = REPO) -> dict:
        return {"number": number, "head": {"ref": branch, "sha": branch.upper(), "repo": repo and {"full_name": repo}}}

    def test_upstack_walks_every_page_of_real_children_and_never_targets_other_branches(self) -> None:
        forks = [self.pr(100 + n, f"fork-{n}", "outsider/example-app") for n in range(94)]
        children = {
            ("a", 1): [self.pr(2, "b"), self.pr(9, "fork", "outsider/example-app"), self.pr(8, "gone", None),
                       self.pr(10, "main"), self.pr(11, "release"), self.pr(12, "unrelated"), *forks],
            ("a", 2): [self.pr(7, "x")],
            ("b", 1): [self.pr(3, "c"), self.pr(4, "d")],
            ("c", 1): [self.pr(5, "a")],
        }
        # a child built on its parent shares a merge base that isn't on the parent's base yet.
        merge_bases = {("A", "B"): "a1", ("A", "X"): "a1", ("B", "C"): "b1", ("B", "D"): "b1", ("A", "UNRELATED"): "m0"}
        ahead = {("main", "a1"), ("a", "b1")}

        def read(path):
            route, _, query = path.removeprefix(f"repos/{REPO}").partition("?")
            if route == "":
                return {"default_branch": "main"}
            if route.startswith("/branches/"):
                return {"protected": route == "/branches/release"}
            if route.startswith("/compare/"):
                left, right = route.removeprefix("/compare/").split("...")
                if (left, right) in merge_bases:
                    return {"merge_base_commit": {"sha": merge_bases[left, right]}}
                # a diverged base still counts: only commits the base lacks matter.
                return {"ahead_by": 2 if (left, right) in ahead else 0, "status": "diverged"}
            params = dict(part.split("=", 1) for part in query.split("&"))
            return children.get((params["base"], int(params["page"])), [])

        with patch("runner.github", side_effect=read), patch("runner.log"):
            stack = upstack(REPO, "a", "A", "main")
        self.assertEqual([(entry["pr"], entry["branch"], entry["parent"]) for entry in stack],
                         [(2, "b", "a"), (7, "x", "a"), (3, "c", "b"), (4, "d", "b")])


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
        self.concurrent_merge = False
        self.lose_response = False
        self.omit_merge_commit = False
        self.job = {
            "repo": "example/docs", "pr": 1, "source_sha": "a" * 40,
            "source_branch": "feature",
        }
        self.api = "repos/example/docs/pulls"
        self.source_files = [{"filename": "src/service.py"}]
        self.branch = f'{CONFIG["docs_update"]["branch_prefix"]}1-{"a" * 12}'
        self.followup = {
            "number": 2,
            "head": {"repo": {"full_name": "example/docs"},
                     "ref": self.branch, "sha": self.final_head},
            "base": {"repo": {"full_name": "example/docs"}, "ref": "main"},
        }
        self.real_popen = subprocess.Popen
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
        self.process = mocks[3]
        self.github_read = mocks[1]
        self.log = mocks[5]

    def read_github(self, path):
        if path == f"{self.api}/1":
            return {
                "state": "closed", "merged_at": "2026-09-27T00:00:00Z",
                "merge_commit_sha": self.job["source_sha"],
                "base": self.followup["base"], "head": self.followup["head"],
            }
        if path == f"{self.api}/1/files?per_page=100":
            return self.source_files
        if path == f"{self.api}/2/files?per_page=100":
            if self.move_head:
                self.current_head = "c" * 40
            return getattr(self, "followup_files", [{"filename": "docs/guide.md"}])
        if path == f"{self.api}?state=open&head=example:{self.branch}&base=main":
            return []
        if path == f"{self.api}/2":
            head = self.current_head
            if getattr(self, "stale_head_reads", 0):
                head = self.stale_head
                self.stale_head_reads -= 1
            return {
                **self.followup,
                "state": "closed" if self.merged_head else "open",
                **({"state": "closed"} if getattr(self, "closed_followup", False) else {}),
                "head": {**self.followup["head"], "sha": head},
                "merged_at": "2026-09-27T00:00:00Z" if self.merged_head else None,
                "merge_commit_sha": "d" * 40 if self.merged_head and not self.omit_merge_commit else None,
            }
        raise AssertionError(f"unexpected GitHub read: {path}")

    def write_github(self, method, path, payload):
        if method == "PATCH" and path == f"{self.api}/2":
            self.closed_followup = True
            return {"state": "closed"}
        if method == "POST" and path == self.api:
            return copy.deepcopy(self.followup)
        if method == "PUT" and path == f"{self.api}/2/merge":
            self.merge_attempts += 1
            if getattr(self, "move_head_at_merge", False):
                self.current_head = "c" * 40
            if payload.get("sha", self.current_head) != self.current_head:
                if self.concurrent_merge:
                    self.merged_head = self.current_head
                raise HTTPError(path, 409, "Head branch was modified", Message(), None)
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
        if args[:4] == ["git", "diff", "--name-status", "-z"]:
            return "M\0docs/guide.md\0"
        if args[:2] in (["gh", "auth"], ["git", "clone"], ["git", "fetch"],
                        ["git", "checkout"], ["git", "ls-remote"],
                        ["git", "status"], ["git", "push"]):
            return ""
        raise AssertionError(f"unexpected command: {args}")

    def run_worker(self):
        docs_worker(self.job, self.root, {}, self.run_command,
                    time.monotonic() + 60, self.root / "settings.json")

    def test_docs_only_and_empty_sources_stop_before_agent_work(self) -> None:
        for files in ([], [{"filename": "docs/guide.md"}, {"filename": "docs/api/auth.md"}]):
            with self.subTest(files=files), patch.object(
                self, "run_command", side_effect=AssertionError("source must be skipped")
            ) as run:
                self.source_files = files
                self.run_worker()
                run.assert_not_called()
                self.process.assert_not_called()
                self.assertIsNone(self.merged_head)
                self.assertEqual(self.merge_attempts, 0)

    def test_mixed_source_reaches_docs_update(self) -> None:
        self.source_files.append({"filename": "docs/guide.md"})
        self.run_worker()
        self.assertEqual(self.merged_head, self.final_head)

    def test_docs_folder_lookalikes_do_not_skip_code_changes(self) -> None:
        for path in ("docs-extra/service.py", "src/docs/service.py"):
            with self.subTest(path=path):
                self.source_files = [{"filename": path}]
                self.merged_head = None
                self.run_worker()
                self.assertEqual(self.merged_head, self.final_head)

    def test_changed_head_is_not_merged_or_retried(self) -> None:
        self.move_head = True
        with self.assertRaisesRegex(RuntimeError, "docs pull request changed"):
            self.run_worker()
        self.assertIsNone(self.merged_head)
        self.assertEqual(self.merge_attempts, 0)
        self.log.assert_not_called()

    def test_concurrent_merge_of_changed_head_is_not_confirmed(self) -> None:
        self.move_head_at_merge = True
        self.concurrent_merge = True
        with self.assertRaisesRegex(RuntimeError, "docs pull request changed"):
            self.run_worker()
        self.assertNotEqual(self.merged_head, self.final_head)
        self.assertEqual(self.merged_head, self.current_head)
        self.assertEqual(self.merge_attempts, 1)
        self.log.assert_not_called()

    def test_validated_head_is_merged_for_code_only_source(self) -> None:
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

    def disposable_checkout(self, *, mode="write", agent="docs", configured=True, move_base=None,
                            stale_head_reads=0):
        """Run the docs flow against a local Git remote and an actual Node hook."""
        upstream = self.root / "upstream"
        self.merged_head = None
        self.process.side_effect = self.real_popen
        upstream.mkdir()

        def command(args, cwd=upstream, env=None):
            return subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                                  text=True, check=True).stdout.strip()

        command(["git", "init", "-b", "main"])
        (upstream / "docs").mkdir()
        (upstream / "docs/guide.md").write_text("Original guide\n")
        (upstream / "src").mkdir()
        (upstream / "src/service.py").write_text("service\n")
        (upstream / ".railway").mkdir()
        (upstream / ".railway/worker-release.mjs").write_text(
            "import fs from 'node:fs';\n"
            "const input = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
            "if (!process.env.GH_TOKEN || !process.env.HOME || !process.env.PATH || "
            "input.repo !== 'example/docs' "
            "|| Number(process.env.OMP_POSTPROCESS_DEADLINE) <= Date.now() / 1000) process.exit(3);\n"
            "if (['CLI_PROXY_API_KEY', 'JARVIS_RUNNER_TOKEN', 'JARVIS_CONSULT_URL', "
            "'HOOK_MODE', 'EXPECT_BASE'].some(key => key in process.env)) process.exit(5);\n"
            f"const mode = {json.dumps(mode)};\n"
            "if (mode === 'fail') process.exit(4);\n"
            "const previous = fs.existsSync('.railway/worker-releases.json') ? JSON.parse(fs.readFileSync('.railway/worker-releases.json', 'utf8')) : {};\n"
            "if (mode === 'write') fs.writeFileSync('.railway/worker-releases.json', JSON.stringify({...previous, ...input}));\n"
            "if (mode === 'forbidden') fs.writeFileSync('src/injected.txt', 'not allowed');\n"
            "if (mode === 'rename') fs.renameSync('src/service.py', '.railway/worker-releases.json');\n"
            "if (mode === 'symlink') fs.symlinkSync('../src/service.py', '.railway/worker-releases.json');\n"
        )
        command(["git", "add", "-A"])
        command(["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base"])
        self.job["source_sha"] = command(["git", "rev-parse", "HEAD"])
        self.branch = f'{CONFIG["docs_update"]["branch_prefix"]}1-{self.job["source_sha"][:12]}'
        self.followup["head"]["ref"] = self.branch
        entry = {"branch": "main", "folders": ["docs"]}
        if configured:
            entry["postprocess"] = {"command": ["node", ".railway/worker-release.mjs", "record"],
                                       "files": [".railway/worker-releases.json"]}
        CONFIG["docs_update"]["repositories"]["example/docs"] = entry
        self.real_env = {**os.environ, "GH_TOKEN": "disposable-token", "HOOK_MODE": mode,
                         "EXPECT_BASE": self.job["source_sha"],
                         "CLI_PROXY_API_KEY": "test-proxy-key", "JARVIS_RUNNER_TOKEN": "test-jarvis-token",
                         "JARVIS_CONSULT_URL": "https://example.invalid/consult",
                         "GIT_AUTHOR_NAME": "autokas[bot]", "GIT_AUTHOR_EMAIL": "bot@example.com",
                         "GIT_COMMITTER_NAME": "autokas[bot]", "GIT_COMMITTER_EMAIL": "bot@example.com"}
        self.agent_runs = 0
        self.base_moves = 0

        def advance_base():
            self.base_moves += 1
            guide = ("Original guide\nUpdated guide\n" if move_base == "covered" else
                     "Latest guide\n" if self.base_moves == 1 else f"Latest guide {self.base_moves}\n")
            (upstream / "docs/guide.md").write_text(guide)
            (upstream / "src/service.py").write_text("latest service\n")
            (upstream / ".railway/worker-releases.json").write_text(json.dumps({"release": "newer-base"}))
            command(["git", "add", "-A"])
            command(["git", "commit", "-m", "feat: concurrent base update"], upstream, self.real_env)
            self.latest_base = command(["git", "rev-parse", "HEAD"])
            return self.latest_base
        self.advance_base = advance_base

        def popen(args, *positional, **kwargs):
            if args[0] != "omp":
                return self.real_popen(args, *positional, **kwargs)
            self.agent_runs += 1
            checkout = self.root / "repo"
            if agent == "docs" and not (move_base == "covered" and self.agent_runs > 1):
                guide = checkout / "docs/guide.md"
                guide.write_text(guide.read_text() + "Updated guide\n" if move_base else "Updated guide\n")
                command(["git", "add", "-A"], checkout)
                command(["git", "commit", "-m", "docs: update guide"], checkout, self.real_env)
            elif agent == "rename":
                command(["git", "mv", "src/service.py", "docs/service.py"], checkout)
                command(["git", "commit", "-m", "docs: rename service"], checkout, self.real_env)
            if move_base == "always" or (move_base == "agent" and self.base_moves == 0):
                advance_base()
            return self.process.return_value
        self.process.side_effect = popen

        def git_run(args, cwd):
            previous_head = self.current_head
            if args[:2] == ["gh", "auth"]:
                return ""
            if args[:2] == ["git", "clone"]:
                args = ["git", "clone", "--no-checkout", str(upstream), args[-1]]
            output = command(args, cwd, self.real_env)
            if move_base == "hook" and args == ["git", "commit", "-m", "chore: record worker release state"] and self.base_moves == 0:
                advance_base()
            if args[:2] == ["git", "push"]:
                if any(arg.startswith("--force-with-lease=") for arg in args):
                    self.stale_head = previous_head
                    self.stale_head_reads = stale_head_reads
                self.final_head = command(["git", "rev-parse", "HEAD"], cwd, self.real_env)
                self.current_head = self.final_head
                self.followup["head"]["sha"] = self.final_head
                names = command(["git", "diff", "--name-only", "origin/main", "HEAD"], cwd)
                self.followup_files = [{"filename": name} for name in names.splitlines()]
                if move_base in {"publication", "covered"} and self.base_moves == 0:
                    advance_base()
            return output
        self.git_run = git_run
        if move_base == "merge":
            original_write = self.write_github

            def merge_with_git(method, path, payload):
                if method == "PUT":
                    if self.base_moves == 0:
                        advance_base()
                    try:
                        command(["git", "merge", "--no-ff", self.branch, "-m", "merge docs"], upstream, self.real_env)
                    except subprocess.CalledProcessError as error:
                        self.conflict_output = error.stdout
                        command(["git", "merge", "--abort"])
                        self.merge_attempts += 1
                        raise HTTPError(path, 409, "Merge conflict", Message(), None) from error
                return original_write(method, path, payload)
            self.github_write_patch = patch("runner.github_request", side_effect=merge_with_git)
            self.github_write_patch.start()
            self.addCleanup(self.github_write_patch.stop)

    def run_disposable(self):
        docs_worker(self.job, self.root, self.real_env, self.git_run,
                    time.monotonic() + 60, self.root / "settings.json")


    def test_disposable_docs_and_manifest_share_followup(self) -> None:
        self.disposable_checkout()
        self.run_disposable()
        self.assertEqual(self.merged_head, self.final_head)
        self.assertEqual({item["filename"] for item in self.followup_files},
                         {"docs/guide.md", ".railway/worker-releases.json"})
        self.assertEqual(json.loads((self.root / "repo/.railway/worker-releases.json").read_text()),
                         {"repo": self.job["repo"], "source_sha": self.job["source_sha"],
                          "base_sha": self.job["source_sha"]})

    def test_stale_docs_are_regenerated_with_manifest_from_latest_base(self) -> None:
        for stage in ("agent", "hook", "publication"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory)
                self.disposable_checkout(move_base=stage)
                self.run_disposable()
                self.assertEqual(self.agent_runs, 2)
                self.assertEqual((self.root / "repo/docs/guide.md").read_text(), "Latest guide\nUpdated guide\n")
                self.assertEqual((self.root / "repo/src/service.py").read_text(), "latest service\n")
                manifest = json.loads((self.root / "repo/.railway/worker-releases.json").read_text())
                self.assertEqual(manifest["base_sha"], self.latest_base)
                self.assertEqual(self.merged_head, self.final_head)
                self.assertEqual(manifest["release"], "newer-base")

    def test_reconciled_followup_waits_for_its_new_head_before_merge(self) -> None:
        self.disposable_checkout(move_base="publication", stale_head_reads=2)
        with patch("runner.time.sleep"):
            self.run_disposable()
        self.assertEqual(self.agent_runs, 2)
        self.assertEqual(self.merged_head, self.final_head)
        self.assertNotEqual(self.merged_head, self.stale_head)
        self.assertEqual((self.root / "repo/docs/guide.md").read_text(), "Latest guide\nUpdated guide\n")

    def test_reconciled_followup_rejects_an_unexpected_head_after_sync_lag(self) -> None:
        self.disposable_checkout(move_base="publication", stale_head_reads=1)

        def read(path):
            response = self.read_github(path)
            if (path == f"{self.api}/2" and hasattr(self, "stale_head")
                    and response["head"]["sha"] != self.stale_head):
                response["head"]["sha"] = "c" * 40
            return response

        self.github_read.side_effect = read
        with patch("runner.time.sleep"), self.assertRaisesRegex(RuntimeError, "docs pull request changed"):
            self.run_disposable()
        self.assertIsNone(self.merged_head)
        self.assertEqual(self.merge_attempts, 0)

    def test_reconciled_followup_stops_when_head_does_not_sync(self) -> None:
        self.disposable_checkout(move_base="publication", stale_head_reads=100)
        with patch("runner.time.sleep") as sleep, self.assertRaisesRegex(RuntimeError, "docs pull request changed"):
            self.run_disposable()
        self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 15)
        self.assertIsNone(self.merged_head)
        self.assertEqual(self.merge_attempts, 0)

    def test_reconciled_followup_wait_cannot_exceed_worker_deadline(self) -> None:
        self.disposable_checkout(move_base="publication", stale_head_reads=100)
        clock = [0.0]
        deadline = 3.5

        def sleep(delay):
            clock[0] += delay
            self.assertLess(clock[0], deadline)

        with patch("runner.time.monotonic", side_effect=lambda: clock[0]), patch("runner.time.sleep", side_effect=sleep):
            with self.assertRaisesRegex(RuntimeError, "docs pull request changed"):
                docs_worker(self.job, self.root, self.real_env, self.git_run,
                            deadline, self.root / "settings.json")
        self.assertIsNone(self.merged_head)
        self.assertEqual(self.merge_attempts, 0)

    def test_late_real_git_conflict_is_reconciled_before_merge(self) -> None:
        self.disposable_checkout(move_base="merge")
        self.run_disposable()
        self.assertIn("CONFLICT", self.conflict_output)
        self.assertEqual(self.merge_attempts, 2)
        self.assertEqual(self.agent_runs, 2)
        self.assertEqual((self.root / "upstream/docs/guide.md").read_text(), "Latest guide\nUpdated guide\n")
        manifest = json.loads((self.root / "upstream/.railway/worker-releases.json").read_text())
        self.assertEqual((manifest["base_sha"], manifest["release"]), (self.latest_base, "newer-base"))

    def test_published_followup_is_closed_when_latest_base_already_covers_source(self) -> None:
        self.disposable_checkout(configured=False, move_base="covered")
        self.run_disposable()
        self.assertTrue(self.closed_followup)
        self.assertIsNone(self.merged_head)
        self.assertEqual(self.merge_attempts, 0)
        self.assertEqual((self.root / "repo/docs/guide.md").read_text(), "Original guide\nUpdated guide\n")

    def test_continually_moving_base_stops_without_publishing_stale_docs(self) -> None:
        self.disposable_checkout(move_base="always")
        with self.assertRaisesRegex(RuntimeError, "base kept moving"):
            self.run_disposable()
        self.assertEqual(self.agent_runs, 3)
        self.assertIsNone(self.merged_head)
        self.assertEqual(self.merge_attempts, 0)

    def test_disposable_hook_rejects_failure_and_forbidden_output(self) -> None:
        for mode, error in (("fail", "exited with 4"), ("forbidden", "undeclared"),
                            ("rename", "undeclared"), ("symlink", "symlink")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory)
                self.disposable_checkout(mode=mode)
                with self.assertRaisesRegex(RuntimeError, error):
                    self.run_disposable()
                self.assertEqual(self.merge_attempts, 0)

    def test_disposable_agent_rename_source_is_rejected_before_hook(self) -> None:
        self.disposable_checkout(agent="rename")
        with self.assertRaisesRegex(RuntimeError, "non-documentation"):
            self.run_disposable()
        self.assertEqual(self.merge_attempts, 0)

    def test_disposable_noop_only_for_configured_repository(self) -> None:
        self.disposable_checkout(mode="noop", agent="none")
        self.run_disposable()
        self.assertEqual(self.merge_attempts, 0)
        self.assertEqual(self.log.call_args.args[0], "docs_update_no_change")

    def test_accurate_docs_without_postprocess_need_no_followup(self) -> None:
        self.disposable_checkout(mode="noop", agent="none", configured=False)
        self.run_disposable()
        self.assertEqual(self.merge_attempts, 0)
        self.assertIsNone(self.merged_head)

    def test_disposable_configured_traversal_never_runs_hook(self) -> None:
        self.disposable_checkout()
        CONFIG["docs_update"]["repositories"]["example/docs"]["postprocess"]["files"] = [".railway/../src/injected.txt"]
        with self.assertRaisesRegex(RuntimeError, "invalid docs postprocess output path"):
            self.run_disposable()
        self.assertEqual(self.merge_attempts, 0)

if __name__ == "__main__":
    unittest.main()
