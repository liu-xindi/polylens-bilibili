"""搜索：字段映射、偏移量游标分页、条目类型容错。不联网。"""

from __future__ import annotations

from typing import Any

import pytest

from polylens_bilibili.api import _search as search_mod
from polylens_bilibili.api._signing import NavInfo
from polylens_bilibili.errors import BilibiliError, RateLimitedError
from polylens_bilibili.models import Page, SearchItem

# ── 标题清洗 / 字段映射 ─────────────────────────────────────────────────────


def test_strip_em_removes_highlight_tags() -> None:
    assert search_mod._strip_em('这是<em class="keyword">Python</em>教程') == "这是Python教程"


def test_to_search_item_maps_fields() -> None:
    raw = {
        "title": '<em class="keyword">py</em>入门', "bvid": "BV1xx", "author": "up主", "mid": 42,
        "play": 12345, "danmaku": 67, "duration": "10:00", "pubdate": 1700000000,
    }
    item = search_mod._to_search_item(raw)
    assert item is not None
    assert item.title == "py入门"  # 高亮标签清洗
    assert item.url == "https://www.bilibili.com/video/BV1xx"  # bvid 拼完整链接
    assert item.author == "up主"
    assert item.author_url == "https://space.bilibili.com/42"
    assert item.view_count == 12345
    assert item.danmaku_count == 67
    assert item.duration_sec == 600.0
    assert item.published_at is not None  # pubdate 转本机时区可读串


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("10:00", 600.0),      # 常见形态
        ("23:3", 1383.0),      # 秒不补零，平台原样如此
        ("5505:10", 330310.0), # 长视频作"总分钟:秒"，没有小时段
        ("45", 45.0),          # 只有秒
        ("1:02:03", 3723.0),   # 平台哪天补上小时段也解得对
        ("0:00", 0.0),
    ],
)
def test_duration_seconds_parses_platform_text(text: str, seconds: float) -> None:
    assert search_mod._duration_seconds(text) == seconds


@pytest.mark.parametrize("junk", [None, 600, "", "abc", "1:2:3:4", "1:-2", "1:x"])
def test_duration_seconds_degrades_to_none(junk: Any) -> None:
    """解析不了只让这一格缺失，不带崩整条。"""
    assert search_mod._duration_seconds(junk) is None


# ── 分页（游标为绝对偏移量 / has_more 边界）─────────────────────────────────


def _raw(index: int) -> dict[str, Any]:
    """一条可辨认的原始结果：序号编进标题与 bvid，用于核对翻页有没有错位。"""
    return {"title": f"t{index}", "bvid": f"BV{index}"}


def _stub(monkeypatch: pytest.MonkeyPatch, result: Any, **extra: Any) -> Any:
    """打桩 nav 与 get_json，避免联网；返回打桩 client（记下实际发出的参数）。

    result 按 Any 收：平台真给出畸形条目、乃至整个 result 不是列表时，都要能照原样喂进来。
    extra 并进响应顶层，用于喂 pagesize 这类回显字段。
    """
    monkeypatch.setattr(search_mod, "fetch_nav", lambda client: NavInfo("img", "sub", True))
    # 签名桩原样返回入参，好让测试断言发出的页码与页大小
    monkeypatch.setattr(search_mod, "sign_params", lambda params, *a, **k: params)

    class _Client:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def get_json(self, path, params=None, **kw):
            self.calls.append(dict(params or {}))
            return {"result": result, **extra}

    return _Client()


def _fetch(client, cursor: str | None = None) -> Page[SearchItem]:
    return search_mod.fetch_search(client, "kw", cursor=cursor)


def test_page_size_fixed_and_cursor_counts_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _stub(monkeypatch, [_raw(i) for i in range(30)])
    first = _fetch(client)
    assert client.calls[0]["page"] == 1
    assert client.calls[0]["page_size"] == 30
    assert len(first.items) == 30
    assert first.has_more is True and first.next_cursor == "1"
    _fetch(client, cursor=first.next_cursor)
    assert client.calls[1]["page"] == 2


def test_stops_at_result_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """第 34 页覆盖到第 1020 条，已越过结果上限（1000）→ has_more=false，即使本页有数据。"""
    client = _stub(monkeypatch, [_raw(i) for i in range(990, 1000)])
    page = _fetch(client, cursor="33")
    assert client.calls[0]["page"] == 34
    assert page.has_more is False
    assert page.next_cursor is None


def test_page_before_cap_still_has_more(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _stub(monkeypatch, [_raw(i) for i in range(960, 990)])
    page = _fetch(client, cursor="32")  # 第 33 页止于第 990 条
    assert page.has_more is True and page.next_cursor == "33"


def test_stops_on_empty_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """平台一条都不给 → has_more=false（枯竭）。"""
    page = _fetch(_stub(monkeypatch, []), cursor="3")
    assert page.items == []
    assert page.has_more is False
    assert page.next_cursor is None


def test_echoed_page_size_mismatch_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """平台回显的 pagesize 与请求不符 → 按页数续取会错位，显式报错。"""
    client = _stub(monkeypatch, [_raw(i) for i in range(20)], pagesize=20)
    with pytest.raises(BilibiliError):
        _fetch(client)


def test_echoed_page_size_matching_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """回显与请求一致时照常返回；回显缺席时不判（不是每个响应都带这个字段）。"""
    client = _stub(monkeypatch, [_raw(i) for i in range(30)], pagesize=30)
    assert len(_fetch(client).items) == 30


# ── 参数归一与游标校验 ──────────────────────────────────────────────────────


def test_empty_query_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _stub(monkeypatch, [])
    with pytest.raises(BilibiliError):
        search_mod.fetch_search(client, "   ")
    assert client.calls == []  # 不发请求


def test_negative_cursor_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _stub(monkeypatch, [_raw(1)])
    with pytest.raises(BilibiliError):
        _fetch(client, cursor="-5")


def test_unparsable_cursor_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """游标由本能力发出，解析不了说明调用方自造了。"""
    client = _stub(monkeypatch, [_raw(1)])
    with pytest.raises(BilibiliError):
        _fetch(client, cursor="not-a-number")


# ── 条目映射的类型容错 ──────────────────────────────────────────────────────


def test_item_without_bvid_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """一条坏数据不该毁掉整页，游标照常指向下一页。"""
    client = _stub(monkeypatch, [{"title": "无 bvid"}, _raw(1)])
    page = _fetch(client)
    assert [item.title for item in page.items] == ["t1"]
    assert page.next_cursor == "1"


def test_rate_limited_surfaces_as_error_not_end_of_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """风控要报错，不能判到底：判到底会让翻页静默断在被拦的那一批。"""
    monkeypatch.setattr(search_mod, "fetch_nav", lambda client: NavInfo("img", "sub", True))
    monkeypatch.setattr(search_mod, "sign_params", lambda params, *a, **k: params)

    class _Blocked:
        def get_json(self, path, params=None, **kw):
            raise search_mod._RateLimited()

    with pytest.raises(RateLimitedError):
        _fetch(_Blocked())


@pytest.mark.parametrize("shape", ["abcdefg", {"a": 1}, 5, "", {}, 0, False])
def test_non_list_result_fails_loudly(monkeypatch: pytest.MonkeyPatch, shape: Any) -> None:
    """result 不是列表 = 平台改了响应形状，该显式失败，不能静默返回空页。

    字符串最需要这道判定：它可切片可迭代，逐字符都会被条目级的 isinstance 挡掉，
    于是整页悄悄变空、游标却照 len(字符数) 往前走，看上去像"这批全是坏条目"。
    falsy 的那几个（""、{}、0、False）同样要拦：判定若排在兜空值之后，它们会被
    悄悄改写成 []，当成"没有结果"放行。只有 result 整个缺席才是真的没有结果。
    """
    with pytest.raises(BilibiliError):
        _fetch(_stub(monkeypatch, shape))


def test_all_malformed_page_keeps_going(monkeypatch: pytest.MonkeyPatch) -> None:
    """整页全坏也照样往前翻：平台给了条目就不该在这里判到底。"""
    page = _fetch(_stub(monkeypatch, [{"title": "x"}, "不是对象"]))
    assert page.items == []
    assert page.has_more is True
    assert page.next_cursor == "1"


@pytest.mark.parametrize("junk", [
    ["a"], {"x": 1}, "abc",       # int() 对容器抛 TypeError，对非数字串抛 ValueError
    float("nan"), float("inf"),   # 前者 ValueError，后者 OverflowError
    "nan", "inf",                 # 串形态同样进不了 int()
])
def test_unexpected_field_value_degrades_to_missing(
    monkeypatch: pytest.MonkeyPatch, junk: Any
) -> None:
    """计数或时间戳给成异常类型时，只是该项没有，整条与整页都要留下。"""
    raw = _raw(1) | {"play": junk, "danmaku": junk, "pubdate": junk}
    item = _fetch(_stub(monkeypatch, [raw])).items[0]
    assert item.url.endswith("BV1")
    assert item.published_at is None
    assert item.view_count is None
    assert item.danmaku_count is None


@pytest.mark.parametrize("huge", [10 ** 20, 10 ** 400])
def test_out_of_range_pubdate_only_drops_the_time(
    monkeypatch: pytest.MonkeyPatch, huge: int
) -> None:
    """越界时间戳在 fromtimestamp 处抛 OSError，只该丢掉时间，播放量与整条都要留。

    这个量级对计数是合法整数（Python 整数无上界），对时间戳则越界；
    两个字段的判据不同，不能混为一谈。
    """
    raw = _raw(1) | {"play": huge, "pubdate": huge}
    item = _fetch(_stub(monkeypatch, [raw])).items[0]
    assert item.published_at is None
    assert item.view_count == huge


@pytest.mark.parametrize("absent", [None, "", 0])
def test_absent_title_becomes_empty_string(
    monkeypatch: pytest.MonkeyPatch, absent: Any
) -> None:
    """平台显式给 null 时标题是空串，不是字符串 "None"。"""
    raw = _raw(1) | {"title": absent}
    assert _fetch(_stub(monkeypatch, [raw])).items[0].title == ""


def test_zero_metrics_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """新投稿播放量确实为 0，是真实计数而非缺失。"""
    raw = _raw(1) | {"play": 0, "danmaku": 0}
    item = _fetch(_stub(monkeypatch, [raw])).items[0]
    assert item.view_count == 0
    assert item.danmaku_count == 0
