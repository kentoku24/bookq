#!/usr/bin/env python3
"""Detect wire-breaking changes between base and head API specs.

Compares base vs head revisions of ``api/openapi.v1.yaml`` (OpenAPI 3.1)
and ``api/asyncapi.yaml`` (AsyncAPI 3.0) and exits 1 when a change would
break already-shipped clients (old iOS binaries from App Store review lag,
cached PWA shells). Spectral only lints shape and Schemathesis only checks
"current implementation <=> current spec", so a spec+implementation pair
broken simultaneously (e.g. a new ``required`` entry) slips through both —
this check closes that gap (#232).

Breaking (exit 1):

- removed path / operation / channel / message / response status
- added required query/header/path parameter, or ``required: false -> true``,
  removed required parameter, removed parameter schema
- newly required requestBody
- removed request/response content media type, removed request/response schema
- removed response header, response header ``required`` flip or schema change
- changed operationId (generated clients bind to it)
- AsyncAPI: channel ``address``/``bindings`` change or removed ``servers``
  entry, operation ``action``/``channel``/message-ref change, message
  ``contentType`` change, removed message ``headers`` or payload
- schema: added ``required`` entry, removed property, removed enum value,
  added/removed/changed ``const``, changed ``type``,
  ``additionalProperties`` tightened to ``false``, removed oneOf/anyOf
  variant, changed ``discriminator``

Additive changes pass: optional property, new enum value, new path /
operation / response status, new oneOf/anyOf variant, new operation
message, relaxed ``required``.

Local ``$ref`` chains are resolved before comparison — siblings next to
``$ref`` (legal in 3.1) are merged over the target, so moving a schema
between ``components`` entries or adding a sibling without changing its
shape is clean. Unkeyed oneOf/anyOf/allOf members align by canonical JSON
order, not position. The walk is direction-agnostic on purpose: it may
flag a request-side relaxation a server owner would call safe (e.g.
dropping a ``required`` entry still passes; tightening request enums
fails). Constraint tweaks (``minLength``, ``pattern``, numeric bounds)
are out of scope — shape lint (Spectral) and contract tests
(Schemathesis) own those.

Known gaps (deliberate scope, revisit if they bite): AsyncAPI
``servers``/``bindings`` are compared shallowly (removed server or any
bindings change) — deeper protocol-field diffs would just echo the raw
change; message ``traits``/``correlationId`` are unchecked; parameter
diffs run at both path scope and the merged operation scope, so one
path-level change can report at two locations; unkeyed-variant ``loc``
indexes label by first-equal index when duplicate shapes exist
(cosmetic only).

Usage (CI diffs the PR base revision against the working tree)::

    python scripts/api_breaking_check.py \\
        --base-openapi /tmp/base-openapi.yaml --head-openapi api/openapi.v1.yaml \\
        --base-asyncapi /tmp/base-asyncapi.yaml --head-asyncapi api/asyncapi.yaml

``--allow-breaking`` reports the findings but still exits 0. CI passes it
only when the PR carries the ``api-breaking`` label (see api/README.md
"破壊的変更の例外運用").
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print(
        "error: PyYAML is required "
        "(uv run --with pyyaml python scripts/api_breaking_check.py ...)",
        file=sys.stderr,
    )
    sys.exit(2)

HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}


# --------------------------------------------------------------------------
# $ref resolution
# --------------------------------------------------------------------------

def resolve(node, root):
    """Follow internal ``$ref: '#/...'`` chains; return the target node.

    OpenAPI 3.1 permits siblings next to ``$ref`` — they constrain the
    referenced object, so they are merged over a copy of the target
    rather than compared raw. Without this, ``{$ref: T, description: d}``
    reads as an opaque dict and produces both false positives (added
    sibling looks like a shape change) and false negatives (the $ref
    itself swapping targets goes unnoticed). External refs stay opaque.
    """
    seen = set()
    while (
        isinstance(node, dict)
        and isinstance(node.get("$ref"), str)
        and node["$ref"].startswith("#/")
    ):
        ref = node["$ref"]
        if ref in seen:
            # Cyclic ref: drop the link, compare whatever siblings remain.
            return {k: v for k, v in node.items() if k != "$ref"}
        seen.add(ref)
        target = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                return node  # unresolvable: opaque, compare raw below
            target = target[part]
        if len(node) > 1:
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            node = {**target, **siblings} if isinstance(target, dict) else siblings
        else:
            node = target
    return node


# --------------------------------------------------------------------------
# schema comparison
# --------------------------------------------------------------------------

def _canon_key(node):
    """Canonical sort key for unkeyed combinator members."""
    return json.dumps(node, sort_keys=True, default=str)


def _norm_type(value):
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return sorted(value)


def _branch_key(schema):
    """Stable alignment key for oneOf/anyOf variants.

    Our discriminated unions key branches on ``properties.<name>.const``
    (e.g. ``grant_type`` / request ``type``); fall back to a top-level
    ``const``. Returns None when the branch carries no discriminator, in
    which case callers align leftovers by index.
    """
    if not isinstance(schema, dict):
        return None
    if "const" in schema:
        return ("const", json.dumps(schema["const"], sort_keys=True))
    props = schema.get("properties")
    if isinstance(props, dict):
        for name in ("type", "grant_type", "kind"):
            sub = props.get(name)
            if isinstance(sub, dict) and "const" in sub:
                return (
                    f"properties.{name}.const",
                    json.dumps(sub["const"], sort_keys=True),
                )
    return None


class Differ:
    def __init__(self, base_root, head_root):
        self.base_root = base_root
        self.head_root = head_root
        self.findings = []
        self._active = set()  # (id(base), id(head)) pairs on the stack

    def breaking(self, loc, msg):
        self.findings.append(f"{loc}: {msg}")

    # -- generic schema walk -------------------------------------------

    def diff_schema(self, base, head, loc):
        base = resolve(base, self.base_root)
        head = resolve(head, self.head_root)
        pair = (id(base), id(head))
        if pair in self._active:
            return  # recursive schema (e.g. TreeNode): already being compared
        self._active.add(pair)
        try:
            self._diff_schema_inner(base, head, loc)
        finally:
            self._active.discard(pair)

    def _diff_schema_inner(self, base, head, loc):
        # Boolean schemas (OpenAPI 3.1 / JSON Schema): true allows anything.
        if isinstance(base, bool) or isinstance(head, bool):
            if base is True and head is False:
                self.breaking(loc, "schema tightened from `true` to `false`")
            return
        if not isinstance(base, dict) or not isinstance(head, dict):
            if base != head:
                self.breaking(loc, f"value changed from {base!r} to {head!r}")
            return

        # type
        bt, ht = _norm_type(base.get("type")), _norm_type(head.get("type"))
        if bt != ht:
            self.breaking(loc, f"type changed from {bt} to {ht}")

        # const (wire-stable literals)
        if "const" in base or "const" in head:
            if base.get("const") != head.get("const"):
                self.breaking(
                    loc,
                    f"const changed from {base.get('const')!r} "
                    f"to {head.get('const')!r}",
                )

        # enum: removals break, additions are fine
        if isinstance(base.get("enum"), list) or isinstance(head.get("enum"), list):
            before = base.get("enum") or []
            after = head.get("enum") or []
            removed = [v for v in before if v not in after]
            if removed:
                self.breaking(loc, f"enum values removed: {removed}")

        # required: additions break, removals pass
        before_req = base.get("required") or []
        after_req = head.get("required") or []
        added_req = [r for r in after_req if r not in before_req]
        for name in added_req:
            self.breaking(loc, f"required added: {name!r}")

        # properties: removals break; common keys recurse
        base_props = base.get("properties") or {}
        head_props = head.get("properties") or {}
        if isinstance(base_props, dict) and isinstance(head_props, dict):
            for name in base_props:
                if name not in head_props:
                    self.breaking(loc, f"property removed: {name!r}")
            for name in base_props:
                if name in head_props:
                    self.diff_schema(
                        base_props[name], head_props[name], f"{loc}.properties.{name}"
                    )

        # additionalProperties tightened to false
        ba = base.get("additionalProperties", True)
        ha = head.get("additionalProperties", True)
        if isinstance(ha, dict) and isinstance(ba, dict):
            self.diff_schema(ba, ha, f"{loc}.additionalProperties")
        elif ha is False and ba is not False:
            self.breaking(loc, "additionalProperties tightened to `false`")

        # items / prefixItems
        if "items" in base or "items" in head:
            if isinstance(base.get("items"), dict) and isinstance(
                head.get("items"), dict
            ):
                self.diff_schema(base["items"], head["items"], f"{loc}.items")
        for key in ("prefixItems",):
            if isinstance(base.get(key), list) or isinstance(head.get(key), list):
                bl, hl = base.get(key) or [], head.get(key) or []
                if len(bl) > len(hl):
                    self.breaking(
                        loc, f"{key} shortened ({len(bl)} -> {len(hl)} entries)"
                    )
                for i, (b, h) in enumerate(zip(bl, hl)):
                    self.diff_schema(b, h, f"{loc}.{key}[{i}]")

        # combinators
        for key in ("oneOf", "anyOf"):
            if isinstance(base.get(key), list) or isinstance(head.get(key), list):
                self._diff_options(base.get(key) or [], head.get(key) or [],
                                   loc, key)
        if isinstance(base.get("allOf"), list) or isinstance(head.get("allOf"), list):
            # Canonical order: allOf members carry no stable key, so an
            # insertion mid-list would otherwise shift-align every tail
            # member and report phantom changes.
            bl = sorted(base.get("allOf") or [], key=_canon_key)
            hl = sorted(head.get("allOf") or [], key=_canon_key)
            if len(bl) > len(hl):
                self.breaking(loc, f"allOf shortened ({len(bl)} -> {len(hl)} entries)")
            for i, (b, h) in enumerate(zip(bl, hl)):
                self.diff_schema(b, h, f"{loc}.allOf[{i}]")

        # discriminator change reroutes validation
        if base.get("discriminator") != head.get("discriminator"):
            if "discriminator" in base or "discriminator" in head:
                self.breaking(
                    loc,
                    f"discriminator changed from {base.get('discriminator')!r} "
                    f"to {head.get('discriminator')!r}",
                )

    def _diff_options(self, base_opts, head_opts, loc, key):
        """Align oneOf/anyOf variants by discriminator key, then canonically.

        Unkeyed variants (no discriminator) are aligned by canonical JSON
        order on both sides — index alignment mis-pairs every variant past
        a mid-list insertion, reporting phantom `type changed`/`property
        removed` findings on an additive change (issue #232 review).
        """
        pairs = []
        orphan_base = []

        head_by_key = {}
        head_unkeyed = []
        for h in head_opts:
            k = _branch_key(resolve(h, self.head_root))
            if k is None:
                head_unkeyed.append(h)
            else:
                head_by_key.setdefault(k, []).append(h)
        head_unkeyed.sort(key=_canon_key)

        leftover_base = []
        for b in base_opts:
            k = _branch_key(resolve(b, self.base_root))
            if k is not None and k in head_by_key and head_by_key[k]:
                pairs.append((b, head_by_key[k].pop(0), k))
            else:
                leftover_base.append((b, k))

        orphan_base = [(b, k) for b, k in leftover_base if k is not None]
        for b in sorted(
            (b for b, k in leftover_base if k is None), key=_canon_key
        ):
            if head_unkeyed:
                pairs.append((b, head_unkeyed.pop(0), None))
            else:
                orphan_base.append((b, None))

        for b, k in orphan_base:
            label = f" ({k[0]}={json.loads(k[1])})" if k else ""
            self.breaking(loc, f"{key} variant removed{label}")

        for b, h, k in pairs:
            if k is not None:
                _, kval = k
                seg = f"{key}[{k[0]}={json.loads(kval)}]"
            else:
                seg = f"{key}[{base_opts.index(b)}]"
            self.diff_schema(b, h, f"{loc}.{seg}")

    # -- parameters -----------------------------------------------------

    def diff_parameters(self, base_params, head_params, loc):
        base_by_key = _params_by_key(base_params, self.base_root)
        head_by_key = _params_by_key(head_params, self.head_root)
        for key, b in base_by_key.items():
            if key not in head_by_key:
                if isinstance(b, dict) and b.get("required") is True:
                    self.breaking(loc, f"required parameter removed: {key}")
                continue
            h = head_by_key[key]
            if not isinstance(b, dict) or not isinstance(h, dict):
                continue
            if b.get("required") is not True and h.get("required") is True:
                self.breaking(loc, f"parameter became required: {key}")
            if "schema" in b or "schema" in h:
                if "schema" in b and "schema" in h:
                    self.diff_schema(b["schema"], h["schema"], f"{loc}.{key}.schema")
                elif "schema" not in h:
                    self.breaking(loc, f"parameter schema removed: {key}")
        for key, h in head_by_key.items():
            if key not in base_by_key:
                if isinstance(h, dict) and h.get("required") is True:
                    self.breaking(loc, f"required parameter added: {key}")


def _params_by_key(params, root=None):
    """Key parameters by ``in:name``; ``$ref``-ed entries (which carry no
    ``name`` of their own, e.g. ``#/components/parameters/*``) are resolved
    first so a required flip inside a shared parameter is still caught."""
    out = {}
    if isinstance(params, list):
        for p in params:
            if isinstance(p, dict) and "name" not in p and root is not None:
                p = resolve(p, root)
            if isinstance(p, dict) and "name" in p:
                out[f"{p.get('in', 'query')}:{p['name']}"] = p
    return out


# --------------------------------------------------------------------------
# OpenAPI
# --------------------------------------------------------------------------

def diff_openapi(base, head):
    """Return a list of breaking-change findings (empty == compatible)."""
    d = Differ(base, head)
    base_paths = base.get("paths") or {}
    head_paths = head.get("paths") or {}
    for path, b_item in base_paths.items():
        loc = f"openapi.paths.{path}"
        h_item = head_paths.get(path)
        if h_item is None:
            d.breaking(loc, "path removed")
            continue
        if not isinstance(b_item, dict) or not isinstance(h_item, dict):
            continue
        d.diff_parameters(
            b_item.get("parameters"), h_item.get("parameters"), f"{loc}.parameters"
        )
        for method in HTTP_METHODS:
            if method not in b_item:
                continue
            op_loc = f"{loc}.{method}"
            h_op = h_item.get(method)
            if h_op is None:
                d.breaking(op_loc, "operation removed")
                continue
            _diff_operation(d, b_item, b_item[method], h_item, h_op, op_loc, base, head)
    return sorted(set(d.findings))


def _diff_operation(d, b_item, b_op, h_item, h_op, loc, base_root, head_root):
    b_op = resolve(b_op, base_root)
    h_op = resolve(h_op, head_root)
    if not isinstance(b_op, dict) or not isinstance(h_op, dict):
        return
    if "operationId" in b_op or "operationId" in h_op:
        if b_op.get("operationId") != h_op.get("operationId"):
            d.breaking(
                loc,
                f"operationId changed from {b_op.get('operationId')!r} "
                f"to {h_op.get('operationId')!r}",
            )
    # path-level + operation-level parameters merged (operation wins per key)
    b_params = _params_by_key(b_item.get("parameters"), base_root) if isinstance(b_item, dict) else {}
    b_params.update(_params_by_key(b_op.get("parameters"), base_root))
    h_params = _params_by_key(h_item.get("parameters"), head_root) if isinstance(h_item, dict) else {}
    h_params.update(_params_by_key(h_op.get("parameters"), head_root))
    d.diff_parameters(list(b_params.values()), list(h_params.values()), f"{loc}.parameters")

    # requestBody
    b_body = resolve(b_op.get("requestBody"), base_root) if "requestBody" in b_op else None
    h_body = resolve(h_op.get("requestBody"), head_root) if "requestBody" in h_op else None
    if isinstance(h_body, dict) and h_body.get("required") is True:
        if not (isinstance(b_body, dict) and b_body.get("required") is True):
            d.breaking(loc, "requestBody became required")
    if isinstance(b_body, dict) and isinstance(h_body, dict):
        b_content = b_body.get("content") or {}
        h_content = h_body.get("content") or {}
        for media, b_media in b_content.items():
            if media not in h_content:
                d.breaking(loc, f"request media type removed: {media}")
                continue
            b_schema = (b_media or {}).get("schema")
            h_media = h_content[media] or {}
            h_schema = h_media.get("schema")
            if b_schema is not None and h_schema is None:
                d.breaking(loc, f"request schema removed: {media}")
            elif b_schema is not None:
                d.diff_schema(b_schema, h_schema, f"{loc}.requestBody.{media}")

    # responses: removals break, additions pass
    b_resp = b_op.get("responses") or {}
    h_resp = h_op.get("responses") or {}
    for status, b_r in b_resp.items():
        r_loc = f"{loc}.responses.{status}"
        h_r = h_resp.get(status)
        if h_r is None:
            d.breaking(r_loc, "response status removed")
            continue
        b_r = resolve(b_r, base_root)
        h_r = resolve(h_r, head_root)
        if not isinstance(b_r, dict) or not isinstance(h_r, dict):
            continue
        b_content = b_r.get("content") or {}
        h_content = h_r.get("content") or {}
        for media, b_media in b_content.items():
            if media not in h_content:
                d.breaking(r_loc, f"response media type removed: {media}")
                continue
            b_schema = (b_media or {}).get("schema")
            h_media = h_content[media] or {}
            h_schema = h_media.get("schema")
            if b_schema is not None and h_schema is None:
                d.breaking(r_loc, f"response schema removed: {media}")
            elif b_schema is not None:
                d.diff_schema(b_schema, h_schema, f"{r_loc}.{media}")
        _diff_response_headers(d, b_r, h_r, r_loc, base_root, head_root)


def _diff_response_headers(d, b_r, h_r, loc, base_root, head_root):
    """Response headers are wire contract too — `Retry-After` on the 202s
    is how clients learn to poll. A removed header or `required` flip is
    breaking; additions pass."""
    b_headers = b_r.get("headers") or {}
    h_headers = h_r.get("headers") or {}
    if not isinstance(b_headers, dict) or not isinstance(h_headers, dict):
        return
    for name, b_h in b_headers.items():
        h_loc = f"{loc}.headers.{name}"
        h_h = h_headers.get(name)
        if h_h is None:
            d.breaking(h_loc, "response header removed")
            continue
        b_h = resolve(b_h, base_root)
        h_h = resolve(h_h, head_root)
        if not isinstance(b_h, dict) or not isinstance(h_h, dict):
            continue
        if b_h.get("required") is not True and h_h.get("required") is True:
            d.breaking(h_loc, "response header became required")
        if b_h.get("schema") is not None and h_h.get("schema") is not None:
            d.diff_schema(b_h["schema"], h_h["schema"], f"{h_loc}.schema")
        elif b_h.get("schema") is not None:
            d.breaking(h_loc, "response header schema removed")


# --------------------------------------------------------------------------
# AsyncAPI
# --------------------------------------------------------------------------

def diff_asyncapi(base, head):
    """Return a list of breaking-change findings (empty == compatible)."""
    d = Differ(base, head)
    base_channels = base.get("channels") or {}
    head_channels = head.get("channels") or {}
    for name, b_ch in base_channels.items():
        loc = f"asyncapi.channels.{name}"
        h_ch = head_channels.get(name)
        if h_ch is None:
            d.breaking(loc, "channel removed")
            continue
        b_ch = resolve(b_ch, d.base_root)
        h_ch = resolve(h_ch, d.head_root)
        if not isinstance(b_ch, dict) or not isinstance(h_ch, dict):
            continue
        # Wire identity: the address IS the WebSocket path clients dial,
        # and bindings pin the protocol shape — changing either breaks
        # already-shipped clients even when the channel key survives.
        if b_ch.get("address") != h_ch.get("address"):
            d.breaking(
                loc,
                f"channel address changed from {b_ch.get('address')!r} "
                f"to {h_ch.get('address')!r}",
            )
        if b_ch.get("bindings") != h_ch.get("bindings"):
            d.breaking(loc, "channel bindings changed")
        b_servers = b_ch.get("servers") or []
        h_servers = h_ch.get("servers") or []
        for s in b_servers:
            if s not in h_servers:
                d.breaking(loc, f"channel server removed: {s!r}")
        b_msgs = b_ch.get("messages") or {}
        h_msgs = h_ch.get("messages") or {}
        if isinstance(b_msgs, dict) and isinstance(h_msgs, dict):
            # AsyncAPI 3.0 map style: keys are the stable message names.
            for key, b_m in b_msgs.items():
                if key not in h_msgs:
                    d.breaking(loc, f"message removed: {key}")
                    continue
                b_m = resolve(b_m, d.base_root)
                h_m = resolve(h_msgs[key], d.head_root)
                if isinstance(b_m, dict) and isinstance(h_m, dict):
                    _diff_message_payload(d, b_m, h_m, loc, key)
        else:
            # List style (2.x): align by resolved name/title.
            b_list = b_msgs if isinstance(b_msgs, list) else list(b_msgs.values())
            h_list = h_msgs if isinstance(h_msgs, list) else list(h_msgs.values())
            b_names = [_message_name(m, d.base_root) for m in b_list]
            h_names = [_message_name(m, d.head_root) for m in h_list]
            for m, m_name in zip(b_list, b_names):
                if m_name not in h_names:
                    d.breaking(loc, f"message removed: {m_name}")
                    continue
                h_m = h_list[h_names.index(m_name)]
                m = resolve(m, d.base_root)
                h_m = resolve(h_m, d.head_root)
                if isinstance(m, dict) and isinstance(h_m, dict):
                    _diff_message_payload(d, m, h_m, loc, m_name)

    base_ops = base.get("operations") or {}
    head_ops = head.get("operations") or {}
    for op_id, b_op in base_ops.items():
        loc = f"asyncapi.operations.{op_id}"
        h_op = head_ops.get(op_id)
        if h_op is None:
            d.breaking(loc, "operation removed")
            continue
        if not isinstance(b_op, dict) or not isinstance(h_op, dict):
            continue
        # `action` flips the wire direction — send<->receive is breaking.
        if b_op.get("action") != h_op.get("action"):
            d.breaking(
                loc,
                f"operation action changed from {b_op.get('action')!r} "
                f"to {h_op.get('action')!r}",
            )
        # The operation→channel link is how clients learn where a
        # message flows; re-pointing it silently rehomes the traffic.
        b_chan = _ref_id(b_op.get("channel"))
        h_chan = _ref_id(h_op.get("channel"))
        if b_chan != h_chan:
            d.breaking(
                loc, f"operation channel changed from {b_chan!r} to {h_chan!r}"
            )
        # operation→message set: dropping a message id unsubscribes
        # clients that dispatch on it (reader-sync `error`/`state`/...).
        b_mrefs = {_ref_id(m) for m in b_op.get("messages") or []}
        h_mrefs = {_ref_id(m) for m in h_op.get("messages") or []}
        for mref in sorted(b_mrefs - h_mrefs):
            d.breaking(loc, f"operation message removed: {mref}")
    return sorted(set(d.findings))


def _ref_id(node):
    """Stable identity for an operation channel/message link — the spec
    writes these as `{$ref: '#/channels/...'}`; inline objects fall back
    to canonical JSON."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            return ref
    return _canon_key(node)


def _message_name(msg, root):
    msg = resolve(msg, root)
    if isinstance(msg, dict):
        for key in ("name", "title"):
            if isinstance(msg.get(key), str):
                return msg[key]
    return json.dumps(msg, sort_keys=True, default=str)


def _diff_message_payload(d, base_msg, head_msg, loc, name):
    mloc = f"{loc}.messages.{name}"
    if "payload" in base_msg or "payload" in head_msg:
        if "payload" in base_msg and "payload" in head_msg:
            d.diff_schema(base_msg["payload"], head_msg["payload"], mloc)
        elif "payload" not in head_msg:
            d.breaking(loc, f"message payload removed: {name}")
    # Envelope contract: a contentType flip reparses the frame, and the
    # headers schema is part of what clients send/read.
    b_ct = base_msg.get("contentType")
    h_ct = head_msg.get("contentType")
    if (b_ct is not None or h_ct is not None) and b_ct != h_ct:
        d.breaking(
            mloc,
            f"message contentType changed from {b_ct!r} to {h_ct!r}",
        )
    if "headers" in base_msg or "headers" in head_msg:
        if "headers" in base_msg and "headers" in head_msg:
            d.diff_schema(
                resolve(base_msg["headers"], d.base_root),
                resolve(head_msg["headers"], d.head_root),
                f"{mloc}.headers",
            )
        elif "headers" not in head_msg:
            d.breaking(mloc, "message headers removed")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def check_files(base_openapi, head_openapi, base_asyncapi, head_asyncapi):
    findings = []
    if base_openapi is not None and head_openapi is not None:
        with open(base_openapi, encoding="utf-8") as f:
            base = yaml.safe_load(f)
        with open(head_openapi, encoding="utf-8") as f:
            head = yaml.safe_load(f)
        findings += diff_openapi(base or {}, head or {})
    if base_asyncapi is not None and head_asyncapi is not None:
        with open(base_asyncapi, encoding="utf-8") as f:
            base = yaml.safe_load(f)
        with open(head_asyncapi, encoding="utf-8") as f:
            head = yaml.safe_load(f)
        findings += diff_asyncapi(base or {}, head or {})
    return sorted(set(findings))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-openapi", type=Path, default=None)
    parser.add_argument("--head-openapi", type=Path, default=None)
    parser.add_argument("--base-asyncapi", type=Path, default=None)
    parser.add_argument("--head-asyncapi", type=Path, default=None)
    parser.add_argument(
        "--allow-breaking",
        action="store_true",
        help="report findings but exit 0 (CI sets this for `api-breaking` PRs)",
    )
    args = parser.parse_args(argv)

    if (args.base_openapi is None) != (args.head_openapi is None):
        parser.error("--base-openapi and --head-openapi must be given together")
    if (args.base_asyncapi is None) != (args.head_asyncapi is None):
        parser.error("--base-asyncapi and --head-asyncapi must be given together")
    if args.base_openapi is None and args.base_asyncapi is None:
        parser.error("nothing to compare: pass at least one spec pair")

    for path in (
        args.base_openapi,
        args.head_openapi,
        args.base_asyncapi,
        args.head_asyncapi,
    ):
        if path is not None and not path.is_file():
            print(f"error: file not found: {path}", file=sys.stderr)
            return 2

    findings = check_files(
        args.base_openapi, args.head_openapi, args.base_asyncapi, args.head_asyncapi
    )
    if not findings:
        print("api-breaking-check: no breaking changes detected")
        return 0
    print(f"api-breaking-check: {len(findings)} breaking change(s) detected:")
    for finding in findings:
        print(f"  BREAKING {finding}")
    if args.allow_breaking:
        print("api-breaking-check: allowed via --allow-breaking (`api-breaking` label)")
        return 0
    print(
        "api-breaking-check: FAIL — remove the breaking change, or follow "
        "api/README.md '破壊的変更の例外運用' (label `api-breaking` + new version).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
