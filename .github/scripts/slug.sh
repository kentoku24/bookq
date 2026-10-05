#!/usr/bin/env bash
set -euo pipefail
title="${1:-}"
slug=$(printf '%s' "$title" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//' | cut -c1-50 | sed -E 's/-+$//')
if [ -z "$slug" ]; then
  slug="issue-${2:-0}"
fi
printf '%s' "$slug"
