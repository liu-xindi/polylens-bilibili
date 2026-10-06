"""UP 主投稿列表：参数、字段映射、偏移量游标分页。不联网。"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest

from polylens_bilibili.api import _space as space_mod
from polylens_bilibili.api._http import _RateLimited
from polylens_bilibili.api._signing import NavInfo
from polylens_bilibili.errors import AuthRequiredError, BilibiliError, RateLimitedError

MID = 3690981465524933


@pytest.fixture(autouse=True)
def _pin_timezone():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def _raw(index: int) -> dict[str, Any]:
    return {
        "title": f"t{index}", "bvid": f"BV{index}", "created": 1700000000, "length": "16:07",
        "play": 468982, "video_review": 641, "comment": 642, "author": "老何", "mid": MID,
    }


class _Client:
    def __init__(self, vlist: list[dict[str, Any]], total: int) -> None:
        self.vlist = vlist
        self.total = total
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def get_json(self, path: str, params: dict[str, Any], **kw: Any) -> Any:
        self.calls.append((params, kw))
        return {"list": {"vlist": self.vlist}, "page": {"pn": params["pn"], "ps": params["ps"],
                                                        "count": self.total}}


@pytest.fixture(autouse=True)
def _no_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(space_mod, "fetch_nav", lambda c: NavInfo("img", "sub", True))
    monkeypatch.setattr(space_mod, "sign_params", lambda p, *a, **k: p)


def _fetch(client: _Client, **kw: Any):
    return space_mod.fetch_up_videos(client, MID, **kw)  # type: ignore[arg-type]


def test_maps_fields_and_author() -> None:
    author, total, page = _fetch(_Client([_raw(1)], total=75))
    assert author == "老何"
    assert total == 75
    item = page.items[0]
    assert item.title == "t1"
    assert item.url == "https://www.bilibili.com/video/BV1"
    assert item.published_at == "2023-11-15 06:13"
    assert item.duration_sec == 967.0
    assert (item.view_count, item.danmaku_count, item.comment_count) == (468982, 641, 642)


def test_author_ignores_union_videos_by_others() -> None:
    """联合投稿的署名是别人，昵称要从本人投稿里取。"""
    union = {**_raw(0), "author": "特效小哥studio", "mid": 3066511}
    own = {**_raw(1), "mid": MID}
    author, _, _ = _fetch(_Client([union, own], total=2))
    assert author == "老何"


def test_page_without_own_videos_has_no_author() -> None:
    union = {**_raw(0), "author": "特效小哥studio", "mid": 3066511}
    author, _, page = _fetch(_Client([union], total=1))
    assert author is None and len(page.items) == 1


def test_no_videos_means_no_author() -> None:
    author, _, page = _fetch(_Client([], total=0))
    assert author is None
    assert page.items == [] and not page.has_more


@pytest.mark.parametrize(
    ("order", "platform"),
    [("newest", "pubdate"), ("most_viewed", "click"), ("most_favorited", "stow")],
)
def test_order_maps_to_platform_value(order: str, platform: str) -> None:
    client = _Client([], total=0)
    _fetch(client, order=order)
    params, kw = client.calls[0]
    assert params["order"] == platform
    assert params["mid"] == MID
    assert params["ps"] == 40
    assert kw["referer"] == f"https://space.bilibili.com/{MID}/upload/video"


def test_keyword_sent_only_when_given() -> None:
    client = _Client([], total=0)
    _fetch(client)
    _fetch(client, keyword=" 牛顿 ")
    assert "keyword" not in client.calls[0][0]
    assert client.calls[1][0]["keyword"] == "牛顿"


def test_cursor_counts_pages_and_stops_at_total() -> None:
    client = _Client([_raw(i) for i in range(40)], total=75)
    _, _, first = _fetch(client)
    assert client.calls[0][0]["pn"] == 1
    assert first.has_more and first.next_cursor == "1"
    client.vlist = [_raw(i) for i in range(40, 75)]
    _, _, last = _fetch(client, cursor=first.next_cursor)
    assert client.calls[1][0]["pn"] == 2
    assert not last.has_more and last.next_cursor is None


def test_requires_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """未登录时平台常回 412，按风控报错会让调用方等待恢复，故先拦下，不发请求。"""
    monkeypatch.setattr(space_mod, "fetch_nav", lambda c: NavInfo("img", "sub", False))
    client = _Client([_raw(0)], total=1)
    with pytest.raises(AuthRequiredError):
        _fetch(client)
    assert client.calls == []


def test_rate_limit_becomes_error() -> None:
    class _Limited(_Client):
        def get_json(self, *a: Any, **kw: Any) -> Any:
            raise _RateLimited("-352", "/x")

    with pytest.raises(RateLimitedError):
        _fetch(_Limited([], total=0))


def test_unparsable_cursor_rejected() -> None:
    client = _Client([], total=0)
    with pytest.raises(BilibiliError):
        _fetch(client, cursor="abc")
    assert client.calls == []
