"""真实 Provider 端到端冒烟脚本。

这把整条链路对着**真实模型**跑一遍，而不是 Fake 回放：

    生成测试图 → 压缩 → 调端点 → Schema 校验 → OutputPolicyEngine → 播报文本

它不替代测试（pytest 才是），但能回答一个重要问题：
**真实模型在不完全听话的情况下，策略层还挡不挡得住？**

运行：
    python scripts/smoke_provider.py
    python scripts/smoke_provider.py --image 实拍.jpg      # 用真实照片
    python scripts/smoke_provider.py --json                  # 只输出 JSON
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import jsonschema

from kanjian.policy import check, check_ocr_text, find_violations
from gateway.providers import (
    FakeVlmProvider,
    OpenAICompatProvider,
    ProviderError,
    discover_vision_model,
)

SCHEMAS = REPO / "schemas"

try:
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env")
except ImportError:
    pass


def _load(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 测试图像
# ---------------------------------------------------------------------------

def make_test_image() -> bytes:
    """生成一张合成场景图：桌面 + 杯子 + 一块带文字的牌子。

    用合成图而不是真实照片，是因为测试数据不得包含真实用户图像，
    而仓库里也不该出现照片。图像内容简单到模型能描述、又包含文字，
    可以同时验证 objects 与 text_regions 两条通道。
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1024, 768), (210, 200, 185))
    d = ImageDraw.Draw(img)

    # 桌面
    d.rectangle([0, 520, 1024, 768], fill=(140, 100, 65))
    # 杯子（圆柱近似）
    d.rectangle([430, 400, 540, 540], fill=(245, 245, 245), outline=(120, 120, 120), width=3)
    d.ellipse([430, 380, 540, 420], fill=(250, 250, 250), outline=(120, 120, 120), width=3)
    # 一本摊开的书
    d.rectangle([650, 470, 880, 520], fill=(235, 232, 220), outline=(100, 100, 100), width=2)
    d.line([765, 470, 765, 520], fill=(150, 150, 150), width=2)
    # 墙上带文字的牌子
    d.rectangle([80, 120, 420, 240], fill=(255, 255, 255), outline=(60, 60, 60), width=3)
    try:
        from PIL import ImageFont

        font = ImageFont.truetype("msyh.ttc", 56)
    except Exception:
        font = None
    d.text((110, 150), "营业中", fill=(20, 20, 20), font=font)
    d.text((110, 210), "9:00-21:00", fill=(40, 40, 40), font=font)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    data = buf.getvalue()

    # 与 TDD §6 的压缩约束对齐：长边 ≤1280，目标 ≤400 KiB
    assert img.size[0] <= 1280
    print(f"  测试图：{img.size[0]}x{img.size[1]}, JPEG {len(data) / 1024:.1f} KiB")
    return data


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

async def run(image_bytes: bytes, *, as_json: bool, timeout_s: float) -> int:
    request_schema = _load("scene-request.schema.json")
    result_schema = _load("scene-result.schema.json")

    base_url = os.environ.get("KANJIAN_OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("KANJIAN_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("KANJIAN_OPENAI_MODEL") or os.environ.get("KANJIAN_MODEL")

    if not base_url or not api_key:
        print("未配置端点，使用 Fake Provider（离线演示）")
        provider = FakeVlmProvider()
    else:
        if not model:
            print("未指定模型，尝试从端点发现支持图像的模型…")
            model = discover_vision_model(base_url, api_key)
            print(f"  发现模型：{model}")
        provider = OpenAICompatProvider(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_s=timeout_s,
            max_tokens=int(os.environ.get("KANJIAN_MAX_TOKENS", "700")),
        )
        print(f"  Provider：{provider.capabilities.name}")
        print(f"  调用地址：{provider.chat_url}")
        print(f"  超时：{timeout_s}s（诊断值；生产预算为 ADR-01 的 6.0s）")

    metadata = {
        "schema_version": "1.0",
        "request_id": "smoke-0001-aaaa",
        "session_id": "smoke-session-01",
        "frame_timestamp_ms": 1793500000000,
        "origin": "USER",
        "mode": "ASK",
        "locale": "zh-CN",
        "max_speech_chars": 40,
        "policy_version": "2026-11-01",
        "query": "前面是什么",
    }
    # 本地先用同一份 Schema 校验请求，两端共用一份契约
    jsonschema.Draft202012Validator(request_schema).validate(metadata)

    print("\n→ 调用 Provider…")
    t0 = time.monotonic()
    try:
        raw = await provider.describe(
            image_bytes=image_bytes, metadata={**metadata, "_result_schema": result_schema}
        )
    except ProviderError as exc:
        elapsed = time.monotonic() - t0
        print(f"✗ Provider 失败（{elapsed:.2f}s）：{exc.code} / {exc.message}（retryable={exc.retryable}）")
        return 1
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    print(f"  完成，耗时 {elapsed_ms / 1000:.2f}s")

    print("→ Schema 校验…")
    errors = sorted(
        jsonschema.Draft202012Validator(result_schema).iter_errors(raw), key=lambda e: list(e.path)
    )
    if errors:
        first = errors[0]
        print(f"✗ 不符合 Schema：{first.message} @ {list(first.path)}")
        print("  按网关规则，这会被映射为 PROVIDER_BAD_RESPONSE，不进入播报。")
        if as_json:
            print(json.dumps(raw, ensure_ascii=False, indent=2))
        return 2
    print("✓ Schema 通过")

    print("\n→ 策略层…")
    candidate = raw.get("speech_candidate", "")
    result = check(candidate, max_chars=metadata["max_speech_chars"])
    vios = find_violations(candidate)
    if vios:
        for v in vios:
            print(f"  ! 命中 {v.category.value}: 「{v.matched}」")
    else:
        print("  无违规")

    print("\n" + "─" * 64)
    print(f"模型原始 speech_candidate：")
    print(f"  「{candidate}」")
    print(f"\n最终播报（经策略层）：")
    print(f"  「{result.text}」")
    if not result.allowed:
        print(f"  （整条拒绝：{result.reason}）")
    elif result.reason == "CLAUSE_STRIPPED":
        print("  （违规从句已删除，保留可靠事实）")

    print("\n结构化结果：")
    print(f"  scene_summary : {raw.get('scene_summary')}")
    print(f"  objects       : {len(raw.get('objects', []))} 个")
    for obj in raw.get("objects", [])[:8]:
        print(
            f"    - {obj.get('label')} / zone={obj.get('zone')} "
            f"/ confidence={obj.get('confidence')}"
        )
    print(f"  uncertainties : {raw.get('uncertainties')}")
    print(f"  image_quality : {raw.get('image_quality')}")

    for tr in raw.get("text_regions", [])[:5]:
        ocr = check_ocr_text(str(tr.get("text", "")))
        verdict = "可朗读" if ocr.allowed else f"拒绝（{ocr.reason}）"
        print(f"  text_region   : 「{tr.get('text')}」 → {verdict}")

    usage = raw.get("usage") or {}
    print(f"\n用量：in={usage.get('input_tokens')} out={usage.get('output_tokens')} "
          f"latency={usage.get('latency_ms')}ms")

    if as_json:
        print("\n原始 JSON：")
        print(json.dumps(raw, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Kanjian 真实 Provider 冒烟")
    ap.add_argument("--image", help="用指定图片代替合成测试图")
    ap.add_argument("--json", action="store_true", help="同时打印原始 JSON")
    ap.add_argument(
        "--timeout",
        type=float,
        default=45.0,
        help="诊断用超时（秒）。生产预算是 ADR-01 的 6.0s；这里默认放宽以便看清真实延迟",
    )
    args = ap.parse_args()

    print("Kanjian 真实 Provider 冒烟")
    print("=" * 64)

    if args.image:
        path = Path(args.image)
        data = path.read_bytes()
        print(f"  图片：{path}（{len(data) / 1024:.1f} KiB）")
    else:
        data = make_test_image()

    if len(data) > 400 * 1024:
        print(f"✗ 图像 {len(data) / 1024:.1f} KiB 超过 400 KiB 上限（TDD §6）")
        return 3

    return asyncio.run(run(data, as_json=args.json, timeout_s=args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())