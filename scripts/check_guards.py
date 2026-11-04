"""CI 守卫入口：薄封装。

真正的逻辑在 `kanjian/guards.py`（可被测试导入）。
这里只负责定位仓库根并输出结果。

用法：
    python scripts/check_guards.py
退出码 0 = 全部通过。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kanjian.guards import run_all  # noqa: E402


def main() -> int:
    failed, results = run_all(ROOT)
    for name, problems in results:
        if problems:
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