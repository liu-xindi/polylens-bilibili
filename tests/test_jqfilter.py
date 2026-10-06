"""jq 过滤：子进程执行、出错兜底与结果按形状编码。不联网。"""

from __future__ import annotations

import pytest

from polylens_bilibili import jqfilter
from polylens_bilibili.jqfilter import JqError, encode, encode_items, run_jq
from polylens_bilibili.models import SubtitleEntry

ENTRIES = [
    SubtitleEntry(start=1.0, end=2.0, content="第一句"),
    SubtitleEntry(start=700.0, end=701.5, content="第二句"),
]


def test_without_expr_is_plain_toon() -> None:
    text, jq_count = encode_items("subtitles", ENTRIES, SubtitleEntry, None)
    assert text == "subtitles[2]{start,end,content}:\n  1,2,第一句\n  700,701.5,第二句"
    assert jq_count is None


def test_uniform_objects_become_table() -> None:
    text, jq_count = encode_items(
        "subtitles", ENTRIES, SubtitleEntry, "[.[] | select(.start >= 600) | {start, content}]"
    )
    assert text == "subtitles[1]{start,content}:\n  700,第二句"
    assert jq_count == 1


def test_stream_of_objects_is_treated_as_array() -> None:
    text, jq_count = encode_items("subtitles", ENTRIES, SubtitleEntry, ".[] | {content}")
    assert text == "subtitles[2]{content}:\n  第一句\n  第二句"
    assert jq_count == 2


def test_single_string_is_returned_raw() -> None:
    text, jq_count = encode_items(
        "subtitles", ENTRIES, SubtitleEntry, 'map(.content) | join("\\n")'
    )
    assert text == "第一句\n第二句"
    assert jq_count is None


def test_empty_result_keeps_header() -> None:
    text, jq_count = encode_items("subtitles", ENTRIES, SubtitleEntry, "[.[] | select(false)]")
    assert text == "subtitles[0]{}:"
    assert jq_count == 0


def test_other_shapes_become_json() -> None:
    assert encode("x", [[1, 2]]) == ("[1,2]", 2)
    assert encode("x", [{"a": [1]}]) == ('{"a":[1]}', None)
    assert encode("x", [[{"a": 1}, {"b": 2}]]) == ('[{"a":1},{"b":2}]', 2)
    assert encode("x", [3]) == ("3", None)


def test_exclude_hides_field_from_jq_input() -> None:
    text, _ = encode_items(
        "subtitles", ENTRIES, SubtitleEntry, "[.[0] | keys[]]", frozenset({"end"})
    )
    assert text == '["content","start"]'


def test_syntax_error_lists_fields() -> None:
    with pytest.raises(JqError) as exc:
        encode_items("subtitles", ENTRIES, SubtitleEntry, ".[")
    assert "syntax error" in str(exc.value)
    assert "start,end,content" in str(exc.value)


def test_runtime_error_is_jq_error() -> None:
    with pytest.raises(JqError, match="Cannot index"):
        run_jq(".a", [1])


def test_infinite_loop_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jqfilter, "TIMEOUT_S", 0.5)
    with pytest.raises(JqError, match="超过"):
        run_jq("[repeat(1)] | length", [])


def test_output_over_limit_is_rejected() -> None:
    with pytest.raises(JqError, match="输出超过"):
        run_jq("range(1000000)", [])


def test_memory_blowup_is_contained() -> None:
    with pytest.raises(JqError):
        run_jq("[range(100000000)] | length", [])
