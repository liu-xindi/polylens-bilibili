"""搜索联想：参数与响应映射。不联网。"""

from __future__ import annotations

from typing import Any

import pytest

from polylens_bilibili.api._suggest import fetch_suggest
from polylens_bilibili.errors import BilibiliError


class _Client:
    def __init__(self, data: Any) -> None:
        self.data = data
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def get_json(self, path: str, params: dict[str, Any], **kw: Any) -> Any:
        self.calls.append((params, kw))
        return self.data


def test_returns_values_in_platform_order() -> None:
    client = _Client({"result": {"tag": [
        {"value": "python", "term": "python", "name": "<em>py</em>thon"},
        {"value": "pycharm", "term": "pycharm", "name": "<em>py</em>charm"},
    ]}})
    assert fetch_suggest(client, " py ") == ["python", "pycharm"]  # type: ignore[arg-type]
    params, kw = client.calls[0]
    assert params == {"term": "py"}
    assert 3 in kw["allow_codes"]


def test_no_suggestion_is_empty_list() -> None:
    """code 3 放行后 data 为空。"""
    assert fetch_suggest(_Client(None), "zzqxjvkwp") == []  # type: ignore[arg-type]


def test_blank_term_rejected_without_request() -> None:
    client = _Client(None)
    with pytest.raises(BilibiliError):
        fetch_suggest(client, "   ")  # type: ignore[arg-type]
    assert client.calls == []
