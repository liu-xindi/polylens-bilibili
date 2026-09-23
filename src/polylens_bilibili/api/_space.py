"""UP 主投稿列表：space/wbi/arc/search（WBI 签名），映射为 UpVideoItem。

浏览器还会带 dm_* 风控参数、w_webid 等，实测登录后只带 WBI 签名就能稳定取到；
未登录时时好时坏，常被风控拦下。
每页固定 40 条，与网页一致；游标是已取的页数。
"""

from __future__ import annotations

from typing import Any

from ..errors import RateLimitedError
from ..models import Page, UpVideoItem
from ._constants import ENDPOINTS
from ._http import HttpClient, _RateLimited
from ._search import _duration_seconds, _epoch_s_to_local, _int_or_none, _parse_offset
from ._signing import fetch_nav, sign_params

PAGE_SIZE = 40
ORDERS = {"newest": "pubdate", "most_viewed": "click", "most_favorited": "stow"}


def _to_item(raw: dict[str, Any]) -> UpVideoItem:
    return UpVideoItem(
        title=str(raw.get("title") or ""),
        url=f"https://www.bilibili.com/video/{raw['bvid']}",
        published_at=_epoch_s_to_local(raw.get("created")),
        duration_sec=_duration_seconds(raw.get("length")),
        view_count=_int_or_none(raw.get("play")),
        danmaku_count=_int_or_none(raw.get("video_review")),
        comment_count=_int_or_none(raw.get("comment")),
    )


def fetch_up_videos(
    client: HttpClient,
    mid: int,
    *,
    cursor: str | None = None,
    order: str = "newest",
    keyword: str | None = None,
) -> tuple[str | None, Page[UpVideoItem]]:
    """取 UP 主的一页投稿，返回 (UP 主昵称, 这一页 + 续取状态)。

    昵称取自条目：接口不单独给，一条投稿都没有时就是 None。
    """
    pages_taken = _parse_offset(cursor)
    params: dict[str, Any] = {
        "mid": mid, "pn": pages_taken + 1, "ps": PAGE_SIZE, "order": ORDERS[order],
    }
    if keyword and keyword.strip():
        params["keyword"] = keyword.strip()
    nav = fetch_nav(client)
    try:
        data = client.get_json(
            ENDPOINTS["space_videos"],
            sign_params(params, nav.img_key, nav.sub_key),
            referer=f"https://space.bilibili.com/{mid}/upload/video",
        )
    except _RateLimited:
        raise RateLimitedError("取 UP 主投稿触发风控，稍后重试。") from None
    vlist = data["list"]["vlist"] or []
    has_more = (pages_taken + 1) * PAGE_SIZE < data["page"]["count"] and len(vlist) > 0
    page = Page(
        items=[_to_item(raw) for raw in vlist],
        has_more=has_more,
        next_cursor=str(pages_taken + 1) if has_more else None,
    )
    return (vlist[0].get("author") if vlist else None), page
