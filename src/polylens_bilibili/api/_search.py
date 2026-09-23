"""搜索：调 search/type（WBI 签名），映射为 SearchItem。

分页游标存的是绝对偏移量（已取到第几条），不是页码。页码的含义依赖每页条数，
而每页条数握在调用方手里，游标察觉不到它变化；偏移量与之无关，因而自足：
调用方中途改 count 也不会重复或漏取。
到底判据：已取条数达结果上限、或平台一条都不给。标题去接口的高亮标签。
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import BilibiliError, RateLimitedError
from ..models import Page, SearchItem, to_local_time
from ._constants import ENDPOINTS, SEARCH_REFERER, SEARCH_RESULT_CAP
from ._http import HttpClient, _RateLimited
from ._signing import fetch_nav, sign_params

_EM_RE = re.compile(r"</?em[^>]*>", re.IGNORECASE)


def _strip_em(text: str) -> str:
    """去搜索结果标题里的高亮标签。"""
    return _EM_RE.sub("", text)


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
        title=_strip_em(str(raw.get("title") or "")),
        url=f"https://www.bilibili.com/video/{bvid}",
        author=raw.get("author") or None,
        published_at=_epoch_s_to_local(raw.get("pubdate")),
        duration_sec=_duration_seconds(raw.get("duration")),
        view_count=_int_or_none(raw.get("play")),
        danmaku_count=_int_or_none(raw.get("danmaku")),
    )


def fetch_search(
    client: HttpClient, keyword: str, *, count: int, cursor: str | None = None
) -> Page[SearchItem]:
    """搜索关键词，从 cursor 指向的位置往后取一批视频结果 + 续取状态。

    平台只认页码，故把偏移量换算成 (页码, 页内跳过数)：页码 = offset // count + 1，
    再丢掉本页开头 offset % count 条，结果精确从第 offset+1 条接上。
    count 不变时跳过数恒为 0，与直接翻页等价，本次即取满 count 条；
    count 中途改小则跳过数非 0，本次只给到本页剩余（少于 count），不重不漏，
    按 next_cursor 再喊一次接着取。
    """
    query = keyword.strip()
    if not query:
        raise BilibiliError("搜索关键词不能为空")
    if count < 1:
        raise BilibiliError(f"count 需为正整数，收到 {count}")
    size = count
    offset = _parse_offset(cursor)
    page_num, skip = divmod(offset, size)
    nav = fetch_nav(client)
    params = {"search_type": "video", "keyword": query, "page": page_num + 1, "page_size": size}
    signed = sign_params(params, nav.img_key, nav.sub_key)
    try:
        data = client.get_json(ENDPOINTS["search_type"], signed, referer=SEARCH_REFERER)
    except _RateLimited:
        # 判到底会把风控伪装成"没有更多结果"，翻页从此静默断掉
        raise RateLimitedError("搜索触发风控，稍后重试。") from None
    # 整套换算系于"平台按请求的 page_size 分页"这一条实测事实, 而它是纯外部的。
    # 平台哪天改回固定页大小, 切片照样成立, 只是窗口错位 —— 结果是静默截断, 没有任何信号。
    # 响应回显了 pagesize 就核一遍, 把这个前提变成代码里自己会报警的不变量。
    echoed_page_size = (data or {}).get("pagesize")
    if echoed_page_size is not None and echoed_page_size != size:
        raise BilibiliError(
            f"平台未按请求的每页条数分页 (请求 {size}, 实为 {echoed_page_size})"
        )
    result = (data or {}).get("result")
    if result is not None and not isinstance(result, list):
        # 条目一级的坏数据跳过, 容器一级的形状变化则显式失败: 后者是平台改了响应,
        # 静默返回空页会把它伪装成"这批全是坏条目"。字符串最需要这道判定 ——
        # 它可切片可迭代, 逐字符都会被条目级的 isinstance 挡掉, 整页悄悄变空而游标照走。
        # 判在兜空值之前: "" 与 {} 也是形状变了, 不是"没有结果"。缺 result 才是没有结果。
        raise BilibiliError(f"搜索响应的 result 不是列表, 而是 {type(result).__name__}")
    raw_items = (result or [])[skip:]
    items = [item for raw in raw_items if (item := _to_search_item(raw)) is not None]
    # 游标按消费掉的原始条目数推进，不是按映射成功的条数：跳过一条坏数据后若只进 1，
    # 下一次会把它后面那条好数据再返回一遍。整页全坏也照样往前翻。
    taken = offset + len(raw_items)
    # 平台结果封顶 1000 条：判的是已取条数撞到上限，与每页取几条无关
    has_more = taken < SEARCH_RESULT_CAP and len(raw_items) > 0
    return Page(items=items, has_more=has_more, next_cursor=str(taken) if has_more else None)
