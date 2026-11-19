#!/usr/bin/env python3
"""Validate spec `examples:` values against their schemas (#237).

Resolves local `$ref`s (components/schemas + components/responses) and
checks every response/requestBody example with `jsonschema`. Fails loudly
on the first mismatch so `spec example` can stay the single fixture source.

Usage:
    python3 scripts/api/validate-examples.py [--spec api/openapi.v1.yaml]
Requires: pyyaml, jsonschema
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("pyyaml required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2)

try:
    import jsonschema
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover
    print("jsonschema required: pip install jsonschema", file=sys.stderr)
    raise SystemExit(2)


def _resolve(node, root, stack=()):
    """Recursively resolve local '#/...' $refs (OpenAPI 3.1 sibling-safe)."""
    if isinstance(node, dict):
        if set(node.keys()) == {"$ref"} or (
            "$ref" in node and str(node["$ref"]).startswith("#/")
        ):
            ref = node["$ref"]
            if not ref.startswith("#/"):
                raise ValueError(f"only local refs supported: {ref}")
            if ref in stack:
                # Recursive schema (e.g. TreeNode.children -> TreeNode):
                # leave an open schema so finite example depth validates.
                return {}
            target = root
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            resolved = _resolve(copy.deepcopy(target), root, stack + (ref,))
            if set(node.keys()) == {"$ref"}:
                return resolved
            merged = dict(resolved) if isinstance(resolved, dict) else resolved
            if isinstance(merged, dict):
                for k, v in node.items():
                    if k != "$ref":
                        merged[k] = _resolve(v, root, stack + (ref,))
            return merged
        return {k: _resolve(v, root, stack) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v, root, stack) for v in node]
    return node


def _iter_examples(spec):
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            if not isinstance(op, dict) or method.startswith("x-"):
                continue
            op_id = op.get("operationId", f"{method} {path}")
            for status, resp in (op.get("responses") or {}).items():
                if not isinstance(resp, dict):
                    continue
                resp_r = _resolve(resp, spec)
                for media, media_obj in (resp_r.get("content") or {}).items():
                    if not isinstance(media_obj, dict):
                        continue
                    schema = media_obj.get("schema")
                    if schema is None:
                        continue
                    if "examples" in media_obj and isinstance(media_obj["examples"], dict):
                        for name, ex in media_obj["examples"].items():
                            if isinstance(ex, dict) and "value" in ex:
                                yield (f"{op_id} {method.upper()} {path} "
                                       f"{status} [{name}] ({media})",
                                       schema, ex["value"])
                    elif "example" in media_obj:
                        yield (f"{op_id} {method.upper()} {path} "
                               f"{status} [default] ({media})",
                               schema, media_obj["example"])
            req = (op.get("requestBody") or {}).get("content") or {}
            req_r = _resolve({"c": req}, spec)["c"]
            for media, media_obj in req_r.items():
                if not isinstance(media_obj, dict):
                    continue
                schema = media_obj.get("schema")
                if schema is None:
                    continue
                if "examples" in media_obj and isinstance(media_obj["examples"], dict):
                    for name, ex in media_obj["examples"].items():
                        if isinstance(ex, dict) and "value" in ex:
                            yield (f"{op_id} request [{name}] ({media})",
                                   schema, ex["value"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default="api/openapi.v1.yaml")
    args = ap.parse_args()
    repo = Path(__file__).resolve().parents[2]
    spec_path = Path(args.spec)
    if not spec_path.is_absolute():
        spec_path = repo / spec_path
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    errors = []
    checked = 0
    for label, schema, value in _iter_examples(spec):
        checked += 1
        try:
            Draft202012Validator(schema).validate(value)
        except jsonschema.ValidationError as e:
            errors.append(f"{label}: {e.message} (at {'/'.join(map(str, e.path))})")
    print(f"checked {checked} examples")
    if errors:
        print(f"{len(errors)} example(s) INVALID:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("all examples valid against their schemas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
