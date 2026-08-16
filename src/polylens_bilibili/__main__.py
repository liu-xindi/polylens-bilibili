"""启动：`polylens-bilibili` 命令 / `python -m polylens_bilibili`。

传输方式（stdio / http）与监听地址由 serve.run 按 命令行 > 环境变量 > 默认 解析。
"""

from __future__ import annotations

from .serve import run


def main() -> None:
    run()


if __name__ == "__main__":
    main()
