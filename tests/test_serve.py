"""serve 配置解析 + 传输选择的单元测试（不起真网络）。"""

from __future__ import annotations

import pytest

from polylens_bilibili import serve
from polylens_bilibili.server import create_server


def test_defaults_to_stdio_localhost_6622() -> None:
    cfg = serve.resolve_config([], env={})
    assert cfg.transport == "stdio"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 6622


def test_env_overrides_defaults() -> None:
    cfg = serve.resolve_config(
        [],
        env={
            "POLYLENS_BILIBILI_TRANSPORT": "http",
            "POLYLENS_BILIBILI_HTTP_HOST": "0.0.0.0",
            "POLYLENS_BILIBILI_HTTP_PORT": "9000",
        },
    )
    assert cfg.transport == "http"
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9000


def test_cli_overrides_env() -> None:
    cfg = serve.resolve_config(
        ["--transport", "stdio", "--port", "7000"],
        env={"POLYLENS_BILIBILI_TRANSPORT": "http", "POLYLENS_BILIBILI_HTTP_PORT": "9000"},
    )
    assert cfg.transport == "stdio"
    assert cfg.port == 7000
    # host 既无命令行也无 env → 回落默认
    assert cfg.host == "127.0.0.1"


def test_invalid_transport_from_env_rejected() -> None:
    with pytest.raises(SystemExit):
        serve.resolve_config([], env={"POLYLENS_BILIBILI_TRANSPORT": "ftp"})


def test_oauth_needs_http_url_and_secret() -> None:
    both = serve.resolve_config(
        [],
        env={
            "POLYLENS_BILIBILI_TRANSPORT": "http",
            "POLYLENS_BILIBILI_PUBLIC_URL": "https://example.com",
            "POLYLENS_BILIBILI_AUTH_SECRET": "s3cret",
        },
    )
    assert both.oauth_enabled is True

    no_secret = serve.resolve_config(
        [],
        env={
            "POLYLENS_BILIBILI_TRANSPORT": "http",
            "POLYLENS_BILIBILI_PUBLIC_URL": "https://example.com",
        },
    )
    assert no_secret.oauth_enabled is False

    stdio = serve.resolve_config(
        [],
        env={
            "POLYLENS_BILIBILI_PUBLIC_URL": "https://example.com",
            "POLYLENS_BILIBILI_AUTH_SECRET": "s3cret",
        },
    )
    assert stdio.oauth_enabled is False


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--transport", "http", "--host", "0.0.0.0", "--port", "8080"], "streamable-http"),
        ([], "stdio"),
    ],
)
def test_run_maps_transport(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: str
) -> None:
    """对外的 http 映射到 FastMCP 的 streamable-http；host/port 透传给 create_server。"""
    captured: dict[str, object] = {}

    class _FakeServer:
        def run(self, transport: str = "stdio") -> None:
            captured["transport"] = transport

    def _fake_create(**kw: object) -> _FakeServer:
        captured.update(kw)
        return _FakeServer()

    monkeypatch.setattr(serve, "create_server", _fake_create)
    serve.run(argv)
    assert captured["transport"] == expected
    if argv:
        assert captured["host"] == "0.0.0.0"
        assert captured["port"] == 8080


def test_create_server_applies_host_port() -> None:
    mcp = create_server(host="0.0.0.0", port=9999)
    assert mcp.settings.host == "0.0.0.0"
    assert mcp.settings.port == 9999
