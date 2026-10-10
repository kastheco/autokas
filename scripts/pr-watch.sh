#!/usr/bin/env bash
# Runs inside an issue-scoped Paperclip process run. Never writes to GitHub.
set -euo pipefail
umask 077

: "${PAPERCLIP_API_URL:?}"
: "${PAPERCLIP_API_KEY:?injected run token required}"
: "${PAPERCLIP_COMPANY_ID:?}"
: "${PAPERCLIP_RUN_ID:?}"
if [[ -z ${PAPERCLIP_TASK_ID:-} ]]; then
  echo 'pr-watch: skipped, an issue-scoped run is required' >&2
  exit 0
fi
api=${PAPERCLIP_API_URL%/}
api=${api%/api}/api
state=${PR_WATCH_STATE_FILE:-/paperclip/pr-watch/state.json}
target=${PR_WATCH_ISSUE_ID:-b123e711-14ac-4a4a-9b4a-024d31ea8f5f}
gh=${PR_WATCH_GH:-/usr/local/bin/gh}
request() {
  curl --fail-with-body --silent --show-error \
    -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
    -H "X-Paperclip-Run-Id: $PAPERCLIP_RUN_ID" \
    -H 'Content-Type: application/json' "$@"
}
mkdir -p "$(dirname "$state")"
# One writer, including manually triggered runs. Lock the stable path, not the
# state inode, since successful snapshots replace that inode atomically.
exec 9>"$state.lock"
if ! flock -n 9; then
  request -X PATCH --data '{"status":"done"}' "$api/issues/$PAPERCLIP_TASK_ID" >/dev/null
  exit 0
fi
work=$(mktemp -d "${PAPERCLIP_RUN_SCRATCH_DIR:-${TMPDIR:-/tmp}}/pr-watch.XXXXXX")
trap 'rm -rf "$work"' EXIT
if [[ -f $state ]]; then
  jq -e '(.version == 1) and (.prs | type == "object")' "$state" >/dev/null
  cp "$state" "$work/old.json"
else
  printf '{"version":1,"prs":{}}\n' >"$work/old.json"
fi
request "$api/companies/$PAPERCLIP_COMPANY_ID/projects" >"$work/projects.json"
# This agent is bound to kastheco. Never probe the other companies on the board.
jq -r '[.[] | .workspaces[]? | .repoUrl // empty |
  capture("^https://github.com/(?<repo>kastheco/[^/]+?)(?:\\.git)?/?$").repo] | unique[]' \
  "$work/projects.json" >"$work/repos"
printf '{}\n' >"$work/current.json"
printf '[]\n' >"$work/readable.json"
while IFS= read -r repo; do
  # Pagination avoids a limit-dependent false closure. Inaccessible repositories
  # keep their previous state until we can read a complete snapshot again.
  if ! "$gh" api --paginate --slurp "repos/$repo/pulls?state=open&per_page=100" >"$work/pulls.json"; then
    echo "pr-watch: skipping unreadable repository $repo" >&2
    continue
  fi
  printf '{}\n' >"$work/repo.json"
  complete=true
  while IFS= read -r number; do
    if ! "$gh" pr view "$number" --repo "$repo" \
      --json url,headRefOid,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,labels >"$work/pr.json" \
      || ! "$gh" api --paginate --slurp "repos/$repo/issues/$number/comments?per_page=100" >"$work/comments.json" \
      || ! "$gh" api --paginate --slurp "repos/$repo/pulls/$number/reviews?per_page=100" >"$work/reviews.json" \
      || ! "$gh" api --paginate --slurp "repos/$repo/pulls/$number/comments?per_page=100" >"$work/inline.json"; then
      complete=false
      break
    fi
    jq --arg key "$repo#$number" --slurpfile pr "$work/pr.json" \
      --slurpfile comments "$work/comments.json" --slurpfile reviews "$work/reviews.json" \
      --slurpfile inline "$work/inline.json" '
      def counted: map(select(((.body // "") | startswith("@autokas")) | not)) | length;
      $pr[0] as $p |
      ($comments[0] | add // []) as $c |
      ($reviews[0] | add // []) as $r |
      ($inline[0] | add // []) as $i |
      .[$key] = {
        url: $p.url,
        fingerprint: {
          head: $p.headRefOid, mergeable: $p.mergeable, mergeState: $p.mergeStateStatus,
          review: $p.reviewDecision,
          checks: [$p.statusCheckRollup[]? | {name: (.name // .context),
            status, conclusion, state}] | sort_by(.name, .status, .conclusion, .state),
          labels: [$p.labels[]?.name] | sort,
          comments: ($c | counted), reviews: ($r | counted), inlineComments: ($i | counted)
        },
        reviewed: (any($r[]; .user.login == "autokas[bot]" and
          .commit_id == $p.headRefOid and .submitted_at != null) or
          any($c[]; .user.login == "autokas[bot]" and
            ((.body // "") | contains("<!-- autokas:pr-agent ")) and
            (try ((.body | capture("<!-- autokas:pr-agent (?<marker>[^\\n]+?) -->").marker |
              fromjson).head == $p.headRefOid) catch false)))
      }' "$work/repo.json" >"$work/next.json"
    mv "$work/next.json" "$work/repo.json"
  done < <(jq -r '.[][] | select(.draft == false) | .number' "$work/pulls.json")
  if [[ $complete == false ]]; then
    echo "pr-watch: skipping incomplete repository $repo" >&2
    continue
  fi
  jq --slurpfile repo "$work/repo.json" '. + $repo[0]' "$work/current.json" >"$work/next.json"
  mv "$work/next.json" "$work/current.json"
  jq --arg repo "$repo" '. + [$repo]' "$work/readable.json" >"$work/next.json"
  mv "$work/next.json" "$work/readable.json"
done <"$work/repos"

jq --argjson now "$(date +%s)" --slurpfile current "$work/current.json" \
  --slurpfile readable "$work/readable.json" '
  .prs as $old | $current[0] as $current |
  reduce ($current | to_entries[]) as $entry (
    {version: 1, prs: $old, events: []};
    $entry.key as $key | $entry.value as $pr | $old[$key] as $before |
    ($before == null or $before.fingerprint.head != $pr.fingerprint.head) as $newHead |
    ($pr.fingerprint.labels | index("autokas:fixing") != null) as $fixing |
    ($pr + {
      firstSeen: (if $newHead then $now else $before.firstSeen end),
      stallWakes: (if $newHead then 0 else $before.stallWakes end),
      fixingSince: (if $fixing then
        (if $newHead then $now else ($before.fixingSince // $now) end)
        else null end)
    }) as $tracked |
    (if $before == null then ["new PR"] else
      [ $pr.fingerprint | to_entries[] |
        select(.value != $before.fingerprint[.key]) | .key + " changed" ] end) as $changes |
    ([if $tracked.reviewed == false and $now - $tracked.firstSeen >= 1800 then
        "no autokas review for 30 minutes" else empty end,
      if $tracked.fixingSince != null and $now - $tracked.fixingSince >= 4500 then
        "autokas:fixing for 75 minutes" else empty end] |
      if $tracked.stallWakes < 2 then . else [] end) as $stalls |
    .prs[$key] = ($tracked | .stallWakes += (if $stalls | length > 0 then 1 else 0 end)) |
    if ($changes + $stalls | length) > 0 then
      .events += [{key: $key, url: $pr.url, reasons: ($changes + $stalls)}]
    else . end
  ) |
  reduce ($old | to_entries[]) as $entry (.;
    ($entry.key | split("#")[0]) as $repo |
    if ($readable[0] | index($repo)) != null and $current[$entry.key] == null then
      del(.prs[$entry.key]) |
      .events += [{key: $entry.key, url: $entry.value.url, reasons: ["closed or no longer ready"]}]
    else . end
  )' "$work/old.json" >"$work/result.json"
if jq -e '.events | length > 0' "$work/result.json" >/dev/null; then
  jq '{body: ("## pr watcher\n\n" + ([.events[] |
    "- [" + .key + "](" + .url + "): " + (.reasons | join(", "))] | join("\n")))}' \
    "$work/result.json" >"$work/comment.json"
  # Commit state only after Paperclip accepts the wake comment. A denied post
  # must not swallow a change or consume one of the two per-head stall wakes.
  request -X POST --data-binary "@$work/comment.json" \
    "$api/issues/$target/comments" >"$work/response.json"
  jq -e '.id != null' "$work/response.json" >/dev/null
  echo "pr-watch: posted $(jq '.events | length' "$work/result.json") PR updates"
else
  echo 'pr-watch: no PR changes or stalls'
fi
# Use the same filesystem as the state for atomic publication.
jq 'del(.events)' "$work/result.json" >"$state.next"
mv "$state.next" "$state"
request -X PATCH --data '{"status":"done"}' "$api/issues/$PAPERCLIP_TASK_ID" >/dev/null
