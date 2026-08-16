"""输入解析、参数归一与能力入口。不联网。"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from polylens_bilibili import client as client_mod
from polylens_bilibili.client import BilibiliClient, _extract_page, _id_params, resolve_video
from polylens_bilibili.errors import BilibiliError

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
    with pytest.raises(BilibiliError, match="BV/av"):
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


def test_resolve_video_short_link_failure_is_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.error import URLError

    def _boom(req: Any, timeout: int = 20):
        raise URLError("nope")

    monkeypatch.setattr(client_mod, "urlopen", _boom)
    with pytest.raises(BilibiliError, match="短链"):
        resolve_video("https://b23.tv/abcdef")


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


def test_resolve_video_normalizes_non_positive_page() -> None:
    assert resolve_video(BV_URL, 0)[1] == 1
    assert resolve_video(BV_URL, -3)[1] == 1


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


def test_get_login_status_returns_none_when_unverifiable() -> None:
    """网络或平台异常时给 null，与"确定未登录"区分开。"""
    with patch.object(client_mod, "fetch_nav", side_effect=RuntimeError("boom")):
        assert BilibiliClient().get_login_status() is None


# ── 能力入口的参数处理 ──────────────────────────────────────────────────────


def test_comment_replies_with_empty_ids_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """没给 id 就没有楼可钻，返回空列表，不报错也不发请求。"""
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


def test_get_frame_clamps_negative_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, float] = {}
    monkeypatch.setattr(BilibiliClient, "_view", lambda self, vid: _view_stub())
    monkeypatch.setattr(
        client_mod, "fetch_frame",
        lambda http, bvid, cid, at: seen.setdefault("at", at) or b"jpeg",
    )
    BilibiliClient().get_frame("BV1xx", timestamp=-5.0)
    assert seen["at"] == 0.0


def test_get_frame_rejects_timestamp_beyond_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    """超出时长要报错：ffmpeg 会给最后一帧，静默返回会让模型以为截到了指定时刻。"""
    monkeypatch.setattr(BilibiliClient, "_view", lambda self, vid: _view_stub(duration=30.0))
    with pytest.raises(BilibiliError, match="超出视频时长"):
        BilibiliClient().get_frame("BV1xx", timestamp=99.0)


def test_get_frame_allows_timestamp_when_duration_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取不到时长时只校验下界，不拦上界。"""
    monkeypatch.setattr(BilibiliClient, "_view", lambda self, vid: _view_stub(duration=None))
    monkeypatch.setattr(client_mod, "fetch_frame", lambda http, bvid, cid, at: b"jpeg")
    assert BilibiliClient().get_frame("BV1xx", timestamp=9999.0) == b"jpeg"
