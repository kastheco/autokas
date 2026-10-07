"""Linear intake contracts, with all external writes and Modal calls mocked."""

import io
import hashlib
import hmac
import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import linear_intake as linear
import runner


def event(action="created", body="approve", activity_id="activity-1"):
    # Official IssueChildWebhookPayload contains team, not project.
    return {"type": "AgentSessionEvent", "action": action, "organizationId": "org-1",
            "webhookTimestamp": 1000000, "promptContext": "Implement the requested change",
            "agentSession": {"id": "session-1", "issue": {
                "id": "issue-uuid", "identifier": "UTM-331", "url": "https://linear.app/utmco/issue/UTM-331",
                "team": {"id": "team-1"}, "teamId": "team-1", "title": "Fix it"}},
            "agentActivity": {"id": activity_id, "content": {"type": "prompt", "body": body}}}


class SignatureTests(unittest.TestCase):
    def test_raw_hmac_and_signed_timestamp(self):
        raw = json.dumps(event()).encode()
        sig = hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
        self.assertIsNotNone(linear.authenticated_payload(raw, sig, "secret", now=1000))
        self.assertIsNone(linear.authenticated_payload(raw + b" ", sig, "secret", now=1000))
        self.assertIsNone(linear.authenticated_payload(raw, sig, "secret", now=1061))
        self.assertIsNone(linear.authenticated_payload(raw, sig, "secret", now=939))
        for timestamp in (True, "1000000", None, float("nan"), float("inf")):
            payload = event()
            payload["webhookTimestamp"] = timestamp
            raw = json.dumps(payload).encode()
            sig = hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
            self.assertIsNone(linear.authenticated_payload(raw, sig, "secret", now=1000))

    def test_signature_precedes_json_parse(self):
        with patch.object(linear.json, "loads") as loads:
            self.assertIsNone(linear.authenticated_payload(b"invalid", "bad", "secret"))
            loads.assert_not_called()

    def test_job_identity_and_claim_keys(self):
        job = linear.event_job(event())
        self.assertEqual(job["key"], "linear:session:session-1")
        self.assertEqual(job["pr"], "UTM-331")
        self.assertEqual((job["mode"], job["target"], job["kind"], job["author"], job["comment"]),
                         ("command", "issue", "linear", "linear", 0))
        self.assertEqual(linear.event_job(event("prompted"))["key"], "linear:prompt:activity-1")
        for bad in ("../evil", "UTM-331/branch", ""):
            payload = event()
            payload["agentSession"]["issue"]["identifier"] = bad
            self.assertIsNone(linear.event_job(payload))

    def test_same_identifier_has_distinct_workspace_branches(self):
        first = event()
        second = event()
        second["organizationId"] = "org-2"
        first_job, second_job = linear.event_job(first), linear.event_job(second)
        self.assertNotEqual(first_job["branch"], second_job["branch"])
        prompted = event("prompted")
        prompted["agentSession"]["id"] = "another-session"
        self.assertEqual(linear.event_job(prompted)["branch"], first_job["branch"])
        for job in (first_job, second_job):
            self.assertTrue(job["branch"].endswith("/UTM-331"))


class WritebackTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for patcher in (patch.object(linear, "OAUTH_STORAGE", Path(directory.name)),
                        patch.object(runner, "LINEAR_OAUTH_VOLUME", Mock())):
            patcher.start()
            self.addCleanup(patcher.stop)
        # Emulate Modal's per-parameter single-input pools without remote calls.
        refresher = runner.LinearOAuthRefresher
        pools = {}
        guard = threading.Lock()

        def pool(client_id, organization_id):
            key = (client_id, organization_id)
            with guard:
                if key not in pools:
                    pools[key] = (threading.Lock(), refresher(client_id=client_id, organization_id=organization_id))
                lock, worker = pools[key]

            def refresh(timeout):
                with lock:
                    return worker.refresh.local(timeout)

            proxy = Mock()
            proxy.refresh.remote.side_effect = refresh
            return proxy

        patcher = patch.object(runner, "LinearOAuthRefresher", side_effect=pool)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_activity_shapes(self):
        job = {**linear.event_job(event()), "repo": "example-org/app"}
        with patch.object(linear, "graphql") as api:
            for kind in ("thought", "elicitation", "response", "error"):
                linear.activity(job, kind, "message")
                self.assertEqual(api.call_args.args[2], {"input": {
                    "agentSessionId": "session-1", "content": {"type": kind, "body": "message"}}})
            linear.activity(job, "action", "Cloning")
            self.assertEqual(api.call_args.args[2]["input"]["content"], {
                "type": "action", "action": "Cloning", "parameter": "example-org/app"})
            self.assertIn("AgentActivityCreateInput!", api.call_args.args[1])

    def test_plan_and_external_urls_input(self):
        plan = [{"content": "Change code", "status": "pending"}]
        urls = [{"label": "Pull request", "url": "https://github.com/example-org/app/pull/1"}]
        with patch.object(linear, "graphql") as api:
            linear.update_session(linear.event_job(event()), plan=plan, externalUrls=urls)
            self.assertEqual(api.call_args.args[2], {"id": "session-1", "input": {"plan": plan, "externalUrls": urls}})
            self.assertIn("agentSessionUpdate(id: $id, input: $input)", api.call_args.args[1])

    def test_org_token_and_timeout(self):
        tokens = {org: {"access_token": token, "refresh_token": "refresh", "expires_at": 99999999999}
                  for org, token in (("org-1", "token-1"), ("org-2", "token-2"))}
        with patch.dict(os.environ, LINEAR_OAUTH_TOKENS=json.dumps(tokens), LINEAR_CLIENT_ID="client"), \
                patch.object(runner, "CLAIMS") as claims, patch.object(linear.urllib.request, "urlopen") as urlopen:
            claims.get.return_value = None
            urlopen.return_value.__enter__.return_value = io.BytesIO(b'{"data":{"agentActivityCreate":{"success":true}}}')
            runner.linear_graphql.local("org-1", "mutation", {})
            self.assertEqual(urlopen.call_args.args[0].headers["Authorization"], "Bearer token-1")
            self.assertLess(urlopen.call_args.kwargs["timeout"], 5)
            with self.assertRaises(ValueError):
                runner.linear_graphql.local("missing", "mutation", {})
            self.assertEqual(urlopen.call_count, 1)

    def test_graphql_failure_not_success(self):
        for result in ({"errors": [{"message": "bad"}]}, {"data": {"agentSessionUpdate": {"success": False}}}):
            with patch.object(linear, "oauth_token", return_value="token"), \
                    patch.object(linear.urllib.request, "urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value = io.BytesIO(json.dumps(result).encode())
                with self.assertRaises(RuntimeError):
                    runner.linear_graphql.local("org-1", "query", {})

    def test_refresh_uses_rotated_credentials_after_next_expiry(self):
        initial = {"access_token": "old", "refresh_token": "refresh-0", "expires_at": 900}
        cache = {}
        claims = Mock()
        claims.get.side_effect = lambda key, default=None: cache.get(key, default)
        claims.put.side_effect = lambda key, value: cache.update({key: value})
        requests = []

        def exchange(request, **kwargs):
            requests.append(linear.urllib.parse.parse_qs(request.data.decode()))
            number = len(requests)
            response = {"access_token": f"access-{number}", "refresh_token": f"refresh-{number}", "expires_in": 3600}
            stream = Mock()
            stream.__enter__ = Mock(return_value=io.BytesIO(json.dumps(response).encode()))
            stream.__exit__ = Mock(return_value=False)
            return stream

        with patch.dict(os.environ, LINEAR_OAUTH_TOKENS=json.dumps({"org": initial}),
                        LINEAR_CLIENT_ID="client", LINEAR_CLIENT_SECRET="secret"), \
                patch.object(runner, "CLAIMS", claims), patch.object(linear.urllib.request, "urlopen", side_effect=exchange), \
                patch.object(linear.time, "time", return_value=1000) as clock:
            self.assertEqual(linear.oauth_token("org", 3), "access-1")
            self.assertEqual(linear.oauth_token("org", 3), "access-1")
            self.assertEqual(len(requests), 1)
            clock.return_value = 5000
            self.assertEqual(linear.oauth_token("org", 3), "access-2")
            cache.clear()  # Modal Dict entries disappear after a workspace is idle for seven days.
            clock.return_value = 1000 + 8 * 86400
            self.assertEqual(linear.oauth_token("org", 3), "access-3")
        self.assertEqual([request["refresh_token"] for request in requests], [["refresh-0"], ["refresh-1"], ["refresh-2"]])
        self.assertEqual(cache["linear:oauth:client:org"]["refresh_token"], "refresh-3")

    def test_valid_legacy_rotation_is_migrated_before_idle_cache_expiry(self):
        initial = {"access_token": "old", "refresh_token": "refresh-0", "expires_at": 900}
        cache = {"linear:oauth:client:org": {"access_token": "current", "refresh_token": "refresh-1", "expires_at": 4600}}
        claims = Mock()
        claims.get.side_effect = lambda key, default=None: cache.get(key, default)
        claims.put.side_effect = lambda key, value: cache.update({key: value})
        with patch.dict(os.environ, LINEAR_OAUTH_TOKENS=json.dumps({"org": initial}),
                        LINEAR_CLIENT_ID="client", LINEAR_CLIENT_SECRET="secret"), \
                patch.object(runner, "CLAIMS", claims), patch.object(linear.urllib.request, "urlopen") as exchange, \
                patch.object(linear.time, "time", return_value=1000) as clock:
            self.assertEqual(linear.oauth_token("org", 3), "current")
            exchange.assert_not_called()
            cache.clear()
            clock.return_value = 1000 + 8 * 86400
            exchange.return_value.__enter__.return_value = io.BytesIO(json.dumps({
                "access_token": "next", "refresh_token": "refresh-2", "expires_in": 3600}).encode())
            self.assertEqual(linear.oauth_token("org", 3), "next")
            self.assertEqual(linear.urllib.parse.parse_qs(exchange.call_args.args[0].data.decode())["refresh_token"], ["refresh-1"])

    def test_uncommitted_rotation_is_not_returned_or_cached(self):
        initial = {"access_token": "old", "refresh_token": "refresh-0", "expires_at": 900}
        with patch.dict(os.environ, LINEAR_OAUTH_TOKENS=json.dumps({"org": initial}),
                        LINEAR_CLIENT_ID="client", LINEAR_CLIENT_SECRET="secret"), \
                patch.object(runner, "CLAIMS") as claims, patch.object(linear.urllib.request, "urlopen") as exchange:
            claims.get.return_value = None
            exchange.return_value.__enter__.return_value = io.BytesIO(json.dumps({
                "access_token": "next", "refresh_token": "refresh-1", "expires_in": 3600}).encode())
            runner.LINEAR_OAUTH_VOLUME.commit.side_effect = RuntimeError("storage unavailable")
            with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
                linear.oauth_token("org", 3)
            claims.put.assert_not_called()


    def test_concurrent_calls_share_one_refresh_and_retain_rotation(self):
        initial = {"access_token": "old", "refresh_token": "refresh-0", "expires_at": 900}
        cache = {}
        start = threading.Barrier(2)
        reads = threading.local()
        exchange_lock = threading.Lock()
        used = []

        def get(key, default=None):
            token = cache.get(key, default)
            if not getattr(reads, "started", False):
                reads.started = True
                start.wait(timeout=5)
            return token

        def exchange(request, **kwargs):
            refresh = linear.urllib.parse.parse_qs(request.data.decode())["refresh_token"][0]
            with exchange_lock:
                if refresh in used:
                    raise RuntimeError("refresh token already consumed")
                used.append(refresh)
            response = {"access_token": "new", "refresh_token": "refresh-1", "expires_in": 3600}
            return io.BytesIO(json.dumps(response).encode())

        claims = Mock()
        claims.get.side_effect = get
        claims.put.side_effect = lambda key, value: cache.update({key: value})
        with patch.dict(os.environ, LINEAR_OAUTH_TOKENS=json.dumps({"org": initial}),
                        LINEAR_CLIENT_ID="client", LINEAR_CLIENT_SECRET="secret"), \
                patch.object(runner, "CLAIMS", claims), \
                patch.object(linear.urllib.request, "urlopen", side_effect=exchange), \
                patch.object(linear.time, "time", return_value=1000), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(linear.oauth_token, "org", 3) for _ in range(2)]
            self.assertEqual([future.result(timeout=5) for future in futures], ["new", "new"])
        self.assertEqual(used, ["refresh-0"])
        self.assertEqual(cache["linear:oauth:client:org"]["refresh_token"], "refresh-1")


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.payload = event()
        self.job = linear.event_job(self.payload)
        self.config = patch.dict(runner.CONFIG, {"allowed_owners": ["example-org", "untapped-media", "kastheco"],
                                                "linear": {"repo_map": {"projects": {}, "teams": {}}, "confidence_threshold": .8,
                                                           "gated_repos": ["example-org/gated"]}})
        self.config.start()
        self.addCleanup(self.config.stop)

    def test_project_before_team_and_suggestions(self):
        runner.CONFIG["linear"]["repo_map"] = {"projects": {"project-1": "example-org/project"}, "teams": {"team-1": "example-org/team"}}
        with patch.object(linear, "graphql", return_value={"issue": {"project": {"id": "project-1"}}}) as api, \
                patch.object(runner, "installed_repos") as installed:
            self.assertEqual(linear.resolve_repository(self.payload, self.job)[0], "example-org/project")
            self.assertIn("project { id }", api.call_args.args[1])
            installed.remote.assert_not_called()

    def test_team_before_suggestions(self):
        runner.CONFIG["linear"]["repo_map"]["teams"]["team-1"] = "example-org/team"
        with patch.object(linear, "graphql") as api, patch.object(runner, "installed_repos") as installed:
            self.assertEqual(linear.resolve_repository(self.payload, self.job), ("example-org/team", []))
            api.assert_not_called()
            installed.remote.assert_not_called()

    def test_disallowed_mapping_is_error_not_fallback(self):
        runner.CONFIG["linear"]["repo_map"]["teams"]["team-1"] = "evil/repo"
        with patch.object(linear, "graphql") as api, patch.object(runner, "installed_repos") as installed:
            with self.assertRaises(ValueError):
                linear.resolve_repository(self.payload, self.job)
            api.assert_not_called()
            installed.remote.assert_not_called()

    def test_suggestions_official_input_and_confidence(self):
        entries = [{"repositoryFullName": "example-org/app", "hostname": "github.com", "confidence": .9},
                   {"repositoryFullName": "evil/repo", "hostname": "github.com", "confidence": 1},
                   {"repositoryFullName": "example-org/not-installed", "hostname": "github.com", "confidence": 1},
                   {"repositoryFullName": "example-org/app", "hostname": "evil.com", "confidence": 1}]
        with patch.object(runner, "installed_repos") as installed, patch.object(linear, "graphql") as api:
            installed.remote.return_value = ["example-org/app", "evil/repo"]
            api.return_value = {"issueRepositorySuggestions": {"suggestions": entries}}
            self.assertEqual(linear.resolve_repository(self.payload, self.job)[0], "example-org/app")
            self.assertEqual(api.call_args.args[2], {"issueId": "issue-uuid", "sessionId": "session-1",
                                                    "repos": [{"hostname": "github.com", "repositoryFullName": "example-org/app"}]})
            entries[0]["confidence"] = .79
            self.assertIsNone(linear.resolve_repository(self.payload, self.job)[0])

    def test_gate_and_direct_spawn_without_dispatch(self):
        with patch.object(runner, "CLAIMS") as claims, patch.object(linear, "resolve_repository") as repository, \
                patch.object(linear, "set_state") as state, patch.object(runner, "worker") as worker, \
                patch.object(runner, "dispatch") as dispatch:
            claims.get.return_value = None
            for repo, gated in (("example-org/gated", True), ("Example-Org/Gated", True), ("example-org/app", False)):
                repository.return_value = (repo, [])
                linear.resolve(self.payload, self.job)
                job = worker.spawn.call_args.args[0]
                self.assertEqual(job["linear"]["plan_only"], gated)
                self.assertEqual(state.call_args.args[1], "planning" if gated else "running")
            dispatch.assert_not_called()

    def test_missing_gate_configuration_cannot_start_ungated_work(self):
        del runner.CONFIG["linear"]["gated_repos"]
        with patch.object(linear, "resolve_repository", return_value=("example-org/app", [])), \
                patch.object(linear, "activity"), patch.object(linear, "set_state"), patch.object(runner, "worker") as worker:
            with self.assertRaisesRegex(ValueError, "Configure linear.gated_repos"):
                linear.resolve(self.payload, self.job)
            worker.spawn.assert_not_called()


    def test_unknown_repo_elicits_and_stops(self):
        with patch.object(runner, "CLAIMS") as claims, patch.object(linear, "resolve_repository", return_value=(None, [])), \
                patch.object(linear, "activity") as activity, patch.object(linear, "set_state") as state, \
                patch.object(runner, "worker") as worker:
            claims.get.return_value = None
            linear.resolve(self.payload, self.job)
            self.assertEqual(activity.call_args.args[1], "elicitation")
            state.assert_called_once_with(self.job, "completed")
            worker.spawn.assert_not_called()

    def test_running_prompt_steers_without_spawn(self):
        payload = event("prompted", "Use existing validation")
        with patch.object(runner, "CLAIMS") as claims, patch.object(linear, "get_state", return_value={"state": "running", "job": self.job}), \
                patch.object(linear, "session_queue") as queue, patch.object(runner, "worker") as worker:
            claims.get.return_value = None
            linear.resolve(payload, linear.event_job(payload))
            queue.return_value.put.assert_called_once_with("Use existing validation")
            worker.spawn.assert_not_called()

    def test_failed_followup_preserves_job_and_allows_next_prompt(self):
        for status in ("running", "awaiting_approval"):
            with self.subTest(status=status):
                saved_job = {**self.job, "repo": "example-org/app"}
                saved = {"state": status, "job": saved_job, "plan": [{"content": "Fix parser", "status": "pending"}]}
                storage = {"linear:state:session-1": saved}
                claims = Mock()
                claims.get.side_effect = lambda key, default=None: storage.get(key, default)
                claims.put.side_effect = lambda key, value, **kwargs: storage.update({key: value}) or True
                with patch.object(runner, "CLAIMS", claims), patch.object(linear, "activity") as activity, \
                        patch.object(linear, "session_queue") as steering, patch.object(runner, "worker") as worker:
                    payload = event("prompted", "")
                    with self.assertRaisesRegex(ValueError, "no prompt text"):
                        linear.resolve(payload, linear.event_job(payload))
                    self.assertEqual(storage["linear:state:session-1"], saved)
                    self.assertEqual(activity.call_args.args[1], "error")
                    payload = event("prompted", "approve" if status == "awaiting_approval" else "Use existing validation")
                    linear.resolve(payload, linear.event_job(payload))
                    if status == "awaiting_approval":
                        approved = worker.spawn.call_args.args[0]
                        self.assertEqual(approved["repo"], saved_job["repo"])
                        self.assertIn("Fix parser", approved["prompt"])
                    else:
                        steering.return_value.put.assert_called_once_with("Use existing validation")


    def test_approval_required_and_once_only(self):
        saved_job = {**self.job, "repo": "untapped-media/tower", "linear": {**self.job["linear"], "plan_only": True}}
        saved = {"state": "awaiting_approval", "job": saved_job, "plan": [{"content": "Fix parser", "status": "pending"}]}
        with patch.object(runner, "CLAIMS") as claims, patch.object(linear, "get_state", return_value=saved), \
                patch.object(linear, "activity") as activity, patch.object(linear, "set_state") as state, \
                patch.object(runner, "worker") as worker:
            claims.get.return_value = None
            for body in ("What about tests?", "do not approve", "yes but wait", "", "looks good maybe"):
                if not body:
                    continue
                payload = event("prompted", body)
                linear.resolve(payload, linear.event_job(payload))
            worker.spawn.assert_not_called()
            claims.put.assert_not_called()
            self.assertEqual(activity.call_args.args[1], "elicitation")
            claims.put.return_value = True
            payload = event("prompted", "Approve!")
            linear.resolve(payload, linear.event_job(payload))
            approved = worker.spawn.call_args.args[0]
            self.assertFalse(approved["linear"]["plan_only"])
            self.assertEqual(approved["key"], "linear:prompt:activity-1")
            self.assertIn("Fix parser", approved["prompt"])
            state.assert_called_once_with(approved, "running")
            claims.put.return_value = False
            linear.resolve(payload, linear.event_job(payload))
            self.assertEqual(worker.spawn.call_count, 1)

    def test_completed_followup_asks_to_delegate_again(self):
        with patch.object(runner, "CLAIMS") as claims, patch.object(linear, "get_state", return_value={"state": "completed", "job": self.job}), \
                patch.object(linear, "activity") as activity, patch.object(runner, "worker") as worker:
            claims.get.return_value = None
            payload = event("prompted", "do another change")
            linear.resolve(payload, linear.event_job(payload))
            self.assertIn("delegate", activity.call_args.args[2])
            worker.spawn.assert_not_called()

    def test_finishing_is_persistable_but_not_a_live_steering_target(self):
        saved = {"state": "finishing", "job": self.job}
        with patch.object(runner, "CLAIMS") as claims, patch.object(linear, "get_state", return_value=saved), \
                patch.object(linear, "activity") as activity, patch.object(linear, "session_queue") as queue:
            linear.set_state(self.job, "finishing")
            claims.put.assert_called_once_with("linear:state:session-1", saved)
            payload = event("prompted", "more work")
            linear.resolve(payload, linear.event_job(payload))
            self.assertEqual(activity.call_args.args[1], "elicitation")
            queue.assert_not_called()

    def test_state_stores_resolved_job(self):
        with patch.object(runner, "CLAIMS") as claims:
            linear.set_state(self.job, "awaiting_approval", plan=[])
            claims.put.assert_called_once_with("linear:state:session-1", {"state": "awaiting_approval", "job": self.job, "plan": []})

    def test_disallowed_repo_emits_concrete_error_and_no_job(self):
        runner.CONFIG["linear"]["repo_map"]["teams"]["team-1"] = "evil/repo"
        with patch.object(linear, "activity") as activity, patch.object(linear, "set_state") as state, \
                patch.object(runner, "worker") as worker:
            with self.assertRaises(ValueError):
                linear.resolve(self.payload, self.job)
            self.assertEqual(activity.call_args.args[1], "error")
            self.assertIn("outside allowed owners", activity.call_args.args[2])
            state.assert_called_once_with(self.job, "error")
            worker.spawn.assert_not_called()


class ReceiverTests(unittest.IsolatedAsyncioTestCase):
    def request(self, payload, signature=None):
        raw = json.dumps(payload).encode()
        request = Mock()
        request.body = AsyncMock(return_value=raw)
        request.headers = {"linear-signature": signature or hmac.new(b"secret", raw, hashlib.sha256).hexdigest()}
        return request

    async def test_bad_auth_never_claims_acknowledges_or_spawns(self):
        with patch.dict(os.environ, LINEAR_WEBHOOK_SECRET="secret"), patch.object(linear.time, "time", return_value=1000), \
                patch.object(runner, "CLAIMS") as claims, patch.object(linear, "activity") as activity, \
                patch.object(runner, "linear_resolve") as resolver:
            for request in (self.request(event(), "bad"), self.request({**event(), "webhookTimestamp": 1})):
                self.assertEqual((await linear.receive(request)).status_code, 401)
            claims.put.aio.assert_not_called()
            activity.assert_not_called()
            resolver.spawn.aio.assert_not_called()

    async def test_ack_precedes_spawn_and_duplicate_does_nothing(self):
        calls = []
        async def spawn(*args):
            calls.append("spawn")
        with patch.dict(os.environ, LINEAR_WEBHOOK_SECRET="secret"), patch.object(linear.time, "time", return_value=1000), \
                patch.object(runner, "CLAIMS") as claims, patch.object(linear, "activity", side_effect=lambda *args: calls.append("ack")) as activity, \
                patch.object(linear, "set_state"), patch.object(runner, "linear_resolve") as resolver:
            claims.get.aio = AsyncMock(return_value=None)
            claims.put.aio = AsyncMock(side_effect=[True, False])
            resolver.spawn.aio = AsyncMock(side_effect=spawn)
            response = await linear.receive(self.request(event()))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(calls, ["ack", "spawn"])
            claims.put.aio.assert_awaited_with("linear:session:session-1", "claimed", skip_if_exists=True)
            response = await linear.receive(self.request(event()))
            self.assertEqual(json.loads(response.body)["status"], "duplicate")
            self.assertEqual(activity.call_count, 1)
            self.assertEqual(resolver.spawn.aio.await_count, 1)

    async def test_ack_failure_releases_claim_for_retry(self):
        with patch.dict(os.environ, LINEAR_WEBHOOK_SECRET="secret"), patch.object(linear.time, "time", return_value=1000), \
                patch.object(runner, "CLAIMS") as claims, patch.object(linear, "activity", side_effect=RuntimeError("offline")), \
                patch.object(runner, "linear_resolve") as resolver:
            claims.get.aio = AsyncMock(return_value=None)
            claims.put.aio = AsyncMock(return_value=True)
            claims.pop.aio = AsyncMock()
            response = await linear.receive(self.request(event()))
            self.assertEqual(response.status_code, 503)
            claims.pop.aio.assert_awaited_once_with("linear:session:session-1", None)
            resolver.spawn.aio.assert_not_called()

    async def test_uncertain_spawn_retains_claim_and_cannot_replay(self):
        with patch.dict(os.environ, LINEAR_WEBHOOK_SECRET="secret"), patch.object(linear.time, "time", return_value=1000), \
                patch.object(runner, "CLAIMS") as claims, patch.object(linear, "activity") as activity, \
                patch.object(linear, "set_state"), patch.object(runner, "log"), \
                patch.object(runner, "linear_resolve") as resolver:
            claims.put.aio = AsyncMock(side_effect=[True, False])
            resolver.spawn.aio = AsyncMock(side_effect=RuntimeError("connection lost"))
            response = await linear.receive(self.request(event()))
            self.assertEqual(response.status_code, 503)
            claims.pop.aio.assert_not_called()
            response = await linear.receive(self.request(event()))
            self.assertEqual(json.loads(response.body)["status"], "duplicate")
            self.assertEqual(resolver.spawn.aio.await_count, 1)
            self.assertEqual(activity.call_count, 1)


if __name__ == "__main__":
    unittest.main()
