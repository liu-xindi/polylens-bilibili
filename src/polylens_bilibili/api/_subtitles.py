"""字幕：player/wbi/v2 接口取轨道地址，再拉取 JSON 解析为 SubtitleEntry。

未登录时平台返回空轨道列表而非报错，与"这个视频没有字幕"无法区分，故取数据前判登录态。
"""

from __future__ import annotations

from typing import Any

from ..errors import AuthRequiredError
from ..models import SubtitleEntry
from ._constants import ENDPOINTS
from ._http import HttpClient
from ._signing import fetch_nav, sign_params


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


def fetch_subtitles(client: HttpClient, aid: int, cid: int) -> list[SubtitleEntry]:
    """抓取字幕，取该视频返回的第一条轨道；无字幕返回空列表。

    时间轴降精度到一位小数：字幕常达数百条，秒以下第二位对定位无用。
    """
    metas = fetch_subtitle_meta(client, aid, cid)
    if not metas:
        return []
    url = metas[0].get("subtitle_url", "")
    if not url:
        return []
    data = client.get_json_url(_normalize_url(url))
    body = (data or {}).get("body") or []
    return [
        SubtitleEntry(
            start=round(float(item.get("from", 0)), 1),
            end=round(float(item.get("to", 0)), 1),
            content=str(item.get("content", "")),
        )
        for item in body
    ]
