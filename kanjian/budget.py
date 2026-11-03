"""BudgetGuard —— 云端用量预算（NFR-COST-002，ADR-10）。

为什么这层必须存在：公益项目不提供维护者付费的默认端点（ADR-07），
用户自己付钱。没有预算保护时，自动模式 + 高频触发可以在半小时内
烧掉一个月的额度，而用户不会收到任何提示。

设计要点：
- **不可关闭**，只能调整阈值（ADR-10）。能关掉这个保护的，往往正是最需要它的人。
- 达 80% 提醒一次，达 100% 暂停自动模式；**手动询问仍可用**——用户明确
  按下的操作不应被预算拦住，但要每次提示。
- 未知 token 按保守上界估算，宁可提前触发（TDD §13：不得按 0 计）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum

#: ADR-10 默认值。可上调至 DEFAULT_*_HARD_CAP_MULTIPLIER 倍，不可关闭。
DEFAULT_DAILY_REQUESTS = 300
DEFAULT_DAILY_COST_CNY = 3.00
DEFAULT_SESSION_REQUESTS = 100
HARD_CAP_MULTIPLIER = 10

#: 达此比例时提醒一次（不改变状态）。
WARN_RATIO = 0.80

#: 单次请求在 token 未知时的保守上界估算（CNY）。
#: 取 ¥0.02：约等于一张 1280px JPEG + 短输出的偏高估算，用于"宁可早停"。
UNKNOWN_REQUEST_COST_ESTIMATE_CNY = 0.02


class BudgetState(str, Enum):
    OK = "OK"
    WARNED = "WARNED"
    HALTED = "HALTED"


@dataclass
class BudgetStatus:
    """一次预算查询的结果。调用方据此决定是否发起云端请求。"""

    state: BudgetState
    daily_requests_used: int
    daily_requests_limit: int
    daily_cost_used_cny: float
    daily_cost_limit_cny: float
    session_requests_used: int
    session_requests_limit: int
    message: str = ""

    @property
    def auto_mode_allowed(self) -> bool:
        """自动模式是否允许继续产生云端候选。"""
        return self.state is not BudgetState.HALTED

    @property
    def manual_allowed(self) -> bool:
        """手动询问是否允许。预算从不完全阻止用户明确操作（ADR-10）。"""
        return True

    @property
    def should_prompt_on_manual(self) -> bool:
        """手动询问时是否应附加一句用量提示。"""
        return self.state is not BudgetState.OK


@dataclass
class BudgetGuard:
    """本地用量计数。不持久化到远端，不上传（D-07）。"""

    daily_requests_limit: int = DEFAULT_DAILY_REQUESTS
    daily_cost_limit_cny: float = DEFAULT_DAILY_COST_CNY
    session_requests_limit: int = DEFAULT_SESSION_REQUESTS

    _day: date = field(default_factory=lambda: datetime.now(timezone.utc).date())
    _daily_requests: int = 0
    _daily_cost: float = 0.0
    _session_requests: int = 0
    _warned_today: bool = False

    # -- 阈值调整 ---------------------------------------------------------

    def raise_limits(
        self, *, requests: int | None = None, cost_cny: float | None = None
    ) -> None:
        """上调阈值。超过硬顶则拒绝（ADR-10：可上调至默认值 10 倍）。"""
        if requests is not None:
            cap = DEFAULT_DAILY_REQUESTS * HARD_CAP_MULTIPLIER
            if not (0 < requests <= cap):
                raise ValueError(f"日请求上限须在 1..{cap}，收到 {requests}")
            self.daily_requests_limit = requests
        if cost_cny is not None:
            cap = DEFAULT_DAILY_COST_CNY * HARD_CAP_MULTIPLIER
            if not (0 < cost_cny <= cap):
                raise ValueError(f"日费用上限须在 0..{cap}，收到 {cost_cny}")
            self.daily_cost_limit_cny = cost_cny

    def estimated_cost_message(self) -> str:
        """上调时须读出的一句话（ADR-10）。"""
        return f"预计日费用将升至约 ¥{self.daily_cost_limit_cny:.2f}"

    # -- 计数 -------------------------------------------------------------

    def _roll_day_if_needed(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._day:
            self._day = today
            self._daily_requests = 0
            self._daily_cost = 0.0
            self._warned_today = False

    def record(
        self,
        *,
        cost_cny: float | None = None,
        tokens_known: bool = True,
    ) -> BudgetStatus:
        """记录一次云端请求（成功或失败都计 1，TDD §13.1）。"""
        self._roll_day_if_needed()
        self._daily_requests += 1
        self._session_requests += 1
        if tokens_known and cost_cny is not None:
            self._daily_cost += cost_cny
        else:
            # 不得把缺失 token 当 0（TDD §13）。
            self._daily_cost += UNKNOWN_REQUEST_COST_ESTIMATE_CNY
        return self.status()

    def status(self) -> BudgetStatus:
        self._roll_day_if_needed()

        req_ratio = self._daily_requests / self.daily_requests_limit
        cost_ratio = (
            self._daily_cost / self.daily_cost_limit_cny if self.daily_cost_limit_cny else 0
        )
        session_ratio = self._session_requests / self.session_requests_limit
        worst = max(req_ratio, cost_ratio, session_ratio)

        if worst >= 1.0:
            state = BudgetState.HALTED
            if req_ratio >= 1.0:
                message = "已到本日云端用量上限，自动观察已暂停；可手动询问"
            elif cost_ratio >= 1.0:
                message = "已到本日云端费用上限，自动观察已暂停；可手动询问"
            else:
                message = "已到本次会话云端用量上限，自动观察已暂停；可手动询问"
        elif worst >= WARN_RATIO:
            state = BudgetState.WARNED
            message = "今日云端用量已用约八成"
        else:
            state = BudgetState.OK
            message = ""

        return BudgetStatus(
            state=state,
            daily_requests_used=self._daily_requests,
            daily_requests_limit=self.daily_requests_limit,
            daily_cost_used_cny=round(self._daily_cost, 4),
            daily_cost_limit_cny=self.daily_cost_limit_cny,
            session_requests_used=self._session_requests,
            session_requests_limit=self.session_requests_limit,
            message=message,
        )

    def consume_warning(self) -> bool:
        """返回本次是否应播报提醒；每次跨过 80% 只播一次（SDD §9 不循环打断）。"""
        if self._warned_today:
            return False
        if self.status().state is BudgetState.WARNED:
            self._warned_today = True
            return True
        return False

    def new_session(self) -> None:
        """会话切换时重置会话计数（日计数保留）。"""
        self._session_requests = 0
