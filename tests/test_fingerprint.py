"""SceneFingerprint 测试（FR-TTS-004、TDD §8.3、TEST-TTS-004）。"""
from __future__ import annotations

from kanjian.fingerprint import Fingerprint, Similarity, compare, new_facts


def _fp(objects, summary="", text=None) -> Fingerprint:
    return Fingerprint.from_scene(
        objects=objects, scene_summary=summary, text_regions=text or []
    )


def test_identical_scene_is_same() -> None:
    a = _fp([{"label": "椅子", "zone": "front"}], "走廊")
    b = _fp([{"label": "椅子", "zone": "front"}], "走廊")
    assert compare(a, b) is Similarity.SAME


def test_quantity_words_do_not_change_identity() -> None:
    """'一把椅子' 与 '椅子' 必须判为同场景，否则冷却去重失效。"""
    a = _fp([{"label": "椅子", "zone": "front"}], "正前方有一把椅子")
    b = _fp([{"label": "椅子", "zone": "front"}], "正前方有椅子")
    assert compare(a, b) is Similarity.SAME


def test_new_object_is_changed() -> None:
    a = _fp([{"label": "椅子", "zone": "front"}])
    b = _fp([{"label": "椅子", "zone": "front"}, {"label": "自行车", "zone": "right"}])
    assert compare(a, b) is Similarity.CHANGED


def test_partial_overlap_is_similar() -> None:
    """高重合但不完全相同 → SIMILAR，仅在存在新事实时播增量。

    4 物体中 3 个相同：jaccard = 3/4 = 0.75，恰在 SIMILAR 阈值上。
    """
    a = _fp(
        [
            {"label": "椅子", "zone": "front"},
            {"label": "桌子", "zone": "left"},
            {"label": "书架", "zone": "right"},
        ]
    )
    b = _fp(
        [
            {"label": "椅子", "zone": "front"},
            {"label": "桌子", "zone": "left"},
            {"label": "书架", "zone": "right"},
            {"label": "门", "zone": "right"},
        ]
    )
    assert compare(a, b) is Similarity.SIMILAR
    assert new_facts(a, b) == {"门"}


def test_disjoint_objects_is_changed() -> None:
    a = _fp([{"label": "椅子", "zone": "front"}])
    b = _fp([{"label": "汽车", "zone": "right"}])
    assert compare(a, b) is Similarity.CHANGED


def test_empty_scenes_compare_by_summary() -> None:
    a = _fp([], "画面较暗，看不清")
    b = _fp([], "画面较暗，看不清")
    assert compare(a, b) is Similarity.SAME


def test_new_facts_empty_does_not_mean_safe() -> None:
    """关键边界：new_facts 为空**不**意味着"环境安全"或"物体已离开"。

    这个测试是文档层面的守卫：它断言 API 只返回集合，不提供任何否定语义，
    防止后续有人把它当"已清空"来播报。
    """
    a = _fp([{"label": "椅子", "zone": "front"}])
    b = _fp([{"label": "椅子", "zone": "front"}])
    facts = new_facts(a, b)
    assert facts == set()
    # 返回值类型是 set，没有 is_safe / cleared 之类的属性可用
    assert not hasattr(facts, "is_safe")
    assert isinstance(facts, set)


def test_text_presence_affects_identity() -> None:
    a = _fp([{"label": "门", "zone": "front"}], "门")
    b = _fp([{"label": "门", "zone": "front"}], "门", text=[{"text": "402"}])
    assert compare(a, b) is not Similarity.SAME
