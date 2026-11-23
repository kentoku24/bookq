#!/usr/bin/env bash
#
# discovery-collect.sh — deterministic signal collection for the
# improvement-scan workflow (.github/workflows/improvement-scan.yml).
#
# Read-only: gh api / gh issue list / gh pr list / git grep only.
# Writes a markdown signal pack to the output path ($1, default signals.md).
#
# Env:
#   GITHUB_REPOSITORY  owner/repo (falls back to `gh repo view`)
#   GH_TOKEN           gh auth (inherited from the workflow)
#   SCAN_DAYS          workflow-run lookback window in days (default 14)
#
set -euo pipefail

REPO="${GITHUB_REPOSITORY:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
OUT="${1:-signals.md}"
DAYS="${SCAN_DAYS:-14}"

# GNU date on runners, BSD date on macOS.
if SINCE=$(date -u -d "${DAYS} days ago" +%Y-%m-%d 2>/dev/null); then
  :
else
  SINCE=$(date -u -v"-${DAYS}"d +%Y-%m-%d)
fi
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

TMPDIR_SCAN=$(mktemp -d)
trap 'rm -rf "$TMPDIR_SCAN"' EXIT
FAILURES_FILE="$TMPDIR_SCAN/failures.tsv"
: > "$FAILURES_FILE"

{
  echo "# improvement-scan signals"
  echo
  echo "- generated: $NOW"
  echo "- repo: $REPO"
  echo "- run window: last ${DAYS} days (workflow runs, completed only)"
  echo "- note: per-workflow run lists are capped at the 100 most recent completed runs"
  echo

  # ------------------------------------------------------------------
  # 1. Automation inventory — what already exists (do not re-propose)
  # ------------------------------------------------------------------
  echo "## 1. Existing automation inventory"
  echo
  echo "| workflow | name | schedule |"
  echo "|---|---|---|"
  for f in .github/workflows/*.yml .github/workflows/*.yaml; do
    [ -e "$f" ] || continue
    wname=$(grep -m1 '^name:' "$f" | sed -E 's/^name:[[:space:]]*//; s/^"(.*)"$//' || true)
    cron=$( { grep -oE 'cron:[[:space:]]*"[^"]+"' "$f" || true; } \
      | sed -E 's/.*"([^"]+)".*/\1/' | paste -sd';' -)
    echo "| \`$f\` | $wname | ${cron:-—} |"
  done
  echo

  # ------------------------------------------------------------------
  # 2. Per-workflow run stats (last N days, completed runs)
  # ------------------------------------------------------------------
  echo "## 2. Workflow run stats (last ${DAYS}d, completed)"
  echo
  echo "| workflow | runs | ok | fail | cancel | median wall min* |"
  echo "|---|---:|---:|---:|---:|---:|"

  gh api "repos/$REPO/actions/workflows?per_page=100" \
    --jq '.workflows[] | select(.state == "active" and (.path | startswith(".github/workflows/"))) | [.id, .name, .path] | @tsv' \
  | while IFS=$'\t' read -r wf_id wf_name wf_path; do
      resp=$(gh api "repos/$REPO/actions/workflows/${wf_id}/runs" \
        --method GET -f "created=>=${SINCE}" -f status=completed -f per_page=100 \
        || echo '{"workflow_runs":[],"total_count":0}')
      total_count=$(printf '%s' "$resp" | jq '.total_count // 0')
      runs_json=$(printf '%s' "$resp" | jq '.workflow_runs // []')
      stats=$(printf '%s' "$runs_json" | jq -r '
        [ .[] | { c: .conclusion,
                  d: ((.updated_at | fromdateiso8601) - (.run_started_at | fromdateiso8601)) } ] as $r |
        ($r | [ .[].d ] | sort | .[ (length / 2 | floor) ] // 0) as $m |
        "\($r | length)\t\([$r[] | select(.c == "success")] | length)\t\([$r[] | select(.c == "failure")] | length)\t\([$r[] | select(.c == "cancelled")] | length)\t\((($m / 60 * 10) | round) / 10)"')
      trunc=""
      if [ "$total_count" != "null" ] && [ "$total_count" -gt 100 ] 2>/dev/null; then
        trunc=" (truncated: $total_count total)"
      fi
      printf '| `%s` %s | %s |%s\n' "$wf_path" "$wf_name" "$stats" "$trunc"

      # Accumulate failures for section 3.
      printf '%s' "$runs_json" | jq -r --arg wf "$wf_name" '
        .[] | select(.conclusion == "failure") |
        [ $wf, (.id | tostring), .html_url, .created_at,
          (.head_branch // ""), ((.display_title // .name) | .[0:80]) ] | @tsv' \
        >> "$FAILURES_FILE"
    done
  echo
  echo "\\* wall minutes = updated_at - run_started_at, includes queue time; approximate."
  echo

  # ------------------------------------------------------------------
  # 3. Recent failed runs + failed job names (flake signal)
  # ------------------------------------------------------------------
  echo "## 3. Recent failed workflow runs (latest 15)"
  echo
  echo "| workflow | run | created | branch | title |"
  echo "|---|---|---|---|---|"
  sort -t$'\t' -k4 -r "$FAILURES_FILE" | head -15 \
    | while IFS=$'\t' read -r wf rid url created branch title; do
        printf '| %s | [%s](%s) | %s | %s | %s |\n' \
          "$wf" "$rid" "$url" "${created:0:10}" "$branch" "$title"
      done
  echo

  echo "### 3a. Failed job names (up to 5 newest failed runs)"
  echo
  sort -t$'\t' -k4 -r "$FAILURES_FILE" | head -5 \
    | while IFS=$'\t' read -r wf rid url created branch title; do
        jobs=$(gh api "repos/$REPO/actions/runs/${rid}/jobs?filter=latest&per_page=100" \
          --jq '[.jobs[] | select(.conclusion == "failure") | .name] | join(", ")' \
          2>/dev/null || echo "?")
        printf -- '- %s run %s (%s): %s\n' "$wf" "$rid" "${created:0:10}" "$jobs"
      done
  echo

  # ------------------------------------------------------------------
  # 4. Open issues inventory
  # ------------------------------------------------------------------
  echo "## 4. Open issues inventory"
  echo
  gh issue list --repo "$REPO" --state open --limit 200 \
    --json number,title,labels,createdAt,author > "$TMPDIR_SCAN/issues.json" \
    || echo '[]' > "$TMPDIR_SCAN/issues.json"
  jq -r '
    "- total open: \(length)",
    "- authored by bot accounts: \([.[] | select(.author.login | test("\\[bot\\]$|^app/"))] | length)",
    "",
    "### 4a. Label histogram (top 25)",
    "",
    "| label | count |", "|---|---:|",
    ([.[].labels[].name] | group_by(.) | map({key: .[0], value: length})
     | sort_by(-.value) | .[0:25][] | "| `\(.key)` | \(.value) |"),
    "",
    "### 4b. Newest 60 open issues (number | created | labels | title)",
    "",
    "| # | created | labels | title |", "|---:|---|---|---|",
    (.[0:60][] | "| \(.number) | \(.createdAt[0:10]) | \([.labels[].name] | join(",")) | \(.title[0:90]) |")
  ' "$TMPDIR_SCAN/issues.json"
  echo

  # ------------------------------------------------------------------
  # 5. Open PRs inventory
  # ------------------------------------------------------------------
  echo "## 5. Open PRs inventory"
  echo
  gh pr list --repo "$REPO" --state open --limit 100 \
    --json number,title,isDraft,createdAt,labels,author > "$TMPDIR_SCAN/prs.json" \
    || echo '[]' > "$TMPDIR_SCAN/prs.json"
  jq -r '
    "- total open: \(length)",
    "- drafts: \([.[] | select(.isDraft)] | length)",
    "- dependabot: \([.[] | select(.author.login | test("dependabot"))] | length)",
    "",
    "| # | created | draft | author | title |", "|---:|---|---|---|---|",
    (.[] | "| \(.number) | \(.createdAt[0:10]) | \(.isDraft) | \(.author.login) | \(.title[0:90]) |")
  ' "$TMPDIR_SCAN/prs.json"
  echo

  # ------------------------------------------------------------------
  # 6. Merged PR velocity (last N days)
  # ------------------------------------------------------------------
  echo "## 6. Merged PRs (last ${DAYS}d)"
  echo
  gh pr list --repo "$REPO" --state merged --limit 100 \
    --json number,mergedAt > "$TMPDIR_SCAN/merged.json" \
    || echo '[]' > "$TMPDIR_SCAN/merged.json"
  jq -r --arg since "$SINCE" '
    [ .[] | select(.mergedAt[0:10] >= $since) ] | length
    | "- merged in window: \(.)"
  ' "$TMPDIR_SCAN/merged.json"
  echo

  # ------------------------------------------------------------------
  # 7. TODO/FIXME hotspots in code
  # ------------------------------------------------------------------
  echo "## 7. TODO/FIXME/HACK hotspots (tracked files)"
  echo
  echo "| area | markers |"
  echo "|---|---:|"
  { git grep -l -E 'TODO|FIXME|HACK|XXX' \
    -- '*.rs' '*.py' '*.js' '*.ts' '*.kt' '*.swift' '*.sh' '*.yml' '*.yaml' \
    2>/dev/null || true; } \
  | awk -F/ '{ if (NF > 2) print $1"/"$2; else if (NF == 2) print $1; else print "." }' \
  | sort | uniq -c | sort -rn | head -25 \
  | while read -r cnt area; do printf '| `%s` | %s |\n' "$area" "$cnt"; done
  echo

  # ------------------------------------------------------------------
  # 8. docs/reports + performance TODOs
  # ------------------------------------------------------------------
  echo "## 8. Research reports & perf TODOs"
  echo
  echo "### 8a. Latest docs/reports"
  echo
  if [ -d docs/reports ]; then
    ls -lt docs/reports | head -12 | tail -n +2 \
      | awk '{printf "- `%s` (%s %s %s, %s bytes)\n", $NF, $6, $7, $8, $5}'
  else
    echo "- (docs/reports not present)"
  fi
  echo
  echo "### 8b. docs/performance.md open markers"
  echo
  if [ -f docs/performance.md ]; then
    grep -nE 'TODO|TBD|FIXME|- \[ \]' docs/performance.md | head -30 \
      | sed -E 's/^([0-9]+):(.{0,110}).*/- L\1:\2/' || echo "- (none)"
  else
    echo "- (docs/performance.md not present)"
  fi
  echo
} > "$OUT"

echo "wrote $OUT"
