"""UP 主资料：web-interface/card，即网页上悬停头像弹出的名片，映射为 UpInfo。

浏览器会带 photo 与一串埋点参数，实测只传 mid 就能取到同样的结果，不需 WBI 签名，未登录也行。
mid 不存在时平台回 code -404。总播放数这里没有，只有空间页的 upstat 接口给。
"""

from __future__ import annotations

from typing import Any

from ..errors import BilibiliError, RateLimitedError
from ..models import UpInfo, space_url
from ._constants import ENDPOINTS
from ._http import HttpClient, _RateLimited
from ._search import _int_or_none

_NOT_FOUND = -404
_OFFICIAL_TYPES = {0: "个人认证", 1: "机构认证"}
_VIP_TYPES = {1: "大会员", 2: "年度大会员"}


def _text(value: Any) -> str | None:
    return str(value) if value else None


def _vip(raw: dict[str, Any]) -> str | None:
    """生效中的大会员档位；过期或从未开通为 None。

    type 在过期后仍保留原档位，只看 status 才知道是否生效。
    """
    if raw.get("status") != 1:
        return None
    label = (raw.get("label") or {}).get("text")
    return label or _VIP_TYPES.get(raw.get("type"))  # type: ignore[arg-type]


def _to_info(mid: int, data: dict[str, Any]) -> UpInfo:
    card = data.get("card") or {}
    official = card.get("Official") or {}
    return UpInfo(
        mid=mid,
        author=_text(card.get("name")),
        author_url=space_url(mid),
        sign=_text(card.get("sign")),
        sex=_text(card.get("sex")),
        level=_int_or_none((card.get("level_info") or {}).get("current_level")),
        follower_count=_int_or_none(data.get("follower")),
        following_count=_int_or_none(card.get("attention")),
        video_count=_int_or_none(data.get("archive_count")),
        article_count=_int_or_none(data.get("article_count")),
        like_count=_int_or_none(data.get("like_num")),
        official_type=_OFFICIAL_TYPES.get(official.get("type")),  # type: ignore[arg-type]
        official_title=_text(official.get("title")),
        vip=_vip(card.get("vip") or {}),
        face_url=_text(card.get("face")),
    )


def fetch_up_info(client: HttpClient, mid: int) -> UpInfo:
    try:
        data = client.get_json(ENDPOINTS["up_card"], {"mid": mid}, allow_codes={_NOT_FOUND})
    except _RateLimited:
        raise RateLimitedError("取 UP 主资料触发风控，稍后重试。") from None
    if data is None:
        raise BilibiliError(f"UP 主不存在：mid {mid}")
    return _to_info(mid, data)
