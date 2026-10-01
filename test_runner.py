"""Behavior regressions for CodeRabbit review and comment intake."""

import copy
import json
import os
import subprocess
from email.message import Message
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


    def test_installed_repository_is_not_filtered_by_configured_allowlists(self) -> None:
        event = review_event()
        event["repository"]["full_name"] = "unlisted-owner/unlisted-repository"
        event["pull_request"]["base"]["repo"]["full_name"] = "unlisted-owner/unlisted-repository"
        event["review"]["pull_request_url"] = "https://api.github.com/repos/unlisted-owner/unlisted-repository/pulls/142"
        job = event_job("pull_request_review", event)
        self.assertIsNotNone(job)
        assert job is not None
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
            return {
                **self.followup,
                "head": {**self.followup["head"], "sha": self.current_head},
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
        with self.assertRaisesRegex(RuntimeError, "docs pull request merge failed or is uncertain"):
            self.run_worker()
        self.assertIsNone(self.merged_head)
        self.assertEqual(self.merge_attempts, 1)
        self.log.assert_not_called()

    def test_concurrent_merge_of_changed_head_is_not_confirmed(self) -> None:
        self.move_head = True
        self.concurrent_merge = True
        with self.assertRaisesRegex(RuntimeError, "docs pull request merge failed or is uncertain"):
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

    def disposable_checkout(self, *, mode="write", agent="docs", configured=True):
        """Run the docs flow against a local Git remote and an actual Node hook."""
        upstream = self.root / "upstream"
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
            "input.repo !== 'example/docs' || input.base_sha !== input.source_sha "
            "|| Number(process.env.OMP_POSTPROCESS_DEADLINE) <= Date.now() / 1000) process.exit(3);\n"
            "if (['CLI_PROXY_API_KEY', 'JARVIS_RUNNER_TOKEN', 'JARVIS_CONSULT_URL', "
            "'HOOK_MODE', 'EXPECT_BASE'].some(key => key in process.env)) process.exit(5);\n"
            f"const mode = {json.dumps(mode)};\n"
            "if (mode === 'fail') process.exit(4);\n"
            "if (mode === 'write') fs.writeFileSync('.railway/worker-releases.json', JSON.stringify(input));\n"
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

        def popen(args, *positional, **kwargs):
            if args[0] != "omp":
                return self.real_popen(args, *positional, **kwargs)
            checkout = self.root / "repo"
            if agent == "docs":
                (checkout / "docs/guide.md").write_text("Updated guide\n")
                command(["git", "add", "-A"], checkout)
                command(["git", "commit", "-m", "docs: update guide"], checkout, self.real_env)
            elif agent == "rename":
                command(["git", "mv", "src/service.py", "docs/service.py"], checkout)
                command(["git", "commit", "-m", "docs: rename service"], checkout, self.real_env)
            return self.process.return_value
        self.process.side_effect = popen

        def git_run(args, cwd):
            if args[:2] == ["gh", "auth"]:
                return ""
            if args[:2] == ["git", "clone"]:
                args = ["git", "clone", "--no-checkout", str(upstream), args[-1]]
            output = command(args, cwd, self.real_env)
            if args[:2] == ["git", "push"]:
                self.final_head = command(["git", "rev-parse", "HEAD"], cwd, self.real_env)
                self.current_head = self.final_head
                self.followup["head"]["sha"] = self.final_head
                names = command(["git", "diff", "--name-only", self.job["source_sha"], "HEAD"], cwd)
                self.followup_files = [{"filename": name} for name in names.splitlines()]
            return output
        self.git_run = git_run

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

    def test_disposable_unconfigured_noop_remains_error(self) -> None:
        self.disposable_checkout(mode="noop", agent="none", configured=False)
        with self.assertRaisesRegex(RuntimeError, "no committed change"):
            self.run_disposable()
        self.assertEqual(self.merge_attempts, 0)

    def test_disposable_configured_traversal_never_runs_hook(self) -> None:
        self.disposable_checkout()
        CONFIG["docs_update"]["repositories"]["example/docs"]["postprocess"]["files"] = [".railway/../src/injected.txt"]
        with self.assertRaisesRegex(RuntimeError, "invalid docs postprocess output path"):
            self.run_disposable()
        self.assertEqual(self.merge_attempts, 0)

if __name__ == "__main__":
    unittest.main()
