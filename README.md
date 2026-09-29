<p align="center">
  <img src="docs/assets/autokas-readme.svg" alt="autokas. review fixes, docs management, fully automated in the cloud. powered by omp and modal.com." width="860">
</p>

<p align="center">
  <a href="runner.py">runner</a> ·
  <a href="config.json">config</a> ·
  <a href="consult.py">advisor</a> ·
  <a href="deploy.py">deploy</a> ·
  <a href="docs/operations.md">operations</a> ·
  <a href="kas-voice-profile.md">voice profile</a> ·
  <a href="skills">skills</a>
</p>

autokas fixes CodeRabbit review findings without anyone sitting at a keyboard. a signed GitHub webhook lands on Modal, a configured omp agent checks out the pull request in a disposable container, runs the repo's checks, commits, pushes to the PR branch and exits.

the runner dispatches the job, not the agent's working process. omp owns investigation, edits, checks and publication. there is no controller, review service, scheduler, database or recovery loop. scope is set in the project ticket.

## how a job runs

1. GitHub sends `issue_comment`, `pull_request_review_comment` or `pull_request_review` to the Modal webhook. unsigned requests get `401`.
2. the receiver accepts only completed CodeRabbit reviews or comments that carry the fenced agent prompt, on a PR whose head belongs to the approved base repository.
3. a Modal Dict claims the review and finding fingerprints, so duplicate deliveries don't start a second job.
4. one `PRWorker` per repo and PR clones the repo, checks out the PR head, verifies its SHA and starts omp with the job's model profile.
5. omp fixes what's still valid, commits as `autokas[bot]` in Conventional Commits form, pushes, updates its queued status comments and posts one outcome comment.
6. the container exits.

## advisors

a repo can be paired with an advisor: an external service omp consults before it changes core business rules or intended behavior. the advisor answers business-intent questions only. it isn't a technical, safety or security gatekeeper, and omp still judges those against the code.

`consult.py` is the client. it sends a bounded question, keeps the full answer in a private temp file for omp to read, and fails closed on any interrupted or empty response. an advisor's bearer and endpoint go only to jobs for repos it's paired with.

today the one advisor is Jarvis, paired with `example-org` repos. other owners get technical fixes with no advisor, and business-logic changes there are reported instead of made. the full policy is in [docs/operations.md](docs/operations.md).

autokas also opens follow-up docs PRs after merges, limited to each repo's configured documentation folders.

## where it runs

autokas is a GitHub App owned by `kastheco`. the repositories it's installed on are the only boundary, and the runner keeps no second allowlist. mint-per-repo installation tokens keep one owner's credentials away from another's.

the installed set is managed in the App's GitHub installation settings.

the Modal app is `omp-runner` in workspace `example-workspace`, environment `main`. the repo was renamed to `autokas` on GitHub, but app, function and package names stay as they are so live hooks and secrets keep working. models come from the Railway CLIProxyAPI service through omp's native `models.yml`.

## set up

```sh
uv venv
uv pip install -r requirements.txt
source .venv/bin/activate
modal token new
modal profile list
modal environment list
```

kas does the Modal login and consent. then create two secrets in the approved environment, entering values through Modal's own form or SDK, never through arguments, chat or tracked files:

| secret | values | scope |
| -- | -- | -- |
| `omp-runner-webhook` | `GITHUB_WEBHOOK_SECRET` | receiver only |
| `omp-runner-worker` | `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `CLI_PROXY_API_KEY`, `JARVIS_RUNNER_TOKEN` | worker only |

`./setup_autokas.py` creates the GitHub App, finds its `kastheco` installation and can write the worker secret and deploy. it asks before each of those steps.

model logins live in the Railway proxy volume, not Modal:

```sh
npm run login:codex
npm run login:claude
```

## deploy

pushes to `main` run `.github/workflows/deploy.yml`, which runs the tests and then `deploy.py`. it pauses the repo hooks, drains running work, redeploys with `modal deploy --strategy recreate runner.py`, checks that an unsigned request returns `401` and a signed ping returns `200` with the expected source revision, then restores the hooks and replays anything missed. rollback is a revert on `main`.

## test

```sh
python -m unittest discover -v
```

covers intake and identity boundaries, review state and prompt selection, installation token isolation, clean-review acknowledgments, deploy reconciliation and docs follow-up merge safety, all without external calls.

## stop it

disable a repo's webhook to stop new intake and let running work finish. uninstalling the App from a repo also stops new deliveries but doesn't cancel calls already queued. cancel an active job with Modal's native call cancellation, then reconcile the PR before doing anything else.

## repository map

```text
runner.py             webhook receiver, dispatcher, PR worker and docs worker
config.json           models, tools, profiles, timeouts and git identity
consult.py            bounded client for advisor consultations
deploy.py             hook pause, drain, deploy, verify, restore and replay
setup_autokas.py      one-time GitHub App and Modal setup wizard
kas-voice-profile.md  voice rules appended to every job's system policy
skills/               bundled skills, with pinned sources and licenses
test_*.py             unit tests for intake, auth, dispatch, deploy and consult
docs/                 readme banner and long-form operations notes
```

for the advisor rules, provider accounts, live verification path and duplicate-claim limits, read [`docs/operations.md`](docs/operations.md).
