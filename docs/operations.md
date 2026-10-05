# autokas operations

long-form operating notes for the runner, moved out of the readme unchanged. the Modal app, function names and package name stay `omp-runner` after the GitHub repo rename to `autokas`, so live URLs, secrets and logs keep working. start at the [readme](../README.md).

CodeRabbit or Cursor Bugbot comment → signed Modal webhook → configured omp in the event's PR worktree → checks, commit, ordinary push → exit.

scope is deliberately narrow. `runner.py` dispatches the job, not the agent's working process. omp owns investigation, edits, checks and publication. there is no controller, scheduler, database, recovery loop or archive. the only review autokas writes itself is one PR-Agent `/review` comment per ready PR, or per `@autokas review` command, sent through the same proxy. there is no other review logic.

## current state

the runner is deployed as a Modal app, `omp-runner`, in the environment you choose. its endpoint rejects unsigned requests. CodeRabbit and Cursor Bugbot intake is available for every repository where the `autokas` GitHub App is installed. repository installation scope is the only repository boundary.

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

the deploy workflow builds production `config.json` from the tracked `config.example.json` and takes only the private keys from the `AUTOKAS_CONFIG_JSON` secret: `docs_update`, `jarvis_owner`, `jarvis_url`, `omp_models` and `owner_approvals`. versions, tools, top-level model selections, `omp_settings` and every other public setting change through a normal PR to `config.example.json`, never through the secret. `omp_models` is supplied by `AUTOKAS_CONFIG_JSON` and can override the tracked value. other keys in the secret are ignored.

- coding jobs use `railway-codex/gpt-6.1-sol`, high reasoning and requested priority service. docs jobs use `railway-codex/gpt-6-luna`, medium reasoning and default service. both use CLIProxyAPI's Responses API. native omp flags set the model, reasoning effort and service tier explicitly. all model roles follow the selected job profile. model fallback and automatic agent retries are disabled.
- PR-Agent reviews use `pr_review` in `config.example.json`: `enabled` (`true`, set it to `false` to stop automatic reviews and `@autokas review`), the pinned `pr_agent_version`, `model` (`railway-codex/gpt-6-astra`), `thinking` (`medium`) and `max_model_tokens` (`200000`). `max_model_tokens` limits one review's prompt. a bigger diff is pruned to fit and the pruned parts aren't reviewed. it isn't a monthly budget. the `pr_review` Modal function runs in its own small Python image with git (0.5 CPU, 1 GiB, seven-minute timeout, no retries) and never starts a coding container. an automatic review is pinned to the head in its job, and a command to the head it started on. the function looks up the merge base with GitHub's compare API and fetches it and the head at depth 1, with the installation token passed only to git through its environment, then runs PR-Agent's plain-diff mode (`--diff-file`, `--output`) inside the checkout so it has full file context. it removes the checkout's root `pyproject.toml` first, because PR-Agent tries to read settings from it. PR-Agent's environment holds only `CLI_PROXY_API_KEY`, the provider's `baseUrl` and the model settings, with no GitHub token and nothing from the model list in `omp_models`. it sets no fallback model, no temperature (litellm rejects it while reasoning is on), `propagate_tool_errors` so failures exit non-zero, and no repository or global `.pr_agent.toml`. the runner re-fetches the PR after the review and posts the output as one `autokas[bot]` comment ending in `reviewed head <sha>` only if the PR is open and still on that head. otherwise it logs `review_outdated` and posts nothing. an empty diff logs `pr_review_empty`. Jarvis settings are never passed in.
- PR-Agent fixes: every review asks PR-Agent to start each finding's header with `[P0]` to `[P3]`, and PR-Agent's `--json-output` gives the runner the findings. the posted comment carries an `<!-- autokas:pr-agent {...} -->` marker with the reviewed head, the round and the findings. `pr_agent` in `config.example.json` is the `autokas[bot]` login and ID. after posting, the review function builds an issue-comment finding job from its own comment (reviewer `pr_agent`), claims its key and dispatches it. that's the only PR-Agent fix intake. coding jobs comment as `autokas[bot]` too, so webhook and deploy-reconcile deliveries of `autokas[bot]` comments never start a fix, even when they carry the marker. the job carries findings at or above `pr_review.fix_severity` (`P2`; `null` turns fixes off) while the round is at most `pr_review.max_fix_rounds` (`3`). it then follows the existing finding path: queued status, PR-scoped `PRWorker`, revalidation of the comment's author and findings before omp starts, and an untrusted-finding prompt. when the fix's commit is confirmed at or beneath the current PR head, the worker queues a `pr_review` job of kind `fix` pinned to that current head with the next round. a parent restack between the fix push and confirmation does not suppress that review. it's treated like an automatic review: drafts, ignored PRs and generated docs PRs are skipped. a review with nothing at or above the threshold logs `pr_review_no_fix`.
- omp: `18.4.10`, Bun: `1.4.2`. the image includes Node 22, Corepack, git, gh and build tools. target dependencies are installed by omp using the repository's own instructions.
- runner commits use `autokas[bot] <334744567+autokas[bot]@users.noreply.github.com>` as both author and committer. the numeric ID and login match the GitHub bot account, so GitHub can attribute commits independently of the push credential. every commit must follow Conventional Commits 1.0.0, such as `fix(auth): preserve the session on refresh`, even if repository examples use another format. GitHub API calls, comments, review replies, thread resolution and pushes use the `autokas` GitHub App installation token. the App is owned by `kastheco`; its private key, App ID and installation ID stay in the Modal `omp-runner-worker` secret. the required repository permissions are contents read/write, workflows read/write, pull requests read/write and issues read/write.
- tools: read, bash, edit, write, grep, glob, lsp and todo. extension discovery is disabled. normal repository instructions remain available.
- `kas-voice-profile.md` is a byte-identical copy of kas's canonical local profile and is appended to every job's system policy. voice matching never depends on an optional skill invocation.
- `skills/` contains the local unslop skill and all 38 Matt Pocock skills, including supporting files and licenses. `skills/SOURCES.json` records pinned upstream revisions and file hashes. the image carries these assets, and every fresh agent home links them into native omp skill discovery. matching skills are read on demand; they cannot expand the job's authority or override its safety policy. specialized skill workflows may still require their own repository tools.
- the durable `worker` entry queue dispatches to a native Modal `PRWorker` instance. reviews and commands keep their lowercase repo/PR key, so different PRs run concurrently. docs updates share a lowercase repo/base-branch key, preserving branch case, so separate source PRs targeting the same base wait in the existing Modal queue. each key has at most one single-use container and one active input. each agent has 2 CPU, 8 GiB, a one-hour timeout, a fresh temporary home and checkout. Modal shuts down its container after that job. queue order is not guaranteed.
- generated docs PRs include `@coderabbitai ignore` in their initial description, so CodeRabbit skips automatic review. their body marker and `docs-update/` branch prefix exclude automatic findings and merged-PR intake. the worker also excludes older queued automatic findings using fetched PR metadata. explicit `@autokas` commands still run on generated docs PRs after the dispatcher's existing write-access check; ordinary PR reviews remain enabled.
- the dispatcher maps a completed review and its inline comment deliveries to one review job, regardless of arrival order. it fetches and paginates that exact review's inline findings, then atomically claims the review and finding fingerprints before starting a coding worker. each targeted inline finding gets one queued reply. when inline targets exist, there is no additional PR-level queued message. review-only and conversation findings retain a linked conversation acknowledgment. confirmed comment receipts go to the agent so it can edit those exact statuses. uncertain writes are reconciled against the configured bot account, never blindly repeated.
- completed clean reviews from CodeRabbit receive a linked “reviewed and okay” comment from `autokas[bot]`, attributing the result to CodeRabbit and identifying the reviewed commit. the dispatcher re-fetches the completed review and current PR head before posting. incomplete, stale, spoofed and generated-docs reviews are not acknowledged as clean. one clean acknowledgment is claimed per repository, PR and head within the existing duplicate-retention window. no coding agent starts for this path. Cursor Bugbot has no clean-review comment to acknowledge: it reports a clean run only through its `Cursor Bugbot` check run, which autokas doesn't consume, so a clean Bugbot run produces no acknowledgment.
- after confirming its commit on the remote PR branch, the agent updates its existing inline queued comments with the outcome, commit link and relevant validation limits. it does not add separate completion replies. it then posts one overall PR outcome and resolves only the exact targeted threads whose findings were fixed and whose status updates were confirmed. rejected, blocked, unfixed and uncertain-push findings get status updates but stay unresolved. conversation comments and review-only findings have no thread to resolve.
- stacked PRs: before launching omp, the runner walks open same-repository PRs whose base is the job's branch, then their children, and passes that bottom-up list to the agent as trusted `upstack` context. after a confirmed fix push, the agent merges each parent into the branch above it (`chore(stack): merge <parent> into <branch>`) and pushes each one with an ordinary non-force push, so the stack stays linear without anyone running `Rebase stack`. it never rebases, force-pushes or runs `gh stack submit`, `sync`, `rebase` or `push`, since those rewrite branches and can drop other people's commits. a moved remote head or a conflict it can't resolve faithfully stops the restack at that branch, and the overall outcome names it. fork heads, issue commands and reporting-only command retries get no upstack. the worker reads the local PR branch itself, not the checkout's final `HEAD`, which the restack moves. it confirms publication on the original repository and branch when that commit matches the remote head or GitHub's compare API proves the remote head is a descendant. unrelated heads and failed ancestry lookups do not confirm publication. command receipts retain the exact fix commit, while PR-Agent follow-up reviews use the current head containing it.
- each job fetches `refs/pull/N/head`, checks out the PR branch and verifies its head SHA. worktrunk is unnecessary for a disposable single-job clone.
- external-fork PR heads are not enabled. the head must belong to the approved base repository, including when that approved repository is itself a fork. all positive PR numbers are eligible; only genuine CodeRabbit findings with the fenced agent prompt, Cursor Bugbot inline comments with a `BUGBOT_BUG_ID` marker and a described finding, or PR-Agent review comments posted by the review function itself with findings at or above `fix_severity`, start work. Bugbot's review summary body and its “Fix in Cursor” links are never passed to the agent: the job prompt is rebuilt from the finding's title, severity, locations and description. a PR-Agent fix prompt is likewise rebuilt from the comment's marker, not its rendered markdown.
- a new human GitHub comment that starts with `@autokas` queues one command job. only users with `admin` or `write` repository permission may trigger it. a PR command runs on that PR's head branch. an issue command starts from the default branch, creates `autokas/issue-<number>`, pushes it and opens one pull request containing `Closes #<number>`. review-comment commands acknowledge the thread root because GitHub rejects replies to replies. edited comments and duplicate deliveries do not run. commands bypass the PR-body ignore markers and the generated-docs exclusion, so they also run on ignored and generated docs PRs. the README's [`@autokas` commands](../README.md#autokas-commands) section is the user-facing summary.
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

repository Actions must be enabled and all four deployment secrets below must be configured. disabling Actions or removing those secrets prevents automatic deployment, even when a PR merges successfully. local deployment is a recovery path, not the normal release path.

pushes to `main` run `.github/workflows/deploy.yml`. a manual dispatch on `main` uses the same serialized workflow. it runs the tests, then `deploy.py` performs the complete cutover:

1. persist the reconcile window (its start, the source revision and reconciled repositories) before anything else.
2. wait for the receiver, dispatcher and aggregate PR worker pools to have no running inputs or backlog across three consecutive checks. a drain timeout prevents deployment. App webhooks can't be paused through GitHub's API, so intake stays open during the cutover.
3. deploy with `modal deploy --strategy recreate runner.py`, reject an unsigned request with `401`, and redeliver the newest signed App delivery from inside Modal, requiring a `200` or `202` with the expected source revision. the App private key never leaves Modal.
4. list every repository the App is installed on (through the `installed_repos` Modal function), then reconstruct eligible comments, reviews and merged-PR events changed since the window began. existing atomic claims suppress duplicate jobs, so deliveries that landed during the cutover aren't run twice.

the workflow uses the `DEPLOY_GH_TOKEN`, `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` and `AUTOKAS_CONFIG_JSON` repository secrets. `AUTOKAS_CONFIG_JSON` supplies only `docs_update`, `jarvis_owner`, `jarvis_url`, `omp_models` and `owner_approvals`. the workflow tests against `config.example.json`, merges those keys into it, then writes the resulting config to `config.json` (mode 0600) before deploying, and refuses to deploy if the secret is missing, invalid or still uses the example webhook. deployment and reconcile output goes to private runner files and only exit statuses are printed, because that output names your repositories. worker logs stay in Modal. `DEPLOY_GH_TOKEN` needs read access to the issues, pull requests and reviews of every installed repository. worker credentials remain in Modal. workflow runs do not cancel an active deployment. do not run a separate local deployment concurrently.

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
2. obtain a fresh real CodeRabbit comment or submitted review containing a fenced “Prompt for AI Agents” or “Prompt to fix review comments”, or a real Cursor Bugbot inline review comment (author `cursor[bot]`). automatic-trigger verification requires a real GitHub delivery. a user-authorized manual replay may fetch an existing review from GitHub and submit that unchanged review through the signed receiver, but must be reported as manual rather than proof of automatic delivery.
3. observe `dispatched`, `started`, `proxy_connected` and `worktree_ready` in native Modal logs. confirm the source revision, selected provider/model and fetched PR head in the disposable checkout.
4. inspect omp's diff, repository checks and behavioral smoke evidence. confirm its commit is the PR's remote head or an ancestor of it and the process terminated. `update_confirmed` checks publication and a successful agent exit, not whether the diff meets the business requirement. kas confirms that separately.
5. exercise the core business-intent rule through this same path. missing real required consultation is a dependency blocker. a business-intent conflict requires the owner's explicit override. technical disagreement must lead to an improved implementation, not another owner-approval request.

repeat the same path after an update or provider switch, and with a later fresh job to prove credentials survive without another login. no workstation tunnel or old runner may be required. `python -m unittest test_runner` covers review intake, identity and relationship boundaries, review state, prompt selection, and docs follow-up merge safety without external calls.

for PR-Agent reviews, after a deploy:

1. open a small same-repo PR as ready, or move a draft to ready, in a repository with the App installed.
2. observe `dispatched`, `routed`, `proxy_connected` with `pr_review.model`, `pr_review_started` and `pr_review_done` with the reviewed head, round and finding severities in Modal logs, and exactly one `autokas[bot]` "PR Reviewer Guide" comment on the PR ending in `reviewed head <sha>`. a failure logs `pr_review_failed` with redacted output.
3. push another commit and confirm no new review appears.
4. comment `@autokas review` and confirm a fresh review for the current head. comment `@autokas review focus on error handling` and confirm the review follows that request. a user without `write` access gets `command_unauthorized` and no comment.
5. on a review with a finding at or above `fix_severity`, confirm one queued status linking the review, a fix run, and after its commit a round 2 review of the new head. confirm the loop stops at a review with nothing at or above the threshold or after `max_fix_rounds` fix runs.

the runner reads the final PR-Agent state marker, so marker-like text in the rendered review can't override the appended state. fix jobs retain that marker's reviewed head. if the PR head moves while a fix is queued, the coding worker logs `review_outdated` and stops before cloning or launching omp. the next review round still uses the confirmed post-fix head.

the review comment stays within GitHub's 65,536-character limit, counted in UTF-8 bytes so it never undercounts. the size budget includes the reviewed-head footer, the full state marker and the trim notice. oversized rendered markdown is trimmed and says so. when the marker exceeds half the limit, finding content is shortened first, then headers. every finding's severity, file path and line range stay intact. if that metadata leaves no room for the trim notice, the review fails before posting or dispatching a fix instead of dropping findings or changing their locations.

`python -m unittest test_pr_review` covers trigger selection, skip rules, the command split, bot-loop safety, routing, access checks, head pinning before and after the review, the checkout's token and `pyproject.toml` handling, the PR-Agent environment, redaction, severity thresholds, untagged findings, the comment marker against hostile finding text, the comment size limit, the account check on review comments, delivered comments never starting a fix, and the round cap without external calls.

`python -m unittest test_pr_review.FixPublicationTests -v` exercises the worker with a disposable local Git remote. a child pushes its fix, then a separate parent checkout merges and pushes before the child worker confirms publication. the next review uses the merged head. the tests also reject unpublished fixes, unrelated advances, changed head identities and failed ancestry lookups. this does not exercise live Modal scheduling or provider calls.

docs updates skip merged source PRs whose changed files are all inside the configured documentation folders. an empty file list also skips the agent. code-only and mixed source changes continue to the docs agent, which may edit only those configured folders. `python -m unittest test_runner.DocsMergeTests` covers this routing, including folder-name lookalikes such as `docs-extra/` and `src/docs/` that aren't inside a configured `docs` folder.

docs publication fetches the latest configured base after the agent, after the hook, and immediately before merge. if it moved, the runner starts fresh from that base and asks the configured docs agent to reconcile the source change with current documentation, then regenerates the hook outputs from the latest baseline. this is bounded to two reconciliations within the original deadline. an already-published follow-up is updated only after checking its exact owned head and scope, using an explicit head-SHA force-with-lease on that docs branch. if the refreshed delta is empty, the runner confirms closure of its exact superseded PR instead of leaving it conflicting.

after replacing an already-published follow-up head, the runner waits only while GitHub still reports the exact prior SHA. polling sleeps for up to 15 seconds in total and stays within the original worker deadline. any different head ends the wait, and the existing scope check must confirm the new exact head before reading the file list or merging. first-time publication is unchanged.

each attempt validates the actual base/head file delta against the docs folders and declared hook outputs, checks the GitHub PR file list and re-fetches its exact head before sending `final_head` as the squash merge's `sha` precondition. a failed merge is reconciled by reading the PR first: a confirmed merge of that exact head is not repeated; a still-open unchanged head is regenerated only when the base really moved. a changed head, unclear publication, or continually moving base stops rather than being overwritten. GitHub's merge API cannot atomically pin the base SHA, so another writer can still move it after the final fetch; same-base autokas docs jobs are serialized, and late rejected conflicts take this bounded reconciliation path.

`python -m unittest test_runner.DocsMergeTests` exercises a real local git documentation conflict and add/add manifest conflict, movement during the agent/hook/publication, preserved newer release state, an already-covered source, bounded movement, and exact-head merge safety. this local proof does not exercise live Modal scheduling or provider behavior.

a docs follow-up can also run an optional per-repository `postprocess` from `docs_update.repositories`, for example `{"command": ["node", ".railway/worker-release.mjs", "record"], "files": [".railway/worker-releases.json"]}`. after validating the agent's docs-only commit, the runner invokes the checked-out `node .railway/worker-release.mjs record` directly (no shell) with its existing GitHub installation token, JSON stdin `{repo, source_sha, base_sha}`, and `OMP_POSTPROCESS_DEADLINE` as epoch seconds. only `.railway/worker-releases.json` may be added or changed by this hook; symlinks, path traversal, rename sources outside the allowlist, failure, or other changes block publication. a valid manifest change is committed with the existing bot identity into the same docs follow-up and covered by the same head-SHA squash-merge precondition. either the docs agent or configured hook may make no change; an empty final delta skips publication. the postprocessor has no Railway or Temporal credentials and cannot change the docs agent's edit permissions. if evidence is missing or conflicting, inspect the failed job rather than bypassing its output guard or publishing an unverified manifest.

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
