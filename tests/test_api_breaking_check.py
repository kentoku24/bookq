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
abc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(abc)


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
    findings = abc.diff_openapi(base, head)
    assert any("required added: 'kind'" in f for f in findings)


def test_property_removal_breaks():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    del head["paths"]["/api/v1/things"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["kind"]
    findings = abc.diff_openapi(base, head)
    assert any("property removed: 'kind'" in f for f in findings)


def test_enum_value_removal_breaks():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["kind"]["enum"].remove("b")
    findings = abc.diff_openapi(base, head)
    assert any("enum values removed: ['b']" in f for f in findings)


def test_type_change_breaks():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["name"] = {"type": "integer"}
    findings = abc.diff_openapi(base, head)
    assert any("type changed" in f for f in findings)


# -- acceptance: additive changes pass --------------------------------------

def test_optional_property_addition_passes():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["nick"] = {"type": "string"}
    assert abc.diff_openapi(base, head) == []


def test_enum_value_addition_passes():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["kind"]["enum"].append("c")
    assert abc.diff_openapi(base, head) == []


# -- structural removals -----------------------------------------------------

def test_path_removal_breaks():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    del head["paths"]["/api/v1/things"]
    assert any("path removed" in f for f in abc.diff_openapi(base, head))


def test_operation_id_change_breaks():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["operationId"] = "listThings"
    assert any("operationId changed" in f for f in abc.diff_openapi(base, head))


def test_required_parameter_addition_breaks():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["parameters"] = [
        {"name": "strict", "in": "query", "required": True,
         "schema": {"type": "string"}}
    ]
    assert any("required parameter added" in f for f in abc.diff_openapi(base, head))


def test_optional_parameter_addition_passes():
    base = _openapi(schema=_thing_schema())
    head = copy.deepcopy(base)
    head["paths"]["/api/v1/things"]["get"]["parameters"] = [
        {"name": "verbose", "in": "query", "schema": {"type": "boolean"}}
    ]
    assert abc.diff_openapi(base, head) == []


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
    findings = abc.diff_openapi(base, head)
    assert any("variant removed" in f for f in findings)


# -- AsyncAPI -----------------------------------------------------------------

def test_asyncapi_required_addition_breaks():
    base = _asyncapi(_thing_schema())
    head = copy.deepcopy(base)
    head["channels"]["readerSync"]["messages"]["thing"]["payload"][
        "required"
    ].append("kind")
    assert abc.diff_asyncapi(base, head) != []


def test_asyncapi_optional_addition_passes():
    base = _asyncapi(_thing_schema())
    head = copy.deepcopy(base)
    head["channels"]["readerSync"]["messages"]["thing"]["payload"][
        "properties"
    ]["nick"] = {"type": "string"}
    assert abc.diff_asyncapi(base, head) == []


def test_asyncapi_message_removal_breaks():
    base = _asyncapi(_thing_schema())
    head = copy.deepcopy(base)
    del head["channels"]["readerSync"]["messages"]["thing"]
    assert any("message removed" in f for f in abc.diff_asyncapi(base, head))


# -- real contracts: self-diff must stay clean ---------------------------------

def test_real_specs_self_diff_is_clean():
    openapi = yaml.safe_load((REPO_ROOT / "api" / "openapi.v1.yaml").read_text())
    asyncapi = yaml.safe_load((REPO_ROOT / "api" / "asyncapi.yaml").read_text())
    assert abc.diff_openapi(openapi, copy.deepcopy(openapi)) == []
    assert abc.diff_asyncapi(asyncapi, copy.deepcopy(asyncapi)) == []


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

    assert abc.main(["--base-openapi", str(base_file),
                     "--head-openapi", str(base_file)]) == 0
    assert abc.main(["--base-openapi", str(base_file),
                     "--head-openapi", str(head_file)]) == 1
    assert abc.main(["--base-openapi", str(base_file),
                     "--head-openapi", str(head_file),
                     "--allow-breaking"]) == 0


def test_cli_nothing_to_compare_errors():
    with pytest.raises(SystemExit):
        abc.main([])
