"""评论结果缓存：容量与淘汰。"""

from __future__ import annotations

from dataclasses import dataclass

from polylens_bilibili.api._result_cache import ResultCache, deep_size


def _sized(value: int) -> int:
    return value


def test_hit_carries_fetch_time():
    cache = ResultCache(wall=lambda: 1_700_000_000)
    cache.put("k", "v")
    hit = cache.get("k")
    assert hit is not None and hit.value == "v" and hit.fetched_at == 1_700_000_000


def test_evicts_least_recently_used_by_size():
    cache = ResultCache(max_bytes=10, sizeof=_sized)
    cache.put("a", 4)
    cache.put("b", 4)
    cache.get("a")
    cache.put("c", 4)
    assert cache.get("b") is None
    assert cache.get("a") is not None and cache.get("c") is not None


def test_one_large_entry_evicts_several_small():
    cache = ResultCache(max_bytes=10, sizeof=_sized)
    for key in "abc":
        cache.put(key, 3)
    cache.put("big", 8)
    assert [k for k in "abc" if cache.get(k) is not None] == []
    assert cache.get("big") is not None


def test_entry_larger_than_cap_is_not_kept():
    cache = ResultCache(max_bytes=10, sizeof=_sized)
    cache.put("a", 4)
    cache.put("huge", 11)
    assert cache.get("huge") is None and cache.get("a") is not None


def test_overwrite_releases_old_size():
    cache = ResultCache(max_bytes=10, sizeof=_sized)
    cache.put("a", 6)
    cache.put("a", 6)
    cache.put("b", 4)
    assert cache.get("a") is not None and cache.get("b") is not None


def test_deep_size_counts_dataclass_fields():
    @dataclass(slots=True)
    class Item:
        text: str

    short, long = deep_size([Item("x")]), deep_size([Item("x" * 1000)])
    assert long - short >= 999
