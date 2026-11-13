#!/usr/bin/env bash
# Regenerate every artifact derived from the API specs (single entry point).
#
#   - app/static/api/api-types.d.ts  (openapi-typescript, version pinned)
#   - api/dist/openapi.v1.json       (YAML->JSON bundle of openapi.v1.yaml)
#   - api/dist/asyncapi.json         (YAML->JSON bundle of asyncapi.yaml)
#   - api/dist/examples.json         (named `examples:` bundle, #237)
#
# Tool versions are pinned here so local runs and CI produce identical
# output. CI (`api-spec-lint.yml` codegen-drift job) runs this script and
# fails on `git status --porcelain` — editing a spec without regenerating
# breaks the PR.
#
# Usage: ./scripts/api/codegen.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# Pinned generator versions — bump deliberately, then commit the regenerated
# artifacts in the same PR so the drift gate stays green.
OPENAPI_TS_VERSION="7.13.0"
PYYAML_VERSION="6.0.3"

npx --yes "openapi-typescript@${OPENAPI_TS_VERSION}" \
  "${ROOT}/api/openapi.v1.yaml" -o "${ROOT}/app/static/api/api-types.d.ts"

mkdir -p "${ROOT}/api/dist"
uv run --with "pyyaml==${PYYAML_VERSION}" \
  python3 "${ROOT}/scripts/api/bundle.py"

uv run --with "pyyaml==${PYYAML_VERSION}" \
  python3 "${ROOT}/scripts/api/extract-examples.py"
