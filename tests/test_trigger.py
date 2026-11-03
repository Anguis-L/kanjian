"""TriggerEngine 测试（ADR-01、ADR-12、TEST-TRG-001~005）。"""
from __future__ import annotations

import pytest

from kanjian.trigger import (
    DEFAULT_COOLDOWN_MS,
    MAX_COOLDOWN_MS,
    MIN_COOLDOWN_MS,
    HIGH_MOTION_LEVEL,
    IgnoreReason,
    TriggerDecision,
    TriggerEngine,
    TriggerInput,
)


def _engine(**kw) -> TriggerEngine:
    return TriggerEngine(**kw)


def test_cooldown_bounds_enforced() -> None:
    """冷却值必须落在 ADR-12 的范围。"""
    with pytest.raises(ValueError):
        TriggerEngine(cooldown_ms=MIN_COOLDOWN_MS - 1)
    with pytest.raises(ValueError):
        TriggerEngine(cooldown_ms=MAX_COOLDOWN_MS + 1)


def test_default_cooldown_is_adr12_value() -> None:
    assert TriggerEngine().cooldown_ms == DEFAULT_COOLDOWN_MS == 20_000


def test_static_scene_does_not_trigger() -> None:
    """静止场景不重复触发（TEST-TRG-001）。"""
    e = _engine()
    out = e.evaluate(TriggerInput(now_ms=1000, change_score=0.0))
    assert out.decision is TriggerDecision.IGNORE
    assert out.reason == IgnoreReason.NO_CHANGE.value


def test_change_triggers_candidate() -> None:
    e = _engine()
    out = e.evaluate(TriggerInput(now_ms=1000, change_score=0.5))
    assert out.decision is TriggerDecision.CANDIDATE_AUTO
    assert out.freshness_deadline_ms > 1000


def test_cooldown_blocks_repeated_trigger() -> None:
    """冷却期内忽略，且不积累补偿请求（SDD §5 规则 3）。"""
    e = _engine(cooldown_ms=20_000)
    # 上次播报在很久以前（0），当前 1000ms 已出冷却
    first = e.evaluate(
        TriggerInput(now_ms=100_000, change_score=0.5, last_speech_end_ms=0)
    )
    assert first.decision is TriggerDecision.CANDIDATE_AUTO
    # 触发后"播报"在 100_100ms 结束 → 冷却到 120_100ms
    second = e.evaluate(
        TriggerInput(now_ms=105_000, change_score=0.9, last_speech_end_ms=100_100)
    )
    assert second.decision is TriggerDecision.IGNORE
    assert second.reason == IgnoreReason.COOLDOWN.value


def test_cooldown_counts_from_speech_end() -> None:
    """冷却从上次播报**结束**起算（SDD §9）。"""
    e = _engine(cooldown_ms=20_000)
    # 播报在 1000ms 结束，冷却到 21000ms
    out = e.evaluate(
        TriggerInput(now_ms=20_500, change_score=0.5, last_speech_end_ms=1000)
    )
    assert out.decision is TriggerDecision.IGNORE
    out2 = e.evaluate(
        TriggerInput(now_ms=21_500, change_score=0.5, last_speech_end_ms=1000)
    )
    assert out2.decision is TriggerDecision.CANDIDATE_AUTO


@pytest.mark.parametrize(
    "kwargs,expected_reason",
    [
        ({"page_visible": False}, IgnoreReason.NOT_VISIBLE),
        ({"has_camera_permission": False}, IgnoreReason.NOT_VISIBLE),
        ({"cloud_enabled": False}, IgnoreReason.CLOUD_DISABLED),
        ({"budget_halted": True}, IgnoreReason.BUDGET_HALTED),
    ],
)
def test_gates_block_auto(kwargs: dict, expected_reason: IgnoreReason) -> None:
    e = _engine()
    out = e.evaluate(TriggerInput(now_ms=1000, change_score=0.9, **kwargs))
    assert out.decision is TriggerDecision.IGNORE
    assert out.reason == expected_reason.value


def test_high_motion_raises_threshold() -> None:
    """IMU 剧烈运动时抬高阈值，避免把晃动当新场景（TDD §8.2）。"""
    e = _engine(change_threshold=0.08)
    calm = e.evaluate(TriggerInput(now_ms=1000, change_score=0.10, motion_level=0.0))
    assert calm.decision is TriggerDecision.CANDIDATE_AUTO

    e2 = _engine(change_threshold=0.08)
    shaky = e2.evaluate(
        TriggerInput(now_ms=1000, change_score=0.10, motion_level=HIGH_MOTION_LEVEL)
    )
    assert shaky.decision is TriggerDecision.IGNORE
    assert shaky.effective_threshold > calm.effective_threshold


def test_thermal_degradation_raises_threshold() -> None:
    """过热/低电时提高阈值（SDD §5 规则 5）。"""
    e = _engine(change_threshold=0.08)
    out = e.evaluate(TriggerInput(now_ms=1000, change_score=0.10, thermal_degraded=True))
    assert out.decision is TriggerDecision.IGNORE


def test_hysteresis_prevents_boundary_flapping() -> None:
    """滞回：触发后需要更低的变化才继续触发，防止边界抖动。"""
    e = _engine(change_threshold=0.08)
    e.evaluate(TriggerInput(now_ms=1000, change_score=0.09))  # 进入触发态
    # 0.07 低于原阈值但高于滞回阈值（0.08*0.75=0.06）→ 继续触发
    out = e.evaluate(TriggerInput(now_ms=3000, change_score=0.07))
    assert out.decision is TriggerDecision.CANDIDATE_AUTO
    # 0.05 低于滞回阈值 → 退出触发态
    out2 = e.evaluate(TriggerInput(now_ms=5000, change_score=0.05))
    assert out2.decision is TriggerDecision.IGNORE


def test_force_user_ignores_cooldown() -> None:
    """用户主动询问不受自动冷却限制（FR-TRG-002）。"""
    e = _engine(cooldown_ms=20_000)
    out = e.force_user(
        TriggerInput(now_ms=2000, change_score=0.0, last_speech_end_ms=1900)
    )
    assert out.decision is TriggerDecision.FORCE_USER


def test_force_user_still_blocked_when_invisible() -> None:
    e = _engine()
    out = e.force_user(TriggerInput(now_ms=2000, page_visible=False))
    assert out.decision is TriggerDecision.IGNORE


def test_stale_candidate_dropped() -> None:
    """过期候选直接丢弃，不排队（FR-TRG-003）。"""
    e = _engine(freshness_ms=1000)
    out = e.evaluate(TriggerInput(now_ms=1000, change_score=0.5))
    assert e.is_stale(out.freshness_deadline_ms, now_ms=5000) is True
    assert e.is_stale(out.freshness_deadline_ms, now_ms=1500) is False


def test_auto_mode_off_blocks_auto() -> None:
    """自动模式默认关闭（FR-CFG-002）：关闭时自动候选一律忽略。"""
    e = _engine()
    out = e.evaluate(TriggerInput(now_ms=1000, change_score=0.9, auto_mode_enabled=False))
    assert out.decision is TriggerDecision.IGNORE
