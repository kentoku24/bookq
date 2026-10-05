#!/usr/bin/env bash
#
# test_gate.sh — regression tests for .github/scripts/gate.sh routing logic.
#
# Usage: bash .github/scripts/test_gate.sh
#
# Tests the 4 gate rules:
#   ① bot senders → allow=no
#   ② non-OWNER/MEMBER/COLLABORATOR → allow=no
#   ③ label routing (ai/implement wins over ai/research)
#   ④ /oc or /opencode mention detection

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GATE="$SCRIPT_DIR/gate.sh"

passed=0
failed=0
total=0

# --- helpers -----------------------------------------------------------

assert_gate() {
  local label="$1" expect_route="$2" expect_allow="$3" expect_mention="$4"
  shift 4
  local payload="$*"

  total=$((total + 1))
  local out
  out=$(printf '%s' "$payload" | bash "$GATE" 2>/dev/null) || {
    echo "FAIL [$total] $label — gate exited non-zero"
    echo "  payload: $payload"
    failed=$((failed + 1))
    return
  }
  local got_route got_allow got_mention
  got_route=$(printf '%s\n' "$out" | grep '^route=' | cut -d= -f2-)
  got_allow=$(printf '%s\n' "$out" | grep '^allow=' | cut -d= -f2-)
  got_mention=$(printf '%s\n' "$out" | grep '^mention=' | cut -d= -f2-)

  if [ "$got_route" = "$expect_route" ] && [ "$got_allow" = "$expect_allow" ] && [ "$got_mention" = "$expect_mention" ]; then
    echo "  ok  [$total] $label"
    passed=$((passed + 1))
  else
    echo "FAIL [$total] $label"
    echo "  expected: route=$expect_route allow=$expect_allow mention=$expect_mention"
    echo "  got:      route=$got_route allow=$got_allow mention=$got_mention"
    failed=$((failed + 1))
  fi
}

# 3-line invariant check: gate output must contain exactly 3 lines
assert_3_lines() {
  local label="$1"
  shift
  local payload="$*"
  total=$((total + 1))
  local out line_count
  out=$(printf '%s' "$payload" | bash "$GATE" 2>/dev/null) || {
    echo "FAIL [$total] $label — gate exited non-zero"
    failed=$((failed + 1))
    return
  }
  line_count=$(printf '%s\n' "$out" | wc -l | tr -d ' ')
  if [ "$line_count" = "3" ]; then
    echo "  ok  [$total] $label (3 lines)"
    passed=$((passed + 1))
  else
    echo "FAIL [$total] $label — expected 3 lines, got $line_count"
    echo "  output: $out"
    failed=$((failed + 1))
  fi
}

# --- payloads ----------------------------------------------------------

# Issue event: implement label, OWNER association
PAYLOAD_IMPL_OWNER='{"sender":{"login":"alice"},"issue":{"author_association":"OWNER","labels":[{"name":"ai/implement"}]},"comment":{}}'

# Issue event: research label, MEMBER association
PAYLOAD_RESEARCH_MEMBER='{"sender":{"login":"bob"},"issue":{"author_association":"MEMBER","labels":[{"name":"ai/research"}]},"comment":{}}'

# Issue event: dual labels (implement wins), COLLABORATOR
PAYLOAD_DUAL='{"sender":{"login":"carol"},"issue":{"author_association":"COLLABORATOR","labels":[{"name":"ai/implement"},{"name":"ai/research"}]},"comment":{}}'

# Issue event: dual labels reversed order (still implement wins)
PAYLOAD_DUAL_REV='{"sender":{"login":"carol"},"issue":{"author_association":"COLLABORATOR","labels":[{"name":"ai/research"},{"name":"ai/implement"}]},"comment":{}}'

# Issue event: no labels, OWNER
PAYLOAD_NO_LABELS='{"sender":{"login":"alice"},"issue":{"author_association":"OWNER","labels":[]},"comment":{}}'

# Issue event: bot sender, OWNER, implement label
PAYLOAD_BOT='{"sender":{"login":"my-bot[bot]"},"issue":{"author_association":"OWNER","labels":[{"name":"ai/implement"}]},"comment":{}}'

# Issue event: github-actions[bot], MEMBER, implement label
PAYLOAD_GH_ACTIONS_BOT='{"sender":{"login":"github-actions[bot]"},"issue":{"author_association":"MEMBER","labels":[{"name":"ai/implement"}]},"comment":{}}'

# Issue event: renovate[bot], MEMBER, research label
PAYLOAD_RENOVATE_BOT='{"sender":{"login":"renovate[bot]"},"issue":{"author_association":"MEMBER","labels":[{"name":"ai/research"}]},"comment":{}}'

# Issue event: CONTRIBUTOR association (not trusted)
PAYLOAD_CONTRIBUTOR='{"sender":{"login":"dave"},"issue":{"author_association":"CONTRIBUTOR","labels":[{"name":"ai/implement"}]},"comment":{}}'

# Issue event: NONE association (not trusted)
PAYLOAD_NONE_ASSOC='{"sender":{"login":"eve"},"issue":{"author_association":"NONE","labels":[{"name":"ai/implement"}]},"comment":{}}'

# Comment event: /oc mention, OWNER issue + OWNER comment
PAYLOAD_COMMENT_OC='{"sender":{"login":"alice"},"issue":{"author_association":"OWNER","labels":[]},"comment":{"author_association":"OWNER","body":"/oc do something"}}'

# Comment event: /opencode mention, MEMBER
PAYLOAD_COMMENT_OPENCODE='{"sender":{"login":"bob"},"issue":{"author_association":"MEMBER","labels":[]},"comment":{"author_association":"MEMBER","body":"/opencode run tests"}}'

# Comment event: /oc in middle of body (not at start → no mention)
PAYLOAD_COMMENT_OC_MIDDLE='{"sender":{"login":"alice"},"issue":{"author_association":"OWNER","labels":[]},"comment":{"author_association":"OWNER","body":"please /oc do something"}}'

# Comment event: OWNER issue + NONE comment (comment assoc overrides → block)
PAYLOAD_COMMENT_NONE='{"sender":{"login":"eve"},"issue":{"author_association":"OWNER","labels":[{"name":"ai/implement"}]},"comment":{"author_association":"NONE","body":"/oc fix this"}}'

# Comment event: NONE issue + OWNER comment (comment assoc overrides → allow)
PAYLOAD_COMMENT_TRUSTED='{"sender":{"login":"alice"},"issue":{"author_association":"NONE","labels":[]},"comment":{"author_association":"OWNER","body":"/oc do something"}}'

# Issue event: empty labels array, OWNER
PAYLOAD_EMPTY_LABELS='{"sender":{"login":"alice"},"issue":{"author_association":"OWNER","labels":[]},"comment":{}}'

# --- run tests ----------------------------------------------------------

echo "=== gate.sh regression tests ==="
echo ""

# Rule ③: label routing
assert_gate "implement label → route=implement"      "implement" "yes" "no"  "$PAYLOAD_IMPL_OWNER"
assert_gate "research label → route=research"         "research"  "yes" "no"  "$PAYLOAD_RESEARCH_MEMBER"
assert_gate "dual labels → implement wins"            "implement" "yes" "no"  "$PAYLOAD_DUAL"
assert_gate "dual labels reversed → implement wins"   "implement" "yes" "no"  "$PAYLOAD_DUAL_REV"
assert_gate "no labels → route=none"                  "none"      "yes" "no"  "$PAYLOAD_NO_LABELS"
assert_gate "empty labels → route=none"               "none"      "yes" "no"  "$PAYLOAD_EMPTY_LABELS"

# Rule ①: bot senders
assert_gate "bot sender [bot] → allow=no"             "implement" "no"  "no"  "$PAYLOAD_BOT"
assert_gate "github-actions[bot] → allow=no"          "implement" "no"  "no"  "$PAYLOAD_GH_ACTIONS_BOT"
assert_gate "renovate[bot] → allow=no"                "research"  "no"  "no"  "$PAYLOAD_RENOVATE_BOT"

# Rule ②: association gating
assert_gate "CONTRIBUTOR → allow=no"                  "implement" "no"  "no"  "$PAYLOAD_CONTRIBUTOR"
assert_gate "NONE assoc → allow=no"                   "implement" "no"  "no"  "$PAYLOAD_NONE_ASSOC"

# Rule ④: mention detection
assert_gate "/oc at start → mention=yes"              "none"      "yes" "yes" "$PAYLOAD_COMMENT_OC"
assert_gate "/opencode at start → mention=yes"        "none"      "yes" "yes" "$PAYLOAD_COMMENT_OPENCODE"
assert_gate "/oc in middle → mention=no"              "none"      "yes" "no"  "$PAYLOAD_COMMENT_OC_MIDDLE"

# Comment association override
assert_gate "issue OWNER + comment NONE → block (body still triggers mention)" "implement" "no"  "yes" "$PAYLOAD_COMMENT_NONE"
assert_gate "issue NONE + comment OWNER → allow"      "none"      "yes" "yes" "$PAYLOAD_COMMENT_TRUSTED"

# --- new behaviors (B1/B3/B4 fixes) -----------------------------------

# B3: label event by a trusted labeler on an untrusted-authored issue → allowed
PAYLOAD_LABEL_MEMBER_ON_NONE='{"action":"labeled","label":{"name":"ai/implement"},"issue":{"author_association":"NONE","labels":[{"name":"ai/implement"}]},"sender":{"login":"bob"}}'
assert_gate "label event: MEMBER labeler on NONE author → allow" "implement" "yes" "no" "$PAYLOAD_LABEL_MEMBER_ON_NONE"

# B3: label event by a bot sender → still blocked (bot rule ① not overridden)
PAYLOAD_LABEL_BOT='{"action":"labeled","label":{"name":"ai/implement"},"issue":{"author_association":"NONE","labels":[{"name":"ai/implement"}]},"sender":{"login":"triage-bot[bot]"}}'
assert_gate "label event: bot labeler → allow=no" "implement" "no" "no" "$PAYLOAD_LABEL_BOT"

# B4: PR review comment /oc by MEMBER (no .issue) → route=implement
PAYLOAD_PR_REVIEW_OC='{"action":"created","pull_request":{"number":7},"comment":{"author_association":"MEMBER","body":"/oc implement"},"sender":{"login":"bob"}}'
assert_gate "PR review /oc by MEMBER → route=implement" "implement" "yes" "yes" "$PAYLOAD_PR_REVIEW_OC"

# B1: repository_dispatch openresearch-issue → allow + implement (bot-allow exception)
PAYLOAD_DISPATCH='{"action":"openresearch-issue","client_payload":{"issue_number":9,"route":"implement"}}'
total=$((total + 1))
out_d=$(GITHUB_EVENT_NAME=repository_dispatch bash "$GATE" 2>/dev/null <<< "$PAYLOAD_DISPATCH")
if [ "$(printf '%s\n' "$out_d" | grep '^route=' | cut -d= -f2-)" = "implement" ] \
   && [ "$(printf '%s\n' "$out_d" | grep '^allow=' | cut -d= -f2-)" = "yes" ] \
   && [ "$(printf '%s\n' "$out_d" | grep '^mention=' | cut -d= -f2-)" = "no" ]; then
  echo "  ok  [$total] repository_dispatch openresearch-issue → allow+implement"
  passed=$((passed + 1))
else
  echo "FAIL [$total] repository_dispatch openresearch-issue → allow+implement"
  echo "  got: $out_d"
  failed=$((failed + 1))
fi

# Output invariant: exactly 3 lines
assert_3_lines "3-line output invariant (implement)"  "$PAYLOAD_IMPL_OWNER"
assert_3_lines "3-line output invariant (research)"   "$PAYLOAD_RESEARCH_MEMBER"
assert_3_lines "3-line output invariant (none)"       "$PAYLOAD_NO_LABELS"

echo ""
echo "=== results: $passed/$total passed, $failed failed ==="

if [ "$failed" -gt 0 ]; then
  exit 1
fi
