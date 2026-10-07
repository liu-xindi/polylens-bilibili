"""polylens-bilibili-mcp: 读取 B 站视频、评论、弹幕、字幕等公开信息的 MCP 服务。

支持用 jq 在返回前筛选和裁剪结果，可本地运行，也可通过 HTTP + OAuth 远程接入。
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("polylens-bilibili-mcp")
except PackageNotFoundError:  # 未安装（如源码树里直接跑）时的兜底
    __version__ = "0.0.0"
