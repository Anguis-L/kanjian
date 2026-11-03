"""BudgetGuard 测试（NFR-COST-002，ADR-10，TEST-COST-001~006）。"""
from __future__ import annotations

import pytest

from kanjian.budget import (
    DEFAULT_DAILY_COST_CNY,
    DEFAULT_DAILY_REQUESTS,
    DEFAULT_SESSION_REQUESTS,
    HARD_CAP_MULTIPLIER,
    BudgetGuard,
    BudgetState,
)


def test_starts_ok() -> None:
    g = BudgetGuard()
    s = g.status()
    assert s.state is BudgetState.OK
    assert s.auto_mode_allowed
    assert not s.should_prompt_on_manual


def test_defaults_match_adr10() -> None:
    """默认值必须与 ADR-10 记录的字面值一致。"""
    g = BudgetGuard()
    assert g.daily_requests_limit == DEFAULT_DAILY_REQUESTS == 300
    assert g.daily_cost_limit_cny == DEFAULT_DAILY_COST_CNY == 3.00
    assert g.session_requests_limit == DEFAULT_SESSION_REQUESTS == 100


def test_warn_at_80_percent_once() -> None:
    """达 80% 只提醒一次，不重复打断（SDD §9、TEST-COST-002a）。

    注意用会话内 80 次（< 会话上限 100）测试日请求维度的 80%：
    240 次会先撞会话上限，测不到 WARN 状态。
    """
    g = BudgetGuard(daily_requests_limit=100)  # 80% = 80 次
    for _ in range(80):
        g.record(cost_cny=0.001)
    assert g.status().state is BudgetState.WARNED
    assert g.consume_warning() is True
    assert g.consume_warning() is False, "提醒不应重复播报"


def test_halt_at_100_percent_requests() -> None:
    """请求数达上限 → 暂停自动模式，手动仍可用（TEST-COST-001）。"""
    g = BudgetGuard()
    for _ in range(300):
        g.record(cost_cny=0.0)
    s = g.status()
    assert s.state is BudgetState.HALTED
    assert not s.auto_mode_allowed
    assert s.manual_allowed, "预算不得完全阻止用户明确操作（ADR-10）"
    assert s.should_prompt_on_manual
    assert "自动观察已暂停" in s.message


def test_halt_at_100_percent_cost() -> None:
    """费用达上限同样暂停（TEST-COST-002）。"""
    g = BudgetGuard()
    for _ in range(10):
        g.record(cost_cny=0.31)  # 3.10 > 3.00
    assert g.status().state is BudgetState.HALTED
    assert "费用" in g.status().message


def test_session_limit() -> None:
    """单会话 100 次上限（TEST-COST-002b）。"""
    g = BudgetGuard()
    for _ in range(100):
        g.record(cost_cny=0.0)
    s = g.status()
    assert s.state is BudgetState.HALTED
    assert "会话" in s.message


def test_new_session_resets_session_count() -> None:
    g = BudgetGuard()
    for _ in range(50):
        g.record(cost_cny=0.0)
    g.new_session()
    s = g.status()
    assert s.session_requests_used == 0
    assert s.daily_requests_used == 50, "日计数不应被会话重置清空"


def test_unknown_tokens_use_conservative_estimate() -> None:
    """缺失 token 不得按 0 计（TDD §13）。"""
    g = BudgetGuard()
    g.record(cost_cny=None, tokens_known=False)
    assert g.status().daily_cost_used_cny > 0


def test_raise_limits_within_cap() -> None:
    g = BudgetGuard()
    g.raise_limits(requests=1000, cost_cny=10.0)
    assert g.daily_requests_limit == 1000
    assert g.daily_cost_limit_cny == 10.0


def test_raise_limits_beyond_hard_cap_rejected() -> None:
    """硬顶为默认值 10 倍（ADR-10，TEST-COST-006）。"""
    g = BudgetGuard()
    cap = DEFAULT_DAILY_REQUESTS * HARD_CAP_MULTIPLIER
    with pytest.raises(ValueError):
        g.raise_limits(requests=cap + 1)
    with pytest.raises(ValueError):
        g.raise_limits(cost_cny=DEFAULT_DAILY_COST_CNY * HARD_CAP_MULTIPLIER + 0.01)


def test_cannot_disable_protection() -> None:
    """不允许把阈值设为 0 或负数来"关闭"保护（ADR-10）。"""
    g = BudgetGuard()
    with pytest.raises(ValueError):
        g.raise_limits(requests=0)
    with pytest.raises(ValueError):
        g.raise_limits(cost_cny=0.0)


def test_estimated_cost_message_on_raise() -> None:
    """上调时须能读出预计日费用（ADR-10）。"""
    g = BudgetGuard()
    g.raise_limits(cost_cny=5.0)
    assert "5.00" in g.estimated_cost_message()


def test_failed_requests_also_count() -> None:
    """成功或失败发起一次都计 1（TDD §13.1）。"""
    g = BudgetGuard()
    g.record(cost_cny=None, tokens_known=False)  # 模拟失败
    assert g.status().daily_requests_used == 1
