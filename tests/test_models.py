"""数据形状与 TOON 编码。"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from typing import Any

import pytest

from polylens_bilibili.models import (
    Comment,
    Danmaku,
    SearchItem,
    SubtitleEntry,
    VideoInfo,
    VideoPart,
    to_local_time,
    to_toon,
    toon_table,
)


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


# ── to_local_time ───────────────────────────────────────────────────────────


def test_to_local_time_renders_local_readable_string() -> None:
    assert to_local_time(1700000000) == "2023-11-15 06:13"


@pytest.mark.parametrize("absent", [None, 0])
def test_to_local_time_treats_zero_as_absent(absent) -> None:
    assert to_local_time(absent) is None


# ── toon_table 编码 ─────────────────────────────────────────────────────────


def test_toon_table_header_and_rows() -> None:
    """表头声明列一次（含行数），每行按列给值、两空格缩进。"""
    out = toon_table("items", [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}], ["a", "b"])
    assert out == "items[2]{a,b}:\n  1,x\n  2,y"


def test_toon_table_empty_list_is_header_only() -> None:
    """空列表只留声明（长度 0），无数据行。"""
    assert toon_table("items", [], ["a", "b"]) == "items[0]{a,b}:"


def test_toon_cell_none_is_empty_zero_is_written() -> None:
    """None 留空（该项无值）；数值 0 照常写出（有值且为零）。"""
    assert toon_table("t", [{"a": None, "b": 0}], ["a", "b"]) == "t[1]{a,b}:\n  ,0"


def test_toon_cell_quotes_when_value_has_delimiter_or_newline() -> None:
    """值含逗号、换行、引号时加引号并转义，避免破坏列对齐。"""
    out = toon_table("t", [{"c": 'a,b\nc"d'}], ["c"])
    assert out == 't[1]{c}:\n  "a,b\\nc\\"d"'


def test_toon_cell_quotes_numeric_looking_strings() -> None:
    """形似数字的字符串加引号，避免消费端把它当成数值。"""
    assert toon_table("t", [{"id": "114514"}], ["id"]) == 't[1]{id}:\n  "114514"'


def test_toon_float_drops_trailing_zero() -> None:
    """整数值的 float 去小数（5.0→5），非整数保留精度。"""
    assert toon_table("t", [{"x": 5.0, "y": 5.1}], ["x", "y"]) == "t[1]{x,y}:\n  5,5.1"


def test_toon_bool_is_lowercase_literal() -> None:
    assert toon_table("t", [{"a": True, "b": False}], ["a", "b"]) == "t[1]{a,b}:\n  true,false"


# ── to_toon：列由数据类字段派生，表头固定 ───────────────────────────────────


def _comment(**kw: Any) -> Comment:
    base = Comment(
        id="1", author="u", author_url=None, author_level=None, is_up=False,
        ip_location=None, content="hi", like_count=0, reply_count=0,
        parent_id=None, created_at=None, is_top=False, up_liked=False,
        image_urls=None, link_titles=None,
    )
    return replace(base, **kw)


def test_comments_toon_columns_are_fixed() -> None:
    """列固定，与本批数据无关；计数为 0 照常写出，id 为数字串加引号，布尔写成字面量。"""
    out = to_toon("comments", [_comment()], Comment)
    expect = (
        "comments[1]{id,author,author_url,author_level,is_up,ip_location,content,like_count,"
        "reply_count,parent_id,created_at,is_top,up_liked,image_urls,link_titles}:\n"
        '  "1",u,,,false,,hi,0,0,,,false,false,,'
    )
    assert out == expect


def test_comments_toon_optional_columns_stay_when_batch_has_none() -> None:
    """整批都没有的列仍在，只是留空：表头稳定，消费端不用逐批解析列。"""
    head = to_toon("comments", [_comment()], Comment).split("\n")[0]
    for column in ("parent_id", "image_urls", "link_titles"):
        assert column in head


def test_comments_toon_carries_images_and_link_titles() -> None:
    """配图与链接标题各占一列，多个值以换行分隔，编码后仍在同一行。"""
    lines = to_toon(
        "comments",
        [_comment(is_top=True, up_liked=True, image_urls="http://a.jpg\nhttp://b.jpg",
                  link_titles="黑神话 | 钟馗\n游科 访谈")],
        Comment,
    ).split("\n")
    assert len(lines) == 2
    assert "true,true" in lines[1]
    assert '"http://a.jpg\\nhttp://b.jpg"' in lines[1]
    assert '"黑神话 | 钟馗\\n游科 访谈"' in lines[1]


def test_danmaku_toon_keeps_zero_timestamp() -> None:
    """弹幕 timestamp=0（视频第 0 秒）是真信号，照常写出。"""
    bullets = [Danmaku(content="x", timestamp=0.0, heat=5)]
    out = to_toon("danmaku", bullets, Danmaku)
    assert out == "danmaku[1]{content,timestamp,heat}:\n  x,0,5"


def test_subtitles_toon_shape() -> None:
    out = to_toon(
        "subtitles", [SubtitleEntry(start=22.9, end=25.8, content="你知道规则")], SubtitleEntry
    )
    assert out == "subtitles[1]{start,end,content}:\n  22.9,25.8,你知道规则"


def test_search_items_toon_columns_are_fixed() -> None:
    """缺席的列留空位而不消失。"""
    out = to_toon(
        "results",
        [
            SearchItem(title="t1", url="u1", author="甲", author_url="s1", published_at=None,
                       duration_sec=None, category="知识", tags=None, summary=None,
                       view_count=9, danmaku_count=None, comment_count=None, like_count=None,
                       favorite_count=3),
            SearchItem(title="t2", url="u2", author=None, author_url=None, published_at=None,
                       duration_sec=None, category=None, tags=None, summary=None,
                       view_count=None, danmaku_count=None, comment_count=None,
                       like_count=None, favorite_count=None),
        ],
        SearchItem,
    )
    assert out == (
        "results[2]{title,url,author,author_url,published_at,duration_sec,category,tags,summary,"
        "view_count,danmaku_count,comment_count,like_count,favorite_count}:\n"
        "  t1,u1,甲,s1,,,知识,,,9,,,,3\n"
        "  t2,u2,,,,,,,,,,,,"
    )


def test_to_toon_exclude_drops_columns() -> None:
    out = to_toon(
        "subtitles", [SubtitleEntry(start=1.0, end=2.0, content="x")], SubtitleEntry,
        exclude=frozenset({"end"}),
    )
    assert out == "subtitles[1]{start,content}:\n  1,x"


def test_to_toon_empty_list_still_declares_columns() -> None:
    """空批次也要有表头：列由类型派生，不依赖有没有数据。"""
    assert to_toon("subtitles", [], SubtitleEntry) == "subtitles[0]{start,end,content}:"


# ── VideoInfo ─────────────────────────────────────────────────────────────


def test_video_info_keeps_all_fields_including_none_and_zero() -> None:
    """字段恒在：没有的项是 null，为 0 的项照常给出 0。"""
    d = VideoInfo(id="BV1", title="t", view_count=0).model_dump()
    assert d["view_count"] == 0
    assert d["author"] is None
    assert set(d) == {"id", "title", "author", "author_url", "url", "published_at", "summary",
                      "copyright", "staff",
                      "duration_sec", "total_duration_sec", "view_count", "danmaku_count_total",
                      "comment_count",
                      "like_count", "favorite_count", "share_count", "coin_count",
                      "cover_url", "part_count", "category_id",
                      "current_page", "current_part"}


def test_video_info_has_no_parts_list() -> None:
    """分段清单走 get_parts，不塞进单条元信息里。"""
    assert "parts" not in VideoInfo(id="BV1", title="t").model_dump()


def test_parts_toon_shape() -> None:
    out = to_toon("parts", [VideoPart(page=2, part="正片", duration=600.0)], VideoPart)
    assert out == "parts[1]{page,part,duration}:\n  2,正片,600"
