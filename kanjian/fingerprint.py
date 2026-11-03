"""SceneFingerprint —— 场景相似性比较，用于去重与冷却（FR-TTS-004）。

关键约束（TDD §8.3）：
- 输入是**标准化后的物体标签/方位、场景摘要、OCR 类型**，不是整句相等。
  整句相等会让"正前方有椅子"和"正前方有一把椅子"被判为不同，重复播报。
- **不得根据旧指纹断言物体已离开或环境已安全。** 指纹只能说明"看起来像/不像"，
  它没有否定能力。这条是本模块最重要的边界。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Similarity(str, Enum):
    SAME = "same"
    SIMILAR = "similar"
    CHANGED = "changed"


@dataclass(frozen=True)
class Fingerprint:
    """可比较的场景特征。只保留结构性信息，不保留原始文本。"""

    labels: frozenset[str]
    zones: frozenset[str]
    summary_tokens: frozenset[str]
    has_text: bool

    @staticmethod
    def from_scene(
        *,
        objects: list[dict],
        scene_summary: str = "",
        text_regions: list[dict] | None = None,
    ) -> "Fingerprint":
        labels = {_norm_label(o.get("label", "")) for o in objects if o.get("label")}
        zones = {o.get("zone", "unknown") for o in objects}
        tokens = _summary_tokens(scene_summary)
        return Fingerprint(
            labels=frozenset(labels),
            zones=frozenset(zones),
            summary_tokens=frozenset(tokens),
            has_text=bool(text_regions),
        )


#: 数量词与虚词，不应影响相似性判断。
_NOISE = re.compile(
    r"(一把|一张|一个|一辆|一扇|一只|几个|一些|有个|有的|像是|看起来|好像|可能|大概|"
    r"正前方|左侧|右侧|前方|左边|右边|中间|画面|这里|那里)"
)


def _norm_label(label: str) -> str:
    return _NOISE.sub("", label.strip().lower()).strip(" ，。,.!?")


def _summary_tokens(summary: str) -> set[str]:
    """把摘要拆成有意义的片段，去掉数量词与方位虚词。"""
    s = _NOISE.sub("", summary.strip().lower())
    return {t for t in re.split(r"[，。；、,.!?\s]+", s) if t}


def compare(a: Fingerprint, b: Fingerprint) -> Similarity:
    """比较两个指纹。

    判据（TDD §8.3）：
    - SAME：物体集合与场景片段几乎一致 → 冷却期内不播
    - SIMILAR：有交集但有差异 → 只在存在用户可感知的新事实时播增量
    - CHANGED：物体集合显著不同 → 生成新候选
    """
    if a.labels == b.labels and a.summary_tokens == b.summary_tokens and a.has_text == b.has_text:
        return Similarity.SAME

    if not a.labels and not b.labels:
        # 都没有物体：靠摘要判断
        overlap = a.summary_tokens & b.summary_tokens
        if overlap and len(overlap) >= min(len(a.summary_tokens), len(b.summary_tokens)):
            return Similarity.SAME
        return Similarity.CHANGED if not overlap else Similarity.SIMILAR

    overlap = a.labels & b.labels
    union = a.labels | b.labels
    if not overlap:
        return Similarity.CHANGED

    jaccard = len(overlap) / len(union) if union else 0.0
    if jaccard >= 0.75:
        return Similarity.SIMILAR
    return Similarity.CHANGED


def new_facts(previous: Fingerprint, current: Fingerprint) -> set[str]:
    """返回 current 相对 previous 的新增物体。

    用于 SIMILAR 情况下判断"是否值得播增量"（FR-TTS-004）。
    注意：返回空集**不**等于"环境安全"或"物体已离开"——它只表示
    "本次没有看到新东西"。调用方不得据此推断否定事实。
    """
    return set(current.labels - previous.labels)
