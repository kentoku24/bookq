"""Unit tests for scripts/api_breaking_check.py (#232).

Drives diff_openapi/diff_asyncapi with in-memory spec fragments plus a
self-diff of the real api/*.yaml (guards against false positives on the
current contracts), and asserts the CLI exit contract (0 clean / 1 breaking
/ 0 with --allow-breaking).
"""
import copy
import importlib.util
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "api_breaking_check", REPO_ROOT / "scripts" / "api_breaking_check.py"
)
api_breaking_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api_breaking_check)


def _openapi(*, schema=None, path_item=None):
    """Minimal OpenAPI doc with one schema under test."""
    return {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1.0.0"},
        "paths": {
            "/api/v1/things": {
                "get": {
                    "operationId": "getThings",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {"schema": schema or {}}
                            },
                        }
                    },
                }
            }
        },
    }


def _thing_schema():
    return {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": ["a", "b"]},
        },
    }


def _asyncapi(payload):
    return {
        "asyncapi": "3.0.0",
        "info": {"title": "t", "version": "1.0.0"},
        "channels": {
            "readerSync": {
                "messages": {"thing": {"payload": payload}},
            }
        },
        "operations": {"send": {"action": "send"}},
    }


# -- acceptance: the four must-fail cases -----------------------------------

def test_required_addition_breaks():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["required"].append("kind")
    findings = api_breaking_check.diff_openapi(base, head)
    assert any("required added: 'kind'" in f for f in findings)


def test_property_removal_breaks():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    del head["paths"]["/api/v1/things"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["kind"]
    findings = api_breaking_check.diff_openapi(base, head)
    assert any("property removed: 'kind'" in f for f in findings)


def test_enum_value_removal_breaks():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["kind"]["enum"].remove("b")
    findings = api_breaking_check.diff_openapi(base, head)
    assert any("enum values removed: ['b']" in f for f in findings)


def test_type_change_breaks():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["name"] = {"type": "integer"}
    findings = api_breaking_check.diff_openapi(base, head)
    assert any("type changed" in f for f in findings)


# -- acceptance: additive changes pass --------------------------------------

def test_optional_property_addition_passes():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["nick"] = {"type": "string"}
    assert api_breaking_check.diff_openapi(base, head) == []


def test_enum_value_addition_passes():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["kind"]["enum"].append("c")
    assert api_breaking_check.diff_openapi(base, head) == []


# -- structural removals -----------------------------------------------------

def test_path_removal_breaks():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    del head["paths"]["/api/v1/things"]
    assert any("path removed" in f for f in api_breaking_check.diff_openapi(base, head))


def test_operation_id_change_breaks():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["operationId"] = "listThings"
    assert any("operationId changed" in f for f in api_breaking_check.diff_openapi(base, head))


def test_required_parameter_addition_breaks():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["parameters"] = [
        {"name": "strict", "in": "query", "required": True,
         "schema": {"type": "string"}}
    ]
    assert any("required parameter added" in f for f in api_breaking_check.diff_openapi(base, head))


def test_optional_parameter_addition_passes():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["parameters"] = [
        {"name": "verbose", "in": "query", "schema": {"type": "boolean"}}
    ]
    assert api_breaking_check.diff_openapi(base, head) == []


def test_oneof_variant_removal_breaks():
    base = _openapi(
        schema={
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "grant_type": {"const": "authorization_code"},
                    },
                },
                {
                    "type": "object",
                    "properties": {
                        "grant_type": {"const": "refresh_token"},
                    },
                },
            ]
        }
    )
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["oneOf"].pop()
    findings = api_breaking_check.diff_openapi(base, head)
    assert any("variant removed" in f for f in findings)


# -- AsyncAPI -----------------------------------------------------------------

def test_asyncapi_required_addition_breaks():
    base = _asyncapi(_thing_schema())
    head = copy.deepcopy(base)
    head["channels"]["readerSync"]["messages"]["thing"]["payload"][
        "required"
    ].append("kind")
    assert api_breaking_check.diff_asyncapi(base, head) != []


def test_asyncapi_optional_addition_passes():
    base = _asyncapi(_thing_schema())
    head = copy.deepcopy(base)
    head["channels"]["readerSync"]["messages"]["thing"]["payload"][
        "properties"
    ]["nick"] = {"type": "string"}
    assert api_breaking_check.diff_asyncapi(base, head) == []


def test_asyncapi_message_removal_breaks():
    base = _asyncapi(_thing_schema())
    head = copy.deepcopy(base)
    del head["channels"]["readerSync"]["messages"]["thing"]
    assert any("message removed" in f for f in api_breaking_check.diff_asyncapi(base, head))


# -- real contracts: self-diff must stay clean ---------------------------------

def test_real_specs_self_diff_is_clean():
    openapi = yaml.safe_load((REPO_ROOT / "api" / "openapi.v1.yaml").read_text())
    asyncapi = yaml.safe_load((REPO_ROOT / "api" / "asyncapi.yaml").read_text())
    assert api_breaking_check.diff_openapi(openapi, copy.deepcopy(openapi)) == []
    assert api_breaking_check.diff_asyncapi(asyncapi, copy.deepcopy(asyncapi)) == []


# -- review findings (#232 review): $ref siblings, unkeyed combinators,
#    $ref-ed parameters, response headers, media schema removal, and
#    deeper AsyncAPI coverage ---------------------------------------------

def _openapi_with_components(schema):
    doc = _openapi(schema=schema)
    doc["components"] = {
        "schemas": {
            "Thing": _thing_schema(),
            "Other": {"type": "string"},
        }
    }
    return doc


def _head_schema(doc):
    return doc["paths"]["/api/v1/things"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]


def test_ref_sibling_addition_is_clean():
    """OpenAPI 3.1 allows `{$ref: T, description: d}` — adding the sibling
    is not a wire change."""
    base = _openapi_with_components({"$ref": "#/components/schemas/Thing"})
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] = {
        "$ref": "#/components/schemas/Thing",
        "description": "annotated",
    }
    assert api_breaking_check.diff_openapi(base, head) == []


def test_ref_target_swap_through_sibling_detected():
    """Both sides carrying a sibling must not hide a $ref retarget."""
    base = _openapi_with_components(
        {"$ref": "#/components/schemas/Thing", "description": "d"}
    )
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] = {
        "$ref": "#/components/schemas/Other",
        "description": "d",
    }
    assert any(
        "type changed" in f for f in api_breaking_check.diff_openapi(base, head)
    )


def test_unkeyed_oneof_mid_insertion_is_clean():
    """A variant added before existing unkeyed branches must not
    shift-align and report phantom changes."""
    variant_a = {"type": "object", "properties": {"a": {"type": "string"}}}
    variant_b = {"type": "object", "properties": {"b": {"type": "integer"}}}
    base = _openapi(schema={"oneOf": [variant_a, variant_b]})
    head = copy.deepcopy(base)
    _head_schema(head)["oneOf"].insert(
        0, {"type": "object", "properties": {"c": {"type": "boolean"}}}
    )
    assert api_breaking_check.diff_openapi(base, head) == []


def test_unkeyed_oneof_variant_change_still_detected():
    variant_a = {"type": "object", "properties": {"a": {"type": "string"}}}
    variant_b = {"type": "object", "properties": {"b": {"type": "integer"}}}
    base = _openapi(schema={"oneOf": [variant_a, variant_b]})
    head = copy.deepcopy(base)
    _head_schema(head)["oneOf"][1] = {
        "type": "object",
        "properties": {"b": {"type": "string"}},
    }
    assert any(
        "type changed" in f for f in api_breaking_check.diff_openapi(base, head)
    )


def test_ref_parameter_required_flip_breaks():
    doc = _openapi(schema=_thing_schema())
    doc["components"] = {
        "parameters": {
            "Strict": {"name": "strict", "in": "query",
                       "schema": {"type": "string"}}
        }
    }
    doc["paths"]["/api/v1/things"]["get"]["parameters"] = [
        {"$ref": "#/components/parameters/Strict"}
    ]
    head = copy.deepcopy(doc)
    head["components"]["parameters"]["Strict"]["required"] = True
    assert any(
        "parameter became required" in f
        for f in api_breaking_check.diff_openapi(doc, head)
    )


def test_response_header_removal_breaks():
    base = _openapi(schema=_thing_schema())
    resp = base["paths"]["/api/v1/things"]["get"]["responses"]["200"]
    resp["headers"] = {"Retry-After": {"schema": {"type": "integer"}}}
    head = copy.deepcopy(base)
    del head["paths"]["/api/v1/things"]["get"]["responses"]["200"]["headers"]
    assert any(
        "response header removed" in f
        for f in api_breaking_check.diff_openapi(base, head)
    )


def test_media_schema_key_removal_breaks():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["responses"]["200"]["content"][
        "application/json"
    ] = {}
    assert any(
        "response schema removed" in f
        for f in api_breaking_check.diff_openapi(base, head)
    )


def _asyncapi_linked():
    return {
        "asyncapi": "3.0.0",
        "info": {"title": "t", "version": "1.0.0"},
        "channels": {
            "readerSync": {
                "address": "/ws/reader-sync",
                "messages": {
                    "req": {"payload": {"type": "object"}},
                    "err": {"payload": {"type": "object"}},
                },
            }
        },
        "operations": {
            "receive": {
                "action": "receive",
                "channel": {"$ref": "#/channels/readerSync"},
                "messages": [
                    {"$ref": "#/channels/readerSync/messages/req"},
                    {"$ref": "#/channels/readerSync/messages/err"},
                ],
            }
        },
    }


def test_asyncapi_channel_address_change_breaks():
    base = _asyncapi_linked()
    head = copy.deepcopy(base)
    head["channels"]["readerSync"]["address"] = "/ws/reader-sync-v2"
    assert any(
        "channel address changed" in f
        for f in api_breaking_check.diff_asyncapi(base, head)
    )


def test_asyncapi_operation_message_ref_removed_breaks():
    base = _asyncapi_linked()
    head = copy.deepcopy(base)
    head["operations"]["receive"]["messages"].pop()
    assert any(
        "operation message removed" in f
        for f in api_breaking_check.diff_asyncapi(base, head)
    )


def test_asyncapi_operation_action_flip_breaks():
    base = _asyncapi_linked()
    head = copy.deepcopy(base)
    head["operations"]["receive"]["action"] = "send"
    assert any(
        "operation action changed" in f
        for f in api_breaking_check.diff_asyncapi(base, head)
    )


# -- CLI exit contract ----------------------------------------------------------

def test_cli_exit_codes(tmp_path):
    base_doc = _openapi(schema=_thing_schema())
    breaking_doc = copy.deepcopy(base_doc)
    breaking_doc["paths"]["/api/v1/things"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["required"].append("kind")
    base_file = tmp_path / "base.yaml"
    head_file = tmp_path / "head.yaml"
    base_file.write_text(yaml.safe_dump(base_doc))
    head_file.write_text(yaml.safe_dump(breaking_doc))

    assert api_breaking_check.main(["--base-openapi", str(base_file),
                     "--head-openapi", str(base_file)]) == 0
    assert api_breaking_check.main(["--base-openapi", str(base_file),
                     "--head-openapi", str(head_file)]) == 1
    assert api_breaking_check.main(["--base-openapi", str(base_file),
                     "--head-openapi", str(head_file),
                     "--allow-breaking"]) == 0


def test_cli_nothing_to_compare_errors():
    with pytest.raises(SystemExit):
        api_breaking_check.main([])
