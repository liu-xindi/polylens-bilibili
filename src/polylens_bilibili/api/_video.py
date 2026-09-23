"""视频元信息：从 view 接口数据提取并映射为 VideoInfo。

多段视频：一个 BV 下含多段，每段有自己的 cid（弹幕/字幕/截帧按段取）。page 为 1 起的分段序号，
对应链接里的 ?p=N。
"""

from __future__ import annotations

from typing import Any

from ..errors import BilibiliError
from ..models import VideoInfo, VideoPart, space_url, to_local_time
from ._constants import ENDPOINTS
from ._http import HttpClient


def _resolve_page(view: dict[str, Any], page: int) -> dict[str, Any]:
    """定位第 page 段，返回该段条目（含 cid/page/part/duration）。

    单段视频忽略 page，与网页一致（网页对单段视频的 ?p= 不作反应）。
    多段视频优先按 pages 里的 page 字段精确匹配，个别响应缺 page 字段时按位置回退；
    越界则报错，不静默退回第 1 段。
    """
    pages = view.get("pages") or []
    if len(pages) <= 1:
        if pages and pages[0].get("cid"):
            return pages[0]
        if view.get("cid"):
            return {
                "cid": view["cid"], "page": 1, "part": None, "duration": view.get("duration"),
            }
        raise BilibiliError("无法确定视频分段信息")
    for p in pages:
        if p.get("page") == page and p.get("cid"):
            return p
    if 1 <= page <= len(pages) and pages[page - 1].get("cid"):
        return pages[page - 1]
    raise BilibiliError(f"该视频共 {len(pages)} 段，没有第 {page} 段")


def cid_for_page(view: dict[str, Any], page: int) -> int:
    """第 page 段的 cid。弹幕/字幕/截帧接口需要它。"""
    return int(_resolve_page(view, page)["cid"])


def clip_duration(view: dict[str, Any], cid: int) -> float | None:
    """截帧所用分段的时长（秒）：多段取对应段，否则取整片。无法确定时返回 None。"""
    for page in view.get("pages") or []:
        if page.get("cid") == cid and page.get("duration"):
            return float(page["duration"])
    dur = view.get("duration")
    return float(dur) if dur else None


def fetch_view(client: HttpClient, params: dict[str, Any]) -> dict[str, Any]:
    """拉取视频原始 view 数据（含全部分段，与请求的是哪一段无关）。"""
    return client.get_json(ENDPOINTS["video_info"], params)


def _summary_of(view: dict[str, Any]) -> str | None:
    desc_v2 = view.get("desc_v2")
    if isinstance(desc_v2, list) and desc_v2:
        parts = [seg.get("raw_text", "") for seg in desc_v2 if isinstance(seg, dict)]
        text = "".join(parts).strip() or (view.get("desc") or "").strip()
    else:
        text = (view.get("desc") or "").strip()
    return text or None


def build_video_info(view: dict[str, Any], page: int = 1) -> tuple[VideoInfo, int, int]:
    """从 view 数据构造 VideoInfo，同时返回 aid 和当前段 cid（供后续能力使用）。

    标题/作者/统计为整片信息（全段共用）；分段信息只在多段视频上给出。
    """
    # aid 与 bvid 是后续能力的定位依据：aid 给评论接口，bvid 进对外链接。
    # 缺了给默认值会让评论去查 oid=0、链接拼成 /video//，错误推迟到更难定位的地方。
    if not view.get("aid"):
        raise BilibiliError("视频数据缺少 aid，无法定位该视频")
    if not view.get("bvid"):
        raise BilibiliError("视频数据缺少 bvid，无法定位该视频")
    aid = int(view["aid"])
    bvid = str(view["bvid"])
    selected = _resolve_page(view, page)
    cid = int(selected["cid"])
    stat = view.get("stat") or {}
    pages = view.get("pages") or []
    is_multi = len(pages) > 1
    duration = selected.get("duration") if is_multi else view.get("duration")

    info = VideoInfo(
        id=bvid,
        title=view.get("title", ""),
        author=(view.get("owner") or {}).get("name"),
        author_url=space_url((view.get("owner") or {}).get("mid")),
        url=f"https://www.bilibili.com/video/{bvid}/",
        published_at=to_local_time(view.get("pubdate")),
        summary=_summary_of(view),
        duration_sec=float(duration) if duration else None,
        # 顶层 duration 是整片时长（实测等于各段之和），多段分支里被换成了分段时长，这里补回。
        total_duration_sec=_float_or_none(view.get("duration")),
        view_count=stat.get("view"),
        danmaku_count_total=stat.get("danmaku"),
        comment_count=stat.get("reply"),
        like_count=stat.get("like"),
        favorite_count=stat.get("favorite"),
        share_count=stat.get("share"),
        coin_count=stat.get("coin"),
        cover_url=view.get("pic"),
        part_count=view.get("videos"),
        category_id=view.get("tid"),
    )
    if is_multi:
        info.current_page = selected.get("page") or page
        info.current_part = selected.get("part")
    return info, aid, cid


def list_parts(view: dict[str, Any]) -> list[VideoPart]:
    """分段清单。单段视频返回一项。"""
    pages = view.get("pages") or []
    if not pages:
        return [VideoPart(page=1, part=None, duration=_float_or_none(view.get("duration")))]
    return [
        VideoPart(
            page=int(p.get("page") or index),
            part=p.get("part"),
            duration=_float_or_none(p.get("duration")),
        )
        for index, p in enumerate(pages, start=1)
    ]


def _float_or_none(value: Any) -> float | None:
    return float(value) if value else None
