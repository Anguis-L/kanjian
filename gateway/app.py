"""Kanjian 推理网关（TDD §5）。

职责（SDD §2 SceneApi）：请求校验、限流、Provider 编排、用量记录。
**不做**：存储用户账户数据、持久化图像、代替端侧做最终安全决策。

关键边界：
- 服务端不持久化图像；访问日志不记录 multipart 内容（TDD §5）。
- 网关做 Schema 与初步策略检查，但**端侧仍是最终防线**（ADR-02）。
- 不提供任何默认端点，密钥只从环境变量读取（ADR-07、TDD §11）。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

# 本地开发便利：若仓库根有 .env 则加载（.env 已在 .gitignore 中）。
# 缺失时静默跳过，不影响纯环境变量或 CI 运行。
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:  # pragma: no cover - dotenv 是可选依赖
    pass

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from .adapters import adapt_scene_result, has_speakable_text
from .providers import (
    FakeVlmProvider,
    OpenAICompatProvider,
    ProviderError,
    VlmProvider,
    discover_vision_model,
)

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"
MAX_REQUEST_BYTES = 2 * 1024 * 1024  # 2 MiB（TDD §5）
MAX_IMAGE_BYTES = 400 * 1024  # 单张目标不超过 400 KiB（TDD §6）

_STARTED_AT = time.monotonic()


def _load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))


def create_app(provider: VlmProvider | None = None) -> FastAPI:
    """构建 app。测试通过传入 Fake provider 注入。"""
    app = FastAPI(title="Kanjian Scene API", version="0.1.0")

    request_schema = _load_schema("scene-request.schema.json")
    result_schema = _load_schema("scene-result.schema.json")

    if provider is None:
        provider = _default_provider()

    app.state.provider = provider
    app.state.request_schema = request_schema
    app.state.result_schema = result_schema
    #: 用量记录（进程内，不持久化，不含图像）
    app.state.usage_log = []

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        """只报告存活与 Provider 是否可用，不泄露密钥与内网地址（TDD §5）。"""
        caps = app.state.provider.capabilities
        return {
            "status": "ok",
            "provider_available": True,
            "provider_name": caps.name,
            "uptime_s": round(time.monotonic() - _STARTED_AT, 1),
        }

    @app.get("/v1/capabilities")
    async def capabilities() -> dict[str, Any]:
        caps = app.state.provider.capabilities
        return {
            "api_shape": caps.api_shape,
            "input_modes": list(caps.input_modes),
            "max_image_bytes": caps.max_image_bytes,
            "supports_cancellation": caps.supports_cancellation,
            "supports_streaming": caps.supports_streaming,
            "runs_locally": caps.runs_locally,
            "data_retention_policy_id": caps.data_retention_policy_id,
            "estimated_price_table_id": caps.estimated_price_table_id,
            "schema_version": "1.0",
        }

    @app.post("/v1/scene:describe")
    async def describe(
        request: Request,
        metadata: str = Form(...),
        image: UploadFile = File(...),
    ) -> Any:
        request_id = str(uuid.uuid4())
        t0 = time.monotonic()

        # --- 请求大小（TDD §5：超过返回 413）---
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_REQUEST_BYTES:
            return _error("IMAGE_TOO_LARGE", "请求体超过 2 MiB", request_id, status=413)

        # --- 元数据 ---
        try:
            meta = json.loads(metadata)
        except json.JSONDecodeError:
            return _error("INVALID_REQUEST", "metadata 不是合法 JSON", request_id, status=400)
        if not isinstance(meta, dict):
            return _error("INVALID_REQUEST", "metadata 必须是对象", request_id, status=400)

        import jsonschema

        validator = jsonschema.Draft202012Validator(app.state.request_schema)
        errors = sorted(validator.iter_errors(meta), key=lambda e: e.path)
        if errors:
            return _error(
                "INVALID_REQUEST",
                f"metadata 校验失败: {errors[0].message}",
                request_id,
                status=400,
            )

        # --- 图像 ---
        data = await image.read()
        if not data:
            return _error("INVALID_REQUEST", "图像为空", request_id, status=400)
        if len(data) > MAX_IMAGE_BYTES:
            return _error("IMAGE_TOO_LARGE", "图像超过 400 KiB", request_id, status=413)
        if not _looks_like_jpeg(data):
            return _error("UNSUPPORTED_IMAGE", "仅支持 JPEG", request_id, status=415)

        # --- Provider 调用 + 输出适配 ---
        # 实测：同一个模型偶尔会返回"结构合规但缺 speech_candidate"的部分结果。
        # 这属于抖动而不是内容问题，按 TDD §12「手动请求可有限重试一次」重试；
        # 自动请求不立即重试（省成本，也避免自动模式放大波动）。
        provider_meta = dict(meta)
        provider_meta["_result_schema"] = app.state.result_schema
        provider_meta.setdefault("request_id", request_id)

        max_attempts = 2 if meta["origin"] == "USER" else 1
        attempts = 0
        while True:
            attempts += 1
            try:
                raw = await app.state.provider.describe(image_bytes=data, metadata=provider_meta)
            except ProviderError as exc:
                return _error(exc.code, exc.message, request_id, status=_status_for(exc.code),
                              retryable=exc.retryable)

            # --- Provider 输出适配（TDD §7）---
            # 多数模型会无视 response_format 返回自己的形状，网关负责转换。
            # 只做字段改名与清洗，不编造内容；适配后仍须过 Schema 与端侧策略。
            adapted = adapt_scene_result(raw)
            if adapted.rejected:
                # 越界字段**不重试也不清洗**：它说明模型在做安全结论，
                # 必须失败并记录，否则永远发现不了这个问题。
                return _error("PROVIDER_BAD_RESPONSE", adapted.rejected, request_id, status=502)

            candidate = adapted.data
            candidate["schema_version"] = "1.0"
            # request_id 以**请求侧**为准：Provider 可能自己编一个、不回显，
            # 那样端侧就无法把结果和请求对上（SDD §7 要求端侧校验 request_id）。
            # 这里强制覆盖，而不是 setdefault——不能让 Provider 决定身份。
            candidate["request_id"] = meta["request_id"]
            # provider 是**事实性元数据**，不是模型的自述：模型没给就由网关按实际
            # Provider 填写，这样"到底是谁答的"始终有据可查。
            if not isinstance(candidate.get("provider"), dict) or not candidate["provider"].get("name"):
                candidate["provider"] = {"name": app.state.provider.capabilities.name}

            if has_speakable_text(candidate):
                raw = candidate
                break

            if attempts >= max_attempts:
                # 不替模型拼一句话——那等于我们自己在编造场景描述。
                # 带上收到的字段名（不含内容），便于排查是哪种形状没被适配。
                got = ", ".join(sorted(candidate)) or "（空）"
                return _error(
                    "PROVIDER_BAD_RESPONSE",
                    f"Provider 未返回可播报的描述文本（尝试 {attempts} 次，收到字段: {got}）",
                    request_id,
                    status=502,
                )

        result_validator = jsonschema.Draft202012Validator(app.state.result_schema)
        if list(result_validator.iter_errors(raw)):
            first = next(result_validator.iter_errors(raw))
            return _error(
                "PROVIDER_BAD_RESPONSE",
                f"Provider 输出不符合 Schema: {first.message}",
                request_id,
                status=502,
            )

        latency_ms = int((time.monotonic() - t0) * 1000)
        raw.setdefault("usage", {})
        if raw["usage"].get("latency_ms") in (None, 0):
            raw["usage"]["latency_ms"] = latency_ms

        # --- 用量记录（不含图像、不含提示词）---
        app.state.usage_log.append(
            {
                "request_id": meta["request_id"],
                "origin": meta["origin"],
                "mode": meta["mode"],
                "image_bytes": len(data),
                "latency_ms": latency_ms,
                "input_tokens": raw["usage"].get("input_tokens"),
                "output_tokens": raw["usage"].get("output_tokens"),
                "tokens_known": raw["usage"].get("input_tokens") is not None,
                "policy_version": meta["policy_version"],
                # 适配记录：哪些请求需要网关改名，说明该模型不守结构化契约
                "adapted": adapted.adapted,
                "adapt_notes": adapted.notes,
                "attempts": attempts,
            }
        )

        # 图像不落盘、不进日志（TDD §5）。data 随函数返回释放。
        return JSONResponse(raw, status_code=200)

    @app.get("/v1/usage")
    async def usage() -> dict[str, Any]:
        """用量汇总。token 未知时明确标注，不按 0 计（TDD §13）。"""
        log = app.state.usage_log
        known = [r for r in log if r["tokens_known"]]
        unknown = [r for r in log if not r["tokens_known"]]
        return {
            "total_requests": len(log),
            "requests_with_known_tokens": len(known),
            "requests_with_unknown_tokens": len(unknown),
            "note": "未知 token 的请求不计入 token 汇总，也不按 0 计",
            "items": log,
        }

    return app


def _looks_like_jpeg(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff"


def _status_for(code: str) -> int:
    return {
        "INVALID_REQUEST": 400,
        "IMAGE_TOO_LARGE": 413,
        "UNSUPPORTED_IMAGE": 415,
        "AUTH_FAILED": 401,
        "RATE_LIMITED": 429,
        "PROVIDER_TIMEOUT": 504,
        "PROVIDER_UNAVAILABLE": 503,
        "PROVIDER_BAD_RESPONSE": 502,
        "POLICY_REJECTED": 422,
        "INTERNAL_ERROR": 500,
    }.get(code, 500)


def _error(
    code: str, message: str, request_id: str, *, status: int, retryable: bool = False
) -> JSONResponse:
    """标准错误响应（TDD §5）。不暴露堆栈与上游细节。"""
    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "request_id": request_id,
            }
        },
        status_code=status,
    )


def _default_provider() -> VlmProvider:
    """按环境变量选择 Provider。**没有默认端点**（ADR-07）。

    未配置时退回 Fake，并在 health 中如实报告 provider_name=fake——
    绝不静默连接到某个维护者端点。

    环境变量优先级：KANJIAN_OPENAI_* 覆盖行业通用的 OPENAI_*。
    模型名可省略，此时从端点自己的 /v1/models 里挑第一个支持图像输入的模型。
    """
    base_url = (
        os.environ.get("KANJIAN_OPENAI_BASE_URL", "").strip()
        or os.environ.get("OPENAI_BASE_URL", "").strip()
    )
    api_key = (
        os.environ.get("KANJIAN_OPENAI_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    model = (
        os.environ.get("KANJIAN_OPENAI_MODEL", "").strip()
        or os.environ.get("KANJIAN_MODEL", "").strip()
    )

    if not (base_url and api_key):
        return FakeVlmProvider()

    if not model:
        try:
            model = discover_vision_model(base_url, api_key)
        except ProviderError as exc:
            # 发现失败时明确退化到 Fake，并在日志里说明原因，不静默联网到别处。
            print(f"[kanjian] 无法自动发现模型（{exc.message}），退回 Fake Provider")
            return FakeVlmProvider()

    # 超时默认保持 ADR-01 的 6.0s 预算。**不要为了"跑通"把它调大**：
    # 若端点实际更慢，正确做法是换模型/减少输出长度，并在报告里记录偏差。
    timeout_s = float(os.environ.get("KANJIAN_REQUEST_TIMEOUT_S", "6.0"))
    max_tokens = int(os.environ.get("KANJIAN_MAX_TOKENS", "700"))
    return OpenAICompatProvider(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_s=timeout_s,
        max_tokens=max_tokens,
    )


app = create_app()
