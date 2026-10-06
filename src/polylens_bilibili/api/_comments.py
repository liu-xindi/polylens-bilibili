"""评论：WBI 签名的主评论分页 + 楼中楼钻取。

未登录时平台不报错，而是只给几条并声称已到底，故两处都在取数据前判登录态。
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from typing import Any

from ..errors import AuthRequiredError, BilibiliError, RateLimitedError
from ..models import Comment, Page, ReplyThread, space_url, to_local_time
from ._constants import ENDPOINTS, REPLY_PAGE_SIZE
from ._http import HttpClient, _RateLimited
from ._search import _int_or_none
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
    # parent 指向被回复的那条；等于本楼楼主（root_id）时置空：楼层嵌套已表达，只在"互回"时保留。
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


def _withheld_count(data: dict[str, Any]) -> int:
    """平台声称的回复数减去它肯列出的条数。

    两者常有差额（实测 46 对 40、11 对 10），差掉的那些被删除或折叠，任何页码都取不到，
    而它们仍会作为 parent_id 被其他回复引用。差额是发现引用断链的唯一线索。
    """
    declared = (data.get("root") or {}).get("count")
    listable = (data.get("page") or {}).get("count")
    if not isinstance(declared, int) or not isinstance(listable, int):
        return 0
    return max(0, declared - listable)


def _fetch_thread(
    client: HttpClient, aid: int, root_id: int, limit: int | None
) -> tuple[Page[Comment], int]:
    """从头取某主评论楼中楼的前 limit 条；limit 为 None 时一直翻到楼底。"""
    pn = 1
    collected: list[dict[str, Any]] = []
    reached_end = False
    withheld = 0
    upper_mid: int | None = None
    while limit is None or len(collected) < limit:
        data = client.get_json(
            ENDPOINTS["replies_sub"],
            {"oid": aid, "type": 1, "root": root_id, "ps": REPLY_PAGE_SIZE, "pn": pn},
        ) or {}
        owner = (data.get("root") or {}).get("oid")
        if isinstance(owner, int) and owner != aid:
            # 平台按 root 定位楼层，不校验 oid：别的视频的评论 id 照样返回数据
            raise BilibiliError(f"评论 {root_id} 不属于这个视频")
        page = data.get("replies") or []
        withheld = _withheld_count(data)
        upper_mid = _upper_mid(data)
        collected.extend(page)
        if len(page) < REPLY_PAGE_SIZE:
            reached_end = True
            break
        pn += 1
        time.sleep(_REPLY_PAGE_DELAY)
    window = collected if limit is None else collected[:limit]
    replies = [
        _normalize_reply(raw, root_id=root_id, upper_mid=upper_mid) for raw in window
    ]
    has_more = len(window) < len(collected) or not reached_end
    return Page(items=replies, has_more=has_more), withheld


def fetch_replies(
    client: HttpClient,
    aid: int,
    comment_ids: list[str],
    *,
    limit: int | None = None,
) -> list[ReplyThread]:
    """按 comment_id 钻取楼中楼。limit 是每楼只取前多少条；不传则取到楼底。

    内部串行（防风控）；中途触发风控则抛 RateLimitedError，不返回半程结果。
    单个楼取不到（评论不存在、不属于这个视频）只记在该楼的 error 上，不连累其他楼。
    楼中楼接口本身不需要 WBI 签名，这里调 nav 只为拿登录态。
    """
    if limit is not None and limit < 1:
        raise BilibiliError(f"limit 需为正整数，收到 {limit}")
    bad = [cid for cid in comment_ids if not cid.isdigit()]
    if bad:
        raise BilibiliError(f"comment_ids 需为数字 id，收到 {'、'.join(bad)}")
    if not fetch_nav(client).is_login:
        raise AuthRequiredError("comment_replies")
    results: list[ReplyThread] = []
    try:
        for cid in comment_ids:
            try:
                page, withheld = _fetch_thread(client, aid, int(cid), limit)
                results.append(ReplyThread(comment_id=cid, page=page, withheld=withheld))
            except BilibiliError as e:
                results.append(ReplyThread(comment_id=cid, page=Page(items=[]), error=str(e)))
            time.sleep(_AFTER_THREAD_DELAY)
    except _RateLimited:
        raise RateLimitedError("楼中楼抓取触发风控，稍后重试。") from None
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
) -> dict[str, Any]:
    """取一页主评论（WBI 签名）。翻页与终止判断交给 fetch_comments。"""
    params: dict[str, Any] = {
        "oid": aid,
        "type": 1,
        "mode": mode,
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
    sort: str = "hot",
) -> Page[Comment]:
    """抓取视频主评论（纯主评论，不含楼中楼）。置顶评论插入列表最前面。

    cursor=None 从头；count 为想要条数的下限（实际可能略多，整页对齐以保 cursor 续取不丢）。

    两种排序的游标性质不同。热度序的游标只装会话 key，翻页时平台回的 next_offset 恒等于它，
    进度记在平台侧：同一游标每次请求都返回下一批，换连接也一样，取过的批次重取不到。
    同一账号对同一视频的多个热度序会话互相干扰：新开一个会话后，旧游标的结果会退回前几页
    或与已取过的重叠；平台也不校验 key，伪造的 key 照样返回数据。不同视频之间互不影响。
    时间序的游标带位置，可重放，不受新会话影响。

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

    try:
        while offset is not None and len(comments) < want:
            data = _fetch_main_page(client, aid, nav.img_key, nav.sub_key, offset, mode)
            replies = data.get("replies") or []
            if not replies:
                offset = None
                break
            upper_mid = _upper_mid(data)
            if first_page:
                first_page = False
                for raw in data.get("top_replies") or []:
                    comments.append(_normalize_reply(raw, upper_mid=upper_mid))
            for raw in replies:
                comments.append(_normalize_reply(raw, upper_mid=upper_mid))
            cur = data.get("cursor") or {}
            if cur.get("is_end"):
                offset = None
                break
            offset = (cur.get("pagination_reply") or {}).get("next_offset") or None
            if offset is not None and len(comments) < want:
                time.sleep(_MAIN_PAGE_DELAY)
    except _RateLimited:
        if not comments:
            raise RateLimitedError("触发风控，稍后重试。") from None
        return Page(items=comments, has_more=True, next_cursor=offset, rate_limited=True)

    return Page(items=comments, has_more=offset is not None, next_cursor=offset)
