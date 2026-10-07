"""可重放的评论页缓存，进程内共享：http 部署下所有会话共用一份。

只缓存同一请求每次都返回同一页的接口：时间序主评论（游标带位置）与二级评论（按页码）。
热度序的进度记在平台侧，同一游标每次返回下一批，不能缓存。

缓存解析后的页，键里带账号：IP 属地等字段只有登录后才有，换账号不复用。
命中不续期，否则时间序第一页会一直不刷新。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any, NamedTuple

_TTL = 600.0
_MAX_PAGES = 1000


class Cached(NamedTuple):
    value: Any
    fetched_at: int  # epoch 秒，告诉调用方数据有多旧


class PageCache:
    def __init__(
        self,
        ttl: float = _TTL,
        max_pages: int = _MAX_PAGES,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._ttl = ttl
        self._max = max_pages
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._pages: OrderedDict[Hashable, tuple[float, Cached]] = OrderedDict()

    def get(self, key: Hashable) -> Cached | None:
        with self._lock:
            hit = self._pages.get(key)
            if hit is None:
                return None
            expires, cached = hit
            if self._clock() >= expires:
                del self._pages[key]
                return None
            self._pages.move_to_end(key)
            return cached

    def put(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._pages[key] = (self._clock() + self._ttl, Cached(value, int(self._wall())))
            self._pages.move_to_end(key)
            while len(self._pages) > self._max:
                self._pages.popitem(last=False)


page_cache = PageCache()
