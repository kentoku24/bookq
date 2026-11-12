#!/usr/bin/env python3
"""Extract named `examples:` from api/openapi.v1.yaml into api/dist/examples.json.

The bundle is the single fixture source for client tests (#237):
  - PWA:  tests/spec_examples.py loads it and serves it via page.route
  - iOS:  ios/BookqTests/SpecExamples.swift loads it for StubURLProtocol

Usage:
    python3 scripts/api/extract-examples.py [--spec api/openapi.v1.yaml]
        [--out api/dist/examples.json]

Single `example:` entries (parameter examples) are also collected with
name "default" so the bundle covers the whole spec surface.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("pyyaml required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2)


def _walk_content(node, emit, ctx):
    """Collect examples under an OpenAPI media-type object."""
    if not isinstance(node, dict):
        return
    for k, v in node.items():
        if k == "examples" and isinstance(v, dict):
            for name, ex in v.items():
                if isinstance(ex, dict) and "value" in ex:
                    emit(ctx, name, ex.get("summary", ""), ex["value"])
                elif not isinstance(ex, dict):
                    emit(ctx, name, "", ex)
        elif k == "example" and "examples" not in node:
            emit(ctx, "default", "", v)
        elif isinstance(v, (dict, list)):
            _walk_more(v, emit, ctx)


def _walk_more(node, emit, ctx):
    if isinstance(node, dict):
        _walk_content(node, emit, ctx)
    elif isinstance(node, list):
        for item in node:
            _walk_more(item, emit, ctx)


def _deref_response(resp, spec):
    """Follow a top-level `$ref` into components/responses (Prism parity)."""
    if isinstance(resp, dict) and set(resp.keys()) == {"$ref"}:
        ref = resp["$ref"]
        if ref.startswith("#/components/responses/"):
            name = ref.split("/")[-1]
            target = (spec.get("components") or {}).get("responses", {}).get(name)
            if isinstance(target, dict):
                return target, name
    return resp, ""


def extract(spec_path: Path):
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    out: list[dict] = []
    seen = set()

    def emit(ctx, name, summary, value):
        key = (ctx.get("operationId"), ctx.get("status"), ctx.get("media"), name,
               json.dumps(value, sort_keys=True, default=str))
        if key in seen:
            return
        seen.add(key)
        out.append({**ctx, "name": name, "summary": summary, "value": value})

    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            if not isinstance(op, dict) or method.startswith("x-"):
                continue
            op_id = op.get("operationId", f"{method} {path}")
            for section in ("responses",):
                for status, resp in (op.get(section) or {}).items():
                    if not isinstance(resp, dict):
                        continue
                    resp_eff, shared = _deref_response(resp, spec)
                    ctx = {"operationId": op_id, "method": method,
                           "path": path, "status": str(status),
                           "section": "response"}
                    if shared:
                        ctx["sharedResponse"] = shared
                    content = resp_eff.get("content") or {}
                    for media, media_obj in content.items():
                        if not isinstance(media_obj, dict):
                            continue
                        c2 = {**ctx, "media": media}
                        if "examples" in media_obj and isinstance(media_obj["examples"], dict):
                            for name, ex in media_obj["examples"].items():
                                if isinstance(ex, dict) and "value" in ex:
                                    emit(c2, name, ex.get("summary", ""), ex["value"])
                        elif "example" in media_obj:
                            emit(c2, "default", "", media_obj["example"])
            req = (op.get("requestBody") or {}).get("content") or {}
            for media, media_obj in req.items():
                if not isinstance(media_obj, dict):
                    continue
                ctx = {"operationId": op_id, "method": method, "path": path,
                       "status": "request", "section": "request", "media": media}
                if "examples" in media_obj and isinstance(media_obj["examples"], dict):
                    for name, ex in media_obj["examples"].items():
                        if isinstance(ex, dict) and "value" in ex:
                            emit(ctx, name, ex.get("summary", ""), ex["value"])
                elif "example" in media_obj:
                    emit(ctx, "default", "", media_obj["example"])

    return {"version": 1, "spec": str(spec_path), "examples": out}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default="api/openapi.v1.yaml")
    ap.add_argument("--out", default="api/dist/examples.json")
    args = ap.parse_args()
    repo = Path(__file__).resolve().parents[2]
    spec_path = Path(args.spec)
    if not spec_path.is_absolute():
        spec_path = repo / spec_path
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = repo / out_path
    bundle = extract(spec_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"wrote {out_path} ({len(bundle['examples'])} examples)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
