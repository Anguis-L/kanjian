"""网关契约测试（L2）。验证两端共用同一组正反例（TDD §14）。"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.providers import FakeVlmProvider, ProviderError

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


def _fixtures() -> dict:
    return json.loads((SCHEMAS / "fixtures.json").read_text(encoding="utf-8"))


def _jpeg_bytes() -> bytes:
    """最小合法 JPEG 头（足够通过魔数检查）。"""
    return b"\xff\xd8\xff\xe0" + b"\x00" * 64


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(FakeVlmProvider()))


VALID_META = _fixtures()["scene_request"]["valid"][0]["value"]


# ---------------------------------------------------------------------------
# 健康与能力
# ---------------------------------------------------------------------------

def test_health(client: TestClient) -> None:
    r = client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # 不得泄露密钥或内网地址
    assert "api_key" not in json.dumps(body).lower()


def test_capabilities_declares_shape(client: TestClient) -> None:
    r = client.get("/v1/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["api_shape"] == "fixture"
    assert "max_image_bytes" in body


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------

def test_describe_success(client: TestClient) -> None:
    r = client.post(
        "/v1/scene:describe",
        data={"metadata": json.dumps(VALID_META)},
        files={"image": ("frame.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scene_summary"]
    assert body["speech_candidate"]
    assert "usage" in body


def test_result_passes_result_schema(client: TestClient) -> None:
    """返回结果必须能通过 SceneResult schema。"""
    import jsonschema

    r = client.post(
        "/v1/scene:describe",
        data={"metadata": json.dumps(VALID_META)},
        files={"image": ("frame.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")},
    )
    schema = json.loads((SCHEMAS / "scene-result.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(r.json())


# ---------------------------------------------------------------------------
# 请求校验（用 fixtures 的反例，与端侧同一组）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "case",
    _fixtures()["scene_request"]["invalid"],
    ids=[c["case"] for c in _fixtures()["scene_request"]["invalid"]],
)
def test_invalid_metadata_rejected(client: TestClient, case: dict) -> None:
    r = client.post(
        "/v1/scene:describe",
        data={"metadata": json.dumps(case["value"])},
        files={"image": ("frame.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")},
    )
    assert r.status_code == 400, f"{case['case']} 未被拒: {r.status_code} {r.text}"
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_bad_json_metadata(client: TestClient) -> None:
    r = client.post(
        "/v1/scene:describe",
        data={"metadata": "{not json"},
        files={"image": ("frame.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_empty_image(client: TestClient) -> None:
    r = client.post(
        "/v1/scene:describe",
        data={"metadata": json.dumps(VALID_META)},
        files={"image": ("frame.jpg", io.BytesIO(b""), "image/jpeg")},
    )
    assert r.status_code == 400


def test_non_jpeg_rejected(client: TestClient) -> None:
    r = client.post(
        "/v1/scene:describe",
        data={"metadata": json.dumps(VALID_META)},
        files={"image": ("frame.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32), "image/png")},
    )
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "UNSUPPORTED_IMAGE"


def test_image_too_large(client: TestClient) -> None:
    big = b"\xff\xd8\xff" + b"\x00" * (400 * 1024 + 1)
    r = client.post(
        "/v1/scene:describe",
        data={"metadata": json.dumps(VALID_META)},
        files={"image": ("frame.jpg", io.BytesIO(big), "image/jpeg")},
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "IMAGE_TOO_LARGE"


# ---------------------------------------------------------------------------
# Provider 错误映射
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "code,expected_status,retryable",
    [
        ("PROVIDER_TIMEOUT", 504, True),
        ("PROVIDER_UNAVAILABLE", 503, True),
        ("RATE_LIMITED", 429, True),
        ("PROVIDER_BAD_RESPONSE", 502, False),
    ],
)
def test_provider_error_mapped(code: str, expected_status: int, retryable: bool) -> None:
    class FailingProvider:
        @property
        def capabilities(self):
            return FakeVlmProvider().capabilities

        async def describe(self, *, image_bytes, metadata):
            raise ProviderError(code, "识别服务暂时不可用", retryable=retryable)

    client = TestClient(create_app(FailingProvider()))
    r = client.post(
        "/v1/scene:describe",
        data={"metadata": json.dumps(VALID_META)},
        files={"image": ("frame.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")},
    )
    assert r.status_code == expected_status
    body = r.json()
    assert body["error"]["code"] == code
    assert body["error"]["retryable"] is retryable
    assert body["error"]["request_id"]


def test_provider_bad_enum_is_normalized_not_rejected() -> None:
    """非法枚举按 SDD §7.2 做白名单映射，而不是整条拒绝。

    文档规定 Normalizer 的职责就是"方位仅 front/front_left/... 白名单映射"，
    所以 zone="north" 应归一为 unknown，用户仍能拿到描述。
    前提是文本本身没问题——安全过滤在端侧照跑。
    """
    provider = FakeVlmProvider(
        response={
            "schema_version": "1.0",
            "request_id": "x",
            "scene_summary": "走廊",
            "objects": [{"label": "椅子", "zone": "north", "confidence": "medium"}],
            "text_regions": [],
            "uncertainties": [],
            "image_quality": "good",
            "speech_candidate": "前方有椅子。",
            "provider": {"name": "fake"},
        }
    )
    client = TestClient(create_app(provider))
    r = client.post(
        "/v1/scene:describe",
        data={"metadata": json.dumps(VALID_META)},
        files={"image": ("frame.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")},
    )
    assert r.status_code == 200
    assert r.json()["objects"][0]["zone"] == "unknown"


def test_provider_output_without_speakable_text_rejected() -> None:
    """没有可播报文本 → 502。**网关不替模型拼一句话**：
    那等于我们自己在编造场景描述。"""
    provider = FakeVlmProvider(
        response={
            "schema_version": "1.0",
            "request_id": "x",
            "scene_summary": "走廊",
            "objects": [],
            "text_regions": [],
            "uncertainties": [],
            "image_quality": "good",
            "provider": {"name": "fake"},
        }
    )
    client = TestClient(create_app(provider))
    r = client.post(
        "/v1/scene:describe",
        data={"metadata": json.dumps(VALID_META)},
        files={"image": ("frame.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")},
    )
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "PROVIDER_BAD_RESPONSE"


def test_provider_shape_with_speech_alias_accepted() -> None:
    """实测多数模型返回 {"scene":..,"speech":..}——适配后应当可用。"""
    provider = FakeVlmProvider(
        response={"scene": "像是走廊", "speech": "正前方有一把椅子。", "confidence": 0.8}
    )
    client = TestClient(create_app(provider))
    r = client.post(
        "/v1/scene:describe",
        data={"metadata": json.dumps(VALID_META)},
        files={"image": ("frame.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["speech_candidate"] == "正前方有一把椅子。"


def test_dangerous_field_is_not_silently_scrubbed() -> None:
    """危险字段必须响亮失败，不能被适配层悄悄剥掉。

    剥掉虽然能让请求"成功"，但我们就永远不知道模型在输出安全结论。
    """
    provider = FakeVlmProvider(
        response={
            "schema_version": "1.0",
            "request_id": "x",
            "scene_summary": "走廊",
            "objects": [],
            "text_regions": [],
            "uncertainties": [],
            "image_quality": "good",
            "speech_candidate": "前方有椅子。",
            "provider": {"name": "fake"},
            "safe_to_walk": True,
        }
    )
    client = TestClient(create_app(provider))
    r = client.post(
        "/v1/scene:describe",
        data={"metadata": json.dumps(VALID_META)},
        files={"image": ("frame.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")},
    )
    assert r.status_code == 502
    assert "safe_to_walk" in r.json()["error"]["message"]


# ---------------------------------------------------------------------------
# 用量记录（NFR-COST-001）
# ---------------------------------------------------------------------------

def test_usage_log_records_without_image(client: TestClient) -> None:
    client.post(
        "/v1/scene:describe",
        data={"metadata": json.dumps(VALID_META)},
        files={"image": ("frame.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")},
    )
    r = client.get("/v1/usage")
    body = r.json()
    assert body["total_requests"] == 1
    # 不含图像内容，只含字节数
    serialized = json.dumps(body)
    assert "base64" not in serialized.lower()
    assert body["items"][0]["image_bytes"] == len(_jpeg_bytes())


def test_unknown_tokens_not_counted_as_zero(client: TestClient) -> None:
    """Fake Provider 返回 input_tokens=null → 必须标为 unknown（TDD §13）。"""
    client.post(
        "/v1/scene:describe",
        data={"metadata": json.dumps(VALID_META)},
        files={"image": ("frame.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")},
    )
    body = client.get("/v1/usage").json()
    assert body["requests_with_unknown_tokens"] == 1
    assert body["requests_with_known_tokens"] == 0
    assert body["items"][0]["tokens_known"] is False


# ---------------------------------------------------------------------------
# request_id 权威性（SDD §7）
# ---------------------------------------------------------------------------

def test_request_id_comes_from_request_not_provider() -> None:
    """Provider 自造 request_id 时，网关必须用请求侧的，而不是回显 Provider 的。

    真实踩到过：模型返回 `scene-0001-blank`，网关原样记录，
    导致端侧无法把结果和请求对上（迟到结果隔离就失效了）。
    """
    provider = FakeVlmProvider(
        response={
            "schema_version": "1.0",
            "request_id": "provider-invented-id",
            "scene_summary": "走廊",
            "objects": [],
            "text_regions": [],
            "uncertainties": [],
            "image_quality": "good",
            "speech_candidate": "像是一条走廊。",
            "provider": {"name": "fake"},
        }
    )
    client = TestClient(create_app(provider))
    r = client.post(
        "/v1/scene:describe",
        data={"metadata": json.dumps(VALID_META)},
        files={"image": ("frame.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")},
    )
    assert r.status_code == 200
    assert r.json()["request_id"] == VALID_META["request_id"]


def test_usage_log_uses_request_id() -> None:
    """用量记录必须用请求侧 request_id，便于按请求追溯。"""
    client = TestClient(create_app(FakeVlmProvider()))
    client.post(
        "/v1/scene:describe",
        data={"metadata": json.dumps(VALID_META)},
        files={"image": ("frame.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")},
    )
    items = client.get("/v1/usage").json()["items"]
    assert items[0]["request_id"] == VALID_META["request_id"]


class _FlakyProvider:
    """前 N 次返回"缺 speech_candidate"的部分结果，之后正常。

    实测依据：同一模型偶尔会只给 objects/scene_summary 而不给可播报文本。
    """

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    @property
    def capabilities(self):
        return FakeVlmProvider().capabilities

    async def describe(self, *, image_bytes, metadata):
        self.calls += 1
        base = {
            "schema_version": "1.0",
            "request_id": metadata.get("request_id", "x"),
            "scene_summary": "走廊",
            "objects": [],
            "text_regions": [],
            "uncertainties": [],
            "image_quality": "acceptable",
            "provider": {"name": "fake"},
        }
        if self.calls <= self.fail_times:
            return dict(base)  # 缺 speech_candidate
        base["speech_candidate"] = "正前方有一把椅子。"
        return base


def _post(client: TestClient, origin: str = "USER"):
    meta = dict(VALID_META)
    meta["origin"] = origin
    return client.post(
        "/v1/scene:describe",
        data={"metadata": json.dumps(meta)},
        files={"image": ("frame.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")},
    )


def test_user_request_retries_once_on_missing_speech() -> None:
    """手动请求：缺可播报文本时重试一次（TDD §12）。"""
    provider = _FlakyProvider(fail_times=1)
    client = TestClient(create_app(provider))
    r = _post(client, "USER")
    assert r.status_code == 200, r.text
    assert r.json()["speech_candidate"] == "正前方有一把椅子。"
    assert provider.calls == 2


def test_user_request_fails_after_retry() -> None:
    """重试仍失败 → 502，且**不编造**文本。"""
    provider = _FlakyProvider(fail_times=99)
    client = TestClient(create_app(provider))
    r = _post(client, "USER")
    assert r.status_code == 502
    assert provider.calls == 2
    assert "尝试 2 次" in r.json()["error"]["message"]


def test_auto_request_does_not_retry() -> None:
    """自动请求不立即重试：省成本，也避免自动模式放大模型波动。"""
    provider = _FlakyProvider(fail_times=99)
    client = TestClient(create_app(provider))
    r = _post(client, "AUTO")
    assert r.status_code == 502
    assert provider.calls == 1


def test_dangerous_field_not_retried() -> None:
    """越界字段不重试：它不是抖动，是模型在做安全结论，必须被看见。"""

    class _Dangerous:
        def __init__(self) -> None:
            self.calls = 0

        @property
        def capabilities(self):
            return FakeVlmProvider().capabilities

        async def describe(self, *, image_bytes, metadata):
            self.calls += 1
            return {"speech": "有东西。", "safe_to_walk": True}

    p = _Dangerous()
    client = TestClient(create_app(p))
    r = _post(client, "USER")
    assert r.status_code == 502
    assert p.calls == 1, "越界输出不应被重试掩盖"
    assert "safe_to_walk" in r.json()["error"]["message"]


# ---------------------------------------------------------------------------
# 无默认端点（ADR-07）
# ---------------------------------------------------------------------------

def test_no_default_endpoint(monkeypatch) -> None:
    """未配置环境变量时必须退回 Fake，绝不连接某个默认端点。

    注意：gateway.app 在导入时会加载仓库根的 .env（本地开发便利）。
    因此本测试必须清掉**全部**相关变量，否则在开发者机器上会因为
    .env 存在而误判。这是一条"测试环境自洽"的要求，不是宽容处理。
    """
    for key in (
        "KANJIAN_OPENAI_BASE_URL",
        "KANJIAN_OPENAI_API_KEY",
        "KANJIAN_OPENAI_MODEL",
        "KANJIAN_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    app = create_app()
    assert isinstance(app.state.provider, FakeVlmProvider)
    assert app.state.provider.capabilities.name == "fake"


def test_openai_provider_requires_explicit_config() -> None:
    """空配置不得构造 Provider——防止有人写出"默认地址"。"""
    from gateway.providers import OpenAICompatProvider

    with pytest.raises(ValueError):
        OpenAICompatProvider(base_url="", api_key="k", model="m")
    with pytest.raises(ValueError):
        OpenAICompatProvider(base_url="https://x", api_key="", model="m")
