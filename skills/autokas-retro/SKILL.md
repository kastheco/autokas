---
name: autokas-retro
description: "Review repeated autokas recovery and setup work, then file deduplicated kasAI issues."
disable-model-invocation: true
---

# autokas retro

run on demand from an agent with `gh` access to `kastheco` and Linear access to the kashub workspace. this is an external manual pass, not an end-of-job step inside autokas. its output is root-cause issues, not fixes or runner instrumentation.

## 1. collect the sources

read `writing-for-agents` for the writing guide. use the requested runs or date window. if none is given, use the last seven days and report the exact UTC bounds.

- use `gh search prs --owner kastheco --updated '>=YYYY-MM-DD' --limit 100 --sort updated --order desc` to discover recent PRs. split the date window if the result limit is reached. fetch each PR's conversation with `gh api --paginate repos/kastheco/REPO/issues/NUMBER/comments`. verify the autokas bot author, then read its outcome comments, including reporting-only and blocked outcomes. filter comment timestamps separately: a recently updated PR can contain old runs.
- read the activities autokas posted on its Linear sessions in the same window, including setup, tool errors, retries and final responses. use a session-activity reader provided by the installed connection. issue comments aren't session activities. if that reader isn't available, record the exact access gap rather than treating empty issue comments as an empty session history.
- use linked Modal coding-run logs only when reachable with existing access. record unavailable tooling or logs. don't install tooling or request new credentials just to expand this optional source.

save source URLs, timestamps, the window and fetch limits in the task's evidence. record which sources were read and which couldn't be reached. if evidence is sparse, continue with supported findings and report the limit. collection is complete when every discovered source in the stated window is read or has a recorded fetch/access gap.

## 2. reconcile runs and count causes

make one evidence row per distinct run and cause: repository, coding-call URL or Linear session/run identity, timestamp, source links, observed step/error, workaround, result and proposed patch target.

use the **Modal coding run**, not its dispatcher, as the PR run identity. merge repeated comments for that call before extracting evidence, since later comments can add setup details. reconcile a linked Linear session against its coding call so the same run isn't counted twice. keep records without a reliable identity as uncounted evidence.

file a candidate only when **at least two distinct runs demonstrate the same cause**. count runs, not comments, regex hits or retries within one run. keep within-run retry counts separately only when the source states them. group missing dependencies by the shared setup path when one fix addresses them, and show subgroup counts and overlap.

separate recovery from successful recurring setup and label each accurately. distinguish environment setup failures, harness mistakes and application bugs reproduced for a fix. a mention of `modal`, `config.example.json` or auth isn't a failure by itself. unrelated smoke-harness mistakes don't establish one cause.

for each group, name the narrow patch target: environment image, setup script, repo config, prompt or skill. mark inferred causes as inference, not observed fact. rank supported groups by blocked work and recurrence. counting is complete when each candidate has reviewed source evidence, distinct-run counts and a workaround, and rejected or single-run candidates are recorded with their reason.

## 3. classify improvements, checks before rules

keep matt pocock's categories from the vendored `skills/retro`:

- **navigation:** slow file discovery or hidden dependencies may need a navigation pointer.
- **automated checks:** inspect the repository's existing check commands and CI first. an existing check that's unwired or broken is the finding. a repository without a hook or CI guardrail is also a candidate, subject to the repeat threshold above.
- **coding standards:** mechanical mistakes call for a deterministic check in the existing linter, hook or CI, not a new prose rule. reserve reviewer standards for judgement calls. the reviewer has the diff and should own those standards rather than burdening implementation context.
- **global AGENTS.md:** identify steering better placed in checks or reviewer standards. keep always-loaded navigation pointers small.
- **tool economy:** identify repeated expensive calls or setup work that a narrower tool or prepared environment could remove.
- **no-ops:** identify steering instructions that don't change observed behavior.
- **information access:** identify crucial missing information and the narrow read-only access needed to reach it.

these are candidate patch directions, not permission to implement them during the retro. classification is complete when every supported group has a category and the smallest root-cause patch direction, with existing checks accounted for.

## 4. dedupe and file in kasAI

resolve the **kasAI** team in the **kashub** Linear workspace through the installed connection. search that team for each cause's error, component and workaround. paginate results and read plausible open matches, including backlog, todo, in-progress and review states. don't dedupe by title alone or against a different repository's similar symptom.

if an open issue covers the same cause and patch target, add only new run evidence to it and link it in the result. otherwise create one issue per root cause in kasAI. don't assign it to the app's `me` user or start a fix run.

each issue includes:

- the observed cause or explicitly labelled inference, category and patch target.
- the source window, affected repositories, distinct-run count, run identities and evidence links.
- the workaround and result, including unrepaired failures and recurring setup that succeeded.
- the smallest acceptance check for the proposed fix, dedupe searches and any source limitations.

filing is complete when every supported top group links to a created or updated issue, or a specific filing failure is recorded.

## 5. report the pass

post the group/count table and filed issue links on the invoking task. include excluded candidates, source gaps and the threshold. describe the sample as observed outcomes, not all autokas jobs or a fleet failure rate. stop after filing and reporting. fixes belong to those issues.

## first-pass calibration

[the first pass](https://linear.app/kashub/issue/KAS-961) used October 1–10, 2026 PR outcomes: 38 discovered PRs, 116 fetched autokas comments, 112 within the window and 51 distinct coding-call URLs. repeated comments were reconciled by call URL.

- [repository setup](https://linear.app/kashub/issue/KAS-962): 23 runs reported missing Python setup. 22 explicitly named missing `modal`, 3 named missing `uv`, and 2 were in both groups. pinned requirements and public example config repaired most checks. these were one setup-path finding, not separate install tickets.
- [Git interception](https://linear.app/kashub/issue/KAS-963): 2 distinct runs intercepted Git as an agent launch, then restored real Git subprocesses. other harness errors weren't merged into this cause.

Linear issue search and filing worked, but the connection exposed no session-activity reader. Modal logs weren't read because the CLI was unavailable. public config mentions, unrelated harness repairs and isolated tool failures didn't justify additional repeat issues. this limited pass established the two-run threshold, not a claim of complete run-log coverage.
