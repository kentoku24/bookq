#!/usr/bin/env bash
# cargo-mutants wrapper for the bookq Rust workspace.
#
# Why this exists: cargo-mutants copies ONLY rust/ into its scratch tree, but
# viewer-rs reaches outside it via include_str!("../../../web|api/..."). The
# scratch copy therefore fails to compile unless $TMPDIR contains `web` and
# `api` entries resolving back into THIS checkout. This script builds that
# TMPDIR (per-worktree, on the external volume when mounted) and execs
# cargo-mutants with all args forwarded.
#
# Usage (from anywhere):
#   scripts/mutation/rust.sh                 # full workspace run
#   scripts/mutation/rust.sh --in-diff       # only mutants on changed lines
#   scripts/mutation/rust.sh -f archive.rs   # scope to one file
#   scripts/mutation/rust.sh --list          # enumerate without running
#
# Env:
#   MUT_TMPDIR   override scratch dir (default: /Volumes/Sabrent2T/mut-<tree>-tmp
#                when mounted, else ~/.cache/bookq/mutants-<tree>-tmp)
#   CARGO_MUTANTS_JOBS  default -j value (default: 4)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TREE_NAME="$(basename "$REPO_ROOT")"

if [[ -z "${MUT_TMPDIR:-}" ]]; then
  if [[ -d /Volumes/Sabrent2T ]]; then
    MUT_TMPDIR="/Volumes/Sabrent2T/mut-${TREE_NAME}-tmp"
  else
    MUT_TMPDIR="${HOME}/.cache/bookq/mutants-${TREE_NAME}-tmp"
  fi
fi
mkdir -p "$MUT_TMPDIR"
ln -sfn "$REPO_ROOT/web" "$MUT_TMPDIR/web"
ln -sfn "$REPO_ROOT/api" "$MUT_TMPDIR/api"
export TMPDIR="$MUT_TMPDIR"

cd "$REPO_ROOT/rust"
exec cargo mutants -j "${CARGO_MUTANTS_JOBS:-4}" "$@"
