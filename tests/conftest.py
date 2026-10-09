from __future__ import annotations

import pytest

from polylens_bilibili.api import _comments, _http, _result_cache


@pytest.fixture(autouse=True)
def guards(monkeypatch: pytest.MonkeyPatch) -> dict[str, _http._Guard]:
    """各组的节流与熔断是进程级状态：每个用例换一份新的，且不真睡。"""
    fresh = _http._new_guards()
    for guard in fresh.values():
        guard._sleep = lambda _s: None
    monkeypatch.setattr(_http, "_guards", fresh)
    return fresh


@pytest.fixture(autouse=True)
def result_cache(monkeypatch: pytest.MonkeyPatch) -> _result_cache.ResultCache:
    """评论结果缓存也是进程级状态：每个用例换一份空的。"""
    cache = _result_cache.ResultCache()
    monkeypatch.setattr(_comments, "result_cache", cache)
    return cache
