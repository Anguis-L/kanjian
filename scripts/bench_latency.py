"""分辨率—延迟基准测试。

回答一个具体问题：TDD §6 的"长边 ≤1280px、JPEG q80"是不是合理起点？
用同一张图在不同分辨率下各跑若干次，记录延迟与 prompt_tokens。

用法：
    python scripts/bench_latency.py                 # 默认 1280/960/640/480，每档 3 次
    python scripts/bench_latency.py --runs 5
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env")
except ImportError:
    pass

SCHEMA = json.loads((REPO / "schemas" / "scene-result.schema.json").read_text(encoding="utf-8"))


def make_scene() -> "object":
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1600, 1200), (210, 200, 185))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 820, 1600, 1200], fill=(140, 100, 65))
    d.rectangle([650, 600, 820, 820], fill=(245, 245, 245), outline=(120, 120, 120), width=4)
    d.ellipse([650, 570, 820, 630], fill=(250, 250, 250), outline=(120, 120, 120), width=4)
    try:
        from PIL import ImageFont

        font = ImageFont.truetype("msyh.ttc", 80)
    except Exception:
        font = None
    d.rectangle([110, 170, 590, 360], fill=(255, 255, 255), outline=(60, 60, 60), width=4)
    d.text((150, 215), "营业中", fill=(20, 20, 20), font=font)
    return img


def encode(img, long_edge: int, quality: int = 80) -> bytes:
    from PIL import Image

    w, h = img.size
    if max(w, h) > long_edge:
        scale = long_edge / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def call(base_url: str, api_key: str, model: str, image_bytes: bytes, timeout: float) -> tuple[float, dict]:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是视觉信息描述器，只描述可见内容，不给行动建议，"
                    "不判断安全，不输出精确距离。只返回符合 JSON Schema 的 JSON。"
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "描述场景 max_speech_chars=40"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + b64},
                    },
                ],
            },
        ],
        "max_tokens": 600,
        "temperature": 0.2,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "scene_result", "schema": SCHEMA, "strict": False},
        },
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode())
    return time.time() - t0, body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--sizes", default="1280,960,640,480")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    base_url = os.environ.get("KANJIAN_OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("KANJIAN_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("KANJIAN_OPENAI_MODEL") or "auto/deepseek-v4.1-flash"
    if not base_url or not api_key:
        print("未配置端点，无法基准测试")
        return 1

    src = make_scene()
    sizes = [int(s) for s in args.sizes.split(",")]

    print(f"model={model}  runs/size={args.runs}  timeout={args.timeout}s")
    print(f"{'long_edge':>10} {'KiB':>7} {'in_tok':>7} {'json':>5} {'P50':>7} {'max':>7}")
    print("-" * 56)

    rows = []
    for size in sizes:
        data = encode(src, size)
        times, toks, json_ok = [], [], 0
        for _ in range(args.runs):
            try:
                dt, body = call(base_url, api_key, model, data, args.timeout)
            except Exception as exc:  # noqa: BLE001
                print(f"  size={size} 失败: {str(exc)[:70]}")
                continue
            times.append(dt)
            usage = body.get("usage") or {}
            toks.append(usage.get("prompt_tokens"))
            content = (body["choices"][0]["message"].get("content") or "").strip()
            if content.startswith("{"):
                json_ok += 1
        if times:
            rows.append(
                {
                    "size": size,
                    "kib": len(data) / 1024,
                    "tokens": statistics.median([t for t in toks if t]),
                    "json": f"{json_ok}/{len(times)}",
                    "p50": statistics.median(times),
                    "max": max(times),
                }
            )
            r = rows[-1]
            print(
                f"{r['size']:>10} {r['kib']:>7.1f} {r['tokens']:>7} "
                f"{r['json']:>5} {r['p50']:>6.2f}s {r['max']:>6.2f}s"
            )

    print()
    print("目标（NFR-PERF-001）：P50 ≤ 2.0s，P95 ≤ 4.0s（成功样本口径）")
    if rows:
        best = min(rows, key=lambda r: r["p50"])
        print(f"当前最快档位：long_edge={best['size']}，P50={best['p50']:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())