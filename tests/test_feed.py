"""首页推荐流：条目过滤与映射。不联网。"""

from __future__ import annotations

import os
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from polylens_bilibili.api import _feed as feed_mod
from polylens_bilibili.api._feed import _duration_seconds, _to_feed_item, fetch_feed
from polylens_bilibili.api._signing import NavInfo
from polylens_bilibili.errors import BilibiliError


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


def _video(bvid: str = "BV1xx", **kw: Any) -> dict[str, Any]:  # noqa: D103
    base: dict[str, Any] = {
        "goto": "av", "bvid": bvid, "title": "标题",
        "owner": {"name": "up主", "mid": 42}, "pubdate": 1700000000, "duration": 225,
        "stat": {"view": 1234, "danmaku": 5},
        "rcmd_reason": {"reason_type": 0},
    }
    return base | kw


def _stub(monkeypatch: pytest.MonkeyPatch, items: Any) -> MagicMock:
    monkeypatch.setattr(feed_mod, "fetch_nav", lambda c: NavInfo("img", "sub", True))
    monkeypatch.setattr(feed_mod, "sign_params", lambda p, *a, **k: p)
    client = MagicMock()
    client.get_json.return_value = {"item": items}
    return client


# ── 条目映射 ────────────────────────────────────────────────────────────────


def test_maps_fields() -> None:
    item = _to_feed_item(_video())
    assert item is not None
    assert item.title == "标题"
    assert item.url == "https://www.bilibili.com/video/BV1xx"
    assert item.author == "up主"
    assert item.author_url == "https://space.bilibili.com/42"
    assert item.duration_sec == 225.0
    assert item.view_count == 1234
    assert item.published_at == "2023-11-15 06:13"


def test_takes_rcmd_reason_only_when_it_has_text() -> None:
    """多数条目的 rcmd_reason 只有 reason_type，没有文字。"""
    plain = _to_feed_item(_video())
    assert plain is not None and plain.rcmd_reason is None
    with_text = _to_feed_item(_video(rcmd_reason={"content": "1万点赞", "reason_type": 3}))
    assert with_text is not None and with_text.rcmd_reason == "1万点赞"


@pytest.mark.parametrize("seconds", [0, 45, 225, 3723])
def test_duration_seconds_passes_platform_value_through(seconds: int) -> None:
    assert _duration_seconds(seconds) == float(seconds)


@pytest.mark.parametrize("junk", [None, "abc", -5, ["x"]])
def test_duration_seconds_degrades_to_none(junk: Any) -> None:
    assert _duration_seconds(junk) is None


# ── 只收视频条目 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("goto", ["ad", "live"])
def test_skips_non_video_entries(goto: str) -> None:
    """广告位与直播间没有 bvid，混进来会拼出无效链接。"""
    assert _to_feed_item(_video(goto=goto, bvid="")) is None


def test_skips_video_without_bvid() -> None:
    """bvid 是唯一进链接的字段，类型不对会拼出看着能用实则无效的 URL。"""
    assert _to_feed_item(_video(bvid="")) is None
    assert _to_feed_item(_video() | {"bvid": 12345}) is None
    assert _to_feed_item("不是对象") is None


def test_fetch_feed_filters_mixed_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _stub(monkeypatch, [
        _video("BV1a"),
        {"goto": "ad", "bvid": "", "title": ""},
        _video("BV1b"),
        {"goto": "live", "bvid": "", "title": "某直播间"},
    ])
    items = fetch_feed(client, count=4)
    assert [i.url.rsplit("/", 1)[-1] for i in items] == ["BV1a", "BV1b"]


# ── 请求与容错 ──────────────────────────────────────────────────────────────


def test_sends_only_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    """带上 feed_version 平台就会往流里塞广告，只传 ps 拿到的是纯视频。"""
    client = _stub(monkeypatch, [_video()])
    fetch_feed(client, count=12)
    _endpoint, params = client.get_json.call_args[0]
    assert params == {"ps": 12}


@pytest.mark.parametrize("bad", [0, -3])
def test_rejects_non_positive_count(monkeypatch: pytest.MonkeyPatch, bad: int) -> None:
    client = _stub(monkeypatch, [])
    with pytest.raises(BilibiliError):
        fetch_feed(client, count=bad)
    client.get_json.assert_not_called()


def test_empty_batch_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    assert fetch_feed(_stub(monkeypatch, []), count=5) == []


def test_absent_item_key_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feed_mod, "fetch_nav", lambda c: NavInfo("img", "sub", True))
    monkeypatch.setattr(feed_mod, "sign_params", lambda p, *a, **k: p)
    client = MagicMock()
    client.get_json.return_value = {}
    assert fetch_feed(client, count=5) == []


@pytest.mark.parametrize("shape", ["abc", {"a": 1}, 5])
def test_non_list_item_fails_loudly(monkeypatch: pytest.MonkeyPatch, shape: Any) -> None:
    """容器一级的形状变化显式失败，不静默当成空批。"""
    with pytest.raises(BilibiliError):
        fetch_feed(_stub(monkeypatch, shape), count=5)


def test_all_malformed_batch_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """整批都是坏条目时给空表，不报错：平台给了东西，只是我们用不上。"""
    client = _stub(monkeypatch, [{"goto": "ad"}, "不是对象", {"goto": "av"}])
    assert fetch_feed(client, count=5) == []


def test_works_without_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """未登录给通用推荐，不该被拦。"""
    monkeypatch.setattr(feed_mod, "fetch_nav", lambda c: NavInfo("img", "sub", False))
    monkeypatch.setattr(feed_mod, "sign_params", lambda p, *a, **k: p)
    client = MagicMock()
    client.get_json.return_value = {"item": [_video()]}
    assert len(fetch_feed(client, count=1)) == 1


def test_rate_limit_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    from polylens_bilibili.api._http import _RateLimited

    monkeypatch.setattr(feed_mod, "fetch_nav", lambda c: NavInfo("img", "sub", True))
    monkeypatch.setattr(feed_mod, "sign_params", lambda p, *a, **k: p)
    client = MagicMock()
    client.get_json.side_effect = _RateLimited()
    with pytest.raises(_RateLimited):
        fetch_feed(client, count=5)


def test_client_passes_count_through() -> None:
    from polylens_bilibili.client import BilibiliClient

    with patch.object(feed_mod, "fetch_nav", return_value=NavInfo("i", "s", True)):
        with patch("polylens_bilibili.client.fetch_feed", return_value=[]) as spy:
            BilibiliClient().get_feed(count=7)
    assert spy.call_args.kwargs["count"] == 7
