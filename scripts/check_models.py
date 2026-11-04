"""检查某个端点上哪些模型真正遵守 SceneResult 契约。

为什么需要这个：`auto/xxx` 这类**路由组**前缀每次可能路由到不同模型，
其中一些会无视 `response_format`，返回自己的形状（如 `{"scene":..,"speech":..}`）。
网关会正确地拒掉它们（PROVIDER_BAD_RESPONSE），但用户看到的就是失败。

结论：**生产配置应固定具体模型 ID，不要用路由组**。这个脚本帮你挑。

用法：
    python scripts/check_models.py
    python scripts/check_models.py --models auto/deepseek-v4.1-flash,workbuddy/deepseek-v4.1-flash
    python scripts/check_models.py --runs 2
"""

from __future__ import annotations

import argparse
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

try:
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env")
except ImportError:
    pass

SCHEMA = json.loads((REPO / "schemas" / "scene-result.schema.json").read_text(encoding="utf-8"))

SYSTEM = (
    "你是视觉信息描述器，只描述可见内容，不给行动建议，不判断安全，不输出精确距离。"
    "只返回符合给定 JSON Schema 的 JSON，不要 Markdown 代码块，不要解释。"
)


def make_image() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (960, 720), (208, 200, 186))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 500, 960, 720], fill=(138, 100, 64))
    d.rectangle([400, 360, 520, 500], fill=(245, 245, 245), outline=(115, 115, 115), width=3)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def probe(base_url: str, key: str, model: str, image_b64: str, timeout: float) -> dict:
    import urllib.request

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "描述这个场景 max_speech_chars=40"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image_b64}},
                ],
            },
        ],
        "max_tokens": 700,
        "temperature": 0.2,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "scene_result", "schema": SCHEMA, "strict": False},
        },
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode())
    elapsed = time.monotonic() - t0

    content = body["choices"][0]["message"].get("content") or ""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text[4:] if text.lower().startswith("json") else text

    parsed = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e > s:
            try:
                parsed = json.loads(text[s : e + 1])
            except json.JSONDecodeError:
                parsed = None

    if parsed is None:
        return {"model": model, "ok": False, "why": "不是合法 JSON", "latency": elapsed}

    errors = list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(parsed))
    if errors:
        extra = set(parsed) - set(SCHEMA["properties"])
        why = f"字段不符（多出 {sorted(extra)}）" if extra else errors[0].message
        return {"model": model, "ok": False, "why": why, "latency": elapsed}

    usage = body.get("usage") or {}
    served = body.get("model", "?")
    return {
        "model": model,
        "ok": True,
        "why": "",
        "latency": elapsed,
        "served_by": served,
        "speech": (parsed.get("speech_candidate") or "")[:38],
        "in_tokens": usage.get("prompt_tokens"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", help="逗号分隔；默认测常见候选")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()

    base_url = os.environ.get("KANJIAN_OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    key = os.environ.get("KANJIAN_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not (base_url and key):
        print("未配置端点或 key")
        return 1

    default_models = [
        "auto/deepseek-v4.1-flash",
        "workbuddy/deepseek-v4.1-flash",
        "trae-cn/deepseek-v4.1-flash",
        "opendesign/deepseek-v4.1-flash",
        "loomy/DeepSeek-V4.1-Flash",
        "raccoon/sn-deepseek-v4-1-flash-1017",
        "workbuddy/glm-5.3-flash",
    ]
    models = args.models.split(",") if args.models else default_models

    image_b64 = base64.b64encode(make_image()).decode("ascii")
    print(f"端点：{base_url}   每个模型 {args.runs} 次")
    print(f"{'模型':44} {'结果':6} {'P50':>7} 说明")
    print("-" * 100)

    good: list[tuple[str, float]] = []
    for model in models:
        results = []
        for _ in range(args.runs):
            try:
                results.append(probe(base_url, key, model, image_b64, args.timeout))
            except Exception as exc:  # noqa: BLE001
                results.append({"model": model, "ok": False, "why": str(exc)[:40], "latency": 0.0})

        ok_runs = [r for r in results if r["ok"]]
        lat = sorted(r["latency"] for r in results if r["latency"])
        p50 = lat[len(lat) // 2] if lat else 0.0
        verdict = f"{len(ok_runs)}/{len(results)}"
        why = "" if ok_runs else (results[0]["why"] if results else "")
        if ok_runs:
            why = ok_runs[0].get("speech", "")
        print(f"{model:44} {verdict:6} {p50:6.2f}s {why}")
        if len(ok_runs) == len(results):
            good.append((model, p50))

    print()
    if good:
        best = min(good, key=lambda x: x[1])
        print("全部通过且最快的：")
        for m, p in sorted(good, key=lambda x: x[1]):
            print(f"  {m:44} {p:.2f}s")
        print()
        print(f"建议写进 .env：KANJIAN_OPENAI_MODEL={best[0]}")
    else:
        print("没有模型完全通过——检查端点是否支持 response_format")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())