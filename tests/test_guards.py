"""守卫脚本自身的测试。

理由很简单：**一个永远不报警的守卫等于没有守卫。**
这里用临时目录构造违规样例，验证三条守卫确实会失败。
"""

from __future__ import annotations

import json

import pytest

import scripts.check_guards as guards


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    """把守卫的目标根目录指向临时目录。"""
    (tmp_path / "schemas").mkdir()
    (tmp_path / "gateway").mkdir()
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr(guards, "ROOT", tmp_path)
    return tmp_path


def _write_schema(root, props: dict, additional: object = False) -> None:
    (root / "schemas" / "scene-result.schema.json").write_text(
        json.dumps({"properties": props, "additionalProperties": additional}),
        encoding="utf-8",
    )


# --- 守卫 1：危险字段 -------------------------------------------------------

def test_dangerous_field_detected(fake_root) -> None:
    _write_schema(fake_root, {"scene_summary": {}, "safe_to_walk": {}})
    problems = guards.check_dangerous_fields()
    assert problems
    assert "safe_to_walk" in problems[0]


def test_distance_field_detected(fake_root) -> None:
    _write_schema(fake_root, {"distance_m": {}})
    assert guards.check_dangerous_fields()


def test_open_additional_properties_detected(fake_root) -> None:
    _write_schema(fake_root, {"scene_summary": {}}, additional=True)
    assert any("additionalProperties" in p for p in guards.check_dangerous_fields())


def test_clean_schema_passes(fake_root) -> None:
    _write_schema(fake_root, {"scene_summary": {}})
    assert guards.check_dangerous_fields() == []


# --- 守卫 2：硬编码端点 -----------------------------------------------------

def test_hardcoded_endpoint_detected(fake_root) -> None:
    f = fake_root / "gateway" / "bad.py"
    f.write_text('URL = "https://api.somemodel-vendor.io/v1/chat"\n', encoding="utf-8")
    problems = guards.check_hardcoded_endpoints()
    assert problems
    assert "api.somemodel-vendor.io" in problems[0]


def test_docstring_endpoint_allowed(fake_root) -> None:
    """文档里写示例地址是说明，不是端点——守卫必须能区分。"""
    f = fake_root / "gateway" / "ok.py"
    f.write_text(
        '"""示例：用户可填 http://host:port/v1 或 https://api.example.com/v1。"""\n'
        "X = 1\n",
        encoding="utf-8",
    )
    assert guards.check_hardcoded_endpoints() == []


def test_localhost_allowed(fake_root) -> None:
    f = fake_root / "gateway" / "local.py"
    f.write_text('DEFAULT = "http://127.0.0.1:3425/v1"\n', encoding="utf-8")
    assert guards.check_hardcoded_endpoints() == []


def test_comment_endpoint_ignored(fake_root) -> None:
    f = fake_root / "gateway" / "c.py"
    f.write_text("# https://api.real-vendor.example.org/v1\nX = 1\n", encoding="utf-8")
    assert guards.check_hardcoded_endpoints() == []


# --- 守卫 3：图像与密钥 -----------------------------------------------------

def test_image_commit_detected(fake_root) -> None:
    (fake_root / "photo.jpg").write_bytes(b"\xff\xd8\xff")
    assert any("photo.jpg" in p for p in guards.check_binary_and_secrets())


def test_key_commit_detected(fake_root) -> None:
    (fake_root / "server.pem").write_bytes(b"-----BEGIN-----")
    assert guards.check_binary_and_secrets()


def test_docs_image_allowed(fake_root) -> None:
    (fake_root / "docs").mkdir()
    (fake_root / "docs" / "diagram.png").write_bytes(b"\x89PNG")
    assert guards.check_binary_and_secrets() == []


def test_env_must_be_gitignored(fake_root) -> None:
    (fake_root / ".env").write_text("OPENAI_API_KEY=x\n", encoding="utf-8")
    (fake_root / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    assert any(".env" in p for p in guards.check_binary_and_secrets())