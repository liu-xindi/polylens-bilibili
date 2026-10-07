"""评论页缓存：过期与容量。"""

from __future__ import annotations

from polylens_bilibili.api._page_cache import PageCache


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_expires_after_ttl_without_renewal_on_hit():
    clock = _Clock()
    cache = PageCache(ttl=600, clock=clock, wall=lambda: 1_700_000_000)
    cache.put("k", "page")
    clock.now = 599
    hit = cache.get("k")
    assert hit is not None and hit.value == "page" and hit.fetched_at == 1_700_000_000
    clock.now = 600
    assert cache.get("k") is None


def test_evicts_least_recently_used():
    cache = PageCache(max_pages=2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")
    cache.put("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") is not None and cache.get("c") is not None
