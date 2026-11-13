"""Declaration check (#280) — pure Python, no browser.

Every ``api/vectors/*.json`` file declares the languages that must consume
it; this test pins both the declared set and that each language's reader
file actually references the vector. A new vector (or a dropped reader)
fails here instead of drifting silently.
"""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VECTORS_DIR = REPO_ROOT / "api" / "vectors"

# Vector file -> languages that must read it (issue #280's first set +
# kotlin via #460).
EXPECTED = {
    "work-chapters.json": ["python", "js", "swift", "rust", "kotlin"],
    "path-lookup.json": ["python", "js", "swift", "rust", "kotlin"],
    "encode-path.json": ["python", "js", "swift", "rust", "kotlin"],
    "solo-spreads.json": ["js", "swift", "kotlin"],
    "positions-cases.json": ["python", "rust"],
}

# Language -> test file that consumes the vectors for that language.
READERS = {
    "python": "tests/test_conformance_vectors.py",
    "js": "tests/test_conformance_vectors_js.py",
    "swift": "ios/BookqTests/ConformanceVectorsTests.swift",
    "kotlin": "android/core/src/test/kotlin/dev/bookq/core/ConformanceVectorsTest.kt",
    "rust": "rust/viewer-rs/src/conformance_vectors.rs",
}

# (vector, language) pairs whose reader differs from the per-language
# default — positions-cases is consumed by dedicated store tests, not the
# shared conformance harness.
READER_OVERRIDES = {
    ("positions-cases.json", "python"): "tests/test_reading_positions_store.py",
    ("positions-cases.json", "rust"): "rust/viewer-rs/src/state.rs",
}


def test_vector_declarations_match_consumers():
    files = sorted(p.name for p in VECTORS_DIR.glob("*.json"))
    assert files == sorted(EXPECTED), f"unexpected vector set: {files}"
    for name, languages in EXPECTED.items():
        data = json.loads((VECTORS_DIR / name).read_text(encoding="utf-8"))
        assert data["format"] == 1, name
        assert sorted(data["languages"]) == sorted(languages), name
        assert data["cases"], f"{name} must carry cases"
        for lang in languages:
            reader_path = READER_OVERRIDES.get((name, lang), READERS[lang])
            reader = (REPO_ROOT / reader_path).read_text(encoding="utf-8")
            assert name in reader, f"{name} not referenced by {reader_path}"
