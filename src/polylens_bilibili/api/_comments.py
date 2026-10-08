"""评论：WBI 签名的主评论分页 + 二级评论钻取。

未登录时平台不报错，而是只给几条并声称已到底，故两处都在取数据前判登录态。
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Hashable
from dataclasses import replace
from functools import partial
from typing import Any, NamedTuple

from ..errors import AuthRequiredError, BilibiliError, RateLimitedError
from ..models import Comment, Page, ReplyThread, space_url, to_local_time
from ._constants import ENDPOINTS, REPLY_PAGE_SIZE
from ._http import HttpClient, _RateLimited
from ._page_cache import page_cache
from ._search import _int_or_none
from ._signing import fetch_nav, sign_params


def _joined(values: list[str]) -> str | None:
    """以换行连接：链接标题常含空格和 |，换行不会出现在标题与 URL 里。"""
    return "\n".join(values) or None


def _image_urls(content: dict[str, Any]) -> str | None:
    pictures = content.get("pictures") or []
    return _joined([
        str(p["img_src"]) for p in pictures if isinstance(p, dict) and p.get("img_src")
    ])


def _link_titles(content: dict[str, Any]) -> str | None:
    """评论原文里只有裸链接，标题在同一份响应的 jump_url 里，不必另发请求。

    jump_url 里混着两类条目：键是 URL 的才是链接，键是词的是平台自动加的可点击搜索词
    （其 title 与键相同），后者不是评论内容，取进来只会制造噪声。
    """
    jump_url = content.get("jump_url") or {}
    if not isinstance(jump_url, dict):
        return None
    return _joined([
        str(entry["title"])
        for key, entry in jump_url.items()
        if str(key).startswith(("http://", "https://"))
        and isinstance(entry, dict)
        and entry.get("title")
    ])


def _ip_location(reply: dict[str, Any]) -> str | None:
    text = (reply.get("reply_control") or {}).get("location")
    if not isinstance(text, str):
        return None
    return text.removeprefix("IP属地：").strip() or None


def _upper_mid(data: dict[str, Any]) -> int | None:
    return _int_or_none((data.get("upper") or {}).get("mid"))


def _normalize_reply(
    reply: dict[str, Any], *, root_id: int | None = None, upper_mid: int | None = None
) -> Comment:
    rpid = reply.get("rpid")
    if not rpid:
        # 这个 id 是 get_comment_replies 的入参，给空串会让钻取失败在更远的地方。
        raise BilibiliError("评论数据缺少 rpid，无法定位这条评论")
    member = reply.get("member") or {}
    content_field = reply.get("content") or {}
    # parent 指向被回复的那条；等于所属主评论（root_id）时置空：嵌套已表达，只在"互回"时保留。
    parent = reply.get("parent")
    parent_id = str(parent) if parent and parent != root_id else None
    # member.mid 是字符串，upper.mid 是整数
    mid = _int_or_none(member.get("mid"))
    return Comment(
        id=str(rpid),
        author=member.get("uname", ""),
        author_url=space_url(mid),
        author_level=_int_or_none((member.get("level_info") or {}).get("current_level")),
        is_up=mid is not None and mid == upper_mid,
        ip_location=_ip_location(reply),
        content=content_field.get("message", ""),
        like_count=reply.get("like", 0),
        reply_count=reply.get("count", 0),
        parent_id=parent_id,
        created_at=to_local_time(reply.get("ctime")),
        is_top=bool((reply.get("reply_control") or {}).get("is_up_top")),
        up_liked=bool((reply.get("up_action") or {}).get("like")),
        image_urls=_image_urls(content_field),
        link_titles=_link_titles(content_field),
    )


def _listable_count(data: dict[str, Any]) -> int | None:
    count = (data.get("page") or {}).get("count")
    return count if isinstance(count, int) else None


def _withheld_count(data: dict[str, Any]) -> int:
    """平台声称的回复数减去它肯列出的条数。

    两者常有差额（实测 46 对 40、11 对 10），差掉的那些被删除或折叠，任何页码都取不到，
    而它们仍会作为 parent_id 被其他回复引用。差额是发现引用断链的唯一线索。
    """
    declared = (data.get("root") or {}).get("count")
    listable = _listable_count(data)
    if not isinstance(declared, int) or listable is None:
        return 0
    return max(0, declared - listable)


class _ThreadPage(NamedTuple):
    replies: list[Comment]
    withheld: int
    total: int | None  # 平台能列出的回复总数


class _MainPage(NamedTuple):
    top: list[Comment]
    replies: list[Comment]
    next_offset: str | None  # None 表示到底


def _replayable[P: (_ThreadPage, _MainPage)](
    key: Hashable, fetch: Callable[[], P]
) -> tuple[P, int | None]:
    """先查缓存，未命中再请求。返回页与命中时的抓取时刻；空页可能是平台临时异常，不缓存。"""
    hit = page_cache.get(key)
    if hit is not None:
        return hit.value, hit.fetched_at
    page = fetch()
    if page.replies:
        page_cache.put(key, page)
    return page, None


def _oldest(a: int | None, b: int | None) -> int | None:
    return b if a is None else a if b is None else min(a, b)


def _fetch_thread_page(client: HttpClient, aid: int, root_id: int, pn: int) -> _ThreadPage:
    data = client.get_json(
        ENDPOINTS["replies_sub"],
        {"oid": aid, "type": 1, "root": root_id, "ps": REPLY_PAGE_SIZE, "pn": pn},
    ) or {}
    owner = (data.get("root") or {}).get("oid")
    if isinstance(owner, int) and owner != aid:
        # 平台按 root 定位主评论，不校验 oid：别的视频的评论 id 照样返回数据
        raise BilibiliError(f"评论 {root_id} 不属于这个视频")
    upper_mid = _upper_mid(data)
    replies = [
        _normalize_reply(raw, root_id=root_id, upper_mid=upper_mid)
        for raw in data.get("replies") or []
    ]
    return _ThreadPage(replies, _withheld_count(data), _listable_count(data))


def _fetch_thread(
    client: HttpClient, aid: int, comment_id: str, start: int, pages: int
) -> ReplyThread:
    """取某主评论二级评论从第 start 页起的 pages 页，遇到不满的页即停。"""
    root_id = int(comment_id)
    collected: list[Comment] = []
    reached_end = False
    fetched_at: int | None = None
    pn = start
    for pn in range(start, start + pages):
        page, hit_at = _replayable(
            ("sub", client.account, aid, root_id, pn),
            partial(_fetch_thread_page, client, aid, root_id, pn),
        )
        fetched_at = _oldest(fetched_at, hit_at)
        collected.extend(page.replies)
        if len(page.replies) < REPLY_PAGE_SIZE:
            reached_end = True
            break
    # 末页恰好满 20 条时只有 total 能说明已到底
    if page.total is not None and pn * REPLY_PAGE_SIZE >= page.total:
        reached_end = True
    return ReplyThread(
        comment_id=comment_id,
        page=Page(items=collected, has_more=not reached_end, cached_at=to_local_time(fetched_at)),
        withheld=page.withheld,
        total=page.total,
    )


def fetch_replies(
    client: HttpClient,
    aid: int,
    comment_ids: list[str],
    *,
    start_page: int = 1,
    pages: int,
) -> list[ReplyThread]:
    """按 comment_id 钻取二级评论。每条主评论都取从第 start_page 页起的 pages 页，每页 20 条。

    内部串行，请求间隔由 HTTP 层统一控制。
    单条主评论取不到（评论不存在、不属于这个视频）只记在它的 error 上，不影响其他。
    中途触发风控或限流即停：已取完的照常返回，被打断的与没轮到的在 error 里说明原因。
    按页码取，结果可重放，留下已取完的没有副作用。一条都没取完才抛 RateLimitedError。
    二级评论接口本身不需要 WBI 签名，这里调 nav 只为拿登录态。
    """
    if start_page < 1:
        raise BilibiliError(f"start_page 需为正整数，收到 {start_page}")
    if pages < 1:
        raise BilibiliError(f"pages 需为正整数，收到 {pages}")
    bad = [cid for cid in comment_ids if not cid.isdigit()]
    if bad:
        raise BilibiliError(f"comment_ids 需为数字 id，收到 {'、'.join(bad)}")
    if not fetch_nav(client).is_login:
        raise AuthRequiredError("comment_replies")
    results: list[ReplyThread] = []
    for i, cid in enumerate(comment_ids):
        try:
            results.append(_fetch_thread(client, aid, cid, start_page, pages))
        except BilibiliError as e:
            results.append(ReplyThread(comment_id=cid, page=Page(items=[]), error=str(e)))
        except _RateLimited as e:
            reason = e.describe("二级评论抓取")
            if not results:
                raise RateLimitedError(reason) from None
            results.extend(
                ReplyThread(comment_id=rest, page=Page(items=[]), error=reason)
                for rest in comment_ids[i:]
            )
            break
    return results


def _check_cursor(cursor: str) -> None:
    """平台不校验游标，乱写的游标会被当成从头开始，静默重复抓取。

    平台发出的游标都是 base64，解不开的必定不是它发的；能解开的伪造游标仍拦不住。
    """
    try:
        decoded = base64.b64decode(cursor, validate=True)
    except (binascii.Error, ValueError):
        decoded = b""
    if not decoded:
        raise BilibiliError(
            f"无法识别的续取游标: {cursor!r}；请原样回传上次返回的 next_cursor"
        )


#: 对外的排序名 → 平台的 mode 编码。平台在 cursor.support_mode 里声明支持这两种。
_SORT_MODE = {"hot": 3, "newest": 2}


def _fetch_main_page(
    client: HttpClient, aid: int, img_key: str, sub_key: str, offset: str, mode: int
) -> _MainPage:
    """取一页主评论（WBI 签名）。翻页与终止判断交给 fetch_comments。"""
    params: dict[str, Any] = {
        "oid": aid,
        "type": 1,
        "mode": mode,
        "pagination_str": json.dumps({"offset": offset}, separators=(",", ":")),
        "plat": 1,
    }
    signed = sign_params(params, img_key, sub_key, anti_risk=True)
    data = client.get_json(ENDPOINTS["replies_main"], signed) or {}
    upper_mid = _upper_mid(data)
    cur = data.get("cursor") or {}
    next_offset = None if cur.get("is_end") else (
        (cur.get("pagination_reply") or {}).get("next_offset") or None
    )
    return _MainPage(
        top=[_normalize_reply(raw, upper_mid=upper_mid) for raw in data.get("top_replies") or []],
        replies=[_normalize_reply(raw, upper_mid=upper_mid) for raw in data.get("replies") or []],
        next_offset=next_offset,
    )


def fetch_comments(
    client: HttpClient,
    aid: int,
    *,
    count: int,
    cursor: str | None = None,
    sort: str = "hot",
    batch_id: str | None = None,
    session: str | None = None,
) -> Page[Comment]:
    """抓取视频主评论（纯主评论，不含二级评论）。置顶评论插入列表最前面。

    cursor=None 从头；count 为想要条数的下限（实际可能略多，整页对齐以保 cursor 续取不丢）。

    两种排序的游标性质不同。热度序的游标只装会话 key，翻页时平台回的 next_offset 恒等于它，
    进度记在平台侧：同一游标每次请求都返回下一批，换连接也一样，取过的批次重取不到。
    同一账号对同一视频的多个热度序会话互相干扰：新开一个会话后，旧游标只返回已取过的内容，
    新旧游标混用时两者都会回退（10-07 实测）；平台也不校验 key，伪造的 key 照样返回数据。
    不同视频之间互不影响。
    时间序的游标带位置，可重放，不受新会话影响；因此时间序的页走缓存。
    热度序要重放只能靠调用方给的 batch_id：同一游标配同一 batch_id 时整批缓存，再取时原样返回。
    batch_id 由模型自取，各对话常取同样的名字（如 a1），故按 MCP 会话隔离：
    claude.ai 的连接器按对话给出会话 ID，同一对话续接也不变（10-08 实测）。

    中途触发风控时返回已取到的部分并标 rate_limited：热度序下这些页平台已记为取过，
    丢掉它们，调用方用同一游标重试也取不回来。一页都没取到才抛 RateLimitedError。

    到底只认平台给的信号：标了 is_end、返回空页、或没有下一页游标。不按"这页不满 20 条"
    推断，那是错的：第一页有置顶评论时平台只给 19 条常规评论，中途页也出现过 19 条。
    代价是热度序的末页会谎报 is_end=false 并给出游标，要多发一次必然为空的请求才停。
    """
    if count < 1:
        raise BilibiliError(f"count 需为正整数，收到 {count}")
    if sort not in _SORT_MODE:
        raise BilibiliError(f"未知的排序方式 {sort!r}，可选：{'、'.join(_SORT_MODE)}")
    if cursor:
        _check_cursor(cursor)
    key = None
    if sort == "hot" and batch_id:
        key = ("hot", client.account, aid, session, cursor or "", batch_id)
    if key is not None and (hit := page_cache.get(key)) is not None:
        return replace(hit.value, cached_at=to_local_time(hit.fetched_at))
    page = _collect_main(client, aid, count=count, cursor=cursor, sort=sort)
    if key is not None and page.items:
        page_cache.put(key, page)
    return page


def _collect_main(
    client: HttpClient, aid: int, *, count: int, cursor: str | None, sort: str
) -> Page[Comment]:
    mode = _SORT_MODE[sort]
    nav = fetch_nav(client)
    if not nav.is_login:
        raise AuthRequiredError("comments")
    want = count
    comments: list[Comment] = []
    # offset 恒指向"还没取回的那一页"，None 表示没有下一页。抓完一页立刻改写它，
    # 故循环无论从哪个出口结束，它都可以直接当对外游标用。
    # 若改成先推进再抓，游标会停在已经取回的那页上，调用方续取时重复拿到同一批。
    offset: str | None = cursor or ""
    first_page = not cursor  # 置顶评论只在从头的第一页出现
    fetched_at: int | None = None

    try:
        while offset is not None and len(comments) < want:
            fetch = partial(_fetch_main_page, client, aid, nav.img_key, nav.sub_key, offset, mode)
            if sort == "newest":
                page, hit_at = _replayable(("main", client.account, aid, mode, offset), fetch)
                fetched_at = _oldest(fetched_at, hit_at)
            else:
                page = fetch()
            if not page.replies:
                offset = None
                break
            if first_page:
                first_page = False
                comments.extend(page.top)
            comments.extend(page.replies)
            offset = page.next_offset
    except _RateLimited as e:
        if not comments:
            raise RateLimitedError(e.describe("评论")) from None
        return Page(
            items=comments, has_more=True, next_cursor=offset,
            rate_limited=e.describe_partial("评论"), cached_at=to_local_time(fetched_at),
        )

    return Page(
        items=comments, has_more=offset is not None, next_cursor=offset,
        cached_at=to_local_time(fetched_at),
    )
