"""polylens-bilibili: 从 B 站视频中提取信息的 MCP server。"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("polylens-bilibili-mcp")
except PackageNotFoundError:  # 未安装（如源码树里直接跑）时的兜底
    __version__ = "0.0.0"
