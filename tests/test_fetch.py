"""抓取函数测试：mock HttpClient，不联网。"""

from __future__ import annotations

import http.client
import io
import json
import urllib.error
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from polylens_bilibili.api._comments import fetch_comments, fetch_replies
from polylens_bilibili.api._constants import USER_AGENT
from polylens_bilibili.api._danmaku import fetch_danmaku, top_by_heat
from polylens_bilibili.api._http import BilibiliHttpError, HttpClient, _RateLimited
from polylens_bilibili.api._signing import NavInfo
from polylens_bilibili.api._subtitles import fetch_subtitles
from polylens_bilibili.errors import AuthRequiredError, BilibiliError, RateLimitedError

# ── 共用辅助 ────────────────────────────────────────────────────────────────

_PLATFORM_PAGE = 20  # 平台的主评论单页条数，测试里用来拼"满页"

_PLATFORM_CURSOR = "CAESEDE4MzQyNzYxNTEwNTEyMzAaADIECPnBAQ=="  # 实测平台发出的游标

_LOGGED_IN = NavInfo("imgkey", "subkey", True)
_ANONYMOUS = NavInfo("imgkey", "subkey", False)


def _nav_patch(module: str, nav: NavInfo = _LOGGED_IN):
    return patch(f"polylens_bilibili.api.{module}.fetch_nav", return_value=nav)


_SIGN_PATCH = patch(
    "polylens_bilibili.api._comments.sign_params", side_effect=lambda p, *a, **kw: p
)
_SIGN_PATCH_SUB = patch(
    "polylens_bilibili.api._subtitles.sign_params", side_effect=lambda p, *a, **kw: p
)
_SLEEP_PATCH = patch("polylens_bilibili.api._comments.time.sleep")


def _reply(rpid: int, content: str, *, parent: int = 0, count: int = 0, mid: int = 1) -> dict:
    return {
        "rpid": rpid, "parent": parent,
        "member": {"uname": "u", "mid": str(mid)},
        "content": {"message": content},
        "like": 0, "count": count, "ctime": 0,
    }


def _page(replies, *, is_end: bool = True, next_offset: str = "", top_replies=None) -> dict:
    return {
        "replies": replies,
        "top_replies": top_replies or [],
        "cursor": {"is_end": is_end, "pagination_reply": {"next_offset": next_offset}},
    }


# ── fetch_comments ──────────────────────────────────────────────────────────


def test_fetch_comments_top_replies_inserted_first():
    client = MagicMock()
    client.get_json.return_value = _page([_reply(2, "normal")], top_replies=[_reply(1, "top")])
    with _nav_patch("_comments"), _SIGN_PATCH:
        page = fetch_comments(client, aid=100, count=20)
    assert [c.content for c in page.items] == ["top", "normal"]
    assert page.has_more is False and page.next_cursor is None  # 单页 is_end → 抓全


def test_fetch_comments_marks_up_from_upper():
    client = MagicMock()
    client.get_json.return_value = _page(
        [_reply(2, "up", mid=42), _reply(3, "fan")], top_replies=[_reply(1, "top", mid=42)]
    ) | {"upper": {"mid": 42}}
    with _nav_patch("_comments"), _SIGN_PATCH:
        page = fetch_comments(client, aid=100, count=20)
    assert [c.is_up for c in page.items] == [True, True, False]


def test_fetch_comments_empty_replies_returns_empty():
    client = MagicMock()
    client.get_json.return_value = _page([])
    with _nav_patch("_comments"), _SIGN_PATCH:
        page = fetch_comments(client, aid=100, count=20)
    assert page.items == []
    assert page.has_more is False and page.next_cursor is None


def _full_page(seq: int, *, is_end: bool = False) -> dict:
    """一整页主评论（平台单页 20 条）。"""
    return _page(
        [_reply(seq * 100 + i, f"r{seq}-{i}") for i in range(_PLATFORM_PAGE)],
        is_end=is_end, next_offset=f"offset{seq}",
    )


def test_fetch_comments_count_truncates_with_next_cursor():
    """每页满 20 条、非 is_end；count=25 → 翻 2 页够数，has_more=True + next_cursor。"""
    call_count = 0

    def _get_json(endpoint, params):
        nonlocal call_count
        call_count += 1
        return _full_page(call_count)

    client = MagicMock()
    client.get_json.side_effect = _get_json
    with _nav_patch("_comments"), _SIGN_PATCH, _SLEEP_PATCH:
        page = fetch_comments(client, aid=100, count=25)
    assert call_count == 2
    assert len(page.items) == 40
    assert page.has_more is True and page.next_cursor == "offset2"


def test_fetch_comments_short_page_is_not_the_end():
    """不满一页不等于到底：置顶评论占掉首页一个名额，平台只给 19 条常规评论。

    按"不满 20 即到底"推断会在第一页就停下并报已抓全，把上千条评论截成 20 条。
    """
    pages = [
        _page(
            [_reply(i + 1, f"r{i}") for i in range(19)],
            is_end=False, next_offset="offset1", top_replies=[_reply(999, "top")],
        ),
        _page([_reply(100 + i, f"s{i}") for i in range(20)], is_end=False, next_offset="offset2"),
    ]
    client = MagicMock()
    client.get_json.side_effect = lambda endpoint, params: pages[client.get_json.call_count - 1]
    with _nav_patch("_comments"), _SIGN_PATCH, _SLEEP_PATCH:
        page = fetch_comments(client, aid=100, count=35)
    assert client.get_json.call_count == 2  # 首页的 19 条没被当成末页
    assert len(page.items) == 40  # 置顶 1 + 首页 19 + 次页 20
    assert page.has_more is True and page.next_cursor == "offset2"


def test_fetch_comments_cursor_never_points_at_a_consumed_page():
    """游标必须指向还没取回的那页。

    先推进游标再抓下一页的写法，会在下一页触发终止时把游标留在已经取回的那页上，
    调用方续取时重复拿到同一批。
    """
    pages = [
        _full_page(1),  # 满页、非到底，给出 offset1
        _page([_reply(999, "last")], is_end=True, next_offset="offset2"),  # offset1 这页
    ]
    client = MagicMock()
    client.get_json.side_effect = lambda endpoint, params: pages[client.get_json.call_count - 1]
    with _nav_patch("_comments"), _SIGN_PATCH, _SLEEP_PATCH:
        page = fetch_comments(client, aid=100, count=25)
    assert [c.content for c in page.items][-1] == "last"  # 第二页已经收进结果
    assert page.has_more is False and page.next_cursor is None


def test_fetch_comments_empty_page_after_a_full_one_ends_cleanly():
    """热度序的末页谎报 is_end=false 并给游标，下一页才空；空页即到底，不留游标。"""
    pages = [_full_page(1), _page([], is_end=False, next_offset="offset2")]
    client = MagicMock()
    client.get_json.side_effect = lambda endpoint, params: pages[client.get_json.call_count - 1]
    with _nav_patch("_comments"), _SIGN_PATCH, _SLEEP_PATCH:
        page = fetch_comments(client, aid=100, count=25)
    assert len(page.items) == 20
    assert page.has_more is False and page.next_cursor is None


@pytest.mark.parametrize("bad", [0, -3])
def test_fetch_comments_rejects_non_positive_count(bad: int):
    client = MagicMock()
    with _nav_patch("_comments"), _SIGN_PATCH:
        with pytest.raises(BilibiliError):
            fetch_comments(client, aid=100, count=bad)
    client.get_json.assert_not_called()


@pytest.mark.parametrize("bad", [0, -3])
def test_fetch_replies_rejects_non_positive_limit(bad: int):
    client = MagicMock()
    with _nav_patch("_comments"), _SLEEP_PATCH:
        with pytest.raises(BilibiliError):
            fetch_replies(client, 100, ["1"], limit=bad)
    client.get_json.assert_not_called()


def test_fetch_comments_cursor_skips_top_replies():
    """传 cursor（续取）时不插入置顶，置顶只在从头的第一页。"""
    client = MagicMock()
    client.get_json.return_value = _page(
        [_reply(1, "r")], is_end=True, top_replies=[_reply(9, "top")]
    )
    with _nav_patch("_comments"), _SIGN_PATCH:
        page = fetch_comments(client, aid=100, cursor=_PLATFORM_CURSOR, count=20)
    assert [c.content for c in page.items] == ["r"]


def test_fetch_comments_stops_on_is_end():
    client = MagicMock()
    client.get_json.return_value = _page([_reply(1, "r")], is_end=True)
    with _nav_patch("_comments"), _SIGN_PATCH:
        page = fetch_comments(client, aid=100, count=20)
    assert client.get_json.call_count == 1
    assert page.has_more is False and page.next_cursor is None


def test_fetch_comments_rate_limited_midway_returns_partial():
    """热度序下已取回的页平台已记为取过，丢掉就再也取不回，故带着游标返回。"""
    client = MagicMock()
    client.get_json.side_effect = [
        _page([_reply(1, "a")], is_end=False, next_offset="SESSION"),
        _RateLimited(),
    ]
    with _nav_patch("_comments"), _SIGN_PATCH, _SLEEP_PATCH:
        page = fetch_comments(client, aid=100, count=40)
    assert [c.content for c in page.items] == ["a"]
    assert page.rate_limited is True
    assert page.has_more is True and page.next_cursor == "SESSION"


def test_fetch_comments_requires_login():
    """未登录时平台只给几条并声称到底，故取数据前就拦住。"""
    client = MagicMock()
    with _nav_patch("_comments", _ANONYMOUS), _SIGN_PATCH:
        with pytest.raises(AuthRequiredError):
            fetch_comments(client, aid=100, count=20)
    client.get_json.assert_not_called()


# ── fetch_replies ───────────────────────────────────────────────────────────


def _sub_page(endpoint, params):
    ps, pn = params["ps"], params["pn"]
    base = (pn - 1) * ps
    return {"replies": [_reply(base + i + 1, f"r{base + i}") for i in range(ps)]}


def test_fetch_replies_marks_up_from_upper():
    client = MagicMock()
    client.get_json.return_value = {
        "replies": [_reply(1, "up", mid=42), _reply(2, "fan")], "upper": {"mid": 42},
    }
    with _nav_patch("_comments"), _SLEEP_PATCH:
        out = fetch_replies(client, 100, ["555"])
    assert [c.is_up for c in out[0].page.items] == [True, False]


def test_fetch_replies_slices_window_by_limit():
    client = MagicMock()
    client.get_json.side_effect = _sub_page
    with _nav_patch("_comments"), _SLEEP_PATCH:
        out = fetch_replies(client, 100, ["555"], limit=5)
    t = out[0]
    assert t.comment_id == "555"
    assert len(t.page.items) == 5
    assert t.page.has_more is True  # 满页还有更多


def test_fetch_replies_limit_beyond_page_size_accumulates():
    """limit=50 而平台单页 20 → 内部翻 3 页凑满，窗口精确。"""
    seen_pn: list[int] = []

    def _get_json(endpoint, params):
        seen_pn.append(params["pn"])
        return _sub_page(endpoint, params)

    client = MagicMock()
    client.get_json.side_effect = _get_json
    with _nav_patch("_comments"), _SLEEP_PATCH:
        out = fetch_replies(client, 100, ["555"], limit=50)
    t = out[0]
    assert len(t.page.items) == 50
    assert seen_pn == [1, 2, 3]
    assert t.page.items[0].content == "r0" and t.page.items[49].content == "r49"
    assert t.page.has_more is True


def test_fetch_replies_without_limit_reads_whole_thread():
    seen_pn: list[int] = []
    sized = _make_sized_sub_page({555: 45})

    def _get_json(endpoint, params):
        seen_pn.append(params["pn"])
        return sized(endpoint, params)

    client = MagicMock()
    client.get_json.side_effect = _get_json
    with _nav_patch("_comments"), _SLEEP_PATCH:
        out = fetch_replies(client, 100, ["555"])
    t = out[0]
    assert seen_pn == [1, 2, 3]
    assert len(t.page.items) == 45
    assert t.page.items[0].content == "r555-0" and t.page.items[-1].content == "r555-44"
    assert t.page.has_more is False


def _make_sized_sub_page(sizes: dict[int, int]):
    """按 root（=comment_id）给不同楼不同总回复数；rpid = root*1000+下标，跨楼唯一。"""

    def _get(endpoint, params):
        root, ps, pn = params["root"], params["ps"], params["pn"]
        start = (pn - 1) * ps
        page = [
            _reply(root * 1000 + start + i, f"r{root}-{start + i}")
            for i in range(ps)
            if start + i < sizes[root]
        ]
        return {"replies": page}

    return _get


def test_fetch_replies_limit_applies_per_thread():
    """limit 对每个楼各自生效：没取完的楼标 has_more，一次到底的不标。"""
    client = MagicMock()
    client.get_json.side_effect = _make_sized_sub_page({1: 8, 2: 12, 3: 3})
    with _nav_patch("_comments"), _SLEEP_PATCH:
        out = fetch_replies(client, 100, ["1", "2", "3"], limit=5)
    by_id = {t.comment_id: t for t in out}
    assert [len(by_id[c].page.items) for c in ("1", "2", "3")] == [5, 5, 3]
    assert by_id["1"].page.has_more and by_id["2"].page.has_more
    assert by_id["3"].page.has_more is False


def test_fetch_replies_reports_withheld_count():
    """平台声称的回复数多于它肯列出的条数，差额单独报出来。

    差掉的那些被删除或折叠，翻到底也取不到，却仍会被其他回复用 parent_id 指到。
    """
    client = MagicMock()
    client.get_json.return_value = {
        "replies": [_reply(1, "r")],
        "root": {"count": 11},
        "page": {"num": 1, "size": 20, "count": 10},
    }
    with _nav_patch("_comments"), _SLEEP_PATCH:
        out = fetch_replies(client, 100, ["555"], limit=5)
    assert out[0].withheld == 1


@pytest.mark.parametrize(
    "shape",
    [
        {},                                              # 两个计数都没有
        {"root": {"count": 5}},                          # 只有声称数
        {"page": {"count": 5}},                          # 只有可列出数
        {"root": {"count": 3}, "page": {"count": 9}},    # 声称的反而更少
        {"root": {"count": "3"}, "page": {"count": 9}},  # 类型不对
    ],
)
def test_fetch_replies_withheld_degrades_to_zero(shape: dict):
    client = MagicMock()
    client.get_json.return_value = {"replies": [_reply(1, "r")], **shape}
    with _nav_patch("_comments"), _SLEEP_PATCH:
        out = fetch_replies(client, 100, ["555"], limit=5)
    assert out[0].withheld == 0


def test_fetch_replies_requires_login():
    client = MagicMock()
    with _nav_patch("_comments", _ANONYMOUS), _SLEEP_PATCH:
        with pytest.raises(AuthRequiredError):
            fetch_replies(client, 100, ["1"], limit=5)
    client.get_json.assert_not_called()


@pytest.mark.parametrize("bad", ["not-a-cursor", "TOKEN", "===="])
def test_fetch_comments_rejects_unparsable_cursor(bad: str):
    """平台不校验游标，乱写的会被当成从头开始，只能在本地拦。"""
    client = MagicMock()
    with _nav_patch("_comments"), _SIGN_PATCH:
        with pytest.raises(BilibiliError, match="无法识别的续取游标"):
            fetch_comments(client, aid=100, cursor=bad, count=20)
    client.get_json.assert_not_called()


def test_fetch_replies_rejects_non_numeric_ids():
    client = MagicMock()
    with _nav_patch("_comments"), _SLEEP_PATCH:
        with pytest.raises(BilibiliError, match="comment_ids 需为数字 id，收到 abc"):
            fetch_replies(client, 100, ["1", "abc"])
    client.get_json.assert_not_called()


def test_fetch_replies_rejects_comment_of_another_video():
    """平台按 root 定位楼层，不校验 oid；响应里 root.oid 才是评论真正所属的视频。"""
    client = MagicMock()
    client.get_json.return_value = {"replies": [_reply(1, "r")], "root": {"oid": 999}}
    with _nav_patch("_comments"), _SLEEP_PATCH:
        out = fetch_replies(client, 100, ["555"])
    assert out[0].page.items == []
    assert out[0].error is not None and "不属于这个视频" in out[0].error


def test_fetch_replies_isolates_failing_thread():
    def _get(endpoint, params):
        if params["root"] == 1:
            raise BilibiliHttpError("接口返回失败: 12006 没有该评论")
        return {"replies": [_reply(7, "ok")], "root": {"oid": 100}}

    client = MagicMock()
    client.get_json.side_effect = _get
    with _nav_patch("_comments"), _SLEEP_PATCH:
        out = fetch_replies(client, 100, ["555", "1"])
    assert [t.comment_id for t in out] == ["555", "1"]
    assert [c.content for c in out[0].page.items] == ["ok"] and out[0].error is None
    assert out[1].page.items == [] and out[1].error is not None and "12006" in out[1].error


def test_fetch_replies_rate_limited_raises():
    client = MagicMock()
    client.get_json.side_effect = _RateLimited()
    with _nav_patch("_comments"), _SLEEP_PATCH:
        with pytest.raises(RateLimitedError):
            fetch_replies(client, 100, ["1"], limit=5)


# ── fetch_subtitles ─────────────────────────────────────────────────────────

_SUBTITLE_BODY = {
    "body": [
        {"from": 1.04, "to": 3.57, "content": "first line"},
        {"from": 4.0, "to": 6.0, "content": "second line"},
    ]
}

_SUBTITLE_META = [
    {"lan": "zh-CN", "subtitle_url": "//api.bilibili.com/sub_zh.json"},
    {"lan": "en", "subtitle_url": "https://api.bilibili.com/sub_en.json"},
]


def _subtitle_client(tracks: list[dict]) -> MagicMock:
    client = MagicMock()
    client.get_json.return_value = {"subtitle": {"subtitles": tracks}}
    client.get_json_url.return_value = _SUBTITLE_BODY
    return client


def test_fetch_subtitles_returns_entries_with_rounded_timeline():
    client = _subtitle_client([_SUBTITLE_META[0]])
    with _nav_patch("_subtitles"), _SIGN_PATCH_SUB:
        track = fetch_subtitles(client, aid=100, cid=200)
    assert len(track.entries) == 2
    assert track.entries[0].content == "first line"
    assert track.entries[0].start == 1.0 and track.entries[0].end == 3.6  # 降精度到一位小数
    assert track.lang == "zh-CN"
    assert track.available_langs == ["zh-CN"]


def test_fetch_subtitles_protocol_relative_url_fixed():
    client = _subtitle_client([_SUBTITLE_META[0]])
    with _nav_patch("_subtitles"), _SIGN_PATCH_SUB:
        fetch_subtitles(client, aid=100, cid=200)
    assert client.get_json_url.call_args[0][0].startswith("https://")


def test_fetch_subtitles_empty_when_no_tracks():
    client = _subtitle_client([])
    with _nav_patch("_subtitles"), _SIGN_PATCH_SUB:
        track = fetch_subtitles(client, aid=100, cid=200)
    assert track.entries == [] and track.lang is None and track.available_langs == []


def test_fetch_subtitles_requires_login():
    """未登录时平台返回空轨道，与"这个视频没字幕"无法区分，故先判登录态。"""
    client = _subtitle_client([_SUBTITLE_META[0]])
    with _nav_patch("_subtitles", _ANONYMOUS), _SIGN_PATCH_SUB:
        with pytest.raises(AuthRequiredError):
            fetch_subtitles(client, aid=100, cid=200)
    client.get_json.assert_not_called()


# ── fetch_danmaku ───────────────────────────────────────────────────

_DANMAKU_XML = """<?xml version="1.0" encoding="UTF-8"?>
<i>
  <d p="3.0,1,25,16777215,1700000000,0,aaa,11111,5">bullet A</d>
  <d p="1.0,5,25,255,1700000001,0,bbb,22222,8">bullet B</d>
</i>"""


def test_fetch_danmaku_delegates_to_parse():
    client = MagicMock()
    client.get_bytes.return_value = _DANMAKU_XML.encode()
    bullets = fetch_danmaku(client, cid=12345)
    assert len(bullets) == 2
    assert bullets[0].timestamp == 1.0  # 已排序


def test_fetch_danmaku_url_includes_cid():
    client = MagicMock()
    client.get_bytes.return_value = b"<i></i>"
    fetch_danmaku(client, cid=9999)
    assert "oid=9999" in client.get_bytes.call_args[0][0]


# ── 弹幕取样 ────────────────────────────────────────────────────────────────


def _dm(ts: float, heat: int):
    from polylens_bilibili.models import Danmaku

    return Danmaku(content=f"d{ts:g}", timestamp=ts, heat=heat)


def test_top_by_heat_spreads_over_time_within_one_tier() -> None:
    """heat 只有十档且每档条数相等，小 count 会整批落在最高档。

    同档之间无从比较，取最早的几条会让长视频的取样全挤在开头。
    """
    pool = [_dm(float(i), 10) for i in range(100)]
    got = top_by_heat(pool, 5)
    stamps = [b.timestamp for b in got]
    assert stamps == sorted(stamps)
    assert stamps[0] == 0.0 and stamps[-1] == 99.0  # 首尾都取到
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert max(gaps) - min(gaps) <= 1  # 间隔基本均匀


def test_top_by_heat_prefers_higher_tiers_before_spreading() -> None:
    """先取满整档，不满的那一档才等距挑。"""
    pool = [_dm(float(i), 10) for i in range(3)] + [_dm(float(10 + i), 5) for i in range(20)]
    got = top_by_heat(pool, 6)
    assert sum(1 for b in got if b.heat == 10) == 3  # 高档全要
    assert sum(1 for b in got if b.heat == 5) == 3


def test_top_by_heat_is_reproducible() -> None:
    """同一输入两次调用结果相同：随机抽样会破坏这一点。"""
    pool = [_dm(float(i), 10) for i in range(50)]
    assert [b.timestamp for b in top_by_heat(pool, 7)] == [
        b.timestamp for b in top_by_heat(pool, 7)
    ]


@pytest.mark.parametrize("count", [1, 2, 7, 50, 51, 200])
def test_top_by_heat_shape_holds(count: int) -> None:
    pool = [_dm(float(i), (i % 10) + 1) for i in range(50)]
    got = top_by_heat(pool, count)
    assert len(got) == min(count, len(pool))
    assert len({id(b) for b in got}) == len(got)                       # 无重复
    assert [b.timestamp for b in got] == sorted(b.timestamp for b in got)


# ── HttpClient 的风控判定 ───────────────────────────────────────────────────


def _http() -> HttpClient:
    return HttpClient()


def test_get_json_raises_rate_limited_on_412():
    client = _http()
    exc = urllib.error.HTTPError(url="", code=412, msg="", hdrs=http.client.HTTPMessage(), fp=None)
    with patch.object(client, "get_bytes", side_effect=exc), pytest.raises(_RateLimited):
        client.get_json("/test")


@pytest.mark.parametrize("code", [-352, -509])
def test_get_json_raises_rate_limited_on_risk_codes(code: int):
    client = _http()
    raw = json.dumps({"code": code, "message": "风控"}).encode()
    with patch.object(client, "get_bytes", return_value=raw), pytest.raises(_RateLimited):
        client.get_json("/test")


def test_get_json_raises_rate_limited_on_voucher_only_data():
    """code=0 但 data 里只有 v_voucher 挑战票据：这是风控，不是"没有数据"。

    实测搜索接口在此形态下 result 缺失、numResults 也没有；判成空结果会让翻页静默断掉。
    """
    client = _http()
    raw = json.dumps({"code": 0, "data": {"v_voucher": "voucher_abc"}}).encode()
    with patch.object(client, "get_bytes", return_value=raw), pytest.raises(_RateLimited):
        client.get_json("/test")


def test_get_json_keeps_data_when_voucher_accompanies_real_fields():
    """v_voucher 与业务字段同时出现时不算风控，照常返回。"""
    client = _http()
    raw = json.dumps({"code": 0, "data": {"v_voucher": "x", "result": [{"bvid": "BV1"}]}}).encode()
    with patch.object(client, "get_bytes", return_value=raw):
        assert client.get_json("/test")["result"] == [{"bvid": "BV1"}]


def test_get_json_business_error_is_polylens_error():
    """平台非 0 业务码上浮成 BilibiliError，消息用平台原文，能被工具层接住。"""
    client = _http()
    raw = json.dumps({"code": -400, "message": "请求错误"}).encode()
    with patch.object(client, "get_bytes", return_value=raw):
        with pytest.raises(BilibiliHttpError) as exc_info:
            client.get_json("/test")
    assert isinstance(exc_info.value, BilibiliError)


def test_get_json_allow_codes_passes_through():
    """白名单里的码不当错误（未登录的 -101 走这条）。"""
    client = _http()
    raw = json.dumps({"code": -101, "message": "未登录", "data": {"isLogin": False}}).encode()
    with patch.object(client, "get_bytes", return_value=raw):
        assert client.get_json("/nav", allow_codes={-101})["isLogin"] is False


def test_get_json_non412_http_error_reraises():
    client = _http()
    exc = urllib.error.HTTPError(url="", code=403, msg="", hdrs=http.client.HTTPMessage(), fp=None)
    with patch.object(client, "get_bytes", side_effect=exc):
        with pytest.raises(urllib.error.HTTPError):
            client.get_json("/test")


# ── fetch_comments 的风控处理 ───────────────────────────────────────────────


def test_fetch_comments_rate_limited_on_first_page_raises():
    client = MagicMock()
    client.get_json.side_effect = _RateLimited()
    with _nav_patch("_comments"), _SIGN_PATCH, _SLEEP_PATCH:
        with pytest.raises(RateLimitedError):
            fetch_comments(client, aid=100, count=20)


# ── fetch_frame 的前置检查 ──────────────────────────────────────────────────


def test_fetch_frame_without_ffmpeg_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    from polylens_bilibili.api import _frame

    monkeypatch.setattr(_frame.shutil, "which", lambda _name: None)
    client = MagicMock()
    with pytest.raises(BilibiliError):
        _frame.fetch_frame(cast(HttpClient, client), "BV1xx", 0, 1.0)
    client.get_json.assert_not_called()  # 预检在任何网络动作之前


def test_capture_frame_keeps_credentials_out_of_ffmpeg_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """ffmpeg 只该看到本地代理地址。

    改用 ffmpeg 自带的 -headers 也能跑通，但那会把 Cookie 放进命令行，而进程 argv 在
    没挂 hidepid 的 Linux 上同机可读。本地代理存在的理由就是这个，这里把它钉住。
    """
    from polylens_bilibili.api import _frame

    client = MagicMock()
    client.combined_cookie.return_value = "SESSDATA=secret-value; bili_jct=token"
    seen: list[list[str]] = []

    out = tmp_path / "f.jpg"

    def _fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        out.write_bytes(b"\xff\xd8jpeg")  # 出图了，产物检查才放行
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(_frame.subprocess, "run", _fake_run)
    _frame._capture_frame(cast(HttpClient, client), "https://cdn.example/v.m4s", 1.0, str(out))

    argv = " ".join(seen[0])
    assert "secret-value" not in argv
    assert "-headers" not in seen[0]
    assert "http://127.0.0.1:" in argv


def test_capture_frame_proxy_forwards_credentials_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """凭据仍要送到 CDN，只是走代理的上游请求，不走命令行。"""
    from polylens_bilibili.api import _frame

    client = MagicMock()
    client.combined_cookie.return_value = "SESSDATA=secret-value"
    handler_cls: Any = _frame._make_proxy_handler(
        cast(HttpClient, client), "https://cdn.example/v.m4s"
    )
    captured: dict[str, str] = {}

    class _FakeResp:
        status = 200
        headers = {"Content-Type": "video/mp4"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, _n=None):
            return b""

    def _open(req, timeout=None):
        captured.update(req.headers)
        return _FakeResp()

    client._opener.open = _open
    handler = handler_cls.__new__(handler_cls)
    handler.headers = {"Range": "bytes=0-"}
    handler.wfile = io.BytesIO()
    handler.send_response = lambda *a: None
    handler.send_header = lambda *a: None
    handler.end_headers = lambda: None
    handler._proxy()

    # urllib 会把 header 名规范成首字母大写
    assert captured["Cookie"] == "SESSDATA=secret-value"
    assert captured["Range"] == "bytes=0-"          # Range 透传，ffmpeg 才能按需取
    assert captured["User-agent"] == USER_AGENT     # CDN 拒非浏览器 UA，缺它就是 403


@pytest.mark.parametrize("make_output", [False, True])
def test_capture_frame_rejects_empty_output_despite_zero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path, make_output: bool
) -> None:
    """ffmpeg 退出码为 0 不代表出了图。

    实测 ffmpeg 6.1 在目标时刻取不到画面时返回 0，只在 stderr 写一句
    "Output file is empty, nothing was encoded"，产物是空文件或根本没建。
    不查产物就会把空字节当成截帧结果，最终拼成一个 data 为空的图片块。
    """
    from polylens_bilibili.api import _frame

    out = tmp_path / "f.jpg"
    if make_output:
        out.write_bytes(b"")  # NamedTemporaryFile 留下的空占位

    def _fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stderr=b"[out#0/image2] Output file is empty, nothing was encoded",
        )

    monkeypatch.setattr(_frame.subprocess, "run", _fake_run)
    client = MagicMock()
    client.combined_cookie.return_value = ""
    with pytest.raises(RuntimeError):
        _frame._capture_frame(cast(HttpClient, client), "https://cdn/a", 1.0, str(out))


def test_fetch_frame_never_returns_empty_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """产物为空时要落到候选重试与统一报错上，不能把空字节交出去。"""
    from polylens_bilibili.api import _frame

    monkeypatch.setattr(_frame.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        _frame, "_fetch_playurl",
        lambda client, bvid, cid: {"dash": {"video": [
            {"id": 64, "codecs": "avc1", "baseUrl": "https://cdn/a", "backupUrl": ["https://cdn/b"]}
        ]}},
    )
    tried: list[str] = []

    def _empty(client, url, ts, out):
        tried.append(url)
        raise RuntimeError("ffmpeg 退出码为 0 但没有产出画面")

    monkeypatch.setattr(_frame, "_capture_frame", _empty)
    with pytest.raises(BilibiliError):
        _frame.fetch_frame(MagicMock(), "BV1xx", 200, 214.9)
    assert tried == ["https://cdn/a", "https://cdn/b"]  # 备用地址也试过


def test_fetch_frame_requires_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """未登录时平台只给到 480P 而不报错，故取播放地址前先判登录态。"""
    from polylens_bilibili.api import _frame

    monkeypatch.setattr(_frame.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    client = MagicMock()
    with _nav_patch("_frame", _ANONYMOUS):
        with pytest.raises(AuthRequiredError):
            _frame.fetch_frame(client, "BV1xx", 200, 1.0)
    client.get_json.assert_not_called()


# ── 字幕语种 ────────────────────────────────────────────────────────────────


def test_fetch_subtitles_defaults_to_first_track():
    """不指定语种时取平台给的第一条，返回体标明实际拿到的是哪个。"""
    client = _subtitle_client(_SUBTITLE_META)
    with _nav_patch("_subtitles"), _SIGN_PATCH_SUB:
        track = fetch_subtitles(client, aid=100, cid=200)
    assert track.lang == "zh-CN"
    assert track.available_langs == ["zh-CN", "en"]
    assert "sub_zh" in client.get_json_url.call_args[0][0]


def test_fetch_subtitles_picks_requested_lang():
    client = _subtitle_client(_SUBTITLE_META)
    with _nav_patch("_subtitles"), _SIGN_PATCH_SUB:
        track = fetch_subtitles(client, aid=100, cid=200, lang="en")
    assert track.lang == "en"
    assert "sub_en" in client.get_json_url.call_args[0][0]


def test_fetch_subtitles_unknown_lang_raises():
    client = _subtitle_client(_SUBTITLE_META)
    with _nav_patch("_subtitles"), _SIGN_PATCH_SUB:
        with pytest.raises(BilibiliError):
            fetch_subtitles(client, aid=100, cid=200, lang="ja")


def test_fetch_subtitles_track_without_url_raises():
    """有轨道却没有地址，与"这个视频没有字幕"是两回事，静默返回空会把它们混为一谈。"""
    client = _subtitle_client([{"lan": "zh-CN", "subtitle_url": ""}])
    with _nav_patch("_subtitles"), _SIGN_PATCH_SUB:
        with pytest.raises(BilibiliError):
            fetch_subtitles(client, aid=100, cid=200)


# ── 评论排序方式 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("sort", "platform_mode"), [("hot", 3), ("newest", 2)])
def test_fetch_comments_maps_sort_to_platform_mode(sort: str, platform_mode: int):
    """对外用语义名，平台的 mode 编码不外泄。"""
    seen: dict[str, object] = {}

    def _get_json(endpoint, params):
        seen.update(params)
        return _page([_reply(1, "r")], is_end=True)

    client = MagicMock()
    client.get_json.side_effect = _get_json
    with _nav_patch("_comments"), _SIGN_PATCH:
        fetch_comments(client, aid=100, count=5, sort=sort)
    assert seen["mode"] == platform_mode


def test_fetch_comments_defaults_to_hot():
    seen: dict[str, object] = {}

    def _get_json(endpoint, params):
        seen.update(params)
        return _page([_reply(1, "r")], is_end=True)

    client = MagicMock()
    client.get_json.side_effect = _get_json
    with _nav_patch("_comments"), _SIGN_PATCH:
        fetch_comments(client, aid=100, count=5)
    assert seen["mode"] == 3


def test_fetch_comments_rejects_unknown_sort():
    client = MagicMock()
    with _nav_patch("_comments"), _SIGN_PATCH:
        with pytest.raises(BilibiliError):
            fetch_comments(client, aid=100, count=5, sort="oldest")
    client.get_json.assert_not_called()
