"""PWA API-client guards (#236).

- `/api/v1` literals live only in `app/static/api/` (generated
  `endpoints.js` + the typed `v1.js` fallback). Everywhere else the paths
  come from `BookqApi.endpoints` / `BookqApiEndpoints`, so a route rename
  touches one generated file.
- 202 (index-building) retry honors `Retry-After` in exactly one place:
  `BookqApi.fetchWithBuildingRetry`. `app.js` keeps its legacy shallow/full
  retry loop as an explicit follow-up (issue: "app.js は後追い"); the SW's
  `!== 202` is a no-store cache guard, not a retry.
- `endpoints.js` freshness is enforced by re-running the generator.
"""
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC = REPO_ROOT / "app" / "static"
API_DIR = STATIC / "api"

# app.js is excluded from checkJs for now (staged rollout, see jsconfig.json)
# but it must still use BookqApi.endpoints, never literals — so it is IN scope
# for the literal guard below.
_LITERAL_RE = re.compile(r"""['"]/api/v1""")

# 202-status handling: the typed client (retry loop), the app.js legacy loop
# (explicit follow-up) and the SW cache guard only.
_LEGACY_202_FILES = {
    STATIC / "app.js",  # own shallow/full retry loop — follow-up per #236
    STATIC / "sw.js",  # `!== 202` no-store cache guard, not a retry
}
_202_RE = re.compile(r"status\s*!==?\s*202|status\s*===\s*202")


def _js_files():
    out = []
    for p in sorted(STATIC.rglob("*.js")):
        if "vendor" in p.parts:
            continue
        if API_DIR in p.parents or p.parent == API_DIR:
            continue
        out.append(p)
    return out


def test_no_hardcoded_api_v1_literals():
    """No quoted `/api/v1...` literal outside app/static/api/ (*.js)."""
    offenders = {}
    for p in _js_files():
        text = p.read_text(encoding="utf-8")
        hits = _LITERAL_RE.findall(text)
        if hits:
            offenders[str(p.relative_to(REPO_ROOT))] = len(hits)
    assert not offenders, (
        "hard-coded /api/v1 literals outside app/static/api/ — "
        f"use BookqApi.endpoints / BookqApiEndpoints instead: {offenders}"
    )


def test_endpoints_cover_client_paths():
    """endpoints.js carries every path the typed client serves."""
    text = (API_DIR / "endpoints.js").read_text(encoding="utf-8")
    for key in (
        "tree",
        "images",
        "search",
        "facets",
        "thumbSprite",
        "readingPositions",
        "folderVisibility",
        "version",
        "health",
    ):
        assert re.search(rf"^\s*{key}:\s*\"/api/v1/", text, re.M), (
            f"endpoints.js missing {key}"
        )


def test_endpoints_fresh_against_spec():
    """The checked-in endpoints.js matches api/openapi.v1.yaml."""
    proc = subprocess.run(
        [sys.executable, "scripts/gen-api-endpoints.py", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_typed_client_surface():
    """BookqApi exposes the typed client seam (methods + 202 loop + ApiError)."""
    text = (API_DIR / "v1.js").read_text(encoding="utf-8")
    for name in (
        "getTree",
        "getImages",
        "search",
        "getFacets",
        "getPositions",
        "putPosition",
        "deletePositions",
        "getVisibility",
        "putVisibility",
        "fetchWithBuildingRetry",
        "fetchJson",
        "errorCodeOf",
        "ApiError",
    ):
        assert name in text, f"BookqApi missing {name}"
    # Spec-typed response access: these field reads must stay typed against
    # api-types.d.ts so a spec rename breaks tsc (see pwa-typecheck CI).
    for marker in ("TreeResponse", "FolderVisibility", "ReadingPosition"):
        assert marker in text, f"v1.js lost its spec-type tie ({marker})"


def test_retry_after_honored_only_in_client():
    """Only the typed client reads the Retry-After header (*.js)."""
    offenders = []
    for p in sorted(STATIC.rglob("*.js")):
        if "vendor" in p.parts:
            continue
        if "Retry-After" in p.read_text(encoding="utf-8"):
            offenders.append(str(p.relative_to(REPO_ROOT)))
    assert offenders == ["app/static/api/v1.js"], (
        f"Retry-After must be honored only in app/static/api/v1.js: {offenders}"
    )


def test_202_status_checks_are_centralized():
    """202 handling lives in the client loop (+ legacy app.js, SW guard)."""
    offenders = []
    for p in sorted(STATIC.rglob("*.js")):
        if "vendor" in p.parts:
            continue
        if p.parent == API_DIR and p.name == "endpoints.js":
            continue
        if _202_RE.search(p.read_text(encoding="utf-8")):
            offenders.append(p)
    allowed = {API_DIR / "v1.js"} | _LEGACY_202_FILES
    assert set(offenders) <= allowed, (
        "202-status handling outside the typed client/app.js-legacy/SW-guard: "
        f"{[str(p.relative_to(REPO_ROOT)) for p in offenders if p not in allowed]}"
    )
    assert API_DIR / "v1.js" in offenders, "client 202 loop missing in api/v1.js"
