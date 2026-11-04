"""CI 守卫：把边界从"约定"变成"构建失败"。

三条守卫对应三个最容易悄悄退化的地方：

1. **危险字段** — SceneResult 不得出现 safe_to_walk / distance_m / risk_level 等。
   一旦出现，下游就可能把它当事实。
2. **默认端点** — 公开发行版不得内置任何云端地址（ADR-07）。
   ⚠️ 这里只扫**真实字符串常量**，不扫 docstring 与注释——
   文档里出现 `http://host:port/v1` 是说明，不是端点。
3. **图像与密钥** — 不得把照片、音频、私钥提交进仓库。

用法：
    python scripts/check_guards.py
退出码 0 = 全部通过。
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_RESULT_FIELDS = {
    "safe_to_walk",
    "recommended_direction",
    "distance_m",
    "risk_level",
    "can_pass",
    "is_safe",
    "walkable",
}

#: 允许出现在代码字符串里的域名/占位符。真实端点一律必须来自配置。
ALLOWED_URL_MARKERS = (
    "github.com",
    "apache.org",
    "json-schema.org",
    "localhost",
    "127.0.0.1",
    "example.com",
    "your-endpoint",
    "your-host",
    # 文档中的占位写法
    "host:port",
    "://host",
)

BINARY_SUFFIXES = {".jpg", ".jpeg", ".png", ".wav", ".mp3", ".pem", ".key", ".p12"}
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache"}


def _iter_files(suffix: str) -> list[Path]:
    out: list[Path] = []
    for p in ROOT.rglob(f"*{suffix}"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# 守卫 1
# ---------------------------------------------------------------------------

def check_dangerous_fields() -> list[str]:
    schema_path = ROOT / "schemas" / "scene-result.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    problems: list[str] = []

    hit = set(schema.get("properties", {})) & FORBIDDEN_RESULT_FIELDS
    if hit:
        problems.append(f"SceneResult 出现危险字段: {sorted(hit)}")
    if schema.get("additionalProperties") is not False:
        problems.append("SceneResult 必须 additionalProperties: false（否则字段可被注入）")
    return problems


# ---------------------------------------------------------------------------
# 守卫 2：用 AST 只看真实字符串，跳过 docstring
# ---------------------------------------------------------------------------

def _docstring_nodes(tree: ast.AST) -> set[int]:
    """收集所有 docstring 的节点 id，避免把说明文字当成端点。"""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _string_constants(tree: ast.AST) -> list[tuple[str, int]]:
    doc_ids = _docstring_nodes(tree)
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in doc_ids
        ):
            out.append((node.value, getattr(node, "lineno", 0)))
    return out


def check_hardcoded_endpoints() -> list[str]:
    import re

    problems: list[str] = []
    url_rx = re.compile(r"https?://[^\s\"'\)]+")

    for path in _iter_files(".py"):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in {"tests"}:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError as exc:
            problems.append(f"{rel}: 无法解析（{exc.msg}）")
            continue

        for value, lineno in _string_constants(tree):
            for m in url_rx.finditer(value):
                url = m.group(0)
                if any(marker in url for marker in ALLOWED_URL_MARKERS):
                    continue
                problems.append(f"{rel}:{lineno} 疑似硬编码端点 {url}")
    return problems


# ---------------------------------------------------------------------------
# 守卫 3
# ---------------------------------------------------------------------------

def check_binary_and_secrets() -> list[str]:
    problems: list[str] = []
    for path in ROOT.rglob("*"):
        if path.is_dir() or any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(ROOT)
        if path.suffix.lower() in BINARY_SUFFIXES:
            # docs 下的示意图允许存在（当前也没有）
            if rel.parts[0] != "docs":
                problems.append(f"禁止提交图像/音频/密钥: {rel}")

    # .env 必须被忽略
    env_file = ROOT / ".env"
    if env_file.exists():
        gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
        if "\n.env\n" not in gi and not gi.startswith(".env\n"):
            problems.append(".gitignore 未忽略 .env")
    return problems


def main() -> int:
    checks = [
        ("危险字段", check_dangerous_fields),
        ("硬编码端点", check_hardcoded_endpoints),
        ("图像与密钥", check_binary_and_secrets),
    ]
    failed = 0
    for name, fn in checks:
        problems = fn()
        if problems:
            failed += 1
            print(f"✗ {name}")
            for p in problems:
                print(f"    {p}")
        else:
            print(f"✓ {name}")

    if failed:
        print(f"\n{failed} 项守卫未通过")
        return 1
    print("\n全部守卫通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())