<p align="center">
  <img src="docs/assets/autokas-readme.svg" alt="autokas. reviews every PR, fixes the findings, keeps the docs current. powered by omp and modal.com." width="860">
</p>

<p align="center">
  <a href="runner.py">runner</a> ·
  <a href="config.example.json">config</a> ·
  <a href="consult.py">advisor</a> ·
  <a href="deploy.py">deploy</a> ·
  <a href="docs/operations.md">operations</a> ·
  <a href="kas-voice-profile.md">voice profile</a> ·
  <a href="skills">skills</a>
</p>

autokas is a GitHub App that reviews pull requests, fixes review findings and keeps docs current without anyone sitting at a keyboard. a signed GitHub webhook lands on Modal, and each job runs in a disposable container: PR-Agent for reviews, a configured omp agent for anything that changes code. the container exits when the job is done.

the runner dispatches the job, not the agent's working process. omp owns investigation, edits, checks and publication. there is no controller, scheduler, database or recovery loop.

## what it does

- **reviews every PR.** a PR-Agent review posts as `autokas[bot]` when a PR is ready and again on each push, and shows as an `autokas review` check next to CI. findings are tagged `[P0]` to `[P3]`. see [reviews](#reviews).
- **fixes review findings.** findings from CodeRabbit, Cursor Bugbot, Cursor Security Reviewer and autokas's own reviews go to an omp job that fixes what's still valid, runs the repo's checks and pushes to the PR branch. its own findings get up to three fix rounds, each re-reviewed. an `autokas:*` label shows the latest outcome. see [fixes](#fixes).
- **takes instructions.** start a comment with `@autokas` on a PR or issue and it does the work, opening a PR for issues. see [commands](#autokas-commands).
- **takes linear issues.** delegate an issue to the linear app, steer the live run from its session, and get progress and the resulting PR there. some repositories require plan approval before coding. see [linear intake](docs/operations.md#linear-intake).
- **keeps docs current.** after a merge in a configured repo, it opens, validates and merges a follow-up docs PR. see [docs follow-ups](#docs-follow-ups).
- **leaves stacks linear.** fixes never merge into or rewrite upstack branches, and a review after a restack spends no fix round.
- **checks business intent.** a paired advisor answers intent questions before omp changes intended behavior. see [advisors](#advisors).

## reviews

autokas posts one PR-Agent `/review` comment as `autokas[bot]` on a same-repo PR when it's opened as ready, when it moves from draft to ready, and on each later push (`pull_request.synchronize`). a push whose sender matches the configured autokas bot login and ID is skipped because the fix path already reviews that head. webhook redeliveries and fix re-reviews share the same repo/PR/head claim, so a head is reviewed once. drafts, closed PRs, fork heads, generated docs PRs and PRs marked `autokas:ignore` are skipped. setting `pr_review.enabled` to `false` turns off both the automatic reviews and `@autokas review`.

the review runs in its own small Modal function with the pinned `pr_review.pr_agent_version`, not in the omp coding container. autokas checks out the exact queued head and hands PR-Agent the diff from the merge base for ready and command reviews, with that checkout for file context, so PR-Agent never gets a GitHub token. it calls `pr_review.model` (or `pr_review.restack_model` for restacks) through the same CLIProxyAPI service with no fallback model. autokas posts the result as one comment, and only if the PR is still on the reviewed head. a push during the review means nothing is posted. it never pushes, commits, labels or resolves threads. PR-Agent doesn't see the PR title, description or commit messages, and repository `.pr_agent.toml` files are ignored.

ordinary pushes scope the diff to the latest completed bot-authored review whose head is a verified ancestor of the new head, with that review's findings and the full new checkout. if the intermediate review was cancelled, the next review covers every push since that completed review instead of trusting the event's unreviewed before head. if no completed reviewed ancestor is available, it reviews the full merge-base diff with the normal model and consumes a round. if the event's before head is not an ancestor of after, the push is a restack: review the full merge-base diff using `pr_review.restack_model` (default `railway-codex/gpt-6-luna`). a restack marker records `restack: true` and the unchanged counted round, so the restack itself spends no fix budget. a fix started from it uses the next round as usual; its follow-up review counts too.

each finding in a review is tagged `[P0]` to `[P3]`: P0 is a security hole, data loss or an outage, P1 a bug in normal use, P2 a bug under specific inputs or conditions, and P3 maintainability, style or a speculative concern. an untagged finding counts as P2. when a review has findings at or above `pr_review.fix_severity` (default `P2`), autokas queues one fix job for them through the same path as CodeRabbit and Bugbot findings, so a review with fixable findings gets the review comment, then one fix outcome comment. if that fix pushes a commit, autokas reviews only the diff from the prior reviewed head to the new head, with the full new checkout for context. the prior findings go into extra instructions: confirm whether each was fixed, keep every unresolved prior finding in `key_issues_to_review` with its severity and current location, and otherwise report only problems introduced by the diff, not unrelated pre-existing findings. fixed prior findings stay out of that structured list. the narrative summarizes both fixed and unresolved status. unresolved findings still inform the check and the next fix job, subject to the same severity threshold and round cap. if the prior head is no longer an ancestor after a force-push, or GitHub no longer has it, the review falls back to the merge-base diff. a review with nothing at or above the threshold, a fix that pushes nothing, a push from someone else during a round, or reaching `pr_review.max_fix_rounds` (default 3 review rounds eligible for fixes per PR) ends the loop. the last review is still posted. every ready, ordinary push, fix and command review continues from the highest counted round in that PR's existing bot-authored markers, ignoring restack-only markers; a manual `@autokas review` never resets the budget. set `fix_severity` to `null` to keep reviews and turn off the fixes. PRs marked `autokas:ignore` and generated docs PRs get no automatic fixes, even when `@autokas review` reviewed them.

when actionable findings arrive after `pr_review.max_fix_rounds` (default `3`), autokas adds a fix-limit note to that review comment stating that no fixer was queued. the failed review check offers **fix anyway**, a GitHub check action button. clicking it authorizes one fixer for that review's findings on the same head, without resetting the automatic cap. the note also includes `@autokas fix review <review-comment-id>` as a comment-command fallback. the check action requires a human with write or admin access. the comment fallback accepts those users or an exact `trusted_reviewers` bot. both paths reject stale heads and share the same finding claim so they cannot launch duplicate fixers.

the review comment is built from PR-Agent's structured output, not its markdown: a heading with the round, finding count and reviewed commit, one line per finding with its severity and a link to its lines, then the full finding text in a collapsed details section.

### the review check

each PR-Agent review also runs as an `autokas review` check on the head it reviewed, so it shows next to CI and goes stale on the next push like any other check. it fails when the review has findings at or above `pr_review.fix_severity`, the same ones that start a fix, and passes otherwise. its title counts the findings by severity and its details link to the review comment. a review that stops early ends as `skipped` (empty diff), `cancelled` (the head moved) or `neutral` (PR-Agent failed).

the GitHub App needs checks read/write permission, declared in `config.example.json`. installations must accept the updated permission before check runs can be written. a check API failure logs its HTTP status code without response bodies or tokens and does not stop the review.

the check's **re-run** button requests a fresh review of the PR's current head, not the old check commit. it requires write or admin access and keeps the PR's counted fix budget. redelivery of the same request starts no second review; closed PRs and checks without an associated PR are ignored. kas must subscribe the App to `check_run` before enabling this path; checks are already read/write. see [the cutover gap](docs/operations.md#deploy-and-update).

## fixes

a fix job starts from CodeRabbit or Cursor findings, or from a PR-Agent review's findings at or above the fix threshold described above. CodeRabbit and Cursor findings follow this path:

1. GitHub sends `issue_comment`, `pull_request_review_comment` or `pull_request_review` to the Modal webhook. unsigned requests get `401`.
2. the receiver accepts only completed CodeRabbit reviews or comments that carry the fenced agent prompt, and `cursor[bot]` inline comments that carry a Bugbot marked finding or a Security Reviewer finding, on a PR whose head belongs to the approved base repository. each bot is matched by its configured login and id from `config.example.json`, and a bot's comments are only parsed with that bot's own format.
3. a Modal Dict claims the review and finding fingerprints, so duplicate deliveries don't start a second job.
4. one `PRWorker` per repo and PR clones the repo, checks out the PR head, verifies its SHA and starts omp with the job's model profile.
5. omp fixes what's still valid, commits as `autokas[bot]` in Conventional Commits form, pushes and posts one outcome comment covering every finding. finding jobs post no "queued" comment: the `autokas:fixing` label shows the run and the outcome label shows its result.
6. the container exits.

every outcome comment uses one layout, so the result reads at a glance: a heading with a status icon (✅ fixed, 🚫 rejected, ⛔ blocked, ❓ uncertain, ↩️ already fixed) and the commit, one line per finding, a **needs you** line when the owner has to decide something, then checks and reasoning folded into collapsed sections.

Cursor's Bugbot and Security Reviewer both post as `cursor[bot]`, so its inline comments are read in either format. a Security Reviewer finding has a `CURSOR_AUTOMATION_ID` marker and an "Agentic Security Review" heading, and its prompt keeps only the severity and description. review summaries and issue comments from either don't start finding jobs, even when they contain marked finding text. a Cursor review batch uses only its collected inline findings as the agent prompt. CodeRabbit keeps its review-prompt-first behavior, falling back to joined inline prompts when the review has none.

for stacked PRs, a confirmed fix push ends branch publication. the overall outcome names the upstack PRs and branches that need a restack by their owners. autokas leaves those branches unchanged, without merge commits or history rewrites.

### labels

fix jobs for CodeRabbit, Bugbot and PR-Agent findings set one label for the latest job's outcome, replacing any earlier one: `autokas:fixing` while omp runs, then `autokas:fixed` (a fix was pushed, or the host verified an earlier outcome against its publishing-job receipt, the exact findings and an unchanged published fixed tree), `autokas:rejected` (the findings didn't warrant a change) or `autokas:blocked` (omp failed, got blocked, couldn't confirm publication or earlier handling, or reported a missing or unrecognized outcome even after a confirmed push). earlier outcomes without a host receipt, including pre-receipt outcomes and expired receipts, can't confirm `already handled`. the label follows the latest job, not the head, so read it next to the check. `@autokas` commands and docs jobs don't set labels.

that's enough for gh-dash sections, for example:

```yaml
prSections:
  - title: autokas blocked
    filters: is:open author:@me label:autokas:blocked
  - title: autokas fixing
    filters: is:open author:@me label:autokas:fixing
  - title: ready
    filters: is:open author:@me -label:autokas:fixing -label:autokas:blocked status:success
```

## @autokas commands

you don't have to wait for CodeRabbit. start a GitHub comment with `@autokas` or `!kas` and an instruction, and autokas runs it as its own job. both prefixes are case-insensitive aliases with the same access checks and behavior, including `!kas review` and `!kas fix review <review-comment-id>`.

- **on a PR**, in the conversation or on a review thread, it works on that PR's head branch and pushes there.
- **on an issue**, it branches from the default branch as `autokas/issue-<number>`, does the work and opens one PR containing `Closes #<number>`.
- only users with `write` or `admin` access on the repo can trigger it. other people's comments are ignored.
- the only bot exception is `trusted_reviewers`: both login and user ID must match. the example allows `kasthecrew[bot]` to send `@autokas review` and `@autokas fix review <review-comment-id>` on PRs, nothing else. it cannot send arbitrary coding or issue commands. `fix review` can authorize one fixer for the named exhausted review; the check action stays human-only.
- each comment runs once. editing a comment doesn't rerun it, so post a new comment for a follow-up.
- commands still run on PRs marked `autokas:ignore` and on generated docs PRs.
- a PR command that starts with the word `review` posts a fresh PR-Agent review of the current head instead of starting a coding job, and its findings start the fix loop above. anything after `review` goes to PR-Agent as extra instructions, so `@autokas review focus on the auth changes` steers it. it works on drafts too. on an issue, `review` is an ordinary command.
- review requests post no queued acknowledgment and do not reset `max_fix_rounds`. qualifying findings can queue a fixer only within the existing budget.

```text
@autokas the date filter drops the last day of the range, fix it and add a test
```

## advisors

a repo can be paired with an advisor: an external service omp consults before it changes core business rules or intended behavior. the advisor answers business-intent questions only. it isn't a technical, safety or security gatekeeper, and omp still judges those against the code.

`consult.py` is the client. it sends a bounded question, keeps the full answer in a private temp file for omp to read, and fails closed on any interrupted or empty response. an advisor's bearer and endpoint go only to jobs for repos it's paired with.

the advisor is paired to one repository owner through `jarvis_owner` in the config. other owners get technical fixes with no advisor, and business-logic changes there are reported instead of made. the full policy is in [docs/operations.md](docs/operations.md).

## docs follow-ups

autokas also opens follow-up docs PRs after merges. docs jobs targeting the same repo/base branch wait in the existing Modal worker pool, while reviews keep per-PR concurrency. publication refreshes the base and reconciles stale docs and declared postprocess outputs before an exact-head merge. explicit write-authorized commands can still repair generated docs PRs. see [operations](docs/operations.md) for the bounded recovery and verification limits.

## where it runs

autokas is a GitHub App owned by `kastheco`. a repository must have the App installed and its owner must appear in `allowed_owners` before any review, fix, command or docs job can run. owner matching is exact and case-insensitive. an empty or missing list denies all owners. mint-per-repo installation tokens keep one owner's credentials away from another's.

the installed set is managed in the App's GitHub installation settings. events arrive through one App-level webhook pointed at the receiver and subscribed to `check_run`, `issue_comment`, `pull_request`, `pull_request_review` and `pull_request_review_comment`, so installing the App on a repo is all it takes to start delivery. existing Apps need kas to add the `check_run` subscription for re-runs and check actions. no per-repo hooks are needed.

installation alone does not authorize spending the deployment's model quota or Modal balance. set `allowed_owners` in private `config.json`; the Actions deployment reads the JSON list from the `AUTOKAS_ALLOWED_OWNERS_JSON` repository secret. the public example denies all owners. queued jobs recheck the list before routing or execution.

the Modal app is `omp-runner`. the repo was renamed to `autokas` on GitHub, but app, function and package names stay as they are so the App webhook and secrets keep working. models come from the Railway CLIProxyAPI service (its URL is `omp_models` in your config) through omp's native `models.yml`.

## set up

```sh
uv venv
uv pip install -r requirements.txt
source .venv/bin/activate
modal token new
modal profile list
modal environment list
```

do the Modal login and consent yourself. then copy the example config and edit it:

```sh
cp config.example.json config.json
```

`config.json` holds your webhook URL, provider URL and advisor pairing and stays untracked. then create two secrets in the approved environment, entering values through Modal's own form or SDK, never through arguments, chat or tracked files:

| secret | values | scope |
| -- | -- | -- |
| `omp-runner-webhook` | `GITHUB_WEBHOOK_SECRET` | receiver only |
| `omp-runner-worker` | `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `CLI_PROXY_API_KEY`, `JARVIS_RUNNER_TOKEN` | worker only |

`./setup_autokas.py` creates the GitHub App with its webhook inactive, finds its `kastheco` installation and can write the worker secret and deploy. it asks before each of those steps. set the App webhook secret to the value in `omp-runner-webhook`, then activate the webhook in the App settings.

model logins live in the Railway proxy volume, not Modal. export `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID` and `RAILWAY_SERVICE_ID` first, the scripts refuse to run without them:

```sh
npm run login:codex
npm run login:claude
```

## pinned job images

`images/base/Dockerfile` builds the public linux-x64 base from the node, bun and omp pins in `config.example.json`. `.github/workflows/images.yml` builds and smoke-checks clean tracked sources on pushes and PRs. pushes to `main` and authorized manual dispatches publish the smoke-tested image and record its registry digest, versions, source hashes and smoke output. branch pushes and PRs never publish. the first package publication needs its owner to set the base package public in GitHub package settings. the release job checks anonymous digest access before accepting public release evidence.

coding workers pull one private team image by digest. private `config.json` supplies `teams`, with each entry containing `image`, `modal_environment`, `pull_secret` and `worker_secret`. exactly one entry must match `MODAL_ENVIRONMENT`, which defaults to `main`. the example entry is only a shape for tests and must be replaced in `AUTOKAS_CONFIG_JSON` before deployment. the private-config overlay includes `teams` and leaves the existing validation and 0600 host file mode unchanged.

the approved Modal environment needs an `autokas-ghcr-pull` secret with `REGISTRY_USERNAME` and `REGISTRY_PASSWORD`, using a credential with read-only access to the private package. registry credentials authenticate the image pull and are not passed to the coding process. existing worker secret names stay unchanged. configure the actual private digest and pull secret before merging the runner cutover, since pushes to `main` deploy automatically.

team images supply `/opt/autokas/settings.json` and `/opt/autokas/skills`. workers read those baked sources before adding their per-job model roles. deployed clones use `/workspace/repo` and temporary job state uses `/state`. private settings and skill sources are not overlaid from this public checkout. the host agent-container switch is separate from this change.

## deploy

pushes to `main` run `.github/workflows/deploy.yml`, which runs the tests against `config.example.json`, materializes your real config from the `AUTOKAS_CONFIG_JSON` Actions secret, and then runs `deploy.py`. its output is written to private runner files, so public logs show only exit statuses. it saves the reconcile window, drains running work, redeploys with `modal deploy --strategy recreate runner.py`, checks that an unsigned request returns `401` and that a redelivered App event returns `200` or `202` with the expected source revision, then reconstructs eligible comments, reviews and merged-PR events on every installed repo. App webhooks can't be paused through the API, so reconciliation recovers those eligible event types during the cutover; actual PR-ready and push deliveries require App redelivery. rollback is a revert on `main`.

repository reconciliation does not reconstruct `opened`, `ready_for_review` or `synchronize` deliveries from current PR metadata. it cannot recover a push's before/after heads or sender. replay of an actual App delivery retains that payload and follows normal intake and head deduplication.

the [public base image workflow](.github/workflows/images.yml) keeps a separate build artifact for each run attempt. publication downloads the artifact ID from the successful build job, so rerunning all jobs doesn't overwrite earlier evidence and rerunning only publication still uses the smoke-tested build.

## test

```sh
bash scripts/setup-test-env.sh
.venv/bin/python -m unittest discover -v
```

the setup script needs Python 3 with venv support. it installs test dependencies without `uv` or credentials and leaves an existing `config.json` untouched. rerunning it skips installation when `requirements.txt` hasn't changed.

covers intake and identity boundaries, review state and prompt selection, PR-Agent reviews, checks and the fix loop, installation token isolation, clean-review acknowledgments, deploy reconciliation and docs follow-up merge safety, all without external calls.

`test_pr_review` also covers the empty-diff check lifecycle: the review completes its check as `skipped` without running PR-Agent, posting a review or queuing a fix.

## stop it

add `autokas:ignore` or `@autokas ignore` anywhere in a PR body to skip automatic finding fixes, clean-review acknowledgments and docs-update jobs when it merges. markers are case-insensitive. [`@autokas` commands](#autokas-commands) from users with write access still run.

deactivate the App webhook to stop new intake everywhere and let running work finish, or uninstall the App from one repo to stop that repo's deliveries. neither cancels calls already queued. cancel an active job with Modal's native call cancellation, then reconcile the PR before doing anything else.

## repository map

```text
runner.py             webhook receiver, dispatcher, PR worker and docs worker
config.example.json   example models, tools, profiles, timeouts and git identity
consult.py            bounded client for advisor consultations
deploy.py             drain, deploy, verify and reconcile
setup_autokas.py      one-time GitHub App and Modal setup wizard
kas-voice-profile.md  voice rules appended to every job's system policy
skills/               bundled skills, with pinned sources and licenses
test_*.py             unit tests for intake, auth, dispatch, deploy and consult
docs/                 readme banner and long-form operations notes
```

for the advisor rules, provider accounts, live verification path and duplicate-claim limits, read [`docs/operations.md`](docs/operations.md).

## license

autokas is licensed under the [Apache License 2.0](LICENSE). bundled skills under `skills/` keep their own licenses, listed in `skills/SOURCES.json`.
