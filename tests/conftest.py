from __future__ import annotations

import pytest

from polylens_bilibili.api import _http


@pytest.fixture(autouse=True)
def comment_guard(monkeypatch: pytest.MonkeyPatch) -> _http._CommentGuard:
    """评论组的节流与熔断是进程级状态：每个用例换一份新的，且不真睡。"""
    guard = _http._CommentGuard(sleep=lambda _s: None)
    monkeypatch.setattr(_http, "_comment_guard", guard)
    return guard
