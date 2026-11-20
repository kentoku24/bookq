"""Bundle the YAML API specs into single-file JSON for client tests.

Reads `api/openapi.v1.yaml` + `api/asyncapi.yaml` and writes
`api/dist/openapi.v1.json` + `api/dist/asyncapi.json`.

The specs are single-file (only internal `$ref`s, e.g. the recursive
`TreeNode.children`), so "bundling" here is a deterministic YAML->JSON
conversion: internal `$ref`s are preserved as-is and resolved by each
consumer against the same document (iOS `SpecConformanceTests`,
Android `SpecConformanceTest`, Python `json.load`, ...). A full
dereference is impossible by construction — `TreeNode` is recursive.

Determinism: `sort_keys=True`, 2-space indent, UTF-8, trailing newline.
Run via `scripts/api/codegen.sh` (pins `PyYAML==6.0.3`) — never by hand.
"""

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

PAIRS = (
    ("api/openapi.v1.yaml", "api/dist/openapi.v1.json"),
    ("api/asyncapi.yaml", "api/dist/asyncapi.json"),
)


def main() -> None:
    for src, dst in PAIRS:
        doc = yaml.safe_load((ROOT / src).read_text(encoding="utf-8"))
        out = ROOT / dst
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {dst}", file=sys.stderr)


if __name__ == "__main__":
    main()
