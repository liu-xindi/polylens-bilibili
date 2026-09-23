"""数据形状与 TOON 编码。

列表类数据以 TOON 表格串返回：表头声明列，其后逐行按列给值。列由数据类的字段派生，
故字段顺序即表头顺序。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


def space_url(mid: Any) -> str | None:
    """UP 主 mid → 空间链接；没有 mid 就没有链接。"""
    return f"https://space.bilibili.com/{mid}" if mid else None


def to_local_time(ts: int | None) -> str | None:
    """epoch 秒 → 本机时区的可读时间；0 与 None 同视为无时间戳，返回 None。

    面向消费者（LLM）优化：给可读字符串而非数字时间戳。
    """
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M")


def _toon_needs_quote(s: str) -> bool:
    """字符串是否需加引号。"""
    if s == "" or s != s.strip():
        return True
    if s in ("true", "false", "null"):
        return True
    if any(c in s for c in (",", '"', "\\", ":", "[", "]", "{", "}", "\n", "\r", "\t")):
        return True
    if s[0] == "-":
        return True
    try:
        float(s)
        return True  # 形似数字的字符串加引号，避免被当成数值
    except ValueError:
        return False


def _toon_cell(value: Any) -> str:
    """把单个值编码成 TOON 行内单元格。转义序列依 TOON 规范。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    s = str(value)
    if _toon_needs_quote(s):
        esc = (
            s.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{esc}"'
    return s


def toon_table(name: str, rows: list[dict[str, Any]], columns: list[str]) -> str:
    """把同构对象列表编码为 TOON 表格串。

    缺失键或 None 留空：消费端靠列位置对齐，不能跳过。
    """
    header = f"{name}[{len(rows)}]{{{','.join(columns)}}}:"
    lines = ["  " + ",".join(_toon_cell(r.get(c)) for c in columns) for r in rows]
    return "\n".join([header, *lines]) if lines else header


def to_toon(name: str, items: list[Any], item_type: type) -> str:
    """把数据类实例列表编码为 TOON 表格串。

    item_type 显式传入：列表为空时无从取得元素类型，而空表也需要表头。
    """
    columns = [f.name for f in fields(item_type)]
    return toon_table(name, [asdict(it) for it in items], columns)


class QrStatus(StrEnum):
    """扫码登录的轮询状态。与"当前是否已登录"是两回事，后者见 BilibiliClient.get_login_status。"""

    WAITING = "waiting"   # 尚未扫码
    SCANNED = "scanned"   # 已扫，待手机确认
    EXPIRED = "expired"   # 二维码超时
    SUCCESS = "success"


@dataclass(frozen=True, slots=True)
class QrLoginSession:
    key: str
    url: str


@dataclass(frozen=True, slots=True)
class LoginCheckResult:
    status: QrStatus
    cookie: str = ""


@dataclass(slots=True)
class Page[Item]:
    """分页取回的一批条目 + 续取状态。

    令牌是否透明由各能力实现决定，调用方一律原样递回。
    """

    items: list[Item]
    has_more: bool = False
    next_cursor: str | None = None


@dataclass(slots=True)
class Comment:
    """一条评论。楼中楼由 get_comment_replies 单独钻取，不内嵌于此。

    content 是平台原文：评论里的配图与链接标题另立字段，不改写原文。
    """

    id: str
    author: str
    content: str
    like_count: int
    reply_count: int
    parent_id: str | None  # 被回复的那条评论 id；仅楼中楼"互回"时出现（回楼主则为 None）
    created_at: str | None  # 本机时区可读时间（见 to_local_time）
    is_top: bool  # 置顶评论
    up_liked: bool  # UP 主给这条点过赞
    image_urls: str | None  # 配图地址，多张以空格分隔
    link_titles: str | None  # content 里的链接对应的标题，多个以空格分隔


@dataclass(slots=True)
class ReplyThread:
    """对某条主评论钻取楼中楼的一次结果。comment_id 是分组键。"""

    comment_id: str
    page: Page[Comment]
    withheld: int = 0  # 平台声称有、却不肯列出的回复条数（见 _comments._withheld_count）


@dataclass(slots=True)
class SearchItem:
    """搜索结果的一条。url 可直接传给内容类工具。"""

    title: str
    url: str
    author: str | None
    author_url: str | None  # UP 主空间链接，可传给 list_up_videos
    published_at: str | None  # 本机时区可读时间
    duration_sec: float | None  # 时长秒数（平台给的是"总分钟:秒"文本，已换算）
    view_count: int | None
    danmaku_count: int | None
    favorite_count: int | None


@dataclass(slots=True)
class FeedItem:
    """首页推荐流的一条。url 可直接传给内容类工具。"""

    title: str
    url: str
    author: str | None
    author_url: str | None  # UP 主空间链接
    published_at: str | None  # 本机时区可读时间
    duration_sec: float | None  # 时长秒数
    view_count: int | None
    rcmd_reason: str | None  # 平台给的推荐理由，多数条目没有


@dataclass(slots=True)
class UpVideoItem:
    """UP 主投稿列表的一条。url 可直接传给内容类工具。"""

    title: str
    url: str
    published_at: str | None  # 本机时区可读时间
    duration_sec: float | None  # 时长秒数
    view_count: int | None
    danmaku_count: int | None
    comment_count: int | None


@dataclass(slots=True)
class SubtitleEntry:
    """一条字幕。时间轴单位为秒，保留一位小数。"""

    start: float
    end: float
    content: str


@dataclass(slots=True)
class VideoPart:
    """多段视频的一段。"""

    page: int  # 分段序号，1 起
    part: str | None  # 该段标题
    duration: float | None  # 该段时长秒数


@dataclass(slots=True)
class Danmaku:
    """一条弹幕。

    不带 id：弹幕之间无引用关系，平台内部 id 对消费端零效用。
    heat 为 B站弹幕的热度档位（约 1-10），数值越高越热门。
    """

    content: str
    timestamp: float
    heat: int


class VideoInfo(BaseModel):
    """视频元信息。字段恒在，无值为 null。

    这里不写字段说明：outputSchema 的字段描述不进模型上下文（2026-08-24 实测），
    写在这里等于没写。需要模型知道的口径写在 server.py 的工具 description 里。
    """

    id: str
    title: str
    author: str | None = None
    author_url: str | None = None
    url: str | None = None
    published_at: str | None = None
    summary: str | None = None
    duration_sec: float | None = None          # 多段视频为当前段
    total_duration_sec: float | None = None    # 多段视频为全部分段之和
    view_count: int | None = None
    danmaku_count_total: int | None = None     # 多段视频为全部分段之和
    comment_count: int | None = None           # 含楼中楼
    like_count: int | None = None
    favorite_count: int | None = None
    share_count: int | None = None
    coin_count: int | None = None
    cover_url: str | None = None
    part_count: int | None = None
    category_id: int | None = None
    current_page: int | None = None            # 仅多段视频有值
    current_part: str | None = None            # 仅多段视频有值
