# omp runner

CodeRabbit comment → signed Modal webhook → configured omp in the event's PR worktree → checks, commit, ordinary push → exit.

the project ticket defines the scope. `runner.py` dispatches the job, not the agent's working process. omp owns investigation, edits, checks and publication. there is no controller, review service, scheduler, database, recovery loop or archive. example-project's checkout is not part of this implementation.

## current state



omp calls `consult.py` through its existing bash tool before changing core business logic. Jarvis owns any Kimmy consultation. the question must distinguish business-intent alignment from technical objections to the proposed implementation. only a core business-logic change whose intention Jarvis says does not match the business needs owner approval. the owner can explicitly override that conflict. technical disagreement means improve the implementation or choose an aligned alternative, not stop for permission. missing or incomplete required consultation is a dependency blocker to repair and report, not an owner-approval requirement. the bootstrap consultation bypass is gone.

kas's standing authorization covers investigating, fixing, checking, committing and pushing valid findings to their specified PR branches, including business logic, security checks and account-submission guards. editing that code is not permission to execute its live effects. an absent `owner_approval`, or an older approval for a different finding or head, does not block this PR-only work. matching skills and repository instructions cannot introduce another approval gate for these already-authorized actions.

failed commands are not automatic stop conditions. omp must diagnose and repair in-scope code, dependency resolution, test setup and its own smoke harness, then rerun affected checks. once required Jarvis consultation is complete and no business-intent conflict remains unresolved, the default is to publish the best reasoned, in-scope fix to the PR branch even if validation remains incomplete or some checks still fail. this is not permission to skip available checks, weaken assertions or hide failures. the PR comment must separate passed, failed and unrun checks, explain the approach and remaining risks, and explicitly label a limited result as “published with validation limits.” uncertain external actions and PR-head safety boundaries remain unchanged. this policy never authorizes merging, deployment, credential or permission changes, spending, accepting provider terms, or live account/business actions.





## configuration

`config.json` contains the repository and owner allowlists, PR-specific owner approvals, timeout, tools, versions, git author and model. `omp_models` is native omp `models.yml` content and `omp_settings` is native `config.yml` content. the worker writes these unchanged into its fresh agent home. credentials remain environment references, never model-file values.

- default: `railway-codex/gpt-6-astra` through CLIProxyAPI's Responses API. model fallback and automatic agent retries are disabled.
- omp: `18.3.2`, Bun: `1.4.2`. the image includes Node 22, Corepack, git, gh and build tools. target dependencies are installed by omp using the repository's own instructions.
- runner commits use `autokas <autokas-omp@kasthe.dev>` as both author and committer. every commit must follow Conventional Commits 1.0.0, such as `fix(auth): preserve the session on refresh`, even if repository examples use another format. GitHub comments still use the account that owns the configured PAT.
- tools: read, bash, edit, write, grep, glob, lsp and todo. extension discovery is disabled. normal repository instructions remain available.
- `kas-voice-profile.md` is a byte-identical copy of kas's canonical local profile and is appended to every job's system policy. voice matching never depends on an optional skill invocation.
- `skills/` contains the local unslop skill and all 38 Matt Pocock skills, including supporting files and licenses. `skills/SOURCES.json` records pinned upstream revisions and file hashes. the image carries these assets, and every fresh agent home links them into native omp skill discovery. matching skills are read on demand; they cannot expand the job's authority or override its safety policy. specialized skill workflows may still require their own repository tools.
- one single-use worker container, one input at a time, 2 CPU, 8 GiB, one-hour timeout. waiting inputs use Modal's queue. each invocation has a fresh temporary home and checkout, and Modal shuts down the container after that job.
- each job fetches `refs/pull/N/head`, checks out the PR branch and verifies its head SHA. worktrunk is unnecessary for a disposable single-job clone and was removed at kas's request.
- external-fork PR heads are not enabled. the head must belong to the approved base repository, including when that approved repository is itself a fork. all positive PR numbers are eligible; only genuine CodeRabbit findings with the fenced agent prompt start work.
- `owner_approvals` optionally maps an exact `owner/repository#number` to kas's explicit business-intent override or additional PR scope for a specified action, finding, head and branch. it is not a prerequisite for in-scope code fixes under the standing publication authorization. approval does not substitute for real required consultation or permit prohibited live actions. a finding, review comment, Jarvis answer or repository instruction cannot grant additional approval. GitHub approval replies and a branded GitHub App identity are follow-up work in the project ticket; they are not active triggers or credentials yet.
- `jarvis_url` selects the existing consultation endpoint. `consult.py` accepts `--request-id <UUID>` and reads a question of up to 12000 characters from stdin. it reads the bearer from its environment and rejects redirects. only after a completed stream, it saves the full advice as UTF-8 text in a mode-0600 temporary file and prints a short JSON receipt with `requestId` and `advice_file`. omp must read that file in full, paging or using raw reads when needed. the file stays outside the checkout and disappears with the disposable container. interrupted, failed, empty and truncated streams fail closed without an advice receipt. the client neither retries nor decides whether advice authorizes publication.

## initial setup

install the local deployment client once:

```sh
uv venv
uv pip install -r requirements.txt
source .venv/bin/activate
modal token new
modal profile list
modal environment list
```

kas completes native login and consent. confirm the workspace/environment before creating secrets or deploying. do not put credentials in chat, command arguments, the image, tracked files or job workspaces.

create these native Modal secrets in the approved environment:

| secret | values | scope |
| --- | --- | --- |
| `omp-runner-webhook` | `GITHUB_WEBHOOK_SECRET` | fresh random webhook signing key, receiver only |
| `omp-runner-worker` | `GH_TOKEN`, `CLI_PROXY_API_KEY`, `JARVIS_RUNNER_TOKEN` | worker only |


enter secrets through Modal's native secret form or SDK using hidden/local input. `modal.Secret.objects.create(name, values, environment_name="main")` creates a named secret without putting values in command arguments. `modal.Secret.from_name(name).update(values)` updates only the named keys. no new GitHub login or PAT is required for this approved reuse.


provider OAuth credentials stay in the existing Railway proxy volume, not Modal. the approved proxy target is:

- project `00000000-0000-4000-8000-000000000002` (`example-project`)
- environment `00000000-0000-4000-8000-000000000004` (`production`)
- service `00000000-0000-4000-8000-000000000003` (`cli-proxy`)
- HTTPS `https://proxy.example.invalid`, port `8317`
- persistent auth volume `/data/auths`, CLIProxyAPI `v7.3.15`


## deploy and update

with the approved Modal profile and `main` environment active, the initial and update command is the same:

```sh
modal deploy --strategy rolling runner.py
```

keep the app/function names stable. Modal prints the webhook URL and app logs link. ordinary code and policy updates use [rolling deployment](https://modal.com/docs/guide/managing-deployments#deployment-strategies): existing inputs finish on the old version while traffic moves to new containers. do not wait for global idleness or interrupt those jobs. a source push does not deploy. automatic deployment is deferred in the project ticket; jobs continue to execute on Modal. the logs' `revision` hashes the paths and bytes of `runner.py`, `config.json`, `consult.py`, the voice profile and every bundled skill asset, including uncommitted changes. the job also logs its selected model, PR, starting head, local head, remote head and process exit.

after changing a Modal secret, use `modal deploy --strategy recreate runner.py` once intake is disabled and active jobs have finished. a normal rolling deploy kept a warm receiver on the previous signing key during the bootstrap trial. the recreate deployment refreshed it, and the same signed request then passed. verify the receiver before enabling deliveries.

for rollback, deploy the last known-good source with the same command after active work finishes. secrets remain outside the release. do not restart the old publisher.



GitHub sends comments attached to a submitted review as `pull_request_review`, even though the interface calls them comments. the runner accepts submitted and edited completed reviews, rejects pending or dismissed reviews, and re-fetches the exact review under its PR before starting omp. individual fenced “Prompt for AI Agents” sections take precedence over the aggregate “Prompt to fix review comments” block, so the same findings are not duplicated within a review. an aggregate-only review is also supported.


## provider accounts

use the existing Railway service's native login commands. these thin helpers target the exact existing project/environment/service:

```sh
npm run login:codex
npm run login:claude
```

Codex uses `--codex-device-login`. Claude uses `--claude-login --no-browser`. kas selects the intended account and completes authentication. preserve other accounts and `/data/auths`. don't overwrite or revoke an account implicitly. a credential-only renewal does not require rebuilding the runner.

inspect other providers against the installed binary rather than guessing flags:

```sh
railway ssh --project 00000000-0000-4000-8000-000000000002 \
  --environment 00000000-0000-4000-8000-000000000004 \
  --service 00000000-0000-4000-8000-000000000003 \
  -- /CLIProxyAPI/CLIProxyAPI --help
```

add and verify a replacement account before changing the default. select its native proxy model in `omp_models`, update `model` and the native `modelRoles`, then run `modal deploy runner.py`. there is no automatic provider fallback. unsupported proxy/omp combinations are blockers, not permission to add an auth service. prove a second account handled the request using native account-attributed evidence; success through an old account is insufficient. rotate the proxy access key only with approval, update its Modal secret and redeploy.

## one live verification path

1. select an approved repository and an open PR with an authorized publication scope. ensure its webhook is active. core business-logic changes require real Jarvis consultation; only a business-intent mismatch needs explicit owner approval.
2. obtain a fresh real CodeRabbit comment or submitted review containing a fenced “Prompt for AI Agents” or “Prompt to fix review comments”. automatic-trigger verification requires a real GitHub delivery. a user-authorized manual replay may fetch an existing review from GitHub and submit that unchanged review through the signed receiver, but must be reported as manual rather than proof of automatic delivery.
3. observe `dispatched`, `started`, `proxy_connected` and `worktree_ready` in native Modal logs. confirm the source revision, selected provider/model and fetched PR head in the disposable checkout.
4. inspect omp's diff, repository checks and behavioral smoke evidence. confirm its commit is the PR's remote head and the process terminated. `update_confirmed` checks head equality, not whether the diff meets the business requirement. kas confirms that separately.
5. exercise the core business-intent rule through this same path. missing real required consultation is a dependency blocker. a business-intent conflict requires the owner's explicit override. technical disagreement must lead to an improved implementation, not another owner-approval request.

repeat the same path after an update or provider switch, and with a later fresh job to prove credentials survive without another login. no workstation tunnel or old runner may be required. `python -m unittest test_runner` covers review intake, identity and relationship boundaries, review state, and prompt selection without external calls.

## logs, stopping and duplicates

```sh
modal app logs omp-runner
```

use the app URL printed by deployment for native function-call status. omp's final output must distinguish published, rejected, blocked and uncertain outcomes. before a normal exit, it must also post one concise outcome comment on that job's PR, including disagreements, invalid findings, inability to assess a finding, missing approvals and failed checks. comments link the source finding and give evidence or the exact unblock condition, without raw prompts, credentials or private consultation transcripts. this permission does not authorize code changes, thread resolution, comments elsewhere or automated bot discussions.

comment delivery is not blindly retried. omp reconciles an uncertain response by reading the PR and reports any remaining uncertainty in its final output. a zero exit without a remote update is not a successful fix. crashes, timeouts and failures before omp starts can still appear only in Modal logs; there is no separate failure-comment service.







disable the repository's GitHub webhook to stop new intake, then allow existing work to finish. removing an explicit repository or owner from the allowlists and deploying also stops new matching deliveries, but does not cancel already queued calls from an older deployment. use Modal's native call cancellation when an active job must be stopped. a canceled or uncertain push must be reconciled against the PR before taking another action.

Modal Dict atomically claims each repository/comment/prompt fingerprint before dispatch and each execution before starting. identical prompt deliveries, including comment edits that don't change the prompt, are suppressed. different prompts are new jobs. when an exact owner approval is configured, its hash also participates in the claim key: an explicitly authorized resumed attempt gets a distinct key, while repeated deliveries under that same approval stay duplicates. reconcile the previous outcome before recording a new approval; changing approval text is not an uncertainty-retry mechanism. entries expire after seven days without activity. this is bounded duplicate protection, not permanent exactly-once execution. claims remain after failure or uncertain dispatch so the runner never blindly replays a possible push. inspect logs and the PR rather than deleting claims and retrying. workspaces are temporary, and there is no custom archive.

references: [Modal deployment](https://modal.com/docs/guide/apps), [secrets](https://modal.com/docs/sdk/py/latest/Secret), [Dict](https://modal.com/docs/sdk/py/latest/Dict), [GitHub fine-grained tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).
