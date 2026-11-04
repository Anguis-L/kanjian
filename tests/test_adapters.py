"""Provider 输出适配层测试（TDD §7）。

实测依据：magpie 路由上的 `workbuddy/deepseek-v4.1-flash`、`trae-cn/...`、
`workbuddy/glm-5.3-flash` 都返回自己的形状（多出 `description`/`speech`），
只有少数模型遵守 `response_format`。适配层让这些模型可用，
同时守住"不编造"的边界。
"""
from __future__ import annotations

import pytest

from gateway.adapters import adapt_scene_result, has_speakable_text

SCHEMA_KEYS = {
    "schema_version", "request_id", "scene_summary", "objects",
    "text_regions", "uncertainties", "image_quality", "speech_candidate",
    "provider", "usage",
}


# ---------------------------------------------------------------------------
# 真实观测到的形状
# ---------------------------------------------------------------------------

def test_maps_scene_and_speech_shape() -> None:
    """实测形状：{"scene":..,"speech":..,"confidence":..}"""
    r = adapt_scene_result(
        {"scene": "一条走廊", "speech": "正前方有一把椅子。", "confidence": 0.8}
    )
    assert r.data["scene_summary"] == "一条走廊"
    assert r.data["speech_candidate"] == "正前方有一把椅子。"
    assert r.adapted
    assert "confidence" not in r.data, "多余键必须丢弃，否则 Schema 会拒"


def test_maps_description_shape() -> None:
    """实测形状：{"description":..,"speech":..}"""
    r = adapt_scene_result({"description": "室内角落", "speech": "左侧有一扇门。"})
    assert r.data["scene_summary"] == "室内角落"
    assert r.data["speech_candidate"] == "左侧有一扇门。"


def test_conforming_output_untouched() -> None:
    """已经合规的输出不应被改动，且 adapted=False。"""
    good = {
        "schema_version": "1.0",
        "request_id": "x",
        "scene_summary": "走廊",
        "objects": [{"label": "椅子", "zone": "front", "confidence": "medium"}],
        "text_regions": [],
        "uncertainties": ["左侧看不清"],
        "image_quality": "acceptable",
        "speech_candidate": "正前方有一把椅子。",
        "provider": {"name": "p"},
    }
    r = adapt_scene_result(dict(good))
    assert not r.adapted
    for k, v in good.items():
        assert r.data[k] == v


# ---------------------------------------------------------------------------
# 「不编造」的边界
# ---------------------------------------------------------------------------

def test_missing_objects_does_not_mean_empty_scene() -> None:
    """objects 缺失填 []，但必须记 notes 说明这不代表"没有物体"。"""
    r = adapt_scene_result({"speech": "正前方有一把椅子。"})
    assert r.data["objects"] == []
    assert any("不代表没有物体" in n for n in r.notes)


def test_missing_quality_becomes_unknown_never_good() -> None:
    """画质缺失必须填 unknown，绝不能填 good——那是凭空暗示画质合格。"""
    r = adapt_scene_result({"speech": "画面里有一张桌子。"})
    assert r.data["image_quality"] == "unknown"
    assert r.data["image_quality"] != "good"


def test_no_speakable_text_is_flagged() -> None:
    """没有可播报文本时必须能被上层识别为失败，而不是硬凑。"""
    r = adapt_scene_result({"scene": "一条走廊", "objects": []})
    assert not has_speakable_text(r.data)
    assert any("缺少可播报文本" in n for n in r.notes)


def test_whitespace_speech_is_not_speakable() -> None:
    r = adapt_scene_result({"speech": "   "})
    assert not has_speakable_text(r.data)


def test_does_not_fabricate_scene_summary() -> None:
    """摘要缺失就留空，不拿 speech 冒充摘要（那是另一层语义）。"""
    r = adapt_scene_result({"speech": "正前方有一把椅子。"})
    assert r.data["scene_summary"] == ""


# ---------------------------------------------------------------------------
# 物件清洗
# ---------------------------------------------------------------------------

def test_object_aliases_and_invalid_values() -> None:
    r = adapt_scene_result(
        {
            "speech": "有东西。",
            "objects": [
                {"name": "椅子", "position": "front", "certainty": "high", "count": 2},
                {"label": "门", "zone": "north", "confidence": "0.9"},
            ],
        }
    )
    objs = r.data["objects"]
    assert objs[0] == {"label": "椅子", "zone": "front", "confidence": "high", "count_visible": 2}
    # 非法 zone/confidence 退化为 unknown，而不是让整条失败
    assert objs[1]["zone"] == "unknown"
    assert objs[1]["confidence"] == "unknown"


def test_object_without_label_dropped() -> None:
    r = adapt_scene_result({"speech": "有东西。", "objects": [{"zone": "front"}, "椅子", 42]})
    assert r.data["objects"] == []


def test_too_many_objects_truncated() -> None:
    r = adapt_scene_result(
        {"speech": "很多东西。", "objects": [{"label": f"物{i}", "zone": "front"} for i in range(50)]}
    )
    assert len(r.data["objects"]) == 20


# ---------------------------------------------------------------------------
# 其他字段
# ---------------------------------------------------------------------------

def test_text_regions_from_strings() -> None:
    r = adapt_scene_result({"speech": "有字。", "text_regions": ["营业中", "9:00-21:00"]})
    assert r.data["text_regions"] == [{"text": "营业中"}, {"text": "9:00-21:00"}]


def test_uncertainties_single_string() -> None:
    r = adapt_scene_result({"speech": "看不清。", "uncertainties": "画面太暗"})
    assert r.data["uncertainties"] == ["画面太暗"]


def test_extra_top_level_keys_dropped() -> None:
    """实测出现过顶层 confidence，必须丢弃否则 additionalProperties:false 拒绝。"""
    r = adapt_scene_result(
        {"scene": "走廊", "speech": "正前方有椅子。", "confidence": 0.9, "foo": "bar"}
    )
    assert set(r.data) <= SCHEMA_KEYS


def test_forbidden_safety_field_causes_loud_failure() -> None:
    """危险字段必须让整次失败，而不是被静默剥掉。

    剥掉虽然"安全"，但会让我们永远发现不了模型在输出安全结论——
    那是必须被观测到的问题，不是可以抹平的脏数据。
    """
    r = adapt_scene_result({"speech": "正前方有一把椅子。", "safe_to_walk": True})
    assert r.rejected is not None
    assert "safe_to_walk" in r.rejected
    assert r.data == {}


@pytest.mark.parametrize("field", ["distance_m", "risk_level", "recommended_direction"])
def test_forbidden_fields_all_rejected(field: str) -> None:
    r = adapt_scene_result({"speech": "有东西。", field: 1})
    assert r.rejected is not None


def test_non_dict_input_fails_safely() -> None:
    r = adapt_scene_result("不是对象")  # type: ignore[arg-type]
    assert r.data == {}
    assert r.rejected is not None


@pytest.mark.parametrize(
    "shape",
    [
        {"scene": "a", "speech": "b"},
        {"description": "a", "speech": "b"},
        {"summary": "a", "utterance": "b"},
        {"caption": "a", "text": "b"},
    ],
)
def test_speech_alias_coverage(shape: dict) -> None:
    """常见别名组合都能映射出可播报文本。"""
    r = adapt_scene_result(shape)
    assert has_speakable_text(r.data)
