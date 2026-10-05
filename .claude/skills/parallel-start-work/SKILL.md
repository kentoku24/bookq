---
name: parallel-start-work
description: Use when carrying several bookq plans or issues from spec to merged PRs in one session. Trigger on "plan to merge", "run the batch", "merge chain", "worktree batch", "multi-issue run", or when handoff notes cite Boulder works, ledger receipts, or dependency ordered merges.
---

# Plan to Merge

Carry a plan batch from intake to merged PRs without cross contamination: one unit gets one worktree, one branch, one PR. The root re-runs worker acceptance commands before advancing (`AGENTS.md` holds branch and PR rules).

## When to Use

- Three or more plans or issues land in one session with ordering between them
- Handoff notes mention Boulder works, ledger receipts, or a merge chain

Not for: a single isolated fix (still needs its own worktree per AGENTS.md §8 — just do it directly without this skill), research with no PR expected, production deploys.

## Core Pattern

1. **Intake.** List targets. Check Boulder `works[]`, then `git log --all`: completed with zero commits means redo. A detailed issue body serves as spec absent a plan file.
2. **Isolate.** One worktree per unit under `.worktrees/`. Branch from `origin/main`, never local HEAD: issue-track uses `ai/<N>-<slug>`, plan-track uses `ai/<slug>` (local `/start-work` only; plan-track is FORBIDDEN on `ai/implement`/`ai/research`/bridge per AGENTS.md).
3. **Execute.** Delegate per unit: failing test first, small commits, full suite plus dev smoke. PR per track: issue-track `<type>(ai): ... (#N)` with `Closes #N` plus test section; plan-track `<type>(ai): ...` numberless with `Plan: .omo/plans/<slug>.md` (+ merge-base SHA) plus test section.
4. **Verify.** Root re-runs key checks (PR state, suite subset, residue envs, worktrees) before advancing. Sequential chain for shared files, parallel only when disjoint.
5. **Review gate.** review-work lanes (goal, QA, code, security, context) to APPROVE within a round cap, then merge.
6. **Merge chain.** Merge in dependency order via `gh pr merge --merge`. Contaminated branch: cherry-pick to fresh branch, replacement PR, close old as superseded. Cross PR clashes get follow-up issue plus PR. Stale PRs: approved merges, superseded closes with pointer, independent gets reported.

## Quick Reference

| Task | Command |
|---|---|
| Lost work | `git log --all --oneline --grep="#<N>"` (issue-track); `git log --all --oneline --grep="Plan: .omo/plans/"` (plan-track) |
| Isolate | `git worktree add .worktrees/<slug> -b ai/<N>-<slug> origin/main` (issue-track) or `-b ai/<slug>` (plan-track) |
| Suite | `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q` |
| Residue / merge | `./dev.sh list`, `git worktree list`, `gh pr merge --merge <PR>` |

## Common Mistakes

| Mistake | Fix |
|---|---|
| Worktree under `/tmp` | Sandbox denies `/tmp` writes, use `.worktrees/` |
| Branch from HEAD | Drags in unrelated commits, cut from `origin/main` |
| Trusting a worker DoneClaim | Rerun acceptance commands at root |
| Force push to fix a branch | Cherry-pick to a new branch plus replacement PR |

## Example

Issues #129 and #130 share `app/routes.py`, so the root chains them: two worktrees from `origin/main`, worker A lands #131, worker B rebases onto it for #132, review-work approves both, root merges in order, `./dev.sh list` confirms clean.

## Related

- `AGENTS.md` (branch, PR, deny rules), `dev-lifecycle` (suite plus smoke detail)

## Guardrails

No `/tmp` writes. No force push. No `main` push except `gh pr merge`. No `.env` reads. No `./prd.sh` from worktrees. No `down --all --full` without reviewing `list`. No invented issue numbers. Cap review and fix loops.
