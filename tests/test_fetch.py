"""抓取函数测试：mock HttpClient，不联网。"""

from __future__ import annotations

import http.client
import json
import urllib.error
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from polylens_bilibili.api._comments import fetch_comments, fetch_replies
from polylens_bilibili.api._danmaku import fetch_bullet_comments
from polylens_bilibili.api._http import BilibiliHttpError, HttpClient, _RateLimited
from polylens_bilibili.api._signing import NavInfo
from polylens_bilibili.api._subtitles import fetch_subtitles
from polylens_bilibili.errors import AuthRequiredError, PolylensError, RateLimitedError

# ── 共用辅助 ────────────────────────────────────────────────────────────────

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


def _reply(rpid: int, content: str, *, parent: int = 0, count: int = 0) -> dict:
    return {
        "rpid": rpid, "parent": parent,
        "member": {"uname": "u", "mid": 1},
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


def test_fetch_comments_empty_replies_returns_empty():
    client = MagicMock()
    client.get_json.return_value = _page([])
    with _nav_patch("_comments"), _SIGN_PATCH:
        page = fetch_comments(client, aid=100, count=20)
    assert page.items == []
    assert page.has_more is False and page.next_cursor is None


def test_fetch_comments_count_truncates_with_next_cursor():
    """每页 1 条、非 is_end、有 offset；count=3 → 翻 3 页够数，has_more=True + next_cursor。"""
    call_count = 0

    def _get_json(endpoint, params):
        nonlocal call_count
        call_count += 1
        return _page(
            [_reply(call_count, f"reply{call_count}")],
            is_end=False, next_offset=f"offset{call_count}",
        )

    client = MagicMock()
    client.get_json.side_effect = _get_json
    with _nav_patch("_comments"), _SIGN_PATCH, _SLEEP_PATCH:
        page = fetch_comments(client, aid=100, count=3)
    assert call_count == 3
    assert len(page.items) == 3
    assert page.has_more is True and page.next_cursor == "offset3"


def test_fetch_comments_normalizes_non_positive_count():
    """count 归一到至少 1：仍抓一页，不空转。"""
    client = MagicMock()
    client.get_json.return_value = _page([_reply(1, "r")])
    with _nav_patch("_comments"), _SIGN_PATCH:
        page = fetch_comments(client, aid=100, count=0)
    assert len(page.items) == 1


def test_fetch_comments_cursor_skips_top_replies():
    """传 cursor（续取）时不插入置顶，置顶只在从头的第一页。"""
    client = MagicMock()
    client.get_json.return_value = _page(
        [_reply(1, "r")], is_end=True, top_replies=[_reply(9, "top")]
    )
    with _nav_patch("_comments"), _SIGN_PATCH:
        page = fetch_comments(client, aid=100, cursor="TOKEN", count=20)
    assert [c.content for c in page.items] == ["r"]


def test_fetch_comments_stops_on_is_end():
    client = MagicMock()
    client.get_json.return_value = _page([_reply(1, "r")], is_end=True)
    with _nav_patch("_comments"), _SIGN_PATCH:
        page = fetch_comments(client, aid=100, count=20)
    assert client.get_json.call_count == 1
    assert page.has_more is False and page.next_cursor is None


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
    return {"replies": [_reply(base + i, f"r{base + i}") for i in range(ps)]}


def test_fetch_replies_slices_window_by_limit():
    client = MagicMock()
    client.get_json.side_effect = _sub_page
    with _nav_patch("_comments"), _SLEEP_PATCH:
        out = fetch_replies(client, 100, ["555"], limit=5)
    t = out[0]
    assert t.comment_id == "555"
    assert len(t.page.items) == 5
    assert t.page.has_more is True and t.page.next_cursor == "5"  # 满页还有更多


def test_fetch_replies_cursor_jumps_to_page():
    seen_pn: list[int] = []

    def _get_json(endpoint, params):
        seen_pn.append(params["pn"])
        return _sub_page(endpoint, params)

    client = MagicMock()
    client.get_json.side_effect = _get_json
    with _nav_patch("_comments"), _SLEEP_PATCH:
        out = fetch_replies(client, 100, ["555"], limit=5, cursor="40")
    assert seen_pn[0] == 3  # start=40 → pn 从 40//20+1=3 起，跳过前两页
    assert out[0].page.items[0].content == "r40"  # 精确从第 40 条
    assert out[0].page.next_cursor == "45"


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
    assert t.page.has_more is True and t.page.next_cursor == "50"


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


def test_fetch_replies_batch_continuation_lockstep():
    """批量续取：同次调用里没到底的楼共享同一 next_cursor；带它续取整批往下走，
    各楼与上一页不重叠；已到底的楼掉队。"""
    client = MagicMock()
    client.get_json.side_effect = _make_sized_sub_page({1: 8, 2: 12, 3: 3})

    with _nav_patch("_comments"), _SLEEP_PATCH:
        first = fetch_replies(client, 100, ["1", "2", "3"], limit=5)
    by_id = {t.comment_id: t for t in first}
    # 没到底的两楼共享同一个 next_cursor（start+count 整批一致）
    assert by_id["1"].page.has_more and by_id["1"].page.next_cursor == "5"
    assert by_id["2"].page.has_more and by_id["2"].page.next_cursor == "5"
    # 只有 3 条的楼一次到底、掉队（不带 next_cursor）
    assert by_id["3"].page.has_more is False and by_id["3"].page.next_cursor is None

    with _nav_patch("_comments"), _SLEEP_PATCH:
        second = fetch_replies(client, 100, ["1", "2"], limit=5, cursor="5")
    by_id2 = {t.comment_id: t for t in second}
    for cid in ("1", "2"):
        ids1 = {r.id for r in by_id[cid].page.items}
        ids2 = {r.id for r in by_id2[cid].page.items}
        assert ids1 and ids2 and ids1.isdisjoint(ids2)
    assert by_id2["1"].page.has_more is False  # 8 条的楼这一窗到底
    assert by_id2["2"].page.has_more and by_id2["2"].page.next_cursor == "10"


def test_fetch_replies_requires_login():
    client = MagicMock()
    with _nav_patch("_comments", _ANONYMOUS), _SLEEP_PATCH:
        with pytest.raises(AuthRequiredError):
            fetch_replies(client, 100, ["1"], limit=5)
    client.get_json.assert_not_called()


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
        entries = fetch_subtitles(client, aid=100, cid=200)
    assert len(entries) == 2
    assert entries[0].content == "first line"
    assert entries[0].start == 1.0 and entries[0].end == 3.6  # 降精度到一位小数


def test_fetch_subtitles_protocol_relative_url_fixed():
    client = _subtitle_client([_SUBTITLE_META[0]])
    with _nav_patch("_subtitles"), _SIGN_PATCH_SUB:
        fetch_subtitles(client, aid=100, cid=200)
    assert client.get_json_url.call_args[0][0].startswith("https://")


def test_fetch_subtitles_picks_first_track():
    client = _subtitle_client(_SUBTITLE_META)
    with _nav_patch("_subtitles"), _SIGN_PATCH_SUB:
        fetch_subtitles(client, aid=100, cid=200)
    assert "sub_zh" in client.get_json_url.call_args[0][0]


def test_fetch_subtitles_empty_when_no_tracks():
    client = _subtitle_client([])
    with _nav_patch("_subtitles"), _SIGN_PATCH_SUB:
        assert fetch_subtitles(client, aid=100, cid=200) == []


def test_fetch_subtitles_requires_login():
    """未登录时平台返回空轨道，与"这个视频没字幕"无法区分，故先判登录态。"""
    client = _subtitle_client([_SUBTITLE_META[0]])
    with _nav_patch("_subtitles", _ANONYMOUS), _SIGN_PATCH_SUB:
        with pytest.raises(AuthRequiredError):
            fetch_subtitles(client, aid=100, cid=200)
    client.get_json.assert_not_called()


# ── fetch_bullet_comments ───────────────────────────────────────────────────

_DANMAKU_XML = """<?xml version="1.0" encoding="UTF-8"?>
<i>
  <d p="3.0,1,25,16777215,1700000000,0,aaa,11111,5">bullet A</d>
  <d p="1.0,5,25,255,1700000001,0,bbb,22222,8">bullet B</d>
</i>"""


def test_fetch_bullet_comments_delegates_to_parse():
    client = MagicMock()
    client.get_bytes.return_value = _DANMAKU_XML.encode()
    bullets = fetch_bullet_comments(client, cid=12345)
    assert len(bullets) == 2
    assert bullets[0].timestamp == 1.0  # 已排序


def test_fetch_bullet_comments_url_includes_cid():
    client = MagicMock()
    client.get_bytes.return_value = b"<i></i>"
    fetch_bullet_comments(client, cid=9999)
    assert "oid=9999" in client.get_bytes.call_args[0][0]


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
    """平台非 0 业务码上浮成 PolylensError，消息用平台原文，能被工具层接住。"""
    client = _http()
    raw = json.dumps({"code": -400, "message": "请求错误"}).encode()
    with patch.object(client, "get_bytes", return_value=raw):
        with pytest.raises(BilibiliHttpError) as exc_info:
            client.get_json("/test")
    assert isinstance(exc_info.value, PolylensError)
    assert "-400" in str(exc_info.value) and "请求错误" in str(exc_info.value)


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


def test_fetch_comments_rate_limited_midpagination_raises():
    """翻页中途触发风控 → 抛 RateLimitedError，不返回半程结果。"""
    call_count = 0

    def _get_json(endpoint, params):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise _RateLimited()
        return _page(
            [_reply(call_count, f"reply{call_count}")], is_end=False, next_offset=f"off{call_count}"
        )

    client = MagicMock()
    client.get_json.side_effect = _get_json
    with _nav_patch("_comments"), _SIGN_PATCH, _SLEEP_PATCH:
        with pytest.raises(RateLimitedError) as exc_info:
            fetch_comments(client, aid=100, count=100)
    assert "风控" in exc_info.value.message


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
    calls: list[int] = []

    class _Client:
        def ensure_buvid(self) -> None:
            calls.append(1)

    with pytest.raises(PolylensError, match="ffmpeg"):
        _frame.fetch_frame(cast(HttpClient, _Client()), "BV1xx", 0, 1.0)
    assert not calls  # 预检在任何网络动作之前


def test_fetch_frame_requires_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """未登录时平台只给到 480P 而不报错，故取播放地址前先判登录态。"""
    from polylens_bilibili.api import _frame

    monkeypatch.setattr(_frame.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    client = MagicMock()
    with _nav_patch("_frame", _ANONYMOUS):
        with pytest.raises(AuthRequiredError):
            _frame.fetch_frame(client, "BV1xx", 200, 1.0)
    client.get_json.assert_not_called()
