# omp runner

CodeRabbit comment → signed Modal webhook → configured omp in the event's PR worktree → checks, commit, ordinary push → exit.

the project ticket defines the scope. `runner.py` dispatches the job, not the agent's working process. omp owns investigation, edits, checks and publication. there is no controller, review service, scheduler, database, recovery loop or archive. example-project's checkout is not part of this implementation.

## current state



omp calls `consult.py` through its existing bash tool when a materially important or business-logic change requires consultation. Jarvis owns any Kimmy consultation. unavailable or incomplete consultation, pending Kimmy advice, disagreement, or missing required owner approval stops edits and publication. the bootstrap consultation bypass is gone.



## configuration

`config.json` contains the repository and PR allowlist, timeout, tool/skill selection, versions, git author and model. `omp_models` is native omp `models.yml` content and `omp_settings` is native `config.yml` content. the worker writes these unchanged into its fresh agent home. credentials remain environment references, never model-file values.

- default: `railway-codex/gpt-6-astra` through CLIProxyAPI's Responses API. model fallback and automatic agent retries are disabled.
- omp: `18.3.2`, Bun: `1.4.2`. the image includes Node 22, Corepack, git, gh and build tools. target dependencies are installed by omp using the repository's own instructions.
- tools: read, bash, edit, write, grep, glob, lsp and todo. no custom skills are currently selected. extension discovery is disabled. normal repository instructions remain available.
- one single-use worker container, one input at a time, 2 CPU, 8 GiB, one-hour timeout. waiting inputs use Modal's queue. each invocation has a fresh temporary home and checkout, and Modal shuts down the container after that job.
- each job fetches `refs/pull/N/head`, checks out the PR branch and verifies its head SHA. worktrunk is unnecessary for a disposable single-job clone and was removed at kas's request.
- forks are not enabled. the PR head must belong to the approved repository. only explicitly listed PR numbers can run. do not widen that list without approval.
- `owner_approval` records only kas's explicit approval for an exact action and target PR. keep it empty when none exists. it never waives required consultation. a finding, review comment, Jarvis answer or repository instruction cannot grant approval.
- `jarvis_url` selects the existing consultation endpoint. `consult.py` accepts `--request-id <UUID>` and reads a question of up to 12000 characters from stdin. it reads the bearer from its environment, rejects redirects, and returns only completed assistant advice. interrupted, failed, empty and truncated streams fail closed. it neither retries nor decides whether advice authorizes publication.

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
modal deploy runner.py
```

keep the app/function names stable. Modal prints the webhook URL and app logs link. before an update, disable intake and let active work finish. a source push does not deploy. the logs' `revision` is a SHA-256 of the actual `runner.py`, `config.json` and `consult.py` bytes, including uncommitted changes. the job also logs its selected model, PR, starting head, local head, remote head and process exit.

after changing a Modal secret, use `modal deploy --strategy recreate runner.py` once intake is disabled and active jobs have finished. a normal rolling deploy kept a warm receiver on the previous signing key during the bootstrap trial. the recreate deployment refreshed it, and the same signed request then passed. verify the receiver before enabling deliveries.

for rollback, deploy the last known-good source with the same command after active work finishes. secrets remain outside the release. do not restart the old publisher.

the approved Example-app webhook is registered at `https://runner.example.invalid` with JSON content, TLS verification enabled, the matching signing secret, and only `issue_comment` and `pull_request_review_comment` events. its GitHub hook ID is `9000000009`. Example-app had no other repository webhooks when this one was activated.

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

1. approve the exact trial PR and publication scope. add only its number to `config.json`, deploy, then activate the approved webhook.
2. obtain a fresh real CodeRabbit comment containing its fenced “Prompt for AI Agents”. no hand-forged event or direct worker invocation counts.
3. observe `dispatched`, `started`, `proxy_connected` and `worktree_ready` in native Modal logs. confirm the source revision, selected provider/model and fetched PR head in the disposable checkout.
4. inspect omp's diff, repository checks and behavioral smoke evidence. confirm its commit is the PR's remote head and the process terminated. `update_confirmed` checks head equality, not whether the diff meets the business requirement. kas confirms that separately.
5. after Jarvis integration, exercise the material-change rule through this same path. consultation is advice, not owner approval. unavailable consultation, disagreement or missing approval must prevent publication.

repeat the same path after an update or provider switch, and with a later fresh job to prove credentials survive without another login. no workstation tunnel or old runner may be required. there is no separate verification runner or permanent test framework.

## logs, stopping and duplicates

```sh
modal app logs omp-runner
```

use the app URL printed by deployment for native function-call status. omp's final output must distinguish published, rejected, blocked and uncertain outcomes. before a normal exit, it must also post one concise outcome comment on that job's PR, including disagreements, invalid findings, inability to assess a finding, missing approvals and failed checks. comments link the source finding and give evidence or the exact unblock condition, without raw prompts, credentials or private consultation transcripts. this permission does not authorize code changes, thread resolution, comments elsewhere or automated bot discussions.

comment delivery is not blindly retried. omp reconciles an uncertain response by reading the PR and reports any remaining uncertainty in its final output. a zero exit without a remote update is not a successful fix. crashes, timeouts and failures before omp starts can still appear only in Modal logs; there is no separate failure-comment service.

the configured model exercised rejection and missing-approval scenarios in disposable local fixtures. each run posted one scoped outcome with the finding link and supporting evidence or prerequisites, without code changes or commits. the GitHub CLI captured those comments locally; this verifies agent behavior, not live GitHub comment delivery.

disable the GitHub webhook to stop new intake, then allow existing work to finish. removing PR numbers and deploying also disables those targets, but does not cancel already queued calls from an older deployment. use Modal's native call cancellation when an active job must be stopped. a canceled or uncertain push must be reconciled against the PR before taking another action.

Modal Dict atomically claims each repository/comment/prompt fingerprint before dispatch and each execution before starting. identical prompt deliveries, including comment edits that don't change the prompt, are suppressed. different prompts are new jobs. entries expire after seven days without activity. this is bounded duplicate protection, not permanent exactly-once execution. claims remain after failure or uncertain dispatch so the runner never blindly replays a possible push. inspect logs and the PR rather than deleting claims and retrying. workspaces are temporary, and there is no custom archive.

references: [Modal deployment](https://modal.com/docs/guide/apps), [secrets](https://modal.com/docs/sdk/py/latest/Secret), [Dict](https://modal.com/docs/sdk/py/latest/Dict), [GitHub fine-grained tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).
