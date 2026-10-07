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
        patcher = patch.dict(os.environ, LINEAR_WEBHOOK_SECRET="synthetic-signing-secret")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(directory.cleanup)
        for patcher in (patch.object(linear, "OAUTH_STORAGE", Path(directory.name)),
                        patch.object(runner, "LINEAR_OAUTH_VOLUME", Mock())):
            patcher.start()
            self.addCleanup(patcher.stop)
        # Emulate Modal's per-parameter single-input pools without remote calls.
        refresher = runner.LinearOAuthRefresher
        self.refresher = refresher
        pools = {}
        guard = threading.Lock()

        def pool(client_id, organization_id):
            key = (client_id, organization_id)
            with guard:
                if key not in pools:
                    pools[key] = (threading.Lock(), refresher(client_id=client_id, organization_id=organization_id))
                lock, worker = pools[key]

            def refresh(deadline, signature):
                with lock:
                    return worker.refresh.local(deadline, signature)

            proxy = Mock()
            proxy.refresh.remote.side_effect = refresh
            return proxy

        patcher = patch.object(runner, "LinearOAuthRefresher", side_effect=pool)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_peer_cannot_run_unsigned_graphql(self):
        with patch.object(linear, "oauth_token", return_value="synthetic-token") as token, \
                patch.object(linear.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = io.BytesIO(b'{"data":{"viewer":{"id":"private"}}}')
            with self.assertRaises(PermissionError):
                runner.linear_graphql.local("org-2", "query { viewer { id } }", {}, linear.time.time() + 3)
            token.assert_not_called()
            urlopen.assert_not_called()

    def test_graphql_proof_binds_workspace_document_variables_and_deadline(self):
        arguments = ["org-1", "query($id: String!) { issue(id: $id) { id } }", {"id": "issue-1"}, linear.time.time() + 3]
        signature = linear.request_signature("graphql", arguments)
        altered = [
            ["org-2", *arguments[1:]],
            [arguments[0], "mutation { issueDelete(id: \"issue-1\") { success } }", *arguments[2:]],
            [*arguments[:2], {"id": "issue-2"}, arguments[3]],
            [*arguments[:3], arguments[3] + 30],
        ]
        with patch.object(linear, "oauth_token") as token:
            for request in altered:
                with self.subTest(request=request), self.assertRaises(PermissionError):
                    runner.linear_graphql.local(*request, signature)
            token.assert_not_called()

    def test_peer_cannot_extract_tokens_from_refresher(self):
        # Exercise the actual exported Modal method, not the emulated pool.
        refresher = self.refresher(client_id="client", organization_id="org-1")
        with patch.object(linear, "refresh_oauth_token") as refresh:
            for signature in ("", "0" * 64, linear.request_signature("writeback", ["org-1", "session-1"]),
                              linear.request_signature("oauth", ["client", "org-2", 3.0])):
                with self.subTest(signature=signature), self.assertRaises(PermissionError):
                    refresher.refresh.local(3.0, signature)
            refresh.assert_not_called()

    def test_peer_cannot_forge_resolver_intake(self):
        payload = event()
        job = linear.event_job(payload)
        signature = linear.request_signature("resolve", [payload, job])
        with patch.object(linear, "resolve") as resolve:
            for proof in ("", "0" * 64, linear.request_signature("writeback", ["org-1", "session-1"])):
                with self.assertRaises(PermissionError):
                    runner.linear_resolve.local(payload, job, proof)
            with self.assertRaises(PermissionError):
                runner.linear_resolve.local({**payload, "organizationId": "org-2"}, job, signature)
            with self.assertRaises(PermissionError):
                runner.linear_resolve.local(payload, {**job, "prompt": "forged task"}, signature)
            resolve.assert_not_called()

    def test_writeback_cannot_change_scope_or_gain_service_authority(self):
        signature = linear.request_signature("writeback", ["org-1", "session-1"])
        fields = {"type": "thought", "body": "progress"}
        with patch.object(linear, "oauth_token") as token, patch.object(linear, "graphql") as api:
            for org, session, proof in (("org-2", "session-1", signature), ("org-1", "session-2", signature),
                                        ("org-1", "session-1", "0" * 64)):
                with self.assertRaises(PermissionError):
                    runner.linear_writeback.local(org, session, proof, "activity", fields, linear.time.time() + 3)
            with self.assertRaises(PermissionError):
                runner.linear_graphql.local("org-1", "query { viewer { id } }", {}, linear.time.time() + 3, signature=signature)
            api.assert_not_called()
            token.assert_not_called()

    def test_writeback_rejects_arbitrary_operations_and_session_fields(self):
        signature = linear.request_signature("writeback", ["org-1", "session-1"])
        requests = [("query", {"query": "query { viewer { id } }"}),
                    ("session", {"id": "session-2", "plan": []}),
                    ("session", {"status": "complete"}),
                    ("activity", {"type": "thought", "body": "progress", "agentSessionId": "session-2"})]
        with patch.object(linear, "oauth_token") as api:
            for operation, fields in requests:
                with self.subTest(operation=operation, fields=fields), self.assertRaises(ValueError):
                    runner.linear_writeback.local("org-1", "session-1", signature, operation, fields, linear.time.time() + 3)
            api.assert_not_called()

    def test_delegated_writeback_uses_only_bound_session(self):
        job = linear.event_job(event())
        signature = linear.request_signature("writeback", ["org-1", "session-1"])
        job["linear"]["writeback_signature"] = signature
        with patch.object(runner.linear_writeback, "remote", side_effect=runner.linear_writeback.local), \
                patch.object(linear, "oauth_token", return_value="synthetic-token"), \
                patch.object(linear.urllib.request, "urlopen") as http:
            http.return_value.__enter__.side_effect = lambda: io.BytesIO(b'{"data":{"result":{"success":true}}}')
            linear.activity(job, "response", "finished")
            linear.update_session(job, plan=[{"content": "fix", "status": "pending"}],
                                  externalUrls=[{"label": "PR", "url": "https://github.com/example/app/pull/1"}])
            requests = [json.loads(call.args[0].data) for call in http.call_args_list]
        self.assertEqual(requests[0]["variables"], {"input": {
            "agentSessionId": "session-1", "content": {"type": "response", "body": "finished"}}})
        self.assertEqual(requests[1]["variables"]["id"], "session-1")
        self.assertEqual(set(requests[1]["variables"]["input"]), {"plan", "externalUrls"})

    def test_shared_state_does_not_confer_writeback_authority(self):
        job = linear.event_job(event())
        job["linear"]["writeback_signature"] = linear.request_signature("writeback", ["org-1", "session-1"])
        job = linear.signed_execution(job)
        with patch.object(runner, "CLAIMS") as claims:
            linear.set_state(job, "running")
        saved = claims.put.call_args.args[1]["job"]
        self.assertNotIn("writeback_signature", saved["linear"])
        self.assertNotIn("execution_signature", saved["linear"])
        with self.assertRaises(PermissionError):
            linear.authorize_worker(saved)
        with patch.object(linear, "oauth_token") as api, self.assertRaises(PermissionError):
            runner.linear_writeback.local(saved["linear"]["organization_id"], saved["linear"]["session_id"],
                                           saved["linear"].get("writeback_signature", ""), "session", {"plan": []}, linear.time.time() + 3)
        api.assert_not_called()

    def test_ack_writeback_does_not_reset_budget_after_refresh(self):
        now = [1000.0]
        calls = []
        job = linear.event_job(event())
        job["linear"]["writeback_signature"] = linear.request_signature("writeback", ["org-1", "session-1"])
        tokens = {"org-1": {"access_token": "old", "refresh_token": "refresh", "expires_at": 900}}

        def exchange(request, timeout):
            calls.append(request.full_url)
            if timeout < 2:
                now[0] += timeout
                raise TimeoutError("shared request budget exhausted")
            now[0] += 2
            result = ({"access_token": "new", "refresh_token": "rotated", "expires_in": 3600}
                      if request.full_url.endswith("/oauth/token") else {"data": {"result": {"success": True}}})
            return io.BytesIO(json.dumps(result).encode())

        with patch.dict(os.environ, LINEAR_OAUTH_TOKENS=json.dumps(tokens),
                        LINEAR_CLIENT_ID="client", LINEAR_CLIENT_SECRET="synthetic-secret"), \
                patch.object(linear.time, "time", side_effect=lambda: now[0]), \
                patch.object(runner, "CLAIMS") as claims, \
                patch.object(runner.linear_writeback, "remote", side_effect=runner.linear_writeback.local), \
                patch.object(linear.urllib.request, "urlopen", side_effect=exchange):
            claims.get.return_value = None
            with self.assertRaises(TimeoutError):
                linear.activity(job, "thought", "picked up, finding the repo")
        self.assertEqual(calls, ["https://api.linear.app/oauth/token", "https://api.linear.app/graphql"])
        self.assertLessEqual(now[0] - 1000, 3)

    def test_expired_refresher_queue_does_not_consume_refresh_token(self):
        deadline = 1003.0
        signature = linear.request_signature("oauth", ["client", "org-1", deadline])
        refresher = self.refresher(client_id="client", organization_id="org-1")
        with patch.object(linear.time, "time", return_value=deadline), \
                patch.object(linear.urllib.request, "urlopen") as http, \
                self.assertRaises(TimeoutError):
            refresher.refresh.local(deadline, signature)
        http.assert_not_called()

    def test_expired_persistence_keeps_rotation_and_skips_graphql(self):
        now = [1000.0]
        job = linear.event_job(event())
        job["linear"]["writeback_signature"] = linear.request_signature("writeback", ["org-1", "session-1"])
        tokens = {"org-1": {"access_token": "old", "refresh_token": "refresh", "expires_at": 900}}
        with patch.dict(os.environ, LINEAR_OAUTH_TOKENS=json.dumps(tokens),
                        LINEAR_CLIENT_ID="client", LINEAR_CLIENT_SECRET="synthetic-secret"), \
                patch.object(linear.time, "time", side_effect=lambda: now[0]), \
                patch.object(runner, "CLAIMS") as claims, \
                patch.object(runner.linear_writeback, "remote", side_effect=runner.linear_writeback.local), \
                patch.object(linear.urllib.request, "urlopen") as http:
            claims.get.return_value = None
            http.return_value.__enter__.return_value = io.BytesIO(json.dumps({
                "access_token": "new", "refresh_token": "rotated", "expires_in": 3600}).encode())
            runner.LINEAR_OAUTH_VOLUME.commit.side_effect = lambda: now.__setitem__(0, 1003.0)
            with self.assertRaises(TimeoutError):
                linear.activity(job, "thought", "picked up, finding the repo")
        self.assertEqual([call.args[0].full_url for call in http.call_args_list], ["https://api.linear.app/oauth/token"])
        filename = hashlib.sha256(json.dumps(["client", "org-1"]).encode()).hexdigest() + ".json"
        self.assertEqual(json.loads((linear.OAUTH_STORAGE / filename).read_text())["refresh_token"], "rotated")

    def test_org_token_and_timeout(self):
        tokens = {org: {"access_token": token, "refresh_token": "refresh", "expires_at": 99999999999}
                  for org, token in (("org-1", "token-1"), ("org-2", "token-2"))}
        with patch.dict(os.environ, LINEAR_OAUTH_TOKENS=json.dumps(tokens), LINEAR_CLIENT_ID="client"), \
                patch.object(runner, "CLAIMS") as claims, patch.object(linear.urllib.request, "urlopen") as urlopen:
            claims.get.return_value = None
            urlopen.return_value.__enter__.return_value = io.BytesIO(b'{"data":{"agentActivityCreate":{"success":true}}}')
            deadline = linear.time.time() + 3
            signature = linear.request_signature("graphql", ["org-1", "mutation", {}, deadline])
            runner.linear_graphql.local("org-1", "mutation", {}, deadline, signature=signature)
            self.assertEqual(urlopen.call_args.args[0].headers["Authorization"], "Bearer token-1")
            self.assertLess(urlopen.call_args.kwargs["timeout"], 5)
            with self.assertRaises(ValueError):
                signature = linear.request_signature("graphql", ["missing", "mutation", {}, deadline])
                runner.linear_graphql.local("missing", "mutation", {}, deadline, signature=signature)
            self.assertEqual(urlopen.call_count, 1)

    def test_graphql_failure_not_success(self):
        for result in ({"errors": [{"message": "bad"}]}, {"data": {"agentSessionUpdate": {"success": False}}}):
            with patch.object(linear, "oauth_token", return_value="token"), \
                    patch.object(linear.urllib.request, "urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value = io.BytesIO(json.dumps(result).encode())
                with self.assertRaises(RuntimeError):
                    deadline = linear.time.time() + 3
                    signature = linear.request_signature("graphql", ["org-1", "query", {}, deadline])
                    runner.linear_graphql.local("org-1", "query", {}, deadline, signature=signature)

    def test_rotated_credentials_never_enter_shared_claims(self):
        initial = {"access_token": "old", "refresh_token": "refresh-0", "expires_at": 900}
        cache = {"unrelated-job": "claimed"}
        claims = Mock()
        claims.get.side_effect = lambda key, default=None: cache.get(key, default)
        claims.put.side_effect = lambda key, value: cache.update({key: value})
        claims.pop.side_effect = lambda key, default=None: cache.pop(key, default)
        with patch.dict(os.environ, LINEAR_OAUTH_TOKENS=json.dumps({"org": initial}),
                        LINEAR_CLIENT_ID="client", LINEAR_CLIENT_SECRET="secret"), \
                patch.object(runner, "CLAIMS", claims), patch.object(linear.urllib.request, "urlopen") as exchange, \
                patch.object(linear.time, "time", return_value=1000):
            exchange.return_value.__enter__.return_value = io.BytesIO(json.dumps({
                "access_token": "next", "refresh_token": "refresh-1", "expires_in": 3600}).encode())
            self.assertEqual(linear.oauth_token("org", linear.time.time() + 3), "next")
            self.assertEqual(cache, {"unrelated-job": "claimed"})
            self.assertEqual(linear.oauth_token("org", linear.time.time() + 3), "next")
            self.assertEqual(exchange.call_count, 1)
            self.assertEqual(cache, {"unrelated-job": "claimed"})
        filename = hashlib.sha256(json.dumps(["client", "org"]).encode()).hexdigest() + ".json"
        persisted = json.loads((linear.OAUTH_STORAGE / filename).read_text())
        self.assertEqual(persisted["refresh_token"], "refresh-1")

    def test_refresh_uses_rotated_credentials_after_next_expiry(self):
        initial = {"access_token": "old", "refresh_token": "refresh-0", "expires_at": 900}
        cache = {}
        claims = Mock()
        claims.get.side_effect = lambda key, default=None: cache.get(key, default)
        claims.put.side_effect = lambda key, value: cache.update({key: value})
        claims.pop.side_effect = lambda key, default=None: cache.pop(key, default)
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
            self.assertEqual(linear.oauth_token("org", linear.time.time() + 3), "access-1")
            self.assertEqual(linear.oauth_token("org", linear.time.time() + 3), "access-1")
            self.assertEqual(len(requests), 1)
            clock.return_value = 5000
            self.assertEqual(linear.oauth_token("org", linear.time.time() + 3), "access-2")
            cache.clear()
            clock.return_value = 1000 + 8 * 86400
            self.assertEqual(linear.oauth_token("org", linear.time.time() + 3), "access-3")
        self.assertEqual([request["refresh_token"] for request in requests], [["refresh-0"], ["refresh-1"], ["refresh-2"]])
        self.assertEqual(cache, {})
        filename = hashlib.sha256(json.dumps(["client", "org"]).encode()).hexdigest() + ".json"
        self.assertEqual(json.loads((linear.OAUTH_STORAGE / filename).read_text())["refresh_token"], "refresh-3")

    def test_shared_oauth_poison_cannot_replace_durable_credentials(self):
        initial = {"access_token": "trusted", "refresh_token": "trusted-refresh", "expires_at": 4600}
        cache = {"linear:oauth:client:org": {
            "access_token": "poison", "refresh_token": "poison-refresh", "expires_at": 99999999999}}
        claims = Mock()
        claims.get.side_effect = lambda key, default=None: cache.get(key, default)
        claims.pop.side_effect = lambda key, default=None: cache.pop(key, default)
        with patch.dict(os.environ, LINEAR_OAUTH_TOKENS=json.dumps({"org": initial}), LINEAR_CLIENT_ID="client"), \
                patch.object(runner, "CLAIMS", claims), patch.object(linear.time, "time", return_value=1000):
            self.assertEqual(linear.oauth_token("org", 1003), "trusted")
        filename = hashlib.sha256(json.dumps(["client", "org"]).encode()).hexdigest() + ".json"
        self.assertEqual(json.loads((linear.OAUTH_STORAGE / filename).read_text()), initial)
        self.assertEqual(cache, {})

    def test_uncommitted_rotation_does_not_return_credentials(self):
        initial = {"access_token": "old", "refresh_token": "refresh-0", "expires_at": 900}
        with patch.dict(os.environ, LINEAR_OAUTH_TOKENS=json.dumps({"org": initial}),
                        LINEAR_CLIENT_ID="client", LINEAR_CLIENT_SECRET="secret"), \
                patch.object(runner, "CLAIMS") as claims, patch.object(linear.urllib.request, "urlopen") as exchange:
            claims.get.return_value = initial
            exchange.return_value.__enter__.return_value = io.BytesIO(json.dumps({
                "access_token": "next", "refresh_token": "refresh-1", "expires_in": 3600}).encode())
            runner.LINEAR_OAUTH_VOLUME.commit.side_effect = RuntimeError("storage unavailable")
            with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
                linear.oauth_token("org", linear.time.time() + 3)


    def test_concurrent_calls_share_one_refresh_and_retain_rotation(self):
        initial = {"access_token": "old", "refresh_token": "refresh-0", "expires_at": 900}
        cache = {}
        start = threading.Barrier(2)
        exchange_lock = threading.Lock()
        used = []

        def request_token():
            start.wait(timeout=5)
            return linear.oauth_token("org", linear.time.time() + 3)

        def exchange(request, **kwargs):
            refresh = linear.urllib.parse.parse_qs(request.data.decode())["refresh_token"][0]
            with exchange_lock:
                if refresh in used:
                    raise RuntimeError("refresh token already consumed")
                used.append(refresh)
            response = {"access_token": "new", "refresh_token": "refresh-1", "expires_in": 3600}
            return io.BytesIO(json.dumps(response).encode())

        claims = Mock()
        claims.get.side_effect = lambda key, default=None: cache.get(key, default)
        claims.put.side_effect = lambda key, value: cache.update({key: value})
        claims.pop.side_effect = lambda key, default=None: cache.pop(key, default)
        with patch.dict(os.environ, LINEAR_OAUTH_TOKENS=json.dumps({"org": initial}),
                        LINEAR_CLIENT_ID="client", LINEAR_CLIENT_SECRET="secret"), \
                patch.object(runner, "CLAIMS", claims), \
                patch.object(linear.urllib.request, "urlopen", side_effect=exchange), \
                patch.object(linear.time, "time", return_value=1000), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(request_token) for _ in range(2)]
            self.assertEqual([future.result(timeout=5) for future in futures], ["new", "new"])
        self.assertEqual(used, ["refresh-0"])
        self.assertEqual(cache, {})
        filename = hashlib.sha256(json.dumps(["client", "org"]).encode()).hexdigest() + ".json"
        self.assertEqual(json.loads((linear.OAUTH_STORAGE / filename).read_text())["refresh_token"], "refresh-1")


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.payload = event()
        self.job = linear.event_job(self.payload)
        patcher = patch.dict(os.environ, LINEAR_WEBHOOK_SECRET="synthetic-signing-secret")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = patch.dict(runner.CONFIG, {"allowed_owners": ["example-org"],
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


    def test_failed_followup_preserves_job_and_allows_next_prompt(self):
        for status in ("running", "awaiting_approval"):
            with self.subTest(status=status):
                saved_job = {**self.job, "repo": "example-org/app"}
                saved = {"state": status, "job": saved_job, "plan": [{"content": "Fix parser", "status": "pending"}]}
                saved = linear.signed_record("state", "session-1", saved)
                storage = {"linear:state:session-1": saved}
                claims = Mock()
                claims.get.side_effect = lambda key, default=None: storage.get(key, default)
                claims.put.side_effect = lambda key, value, **kwargs: storage.update({key: value}) or True
                with patch.object(runner, "CLAIMS", claims), patch.object(linear, "activity") as activity, \
                        patch.object(linear, "session_queue") as steering, patch.object(runner, "worker") as worker:
                    payload = event("prompted", "")
                    worker.spawn.return_value = Mock(object_id="fc-approved")
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


    def test_tampered_state_cannot_authorize_a_different_task(self):
        record = {"state": "awaiting_approval", "job": {**self.job, "repo": "example-org/gated"},
                  "plan": [{"content": "fix parser", "status": "pending"}]}
        signed = linear.signed_record("state", "session-1", record)
        variants = [{**signed, "state": "running"},
                    {**signed, "job": {**record["job"], "prompt": "attacker task"}},
                    {**signed, "plan": [{"content": "attacker plan", "status": "pending"}]},
                    linear.signed_record("state", "other-session", record),
                    linear.signed_record("approval", "session-1", record)]
        for forged in variants:
            with self.subTest(forged=forged), patch.object(runner, "CLAIMS") as claims, \
                    patch.object(linear, "activity"), patch.object(runner, "worker") as worker:
                claims.get.return_value = forged
                payload = event("prompted", "approve")
                with self.assertRaises(PermissionError):
                    linear.resolve(payload, linear.event_job(payload))
                worker.spawn.assert_not_called()

    def test_worker_cannot_get_forged_job_sealed_as_state(self):
        job = linear.signed_execution({**self.job, "repo": "example-org/app"})
        forged = {**job, "repo": "example-org/other", "prompt": "attacker task"}
        with patch.object(runner, "CLAIMS") as claims, self.assertRaises(PermissionError):
            runner.linear_set_state.local(forged, "awaiting_approval", {"plan": []})
        claims.put.assert_not_called()

    def test_unsigned_saved_plan_cannot_be_approved(self):
        saved = {"state": "awaiting_approval", "job": {**self.job, "repo": "example-org/gated"},
                 "plan": [{"content": "attacker task", "status": "pending"}]}
        with patch.object(runner, "CLAIMS") as claims, patch.object(linear, "activity"), \
                patch.object(runner, "worker") as worker:
            claims.get.return_value = saved
            payload = event("prompted", "approve")
            with self.assertRaises(PermissionError):
                linear.resolve(payload, linear.event_job(payload))
            worker.spawn.assert_not_called()

    def test_preseeded_approval_cannot_substitute_job(self):
        saved_job = {**self.job, "repo": "example-org/gated",
                     "linear": {**self.job["linear"], "plan_only": True}}
        with patch.object(runner, "CLAIMS") as claims, patch.object(linear, "activity"), \
                patch.object(runner, "worker") as worker:
            linear.set_state(saved_job, "awaiting_approval", plan=[{"content": "fix parser", "status": "pending"}])
            saved = claims.put.call_args.args[1]
            claims.get.side_effect = [saved, {"job": {**self.job, "repo": "example-org/app", "prompt": "attacker task"}}]
            claims.put.return_value = False
            payload = event("prompted", "approve")
            with self.assertRaises(PermissionError):
                linear.resolve(payload, linear.event_job(payload))
            worker.spawn.assert_not_called()

    def test_failed_approval_startup_can_retry_original_plan(self):
        saved_job = {**self.job, "repo": "example-org/gated", "linear": {**self.job["linear"], "plan_only": True}}
        saved = {"state": "awaiting_approval", "job": saved_job,
                 "plan": [{"content": "Fix parser", "status": "pending"}]}
        saved = linear.signed_record("state", "session-1", saved)
        storage = {"linear:state:session-1": saved}
        claims = Mock()

        def put(key, value, skip_if_exists=False):
            if skip_if_exists and key in storage:
                return False
            storage[key] = value
            return True

        claims.put.side_effect = put
        claims.get.side_effect = lambda key, default=None: storage.get(key, default)
        with patch.object(runner, "CLAIMS", claims), patch.object(linear, "activity"), \
                patch.object(runner, "worker") as worker, patch.object(linear, "session_queue") as queue:
            worker.spawn.side_effect = [RuntimeError("connection lost"), Mock(object_id="fc-approved")]
            first = event("prompted", "approve", "first-approval")
            with self.assertRaisesRegex(RuntimeError, "connection lost"):
                linear.resolve(first, linear.event_job(first))
            original = worker.spawn.call_args.args[0]
            self.assertEqual(storage["linear:state:session-1"], saved)
            second = event("prompted", "approve", "retry-approval")
            linear.resolve(second, linear.event_job(second))
            self.assertEqual(worker.spawn.call_count, 2)
            self.assertEqual(worker.spawn.call_args.args[0], original)
            self.assertEqual(original["key"], "linear:prompt:first-approval")
            self.assertFalse(original["linear"]["plan_only"])
            self.assertIn("Fix parser", original["prompt"])
            queue.assert_not_called()
            # A confirmed enqueue must not enqueue a third worker before startup.
            third = event("prompted", "approve", "confirmed-approval")
            linear.resolve(third, linear.event_job(third))
            self.assertEqual(worker.spawn.call_count, 2)

    def test_approval_required_and_once_only(self):
        saved_job = {**self.job, "repo": "example-org/gated", "linear": {**self.job["linear"], "plan_only": True}}
        saved = {"state": "awaiting_approval", "job": saved_job, "plan": [{"content": "Fix parser", "status": "pending"}]}
        storage = {}
        claims = Mock()

        def put(key, value, skip_if_exists=False):
            if skip_if_exists and key in storage:
                return False
            storage[key] = value
            return True

        claims.put.side_effect = put
        claims.get.side_effect = lambda key, default=None: storage.get(key, default)
        with patch.object(runner, "CLAIMS", claims), patch.object(linear, "get_state", return_value=saved), \
                patch.object(linear, "activity") as activity, patch.object(runner, "worker") as worker:
            worker.spawn.return_value = Mock(object_id="fc-approved")
            for body in ("What about tests?", "do not approve", "yes but wait", "", "looks good maybe"):
                if not body:
                    continue
                payload = event("prompted", body)
                linear.resolve(payload, linear.event_job(payload))
            worker.spawn.assert_not_called()
            claims.put.assert_not_called()
            self.assertEqual(activity.call_args.args[1], "elicitation")
            payload = event("prompted", "Approve!")
            linear.resolve(payload, linear.event_job(payload))
            approved = worker.spawn.call_args.args[0]
            self.assertFalse(approved["linear"]["plan_only"])
            self.assertEqual(approved["key"], "linear:prompt:activity-1")
            self.assertIn("Fix parser", approved["prompt"])
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
            payload = event("prompted", "more work")
            linear.resolve(payload, linear.event_job(payload))
            self.assertEqual(activity.call_args.args[1], "elicitation")
            queue.assert_not_called()


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
                patch.object(runner, "CLAIMS") as claims, patch.object(linear, "activity", side_effect=lambda *args, **kwargs: calls.append("ack")) as activity, \
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
