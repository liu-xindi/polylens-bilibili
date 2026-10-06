"""HTTP 层：纯标准库。凭据通过构造参数注入。"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import time
import urllib.error
import zlib
from collections import deque
from typing import Any
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from ..errors import BilibiliError
from ._constants import API_BASE, ENDPOINTS, USER_AGENT, WEB_HOME

_DEFAULT_REFERER = WEB_HOME
_log = logging.getLogger("polylens_bilibili")

# 主评论与二级评论被风控时同进同退（实测主评论被拦时换排序、换视频都不通；二级评论未单独验证，
# 按同一套算）。其余接口互不牵连：评论被拦期间搜索、字幕、视频信息照常可用。
_COMMENT_PATHS = frozenset({ENDPOINTS["replies_main"], ENDPOINTS["replies_sub"]})
_RECENT_KEPT = 30


def _group(path: str) -> str:
    return "comments" if path in _COMMENT_PATHS else path


# 各组最近若干次请求的发出时刻，进程内共享。只为风控日志：被拦时记下此前的请求密度，
# 日后据此判断阈值。
_recent: dict[str, deque[float]] = {}


class BilibiliHttpError(BilibiliError):
    """平台返回非 0 业务码。消息为平台原文，不作解释；code 留给调用方按能力改写提示。"""

    def __init__(self, code: Any, message: Any) -> None:
        super().__init__(f"接口返回失败: {code} {message}")
        self.code = code


class _RateLimited(Exception):
    """内部信号：触发风控。由各能力捕获后转成 RateLimitedError。

    signal 是平台给的原始信号：HTTP 状态 412 / 429，业务码 -352 / -509，或 v_voucher
    （code=0 而 data 里只有挑战票据）。各信号的含义与持续多久没有一一验证过，原样带进
    文案与日志，不替平台解释。已知的只有 429：几秒到几十秒就恢复。
    """

    def __init__(self, signal: str, path: str) -> None:
        self.signal = signal
        self.path = path
        super().__init__(self.describe("请求"))  # 没被各能力接住时，工具层原样报出这句

    def describe(self, what: str) -> str:
        if self.signal == "429":
            return f"{what}被平台限流（429），几秒后可重试。"
        return f"{what}触发风控（{self.signal}），稍后重试。"


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
        recent = _recent.setdefault(_group(path), deque(maxlen=_RECENT_KEPT))
        recent.append(time.monotonic())
        try:
            raw = self.get_bytes(url, referer)
        except urllib.error.HTTPError as exc:
            if exc.code in (412, 429):
                raise self._rate_limited(str(exc.code), path) from exc
            raise
        payload = json.loads(raw.decode("utf-8"))
        code = payload.get("code")
        if code in (-352, -509):
            raise self._rate_limited(str(code), path)
        if code != 0 and (allow_codes is None or code not in allow_codes):
            raise BilibiliHttpError(code, payload.get("message"))
        data = payload.get("data")
        # 风控还有一种 code=0 的形态：data 里只剩 v_voucher 这张挑战票据，没有任何业务数据。
        # 不在这里判出来，调用方会把空手而归读成"没有结果"。
        if isinstance(data, dict) and set(data) == {"v_voucher"}:
            raise self._rate_limited("v_voucher", path)
        return data

    def _rate_limited(self, signal: str, path: str) -> _RateLimited:
        now = time.monotonic()
        ages = [round(now - t, 1) for t in reversed(_recent.get(_group(path), ()))]
        _log.warning(
            "风控信号 %s：%s，带 cookie=%s，同组最近请求距今（秒）%s",
            signal, path, bool(self.cookie), ages,
        )
        return _RateLimited(signal, path)

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
