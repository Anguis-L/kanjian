"""TriggerEngine —— 决定哪一帧值得送去理解（ADR-01）。

核心观念（D-03）：采样频率 ≠ 理解频率 ≠ 播报频率。
0.5/1 fps 只是"看到了多少帧"，不是"要理解多少次"。把这两者混为一谈
是这类产品最常见的成本与听觉负担失控来源。

规则顺序（SDD §5，顺序不可乱）：
1. 手动请求 FORCE_USER，不受自动冷却限制
2. 页面不可见/权限缺失/云端禁用 → 忽略云端候选
3. 冷却期内 → 忽略（且**不积累补偿请求**）
4. 帧差/运动不足 → 忽略
5. 资源降级 → 提高阈值或暂停
6. 通过者生成候选；过期未执行则丢弃，不排队
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: ADR-12：开发默认冷却 20000ms，可配 10000–60000。
DEFAULT_COOLDOWN_MS = 20_000
MIN_COOLDOWN_MS = 10_000
MAX_COOLDOWN_MS = 60_000

#: 帧差阈值（初始实验参数，须通过测试调整，TDD §8.2）。
#: 平均绝对差（灰度 0-255 归一化到 0-1）。
DEFAULT_CHANGE_THRESHOLD = 0.08
#: 滞回：从"触发"回到"不触发"需要降到阈值的这个比例以下，防止边界抖动。
HYSTERESIS_RATIO = 0.75
#: IMU 剧烈运动时阈值抬高倍数，避免把晃动当新场景。
MOTION_THRESHOLD_MULTIPLIER = 1.8
#: 高运动幅度判定阈值（归一化加速度）。
HIGH_MOTION_LEVEL = 0.6


class TriggerDecision(str, Enum):
    IGNORE = "IGNORE"
    CANDIDATE_AUTO = "CANDIDATE_AUTO"
    FORCE_USER = "FORCE_USER"


class IgnoreReason(str, Enum):
    NOT_VISIBLE = "NOT_VISIBLE"
    CLOUD_DISABLED = "CLOUD_DISABLED"
    COOLDOWN = "COOLDOWN"
    NO_CHANGE = "NO_CHANGE"
    BUDGET_HALTED = "BUDGET_HALTED"
    STALE = "STALE"


@dataclass(frozen=True)
class TriggerInput:
    """一次采样节拍的全部输入（SDD §5）。"""

    now_ms: int
    last_inference_ms: int | None = None
    last_speech_end_ms: int | None = None
    change_score: float = 0.0
    motion_level: float = 0.0
    page_visible: bool = True
    has_camera_permission: bool = True
    cloud_enabled: bool = True
    auto_mode_enabled: bool = True
    budget_halted: bool = False
    thermal_degraded: bool = False


@dataclass(frozen=True)
class TriggerOutput:
    decision: TriggerDecision
    reason: str = ""
    change_score: float = 0.0
    effective_threshold: float = 0.0
    freshness_deadline_ms: int = 0


class TriggerEngine:
    """有状态的触发器：需要记住"上次是否在触发态"以做滞回。"""

    def __init__(
        self,
        *,
        cooldown_ms: int = DEFAULT_COOLDOWN_MS,
        change_threshold: float = DEFAULT_CHANGE_THRESHOLD,
        freshness_ms: int = 3_000,
    ) -> None:
        if not (MIN_COOLDOWN_MS <= cooldown_ms <= MAX_COOLDOWN_MS):
            raise ValueError(
                f"cooldown_ms 须在 {MIN_COOLDOWN_MS}..{MAX_COOLDOWN_MS}（ADR-12），收到 {cooldown_ms}"
            )
        self.cooldown_ms = cooldown_ms
        self.change_threshold = change_threshold
        self.freshness_ms = freshness_ms
        self._in_trigger_state = False

    def evaluate(self, inp: TriggerInput) -> TriggerOutput:
        # 手动请求：FORCE_USER，不受自动冷却限制（但受单飞限制，由 Coordinator 管）
        if not inp.page_visible:
            return self._ignore(inp, IgnoreReason.NOT_VISIBLE)
        if not inp.has_camera_permission:
            return self._ignore(inp, IgnoreReason.NOT_VISIBLE)
        if not inp.cloud_enabled:
            return self._ignore(inp, IgnoreReason.CLOUD_DISABLED)
        if inp.budget_halted:
            return self._ignore(inp, IgnoreReason.BUDGET_HALTED)
        if not inp.auto_mode_enabled:
            # 自动模式关闭时，自动候选一律忽略；手动由 force_user 单独处理
            return self._ignore(inp, IgnoreReason.NOT_VISIBLE)

        # 冷却期内：忽略，且不积累补偿请求（SDD §5 规则 3）
        # 冷却从**上次播报结束**起算（SDD §9）
        anchor = inp.last_speech_end_ms if inp.last_speech_end_ms is not None else inp.last_inference_ms
        if anchor is not None and (inp.now_ms - anchor) < self.cooldown_ms:
            return self._ignore(inp, IgnoreReason.COOLDOWN)

        threshold = self._effective_threshold(inp)

        # 滞回：已在触发态时，需要更低的变化才继续触发
        needed = threshold * HYSTERESIS_RATIO if self._in_trigger_state else threshold

        if inp.change_score < needed:
            self._in_trigger_state = False
            return self._ignore(inp, IgnoreReason.NO_CHANGE, threshold)

        self._in_trigger_state = True
        return TriggerOutput(
            decision=TriggerDecision.CANDIDATE_AUTO,
            reason="CHANGED",
            change_score=inp.change_score,
            effective_threshold=threshold,
            freshness_deadline_ms=inp.now_ms + self.freshness_ms,
        )

    def force_user(self, inp: TriggerInput) -> TriggerOutput:
        """用户主动询问：立即触发一次新帧推理，不等待采样周期（FR-TRG-002）。"""
        if not inp.page_visible or not inp.has_camera_permission:
            return self._ignore(inp, IgnoreReason.NOT_VISIBLE)
        return TriggerOutput(
            decision=TriggerDecision.FORCE_USER,
            reason="USER_REQUEST",
            change_score=inp.change_score,
            effective_threshold=self._effective_threshold(inp),
            freshness_deadline_ms=inp.now_ms + self.freshness_ms,
        )

    def _effective_threshold(self, inp: TriggerInput) -> float:
        t = self.change_threshold
        # 设备在动：抬高阈值，避免把晃动当新场景（TDD §8.2 规则 3）
        if inp.motion_level >= HIGH_MOTION_LEVEL:
            t *= MOTION_THRESHOLD_MULTIPLIER
        # 过热/低电：提高触发阈值（SDD §5 规则 5）
        if inp.thermal_degraded:
            t *= MOTION_THRESHOLD_MULTIPLIER
        return min(t, 1.0)

    def _ignore(self, inp: TriggerInput, reason: IgnoreReason, threshold: float = 0.0) -> TriggerOutput:
        return TriggerOutput(
            decision=TriggerDecision.IGNORE,
            reason=reason.value,
            change_score=inp.change_score,
            effective_threshold=threshold,
        )

    def is_stale(self, deadline_ms: int, now_ms: int) -> bool:
        """过期候选直接丢弃，不排队（FR-TRG-003）。"""
        return now_ms > deadline_ms
