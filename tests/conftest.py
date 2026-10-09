from __future__ import annotations

import pytest

from polylens_bilibili.api import _comments, _http, _result_cache


@pytest.fixture(autouse=True)
def comment_guard(monkeypatch: pytest.MonkeyPatch) -> _http._CommentGuard:
    """评论组的节流与熔断是进程级状态：每个用例换一份新的，且不真睡。"""
    guard = _http._CommentGuard(sleep=lambda _s: None)
    monkeypatch.setattr(_http, "_comment_guard", guard)
    return guard


@pytest.fixture(autouse=True)
def result_cache(monkeypatch: pytest.MonkeyPatch) -> _result_cache.ResultCache:
    """评论结果缓存也是进程级状态：每个用例换一份空的。"""
    cache = _result_cache.ResultCache()
    monkeypatch.setattr(_comments, "result_cache", cache)
    return cache
