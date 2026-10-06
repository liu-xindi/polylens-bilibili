"""HTTP 层：纯标准库。凭据通过构造参数注入。"""

from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import zlib
from typing import Any
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from ..errors import BilibiliError
from ._constants import API_BASE, USER_AGENT, WEB_HOME

_DEFAULT_REFERER = WEB_HOME


class BilibiliHttpError(BilibiliError):
    """平台返回非 0 业务码。消息为平台原文，不作解释；code 留给调用方按能力改写提示。"""

    def __init__(self, code: Any, message: Any) -> None:
        super().__init__(f"接口返回失败: {code} {message}")
        self.code = code


class _RateLimited(Exception):
    """内部信号：触发风控。由各能力捕获后转成 RateLimitedError。"""


class HttpClient:
    """带 cookie jar 的极简客户端。"""

    def __init__(self, cookie: str = "", timeout: int = 20) -> None:
        self.api_base = API_BASE.rstrip("/")
        self.cookie = cookie.strip()
        self.timeout = timeout
        self._jar = http.cookiejar.CookieJar()
        self._opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self._jar))

    def _headers(self, referer: str = _DEFAULT_REFERER) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": referer,
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def get_bytes(self, url: str, referer: str = _DEFAULT_REFERER) -> bytes:
        request = Request(url, headers=self._headers(referer))
        with self._opener.open(request, timeout=self.timeout) as response:
            return response.read()

    def get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        referer: str = _DEFAULT_REFERER,
        base_url: str | None = None,
        allow_codes: set[int] | None = None,
    ) -> Any:
        """请求 JSON 接口并返回 data 字段；非 0 code 抛错（白名单除外）。"""
        base = base_url if base_url is not None else self.api_base
        url = f"{base}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        try:
            raw = self.get_bytes(url, referer)
        except urllib.error.HTTPError as exc:
            if exc.code == 412:
                raise _RateLimited() from exc
            raise
        payload = json.loads(raw.decode("utf-8"))
        code = payload.get("code")
        if code in (-352, -509):
            raise _RateLimited()
        if code != 0 and (allow_codes is None or code not in allow_codes):
            raise BilibiliHttpError(code, payload.get("message"))
        data = payload.get("data")
        # 风控还有一种 code=0 的形态：data 里只剩 v_voucher 这张挑战票据，没有任何业务数据。
        # 不在这里判出来，调用方会把空手而归读成"没有结果"。
        if isinstance(data, dict) and set(data) == {"v_voucher"}:
            raise _RateLimited()
        return data

    def get_json_url(self, url: str) -> Any:
        """直接 GET 任意 URL 并返回解析后的 JSON（用于字幕文件等外部 URL）。"""
        raw = self.get_bytes(url)
        return json.loads(raw.decode("utf-8"))

    def get_bytes_and_set_cookies(self, url: str) -> tuple[bytes, list[str]]:
        """GET url，返回 (body, Set-Cookie header 值列表)。用于扫码登录。"""
        request = Request(url, headers=self._headers())
        with self._opener.open(request, timeout=self.timeout) as resp:
            body = resp.read()
            set_cookies: list[str] = resp.info().get_all("Set-Cookie") or []
        return body, set_cookies


def inflate_deflate(raw: bytes) -> str:
    """解压 list.so 弹幕接口的 raw-deflate 字节为文本。"""
    try:
        return zlib.decompress(raw, -zlib.MAX_WBITS).decode("utf-8", errors="replace")
    except zlib.error:
        try:
            return zlib.decompress(raw).decode("utf-8", errors="replace")
        except zlib.error:
            return raw.decode("utf-8", errors="replace")
