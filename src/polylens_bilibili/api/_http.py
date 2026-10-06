"""HTTP 层：纯标准库。凭据通过构造参数注入。"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import math
import threading
import time
import urllib.error
import zlib
from collections import deque
from collections.abc import Callable
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
    （code=0 而 data 里只有挑战票据），原样带进文案与日志。只有 412 实测过会持续封禁
    （评论接口按账号封约 15 分钟）；429 几秒就恢复，v_voucher 随机出现，-352 / -509 未观察到，
    都按可直接重试报。
    """

    def __init__(self, signal: str, path: str, retry_in: float | None = None) -> None:
        self.signal = signal
        self.path = path
        self.retry_in = retry_in  # 评论组熔断时距恢复的秒数，其余为 None
        super().__init__(signal)

    def __str__(self) -> str:
        return self.describe("请求")  # 没被各能力接住时，工具层原样报出这句

    def describe(self, what: str) -> str:
        if self.retry_in is not None:
            return (
                f"评论接口触发风控（{self.signal}），约 {_minutes(self.retry_in)} 分钟后再试，"
                "期间重试或重新登录都无效。"
            )
        if self.signal == _BLOCKING_SIGNAL:
            return f"{what}触发风控（{self.signal}），稍后重试。"
        return f"{what}触发风控（{self.signal}），可直接重试。"

    def describe_partial(self, what: str) -> str:
        """中途被拦、已有部分结果时的说明。"""
        if self.retry_in is not None:
            return (
                f"评论接口触发风控（{self.signal}），只取到部分，"
                f"约 {_minutes(self.retry_in)} 分钟后用 next_cursor 续取。"
            )
        if self.signal == _BLOCKING_SIGNAL:
            return f"{what}触发风控（{self.signal}），只取到部分，稍后用 next_cursor 续取。"
        return f"{what}触发风控（{self.signal}），只取到部分，可直接用 next_cursor 续取。"


def _minutes(seconds: float) -> int:
    return max(1, math.ceil(seconds / 60))


_BLOCKING_SIGNAL = "412"
_COMMENT_INTERVAL = 1.0
_PAGES_PER_MINUTE = 20
_COOLDOWN = 900.0
_RECHECK = 120.0


class _CommentGuard:
    """评论组的节流与熔断，进程内共享：http 部署下所有会话共用一份。

    节流：相邻两次评论请求的发出时刻至少隔 _COMMENT_INTERVAL 秒，且任意 60 秒内不超过
    _PAGES_PER_MINUTE 次，跨调用、跨会话都算。两次实测都是每秒一页、约 50 页时被拦；
    每分钟 20 页约为真人快速扫读的速度，统计窗口未知，这个值没有验证过。

    熔断：评论组收到 412 即停用 _COOLDOWN 秒，期间不发请求。风控按账号算：换排序、换视频、
    重新登录、换出口 IP 都不通，实测约 15 分钟后恢复（10-07 为 14.6–15.2 分钟）。到期放行
    一个请求试探，仍被拦则每隔 _RECHECK 秒再试一次。其他信号不熔断。
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_start: float | None = None
        self._starts: deque[float] = deque(maxlen=_PAGES_PER_MINUTE)
        self._blocked_until = 0.0  # 非 0 表示熔断过、尚未确认恢复
        self._signal = ""
        self._probing = False

    def _refuse(self, now: float) -> None:
        if now < self._blocked_until or self._probing:
            raise _RateLimited(self._signal, "", retry_in=max(self._blocked_until - now, 0.0))

    def check(self) -> None:
        """熔断中则抛错。工具入口先查一遍，省掉取视频信息等前置请求。"""
        with self._lock:
            self._refuse(self._clock())

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            self._refuse(now)
            if self._blocked_until:
                self._probing = True
                _log.warning("评论接口熔断到期，放行一个请求试探")
            wait = 0.0
            if self._last_start is not None:
                wait = self._last_start + _COMMENT_INTERVAL - now
            if len(self._starts) == _PAGES_PER_MINUTE:
                wait = max(wait, self._starts[0] + 60 - now)
            if wait > 0:
                self._sleep(wait)
            self._last_start = self._clock()
            self._starts.append(self._last_start)

    def reached(self) -> None:
        """平台正常作答（含业务错误码），说明没被拦。"""
        with self._lock:
            if self._probing:
                _log.warning("评论接口试探通过，熔断解除")
            self._probing = False
            self._blocked_until = 0.0

    def failed(self, signal: str | None) -> float | None:
        """请求失败。signal 为 None 表示与风控无关（网络等）。返回熔断剩余秒数，未熔断为 None。"""
        with self._lock:
            probing, self._probing = self._probing, False
            if signal != _BLOCKING_SIGNAL:
                return None
            duration = _RECHECK if probing else _COOLDOWN
            self._signal = signal
            self._blocked_until = self._clock() + duration
            _log.warning("评论接口熔断 %d 秒（信号 %s）", duration, signal)
            return duration


_comment_guard = _CommentGuard()


def check_comments_open() -> None:
    _comment_guard.check()


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
        if _group(path) != "comments":
            return self._get_json(path, params, referer, base_url, allow_codes)
        guard = _comment_guard  # 取一次：测试会整个替换它
        guard.acquire()
        try:
            data = self._get_json(path, params, referer, base_url, allow_codes)
        except _RateLimited as e:
            e.retry_in = guard.failed(e.signal)
            raise
        except BilibiliHttpError:
            guard.reached()
            raise
        except BaseException:
            guard.failed(None)
            raise
        guard.reached()
        return data

    def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None,
        referer: str,
        base_url: str | None,
        allow_codes: set[int] | None,
    ) -> Any:
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
