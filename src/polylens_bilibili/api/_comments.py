"""评论：WBI 签名的主评论分页 + 楼中楼钻取。

未登录时平台不报错，而是只给几条并声称已到底，故两处都在取数据前判登录态。
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..errors import AuthRequiredError, BilibiliError, RateLimitedError
from ..models import Comment, Page, ReplyThread, to_local_time
from ._constants import ENDPOINTS, MAIN_PAGE_SIZE, REPLY_PAGE_SIZE
from ._http import HttpClient, _RateLimited
from ._signing import fetch_nav, sign_params

_MAIN_PAGE_DELAY = 0.2  # 主评论翻页间隔（抗风控）
_REPLY_PAGE_DELAY = 0.15  # 楼中楼翻页间隔
_AFTER_THREAD_DELAY = 0.2  # 每抓完一楼之后


def _joined(values: list[str]) -> str | None:
    return " ".join(values) or None


def _image_urls(content: dict[str, Any]) -> str | None:
    pictures = content.get("pictures") or []
    return _joined([
        str(p["img_src"]) for p in pictures if isinstance(p, dict) and p.get("img_src")
    ])


def _link_titles(content: dict[str, Any]) -> str | None:
    """评论原文里只有裸链接，标题在同一份响应的 jump_url 里，不必另发请求。"""
    jump_url = content.get("jump_url") or {}
    if not isinstance(jump_url, dict):
        return None
    return _joined([
        str(v["title"]) for v in jump_url.values() if isinstance(v, dict) and v.get("title")
    ])


def _normalize_reply(reply: dict[str, Any], *, root_id: int | None = None) -> Comment:
    rpid = reply.get("rpid")
    if not rpid:
        # 这个 id 是 get_comment_replies 的入参，给空串会让钻取失败在更远的地方。
        raise BilibiliError("评论数据缺少 rpid，无法定位这条评论")
    member = reply.get("member") or {}
    content_field = reply.get("content") or {}
    # parent 指向被回复的那条；等于本楼楼主（root_id）时置空：楼层嵌套已表达，只在"互回"时保留。
    parent = reply.get("parent")
    parent_id = str(parent) if parent and parent != root_id else None
    return Comment(
        id=str(rpid),
        author=member.get("uname", ""),
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


def _fetch_thread(
    client: HttpClient, aid: int, root_id: int, start: int, count: int
) -> Page[Comment]:
    """取某主评论楼中楼的 [start, start+count) 窗口（pn 可跳页，从 start 所在页起）。"""
    pn = start // REPLY_PAGE_SIZE + 1
    skip = start % REPLY_PAGE_SIZE  # 起始页内偏移
    collected: list[dict[str, Any]] = []
    reached_end = False
    while len(collected) < skip + count:
        data = client.get_json(
            ENDPOINTS["replies_sub"],
            {"oid": aid, "type": 1, "root": root_id, "ps": REPLY_PAGE_SIZE, "pn": pn},
        )
        page = (data or {}).get("replies") or []
        collected.extend(page)
        if len(page) < REPLY_PAGE_SIZE:
            reached_end = True
            break
        pn += 1
        time.sleep(_REPLY_PAGE_DELAY)
    window = collected[skip : skip + count]
    replies = [_normalize_reply(raw, root_id=root_id) for raw in window]
    has_more = (skip + len(window) < len(collected)) or not reached_end
    next_cursor = str(start + len(window)) if has_more else None
    return Page(items=replies, has_more=has_more, next_cursor=next_cursor)


def fetch_replies(
    client: HttpClient,
    aid: int,
    comment_ids: list[str],
    *,
    limit: int,
    cursor: str | None = None,
) -> list[ReplyThread]:
    """按 comment_id 钻取楼中楼。limit 是每楼取多少条，超出截断，靠 cursor 续取。

    内部串行（防风控）；中途触发风控则抛 RateLimitedError，不返回半程结果。
    楼中楼接口本身不需要 WBI 签名，这里调 nav 只为拿登录态。
    """
    if limit < 1:
        raise BilibiliError(f"limit 需为正整数，收到 {limit}")
    if not fetch_nav(client).is_login:
        raise AuthRequiredError("comment_replies")
    count = limit
    start = int(cursor) if cursor else 0
    results: list[ReplyThread] = []
    try:
        for cid in comment_ids:
            page = _fetch_thread(client, aid, int(cid), start, count)
            results.append(ReplyThread(comment_id=cid, page=page))
            time.sleep(_AFTER_THREAD_DELAY)
    except _RateLimited:
        raise RateLimitedError("楼中楼抓取触发风控，稍后重试。") from None
    return results


def _fetch_main_page(
    client: HttpClient, aid: int, img_key: str, sub_key: str, offset: str
) -> dict[str, Any]:
    """取一页主评论（WBI 签名，热度序 mode=3）。翻页与终止判断交给 fetch_comments。"""
    params: dict[str, Any] = {
        "oid": aid,
        "type": 1,
        "mode": 3,  # 热度序（B站评论固定按热度取）
        "pagination_str": json.dumps({"offset": offset}, separators=(",", ":")),
        "plat": 1,
    }
    signed = sign_params(params, img_key, sub_key, anti_risk=True)
    return client.get_json(ENDPOINTS["replies_main"], signed) or {}


def fetch_comments(
    client: HttpClient,
    aid: int,
    *,
    count: int,
    cursor: str | None = None,
) -> Page[Comment]:
    """抓取视频主评论（纯主评论，不含楼中楼）。置顶评论插入列表最前面。

    cursor=None 从头；count 为想要条数的下限（实际可能略多，整页对齐以保 cursor 续取不丢）。
    """
    if count < 1:
        raise BilibiliError(f"count 需为正整数，收到 {count}")
    nav = fetch_nav(client)
    if not nav.is_login:
        raise AuthRequiredError("comments")
    want = count
    comments: list[Comment] = []
    offset = cursor or ""
    has_more = False
    next_cursor: str | None = None
    first_page = not cursor  # 置顶评论只在从头的第一页出现

    try:
        while len(comments) < want:
            data = _fetch_main_page(client, aid, nav.img_key, nav.sub_key, offset)
            replies = data.get("replies") or []
            if not replies:
                break  # 空页 → 已抓全
            if first_page:
                first_page = False
                for raw in data.get("top_replies") or []:
                    comments.append(_normalize_reply(raw))
            for raw in replies:
                comments.append(_normalize_reply(raw))
            if len(replies) < MAIN_PAGE_SIZE:
                # 末页平台仍可能给出 is_end=false 与 next_offset：实测某视频共 15 条主评论，
                # 首页给 14 条（另 1 条在 top_replies）却说没到底，下一页才空。照搬会让调用方
                # 多发一次必然为空的请求。主评论每页固定 20 条，不满即到底。
                break
            cur = data.get("cursor") or {}
            if cur.get("is_end"):
                break  # 平台标记到底 → 抓全
            offset = (cur.get("pagination_reply") or {}).get("next_offset", "")
            if not offset:
                break  # 无下一页游标 → 抓全
            has_more = True  # 平台还有更多
            next_cursor = offset
            time.sleep(_MAIN_PAGE_DELAY)
    except _RateLimited:
        raise RateLimitedError("触发风控，稍后重试。") from None

    return Page(items=comments, has_more=has_more, next_cursor=next_cursor)
