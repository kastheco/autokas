# autokas operations

long-form operating notes for the runner, moved out of the readme unchanged. the Modal app, function names and package name stay `omp-runner` after the GitHub repo rename to `autokas`, so live URLs, secrets and logs keep working. start at the [readme](../README.md).

CodeRabbit comment → signed Modal webhook → configured omp in the event's PR worktree → checks, commit, ordinary push → exit.

scope is deliberately narrow. `runner.py` dispatches the job, not the agent's working process. omp owns investigation, edits, checks and publication. there is no controller, review service, scheduler, database, recovery loop or archive.

## current state

the runner is deployed as a Modal app, `omp-runner`, in the environment you choose. its endpoint rejects unsigned requests. CodeRabbit intake is available for every repository where the `autokas` GitHub App is installed. repository installation scope is the only repository boundary.

Jarvis, in these notes, stands for the one configured advisor. its consultation endpoint and bearer are private deployment values: `jarvis_url` in your `config.json` and `JARVIS_RUNNER_TOKEN` in the worker secret. the bearer belongs to a dedicated advisor-side viewer account, never a personal conversation.



Jarvis consultation is restricted to repositories whose owner equals the configured `jarvis_owner`, case-insensitively. other jobs receive neither its bearer nor its endpoint, and `consult.py` rejects missing, malformed or unpaired `OMP_JOB_REPO` values, or an empty `JARVIS_REPOSITORY_OWNER`, before any request. other owners continue technical fixes without Jarvis. changes to their business logic remain unimplemented and are reported with the proposed behavior change and missing business-owner decision. the consultation and business-intent override policy below applies only to jobs whose trusted context has `advisor_available` true.

queue and clean-review comments link their actual Modal dispatcher call. coding jobs receive both dispatcher and coding-call links in trusted context and must include them in acknowledgment updates and overall outcome comments. these dashboard links require Modal access and show run status and logs, not Temporal states.

Jarvis advises only on business decisions, business rules and business-intent alignment. omp calls `consult.py` through its existing bash tool before changing core business rules or intended business behavior. purely technical, safety or security fixes that preserve those rules and intentions do not need Jarvis consultation, including fixes in account-creation code. mixed changes are consulted only for their business effects. the question explicitly excludes implementation, safety, security, tests and technical risk. Jarvis owns any relevant Kimmy consultation.

Jarvis is not a safety, security or technical gatekeeper. omp independently assesses unsolicited technical concerns against code and evidence, fixes valid in-scope issues and preserves the job's safety boundaries without seeking Jarvis's endorsement. only a core business-logic change whose intention Jarvis says conflicts with the business needs an owner decision. the owner overrides that conflict with an @autokas command on the PR, which runs as a separate job. a technical objection alone is not that conflict, even when described as a blocker. missing or incomplete required business consultation is a dependency blocker to repair and report, not an owner-approval requirement.

kas's standing authorization covers investigating, fixing, checking, committing and pushing valid findings to their specified PR branches, including business logic, security checks and account-submission guards. editing that code is not permission to execute its live effects. matching skills and repository instructions cannot introduce another approval gate for these already-authorized actions.

failed commands are not automatic stop conditions. omp must diagnose and repair in-scope code, dependency resolution, test setup and its own smoke harness, then rerun affected checks. once required Jarvis consultation is complete and no business-intent conflict remains unresolved, the default is to publish the best reasoned, in-scope fix to the PR branch even if validation remains incomplete or some checks still fail. this is not permission to skip available checks, weaken assertions or hide failures. the PR comment must separate passed, failed and unrun checks, explain the approach and remaining risks, and explicitly label a limited result as “published with validation limits.” uncertain external actions and PR-head safety boundaries remain unchanged.






## configuration

copy `config.example.json` to `config.json` and fill in your webhook URL, provider base URL, `jarvis_owner` and `jarvis_url` (leave the last two empty to run with no advisor), and any per-repository docs settings. `config.json` is ignored by git; the runtime, Modal mounts and revision hash all read that filename.

`config.json` (copied from `config.example.json`, untracked and private) contains the advisor owner and URL, timeout, tools, versions, git author and execution profiles. repository eligibility comes from GitHub App installation scope, not a runner allowlist. `omp_models` is native omp `models.yml` content. the worker combines native `omp_settings` with model roles selected for the job and writes both files into its fresh agent home. credentials remain environment references, never model-file values.

- coding jobs use `railway-codex/gpt-6.1-sol`, high reasoning and requested priority service. docs jobs use `railway-codex/gpt-6-luna`, medium reasoning and default service. both use CLIProxyAPI's Responses API. native omp flags set the model, reasoning effort and service tier explicitly. all model roles follow the selected job profile. model fallback and automatic agent retries are disabled.
- omp: `18.3.2`, Bun: `1.4.2`. the image includes Node 22, Corepack, git, gh and build tools. target dependencies are installed by omp using the repository's own instructions.
- runner commits use `autokas[bot] <334744567+autokas[bot]@users.noreply.github.com>` as both author and committer. the numeric ID and login match the GitHub bot account, so GitHub can attribute commits independently of the push credential. every commit must follow Conventional Commits 1.0.0, such as `fix(auth): preserve the session on refresh`, even if repository examples use another format. GitHub API calls, comments, review replies, thread resolution and pushes use the `autokas` GitHub App installation token. the App is owned by `kastheco`; its private key, App ID and installation ID stay in the Modal `omp-runner-worker` secret. the required repository permissions are contents read/write, workflows read/write, pull requests read/write and issues read/write.
- tools: read, bash, edit, write, grep, glob, lsp and todo. extension discovery is disabled. normal repository instructions remain available.
- `kas-voice-profile.md` is a byte-identical copy of kas's canonical local profile and is appended to every job's system policy. voice matching never depends on an optional skill invocation.
- `skills/` contains the local unslop skill and all 38 Matt Pocock skills, including supporting files and licenses. `skills/SOURCES.json` records pinned upstream revisions and file hashes. the image carries these assets, and every fresh agent home links them into native omp skill discovery. matching skills are read on demand; they cannot expand the job's authority or override its safety policy. specialized skill workflows may still require their own repository tools.
- the durable `worker` entry queue dispatches to a native Modal `PRWorker` instance keyed by lowercase repository and PR number. each key has at most one single-use worker container and one active input, so different PRs can run concurrently while jobs for the same PR wait in Modal's queue. each agent has 2 CPU, 8 GiB, a one-hour timeout, a fresh temporary home and checkout. Modal shuts down its container after that job. queue order is not guaranteed.
- generated docs PRs include `@coderabbitai ignore` in their initial description, so CodeRabbit skips automatic review. a dedicated body marker excludes their issue comments from runner intake, and the `docs-update/` branch prefix also excludes review events and merged-PR events. the worker checks fetched PR metadata before starting an agent, so older queued findings for generated docs PRs are ignored too. ordinary PR reviews remain enabled.
- the dispatcher maps a completed review and its inline comment deliveries to one review job, regardless of arrival order. it fetches and paginates that exact review's inline findings, then atomically claims the review and finding fingerprints before starting a coding worker. each targeted inline finding gets one queued reply. when inline targets exist, there is no additional PR-level queued message. review-only and conversation findings retain a linked conversation acknowledgment. confirmed comment receipts go to the agent so it can edit those exact statuses. uncertain writes are reconciled against the configured bot account, never blindly repeated.
- completed clean reviews receive a linked “reviewed and okay” comment from `autokas[bot]`, attributing the result to CodeRabbit and identifying the reviewed commit. the dispatcher re-fetches the completed review and current PR head before posting. incomplete, stale, spoofed and generated-docs reviews are not acknowledged as clean. one clean acknowledgment is claimed per repository, PR and head within the existing duplicate-retention window. no coding agent starts for this path.
- after confirming its commit on the remote PR branch, the agent updates its existing inline queued comments with the outcome, commit link and relevant validation limits. it does not add separate completion replies. it then posts one overall PR outcome and resolves only the exact targeted threads whose findings were fixed and whose status updates were confirmed. rejected, blocked, unfixed and uncertain-push findings get status updates but stay unresolved. conversation comments and review-only findings have no thread to resolve.
- each job fetches `refs/pull/N/head`, checks out the PR branch and verifies its head SHA. worktrunk is unnecessary for a disposable single-job clone.
- external-fork PR heads are not enabled. the head must belong to the approved base repository, including when that approved repository is itself a fork. all positive PR numbers are eligible; only genuine CodeRabbit findings with the fenced agent prompt start work.
- a new human GitHub comment that starts with `@autokas` queues one command job. only users with `admin` or `write` repository permission may trigger it. a PR command runs on that PR's head branch. an issue command starts from the default branch, creates `autokas/issue-<number>`, pushes it and opens one pull request containing `Closes #<number>`. review-comment commands acknowledge the thread root because GitHub rejects replies to replies. edited comments and duplicate deliveries do not run.
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
| `omp-runner-worker` | `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `CLI_PROXY_API_KEY`, `JARVIS_RUNNER_TOKEN` | worker only |

`autokas` uses a GitHub App installation token with Contents, Workflows, Pull requests and Issues read/write permissions. Repository access is controlled only by the repositories selected when the App is installed. The runner does not apply a second repository allowlist.

enter secrets through Modal's native secret form or SDK using hidden/local input. `modal.Secret.objects.create(name, values, environment_name="main")` creates a named secret without putting values in command arguments. `modal.Secret.from_name(name).update(values)` updates only the named keys. no new GitHub login or PAT is required for this approved reuse.


provider OAuth credentials stay in the existing Railway proxy volume, not Modal. the proxy target is:

- your Railway project, environment and service ids, exported as `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID` and `RAILWAY_SERVICE_ID`
- the service's HTTPS URL, set as `omp_models.providers.*.baseUrl` in `config.json` (`https://proxy.example.invalid/v1` in the example)
- persistent auth volume `/data/auths`, CLIProxyAPI


## deploy and update

pushes to `main` run `.github/workflows/deploy.yml`. a manual dispatch on `main` uses the same serialized workflow. it runs the tests, then `deploy.py` performs the complete cutover:

1. persist the reconcile window (its start, the source revision and reconciled repositories) before anything else.
2. wait for the receiver, dispatcher and aggregate PR worker pools to have no running inputs or backlog across three consecutive checks. a drain timeout prevents deployment. App webhooks can't be paused through GitHub's API, so intake stays open during the cutover.
3. deploy with `modal deploy --strategy recreate runner.py`, reject an unsigned request with `401`, and redeliver the newest signed App delivery from inside Modal, requiring a `200` or `202` with the expected source revision. the App private key never leaves Modal.
4. list every repository the App is installed on (through the `installed_repos` Modal function), then reconstruct eligible comments, reviews and merged-PR events changed since the window began. existing atomic claims suppress duplicate jobs, so deliveries that landed during the cutover aren't run twice.

the workflow uses the `DEPLOY_GH_TOKEN`, `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` and `AUTOKAS_CONFIG_JSON` repository secrets. `AUTOKAS_CONFIG_JSON` holds the full contents of your private `config.json`. the workflow tests against `config.example.json`, then writes the secret to `config.json` (mode 0600) before deploying, and refuses to deploy if it is missing, invalid or still the example webhook. deployment and reconcile output goes to private runner files and only exit statuses are printed, because that output names your repositories. worker logs stay in Modal. `DEPLOY_GH_TOKEN` needs read access to the issues, pull requests and reviews of every installed repository. worker credentials remain in Modal. workflow runs do not cancel an active deployment. do not run a separate local deployment concurrently.

the reconcile window and its progress live in the existing Modal Dict under `__deployment_pause__`. failure cleanup always runs the reconcile. if the runner is forcibly terminated before cleanup can finish, manually dispatch the workflow again. the next run reconciles from the unfinished window's start, so nothing in that gap is skipped. uncertain job dispatches retain their claims rather than blindly repeating publication.

for rollback, revert the source on `main` and let the same workflow deploy it. after changing a Modal secret, manually dispatch the workflow to recreate containers through the same drain and reconcile. keep app and function names stable. secrets remain outside the release.

the logs' `revision` hashes the paths and bytes of `runner.py`, `config.json`, `consult.py`, the voice profile and every bundled skill asset. every receiver response carries that revision so the workflow verifies the running code, not just an HTTP success. each coding job also logs its selected model, reasoning effort, requested service tier, PR and worktree head.

the `autokas` App has one webhook, pointed at the `webhook_url` in your `config.json` (`https://runner.example.invalid/webhook` in the example), with JSON content, the matching signing secret, and the `issue_comment`, `pull_request`, `pull_request_review_comment` and `pull_request_review` events. no repository webhooks are needed. the installed `autokas` App is the repository boundary. installing it on a repository makes that repository eligible and starts delivery, and no separate runner allowlist or per-repository registration is required.


GitHub delivers submitted reviews and inline review comments as distinct events. the runner accepts completed reviews, rejects pending or dismissed reviews, and verifies review membership through GitHub IDs rather than prompt similarity. both event paths fetch the same review and canonical finding set before dispatch, so an aggregate prompt and its differently worded inline prompts cannot create separate coding runs. a review without an aggregate prompt uses its inline prompts. a review without inline findings still gets one review-only job. the worker revalidates the complete finding fingerprint before starting omp.


## provider accounts

use the existing Railway service's native login commands. these thin helpers target the project, environment and service named by the required `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID` and `RAILWAY_SERVICE_ID` variables, and refuse to run if any is unset:

```sh
npm run login:codex
npm run login:claude
```

Codex uses `--codex-device-login`. Claude uses `--claude-login --no-browser`. kas selects the intended account and completes authentication. preserve other accounts and `/data/auths`. don't overwrite or revoke an account implicitly. a credential-only renewal does not require rebuilding the runner.

inspect other providers against the installed binary rather than guessing flags:

```sh
railway ssh --project "$RAILWAY_PROJECT_ID" \
  --environment "$RAILWAY_ENVIRONMENT_ID" \
  --service "$RAILWAY_SERVICE_ID" \
  -- /CLIProxyAPI/CLIProxyAPI --help
```

add and verify a replacement account before changing the default. select its native proxy model in `omp_models`, update `model` and the native `modelRoles`, then push the change to `main` for the deployment workflow. there is no automatic provider fallback. unsupported proxy/omp combinations are blockers, not permission to add an auth service. prove a second account handled the request using native account-attributed evidence. success through an old account is insufficient. rotate the proxy access key only with approval, update its Modal secret and manually dispatch the deployment workflow.

## one live verification path

1. select an approved repository and an open PR with an authorized publication scope. ensure the App is installed on it and the App webhook is active. core business-logic changes require real Jarvis consultation; only a business-intent mismatch needs explicit owner approval.
2. obtain a fresh real CodeRabbit comment or submitted review containing a fenced “Prompt for AI Agents” or “Prompt to fix review comments”. automatic-trigger verification requires a real GitHub delivery. a user-authorized manual replay may fetch an existing review from GitHub and submit that unchanged review through the signed receiver, but must be reported as manual rather than proof of automatic delivery.
3. observe `dispatched`, `started`, `proxy_connected` and `worktree_ready` in native Modal logs. confirm the source revision, selected provider/model and fetched PR head in the disposable checkout.
4. inspect omp's diff, repository checks and behavioral smoke evidence. confirm its commit is the PR's remote head and the process terminated. `update_confirmed` checks head equality, not whether the diff meets the business requirement. kas confirms that separately.
5. exercise the core business-intent rule through this same path. missing real required consultation is a dependency blocker. a business-intent conflict requires the owner's explicit override. technical disagreement must lead to an improved implementation, not another owner-approval request.

repeat the same path after an update or provider switch, and with a later fresh job to prove credentials survive without another login. no workstation tunnel or old runner may be required. `python -m unittest test_runner` covers review intake, identity and relationship boundaries, review state, prompt selection, and docs follow-up merge safety without external calls.

docs updates skip merged source PRs whose changed files are all inside the configured documentation folders. an empty file list also skips the agent. code-only and mixed source changes continue to the docs agent, which may edit only those configured folders. `python -m unittest test_runner.DocsMergeTests` covers this routing, including folder-name lookalikes such as `docs-extra/` and `src/docs/` that aren't inside a configured `docs` folder.

docs follow-up squash merges send the validated `final_head` as GitHub's `sha` precondition. if the head changes before the merge request, GitHub rejects the mismatch instead of merging an unvalidated commit. after a failed or lost merge response, fallback confirmation requires `merged_at`, `merge_commit_sha` and a PR head SHA matching `final_head`. the runner keeps its existing fallback reads and final merge-confirmation check without retrying the merge against a new head. `python -m unittest test_runner.DocsMergeTests` covers a changed head, a concurrent merge of a different head, a successful merge, a lost successful response, and missing merge confirmation.

A docs follow-up can also run an optional per-repository `postprocess` from `docs_update.repositories`, for example `{"command": ["node", ".railway/worker-release.mjs", "record"], "files": [".railway/worker-releases.json"]}`. After validating the agent's docs-only commit, the runner invokes the checked-out `node .railway/worker-release.mjs record` directly (no shell) with its existing GitHub installation token, JSON stdin `{repo, source_sha, base_sha}`, and `OMP_POSTPROCESS_DEADLINE` as epoch seconds. Only `.railway/worker-releases.json` may be added or changed by this hook; symlinks, path traversal, rename sources outside the allowlist, failure, or other changes block publication. A valid manifest change is committed with the existing bot identity into the same docs follow-up and covered by the same head-SHA squash-merge precondition. A configured hook may make no change, including when the docs agent made none; repositories without a hook retain their prior docs-only and nonempty-change rules. The postprocessor has no Railway or Temporal credentials and cannot change the docs agent's edit permissions. If evidence is missing or conflicting, inspect the failed job rather than bypassing its output guard or publishing an unverified manifest.

the hook receives only `PATH`, `HOME`, `GH_TOKEN`, `CI`, `GH_PROMPT_DISABLED`, `GIT_TERMINAL_PROMPT` and `OMP_POSTPROCESS_DEADLINE`. it does not inherit the worker's proxy key or Jarvis consultation settings.

## logs, stopping and duplicates

```sh
modal app logs omp-runner
```

use the app URL printed by deployment for native function-call status. omp's final output distinguishes published, rejected, blocked, uncertain and already-handled outcomes. before a normal exit, it updates its existing queued statuses and posts one concise PR outcome, except when an earlier runner job already published and reported every finding's fix. that case updates the queued statuses to link the verified earlier outcome without another PR comment, commit or push. verification requires the bot author, exact finding coverage, a published commit reachable from the current PR head, and current code that still contains the fix. whole-review outcomes also require verified membership of the inline finding in that review. queued acknowledgments, unrelated fixes and similar wording are not proof of an earlier fix.

comment delivery is not blindly retried. omp reconciles an uncertain response by reading the PR and reports any remaining uncertainty in its final output. a zero exit without a remote update is not a successful fix. crashes, timeouts and failures before omp starts can still appear only in Modal logs; there is no separate failure-comment service.







deactivate the `autokas` App webhook to stop new intake everywhere, then allow existing work to finish. uninstalling `autokas` from a repository stops new deliveries for that repository only. neither cancels already queued calls from an older deployment. use Modal's native call cancellation when an active job must be stopped. a canceled or uncertain push must be reconciled against the PR before taking another action.

Modal Dict atomically claims each incoming repository/comment/prompt fingerprint, each canonical review before routing, and each execution before starting. identical deliveries and metadata-only edits are suppressed. aggregate and inline events share the review routing claim. changed finding prompts produce a new review fingerprint. each `@autokas` command comment has its own key, so an explicitly authorized follow-up is a new comment, not an edited one. reconcile the previous outcome before issuing another command. entries expire after seven days without activity, so this is bounded duplicate protection rather than permanent exactly-once semantics. claims remain after failed or uncertain dispatch and publication, and must not be deleted to trigger a blind retry.

command retries use a separate `command:<job key>` record. new command starts store `command_started_v2` in `started:<job key>` before creating that record. if preemption interrupts those writes, redelivery can recreate a missing `preparing` record only for that marker. legacy starts with missing records stay reporting-only, and existing command records aren't overwritten. a `preparing` record means omp hasn't launched and preparation can run again. immediately before launch, the runner records the starting head and publication branch, so a later interruption can't silently execute the same command twice.

command commits carry `Autokas-Command: <sha256 of job key>` as a Git trailer. a retry checks up to 1,000 reachable commits on the original publication branch for that exact trailer from the configured bot, or for a commit whose publication the runner already confirmed. a changed PR head or queued acknowledgment alone isn't publication evidence. issue commands use `autokas/issue-<number>` for this check.

confirmed publications and confirmed no-change completions resume reporting only, without checking out code or executing the command again. an uncertain publication also starts a reporting-only run that explains the missing evidence. missing legacy execution records, changed branches, unavailable GitHub reads and missing commit receipts never authorize command replay. review and docs reconciliation remain separate.

`python -m unittest test_runner.CommandInitializationTests -v` reaches the `PRWorker.run` launch boundary with a disposable local Git remote. it checks that pre-launch redelivery uses the original command prompt and the expected checked-out head and branch. legacy starts without execution records, uncertain executions and completed no-change commands use the reporting-only prompt in an empty worktree, without replacing their execution records.

references: [Modal deployment](https://modal.com/docs/guide/apps), [secrets](https://modal.com/docs/sdk/py/latest/Secret), [Dict](https://modal.com/docs/sdk/py/latest/Dict), [GitHub fine-grained tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

### autokas GitHub App setup

create the `autokas` GitHub App under the `kastheco` organization with contents, workflows, pull requests and issues read/write permissions. install it only on approved repositories, then add `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY` to the `omp-runner-worker` Modal secret. the worker resolves each repository’s installation with the app JWT before minting a short-lived installation token. cached tokens are isolated by repository and refreshed before expiry. a missing installation fails without falling back to another organization or a personal GitHub PAT. the agent receives the token selected for its job repository. public bot identity lookups need no installation credential.

Run `./setup_autokas.py` from the repository root to create the `autokas` App through GitHub, discover its `kastheco` installation, optionally write the Modal secret, and deploy the cutover. The wizard asks before account creation and deployment.
