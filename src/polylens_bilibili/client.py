"""能力入口：把工具层的请求翻译成对 api 层的调用。

登录判断不在这里集中做，各能力在自己取数据的路径上判（见 api 下对应模块）。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .api._comments import fetch_comments, fetch_replies
from .api._constants import SHORT_LINK_HOSTS, USER_AGENT
from .api._danmaku import fetch_danmaku, top_by_heat
from .api._frame import fetch_frame
from .api._http import HttpClient, _RateLimited
from .api._login import check_qr_login, start_qr_login
from .api._search import fetch_search
from .api._signing import fetch_nav
from .api._subtitles import SubtitleTrack, fetch_subtitles
from .api._video import build_video_info, cid_for_page, clip_duration, fetch_view, list_parts
from .errors import BilibiliError
from .models import (
    Comment,
    Danmaku,
    LoginCheckResult,
    Page,
    QrLoginSession,
    ReplyThread,
    SearchItem,
    VideoInfo,
    VideoPart,
)

_BV_RE = re.compile(r"BV[0-9A-Za-z]+")
_AV_RE = re.compile(r"\bav(\d+)\b", re.IGNORECASE)
_PAGE_RE = re.compile(r"[?&]p=(\d+)")


def _expand_short_link(url: str, cookie: str = "", timeout: int = 20) -> str:
    """b23.tv 之类的短链展开成完整链接；不是短链则原样返回。

    带上 cookie：短链走的是网页域名，平台对部分出口 IP 的网页域名有风控，
    同一台机器裸 UA 得 412、带登录 cookie 则正常跳转。匿名 cookie 不管用。
    """
    if urlparse(url).netloc not in SHORT_LINK_HOSTS:
        return url
    headers = {"User-Agent": USER_AGENT}
    if cookie.strip():
        headers["Cookie"] = cookie.strip()
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as resp:  # noqa: S310
            return resp.geturl()
    except (HTTPError, URLError) as exc:
        hint = (
            "改用完整视频链接（含 BV 号）后重试。"
            if cookie.strip()
            else "本服务的出口地址可能被平台限制；登录后重试，或改用完整视频链接（含 BV 号）。"
        )
        raise BilibiliError(f"短链解析失败，无法展开 {url}；{hint}") from exc


def _extract_video_id(text: str) -> str:
    bv = _BV_RE.search(text)
    if bv:
        return bv.group(0)
    av = _AV_RE.search(text)
    if not av:
        raise BilibiliError(f"无法从输入解析 BV/av 号：{text}")
    return f"av{av.group(1)}"


def _extract_page(text: str) -> int | None:
    """从链接里取分段序号 ?p=N。取不到或不是正整数时返回 None。"""
    m = _PAGE_RE.search(text)
    if not m:
        return None
    value = int(m.group(1))
    return value if value >= 1 else None


def resolve_video(url: str, page: int | None = None, cookie: str = "") -> tuple[str, int]:
    """把输入解析成 (视频号, 分段序号)。

    分段序号取值顺序：显式传入 > 链接里的 ?p=N > 第 1 段。
    cookie 只在输入是短链时用得上，展开短链要它才能过风控。
    """
    if page is not None and page < 1:
        raise BilibiliError(f"page 需为正整数，收到 {page}")
    expanded = _expand_short_link(url, cookie)
    # 链接自带的 ?p=0 由 _extract_page 归入"没写"，不为它报错：这样的链接在网页上照样能打开。
    chosen = page if page is not None else _extract_page(expanded)
    return _extract_video_id(expanded), chosen or 1


class BilibiliClient:
    """一次工具调用的作用域内共用一个 HTTP 会话。"""

    def __init__(self, cookie: str = "") -> None:
        self._http = HttpClient(cookie=cookie)

    def _view(self, video_id: str) -> dict[str, Any]:
        return fetch_view(self._http, _id_params(video_id))

    def get_login_status(self) -> bool | None:
        """调平台接口核验 cookie 是否有效。None 表示无法验证。

        只把"确实无从判断"的情形归为 None：网络不可达、平台报错或改了响应形状、触发风控。
        其余异常照常上浮，免得代码缺陷被伪装成"平台不给答案"。
        """
        try:
            return fetch_nav(self._http).is_login
        except (BilibiliError, _RateLimited, OSError, ValueError, KeyError):
            return None

    def start_qr_login(self) -> QrLoginSession:
        return start_qr_login(self._http)

    def check_qr_login(self, key: str) -> LoginCheckResult:
        return check_qr_login(self._http, key)

    def get_video_info(self, video_id: str, page: int = 1) -> VideoInfo:
        info, _aid, _cid = build_video_info(self._view(video_id), page)
        return info

    def get_comments(
        self, video_id: str, *, count: int, cursor: str | None = None, sort: str = "hot"
    ) -> Page[Comment]:
        _info, aid, _cid = build_video_info(self._view(video_id))
        return fetch_comments(self._http, aid, count=count, cursor=cursor, sort=sort)

    def get_comment_replies(
        self, video_id: str, *, comment_ids: list[str], limit: int, cursor: str | None = None
    ) -> list[ReplyThread]:
        if not comment_ids:
            return []
        _info, aid, _cid = build_video_info(self._view(video_id))
        return fetch_replies(
            self._http, aid, [str(c) for c in comment_ids], limit=limit, cursor=cursor
        )

    def get_danmaku(
        self, video_id: str, *, count: int, page: int = 1
    ) -> list[Danmaku]:
        view = self._view(video_id)
        bullets = fetch_danmaku(self._http, cid_for_page(view, page))
        return top_by_heat(bullets, count)

    def get_subtitles(
        self, video_id: str, page: int = 1, lang: str | None = None
    ) -> SubtitleTrack:
        view = self._view(video_id)
        _info, aid, cid = build_video_info(view, page)
        return fetch_subtitles(self._http, aid, cid, lang)

    def get_parts(self, video_id: str) -> list[VideoPart]:
        return list_parts(self._view(video_id))

    def get_frame(self, video_id: str, *, timestamp: float, page: int = 1) -> bytes:
        at = float(timestamp)
        if at < 0:
            raise BilibiliError(f"timestamp 不能为负数，收到 {at:g}")
        view = self._view(video_id)
        bvid = str(view.get("bvid") or video_id)
        cid = cid_for_page(view, page)
        duration = clip_duration(view, cid)
        if duration is not None and at > duration:
            raise BilibiliError(f"请求的时间 {at:g} 秒超出视频时长（约 {duration:g} 秒）")
        return fetch_frame(self._http, bvid, cid, at)

    def search(
        self, *, query: str, count: int, cursor: str | None = None
    ) -> Page[SearchItem]:
        return fetch_search(self._http, query, count=count, cursor=cursor)


def _id_params(video_id: str) -> dict[str, Any]:
    if video_id.startswith("BV"):
        return {"bvid": video_id}
    if video_id.startswith("av"):
        return {"aid": int(video_id[2:])}
    raise BilibiliError(f"无法识别的视频号：{video_id}")
