"""Authenticated Linear agent-session intake and write-back.

Payloads follow Linear's agent-interaction docs and official SDK schema (2026-10-07).
Runner is imported lazily so it can register these functions on its own Modal app.
"""

import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any

import modal
from fastapi import Request
from fastapi.responses import JSONResponse

LINEAR_SECRET = modal.Secret.from_name(
    "omp-runner-linear", required_keys=["LINEAR_WEBHOOK_SECRET", "LINEAR_OAUTH_TOKENS",
                                       "LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET"]
)
GATED_REPOS = frozenset({
    "untapped-media/tower", "untapped-media/wallet-pass-server",
    "untapped-media/rewards-vault-server", "untapped-media/radar", "kastheco/autokas",
})
STATES = frozenset({"resolving", "running", "planning", "finishing", "awaiting_approval", "completed", "error"})
ACTIVITY_MUTATION = """mutation($input: AgentActivityCreateInput!) {
  agentActivityCreate(input: $input) { success }
}"""
SESSION_MUTATION = """mutation($id: String!, $input: AgentSessionUpdateInput!) {
  agentSessionUpdate(id: $id, input: $input) { success }
}"""
SUGGESTIONS_QUERY = """query($issueId: String!, $sessionId: String!, $repos: [CandidateRepository!]!) {
  issueRepositorySuggestions(issueId: $issueId, agentSessionId: $sessionId,
    candidateRepositories: $repos) { suggestions { repositoryFullName hostname confidence } }
}"""


def oauth_credentials(organization_id: str, client_id: str) -> dict[str, Any]:
    """Read the latest credentials for one client and workspace."""
    import runner
    tokens = json.loads(os.environ["LINEAR_OAUTH_TOKENS"])
    initial = tokens.get(organization_id) if isinstance(tokens, dict) else None
    if not isinstance(initial, dict):
        raise ValueError("No Linear OAuth token for this organization")
    cached = runner.CLAIMS.get(f"linear:oauth:{client_id}:{organization_id}", None)
    return cached if cached and cached["expires_at"] > initial["expires_at"] else initial


def oauth_token(organization_id: str, timeout: float) -> str:
    """Use valid cached tokens, serializing rotations across all callers."""
    import runner
    client_id = os.environ["LINEAR_CLIENT_ID"]
    token = oauth_credentials(organization_id, client_id)
    if token["expires_at"] > time.time() + 60:
        return token["access_token"]
    return runner.LinearOAuthRefresher(client_id=client_id, organization_id=organization_id).refresh.remote(timeout)


def refresh_oauth_token(organization_id: str, client_id: str, timeout: float) -> str:
    """Reread and rotate credentials inside this workspace's single-input pool."""
    import runner
    if client_id != os.environ["LINEAR_CLIENT_ID"]:
        raise ValueError("Linear OAuth client mismatch")
    token = oauth_credentials(organization_id, client_id)
    if token["expires_at"] <= time.time() + 60:
        request = urllib.request.Request(
            "https://api.linear.app/oauth/token",
            data=urllib.parse.urlencode({
                "grant_type": "refresh_token", "refresh_token": token["refresh_token"],
                "client_id": client_id,
                "client_secret": os.environ["LINEAR_CLIENT_SECRET"],
            }).encode(),
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            refreshed = json.load(response)
        token = {"access_token": refreshed["access_token"],
                 "refresh_token": refreshed["refresh_token"],
                 "expires_at": time.time() + refreshed["expires_in"]}
        runner.CLAIMS.put(f"linear:oauth:{client_id}:{organization_id}", token)
    return token["access_token"]


def graphql(organization_id: str, query: str, variables: dict[str, Any], timeout: float = 3.0) -> dict[str, Any]:
    """Use only the OAuth token belonging to this event's organization."""
    token = oauth_token(organization_id, timeout)
    request = urllib.request.Request(
        "https://api.linear.app/graphql", data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    if result.get("errors") or not isinstance(result.get("data"), dict):
        raise RuntimeError("Linear GraphQL request failed")
    data = result["data"]
    if any(isinstance(value, dict) and value.get("success") is False for value in data.values()):
        raise RuntimeError("Linear mutation was not successful")
    return data


def activity(job: dict[str, Any], type: str, body: str | dict[str, Any]) -> dict[str, Any]:
    """Emit an activity; action content uses action/parameter, not body."""
    if type not in {"thought", "action", "elicitation", "response", "error"}:
        raise ValueError("Unsupported Linear activity type")
    content = {"type": type}
    if type == "action":
        content.update(body if isinstance(body, dict) else {
            "action": body, "parameter": job.get("repo", job["linear"]["identifier"]),
        })
    else:
        content["body"] = body
    linear = job["linear"]
    return graphql(linear["organization_id"], ACTIVITY_MUTATION, {
        "input": {"agentSessionId": linear["session_id"], "content": content},
    })


def update_session(job: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """Replace a plan or set external URLs using the official update input."""
    linear = job["linear"]
    return graphql(linear["organization_id"], SESSION_MUTATION, {
        "id": linear["session_id"], "input": fields,
    })


def session_queue(session_id: str) -> modal.Queue:
    """One durable steering queue per session, with a safe bounded name."""
    import runner
    suffix = hashlib.sha256(session_id.encode()).hexdigest()[:32]
    return modal.Queue.from_name(f'{runner.CONFIG["app"]}-linear-{suffix}', create_if_missing=True)


def get_state(session_id: str) -> dict[str, Any] | None:
    import runner
    return runner.CLAIMS.get(f"linear:state:{session_id}", None)


def set_state(job: dict[str, Any], state: str, **fields: Any) -> None:
    """Persist the resolved job so a subsequent explicit approval can start it."""
    import runner
    if state not in STATES:
        raise ValueError("Unsupported Linear intake state")
    runner.CLAIMS.put(f'linear:state:{job["linear"]["session_id"]}', {
        "state": state, "job": job, **fields,
    })


def authenticated_payload(raw: bytes, signature: str, secret: str, now: float | None = None) -> dict[str, Any] | None:
    """Authenticate bytes before parsing or trusting the signed timestamp."""
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", signature) or not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(raw)
        timestamp = payload["webhookTimestamp"]
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            return None
        if abs((time.time() if now is None else now) * 1000 - timestamp) > 60000:
            return None
        return payload
    except (ValueError, KeyError, TypeError):
        return None


def event_job(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Create a Linear command without ever interpreting its identifier as a GH issue."""
    if payload.get("type") != "AgentSessionEvent" or payload.get("action") not in {"created", "prompted"}:
        return None
    session = payload.get("agentSession") or {}
    issue = session.get("issue") or {}
    session_id, organization = session.get("id"), payload.get("organizationId")
    identifier = issue.get("identifier")
    if not all(isinstance(value, str) and value for value in (session_id, organization, identifier, issue.get("url"), issue.get("id"))):
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*-\d+", identifier):
        return None
    if payload["action"] == "created":
        key = f"linear:session:{session_id}"
    else:
        activity_id = (payload.get("agentActivity") or {}).get("id")
        if not isinstance(activity_id, str) or not activity_id:
            return None
        key = f"linear:prompt:{activity_id}"
    workspace = hashlib.sha256(organization.encode()).hexdigest()
    return {"mode": "command", "target": "issue", "kind": "linear", "pr": identifier,
            "comment": 0, "author": "linear", "source_url": issue["url"],
            "branch": f"autokas/{workspace}/{identifier}", "prompt": payload.get("promptContext") or "", "key": key,
            "linear": {"session_id": session_id, "organization_id": organization,
                       "identifier": identifier, "issue_url": issue["url"], "plan_only": False}}


def resolve_repository(payload: dict[str, Any], job: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    """Prefer project mapping, then team mapping, then confident installed suggestions."""
    import runner
    issue = payload["agentSession"]["issue"]
    settings = runner.CONFIG.get("linear", {})
    mapping = settings.get("repo_map", {})
    # IssueChildWebhookPayload has team, but no project: hydrate project first.
    if mapping.get("projects") and "project" not in issue:
        result = graphql(job["linear"]["organization_id"],
                         "query($id: String!) { issue(id: $id) { project { id } } }",
                         {"id": issue["id"]})
        issue = {**issue, "project": result["issue"]["project"]}
    for field, group in (("project", "projects"), ("team", "teams")):
        entity = issue.get(field) or {}
        repo = mapping.get(group, {}).get(entity.get("id"))
        if repo:
            if not runner.allowed_repository(repo):
                raise ValueError("Configured Linear repository is outside allowed owners")
            return repo, []
    installed = [repo for repo in runner.installed_repos.remote() if runner.allowed_repository(repo)]
    if not installed:
        return None, []
    result = graphql(job["linear"]["organization_id"], SUGGESTIONS_QUERY, {
        "issueId": issue["id"], "sessionId": job["linear"]["session_id"],
        "repos": [{"hostname": "github.com", "repositoryFullName": repo} for repo in installed],
    })
    candidates = sorted((entry for entry in result["issueRepositorySuggestions"]["suggestions"]
                         if entry.get("repositoryFullName") in installed
                         and entry.get("hostname") in (None, "github.com")
                         and isinstance(entry.get("confidence"), (int, float))),
                        key=lambda entry: entry["confidence"], reverse=True)
    threshold = settings.get("confidence_threshold", 0.8)
    if candidates and candidates[0]["confidence"] >= threshold:
        return candidates[0]["repositoryFullName"], candidates
    return None, candidates


def explicit_approval(body: str) -> bool:
    """An arbitrary follow-up is not consent to execute a gated plan."""
    return body.strip().lower().rstrip(".! ") in {"approve", "approved", "proceed", "yes", "go ahead"}


def resolve(payload: dict[str, Any], job: dict[str, Any]) -> None:
    """Resolve or steer an already-claimed event; never call dispatch and re-claim it."""
    import runner
    try:
        if payload["action"] == "prompted":
            saved = get_state(job["linear"]["session_id"])
            if saved and saved["job"]["linear"]["organization_id"] != job["linear"]["organization_id"]:
                raise ValueError("Linear session organization mismatch")
            incoming = payload.get("agentActivity") or {}
            body = (incoming.get("content") or {}).get("body", "")
            if not isinstance(body, str) or not body.strip():
                raise ValueError("Linear follow-up has no prompt text")
            if saved and saved["state"] in {"running", "planning", "resolving"}:
                session_queue(job["linear"]["session_id"]).put(body)
                return
            if saved and saved["state"] == "awaiting_approval":
                if not explicit_approval(body):
                    activity(job, "elicitation", "The plan is waiting for approval. Reply 'approve' to start implementation.")
                    return
                if not runner.CLAIMS.put(f'linear:approval:{job["linear"]["session_id"]}', "claimed", skip_if_exists=True):
                    return
                approved = {**saved["job"], "key": job["key"], "linear": {**saved["job"]["linear"], "plan_only": False}}
                approved["prompt"] += "\n\nApproved implementation plan:\n" + json.dumps(saved.get("plan", []))
                if not runner.allowed_repository(approved["repo"]):
                    raise ValueError("Linear repository is outside allowed owners")
                set_state(approved, "running")
                runner.worker.spawn(approved)
                return
            activity(job, "elicitation", "There is no live job for this session. Please delegate the issue to Autokas again.")
            return
        repo, candidates = resolve_repository(payload, job)
        if repo is None:
            choices = "\n".join(f'- {entry["repositoryFullName"]} ({entry["confidence"]:.0%})' for entry in candidates[:5])
            activity(job, "elicitation", "I could not confidently resolve the repository. Configure the project or team mapping and delegate again."
                     + ("\nTop candidates:\n" + choices if choices else ""))
            set_state(job, "completed")
            return
        job = {**job, "repo": repo, "linear": {**job["linear"], "plan_only": repo.lower() in GATED_REPOS}}
        set_state(job, "planning" if job["linear"]["plan_only"] else "running")
        runner.worker.spawn(job)
    except Exception as exc:
        set_state(job, "error")
        safe_reason = str(exc) if isinstance(exc, ValueError) else "Repository resolution or job startup failed; inspect the runner logs."
        activity(job, "error", safe_reason)
        raise


async def receive(request: Request) -> JSONResponse:
    """Authenticate, claim once, acknowledge synchronously and spawn resolution."""
    import runner
    raw = await request.body()
    payload = authenticated_payload(raw, request.headers.get("linear-signature", ""), os.environ["LINEAR_WEBHOOK_SECRET"])
    if payload is None:
        return JSONResponse({"error": "invalid signature or timestamp"}, status_code=401)
    job = event_job(payload)
    if job is None:
        return JSONResponse({"status": "ignored"})
    if not await runner.CLAIMS.put.aio(job["key"], "claimed", skip_if_exists=True):
        return JSONResponse({"status": "duplicate"})
    try:
        if payload["action"] == "created":
            await asyncio.wait_for(asyncio.to_thread(activity, job, "thought", "picked up, finding the repo"), timeout=3.5)
            await asyncio.to_thread(set_state, job, "resolving")
    except Exception:
        await runner.CLAIMS.pop.aio(job["key"], None)
        return JSONResponse({"error": "Linear acknowledgment unavailable"}, status_code=503)
    try:
        await runner.linear_resolve.spawn.aio(payload, job)
    except Exception:
        # A failed spawn response may still have started the resolver. Never replay.
        runner.log("linear_spawn_uncertain", key=job["key"])
        return JSONResponse({"error": "Linear dispatch uncertain"}, status_code=503)
    return JSONResponse({"status": "accepted"})


def register(runner: Any) -> None:
    """Register both functions on the existing app without a circular top-level import."""
    receive.__name__ = "linear_webhook"
    resolve.__name__ = "linear_resolve"
    receiver = modal.fastapi_endpoint(method="POST")(receive)
    runner.linear_webhook = runner.app.function(
        image=runner.with_runner_files(runner.BASE_IMAGE), secrets=[LINEAR_SECRET], timeout=30,
    )(receiver)
    runner.linear_resolve = runner.app.function(
        image=runner.IMAGE, secrets=[runner.WORKER_SECRET, LINEAR_SECRET], timeout=300,
    )(resolve)
