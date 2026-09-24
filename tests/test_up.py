"""UP 主资料：参数与字段映射。不联网。"""

from __future__ import annotations

from typing import Any

import pytest

from polylens_bilibili.api._http import _RateLimited
from polylens_bilibili.api._up import fetch_up_info
from polylens_bilibili.errors import BilibiliError, RateLimitedError

MID = 546195


def _card(**over: Any) -> dict[str, Any]:
    card = {
        "mid": str(MID), "name": "老番茄", "sex": "男", "sign": "天天开心",
        "face": "https://i0.hdslb.com/face.jpg", "fans": 100, "attention": 52,
        "level_info": {"current_level": 6},
        "Official": {"role": 1, "title": "2025百大UP主", "desc": "", "type": 0},
        "vip": {"type": 2, "status": 1, "label": {"text": "年度大会员"}},
    }
    card.update(over)
    return {"card": card, "follower": 100, "archive_count": 497, "article_count": 3,
            "like_num": 198533}


class _Client:
    def __init__(self, data: Any = None, exc: Exception | None = None) -> None:
        self.data = data
        self.exc = exc
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def get_json(self, path: str, params: dict[str, Any], **kw: Any) -> Any:
        self.calls.append((path, params, kw))
        if self.exc:
            raise self.exc
        return self.data


def _fetch(client: _Client):
    return fetch_up_info(client, MID)  # type: ignore[arg-type]


def test_maps_fields() -> None:
    client = _Client(_card())
    info = _fetch(client)
    path, params, kw = client.calls[0]
    assert path == "/x/web-interface/card"
    assert params == {"mid": MID}
    assert kw["allow_codes"] == {-404}
    assert info.model_dump() == {
        "mid": MID, "author": "老番茄", "author_url": f"https://space.bilibili.com/{MID}",
        "sign": "天天开心", "sex": "男", "level": 6, "follower_count": 100,
        "following_count": 52, "video_count": 497, "article_count": 3, "like_count": 198533,
        "official_type": "个人认证", "official_title": "2025百大UP主", "vip": "年度大会员",
        "face_url": "https://i0.hdslb.com/face.jpg",
    }


def test_unverified_and_expired_vip_are_null() -> None:
    info = _fetch(_Client(_card(
        sign="",
        Official={"role": 0, "title": "", "desc": "", "type": -1},
        vip={"type": 1, "status": 0, "label": {"text": ""}},
    )))
    assert info.sign is None
    assert info.official_type is None and info.official_title is None
    assert info.vip is None


def test_vip_falls_back_to_type_without_label() -> None:
    info = _fetch(_Client(_card(vip={"type": 1, "status": 1, "label": {"text": ""}})))
    assert info.vip == "大会员"


def test_organization_verification() -> None:
    info = _fetch(_Client(_card(Official={"role": 3, "title": "某机构", "desc": "", "type": 1})))
    assert info.official_type == "机构认证"


def test_missing_mid_raises() -> None:
    with pytest.raises(BilibiliError, match="UP 主不存在"):
        _fetch(_Client(None))


def test_rate_limited_is_translated() -> None:
    with pytest.raises(RateLimitedError):
        _fetch(_Client(exc=_RateLimited()))
