# omp runner

one cloud runner for a configured omp agent. CodeRabbit posts an agent prompt, Modal runs omp in the PR worktree, omp checks its work, commits and pushes to that PR, and the job exits. material business-logic changes follow the user's jarvis/kimmy consultation policy.

## implementation scope

the project ticket is the implementation plan and scope boundary. this repository is prepared for implementation; no runner has been implemented or deployed yet.

keep the existing Railway CLIProxyAPI service for provider logins. use native provider login commands and one Modal deployment command for updates. verify through the real CodeRabbit-to-PR path.

this is a fresh implementation, not a port of example-project. the old repository, uncommitted work, and Railway services remain untouched. carry over only explicitly selected non-secret omp configuration, skills, and thin provider-login commands. do not import the old controller, OpenHands runtime, or Railway deployment plan.
