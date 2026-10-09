"""评论结果缓存，进程内共享：http 部署下所有会话共用一份。

按整次调用缓存：键是除 jq 外的全部参数，命中就整批原样给回，不与新取的页拼接。
只缓存完整取完的结果；中途被风控打断的部分结果不缓存。

不设有效期，按估算的内存占用设上限，满了淘汰最久没用的；服务重启即清空。
键里带账号：IP 属地等字段只有登录后才有，换账号不复用。
"""

from __future__ import annotations

import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import fields, is_dataclass
from typing import Any, NamedTuple

_MAX_BYTES = 50 * 1024 * 1024


class Cached(NamedTuple):
    value: Any
    fetched_at: int  # epoch 秒，告诉调用方数据有多旧


def deep_size(obj: Any, seen: set[int] | None = None) -> int:
    """对象连同其引用的容器、数据类字段的大致字节数。只覆盖评论结果用到的类型。"""
    seen = set() if seen is None else seen
    if id(obj) in seen:
        return 0
    seen.add(id(obj))
    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        size += sum(deep_size(k, seen) + deep_size(v, seen) for k, v in obj.items())
    elif isinstance(obj, list | tuple | set | frozenset):
        size += sum(deep_size(v, seen) for v in obj)
    elif is_dataclass(obj) and not isinstance(obj, type):
        size += sum(deep_size(getattr(obj, f.name), seen) for f in fields(obj))
    return size


class ResultCache:
    def __init__(
        self,
        max_bytes: int = _MAX_BYTES,
        wall: Callable[[], float] = time.time,
        sizeof: Callable[[Any], int] = deep_size,
    ) -> None:
        self._max = max_bytes
        self._wall = wall
        self._sizeof = sizeof
        self._lock = threading.Lock()
        self._entries: OrderedDict[Hashable, tuple[int, Cached]] = OrderedDict()
        self._bytes = 0

    def get(self, key: Hashable) -> Cached | None:
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return None
            self._entries.move_to_end(key)
            return hit[1]

    def put(self, key: Hashable, value: Any) -> None:
        size = self._sizeof(value)
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= old[0]
            if size > self._max:
                return
            self._entries[key] = (size, Cached(value, int(self._wall())))
            self._bytes += size
            while self._bytes > self._max:
                _, (evicted, _) = self._entries.popitem(last=False)
                self._bytes -= evicted


result_cache = ResultCache()
