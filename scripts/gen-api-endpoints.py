#!/usr/bin/env python3
"""Generate app/static/api/endpoints.js from api/openapi.v1.yaml.

The PWA must not hard-code `/api/v1/...` strings outside `app/static/api/`
(#236): this file is the single source of truth for endpoint paths, loaded
by the page (before `api/v1.js`) and by the Service Worker via
`importScripts` (which cannot see `window.BookqApi`).

Sources:
  - v1 paths: `api/openapi.v1.yaml` (`paths:` keys starting with `/api/v1/`)
  - EXTRA (not in the OpenAPI schema on purpose): `/api/v1/pair-report`,
    the test-only #142 bridge (`app/api_v1.py`, `include_in_schema=False`)

Usage:
  python3 scripts/gen-api-endpoints.py          # rewrite endpoints.js
  python3 scripts/gen-api-endpoints.py --check  # CI freshness gate (exit 1 on drift)

Only the Python standard library is used.
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC = REPO_ROOT / "api" / "openapi.v1.yaml"
OUT = REPO_ROOT / "app" / "static" / "api" / "endpoints.js"

# Spec path -> BookqApiEndpoints key. An unknown /api/v1/* path in the spec
# fails the script on purpose: add the mapping here first.
PATH_TO_KEY = {
    "/api/v1/tree": "tree",
    "/api/v1/images": "images",
    "/api/v1/search": "search",
    "/api/v1/facets": "facets",
    "/api/v1/thumb-sprite": "thumbSprite",
    "/api/v1/reading-positions": "readingPositions",
    "/api/v1/folder-visibility": "folderVisibility",
    "/api/v1/index/refresh": "indexRefresh",
    "/api/v1/version": "version",
    "/api/v1/health": "health",
    # Native-client only (the PWA never calls it) — listed so the
    # unmapped-path gate stays exact.
    "/api/v1/auth/token": "authToken",
}

# Deliberately outside the OpenAPI schema (include_in_schema=False).
EXTRA = {
    "pairReport": "/api/v1/pair-report",  # test-only #142 bridge (app/api_v1.py)
}

HEADER = """/* bookq api endpoints — shared `/api/v1/...` path constants.
 * GENERATED — do not hand-edit. Regenerate:
 *   python3 scripts/gen-api-endpoints.py
 * Sources: api/openapi.v1.yaml (v1 paths) + test-only pair-report
 * (app/api_v1.py, include_in_schema=False — see script EXTRA).
 * Loads in both the page (before api/v1.js) and the Service Worker via
 * importScripts (no window there) — hence globalThis, no window/document.
 */
"""


def _spec_paths():
    text = SPEC.read_text(encoding="utf-8")
    # Top-level `paths:` keys look like `  /api/v1/tree:`.
    found = re.findall(r'^  (/api/v1/[A-Za-z0-9/_{}-]+):\s*$', text, re.M)
    # Ignore templated leftovers (none today, but fail loudly if added).
    return sorted(set(found))


def build():
    spec_paths = _spec_paths()
    unknown = [p for p in spec_paths if p not in PATH_TO_KEY]
    if unknown:
        raise SystemExit(
            "gen-api-endpoints: unmapped spec paths (add to PATH_TO_KEY): "
            + ", ".join(unknown)
        )
    entries = [(key, path) for path, key in sorted(PATH_TO_KEY.items())]
    entries += [(key, EXTRA[key]) for key in sorted(EXTRA)]
    lines = [HEADER, '(function () {\n', '  "use strict";\n', "\n", "  var E = {\n"]
    for key, path in entries:
        lines.append(f'    {key}: "{path}",\n')
    lines += [
        "  };\n",
        "\n",
        "  var g = (typeof globalThis !== 'undefined') ? globalThis : self;\n",
        "  g.BookqApiEndpoints = E;\n",
        "})();\n",
    ]
    return "".join(lines)


def main(argv):
    check = "--check" in argv
    content = build()
    if check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else None
        if current != content:
            print(f"gen-api-endpoints: {OUT} is stale — run scripts/gen-api-endpoints.py")
            return 1
        print(f"gen-api-endpoints: {OUT} is fresh")
        return 0
    OUT.write_text(content, encoding="utf-8")
    print(f"gen-api-endpoints: wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
