"""弹幕：list.so XML 接口 → Danmaku。"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from ..errors import BilibiliError, RateLimitedError
from ..models import Danmaku
from ._constants import ENDPOINTS
from ._http import HttpClient, _RateLimited, inflate_deflate


def fetch_danmaku_xml(client: HttpClient, cid: int) -> str:
    """抓取并解压某 cid 的弹幕 XML。"""
    try:
        raw = client.get_api_bytes(ENDPOINTS["danmaku_xml"], {"oid": cid})
    except _RateLimited as e:
        raise RateLimitedError(e.describe("取弹幕")) from None
    return inflate_deflate(raw)


def parse_danmaku_xml(xml: str) -> list[Danmaku]:
    """解析弹幕 XML 为 Danmaku 列表（按视频内时间升序）。

    取 content/timestamp/heat；字号、颜色、滚动方式、弹幕池、发送者哈希等纯渲染属性不参与分析，
    不返回（弹幕常达上千条，省略可明显减小体积）。
    """
    root = ET.fromstring(xml.encode("utf-8"))
    result: list[Danmaku] = []
    for node in root.findall("d"):
        p = node.get("p")
        if not p:
            continue
        fields = p.split(",")
        if len(fields) < 9:
            continue
        result.append(
            Danmaku(
                content=(node.text or "").strip(),
                timestamp=float(fields[0]),
                heat=int(fields[8]),  # p 第 9 字段：B站弹幕热度档位（1-10 十分位）
            )
        )
    result.sort(key=lambda b: b.timestamp)
    return result


def fetch_danmaku(client: HttpClient, cid: int) -> list[Danmaku]:
    """抓取并解析某视频的全部弹幕。"""
    return parse_danmaku_xml(fetch_danmaku_xml(client, cid))


def _spread_over_time(bullets: list[Danmaku], count: int) -> list[Danmaku]:
    """从时间升序的一批里按时间轴等距挑 count 条。

    指针只向前走，且每步都给后面留够剩余名额，所以挑出来的互不重复、仍是时间升序。
    """
    total = len(bullets)
    if count >= total:
        return list(bullets)
    lo, hi = bullets[0].timestamp, bullets[-1].timestamp
    if count == 1 or hi <= lo:
        return list(bullets[:count])
    picked: list[Danmaku] = []
    j = 0
    for i in range(count):
        target = lo + (hi - lo) * i / (count - 1)
        limit = total - (count - i)  # 再往前走就不够挑了
        while j < limit and abs(bullets[j + 1].timestamp - target) <= abs(
            bullets[j].timestamp - target
        ):
            j += 1
        picked.append(bullets[j])
        j += 1
    return picked


def top_by_heat(bullets: list[Danmaku], count: int) -> list[Danmaku]:
    """从全量弹幕取最热 count 条，再按时间轴升序返回。

    heat 只有十档，且实测每档条数相等——它是平台按分位打的标记，不是连续的热度值。
    于是请求条数小于一档规模时，整批都落在最高档里，同档之间无从比较。
    这时按时间轴等距取，而不是取最早的那几条：后者会让一个半小时的分段取 20 条
    全落在开头两分钟。
    """
    if count < 1:
        raise BilibiliError(f"count 需为正整数，收到 {count}")
    by_time = sorted(bullets, key=lambda b: b.timestamp)
    if count >= len(by_time):
        return by_time
    ranked = sorted(by_time, key=lambda b: b.heat, reverse=True)
    cutoff = ranked[count - 1].heat
    above = [b for b in by_time if b.heat > cutoff]
    tier = [b for b in by_time if b.heat == cutoff]
    return sorted(above + _spread_over_time(tier, count - len(above)),
                  key=lambda b: b.timestamp)
