"""输入解析、参数归一与能力入口。不联网。"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from polylens_bilibili import client as client_mod
from polylens_bilibili.client import (
    BilibiliClient,
    _extract_page,
    _id_params,
    resolve_up,
    resolve_video,
)
from polylens_bilibili.errors import BilibiliError, RateLimitedError

BV_URL = "https://www.bilibili.com/video/BV1xx411c7mD/"


# ── 视频号解析 ──────────────────────────────────────────────────────────────


def test_resolve_video_extracts_bv_from_url() -> None:
    assert resolve_video(BV_URL)[0] == "BV1xx411c7mD"


def test_resolve_video_accepts_bare_id() -> None:
    """裸号直接可用，不必写成链接。"""
    assert resolve_video("BV1xx411c7mD")[0] == "BV1xx411c7mD"
    assert resolve_video("av170001")[0] == "av170001"


def test_resolve_video_extracts_from_share_text() -> None:
    assert resolve_video("看这个 av170001 真不错")[0] == "av170001"


def test_resolve_video_rejects_unparsable_input() -> None:
    with pytest.raises(BilibiliError):
        resolve_video("https://example.com/whatever")


def test_resolve_video_expands_short_link(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        def geturl(self) -> str:
            return BV_URL + "?p=2"

        def __enter__(self):
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    monkeypatch.setattr(client_mod, "urlopen", lambda req, timeout=20: _Resp())
    video_id, page = resolve_video("https://b23.tv/abcdef")
    assert video_id == "BV1xx411c7mD"
    assert page == 2  # 展开后的链接里的 ?p= 同样生效


# ── 分段序号 ────────────────────────────────────────────────────────────────


def test_extract_page_from_query() -> None:
    assert _extract_page(BV_URL + "?p=2") == 2
    assert _extract_page("https://www.bilibili.com/video/BV1xx?spm_id=x&p=3&vd=y") == 3
    assert _extract_page("BV1xx?p=4") == 4


def test_extract_page_absent_or_non_positive() -> None:
    assert _extract_page(BV_URL) is None
    assert _extract_page("BV1xx?p=0") is None


def test_resolve_video_page_precedence() -> None:
    """显式传入 > 链接里的 ?p= > 第 1 段。"""
    assert resolve_video(BV_URL + "?p=3")[1] == 3
    assert resolve_video(BV_URL + "?p=3", 5)[1] == 5
    assert resolve_video(BV_URL)[1] == 1


@pytest.mark.parametrize("bad", [0, -3])
def test_resolve_video_rejects_non_positive_page(bad: int) -> None:
    with pytest.raises(BilibiliError):
        resolve_video(BV_URL, bad)


def test_link_with_zero_page_falls_back_instead_of_failing() -> None:
    """?p=0 是链接自带的，不是模型传的；这样的链接在网页上照样能打开，不该为它报错。"""
    assert resolve_video(BV_URL + "?p=0")[1] == 1


# ── 视频号 → 接口参数 ───────────────────────────────────────────────────────


def test_id_params_by_kind() -> None:
    assert _id_params("BV1xx411c7mD") == {"bvid": "BV1xx411c7mD"}
    assert _id_params("av170001") == {"aid": 170001}


def test_id_params_rejects_unknown_shape() -> None:
    with pytest.raises(BilibiliError):
        _id_params("xyz123")


# ── get_login_status ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("is_login", [True, False])
def test_get_login_status_reports_platform_answer(is_login: bool) -> None:
    with patch.object(client_mod, "fetch_nav", return_value=(("i", "s", is_login))) as nav:
        nav.return_value = type("N", (), {"is_login": is_login})()
        assert BilibiliClient().get_login_status() is is_login


@pytest.mark.parametrize(
    "failure",
    [OSError("network down"), BilibiliError("接口返回失败: -400"), KeyError("wbi_img")],
)
def test_get_login_status_returns_none_when_unverifiable(failure: Exception) -> None:
    """网络不可达、平台报错、响应改形状都归为 null，与"确定未登录"区分开。"""
    with patch.object(client_mod, "fetch_nav", side_effect=failure):
        assert BilibiliClient().get_login_status() is None


def test_get_login_status_lets_unexpected_errors_surface() -> None:
    """代码缺陷不该被伪装成"平台不给答案"。"""
    with patch.object(client_mod, "fetch_nav", side_effect=TypeError("bug")):
        with pytest.raises(TypeError):
            BilibiliClient().get_login_status()


# ── 能力入口的参数处理 ──────────────────────────────────────────────────────


def test_comment_replies_with_empty_ids_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """没给 id 就没有可取的回复，返回空列表，不报错也不发请求。"""
    called: list[int] = []
    monkeypatch.setattr(
        BilibiliClient, "_view", lambda self, vid: called.append(1) or {}  # type: ignore[func-returns-value]
    )
    assert BilibiliClient().get_comment_replies("BV1xx", comment_ids=[], limit=5) == []
    assert not called


def _view_stub(duration: float | None = 100.0) -> dict[str, Any]:
    return {
        "aid": 1, "bvid": "BV1xx", "cid": 200, "title": "t",
        "stat": {}, "duration": duration,
    }


def test_get_frame_rejects_negative_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[int] = []
    monkeypatch.setattr(
        BilibiliClient, "_view", lambda self, vid: called.append(1) or _view_stub()
    )
    with pytest.raises(BilibiliError):
        BilibiliClient().get_frame("BV1xx", timestamp=-5.0)
    assert not called  # 校验在取视频数据之前


def test_get_frame_rejects_timestamp_beyond_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    """超出时长要报错：ffmpeg 会给最后一帧，静默返回会让模型以为截到了指定时刻。"""
    monkeypatch.setattr(BilibiliClient, "_view", lambda self, vid: _view_stub(duration=30.0))
    with pytest.raises(BilibiliError):
        BilibiliClient().get_frame("BV1xx", timestamp=99.0)


def test_get_frame_rejects_timestamp_equal_to_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """时间轴是 [0, duration)，等于时长那一刻没有帧。

    放过去的话 ffmpeg 会失败，报出来的形状与其他参数边界不一致。
    """
    monkeypatch.setattr(BilibiliClient, "_view", lambda self, vid: _view_stub(duration=30.0))
    with pytest.raises(BilibiliError):
        BilibiliClient().get_frame("BV1xx", timestamp=30.0)


def test_get_frame_allows_timestamp_when_duration_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取不到时长时只校验下界，不拦上界。"""
    monkeypatch.setattr(BilibiliClient, "_view", lambda self, vid: _view_stub(duration=None))
    monkeypatch.setattr(client_mod, "fetch_frame", lambda http, bvid, cid, at: b"jpeg")
    assert BilibiliClient().get_frame("BV1xx", timestamp=9999.0) == b"jpeg"


# ── 短链展开 ────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, url: str) -> None:
        self._url = url

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a: Any) -> None:
        return None


@pytest.mark.parametrize(
    "text",
    [
        "https://b23.tv/abcdef",
        "【标题】 https://b23.tv/abcdef",                       # App 分享按钮的默认格式
        "【标题】 https://b23.tv/abcdef?share_source=copy_web",
        "看这个 https://b23.tv/abcdef。后面还有中文",
        "【a】https://bili2233.cn/abcdef 附言",
    ],
)
def test_short_link_found_inside_share_text(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    """分享文案里链接前面有标题，不能把整段丢给 urlparse。"""
    seen: dict[str, Any] = {}

    def _fake(req, timeout=20):
        seen["url"] = req.full_url
        return _Resp(BV_URL)

    monkeypatch.setattr(client_mod, "urlopen", _fake)
    assert resolve_video(text) == ("BV1xx411c7mD", 1)
    assert "。" not in seen["url"] and "附言" not in seen["url"]  # 中文没被吃进链接


def test_short_link_keeps_the_page_written_by_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """短链跳转不转发 query，跳转目标还自带 p=1，只看展开后的链接会把段号盖成 1。"""
    monkeypatch.setattr(
        client_mod, "urlopen",
        lambda req, timeout=20: _Resp(BV_URL + "?share_source=copy&p=1&spmid=x"),
    )
    assert resolve_video("https://b23.tv/abcdef?p=150") == ("BV1xx411c7mD", 150)
    assert resolve_video("【标题】 https://b23.tv/abcdef?p=150") == ("BV1xx411c7mD", 150)


def test_short_link_falls_back_to_page_in_expanded_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分段分享出来的短链自己不带 ?p=，段号在展开后的链接里。"""
    monkeypatch.setattr(
        client_mod, "urlopen", lambda req, timeout=20: _Resp(BV_URL + "?p=7&share_source=copy")
    )
    assert resolve_video("https://b23.tv/abcdef") == ("BV1xx411c7mD", 7)


def test_explicit_page_still_wins_over_both(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        client_mod, "urlopen", lambda req, timeout=20: _Resp(BV_URL + "?p=1")
    )
    assert resolve_video("https://b23.tv/abcdef?p=150", page=3) == ("BV1xx411c7mD", 3)


def test_short_link_carries_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    """短链走网页域名，平台对部分出口地址有风控，带登录 cookie 才放行。"""
    seen: dict[str, Any] = {}

    def _fake(req, timeout=20):
        seen["headers"] = dict(req.headers)
        return _Resp(BV_URL)

    monkeypatch.setattr(client_mod, "urlopen", _fake)
    resolve_video("https://b23.tv/abcdef", cookie="SESSDATA=x")
    assert seen["headers"].get("Cookie") == "SESSDATA=x"


def test_short_link_without_cookie_sends_no_cookie_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def _fake(req, timeout=20):
        seen["headers"] = dict(req.headers)
        return _Resp(BV_URL)

    monkeypatch.setattr(client_mod, "urlopen", _fake)
    resolve_video("https://b23.tv/abcdef")
    assert "Cookie" not in seen["headers"]


@pytest.mark.parametrize(
    "text", ["b23.tv/abcdef", "【标题】 b23.tv/abcdef 附言", "bili2233.cn/abcdef"]
)
def test_short_link_without_scheme(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    seen: dict[str, Any] = {}

    def _fake(req, timeout=20):
        seen["url"] = req.full_url
        return _Resp(BV_URL)

    monkeypatch.setattr(client_mod, "urlopen", _fake)
    assert resolve_video(text) == ("BV1xx411c7mD", 1)
    assert seen["url"].startswith("https://") and seen["url"].endswith("/abcdef")


def test_bare_host_inside_longer_domain_is_not_a_short_link() -> None:
    with pytest.raises(BilibiliError, match="无法从输入解析"):
        resolve_video("notb23.tv/abcdef")


def test_dead_short_link_is_reported_as_such(monkeypatch: pytest.MonkeyPatch) -> None:
    """失效短链不跳转，平台直接回 200，展开后还是短链本身。"""
    monkeypatch.setattr(
        client_mod, "urlopen", lambda req, timeout=20: _Resp("https://b23.tv/zzzzzzz")
    )
    with pytest.raises(BilibiliError, match="短链无效或已失效"):
        resolve_video("https://b23.tv/zzzzzzz")


def test_non_short_link_never_goes_online(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(req, timeout=20):
        raise AssertionError("不该联网")

    monkeypatch.setattr(client_mod, "urlopen", _boom)
    assert resolve_video(BV_URL)[0] == "BV1xx411c7mD"


# ── UP 主定位 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "https://space.bilibili.com/3690981465524933",
        "https://space.bilibili.com/3690981465524933/upload/video",
        "space.bilibili.com/3690981465524933?spm_id_from=333.1007",
        " 3690981465524933 ",
    ],
)
def test_resolve_up_accepts_space_link_or_mid(text: str) -> None:
    assert resolve_up(text) == 3690981465524933


def test_resolve_up_rejects_other_input() -> None:
    with pytest.raises(BilibiliError):
        resolve_up("高中物理老何")


def test_comment_tools_refuse_while_breaker_open(comment_guard: Any) -> None:
    """熔断期间连取视频信息的请求也不发。"""
    comment_guard.failed("412")
    client = BilibiliClient()
    with patch("polylens_bilibili.client.fetch_view", side_effect=AssertionError("不该发请求")):
        with pytest.raises(RateLimitedError, match="约 15 分钟后再试"):
            client.get_comments("BV1xx", count=20)
        with pytest.raises(RateLimitedError, match="约 15 分钟后再试"):
            client.get_comment_replies("BV1xx", comment_ids=["1"])
