"""通过 HTTP 调网关的完整客户端示例（可当集成冒烟用）。

与 smoke_provider.py 的区别：那个直连 Provider，这个走**真实 HTTP 网关**，
包含 multipart 上传、请求校验、结果校验与端侧策略。

用法：
    # 先启动网关
    uvicorn gateway.app:app --port 8000
    # 再运行
    python scripts/http_client_example.py --url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import httpx

from kanjian.policy import check, check_ocr_text


def make_image() -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1024, 768), (205, 198, 185))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 540, 1024, 768], fill=(135, 98, 62))
    d.rectangle([420, 400, 545, 540], fill=(244, 244, 244), outline=(110, 110, 110), width=3)
    d.ellipse([420, 378, 545, 420], fill=(250, 250, 250), outline=(110, 110, 110), width=3)
    try:
        font = ImageFont.truetype("msyh.ttc", 52)
    except Exception:
        font = None
    d.rectangle([80, 110, 430, 200], fill=(255, 255, 255), outline=(50, 50, 50), width=3)
    d.text((105, 130), "会议室 301", fill=(20, 20, 20), font=font)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--image", help="用指定图片")
    args = ap.parse_args()

    if args.image:
        image = Path(args.image).read_bytes()
    else:
        image = make_image()
    print(f"图像：{len(image) / 1024:.1f} KiB")

    metadata = {
        "schema_version": "1.0",
        "request_id": "http-example-0001",
        "session_id": "http-session-01",
        "frame_timestamp_ms": 1793500000000,
        "origin": "USER",
        "mode": "ASK",
        "locale": "zh-CN",
        "max_speech_chars": 40,
        "policy_version": "2026-11-01",
        "query": "前面是什么",
    }

    print(f"\n→ POST {args.url}/v1/scene:describe")
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            f"{args.url}/v1/scene:describe",
            data={"metadata": json.dumps(metadata, ensure_ascii=False)},
            files={"image": ("frame.jpg", image, "image/jpeg")},
        )

        print(f"← HTTP {resp.status_code}")
        if resp.status_code != 200:
            print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
            return 1

        body = resp.json()
        usage_summary = client.get(f"{args.url}/v1/usage").json()
    print(f"  scene_summary : {body.get('scene_summary')}")
    print(f"  objects       : {len(body.get('objects', []))} 个")
    for o in body.get("objects", []):
        print(f"    - {o['label']} / {o['zone']} / {o['confidence']}")
    print(f"  provider      : {body.get('provider')}")
    print(f"  usage         : {body.get('usage')}")

    print("\n端侧最终策略（ADR-02：端侧才是最终防线）")
    result = check(body.get("speech_candidate", ""), max_chars=metadata["max_speech_chars"])
    print(f"  模型候选 → 「{body.get('speech_candidate')}」")
    print(f"  最终播报 → 「{result.text}」")
    if not result.allowed:
        print(f"  （拒绝：{result.reason}）")

    for tr in body.get("text_regions", []):
        text = str(tr.get("text", ""))
        ocr = check_ocr_text(text)
        print(f"  OCR 「{text}」 → {'可朗读' if ocr.allowed else '拒绝朗读'}")

    usage = usage_summary
    print(f"\n网关用量：共 {usage['total_requests']} 次，"
          f"未知 token {usage['requests_with_unknown_tokens']} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())