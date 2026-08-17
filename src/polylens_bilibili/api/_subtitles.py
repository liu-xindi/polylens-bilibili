"""字幕：player/wbi/v2 接口取轨道地址，再拉取 JSON 解析为 SubtitleEntry。

未登录时平台返回空轨道列表而非报错，与"这个视频没有字幕"无法区分，故取数据前判登录态。
"""

from __future__ import annotations

from typing import Any, NamedTuple

from ..errors import AuthRequiredError, BilibiliError
from ..models import SubtitleEntry
from ._constants import ENDPOINTS
from ._http import HttpClient
from ._signing import fetch_nav, sign_params


class SubtitleTrack(NamedTuple):
    entries: list[SubtitleEntry]
    lang: str | None  # 本次实际取的轨道语种；无字幕时为 None
    available_langs: list[str]  # 该段全部可选轨道


def _normalize_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    return url


def fetch_subtitle_meta(client: HttpClient, aid: int, cid: int) -> list[dict[str, Any]]:
    """从 player/wbi/v2 获取字幕元数据列表（该视频无字幕时为空列表）。"""
    nav = fetch_nav(client)
    if not nav.is_login:
        raise AuthRequiredError("subtitles")
    params = sign_params(
        {"aid": aid, "cid": cid, "isGaiaAvoided": 0, "web_location": 1315873},
        nav.img_key,
        nav.sub_key,
    )
    data = client.get_json(ENDPOINTS["player_v2"], params)
    subtitle_info = (data or {}).get("subtitle") or {}
    return subtitle_info.get("subtitles") or []


def _pick_track(metas: list[dict[str, Any]], lang: str | None) -> dict[str, Any]:
    """选轨道。不指定语种时取平台给的第一条。

    同一稿件各段的轨道构成可能不同（实测某合集 p1 是 en-US + ai-zh，p64 只有 ai-zh），
    所以"第一条"是哪个语种随段而变，调用方要确定语种就得显式传 lang。
    """
    if lang is None:
        return metas[0]
    for meta in metas:
        if meta.get("lan") == lang:
            return meta
    available = "、".join(str(m.get("lan")) for m in metas)
    raise BilibiliError(f"该段没有 {lang} 字幕，可选：{available}")


def fetch_subtitles(
    client: HttpClient, aid: int, cid: int, lang: str | None = None
) -> SubtitleTrack:
    """抓取字幕。无字幕返回空轨道。

    时间轴降精度到一位小数：字幕常达数百条，秒以下第二位对定位无用。
    """
    metas = fetch_subtitle_meta(client, aid, cid)
    available = [str(m.get("lan")) for m in metas if m.get("lan")]
    if not metas:
        return SubtitleTrack([], None, available)
    meta = _pick_track(metas, lang)
    url = meta.get("subtitle_url", "")
    if not url:
        # 有轨道却没有地址：与"这个视频没有字幕"是两回事，静默返回空会把它们混为一谈。
        raise BilibiliError(f"字幕轨 {meta.get('lan')} 没有可下载的地址")
    data = client.get_json_url(_normalize_url(url))
    body = (data or {}).get("body") or []
    entries = [
        SubtitleEntry(
            start=round(float(item.get("from", 0)), 1),
            end=round(float(item.get("to", 0)), 1),
            content=str(item.get("content", "")),
        )
        for item in body
    ]
    return SubtitleTrack(entries, str(meta.get("lan")) if meta.get("lan") else None, available)
