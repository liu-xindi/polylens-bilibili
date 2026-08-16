"""弹幕：list.so XML 接口 → Danmaku。"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from ..models import Danmaku
from ._constants import DANMAKU_XML_URL
from ._http import HttpClient, inflate_deflate


def fetch_danmaku_xml(client: HttpClient, cid: int) -> str:
    """抓取并解压某 cid 的弹幕 XML。"""
    return inflate_deflate(client.get_bytes(f"{DANMAKU_XML_URL}?oid={cid}"))


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


def top_by_heat(bullets: list[Danmaku], count: int) -> list[Danmaku]:
    """从全量弹幕取最热 count 条，再按时间轴升序返回。"""
    want = max(1, count)
    if want >= len(bullets):
        return sorted(bullets, key=lambda b: b.timestamp)
    hottest = sorted(bullets, key=lambda b: b.heat, reverse=True)[:want]
    return sorted(hottest, key=lambda b: b.timestamp)
