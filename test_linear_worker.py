"""Linear command routing, read-only planning and JSON-lines RPC regressions."""
import copy
import io
import tarfile
import os
import queue
import json
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import runner


class Claims:
    def __init__(self):
        self.data = {}

    def put(self, key, value, skip_if_exists=False):
        if skip_if_exists and key in self.data:
            return False
        self.data[key] = copy.deepcopy(value)
        return True

    def get(self, key, default=None):
        return copy.deepcopy(self.data.get(key, default))


JOB = {**runner.linear_intake.event_job({
    "type": "AgentSessionEvent", "action": "created", "organizationId": "o",
    "promptContext": "Implement the issue",
    "agentSession": {"id": "s", "issue": {
        "id": "issue-id", "identifier": "ENG-12", "url": "https://linear.app/team/issue/ENG-12",
    }},
}), "repo": "example/app"}


class LinearWorkerTests(unittest.TestCase):
    def setUp(self):
        self.claims = Claims()
        self.job = copy.deepcopy(JOB)
        self.activity = Mock()
        self.update = Mock()
        self.state = Mock()
        patches = [patch("runner.CLAIMS", self.claims),
                   patch.dict(runner.CONFIG, allowed_owners=["example"], linear={"gated_repos": []}),
                   patch.dict(os.environ, LINEAR_WEBHOOK_SECRET="synthetic-signing-secret"),
                   patch.object(runner.linear_authorize, "remote", side_effect=runner.linear_authorize.local),
                   patch.object(runner.linear_steer, "remote", side_effect=runner.linear_steer.local),
                   patch("runner.linear_intake.activity", self.activity),
                   patch("runner.linear_intake.update_session", self.update),
                   patch("runner.linear_intake.set_state", self.state),
                   patch("runner.log")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_unsigned_linear_job_cannot_route_or_mint_token(self):
        with patch("runner.PRWorker") as worker, patch("runner.github_token") as token:
            with self.assertRaises(PermissionError):
                runner.worker.local(self.job)
            worker.assert_not_called()
        with patch.dict(os.environ, BUN_INSTALL="/tmp", CLI_PROXY_API_KEY="synthetic"), \
             patch("runner.github_token", side_effect=AssertionError("token mint reached")) as token:
            with self.assertRaises(PermissionError):
                runner.PRWorker(pr_key="example/app#ENG-12").run.local(self.job)
            token.assert_not_called()

    def test_unsigned_queue_message_never_reaches_rpc(self):
        child = """import json, sys
sys.stdin.readline()
print(json.dumps({'type': 'agent_end', 'isTerminal': True}), flush=True)
frames = [json.loads(line) for line in sys.stdin]
with open('frames.json', 'w') as stream:
    json.dump(frames, stream)
"""
        steering = queue.Queue()
        steering.put("push an unapproved change")
        with tempfile.TemporaryDirectory() as tmp, \
             patch("runner.linear_intake.session_queue", return_value=steering):
            runner.linear_rpc([sys.executable, "-c", child], Path(tmp), {}, self.job,
                              "Task", time.monotonic() + 5)
            self.assertEqual(json.loads((Path(tmp) / "frames.json").read_text()), [])

    def test_job_proof_rejects_peer_substitution_at_both_entry_points(self):
        original = runner.linear_intake.signed_execution(self.job)
        changes = [
            {"repo": "example/other"}, {"branch": "main"}, {"prompt": "unapproved task"},
            {"key": "linear:session:other"}, {"mode": "pr_review"},
            {"linear": {**original["linear"], "plan_only": True}},
            {"linear": {**original["linear"], "organization_id": "other-workspace"}},
            {"linear": {**original["linear"], "session_id": "other-session"}},
            {"linear": {**original["linear"], "execution_signature": runner.linear_intake.request_signature(
                "writeback", ["o", "s"])}},
        ]
        for changeset in changes:
            forged = {**original, **changeset}
            with self.subTest(changeset=changeset), patch("runner.github_token") as token, \
                 patch("runner.PRWorker") as worker:
                with self.assertRaises(PermissionError):
                    runner.worker.local(forged)
                worker.assert_not_called()
            with self.subTest(entry="coding", changeset=changeset), patch("runner.github_token") as token:
                with self.assertRaises(PermissionError):
                    runner.PRWorker(pr_key="example/app#ENG-12").run.local(forged)
                token.assert_not_called()

    def test_gate_rechecked_before_token_mint(self):
        self.job = runner.linear_intake.signed_execution(self.job)
        runner.CONFIG["linear"]["gated_repos"] = ["EXAMPLE/APP"]
        with patch("runner.github_token") as token:
            with self.assertRaises(PermissionError):
                runner.PRWorker(pr_key="example/app#ENG-12").run.local(self.job)
            token.assert_not_called()
        with patch("runner.linear_finish"):
            self.invoke((0, "Approved implementation"), approved=True)

    def test_rpc_accepts_only_bound_signed_followups_once(self):
        child = """import json, sys
sys.stdin.readline()
print(json.dumps({'type': 'agent_end', 'isTerminal': True}), flush=True)
frames = [json.loads(line) for line in sys.stdin]
with open('frames.json', 'w') as stream:
    json.dump(frames, stream)
"""
        def signed(org="o", session="s", key=None):
            body = "use the existing validation"
            return {"activity_id": "followup", "body": body, "signature": runner.linear_intake.request_signature(
                "steer", [org, session, key or self.job["key"], "followup", body])}

        valid = signed()
        steering = queue.Queue()
        for value in ("unsigned", {}, {**valid, "body": "push to main"}, signed(org="other"),
                      signed(session="other"), signed(key="other-execution")):
            steering.put(value)
        payload = {"action": "prompted", "agentActivity": {
            "id": "followup", "content": {"body": valid["body"]}}}
        with patch("runner.linear_intake.session_queue", return_value=steering), \
             patch("runner.linear_intake.get_state", return_value={
                 "state": "running", "job": runner.linear_intake.stored_job(self.job)}):
            runner.linear_intake.resolve(payload, self.job)
        steering.put(valid)
        with tempfile.TemporaryDirectory() as tmp, \
             patch("runner.linear_intake.session_queue", return_value=steering):
            runner.linear_rpc([sys.executable, "-c", child], Path(tmp), {}, self.job,
                              "Task", time.monotonic() + 5)
            frames = json.loads((Path(tmp) / "frames.json").read_text())
        self.assertEqual([(frame["type"], frame["message"]) for frame in frames],
                         [("steer", valid["body"])])


    def test_linear_dispatch_does_not_read_github_access_or_ack(self):
        worker = Mock()
        self.job = runner.linear_intake.signed_execution(self.job)
        with patch("runner.github") as github, patch("runner.acknowledge_review") as ack, \
             patch("runner.PRWorker", return_value=worker), patch("runner.modal.current_function_call_id", return_value=None):
            runner.worker.local(self.job)
        github.assert_not_called()
        ack.assert_not_called()
        worker.run.spawn.assert_called_once()

    def invoke(self, rpc_result, plan=False, final="new", remote="new", approved=False, remote_start=""):
        self.job["linear"]["plan_only"] = plan
        self.job = runner.linear_intake.signed_execution(self.job, approved=approved)
        seen = {}

        def run(args, **kwargs):
            if args[:2] == ["git", "clone"]:
                Path(args[-1]).mkdir()
            output = ""
            if args[:2] == ["git", "rev-parse"]:
                output = "base" if "head_reads" not in seen else final
                seen["head_reads"] = True
            if args[:3] == ["git", "branch", "--show-current"]:
                output = "main"
            if args[:2] == ["git", "ls-remote"]:
                seen["remote_args"] = args
                current = remote if "remote_read" in seen else remote_start
                seen["remote_read"] = True
                output = current + "\trefs/heads/" + self.job["branch"] if current else ""
            return subprocess.CompletedProcess(args, 0, output, "")

        def rpc(args, worktree, env, job, prompt, deadline, **kwargs):
            seen["args"] = args
            seen["env"] = env
            seen["record_at_launch"] = self.claims.get("command:" + self.job["key"])
            if callable(rpc_result):
                return rpc_result(args, worktree, env, job, prompt, deadline)
            return rpc_result

        checkout = io.BytesIO()
        with tarfile.open(fileobj=checkout, mode="w") as archive:
            content = b"tracked repository content"
            entry = tarfile.TarInfo("source.txt")
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))

        def planner(job, archive, proxy_key, expires_at):
            return runner.linear_planner.local(job, checkout.getvalue(), proxy_key, expires_at)

        with patch.dict(os.environ, PATH=os.environ["PATH"], BUN_INSTALL="/tmp", CLI_PROXY_API_KEY="test"), \
             patch("runner.github_token", return_value="token"), patch("runner.check_proxy_model"), \
             patch("runner.subprocess.run", side_effect=run), patch("runner.linear_rpc", side_effect=rpc), \
             patch.object(runner.linear_planner, "remote", side_effect=planner), \
             patch("runner.modal.current_function_call_id", return_value=None), patch("runner.github", return_value=[]):
            runner.PRWorker(pr_key="example/app#ENG-12").run.local(self.job)
        return seen

    def test_plan_is_readonly_and_awaits_explicit_approval(self):
        runner.CONFIG["linear"]["gated_repos"] = ["example/app"]
        seen = self.invoke((0, '["Inspect callers", "Implement and test"]'), plan=True)
        args = seen["args"]
        self.assertEqual(args[args.index("--tools") + 1], "read,grep,glob")
        self.assertIn("--no-lsp", args)
        self.assertNotIn("--print", args)
        self.assertNotIn("remote_args", seen)
        self.assertIsNone(seen["record_at_launch"])
        self.assertNotIn("GH_TOKEN", seen["env"])
        self.assertNotIn("JARVIS_RUNNER_TOKEN", seen["env"])

        self.update.assert_called_once_with(self.job, plan=[{"content": "Inspect callers", "status": "pending"},
                                                          {"content": "Implement and test", "status": "pending"}])
        self.assertEqual(self.state.call_args.args[1], "awaiting_approval")
        self.assertEqual(self.activity.call_args.args[1], "elicitation")

    def test_planner_child_reads_checkout_but_not_worker_credentials(self):
        checkout = io.BytesIO()
        with tarfile.open(fileobj=checkout, mode="w") as archive:
            content = b"repository fixture"
            entry = tarfile.TarInfo("source.txt")
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
        child = """import json, os, pathlib, sys
frame = json.loads(sys.stdin.readline())
assert pathlib.Path('source.txt').read_text() == 'repository fixture'
for key in ('GH_TOKEN', 'GITHUB_APP_PRIVATE_KEY', 'JARVIS_RUNNER_TOKEN', 'LINEAR_WEBHOOK_SECRET'):
    assert key not in os.environ, key
text = json.dumps(['inspect repository fixture', 'implement after approval'])
print(json.dumps({'type':'message_end','message':{'role':'assistant','content':[{'type':'text','text':text}]}}), flush=True)
print(json.dumps({'type':'agent_end','isTerminal':True}), flush=True)
"""
        actual_rpc = runner.linear_rpc

        def rpc(args, worktree, env, job, prompt, deadline, **kwargs):
            return actual_rpc([sys.executable, "-u", "-c", child], worktree, env, job, prompt, deadline, **kwargs)

        with patch.dict(os.environ, BUN_INSTALL="/tmp", GH_TOKEN="synthetic-gh", \
                        GITHUB_APP_PRIVATE_KEY="synthetic-app", JARVIS_RUNNER_TOKEN="synthetic-advisor"), \
                patch("runner.linear_rpc", side_effect=rpc), patch("runner.linear_intake.session_queue") as queue:
            queue.return_value.get.return_value = None
            code, summary = runner.linear_planner.local(
                runner.linear_intake.stored_job(self.job), checkout.getvalue(), "synthetic-proxy", time.time() + 5)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(summary), ['inspect repository fixture', 'implement after approval'])
        self.state.assert_not_called()

    def test_normal_rpc_records_identifier_branch_before_execution(self):
        with patch("runner.linear_finish") as finish:
            seen = self.invoke((0, "Changed validation. Checks: unit tests passed."))
        self.assertEqual(seen["record_at_launch"]["branch"], self.job["branch"])
        self.assertEqual(seen["record_at_launch"]["state"], "executing")
        self.assertEqual(seen["remote_args"][-1], "refs/heads/" + self.job["branch"])
        finish.assert_called_once()
        self.assertEqual(self.claims.get("command:" + self.job["key"])["published_head"], "new")

    def test_progress_failure_does_not_lose_publication_receipt(self):
        def activity(job, kind, body):
            if kind == "action":
                raise TimeoutError("activity unavailable")
        self.activity.side_effect = activity
        summary = "Changed validation. Checks passed."
        with patch("runner.linear_finish") as finish:
            self.invoke((0, summary))
        record = self.claims.get("command:" + self.job["key"])
        self.assertEqual(record["published_head"], "new")
        self.assertEqual(record["linear_summary"], summary)
        finish.assert_called_once_with(self.job, summary, {"status": "published", "commit": "new"})

    def test_final_response_failure_remains_strict(self):
        self.activity.side_effect = TimeoutError("response unavailable")
        with patch("runner.github", return_value=[]), self.assertRaisesRegex(TimeoutError, "response unavailable"):
            runner.linear_finish(self.job, "Investigation complete", {"status": "completed"})
        record = self.claims.get("command:" + self.job["key"])
        self.assertTrue(record["linear_report_started"])
        self.assertFalse(record["linear_reported"])


    def test_unconfirmed_push_stops_with_linear_error(self):
        with self.assertRaisesRegex(RuntimeError, "could not be confirmed"):
            self.invoke((0, "Attempted changes"), remote="other")
        self.assertEqual(self.state.call_args.args[1], "error")
        self.assertEqual(self.activity.call_args.args[1], "error")

    def test_rpc_failures_do_not_publish_agent_output(self):
        private = "private prompt /work/private/file synthetic-gh-token synthetic-proxy-key synthetic-jarvis-token"
        assistant = {"role": "assistant", "content": [{"type": "text", "text": private}]}
        terminal = {"type": "agent_end", "isTerminal": True}
        cases = {
            "error": ([{"type": "message_end", "message": {**assistant, "stopReason": "error", "errorMessage": private}}, terminal], 0),
            "aborted": ([{"type": "message_end", "message": {**assistant, "stopReason": "aborted", "errorMessage": private}}, terminal], 0),
            "exit": ([{"type": "message_end", "message": assistant}, terminal], 7),
            "rejected": ([{"type": "response", "success": False, "command": private, "error": private}], 0),
            "closed": ([], 0),
        }
        popen = subprocess.Popen
        rpc = runner.linear_rpc
        for name, (frames, exit_code) in cases.items():
            with self.subTest(name=name):
                self.job["key"] = JOB["key"] + ":" + name
                child = f"""import json, sys
sys.stdin.readline()
print({private!r}, file=sys.stderr, flush=True)
for frame in {frames!r}:
    print(json.dumps(frame), flush=True)
if {bool(frames and frames[-1] == terminal)!r}:
    sys.stdin.read()
sys.exit({exit_code})
"""

                def launch(args, **kwargs):
                    return popen([sys.executable, "-c", child], **kwargs)

                with patch("runner.subprocess.Popen", side_effect=launch), \
                     patch("runner.linear_intake.session_queue", return_value=queue.Queue()), \
                     patch("runner.log") as log:
                    with self.assertRaises(RuntimeError) as caught:
                        self.invoke(rpc)
                self.assertEqual(self.state.call_args.args[1], "error")
                self.assertEqual(self.activity.call_args.args[1], "error")
                reports = str((self.state.call_args, self.activity.call_args, log.call_args_list))
                exception = "".join(traceback.format_exception(caught.exception))
                for text in (reports, exception):
                    for sensitive in ("private prompt", "/work/private/file", "synthetic-gh-token",
                                      "synthetic-proxy-key", "synthetic-jarvis-token"):
                        self.assertNotIn(sensitive, text)
                record = self.claims.get("command:" + self.job["key"])
                self.assertNotIn("linear_summary", record)
                self.assertNotIn("published_head", record)

    def test_no_change_completion_is_durable_without_remote_branch(self):
        self.invoke((0, "Investigation only. No checks run."), final="base", remote="")
        with patch("runner.github") as github:
            result = runner.command_publication(self.job)
        self.assertEqual(result["status"], "completed")
        github.assert_not_called()

    def test_reused_branch_without_push_completes_with_actual_summary(self):
        for final in ("base", "old", "unpublished"):
            with self.subTest(final=final):
                self.job["key"] = JOB["key"] + ":" + final
                summary = "No push. The task branch already exists."
                self.invoke((0, summary), final=final, remote="old", remote_start="old")
                record = self.claims.get("command:" + self.job["key"])
                self.assertEqual(record["state"], "completed")
                self.assertNotIn("published_head", record)
                self.assertEqual(self.activity.call_args.args, (self.job, "response", summary))
                self.assertEqual(self.state.call_args.args, (self.job, "completed"))

    def test_reused_branch_new_push_is_published(self):
        with patch("runner.linear_finish"):
            self.invoke((0, "Updated validation"), remote_start="old")
        record = self.claims.get("command:" + self.job["key"])
        self.assertEqual(record["published_head"], "new")

    def test_completed_run_does_not_attach_preexisting_pr(self):
        pr = {"html_url": "https://github.com/example/app/pull/4", "draft": False, "number": 4,
              "head": {"ref": self.job["branch"], "repo": {"full_name": "example/app"}}}
        summary = "The task branch already exists. No changes published."
        with patch("runner.github", return_value=[pr]):
            runner.linear_finish(self.job, summary, {"status": "completed"})
        self.update.assert_not_called()
        self.assertEqual(self.activity.call_args.args, (self.job, "response", summary))
        self.assertEqual(self.state.call_args.args, (self.job, "completed"))

    def test_finish_links_verified_pr_and_draft(self):
        pr = {"html_url": "https://github.com/example/app/pull/4", "draft": True, "number": 4,
              "head": {"ref": self.job["branch"], "repo": {"full_name": "example/app"}}}
        with patch("runner.github", return_value=[pr]):
            runner.linear_finish(self.job, "Tests failed", {"status": "published"})
        self.update.assert_called_once_with(self.job, externalUrls=[{"label": "Pull request", "url": pr["html_url"]}])
        self.assertIn("Tests failed", self.activity.call_args.args[2])
        self.assertIn("(draft)", self.activity.call_args.args[2])
        self.assertTrue(self.claims.get("command:" + self.job["key"])["linear_reported"])

    def test_published_branch_without_pr_is_error(self):
        with patch("runner.github", return_value=[]), self.assertRaisesRegex(RuntimeError, "no open pull request"):
            runner.linear_finish(self.job, "Changed code", {"status": "published"})
        self.activity.assert_not_called()

    def test_plan_rejects_fake_or_empty_checklist(self):
        for value in ("[]", '[" "]', '{"plan": ["do work"]}', "plain prose"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                runner.linear_plan(value)

    def test_disallowed_linear_repository_reports_error_without_dispatch(self):
        self.job["repo"] = "outside/app"
        self.job = runner.linear_intake.signed_execution(self.job)
        with patch("runner.PRWorker") as worker:
            runner.worker.local(self.job)
        worker.assert_not_called()
        self.assertEqual(self.activity.call_args.args[1], "error")

    def test_bootstrap_failure_is_reported_to_linear(self):
        self.job = runner.linear_intake.signed_execution(self.job)
        with patch.dict(os.environ, PATH=os.environ["PATH"], BUN_INSTALL="/tmp", CLI_PROXY_API_KEY="test"), \
             patch("runner.github_token", side_effect=RuntimeError("installation unavailable")):
            with self.assertRaisesRegex(RuntimeError, "installation unavailable"):
                runner.PRWorker(pr_key="example/app#ENG-12").run.local(self.job)
        self.assertEqual(self.activity.call_args.args[1], "error")

    def test_uncertain_response_is_not_duplicated(self):
        self.claims.put("command:" + self.job["key"], {"linear_report_started": True})
        with self.assertRaisesRegex(RuntimeError, "uncertain"), patch("runner.github") as github:
            runner.linear_finish(self.job, "Earlier result", {"status": "completed"})
        github.assert_not_called()
        self.activity.assert_not_called()

    def test_rpc_modal_empty_queue_returns_none(self):
        child = "import json,sys; sys.stdin.readline(); print(json.dumps({'type':'agent_end','isTerminal':True}),flush=True); sys.stdin.read()"
        modal_queue = Mock()
        modal_queue.get.return_value = None
        with tempfile.TemporaryDirectory() as tmp, patch("runner.linear_intake.session_queue", return_value=modal_queue):
            code, summary = runner.linear_rpc([sys.executable, "-c", child], Path(tmp), dict(os.environ),
                                              self.job, "Task", time.monotonic() + 5)
        self.assertEqual(code, 0)
        modal_queue.get.assert_called_once_with(block=False)

    def test_rpc_progress_failure_does_not_kill_agent(self):
        child = """import json, sys
sys.stdin.readline()
for command in ('python -m pytest', 'git push origin task'):
    print(json.dumps({'type': 'tool_execution_start', 'toolName': 'bash', 'args': {'command': command}}), flush=True)
print(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Checks completed'}]}}), flush=True)
print(json.dumps({'type': 'agent_end', 'isTerminal': True}), flush=True)
sys.stdin.read()
"""
        self.activity.side_effect = TimeoutError("activity unavailable")
        with tempfile.TemporaryDirectory() as tmp, patch("runner.linear_intake.session_queue", return_value=queue.Queue()):
            code, summary = runner.linear_rpc([sys.executable, "-c", child], Path(tmp), dict(os.environ),
                                              self.job, "Task", time.monotonic() + 5)
        self.assertEqual((code, summary), (0, "Checks completed"))
        self.assertEqual(self.state.call_args.args[1], "finishing")



    def test_rpc_deadline_terminates_process(self):
        with tempfile.TemporaryDirectory() as tmp, patch("runner.linear_intake.session_queue", return_value=queue.Queue()):
            with self.assertRaises(TimeoutError):
                runner.linear_rpc([sys.executable, "-c", "import time; time.sleep(30)"], Path(tmp),
                                  dict(os.environ), self.job, "Task", time.monotonic() + 0.1)


if __name__ == "__main__":
    unittest.main()
