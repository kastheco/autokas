"""PR-Agent reviews: one per ready PR or explicit command, never a coding job."""

import copy
import subprocess
import unittest
from unittest.mock import Mock, patch

import runner
from test_queue_ack import REPO


HEAD = "b" * 40
PR = {"number": 42, "state": "open", "draft": False, "body": "adds the parser",
      "base": {"ref": "main", "repo": {"full_name": REPO}},
      "head": {"sha": HEAD, "ref": "feature/parser", "repo": {"full_name": REPO}}}


def pr_event(action, **changes):
    return {"action": action, "repository": {"full_name": REPO},
            "pull_request": {**copy.deepcopy(PR), **changes}}


def comment_event(body, user_type="User", on_issue=False, event="issue_comment"):
    comment = {"id": 555, "body": body, "user": {"login": "kas", "type": user_type},
               "html_url": f"https://github.com/{REPO}/pull/42#issuecomment-555"}
    payload = {"action": "created", "repository": {"full_name": REPO}, "sender": comment["user"],
               "comment": comment}
    if event == "issue_comment":
        payload["issue"] = {"number": 42} if on_issue else {
            "number": 42, "pull_request": {"url": f"https://api.github.com/repos/{REPO}/pulls/42"}}
    else:
        payload["pull_request"] = copy.deepcopy(PR)
    return event, payload


def enabled(value=True):
    return patch.dict(runner.CONFIG["pr_review"], enabled=value)


class PRReviewIntakeTests(unittest.TestCase):
    def test_reviews_once_when_ready_never_on_push(self):
        cases = (("opened", {}, True), ("opened", {"draft": True}, False),
                 ("ready_for_review", {}, True), ("synchronize", {}, False), ("reopened", {}, False))
        for action, changes, expected in cases:
            with self.subTest(action=action, changes=changes), enabled():
                job = runner.event_job("pull_request", pr_event(action, **changes))
                if expected:
                    self.assertEqual(job["mode"], "pr_review")
                    self.assertEqual(job["head"], HEAD)
                    self.assertEqual(job["key"], f"{REPO}:pr_review:42:{HEAD}")
                else:
                    self.assertIsNone(job)

    def test_ineligible_prs_are_not_reviewed(self):
        cases = {
            "generated docs": {"head": {**PR["head"], "ref": runner.CONFIG["docs_update"]["branch_prefix"] + "x"}},
            "ignored": {"body": "please skip\n\nautokas:ignore"},
            "fork head": {"head": {**PR["head"], "repo": {"full_name": "someone/fork"}}},
            "closed": {"state": "closed"},
            "bad head sha": {"head": {**PR["head"], "sha": "abc"}},
        }
        for name, changes in cases.items():
            with self.subTest(name), enabled():
                self.assertIsNone(runner.event_job("pull_request", pr_event("opened", **changes)))

    def test_disabled_review_accepts_nothing(self):
        with enabled(False):
            self.assertIsNone(runner.event_job("pull_request", pr_event("opened")))
            self.assertIsNone(runner.event_job(*comment_event("@autokas review")))

    def test_review_command_is_its_own_job_and_other_commands_stay_coding_jobs(self):
        with enabled():
            for event in ("issue_comment", "pull_request_review_comment"):
                with self.subTest(event=event):
                    job = runner.event_job(*comment_event("@autokas  Review ", event=event))
                    self.assertEqual(job["mode"], "pr_review")
                    self.assertEqual(job["key"], f"{REPO}:pr_review:command:555")
                    self.assertEqual(job["author"], "kas")
            job = runner.event_job(*comment_event("@autokas review the parser and fix it"))
            self.assertEqual(job["mode"], "command")
            self.assertEqual(job["prompt"], "review the parser and fix it")

    def test_review_command_ignores_issues_and_bots(self):
        with enabled():
            self.assertIsNone(runner.event_job(*comment_event("@autokas review", on_issue=True)))
            # autokas[bot] posts the review itself; nothing it writes may start another job.
            self.assertIsNone(runner.event_job(*comment_event("@autokas review", user_type="Bot")))
            self.assertIsNone(runner.event_job(*comment_event("## PR Reviewer Guide 🔍\n@autokas review",
                                                              user_type="Bot")))


class PRReviewRunTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        for target, kwargs in (
            ("log", {"side_effect": lambda event, **fields: self.logs.append((event, fields))}),
            ("github_token", {"return_value": "ghs_installation_token"}),
            ("check_proxy_model", {}),
        ):
            patcher = patch.object(runner, target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        environ = patch.dict(runner.os.environ, {"CLI_PROXY_API_KEY": "proxy-secret-key",
                                                 "JARVIS_RUNNER_TOKEN": "jarvis-secret"})
        environ.start()
        self.addCleanup(environ.stop)

    def run_review(self, job, pr=None, permission="write", result=None):
        def github(path):
            return {"permission": permission} if path.endswith("/permission") else copy.deepcopy(pr or PR)

        run = Mock(return_value=result or subprocess.CompletedProcess([], 0, "", ""))
        with patch.object(runner, "github", side_effect=github), patch.object(runner.subprocess, "run", run):
            runner.pr_review.local(job)
        return run

    def auto_job(self, head=HEAD):
        return {"mode": "pr_review", "kind": "pull_request", "repo": REPO, "pr": 42, "head": head,
                "key": f"{REPO}:pr_review:42:{head}"}

    def command_job(self):
        return {"mode": "pr_review", "kind": "issue_comment", "repo": REPO, "pr": 42, "comment": 555,
                "author": "kas", "key": f"{REPO}:pr_review:command:555"}

    def events(self):
        return [event for event, _ in self.logs]

    def test_worker_routes_reviews_to_pr_agent_not_a_coding_container(self):
        with patch.object(runner, "pr_review") as review, patch.object(runner, "PRWorker") as coding:
            review.spawn.return_value = Mock(object_id="call")
            runner.worker.local(self.auto_job())
        review.spawn.assert_called_once()
        coding.assert_not_called()

    def test_automatic_review_skips_moved_head(self):
        run = self.run_review(self.auto_job(head="c" * 40))
        run.assert_not_called()
        self.assertEqual(self.events(), ["review_outdated"])

    def test_command_needs_write_access(self):
        for permission, runs in (("admin", 1), ("write", 1), ("read", 0), ("none", 0)):
            with self.subTest(permission=permission):
                self.logs.clear()
                run = self.run_review(self.command_job(), permission=permission)
                self.assertEqual(run.call_count, runs)
                if not runs:
                    self.assertEqual(self.events(), ["command_unauthorized"])

    def test_command_reviews_drafts_that_automatic_triggers_skip(self):
        draft = {**PR, "draft": True}
        self.assertEqual(self.run_review(self.auto_job(), pr=draft).call_count, 0)
        self.assertEqual(self.run_review(self.command_job(), pr=draft).call_count, 1)
        closed = {**PR, "state": "closed"}
        self.assertEqual(self.run_review(self.command_job(), pr=closed).call_count, 0)

    def test_review_uses_proxy_and_app_token_without_jarvis_or_fallback(self):
        run = self.run_review(self.auto_job())
        args, kwargs = run.call_args
        env = kwargs["env"]
        provider, model = runner.CONFIG["pr_review"]["model"].split("/", 1)
        self.assertEqual(args[0][-3:], ["--pr_url", f"https://github.com/{REPO}/pull/42", "review"])
        self.assertEqual(env["OPENAI__API_BASE"], runner.CONFIG["omp_models"]["providers"][provider]["baseUrl"])
        self.assertEqual(env["OPENAI__KEY"], "proxy-secret-key")
        self.assertEqual(env["CONFIG__MODEL"], f"openai/{model}")
        self.assertEqual(env["CONFIG__FALLBACK_MODELS"], "[]")
        self.assertEqual(env["GITHUB__USER_TOKEN"], "ghs_installation_token")
        self.assertNotIn("jarvis-secret", env.values())
        self.assertFalse(any(key.startswith("JARVIS") for key in env))
        self.assertEqual(self.events(), ["pr_review_started", "pr_review_done"])

    def test_failed_review_logs_redacted_output_and_fails(self):
        leak = "token ghs_installation_token key proxy-secret-key rejected"
        with self.assertRaises(RuntimeError):
            self.run_review(self.auto_job(), result=subprocess.CompletedProcess([], 1, "", leak))
        event, fields = self.logs[-1]
        self.assertEqual(event, "pr_review_failed")
        self.assertNotIn("ghs_installation_token", fields["detail"])
        self.assertNotIn("proxy-secret-key", fields["detail"])
        self.assertIn("[redacted]", fields["detail"])


if __name__ == "__main__":
    unittest.main()
