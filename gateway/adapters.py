"""Provider 输出适配层（TDD §7）。

**为什么需要它**：实测发现多数模型（尤其经路由组访问时）会无视
`response_format: json_schema`，返回自己的形状，例如：

    {"scene": "...", "speech": "...", "confidence": 0.8}
    {"description": "...", "speech": "..."}

TDD §7 明确允许网关做转换：「若 Provider 不支持可靠结构化输出，网关负责转换；
转换失败返回 PROVIDER_BAD_RESPONSE」。所以这里做字段映射，
而不是要求用户非得找到一个完美遵守契约的模型。

**边界（很重要）**：
- 只做**字段改名**，绝不编造内容。模型没给 `objects` 就填空数组，
  不表示"没有物体"，只表示"它没给结构化物件"——播报文本仍由
  `speech_candidate` 承载，安全策略照样跑它。
- `image_quality` 缺失一律填 `unknown`，**绝不填 good**——
  那会凭空暗示画质合格。
- 缺少可播报文本（speech_candidate 及其别名都没有）时**直接失败**，
  不替模型拼一句话。
- 适配后**必须再过一次 Schema 校验**，不通过仍然算 PROVIDER_BAD_RESPONSE。
"""

from __future__ import annotations

from typing import Any

#: 各字段可接受的别名，按优先级排列。只做改名，不做语义推断。
_ALIASES: dict[str, tuple[str, ...]] = {
    "scene_summary": ("scene_summary", "scene", "description", "summary", "caption"),
    "speech_candidate": ("speech_candidate", "speech", "utterance", "text"),
    "uncertainties": ("uncertainties", "uncertainty", "caveats"),
    "text_regions": ("text_regions", "texts", "ocr"),
    "image_quality": ("image_quality", "quality"),
}

#: 目标 Schema 允许出现的键。适配后多余的键会被丢弃，
#: 否则 additionalProperties:false 会拒绝整个结果。
_KEEP = (
    "schema_version",
    "request_id",
    "scene_summary",
    "objects",
    "text_regions",
    "uncertainties",
    "image_quality",
    "speech_candidate",
    "provider",
    "usage",
)

#: **禁止静默清洗**的字段：模型输出这些，说明它在尝试做安全结论或测距，
#: 属于 D-01/D-08 明确禁止的越界行为。把它剥掉虽然技术上安全，
#: 却会让我们永远发现不了"模型正在输出安全结论"这件事——
#: 所以要响亮地失败并记录，而不是悄悄抹掉。
FORBIDDEN_PROVIDER_FIELDS = frozenset(
    {
        "safe_to_walk",
        "distance_m",
        "risk_level",
        "recommended_direction",
        "can_pass",
        "is_safe",
        "walkable",
    }
)

_QUALITY_VALUES = {"good", "acceptable", "poor", "unknown"}
_ZONES = {"front", "front_left", "front_right", "left", "right", "unknown"}
_CONFIDENCE = {"low", "medium", "high", "unknown"}


class AdaptationResult:
    """适配结果。

    - ``data``：映射后的结果（可能为空）
    - ``adapted``：是否发生过字段改名/清洗
    - ``notes``：说明，用于可观测性
    - ``rejected``：非空表示**必须直接失败**，不得进入播报
    """

    __slots__ = ("data", "adapted", "notes", "rejected")

    def __init__(
        self,
        data: dict[str, Any],
        adapted: bool,
        notes: list[str],
        rejected: str | None = None,
    ) -> None:
        self.data = data
        self.adapted = adapted
        self.notes = notes
        self.rejected = rejected


def adapt_scene_result(raw: dict[str, Any]) -> AdaptationResult:
    """把 Provider 的原始输出尽量映射成 SceneResult 形状。"""
    if not isinstance(raw, dict):
        return AdaptationResult({}, True, ["输出不是对象"], rejected="输出不是 JSON 对象")

    # 先查禁止字段：这些不能被"清洗"掉，必须让整次识别失败。
    forbidden = sorted(set(raw) & FORBIDDEN_PROVIDER_FIELDS)
    if forbidden:
        return AdaptationResult(
            {},
            True,
            [f"出现禁止字段 {forbidden}"],
            rejected=f"Provider 输出越界字段 {forbidden}",
        )

    out: dict[str, Any] = {}
    notes: list[str] = []
    adapted = False

    for target, aliases in _ALIASES.items():
        for name in aliases:
            if name in raw and raw[name] not in (None, ""):
                out[target] = raw[name]
                if name != target:
                    notes.append(f"{name}→{target}")
                    adapted = True
                break

    # objects：逐个清洗；无效条目丢弃而不是让整个结果失败——
    # 播报文本才是主体，丢掉一个畸形物件不应该让整次识别作废。
    objects = raw.get("objects")
    cleaned: list[dict[str, Any]] = []
    if isinstance(objects, list):
        for item in objects:
            if not isinstance(item, dict):
                continue
            label = item.get("label") or item.get("name") or item.get("object")
            if not isinstance(label, str) or not label.strip():
                continue
            zone = item.get("zone") or item.get("position") or item.get("direction")
            confidence = item.get("confidence") or item.get("certainty")
            entry: dict[str, Any] = {
                "label": label.strip()[:40],
                "zone": zone if zone in _ZONES else "unknown",
                "confidence": confidence if confidence in _CONFIDENCE else "unknown",
            }
            count = item.get("count_visible") or item.get("count")
            if isinstance(count, int) and 1 <= count <= 20:
                entry["count_visible"] = count
            cleaned.append(entry)
            if set(item) - {"label", "name", "object", "zone", "position",
                            "direction", "confidence", "certainty",
                            "count_visible", "count"}:
                adapted = True
    out["objects"] = cleaned[:20]
    if objects is None:
        notes.append("objects 缺失→空数组（不代表没有物体）")

    # text_regions：可能给成字符串数组，也可能给成对象数组
    regions = raw.get("text_regions")
    norm_regions: list[dict[str, Any]] = []
    if isinstance(regions, list):
        for r in regions:
            if isinstance(r, str) and r.strip():
                norm_regions.append({"text": r.strip()[:300]})
            elif isinstance(r, dict) and isinstance(r.get("text"), str):
                item = {"text": r["text"][:300]}
                if isinstance(r.get("legible"), bool):
                    item["legible"] = r["legible"]
                norm_regions.append(item)
            else:
                adapted = True
    elif isinstance(regions, str) and regions.strip():
        norm_regions.append({"text": regions.strip()[:300]})
        adapted = True
    out["text_regions"] = norm_regions[:10]

    # uncertainties：字符串数组或单个字符串
    unc = raw.get("uncertainties")
    if isinstance(unc, str):
        out["uncertainties"] = [unc[:100]]
        adapted = True
    elif isinstance(unc, list):
        out["uncertainties"] = [str(u)[:100] for u in unc if str(u).strip()][:10]
    else:
        out["uncertainties"] = []

    # 画质：缺失填 unknown，**绝不填 good**
    quality = out.get("image_quality")
    out["image_quality"] = quality if quality in _QUALITY_VALUES else "unknown"

    # 直通字段
    for key in ("schema_version", "request_id", "provider", "usage"):
        if key in raw:
            out[key] = raw[key]

    if not out.get("scene_summary"):
        # 没有摘要不算致命：播报才是主体。给一个中性占位，
        # 不编造场景内容。
        out["scene_summary"] = ""
        notes.append("scene_summary 缺失")

    if not out.get("speech_candidate"):
        notes.append("缺少可播报文本")

    # 丢弃多余键，否则 additionalProperties:false 会拒绝
    out = {k: v for k, v in out.items() if k in _KEEP}

    return AdaptationResult(out, adapted, notes)


def has_speakable_text(data: dict[str, Any]) -> bool:
    """是否拿到了可播报文本。没有它就无法服务用户，必须失败而不是硬凑。"""
    text = data.get("speech_candidate")
    return isinstance(text, str) and bool(text.strip())
