#!/usr/bin/env bash
#
# gate.sh — shared event gate for opencode auto-run workflows.
#
# Reads a GitHub event JSON payload on STDIN and prints EXACTLY 3 lines:
#   route=implement|research|none
#   allow=yes|no
#   mention=yes|no
#
# Judgment rules (evaluated in this order):
#   ① event.sender.login ends with "[bot]"  -> allow=no
#      (App-token comments DO refire workflows; GITHUB_TOKEN does not.
#       Blocking bot senders prevents infinite loops.)
#   ② author_association NOT in OWNER/MEMBER/COLLABORATOR -> allow=no
#      (issue events: event.issue.author_association;
#       comment events: event.comment.author_association)
#   ③ labels (event.issue.labels[].name):
#        contains ai/implement           -> route=implement
#        else contains ai/research       -> route=research
#        dual-label                      -> implement WINS
#        neither                         -> route=none
#   ④ event.comment.body starts with "/oc" or "/opencode" -> mention=yes
#
# 失効ロック (stale lock) policy:
#   This gate deliberately performs NO liveness/run-list queries (the old
#   self-match defect is abolished). If the "🤖 processing" sentinel label is
#   already present when the gate passes, the CALLER treats it as a 失効ロック
#   (stale lock) left behind by a cancelled or timed-out run: the sentinel is
#   deleted and execution PROCEEDS NORMALLY. Recovery is always visible —
#   never silently exit on a leftover sentinel.

set -euo pipefail

payload=$(cat)

# GITHUB_EVENT_NAME is exported by the workflow so gate.sh can special-case
# internal dispatches (see the openresearch bridge exception below).
event_name="${GITHUB_EVENT_NAME:-}"

sender=$(printf '%s' "$payload" | jq -r '.sender.login // ""')

# author_association: comment events take precedence over issue events.
assoc=$(printf '%s' "$payload" | jq -r '
  if has("comment") and (.comment.author_association != null) then .comment.author_association
  elif has("issue") and (.issue.author_association != null) then .issue.author_association
  else "" end')

labels=$(printf '%s' "$payload" | jq -r '[.issue.labels[]?.name] | join("\n")')

body=$(printf '%s' "$payload" | jq -r '.comment.body // ""')

route=none
allow=yes
mention=no

# ----------------------------------------------------------------------
# Internal openresearch bridge dispatch — bot-allow exception.
# repository_dispatch is emitted ONLY by our own bridge workflow, after it
# created a mirrored bookq issue carrying the `Related: kentoku24/openresearch#N`
# marker. The consumer workflow (opencode-auto.yml) re-verifies that marker
# before running opencode, so this is a trusted internal channel — NOT an
# external bot loop. Rule ① (bot) and ② (association) are intentionally skipped.
# ----------------------------------------------------------------------
if [ "$event_name" = "repository_dispatch" ]; then
  dispatch_action=$(printf '%s' "$payload" | jq -r '.action // ""')
  if [ "$dispatch_action" = "openresearch-issue" ]; then
    route=$(printf '%s' "$payload" | jq -r '.client_payload.route // "implement"')
    allow=yes
    mention=no
    printf 'route=%s\n' "$route"
    printf 'allow=%s\n' "$allow"
    printf 'mention=%s\n' "$mention"
    exit 0
  fi
fi

# Rule ③: label routing (dual-label -> implement wins).
case "$labels" in
  *"ai/implement"*) route=implement ;;
  *"ai/research"*)  route=research ;;
esac

# Rule ①: bot senders are never allowed.
case "$sender" in
  *"[bot]") allow=no ;;
esac

# Rule ②: only OWNER / MEMBER / COLLABORATOR are trusted.
# EXCEPTION: label events. Only collaborators (triage+) can add labels, so the
# labeler is implicitly trusted; gating on the (possibly external) issue author
# would wrongly block triage by trusted maintainers. Rule ① (bot) still applies.
is_label_event=$(printf '%s' "$payload" | jq -r 'has("label") or (.action == "labeled")')
if [ "$is_label_event" = "true" ]; then
  : # skip association gate for label events
else
  case "$assoc" in
    OWNER|MEMBER|COLLABORATOR) : ;;
    *) allow=no ;;
  esac
fi

# Rule ④: /oc or /opencode mention in comment body.
case "$body" in
  /oc*|/opencode*) mention=yes ;;
esac

# A recognized mention on a PR review comment (which has no .issue object, so
# no label routing is possible) routes to implement so the mention channel is
# actionable on PRs. Issue-comment mentions keep route=none — the mention
# channel triggers on mention=yes regardless of route.
if [ "$mention" = "yes" ] && [ "$route" = "none" ]; then
  has_issue=$(printf '%s' "$payload" | jq -r 'has("issue")')
  if [ "$has_issue" = "false" ]; then
    route=implement
  fi
fi

printf 'route=%s\n' "$route"
printf 'allow=%s\n' "$allow"
printf 'mention=%s\n' "$mention"
