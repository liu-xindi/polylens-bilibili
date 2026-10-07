"""传输与配置解析。

stdio 不走 OAuth，凭据取自本地文件，是规范的要求。
http 用 FastMCP 自带的 streamable-http，uvicorn 随 mcp[cli] 一起来，不必另加依赖。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass

from .server import create_server

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 6622

_VALID_TRANSPORTS = ("stdio", "http")

_ENV_PREFIX = "POLYLENS_BILIBILI_"


@dataclass(frozen=True, slots=True)
class ServeConfig:
    transport: str  # "stdio" | "http"
    host: str
    port: int
    public_url: str | None = None  # OAuth 元数据用的对外公网地址
    auth_secret: str | None = None  # OAuth 同意页把关的机主口令
    insecure_no_auth: bool = False  # 显式允许 http 无鉴权运行，仅供本机调试

    @property
    def oauth_enabled(self) -> bool:
        """仅当 网络模式 + 公网地址 + 机主口令 都齐才开 OAuth。"""
        return self.transport == "http" and bool(self.public_url) and bool(self.auth_secret)


def resolve_config(
    argv: list[str] | None = None, env: Mapping[str, str] | None = None
) -> ServeConfig:
    """按 命令行 > 环境变量 > 默认 解析出运行配置。"""
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(prog="polylens-bilibili-mcp")
    parser.add_argument("--transport", choices=["stdio", "http"])
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--public-url")  # 口令只走环境变量，不上命令行（避免出现在进程列表里）
    args = parser.parse_args(argv)

    transport = args.transport or env.get(f"{_ENV_PREFIX}TRANSPORT") or "stdio"
    if transport not in _VALID_TRANSPORTS:
        raise SystemExit(f"未知的传输方式: {transport}（可选 stdio / http）")

    host = args.host or env.get(f"{_ENV_PREFIX}HTTP_HOST") or _DEFAULT_HOST

    port_raw = args.port if args.port is not None else env.get(f"{_ENV_PREFIX}HTTP_PORT")
    port = int(port_raw) if port_raw is not None else _DEFAULT_PORT

    public_url = args.public_url or env.get(f"{_ENV_PREFIX}PUBLIC_URL") or None
    auth_secret = (env.get(f"{_ENV_PREFIX}AUTH_SECRET") or "").strip() or None
    insecure = env.get(f"{_ENV_PREFIX}INSECURE_NO_AUTH", "").strip().lower() in ("1", "true", "yes")

    return ServeConfig(
        transport=transport, host=host, port=port,
        public_url=public_url, auth_secret=auth_secret, insecure_no_auth=insecure,
    )


def run(argv: list[str] | None = None) -> None:
    config = resolve_config(argv)
    # 监听本机回环也不等于只有本机可达：反向代理、隧道都会把它转到公网。
    # 所以配置不全时拒绝启动，而不是退到无鉴权；要无鉴权必须显式开关。
    if config.transport == "http" and not config.oauth_enabled and not config.insecure_no_auth:
        raise SystemExit(
            f"网络模式需要同时设置 {_ENV_PREFIX}PUBLIC_URL 与 {_ENV_PREFIX}AUTH_SECRET "
            "以启用 OAuth；"
            f"本机调试确需无鉴权时设 {_ENV_PREFIX}INSECURE_NO_AUTH=1。"
        )
    server = create_server(
        host=config.host,
        port=config.port,
        public_url=config.public_url if config.oauth_enabled else None,
        auth_secret=config.auth_secret if config.oauth_enabled else None,
    )
    if config.transport == "http":
        if not config.oauth_enabled:
            print(
                f"{_ENV_PREFIX}INSECURE_NO_AUTH 已开启：当前无鉴权，"
                "仅适合本机调试，不要暴露于公网。",
                file=sys.stderr,
            )
        server.run(transport="streamable-http")
    else:
        server.run(transport="stdio")
