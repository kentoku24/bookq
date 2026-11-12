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

# Vector file -> languages that must read it (issue #280's first set).
EXPECTED = {
    "work-chapters.json": ["python", "js", "swift"],
    "path-lookup.json": ["python", "js", "swift"],
    "encode-path.json": ["python", "js", "swift"],
    "solo-spreads.json": ["js", "swift"],
}

# Language -> test file that consumes the vectors for that language.
READERS = {
    "python": "tests/test_conformance_vectors.py",
    "js": "tests/test_conformance_vectors_js.py",
    "swift": "ios/BookqTests/ConformanceVectorsTests.swift",
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
            reader = (REPO_ROOT / READERS[lang]).read_text(encoding="utf-8")
            assert name in reader, f"{name} not referenced by {READERS[lang]}"
