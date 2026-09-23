"""首页推荐流：index/top/feed/rcmd（WBI 签名），映射为 FeedItem。

只传 ps。浏览器还会带 fresh_idx、brush、feed_version 等十来个参数，实测都不影响取到什么：
位置由平台按账号维护，客户端传的刷新序号连"重放同一批"都做不到，纯属埋点。其中
feed_version 反而是广告位的开关，带上它平台会往流里塞广告与直播条目，不带就是纯视频。
"""

from __future__ import annotations

from typing import Any

from ..errors import BilibiliError
from ..models import FeedItem, space_url
from ._constants import ENDPOINTS
from ._http import HttpClient
from ._search import _epoch_s_to_local, _int_or_none
from ._signing import fetch_nav, sign_params


def _duration_seconds(value: Any) -> float | None:
    """推荐流的 duration 已经是秒数；负数与非数字当此项没有。"""
    total = _int_or_none(value)
    return float(total) if total is not None and total >= 0 else None


def _to_feed_item(raw: Any) -> FeedItem | None:
    """单条映射。只收视频条目，取不到 bvid 就跳过。

    goto 除 av 外还可能是 ad（广告位）与 live（直播间），两者都没有 bvid，
    混进来会拼出看着能用实则无效的链接。
    """
    if not isinstance(raw, dict) or raw.get("goto") != "av":
        return None
    bvid = raw.get("bvid")
    if not isinstance(bvid, str) or not bvid:
        return None
    stat = raw.get("stat") or {}
    return FeedItem(
        title=str(raw.get("title") or ""),
        url=f"https://www.bilibili.com/video/{bvid}",
        author=(raw.get("owner") or {}).get("name") or None,
        author_url=space_url((raw.get("owner") or {}).get("mid")),
        published_at=_epoch_s_to_local(raw.get("pubdate")),
        duration_sec=_duration_seconds(raw.get("duration")),
        view_count=_int_or_none(stat.get("view")),
        rcmd_reason=(raw.get("rcmd_reason") or {}).get("content") or None,
    )


def fetch_feed(client: HttpClient, *, count: int) -> list[FeedItem]:
    """取一批首页推荐。

    每次调用都是新的一批：平台按账号维护位置，没有游标也没有尽头。
    未登录时给通用推荐，与登录态的结果不重叠。
    """
    if count < 1:
        raise BilibiliError(f"count 需为正整数，收到 {count}")
    nav = fetch_nav(client)
    data = client.get_json(ENDPOINTS["feed_rcmd"], sign_params({"ps": count},
                                                               nav.img_key, nav.sub_key))
    items = (data or {}).get("item")
    if items is not None and not isinstance(items, list):
        # 与搜索一致：条目一级的坏数据跳过，容器一级的形状变化显式失败。
        raise BilibiliError(f"推荐流响应的 item 不是列表, 而是 {type(items).__name__}")
    return [item for raw in (items or []) if (item := _to_feed_item(raw)) is not None]
