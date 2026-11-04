"""边界守卫：把"约定"变成"构建失败"。

三条守卫对应三个最容易悄悄退化的地方：

1. **危险字段** — SceneResult 不得出现 safe_to_walk / distance_m / risk_level 等。
   一旦出现，下游就可能把它当事实。
2. **默认端点** — 公开发行版不得内置任何云端地址（ADR-07）。
   只扫**真实字符串常量**，不扫 docstring 与注释——文档里出现
   `http://host:port/v1` 是说明，不是端点。
3. **图像与密钥** — 不得把照片、音频、私钥提交进仓库。

设计上是纯函数，接收 ``root``，便于在临时目录里用违规样例测试。
**一个永远不报警的守卫等于没有守卫**，所以它自己必须有测试。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

FORBIDDEN_RESULT_FIELDS = frozenset(
    {
        "safe_to_walk",
        "recommended_direction",
        "distance_m",
        "risk_level",
        "can_pass",
        "is_safe",
        "walkable",
    }
)

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
    "host:port",
    "://host",
)

BINARY_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".wav", ".mp3", ".pem", ".key", ".p12"}
)

SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", "build", "dist"}
)

_URL_RX = re.compile(r"https?://[^\s\"'\)]+")


def root_of(start: Path) -> Path:
    """从任意文件路径向上找到仓库根（含 pyproject.toml）。"""
    p = start.resolve()
    for parent in [p, *p.parents]:
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            return parent
    return p


def _iter_files(root: Path, suffix: str) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob(f"*{suffix}"):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# 守卫 1：危险字段
# ---------------------------------------------------------------------------

def check_dangerous_fields(root: Path) -> list[str]:
    schema_path = root / "schemas" / "scene-result.schema.json"
    if not schema_path.exists():
        return [f"找不到 {schema_path}"]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    problems: list[str] = []

    hit = set(schema.get("properties", {})) & FORBIDDEN_RESULT_FIELDS
    if hit:
        problems.append(f"SceneResult 出现危险字段: {sorted(hit)}")
    if schema.get("additionalProperties") is not False:
        problems.append("SceneResult 必须 additionalProperties: false（否则字段可被注入）")
    return problems


# ---------------------------------------------------------------------------
# 守卫 2：硬编码端点（AST，跳过 docstring）
# ---------------------------------------------------------------------------

def _docstring_ids(tree: ast.AST) -> set[int]:
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
    doc_ids = _docstring_ids(tree)
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in doc_ids
        ):
            out.append((node.value, getattr(node, "lineno", 0)))
    return out


def check_hardcoded_endpoints(root: Path) -> list[str]:
    """扫描 Python 与 Kotlin 源码中的硬编码端点。

    Kotlin 侧尤其要查：Android 端才是用户真正安装的东西，
    一个内置的默认网关地址比后端的默认端点更难被发现。
    Python 用 AST（能精确跳过 docstring），Kotlin 用逐行正则
    （注释与 KDoc 以 // 或 * 开头，直接跳过）。
    """
    problems: list[str] = []

    for path in _iter_files(root, ".py"):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in {"tests"}:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError as exc:
            problems.append(f"{rel}: 无法解析（{exc.msg}）")
            continue

        for value, lineno in _string_constants(tree):
            for m in _URL_RX.finditer(value):
                url = m.group(0)
                if any(marker in url for marker in ALLOWED_URL_MARKERS):
                    continue
                problems.append(f"{rel}:{lineno} 疑似硬编码端点 {url}")

    for path in _iter_files(root, ".kt"):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in {"tests", "test", "androidTest"}:
            continue
        if "src/test" in str(rel).replace("\\", "/") or "src/androidTest" in str(rel).replace("\\", "/"):
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
        ):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
                continue
            for m in _URL_RX.finditer(line):
                url = m.group(0)
                if any(marker in url for marker in ALLOWED_URL_MARKERS):
                    continue
                problems.append(f"{rel}:{lineno} 疑似硬编码端点 {url}")

    return problems


# ---------------------------------------------------------------------------
# 守卫 3：图像与密钥
# ---------------------------------------------------------------------------

def check_binary_and_secrets(root: Path) -> list[str]:
    problems: list[str] = []
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            # docs 下的示意图允许存在（当前也没有）
            if not rel.parts or rel.parts[0] != "docs":
                problems.append(f"禁止提交图像/音频/密钥: {rel}")

    env_file = root / ".env"
    gitignore = root / ".gitignore"
    if env_file.exists() and gitignore.exists():
        lines = {ln.strip() for ln in gitignore.read_text(encoding="utf-8").splitlines()}
        if ".env" not in lines:
            problems.append(".gitignore 未忽略 .env")
    return problems


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

CHECKS = (
    ("危险字段", check_dangerous_fields),
    ("硬编码端点", check_hardcoded_endpoints),
    ("图像与密钥", check_binary_and_secrets),
)


def run_all(root: Path) -> tuple[int, list[tuple[str, list[str]]]]:
    """返回 (失败项数, [(守卫名, 问题列表)])。"""
    results: list[tuple[str, list[str]]] = []
    failed = 0
    for name, fn in CHECKS:
        problems = fn(root)
        if problems:
            failed += 1
        results.append((name, problems))
    return failed, results