"""搜索：调 search/type（WBI 签名），映射为 SearchItem。

每页固定 30 条，游标是已取的页数。
到底判据：已取条数达结果上限、或平台一条都不给。标题去接口的高亮标签。
"""

from __future__ import annotations

import html
import re
from typing import Any

from ..errors import BilibiliError, RateLimitedError
from ..models import Page, SearchItem, space_url, to_local_time
from ._constants import ENDPOINTS, SEARCH_PAGE_CAP, SEARCH_PAGE_SIZE, SEARCH_REFERER
from ._http import HttpClient, _RateLimited
from ._signing import fetch_nav, sign_params

_EM_RE = re.compile(r"</?em[^>]*>", re.IGNORECASE)


def _clean_title(text: str) -> str:
    """去搜索结果标题里的高亮标签，再还原 &amp; 之类的 HTML 实体。

    先去标签后还原：标题原文里若有 &lt;em&gt;，还原出的 <em> 是标题内容，不该被当标签去掉。
    """
    return html.unescape(_EM_RE.sub("", text))


def _parse_offset(cursor: str | None) -> int:
    """游标 → 绝对偏移量（已取到第几条）。不传即从头。

    游标由本能力发出，解析不了说明调用方自造了，与"游标不透明、原样回传"的契约相悖。
    """
    if cursor is None:
        return 0
    try:
        offset = int(cursor)
    except ValueError:
        raise BilibiliError(
            f"无法识别的续取游标: {cursor!r}；请原样回传上次返回的 next_cursor"
        ) from None
    if offset < 0:
        raise BilibiliError(
            f"无法识别的续取游标: {cursor!r}；请原样回传上次返回的 next_cursor"
        )
    return offset


def _int_or_none(value: Any) -> int | None:
    """转成整数；转不动就当此项没有。

    平台字段的类型不可控：数组/对象抛 TypeError，非数字串与 nan 抛 ValueError，
    inf 抛 OverflowError。任一情形都只该让这一项缺失，不该带崩整页。
    转成功后 0 是真实计数（新投稿播放量确实为 0），照常留下。
    """
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _duration_seconds(text: Any) -> float | None:
    """平台的时长文本 → 秒数；解析不了就当此项没有。

    冒号分段从右往左依次是秒、分、时。平台实测只给"总分钟:秒"（长视频作 "5505:10"，
    没有小时段），按位取仍兼容它哪天补上小时段。
    """
    if not isinstance(text, str):
        return None
    chunks = text.strip().split(":")
    if not 1 <= len(chunks) <= 3:
        return None
    total = 0
    for unit, chunk in zip((1, 60, 3600), reversed(chunks), strict=False):
        try:
            value = int(chunk)
        except ValueError:
            return None
        if value < 0:
            return None
        total += unit * value
    return float(total)


def _epoch_s_to_local(value: Any) -> str | None:
    """epoch 秒 → 本机时区可读串；转不动或越界就当没有发布时间。

    越界的时间戳在 fromtimestamp 处抛 OSError（平台相关，也可能是 OverflowError）。
    """
    try:
        return to_local_time(int(value))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _to_search_item(raw: Any) -> SearchItem | None:
    """单条映射。取不到 bvid 就跳过，免得一条坏数据毁掉整页。

    bvid 是唯一进链接的字段，类型不对会拼出看着能用实则无效的 URL，
    比整条缺失更难察觉，故连类型一并判。标题/作者类型不对只是那一格难看，
    不影响消费端据链接继续取内容，照原样透传。
    """
    if not isinstance(raw, dict):
        return None
    bvid = raw.get("bvid")
    if not isinstance(bvid, str) or not bvid:
        return None
    return SearchItem(
        title=_clean_title(str(raw.get("title") or "")),
        url=f"https://www.bilibili.com/video/{bvid}",
        author=raw.get("author") or None,
        author_url=space_url(raw.get("mid")),
        published_at=_epoch_s_to_local(raw.get("pubdate")),
        duration_sec=_duration_seconds(raw.get("duration")),
        category=raw.get("typename") or None,
        tags=raw.get("tag") or None,
        summary=raw.get("description") or None,
        view_count=_int_or_none(raw.get("play")),
        danmaku_count=_int_or_none(raw.get("danmaku")),
        comment_count=_int_or_none(raw.get("review")),
        like_count=_int_or_none(raw.get("like")),
        favorite_count=_int_or_none(raw.get("favorites")),
    )


ORDERS = {
    "relevance": "totalrank",
    "newest": "pubdate",
    "most_viewed": "click",
    "most_danmaku": "dm",
    "most_favorited": "stow",
}


def fetch_search(
    client: HttpClient, keyword: str, *, cursor: str | None = None, order: str = "relevance"
) -> Page[SearchItem]:
    """搜索关键词，取 cursor 指向的那一页视频结果 + 续取状态。"""
    query = keyword.strip()
    if not query:
        raise BilibiliError("搜索关键词不能为空")
    pages_taken = _parse_offset(cursor)
    if pages_taken >= SEARCH_PAGE_CAP:
        return Page(items=[])
    nav = fetch_nav(client)
    params = {
        "search_type": "video", "keyword": query, "page": pages_taken + 1,
        "page_size": SEARCH_PAGE_SIZE, "order": ORDERS[order],
    }
    signed = sign_params(params, nav.img_key, nav.sub_key)
    try:
        data = client.get_json(ENDPOINTS["search_type"], signed, referer=SEARCH_REFERER)
    except _RateLimited as e:
        # 判到底会把风控伪装成"没有更多结果"，翻页从此静默断掉
        raise RateLimitedError(e.describe("搜索")) from None
    # 平台哪天不按请求的 page_size 分页，按页数续取就会错位，结果静默截断或重复。
    # 响应回显了 pagesize 就核一遍，把这个前提变成代码里自己会报警的不变量。
    echoed_page_size = (data or {}).get("pagesize")
    if echoed_page_size is not None and echoed_page_size != SEARCH_PAGE_SIZE:
        raise BilibiliError(
            f"平台未按请求的每页条数分页（请求 {SEARCH_PAGE_SIZE}，实为 {echoed_page_size}）"
        )
    result = (data or {}).get("result")
    if result is not None and not isinstance(result, list):
        # 条目一级的坏数据跳过, 容器一级的形状变化则显式失败: 后者是平台改了响应,
        # 静默返回空页会把它伪装成"这批全是坏条目"。字符串最需要这道判定 ——
        # 它可切片可迭代, 逐字符都会被条目级的 isinstance 挡掉, 整页悄悄变空而游标照走。
        # 判在兜空值之前: "" 与 {} 也是形状变了, 不是"没有结果"。缺 result 才是没有结果。
        raise BilibiliError(f"搜索响应的 result 应为列表，实为 {type(result).__name__}")
    # 页码越过总页数时平台不给空页，而是回显最后一页的页码并返回那一页
    echoed_page = (data or {}).get("page")
    if isinstance(echoed_page, int) and echoed_page != pages_taken + 1:
        return Page(items=[])
    raw_items = result or []
    items = [item for raw in raw_items if (item := _to_search_item(raw)) is not None]
    num_pages = (data or {}).get("numPages")
    has_more = (
        pages_taken + 1 < SEARCH_PAGE_CAP
        and len(raw_items) > 0
        and (not isinstance(num_pages, int) or pages_taken + 1 < num_pages)
    )
    return Page(
        items=items, has_more=has_more, next_cursor=str(pages_taken + 1) if has_more else None
    )
