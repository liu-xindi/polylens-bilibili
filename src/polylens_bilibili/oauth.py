"""进程内 OAuth 授权服务器：单用户，自部署，远程 http 用。

本服务同时是资源服务器与授权服务器，规范允许同进程 co-host。
协议端点与各项校验由 mcp.server.auth 提供，本模块只补 provider、同意页、接线三件。
注册机制用 DCR，因为 claude.ai 当前只支持它。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl

_ACCESS_TTL = 3600  # 访问令牌 1 小时；刷新令牌长期（claude.ai 后台静默续）
_CODE_TTL = 600  # 授权码 10 分钟（库也会校验过期）
_EMPTY: dict[str, dict[str, Any]] = {"clients": {}, "access_tokens": {}, "refresh_tokens": {}}


def _store_path() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache / "polylens-bilibili" / "oauth" / "store.json"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class _Pending:
    client_id: str
    client_label: str
    params: AuthorizationParams
    created_at: float = field(default_factory=time.time)


class OAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """令牌只存哈希。"""

    def __init__(
        self, *, issuer_url: str, resource_url: str, store_path: Path | None = None
    ) -> None:
        self._issuer = issuer_url.rstrip("/")
        self._resource = resource_url
        self._path = store_path or _store_path()
        self._codes: dict[str, AuthorizationCode] = {}  # code → 授权码（内存，一次性）
        self._pending: dict[str, _Pending] = {}  # req_id → 待同意请求（内存）

    # ── 磁盘存储 ──────────────────────────────────────────────────────────────
    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {k: {} for k in _EMPTY}
        return {k: dict(data.get(k, {})) for k in _EMPTY}

    def _save(self, store: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(self._path, 0o600)  # 仅本人可读写
        except OSError:
            pass

    # ── 客户端（DCR）─────────────────────────────────────────────────────────
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self._load()["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        store = self._load()
        store["clients"][str(client_info.client_id)] = client_info.model_dump(mode="json")
        self._save(store)

    # ── 授权（→ 跳转同意页）─────────────────────────────────────────────────
    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        req_id = secrets.token_urlsafe(24)
        label = client.client_name or str(client.client_id)
        self._pending[req_id] = _Pending(
            client_id=str(client.client_id), client_label=label, params=params
        )
        return f"{self._issuer}/consent?req={req_id}"

    def pending_label(self, req_id: str) -> str | None:
        pending = self._pending.get(req_id)
        return pending.client_label if pending else None

    def grant_pending(self, req_id: str) -> str | None:
        pending = self._pending.pop(req_id, None)
        if pending is None:
            return None
        p = pending.params
        code = secrets.token_urlsafe(32)  # ≥256 位熵，远超规范 160 位要求
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=p.scopes or [],
            expires_at=time.time() + _CODE_TTL,
            client_id=pending.client_id,
            code_challenge=p.code_challenge,
            redirect_uri=p.redirect_uri,
            redirect_uri_provided_explicitly=p.redirect_uri_provided_explicitly,
            resource=p.resource,
        )
        return construct_redirect_uri(
            str(p.redirect_uri), code=code, state=p.state, iss=self._issuer
        )

    # ── 令牌：换取 / 加载 / 吊销 ──────────────────────────────────────────────
    def _mint(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        at = AccessToken(
            token=access, client_id=client_id, scopes=scopes,
            expires_at=now + _ACCESS_TTL, resource=resource, subject="owner",
        )
        rt = RefreshToken(token=refresh, client_id=client_id, scopes=scopes, subject="owner")
        store = self._load()
        store["access_tokens"][_hash(access)] = _strip_token(at)
        store["refresh_tokens"][_hash(refresh)] = _strip_token(rt)
        self._save(store)
        return OAuthToken(
            access_token=access, token_type="Bearer", expires_in=_ACCESS_TTL,
            scope=" ".join(scopes) if scopes else None, refresh_token=refresh,
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self._codes.get(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)  # 一次性
        return self._mint(
            str(client.client_id), authorization_code.scopes, authorization_code.resource
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = self._load()["refresh_tokens"].get(_hash(refresh_token))
        return RefreshToken(token=refresh_token, **data) if data else None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        store = self._load()  # 轮换：作废旧刷新令牌，发一对新的
        store["refresh_tokens"].pop(_hash(refresh_token.token), None)
        self._save(store)
        return self._mint(str(client.client_id), scopes or refresh_token.scopes, self._resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self._load()["access_tokens"].get(_hash(token))
        if data is None:
            return None
        at = AccessToken(token=token, **data)
        if at.expires_at and at.expires_at < time.time():
            return None  # 已过期
        return at

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        h = _hash(token.token)
        store = self._load()
        store["access_tokens"].pop(h, None)
        store["refresh_tokens"].pop(h, None)
        self._save(store)


def _strip_token(model: AccessToken | RefreshToken) -> dict[str, Any]:
    """存储用：丢掉令牌原文，按哈希索引。"""
    d = model.model_dump(mode="json")
    d.pop("token", None)
    return d


# ── 接到 FastMCP 上 ─────────────────────────────────────────────────────────
def build_oauth(public_url: str) -> tuple[dict[str, Any], OAuthProvider]:
    """返回 (传给 FastMCP 构造器的 auth kwargs, provider)。

    服务跑在反代后，进来的 Host 是公网域名，需列入 allowed_hosts，否则库的 DNS 重绑定防护会拦下。
    """
    base = public_url.rstrip("/")
    provider = OAuthProvider(issuer_url=base, resource_url=f"{base}/mcp")
    host = urlparse(base).hostname or "localhost"
    kwargs: dict[str, Any] = {
        "auth": AuthSettings(
            issuer_url=AnyHttpUrl(base),
            resource_server_url=AnyHttpUrl(f"{base}/mcp"),
            client_registration_options=ClientRegistrationOptions(enabled=True),  # DCR 开
            revocation_options=RevocationOptions(enabled=True),
        ),
        "auth_server_provider": provider,
        "transport_security": TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host, f"{host}:*", "127.0.0.1:*", "localhost:*"],
            allowed_origins=[f"https://{host}", f"https://{host}:*", f"http://{host}:*"],
        ),
    }
    return kwargs, provider


def register_consent_route(mcp: Any, provider: OAuthProvider, auth_secret: str) -> None:
    """在 FastMCP 上挂 /consent 页：机主输入口令，正确则发授权码回跳客户端。"""
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, RedirectResponse, Response

    async def consent(request: Request) -> Response:
        if request.method == "GET":
            req_id = request.query_params.get("req", "")
            label = provider.pending_label(req_id)
            if label is None:
                return HTMLResponse(
                    _render(notice="授权请求无效或已过期，回到客户端重新发起。"), status_code=400
                )
            return HTMLResponse(_render(req_id=req_id, client_label=label))
        form = await request.form()
        req_id, secret = str(form.get("req", "")), str(form.get("secret", ""))
        label = provider.pending_label(req_id)
        if label is None:
            return HTMLResponse(
                _render(notice="授权请求无效或已过期，回到客户端重新发起。"), status_code=400
            )
        if not secrets.compare_digest(secret, auth_secret):
            return HTMLResponse(
                _render(req_id=req_id, client_label=label, error="口令不正确。"),
                status_code=401,
            )
        redirect = provider.grant_pending(req_id)
        if redirect is None:
            return HTMLResponse(_render(notice="授权请求无效或已过期。"), status_code=400)
        return RedirectResponse(url=redirect, status_code=302)

    mcp.custom_route("/consent", methods=["GET", "POST"])(consent)


# 配色取自 shadcn 的 claude 主题（oklch 语义变量），只搬同意页用得上的那十来个。
# 主题在浅色下的 destructive 是近黑的 oklch(0.19 0.00 106.59)，用作错误提示与正文难以区分，
# 故两个模式统一用它的深色值（红）。
# 主题的 ring 是蓝色 oklch(0.59 0.17 253.06)，落在这一页会是唯一的冷色，故未采用：
# 聚焦环用 primary 着色。
# background 与 card 在主题里同色，卡片靠 border 与 shadow 分层，不靠底色差。
_PAGE_CSS = """
:root {
  color-scheme: light dark;
  --background: oklch(0.98 0.01 95.10);
  --foreground: oklch(0.34 0.03 95.72);
  --card: oklch(0.98 0.01 95.10);
  --card-foreground: oklch(0.19 0.00 106.59);
  --primary: oklch(0.62 0.14 39.04);
  --primary-foreground: oklch(1.00 0 0);
  --muted-foreground: oklch(0.61 0.01 97.42);
  --border: oklch(0.88 0.01 97.36);
  --input: oklch(0.76 0.02 98.35);
  --destructive: oklch(0.64 0.21 25.33);
  --radius: 0.5rem;
  --shadow-lg: 0px 4px 8px -1px hsl(0 0% 0% / 0.10), 0px 4px 6px -2px hsl(0 0% 0% / 0.10);
}
@media (prefers-color-scheme: dark) {
  :root {
    --background: oklch(0.27 0.00 106.64);
    --foreground: oklch(0.81 0.01 93.01);
    --card: oklch(0.27 0.00 106.64);
    --card-foreground: oklch(0.98 0.01 95.10);
    --primary: oklch(0.67 0.13 38.76);
    --muted-foreground: oklch(0.77 0.02 99.07);
    --border: oklch(0.36 0.01 106.89);
    --input: oklch(0.43 0.01 100.22);
    --shadow-lg: 0px 4px 8px -1px hsl(0 0% 0% / 0.36), 0px 4px 6px -2px hsl(0 0% 0% / 0.36);
  }
}
* { box-sizing: border-box; }
body {
  margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC",sans-serif;
  background:var(--background); color:var(--foreground);
}
.card {
  width:100%; max-width:400px; margin:24px; padding:32px 28px;
  background:var(--card); color:var(--card-foreground);
  border:1px solid var(--border); border-radius:calc(var(--radius) + 4px);
  box-shadow:var(--shadow-lg);
}
.brand {
  font-size:12px; letter-spacing:.12em; text-transform:uppercase;
  color:var(--muted-foreground); margin-bottom:20px;
}
h1 { font-size:20px; margin:0 0 6px; font-weight:600; letter-spacing:-0.01em; }
.sub { font-size:14px; color:var(--muted-foreground); margin:0 0 24px; line-height:1.6; }
.client { font-weight:600; color:var(--primary); }
label { display:block; font-size:13px; font-weight:500; margin-bottom:8px; }
input[type=password]{
  width:100%; padding:10px 13px; font-size:14px; font-family:inherit;
  border:1px solid var(--input); border-radius:var(--radius);
  background:transparent; color:inherit; outline:none;
  transition:border-color .15s, box-shadow .15s;
}
input[type=password]::placeholder{ color:var(--muted-foreground); }
input[type=password]:focus{
  border-color:var(--primary);
  box-shadow:0 0 0 3px color-mix(in oklch, var(--primary) 25%, transparent);
}
button {
  width:100%; margin-top:20px; padding:10px; font-size:14px; font-weight:500;
  font-family:inherit; color:var(--primary-foreground); background:var(--primary);
  border:none; border-radius:var(--radius); cursor:pointer;
  transition:background-color .15s;
}
button:hover{ background:color-mix(in oklch, var(--primary) 90%, transparent); }
button:focus-visible{
  outline:none; box-shadow:0 0 0 3px color-mix(in oklch, var(--primary) 35%, transparent);
}
.err { margin-top:14px; font-size:13px; color:var(--destructive); }
.notice { font-size:15px; color:var(--muted-foreground); line-height:1.6; text-align:center; }
.foot {
  margin-top:24px; font-size:12px; color:var(--muted-foreground);
  text-align:center; opacity:.8;
}
"""


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _render(
    *, req_id: str = "", client_label: str = "", error: str = "", notice: str = ""
) -> str:
    """渲染同意页。notice 非空表示只显示一条提示，无表单。"""
    if notice:
        body = f'<p class="notice">{_esc(notice)}</p>'
    else:
        err = f'<p class="err">{_esc(error)}</p>' if error else ""
        body = (
            f'<h1>授权访问</h1>'
            f'<p class="sub"><span class="client">{_esc(client_label)}</span> '
            f"请求连接到你的 B 站内容服务。输入机主口令以授权。</p>"
            f'<form method="post" action="/consent">'
            f'<input type="hidden" name="req" value="{_esc(req_id)}">'
            f'<label for="secret">机主口令</label>'
            f'<input type="password" id="secret" name="secret" autofocus autocomplete="off" '
            f'placeholder="••••••••">'
            f'<button type="submit">授权</button>'
            f"{err}</form>"
        )
    return (
        "<!doctype html><html lang=zh><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>polylens-bilibili 授权</title><style>{_PAGE_CSS}</style></head>"
        f'<body><div class="card"><div class="brand">polylens · bilibili</div>{body}'
        '<p class="foot">仅机主本人应进行此操作</p></div></body></html>'
    )
