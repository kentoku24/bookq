#!/usr/bin/env bash
#
# oc-fail-classify.sh — classify an opencode-auto job failure for issue triage (#635).
#
# Usage: oc-fail-classify.sh <run-id> <repo> <job-id> [name=outcome ...]
#
# Primary signal: `gh run view --json jobs` — the FIRST step with
# conclusion=failure inside the calling job is the causal failure (later
# steps are skipped unless they carry if: failure()/always()). The step
# name maps to a class; `--log-failed` patterns then refine the hint and
# reclassify the two ambiguous cases (agent-internal push rejection,
# test-env vs test-regression).
#
# Fallback: name=outcome pairs (GitHub step outcomes, e.g. pushpr=failure)
# mapped through a fixed table — used when the API fetch fails (run still
# in progress, token lacks actions:read, gh missing, ...).
#
# Prints exactly one line: "<class>|<hint>". Always exits 0 — this runs
# inside the ❌ failed label step and must never mask the transition.
#
# Classes: push | env | test | agent | infra | no-output | unknown

set -uo pipefail

run_id="${1:-}"
repo="${2:-}"
job="${3:-}"
if [ "$#" -ge 3 ]; then shift 3; else set --; fi
pairs=("$@")

emit() { printf '%s|%s\n' "$1" "$2"; }

has() { # $1 = egrep pattern against the failed-step log
  [ -n "$log" ] && printf '%s\n' "$log" | grep -qE -- "$1"
}

# --- fetch signals ---------------------------------------------------------
log=""
failed_step=""
if [ -n "$run_id" ] && [ -n "$repo" ] && command -v gh >/dev/null 2>&1; then
  jobs_json=$(timeout 60 gh run view "$run_id" --repo "$repo" --json jobs 2>/dev/null) || jobs_json=""
  if [ -n "$jobs_json" ]; then
    if [ -n "$job" ]; then
      failed_step=$(printf '%s' "$jobs_json" | jq -r --arg j "$job" \
        '[.jobs[] | select(.name == $j) | .steps[]? | select(.conclusion == "failure") | .name] | first // empty' 2>/dev/null)
    else
      failed_step=$(printf '%s' "$jobs_json" | jq -r \
        '[.jobs[].steps[]? | select(.conclusion == "failure") | .name] | first // empty' 2>/dev/null)
    fi
  fi
  log=$(timeout 60 gh run view "$run_id" --repo "$repo" --log-failed 2>/dev/null) || log=""
fi

# --- classify by failed step name ------------------------------------------
if [ -n "$failed_step" ]; then
  case "$failed_step" in
    *"Branch, commit, push"*|*"open PR"*)
      if has 'no commits and no PR|agent produced nothing'; then
        emit no-output "エージェントが成果を残せなかった（worktree 分離・push 拒否・空 PR の可能性）"
      elif has 'refusing to allow a GitHub App|without .?workflows.? permission'; then
        emit push "変更が .github/workflows/ を含み GITHUB_TOKEN に workflows 権限が不足（job permissions を確認）"
      else
        emit push "git push / PR 作成ステップで失敗（non-fast-forward・権限・workflows 制限）"
      fi
      exit 0
      ;;
    *"test gate"*|*"pytest"*)
      # No env refinement here: pytest failure output routinely embeds
      # playwright/browser strings from unrelated tests — a false "env"
      # would misdirect triage. The hint already covers both causes.
      emit test "テストゲート失敗 — agent 変更の回帰（検出としては正常）またはテスト環境起因"
      exit 0
      ;;
    *"Preflight"*|*"preflight"*)
      emit env "環境 preflight 失敗 — runner イメージの依存不足を疑う"
      exit 0
      ;;
    *"Run opencode"*)
      # The agent step fails only when opencode itself exits non-zero. A
      # rejected git push inside the run is the most actionable cause —
      # the agent cannot force-push (opencode.json) so it may give up.
      if has 'refusing to allow a GitHub App|failed to push some refs|remote rejected|non-fast-forward'; then
        if has 'refusing to allow a GitHub App|without .?workflows.? permission'; then
          emit push "agent の git push が .github/workflows/ 権限で拒否 — job permissions を確認"
        else
          emit push "agent の git push が拒否（non-fast-forward / 権限 — branch 競合の可能性）"
        fi
      elif has 'exit code 56|Could not resolve|Connection reset|Network is unreachable|Temporary failure'; then
        emit infra "opencode API / ネットワークへの一過性障害 — 再試行で回復する可能性"
      else
        emit agent "opencode CLI / agent 実行自体の失敗（LLM・API・タイムアウト）"
      fi
      exit 0
      ;;
    *)
      # Untagged env/setup step: checkout, sign-in, toolchain, cache,
      # system deps, cargo build, playwright install, label swaps, ...
      if has 'exit code 56|exit code 35|Could not resolve|Connection reset|Network is unreachable|Temporary failure|502 Bad Gateway|503 Service'; then
        emit infra "一過性のネットワーク/インフラ障害 — 再試行で回復する可能性"
      else
        emit env "環境/セットアップ系ステップで失敗: ${failed_step}"
      fi
      exit 0
      ;;
  esac
fi

# --- fallback: step outcomes ----------------------------------------------
for pair in ${pairs[@]+"${pairs[@]}"}; do
  [ -z "$pair" ] && continue
  name="${pair%%=*}"
  outcome="${pair#*=}"
  [ "$outcome" != "failure" ] && continue
  case "$name" in
    pushpr)    emit push "git push / PR 作成ステップで失敗（branch 競合・権限・workflows 制限）"; exit 0 ;;
    testgate)  emit test "テストゲート失敗 — agent 変更の回帰（検出としては正常）またはテスト環境起因"; exit 0 ;;
    preflight) emit env "環境 preflight 失敗 — runner イメージの依存不足を疑う"; exit 0 ;;
    agent)     emit agent "opencode CLI / agent 実行自体の失敗（LLM・API・タイムアウト）"; exit 0 ;;
    *)         emit env "環境/セットアップ系ステップで失敗"; exit 0 ;;
  esac
done

if [ "${#pairs[@]}" -gt 0 ]; then
  # Tagged steps all succeeded/skipped yet the job failed → the failure was
  # in an untagged env/setup step (checkout, toolchain, cargo build, ...).
  emit env "環境/セットアップ系ステップで失敗（タグ付きステップは未失敗）"
else
  emit unknown "失敗分類不能（ログ取得失敗・ステップ情報なし）"
fi
exit 0
