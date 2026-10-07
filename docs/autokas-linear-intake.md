# autokas: linear intake

status: implemented. app provisioning and OAuth refresh are verified. production deployment and end-to-end sandbox acceptance remain unverified.

## goal

delegating a linear issue to autokas starts the same coding job an `@autokas` comment on a github issue starts today, and the linear issue shows progress and the resulting PR. a follow-up message in the linear session while the job runs steers it. after the PR opens, the existing review and fix loop runs unchanged.

## scope

in:
- a linear app installed as an agent (`actor=app`) in two configured workspaces.
- a second webhook endpoint for linear agent session events.
- mapping a linear session to a repo and a coding job.
- steering a running job from the linear session.
- progress, the PR link and the outcome written back to the linear session.
- a plan-first gate for the installed repositories listed in private `linear.gated_repos` configuration.

out, for later specs:
- run log, spend caps, DMS status widget.
- triage automation (auto-delegation without a human).
- any change to PR-Agent review, finding fixes, docs follow-ups or stack handling.

## linear side (from linear's developer docs, developer preview)

- app created with webhooks on and the "agent session events" category enabled. scopes include `app:assignable` and `app:mentionable`. assigning the app sets it as the issue's delegate, a human stays assignee.
- each workspace that installs the app gets its own OAuth access and refresh tokens. access tokens expire after 24 hours. renew them with the app credentials and retain rotated refresh tokens. payloads carry `organizationId`.
- `AgentSessionEvent` webhooks with `action: created` (delegated or mentioned) or `prompted` (a user follow-up, text in `agentActivity.content.body`).
- the receiver must return within 5 seconds. after `created`, the agent must emit an activity or set `externalUrls` within 10 seconds or the session is marked unresponsive.
- `promptContext` on the payload is a ready-made string with the issue, parent, project, comment threads and team guidance. use it as the job prompt.
- progress goes back through `agentActivityCreate` with types `thought`, `action`, `elicitation`, `response`, `error`. session state follows the last activity, no manual state.
- `agentSessionUpdate` sets `externalUrls` (put the PR there, linear says this enables PR features) and an optional `plan` checklist.
- `issueRepositorySuggestions` ranks candidate repos for an issue.
- `Linear-Signature` is a hex HMAC-SHA256 of the raw body with the webhook signing secret. the body's `webhookTimestamp` is unix milliseconds, and linear recommends rejecting anything more than 60 seconds off. source: https://linear.app/developers/webhooks.
- the API is developer preview and may change. pin the shapes in tests.

## autokas changes

linear intake and OAuth exchange code live in `linear_intake.py`. `runner.py` registers the per-workspace OAuth refresh class and owns the worker changes in items 5 and 6.

the dispatcher and coding containers do not mount `omp-runner-linear`. `linear_graphql` handles authenticated API requests in a separate Modal container and returns only GraphQL data to its callers. the webhook receiver, resolver and OAuth refresher retain their secret mounts. this preserves progress and write-back without exposing the Linear credential bundle through the coding worker's parent environment.

1. **new endpoint `linear_webhook`.** separate from the github `webhook`, with its own secret `omp-runner-linear`: the webhook signing secret, OAuth client credentials and each workspace's access token, refresh token and expiry, keyed by `organizationId`. every token lookup uses a single-container, single-input Modal class pool keyed by client id and organization id. the pool reloads credentials from the `<app>-linear-oauth` Modal Volume, reuses valid access tokens and commits rotations before returning them. concurrent callers reuse the completed rotation, and idle workspaces retain it without a shared token cache. neither access nor refresh tokens are written to `CLAIMS`. existing shared OAuth entries are migrated and removed after durable persistence on first use. idle entries require operator migration after deployment, and any already-exposed credentials need an operator's rotation assessment. check `Linear-Signature` on the raw bytes and `webhookTimestamp` within 60 seconds, `401` otherwise.
2. **ack.** before returning, the receiver posts one `thought` ("picked up, finding the repo").
3. **dedupe.** in the existing `CLAIMS` dict, `created` claims `linear:session:<agentSession.id>` and `prompted` claims `linear:prompt:<agentActivity.id>`.
4. **repo resolution, in order.** a repo map in private config from linear team or project to `owner/repo`. then `issueRepositorySuggestions` over `installed_repos()`, accepted only above a confidence threshold. otherwise an `elicitation` listing the top candidates and stop. a repo that fails `allowed_repository()` gets an `error` activity and no job.
5. **issue command flow without a github issue.** linear jobs run as `mode: "command"`, `target: "issue"` with the linear session id, organization id, identifier and issue url. where the flow assumes a github issue number:
   - new jobs use `autokas/<workspace-sha256>/<linear-identifier>`. the workspace component is the full SHA-256 hex digest of `organizationId`, so matching identifiers in different workspaces don't share a branch or reconcile to each other's PR. the identifier remains in the branch for linear linking. the worker takes the branch from the job instead of using its github issue branch format.
   - the command prompt gets a linear variant: task text is `promptContext`, PR title includes the identifier, the PR body links the linear issue instead of `Closes #<number>`, and there's no `gh issue comment` outcome.
   - no queued reply or acknowledgment comment on github.
6. **steering.** linear jobs run omp with `--mode rpc` instead of `--print`, same flags otherwise. the worker sends the task as a `prompt` command and reads events until it finishes. meanwhile it forwards each message from a `modal.Queue` named for the session to omp as a `steer` command. on `prompted`, the receiver puts `agentActivity.content.body` on that queue if the session has a live job. otherwise it answers with an `elicitation` saying to delegate again. a failed follow-up reports an error without replacing the saved job or plan. github-started jobs keep `--print`.
7. **write-back.** best-effort `action` activities at clone, checks and push. failed progress delivery logs `linear_activity_uncertain` without interrupting the agent or publication bookkeeping. on PR open, `agentSessionUpdate` adds the PR to `externalUrls`. finish with one strict `response` (what changed, checks run, PR link, draft or not) or `error` (why it stopped).
8. **plan-first gate.** private `linear.gated_repos` lists the installed repositories requiring a plan, matched case-insensitively. configure the list in the `AUTOKAS_CONFIG_JSON` overlay before deployment, retaining the existing gated repositories. a missing or malformed list stops job startup rather than silently removing approval gates. for listed repositories, the first run only investigates with read-only tools and posts a `plan` plus an `elicitation` asking to proceed. an explicit approval in the next `prompted` message starts the coding run.
9. **identity and access.** commits and PRs stay `autokas[bot]` through the existing github app. linear writes use the linear app token. anyone who can delegate to the app may use it.

approval startup keeps the saved job and plan in `awaiting_approval` until the coding worker starts. the approval claim stores the original approved job, and a successful spawn adds its Modal call id. if the spawn response fails, a later explicit approval retries that same job and command key. the serialized coding pool uses its existing execution and publication records to reconcile duplicate deliveries instead of executing the task again. a confirmed spawn receipt prevents another approval from enqueueing more work.

## acceptance

- delegating a test issue in a sandbox linear team to autokas produces, without other input: an ack within 10 seconds, progress activities, a PR on `autokas/<workspace-sha256>/<identifier>` linked in the linear session, and a final `response`.
- a follow-up message sent in the session during that run steers it.
- the PR then gets the normal `autokas review` check and fix loop.
- redelivering the same webhook does not start a second job.
- concurrent API calls for one client and workspace consume an expiring refresh token once and retain the rotated token for the next expiry.
- matching issue identifiers in different workspaces use distinct branches, even when both resolve to the same github repo.
- an issue with no resolvable repo ends in an elicitation, not a guess.
- delegating an issue for a gated repo posts a plan and waits. approving in the session starts the run.
- a bad signature gets `401`. a repo outside `allowed_owners` ends in an `error` activity and no job.
- existing tests pass, and new tests cover signature check, claim key, repo resolution order, gate and write-back payload shapes, all without external calls, matching the current test style.