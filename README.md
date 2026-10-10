<p align="center">
  <img src="docs/assets/autokas-readme.svg" alt="autokas. reviews every PR, fixes the findings, keeps the docs current. powered by omp and modal.com." width="860">
</p>

<p align="center">
  <a href="runner.py">runner</a> ·
  <a href="config.example.json">config</a> ·
  <a href="images/base/Dockerfile">base image</a> ·
  <a href="consult.py">advisor</a> ·
  <a href="deploy.py">deploy</a> ·
  <a href="docs/operations.md">operations</a> ·
  <a href="kas-voice-profile.md">voice profile</a> ·
  <a href="skills">skills</a>
</p>

autokas is a GitHub App that reviews pull requests, fixes review findings and keeps docs current without anyone sitting at a keyboard. a signed GitHub or Linear webhook lands on Modal, and each job runs in a disposable container: PR-Agent for reviews, a configured omp agent for anything that changes code. coding jobs run in a private team image pinned by digest. the container exits when the job is done.

the runner dispatches the job, not the agent's working process. omp owns investigation, edits, checks and publication. there is no controller, scheduler, database or recovery loop.

## what it does

- **reviews every PR.** a PR-Agent review posts as `autokas[bot]` when a PR is ready and again on each push, and shows as an `autokas review` check next to CI. findings are tagged `[P0]` to `[P3]`. see [reviews](#reviews).
- **fixes review findings.** findings from CodeRabbit, Cursor Bugbot, Cursor Security Reviewer and autokas's own reviews go to an omp job that fixes what's still valid, runs the repo's checks and pushes to the PR branch. its own findings get up to three fix rounds, each re-reviewed. an `autokas:*` label shows the latest outcome. see [fixes](#fixes).
- **takes instructions.** start a comment with `@autokas` or `!kas` on a PR or issue and it does the work, opening a PR for issues. see [commands](#autokas-commands).
- **takes linear issues.** delegate an issue to the linear app, steer the live run from its session, and get progress and the resulting PR there. gated repositories need plan approval before coding. see [linear intake](#linear-intake).
- **keeps docs current.** after a merge in a configured repo, it opens, validates and merges a follow-up docs PR. see [docs follow-ups](#docs-follow-ups).
- **leaves stacks linear.** fixes never merge into or rewrite upstack branches, and a review after a restack spends no fix round.
- **checks business intent.** a paired advisor answers intent questions before omp changes intended behavior. see [advisors](#advisors).

## reviews

autokas posts one PR-Agent `/review` comment as `autokas[bot]` on a same-repo PR when it's opened as ready, when it moves from draft to ready, and on each later push (`pull_request.synchronize`). a push whose sender matches the configured autokas bot login and ID is skipped, since the fix path already reviews that head. webhook redeliveries and fix re-reviews share one repo/PR/head claim, so a head is reviewed once. drafts, closed PRs, fork heads, generated docs PRs and PRs marked `autokas:ignore` are skipped. setting `pr_review.enabled` to `false` turns off both the automatic reviews and `@autokas review`.

the review runs in its own small Modal function with the pinned `pr_review.pr_agent_version`, not in the coding image. autokas checks out the exact queued head and hands PR-Agent the diff from the merge base, with that checkout for file context, so PR-Agent never gets a GitHub token. it calls `pr_review.model` (default `railway-codex/gpt-6.1-sol` at the priority service tier) through the CLIProxyAPI service with no fallback model. autokas posts the result as one comment, and only if the PR is still on the reviewed head. a push during the review means nothing is posted. it never pushes, commits, labels or resolves threads. PR-Agent doesn't see the PR title, description or commit messages, and repository `.pr_agent.toml` files are ignored.

an ordinary push reviews only the diff since the latest completed bot-authored review whose head is a verified ancestor of the new head, with that review's findings and the full new checkout. if the intermediate review was cancelled, the next one covers every push since the last completed review. if no completed reviewed ancestor exists, it reviews the full merge-base diff and consumes a round. if the event's before head isn't an ancestor of after, the push is a restack: autokas reviews the full merge-base diff with `pr_review.restack_model` (default `railway-codex/claude-haiku-5-5`) and records `restack: true` with the unchanged round, so the restack spends no fix budget.

each finding is tagged `[P0]` to `[P3]`. P0 is a security hole, data loss or an outage, P1 a bug in normal use, P2 a bug under specific inputs or conditions, and P3 maintainability, style or a speculative concern. an untagged finding counts as P2. when a review has findings at or above `pr_review.fix_severity` (default `P2`), autokas queues one fix job for them through the same path as CodeRabbit and Bugbot findings. if the fix pushes a commit, autokas reviews only the diff from the prior reviewed head to the new head and asks PR-Agent to confirm each prior finding, keep unresolved ones with their current location, and otherwise report only problems the diff introduced. after a force-push that drops the prior head, the review falls back to the merge-base diff.

the loop ends when a review has nothing at or above the threshold, a fix pushes nothing, someone else pushes during a round, or the PR reaches `pr_review.max_fix_rounds` (default `3`). the last review is still posted. every review continues from the highest counted round in the PR's bot-authored markers, so a manual `@autokas review` never resets the budget. set `fix_severity` to `null` to keep reviews and turn off fixes. PRs marked `autokas:ignore` and generated docs PRs get no automatic fixes.

when actionable findings arrive after the round cap, the review comment says no fixer was queued, and the failed review check offers a **fix anyway** button. clicking it authorizes one fixer for that review's findings on the same head without resetting the cap. it requires a human with write or admin access. `@autokas fix review <review-comment-id>` is the comment fallback, which also accepts an exact `trusted_reviewers` bot. both paths reject stale heads and share one finding claim, so they can't launch duplicate fixers.

the review comment is built from PR-Agent's structured output, not its markdown: a heading with the round, finding count and reviewed commit, one line per finding with its severity and a link to its lines, then the full finding text in a collapsed section.

### the review check

each review also runs as an `autokas review` check on the head it reviewed, so it sits next to CI and goes stale on the next push. it fails when the review has findings at or above `pr_review.fix_severity`, the same ones that start a fix, and passes otherwise. its title counts findings by severity and its details link to the review comment. a review that stops early ends as `skipped` (empty diff), `cancelled` (the head moved) or `neutral` (PR-Agent failed). a check API failure logs its HTTP status, never response bodies or tokens, and doesn't stop the review.

the check's **re-run** button reviews the PR's current head, not the old check commit. it needs write or admin access and keeps the PR's counted fix budget. a redelivered request starts no second review. the App needs checks read/write permission and a `check_run` subscription for re-runs and **fix anyway**.

## fixes

a fix job starts from CodeRabbit or Cursor findings, or from an autokas review's findings at or above the fix threshold. external findings follow this path:

1. GitHub sends `issue_comment`, `pull_request_review_comment` or `pull_request_review` to the Modal webhook. unsigned requests get `401`.
2. the receiver accepts only completed CodeRabbit reviews or comments with the fenced agent prompt, and `cursor[bot]` inline comments with a Bugbot or Security Reviewer finding, on a PR whose head belongs to the base repository. each bot is matched by its configured login and ID, and its comments are parsed only in its own format.
3. a Modal Dict claims the review and finding fingerprints, so duplicate deliveries don't start a second job.
4. one `PRWorker` per repo and PR pulls the team image, clones the repo into a temporary job directory, checks out the PR head, verifies its SHA and starts omp with the job's model profile.
5. omp fixes what's still valid, commits as `autokas[bot]` in Conventional Commits form, pushes and posts one outcome comment covering every finding.
6. the container exits, and the job directory goes with it.

every outcome comment uses one layout: a heading with a status icon (✅ fixed, 🚫 rejected, ⛔ blocked, ❓ uncertain, ↩️ already fixed) and the commit, one line per finding, a **needs you** line when the owner has to decide something, then checks and reasoning in collapsed sections.

Cursor's Bugbot and Security Reviewer both post as `cursor[bot]`. a Security Reviewer finding carries a `CURSOR_AUTOMATION_ID` marker and an "Agentic Security Review" heading. review summaries and issue comments from either bot never start finding jobs. CodeRabbit uses its review prompt first and falls back to the joined inline prompts.

for stacked PRs, a confirmed fix push ends publication. the outcome names the upstack PRs that need a restack by their owners, and autokas leaves those branches alone.

### labels

fix jobs set one label for the latest job's outcome, replacing any earlier one:

| label | meaning |
| -- | -- |
| `autokas:fixing` | omp is running |
| `autokas:fixed` | a fix was pushed, or the host verified an earlier outcome against its publishing-job receipt |
| `autokas:rejected` | the findings didn't warrant a change |
| `autokas:blocked` | omp failed or got blocked, publication couldn't be confirmed, or the outcome was missing |

the label follows the latest job, not the head, so read it next to the check. `@autokas` commands and docs jobs don't set labels. that's enough for gh-dash sections:

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

start a GitHub comment with `@autokas` or `!kas` and an instruction, and autokas runs it as its own job. both prefixes are case-insensitive and behave the same.

- **on a PR**, in the conversation or on a review thread, it works on that PR's head branch and pushes there.
- **on an issue**, it branches from the default branch as `autokas/issue-<number>`, does the work and opens one PR containing `Closes #<number>`.
- only users with `write` or `admin` access on the repo can trigger it.
- the only bot exception is `trusted_reviewers`, matched by both login and user ID. the example lets `kasthecrew[bot]` send `@autokas review` and `@autokas fix review <review-comment-id>` on PRs, nothing else.
- each comment runs once. editing it doesn't rerun it, so post a new comment for a follow-up.
- commands still run on PRs marked `autokas:ignore` and on generated docs PRs.
- `review` on a PR posts a fresh review of the current head instead of starting a coding job, and its findings start the fix loop within the existing budget. anything after `review` goes to PR-Agent as extra instructions. it works on drafts too.

```text
@autokas the date filter drops the last day of the range, fix it and add a test
!kas review focus on the auth changes
```

## linear intake

delegating a linear issue to the app starts a coding job for it. `linear.repo_map` maps linear projects and teams to repositories, the run's progress and its PR show up in the agent session, and follow-ups sent during the run steer omp. repositories in `linear.gated_repos` investigate first, in a separate planner container with no secrets, and coding starts only after the plan is approved in linear. the GitHub installation and `allowed_owners` still apply. setup, OAuth tokens and the signed-call proofs are in [operations](docs/operations.md#linear-intake).

## advisors

a repo can be paired with an advisor: an external service omp consults before it changes core business rules or intended behavior. it answers business-intent questions only. it isn't a technical, safety or security gatekeeper, and omp still judges those against the code.

`consult.py` is the client. it sends a bounded question, keeps the full answer in a private temp file for omp to read, and fails closed on any interrupted or empty response. the advisor is paired to one repository owner through `jarvis_owner`, and its bearer and endpoint go only to jobs for that owner's repos. other owners get technical fixes with no advisor, and business-logic changes there are reported instead of made.

## docs follow-ups

after a merge in a configured repo, autokas opens a follow-up docs PR. docs jobs for the same repo and base branch wait in one Modal worker pool, while reviews keep per-PR concurrency. publication refreshes the base and reconciles stale docs and declared postprocess outputs before an exact-head merge. write-authorized commands can still repair generated docs PRs.

## job images

coding jobs never build their image at deploy time. they pull a pinned image from GHCR.

- **`ghcr.io/kastheco/autokas-base`** is public and team-neutral. `images/base/Dockerfile` builds it for linux-x64 from the `node_version`, `bun_version` and `omp_version` pins in `config.example.json`, with git, gh, Corepack and build tools. [`images.yml`](.github/workflows/images.yml) builds it from a clean checkout and smoke-checks it with no mounts, network or credentials. pushes to `main` and authorized manual dispatches publish the smoke-tested build and keep a release artifact with the registry digest, pins, source hashes and smoke output. PRs and branch pushes build and smoke-check only.
- **`ghcr.io/kastheco/autokas-teams/<team>`** is private. each team image starts from the base by digest and adds that team's `/opt/autokas/settings.json` and `/opt/autokas/skills`. it's built in the private `autokas-teams` repo.

private `config.json` maps each Modal environment to its team under `teams`: `image` (an `autokas-teams` digest), `modal_environment`, `pull_secret` and `worker_secret`. `MODAL_ENVIRONMENT` must be set before importing the runner, and exactly one entry must match it. a missing or empty value fails instead of defaulting to `main`. the example entry is a shape for tests only.

workers overlay `omp_settings` from the config onto the baked settings by top-level key, then add their per-job model roles. a configured object replaces the whole baked object, and anything absent keeps the team default. checkouts and agent homes live in a unique temporary job directory under `/state`, so they're removed when the job ends, including on failure. registry credentials authenticate the pull and never reach the coding process.

## where it runs

autokas is a GitHub App owned by `kastheco`. a repository needs the App installed and its owner in `allowed_owners` before any job runs. owner matching is exact and case-insensitive, and an empty or missing list denies every owner. per-owner installation tokens keep one owner's credentials away from another's. queued jobs recheck the list before running.

events arrive through one App-level webhook subscribed to `check_run`, `issue_comment`, `pull_request`, `pull_request_review` and `pull_request_review_comment`, so installing the App on a repo is all it takes. installation alone doesn't authorize spending model quota or Modal balance, which is what `allowed_owners` is for. the public example denies all owners.

the Modal app is `omp-runner`. the repo was renamed to `autokas`, but app, function and secret names stay as they were so the webhook and secrets keep working. models come from the Railway CLIProxyAPI service (`omp_models` in the config) through omp's native `models.yml`.

## set up

```sh
uv venv
uv pip install -r requirements.txt
source .venv/bin/activate
modal token new
cp config.example.json config.json
```

do the Modal login and consent yourself. `config.json` holds your webhook URL, provider URL, team images and advisor pairing, and stays untracked. create these secrets in the approved Modal environment, entering values through Modal's own form or SDK, never through arguments, chat or tracked files:

| secret | values | used by |
| -- | -- | -- |
| `omp-runner-webhook` | `GITHUB_WEBHOOK_SECRET` | receiver |
| `omp-runner-worker` | `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `CLI_PROXY_API_KEY`, `JARVIS_RUNNER_TOKEN` | workers |
| `omp-runner-linear` | `LINEAR_WEBHOOK_SECRET`, `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET`, `LINEAR_OAUTH_TOKENS` | linear intake |
| `autokas-ghcr-pull` | `REGISTRY_USERNAME`, `REGISTRY_PASSWORD` (read-only package access) | team image pulls |

`./setup_autokas.py` creates the GitHub App with its webhook inactive, finds its `kastheco` installation and can write the worker secret and deploy, asking before each step. set the App webhook secret to the value in `omp-runner-webhook`, then activate the webhook.

model logins live in the Railway proxy volume, not Modal. export `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID` and `RAILWAY_SERVICE_ID` first:

```sh
npm run login:codex
npm run login:claude
```

## deploy

pushes to `main` run [`deploy.yml`](.github/workflows/deploy.yml). it runs the tests against `config.example.json`, builds the real config from the `AUTOKAS_CONFIG_JSON` Actions secret, sets `MODAL_ENVIRONMENT=main` and runs `deploy.py`. its output goes to private runner files, so public logs show only exit statuses. `deploy.py` saves the reconcile window, drains running work, redeploys with `modal deploy --strategy recreate runner.py`, checks that an unsigned request gets `401` and a redelivered App event gets `200` or `202` with the expected revision, then reconstructs eligible comments, reviews and merged-PR events on every installed repo.

reconciliation can't rebuild `opened`, `ready_for_review`, `synchronize` or check re-run deliveries from PR metadata, so those need App redelivery if they land during a cutover. rollback is a revert on `main`.

## test

```sh
bash scripts/setup-test-env.sh
export MODAL_ENVIRONMENT=main
.venv/bin/python -m unittest discover -v
```

the setup script needs Python 3 with venv support. it installs test dependencies without `uv` or credentials, leaves an existing `config.json` alone, and skips installation when `requirements.txt` hasn't changed. the suite covers intake and identity boundaries, reviews, checks and the fix loop, team image selection, installation token isolation, linear intake, deploy reconciliation and docs follow-up merge safety, all without external calls. `smoke_helper.GitOmpSmoke` runs real git and captures the omp launch for smoke tests.

## stop it

add `autokas:ignore` or `@autokas ignore` anywhere in a PR body to skip automatic fixes, clean-review acknowledgments and docs jobs when it merges. markers are case-insensitive. [`@autokas` commands](#autokas-commands) from users with write access still run.

deactivate the App webhook to stop new intake everywhere and let running work finish, or uninstall the App from one repo to stop its deliveries. neither cancels calls already queued. cancel an active job with Modal's native call cancellation, then reconcile the PR before doing anything else.

## repository map

```text
runner.py                webhook receiver, dispatcher, PR worker and docs worker
linear_intake.py         linear webhook, agent sessions and plan approval
consult.py               bounded client for advisor consultations
deploy.py                drain, deploy, verify and reconcile
setup_autokas.py         one-time GitHub App and Modal setup wizard
config.example.json      example models, tools, teams, timeouts and git identity
images/base/             public base image and its baked settings
images/smoke.py          image smoke check run by images.yml
.github/workflows/       deploy and public base image workflows
scripts/                 test setup and the paperclip PR watcher
smoke_helper.py          real-git smoke fixture that captures omp launches
kas-voice-profile.md     voice rules appended to every job's system policy
skills/                  bundled skills, with pinned sources and licenses
test_*.py                unit tests
docs/                    readme banner, operations notes and the linear intake spec
```

for configuration, provider accounts, the live verification path, the paperclip PR watcher and duplicate-claim limits, read [`docs/operations.md`](docs/operations.md).

## license

autokas is licensed under the [Apache License 2.0](LICENSE). bundled skills under `skills/` keep their own licenses, listed in `skills/SOURCES.json`.
