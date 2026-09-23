"""OAuth 授权服务器单元测试（不起网络）：provider 各方法 + create_server 接线。"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import anyio
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from polylens_bilibili import oauth
from polylens_bilibili.server import create_server

_REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _run(coro):
    return anyio.run(lambda: coro)


def _provider(tmp_path) -> oauth.OAuthProvider:
    return oauth.OAuthProvider(
        issuer_url="https://mcp.example.com",
        resource_url="https://mcp.example.com/mcp",
        store_path=tmp_path / "store.json",
    )


def _client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="client-1",
        client_name="Claude",
        redirect_uris=[AnyUrl(_REDIRECT)],
    )


def _params() -> AuthorizationParams:
    return AuthorizationParams(
        state="state-xyz",
        scopes=[],
        code_challenge="challenge-abc",
        redirect_uri=AnyUrl(_REDIRECT),
        redirect_uri_provided_explicitly=True,
        resource="https://mcp.example.com/mcp",
    )


async def _full_grant(p: oauth.OAuthProvider) -> tuple[str, OAuthClientInformationFull]:
    """注册客户端 → authorize → grant_pending，返回 (授权码, client)。"""
    client = _client()
    await p.register_client(client)
    url = await p.authorize(client, _params())
    req_id = parse_qs(urlparse(url).query)["req"][0]
    redirect = p.grant_pending(req_id)
    assert redirect is not None
    code = parse_qs(urlparse(redirect).query)["code"][0]
    return code, client


def test_register_and_get_client_roundtrip(tmp_path) -> None:
    p = _provider(tmp_path)
    _run(p.register_client(_client()))
    got = _run(p.get_client("client-1"))
    assert got is not None
    assert got.client_name == "Claude"
    assert _run(p.get_client("nope")) is None


def test_authorize_redirects_to_consent_with_pending(tmp_path) -> None:
    p = _provider(tmp_path)
    url = _run(p.authorize(_client(), _params()))
    assert url.startswith("https://mcp.example.com/consent?req=")
    req_id = parse_qs(urlparse(url).query)["req"][0]
    assert p.pending_label(req_id) == "Claude"  # 同意页据此显示"谁在请求"
    assert p.pending_label("bogus") is None


def test_grant_pending_issues_code_with_state_and_iss(tmp_path) -> None:
    p = _provider(tmp_path)
    url = _run(p.authorize(_client(), _params()))
    req_id = parse_qs(urlparse(url).query)["req"][0]
    redirect = p.grant_pending(req_id)
    assert redirect is not None
    q = parse_qs(urlparse(redirect).query)
    assert urlparse(redirect)._replace(query="").geturl() == _REDIRECT
    assert q["state"] == ["state-xyz"]
    assert q["iss"] == ["https://mcp.example.com"]
    assert q["code"][0]
    # 同一请求不能再发码（一次性）
    assert p.grant_pending(req_id) is None


def test_grant_unknown_request_returns_none(tmp_path) -> None:
    assert _provider(tmp_path).grant_pending("never-existed") is None


def test_code_exchange_yields_working_access_token(tmp_path) -> None:
    p = _provider(tmp_path)

    async def scenario():
        code, client = await _full_grant(p)
        auth_code = await p.load_authorization_code(client, code)
        assert auth_code is not None
        token = await p.exchange_authorization_code(client, auth_code)
        # 换发的访问令牌可被 load_access_token 验证通过
        at = await p.load_access_token(token.access_token)
        assert at is not None and at.client_id == "client-1"
        # 授权码一次性：换过之后再 load 不到
        assert await p.load_authorization_code(client, code) is None
        return token

    token = _run(scenario())
    assert token.refresh_token


def test_tokens_stored_hashed_not_plaintext(tmp_path) -> None:
    p = _provider(tmp_path)

    async def scenario():
        code, client = await _full_grant(p)
        auth_code = await p.load_authorization_code(client, code)
        assert auth_code is not None
        return await p.exchange_authorization_code(client, auth_code)

    token = _run(scenario())
    raw = (tmp_path / "store.json").read_text(encoding="utf-8")
    assert token.access_token not in raw  # 原文不落盘
    assert token.refresh_token not in raw
    store = json.loads(raw)
    assert len(store["access_tokens"]) == 1  # 只存了哈希索引的记录
    assert (tmp_path / "store.json").stat().st_mode & 0o777 == 0o600


def test_refresh_rotates_and_invalidates_old(tmp_path) -> None:
    p = _provider(tmp_path)

    async def scenario():
        code, client = await _full_grant(p)
        auth_code = await p.load_authorization_code(client, code)
        assert auth_code is not None
        token = await p.exchange_authorization_code(client, auth_code)
        old_refresh = token.refresh_token
        assert old_refresh is not None
        rt = await p.load_refresh_token(client, old_refresh)
        assert rt is not None
        new = await p.exchange_refresh_token(client, rt, [])
        # 旧刷新令牌已作废、新的可用
        assert await p.load_refresh_token(client, old_refresh) is None
        assert new.refresh_token and new.refresh_token != old_refresh
        assert await p.load_access_token(new.access_token) is not None

    _run(scenario())


def test_expired_access_token_rejected(tmp_path, monkeypatch) -> None:
    p = _provider(tmp_path)

    async def scenario():
        code, client = await _full_grant(p)
        auth_code = await p.load_authorization_code(client, code)
        assert auth_code is not None
        token = await p.exchange_authorization_code(client, auth_code)
        # 把时钟拨到很久以后 → 访问令牌应判定过期
        monkeypatch.setattr(oauth.time, "time", lambda: 9_999_999_999.0)
        assert await p.load_access_token(token.access_token) is None

    _run(scenario())


def test_revoke_token_removes_it(tmp_path) -> None:
    p = _provider(tmp_path)

    async def scenario():
        code, client = await _full_grant(p)
        auth_code = await p.load_authorization_code(client, code)
        assert auth_code is not None
        token = await p.exchange_authorization_code(client, auth_code)
        at = await p.load_access_token(token.access_token)
        assert at is not None
        await p.revoke_token(at)
        assert await p.load_access_token(token.access_token) is None

    _run(scenario())


# ── create_server 接线 ───────────────────────────────────────────────────────
def test_create_server_enables_oauth_with_url_and_secret() -> None:
    mcp = create_server(
        host="127.0.0.1",
        port=6622,
        public_url="https://mcp.example.com",
        auth_secret="hunter2",
    )
    assert mcp.settings.auth is not None
    assert str(mcp.settings.auth.issuer_url).rstrip("/") == "https://mcp.example.com"


def test_create_server_no_oauth_without_secret() -> None:
    mcp = create_server(public_url="https://mcp.example.com")
    assert mcp.settings.auth is None


def test_create_server_no_oauth_by_default() -> None:
    assert create_server().settings.auth is None
