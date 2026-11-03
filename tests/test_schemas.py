"""校验 schemas 与 fixtures 自身一致性。

这是"契约的契约"：如果 fixtures 里的 invalid 样本其实能通过，
或者 valid 样本被拒，说明 schema 写错了，必须在任何实现之前发现。
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


def _load(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fixtures() -> dict:
    return _load("fixtures.json")


@pytest.fixture(scope="module")
def request_schema() -> dict:
    return _load("scene-request.schema.json")


@pytest.fixture(scope="module")
def result_schema() -> dict:
    return _load("scene-result.schema.json")


@pytest.mark.parametrize(
    "schema_file,group",
    [("scene-request.schema.json", "scene_request"), ("scene-result.schema.json", "scene_result")],
)
def test_schema_is_valid_jsonschema(schema_file: str, group: str) -> None:
    """Schema 自身必须是合法的 JSON Schema。"""
    schema = _load(schema_file)
    jsonschema.Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize(
    "schema_file,group",
    [("scene-request.schema.json", "scene_request"), ("scene-result.schema.json", "scene_result")],
)
def test_valid_fixtures_pass(schema_file: str, group: str, fixtures: dict) -> None:
    schema = _load(schema_file)
    validator = jsonschema.Draft202012Validator(schema)
    for case in fixtures[group]["valid"]:
        errors = list(validator.iter_errors(case["value"]))
        assert not errors, f"valid 样本 {case['case']} 被拒: {[e.message for e in errors]}"


@pytest.mark.parametrize(
    "schema_file,group",
    [("scene-request.schema.json", "scene_request"), ("scene-result.schema.json", "scene_result")],
)
def test_invalid_fixtures_rejected_for_stated_reason(
    schema_file: str, group: str, fixtures: dict
) -> None:
    """反例必须被拒，且拒绝原因与 fixtures 声明的 reason 一致。

    只断言"被拒"不够：如果 schema 因为别的字段错误而拒绝，
    真正的漏洞（比如 gps 泄漏）可能仍然存在。
    """
    schema = _load(schema_file)
    validator = jsonschema.Draft202012Validator(schema)
    for case in fixtures[group]["invalid"]:
        errors = list(validator.iter_errors(case["value"]))
        assert errors, f"invalid 样本 {case['case']} 竟然通过了 schema"
        keywords = {e.validator for e in errors}
        assert case["reason"] in keywords, (
            f"样本 {case['case']} 被拒原因 {keywords} 与声明的 '{case['reason']}' 不符"
        )


def test_result_schema_forbids_dangerous_fields(result_schema: dict) -> None:
    """SceneResult 不得有任何可被下游当作安全结论的字段。"""
    props = set(result_schema["properties"])
    forbidden = {
        "safe_to_walk",
        "recommended_direction",
        "distance_m",
        "risk_level",
        "can_pass",
        "is_safe",
    }
    assert not (props & forbidden), f"SceneResult 出现危险字段: {props & forbidden}"
    assert result_schema["additionalProperties"] is False


def test_request_schema_forbids_identity_fields(request_schema: dict) -> None:
    """SceneRequest 不得携带身份/位置类字段。"""
    props = set(request_schema["properties"])
    forbidden = {"gps", "location", "user_name", "contacts", "advertising_id", "history"}
    assert not (props & forbidden), f"SceneRequest 出现禁用字段: {props & forbidden}"
    assert request_schema["additionalProperties"] is False
