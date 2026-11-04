"""端点归一化与模型发现测试。

这类 bug 的特点是"静默失败"：URL 拼错不会报语法错，只会 404，
而开发者很容易误以为是端点不支持。所以必须有回归测试。
"""

from __future__ import annotations

import pytest

from gateway.providers import OpenAICompatProvider, normalize_base_url


@pytest.mark.parametrize(
    "given,expected",
    [
        # 用户直接粘贴 OpenAI 文档里的地址（已含 /v1）——最常见的输入
        ("http://127.0.0.1:3425/v1", "http://127.0.0.1:3425/v1"),
        ("http://127.0.0.1:3425/v1/", "http://127.0.0.1:3425/v1"),
        # 裸主机名
        ("http://127.0.0.1:3425", "http://127.0.0.1:3425/v1"),
        ("http://127.0.0.1:3425/", "http://127.0.0.1:3425/v1"),
        # 带子路径的部署
        ("https://api.example.com/openai/v1", "https://api.example.com/openai/v1"),
        ("https://api.example.com/openai", "https://api.example.com/openai/v1"),
        # 尾部空白
        ("  https://api.example.com/v1  ", "https://api.example.com/v1"),
    ],
)
def test_normalize_base_url(given: str, expected: str) -> None:
    assert normalize_base_url(given) == expected


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:3425/v1",
        "http://127.0.0.1:3425",
        "http://127.0.0.1:3425/v1/",
    ],
)
def test_chat_url_never_doubles_v1(base_url: str) -> None:
    """回归：base_url 已含 /v1 时，绝不能拼出 /v1/v1/chat/completions。"""
    p = OpenAICompatProvider(base_url=base_url, api_key="k", model="m")
    assert p.chat_url == "http://127.0.0.1:3425/v1/chat/completions"
    assert "/v1/v1/" not in p.chat_url


def test_chat_url_preserves_subpath() -> None:
    p = OpenAICompatProvider(
        base_url="https://gw.example.com/openai/v1", api_key="k", model="m"
    )
    assert p.chat_url == "https://gw.example.com/openai/v1/chat/completions"


def test_model_required() -> None:
    """空模型名不得构造 Provider（否则请求会打到端点后 400，难排查）。"""
    with pytest.raises(ValueError):
        OpenAICompatProvider(base_url="http://x/v1", api_key="k", model="")