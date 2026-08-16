"""MCP server：内容工具 + 搜索 + 登录工具。

参数语义写在 Field(description=...) 里由 inputSchema 承载，返回字段写在各返回模型上
由 outputSchema 承载，docstring 只说明工具做什么。
"""

from __future__ import annotations

import base64
import functools
import io
import json
import time
from collections.abc import Callable
from typing import Annotated, Any

import segno
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from .client import BilibiliClient, resolve_target
from .credentials import delete_cookie, load_cookie, save_cookie
from .errors import PolylensError
from .models import (
    BulletComment,
    Comment,
    ContentInfo,
    LoginStatus,
    SearchItem,
    SubtitleEntry,
    to_toon,
)

# ── 参数说明 ────────────────────────────────────────────────────────────────

_URL_DESC = "视频链接、b23.tv 短链，或裸 BV/av 号；含链接的分享文案也可直接传入。"
_PAGE_DESC = (
    "分段序号，1 起。不传时取链接里的 ?p=N，两者都没有则第 1 段。单段视频忽略此项。"
)
_CURSOR_DESC = "续取游标：不传从头开始，回传上次返回的 next_cursor 取下一批。"

# ── 返回模型 ────────────────────────────────────────────────────────────────

_TOON_NOTE = (
    "TOON 表格串：首行 表名[行数]{列名,…}: ，其后每行按列给值，空位表示该项无值。"
)


class ContentInfoResult(ContentInfo):
    elapsed_s: float | None = Field(default=None, description="服务端处理秒数")


class CommentsResult(BaseModel):
    video_id: str = Field(description="解析出的视频号，可用于确认链接指向")
    count: int = Field(description="本批返回的评论条数")
    comments: str = Field(
        description=(
            f"{_TOON_NOTE} 列为 id,author,content,like_count,reply_count,parent_id,created_at。"
            "id 可传给 get_comment_replies 钻取楼中楼；content 为空时可能是纯表情或图片，"
            "不代表这条评论没有内容。"
        )
    )
    has_more: bool = Field(description="true 表示平台还有更多，false 表示已抓全")
    next_cursor: str | None = Field(default=None, description="续取令牌，原样回传给 cursor")
    elapsed_s: float | None = Field(default=None, description="服务端处理秒数")


class ReplyThreadItem(BaseModel):
    comment_id: str = Field(description="所属主评论 id")
    replies: str = Field(description=f"{_TOON_NOTE} 列与 get_comments 的 comments 相同。")
    has_more: bool = Field(description="这个楼是否还有更多回复")
    next_cursor: str | None = Field(default=None, description="这个楼的续取令牌")


class CommentRepliesResult(BaseModel):
    video_id: str = Field(description="解析出的视频号")
    results: list[ReplyThreadItem] = Field(
        description=(
            "每个楼一项。翻页状态在每项里各一份，本工具没有顶层的 has_more；"
            "同次调用里没到底的楼可以把 id 一并带上，用同一个 next_cursor 整批续翻。"
        )
    )
    elapsed_s: float | None = Field(default=None, description="服务端处理秒数")


class BulletCommentsResult(BaseModel):
    video_id: str = Field(description="解析出的视频号")
    count: int = Field(description="本次返回的弹幕条数")
    bullet_comments: str = Field(
        description=(
            f"{_TOON_NOTE} 列为 content,timestamp,heat。timestamp 是视频内秒数，"
            "heat 是热度档位（约 1-10），数值越高越热门，同档内不再细分。"
        )
    )
    elapsed_s: float | None = Field(default=None, description="服务端处理秒数")


class SubtitlesResult(BaseModel):
    video_id: str = Field(description="解析出的视频号")
    count: int = Field(description="字幕条数")
    subtitles: str = Field(
        description=(
            f"{_TOON_NOTE} 列为 start,end,content，起止为视频内秒数，保留一位小数。"
        )
    )
    elapsed_s: float | None = Field(default=None, description="服务端处理秒数")


class SearchResult(BaseModel):
    count: int = Field(description="本批返回的条目数")
    results: str = Field(
        description=(
            f"{_TOON_NOTE} 列为 title,url,author,published_at,duration,view_count,danmaku_count。"
            "url 可直接传给内容类工具。"
        )
    )
    has_more: bool = Field(description="true 表示还有更多结果")
    next_cursor: str | None = Field(default=None, description="续取令牌，原样回传给 cursor")
    elapsed_s: float | None = Field(default=None, description="服务端处理秒数")


class LoginStatusResult(BaseModel):
    is_login: bool | None = Field(
        description="true 已登录，false 未登录，null 表示无法验证（网络或平台异常）"
    )


class CookieSavedResult(BaseModel):
    file: str = Field(description="凭据写入的文件路径")
    message: str = Field(description="结果说明")


class LogoutResult(BaseModel):
    deleted: bool = Field(description="true 表示原本存有凭据并已删除")
    message: str = Field(description="结果说明")


class QrCheckResult(BaseModel):
    status: str = Field(
        description="waiting 未扫码，scanned 已扫待手机确认，success 登录成功，expired 二维码过期"
    )
    message: str = Field(description="下一步该做什么")


# ── 工具层辅助 ──────────────────────────────────────────────────────────────


def _client() -> BilibiliClient:
    return BilibiliClient(load_cookie())


def _timed(fn: Callable[..., Any]) -> Callable[..., Any]:
    """测量服务端墙钟并挂到返回上。计的是服务端处理耗时，不含 MCP 传输与客户端排队。"""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        result = fn(*args, **kwargs)
        return _attach_elapsed(result, round(time.perf_counter() - t0, 3))

    return wrapper


def _attach_elapsed(result: Any, elapsed_s: float) -> Any:
    """模型返回直接设字段；内容块列表写进末尾的 JSON 元信息块。"""
    if isinstance(result, BaseModel):
        setattr(result, "elapsed_s", elapsed_s)  # noqa: B010
        return result
    if isinstance(result, list):
        for i in range(len(result) - 1, -1, -1):
            if getattr(result[i], "type", None) != "text":
                continue
            meta = json.loads(result[i].text)
            meta["elapsed_s"] = elapsed_s
            result[i] = TextContent(type="text", text=json.dumps(meta, ensure_ascii=False))
            break
    return result


def _png_block(data: bytes, mime: str) -> ImageContent:
    return ImageContent(type="image", mimeType=mime, data=base64.b64encode(data).decode())


def _make_qr_png(url: str) -> bytes:
    qr = segno.make(url, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=8, border=2)
    return buf.getvalue()


_SERVER_INSTRUCTIONS = (
    "本服务从 B 站视频中提取信息：元信息、评论、楼中楼、弹幕、字幕、视频帧，并支持按关键词搜索。"
    "内容类工具的 url 参数接受视频链接、b23.tv 短链或裸 BV/av 号。"
    "评论、楼中楼、字幕、视频帧需要登录，未登录时会明确报错；"
    "元信息、弹幕、搜索无需登录。登录用 set_cookie 写入浏览器 Cookie，或用 start_qr_login 扫码。"
    "翻页统一：has_more=true 时把同一处返回的 next_cursor 原样回传给 cursor 取下一批，"
    "=false 表示已到底；游标不透明，不要自造或解析。"
    "列表类数据以 TOON 表格串返回，表头固定，同一工具每次返回的列相同。"
    "日历时间按运行本机的时区呈现。内容类工具与搜索的返回附 elapsed_s，为服务端处理秒数。"
)

_STATUS_MSG = {
    LoginStatus.WAITING: (
        "尚未扫码，用 B站 App 扫描二维码。等用户确认已扫码后再调用本工具，不反复轮询。"
    ),
    LoginStatus.SCANNED: (
        "已扫码，在手机上确认登录。等用户确认完成后再调用本工具，不反复轮询。"
    ),
    LoginStatus.EXPIRED: "二维码已过期，重新调用 start_qr_login 获取新二维码。",
}

_READS_PLATFORM = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
_WRITES_CREDENTIAL = ToolAnnotations(readOnlyHint=False, openWorldHint=True)
_LOCAL_ONLY = ToolAnnotations(readOnlyHint=False, openWorldHint=False)


def create_server(
    *,
    host: str | None = None,
    port: int | None = None,
    public_url: str | None = None,
    auth_secret: str | None = None,
) -> FastMCP:
    # 省略时沿用 FastMCP 默认，stdio 下用不到。
    net: dict[str, Any] = {}
    if host is not None:
        net["host"] = host
    if port is not None:
        net["port"] = port

    # 两者缺一即不开 OAuth，保持无鉴权供本机调试。
    oauth_provider = None
    if public_url and auth_secret:
        from .oauth import build_oauth
        auth_kwargs, oauth_provider = build_oauth(public_url)
        net.update(auth_kwargs)

    mcp = FastMCP("polylens-bilibili", instructions=_SERVER_INSTRUCTIONS, **net)

    if oauth_provider is not None and auth_secret:
        from .oauth import register_consent_route
        register_consent_route(mcp, oauth_provider, auth_secret)

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def get_content_info(
        url: Annotated[str, Field(description=_URL_DESC)],
        page: Annotated[int | None, Field(description=_PAGE_DESC)] = None,
    ) -> ContentInfoResult:
        """获取视频的标题、作者、发布时间、简介与各项统计。"""
        video_id, part = resolve_target(url, page)
        info = _client().get_content_info(video_id, part)
        return ContentInfoResult(**info.model_dump())

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def get_comments(
        url: Annotated[str, Field(description=_URL_DESC)],
        count: Annotated[
            int,
            Field(
                description=(
                    "想要的主评论条数。按整页抓取，页内可能有条目被平台过滤，"
                    "故实际返回可多可少。评论按热度序返回，置顶评论排在第一页最前。"
                )
            ),
        ],
        cursor: Annotated[str | None, Field(description=_CURSOR_DESC)] = None,
    ) -> CommentsResult:
        """获取视频的主评论，不含楼中楼。需要登录。"""
        video_id, _ = resolve_target(url)
        page = _client().get_comments(video_id, count=count, cursor=cursor)
        return CommentsResult(
            video_id=video_id,
            count=len(page.items),
            comments=to_toon("comments", page.items, Comment),
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def get_comment_replies(
        url: Annotated[str, Field(description=_URL_DESC)],
        comment_ids: Annotated[
            list[str],
            Field(description="主评论 id 列表，从 get_comments 返回的 comments 表里取。"),
        ],
        limit: Annotated[
            int,
            Field(
                description=(
                    "每个楼取多少条回复，超出的截断，用 cursor 续取。"
                    "楼中楼串行抓取且平台单页 20 条，limit 与楼数一起决定请求次数与耗时。"
                )
            ),
        ],
        cursor: Annotated[str | None, Field(description=_CURSOR_DESC)] = None,
    ) -> CommentRepliesResult:
        """按主评论 id 钻取楼中楼。需要登录。"""
        video_id, _ = resolve_target(url)
        threads = _client().get_comment_replies(
            video_id, comment_ids=comment_ids, limit=limit, cursor=cursor
        )
        return CommentRepliesResult(
            video_id=video_id,
            results=[
                ReplyThreadItem(
                    comment_id=t.comment_id,
                    replies=to_toon("replies", t.page.items, Comment),
                    has_more=t.page.has_more,
                    next_cursor=t.page.next_cursor,
                )
                for t in threads
            ],
        )

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def get_bullet_comments(
        url: Annotated[str, Field(description=_URL_DESC)],
        count: Annotated[
            int,
            Field(
                description=(
                    "想要的弹幕条数。从整片弹幕里取最热的这么多条，结果仍按时间轴排序；"
                    "达到或超过弹幕总数即返回全部。总数见 get_content_info 的 danmaku_count。"
                )
            ),
        ],
        page: Annotated[int | None, Field(description=_PAGE_DESC)] = None,
    ) -> BulletCommentsResult:
        """获取视频弹幕，按热度取一批，仍按时间轴排序。"""
        video_id, part = resolve_target(url, page)
        bullets = _client().get_bullet_comments(video_id, count=count, page=part)
        return BulletCommentsResult(
            video_id=video_id,
            count=len(bullets),
            bullet_comments=to_toon("bullet_comments", bullets, BulletComment),
        )

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def get_subtitles(
        url: Annotated[str, Field(description=_URL_DESC)],
        page: Annotated[int | None, Field(description=_PAGE_DESC)] = None,
    ) -> SubtitlesResult:
        """获取视频字幕，逐句返回。字幕为 AI 生成，可能有误。需要登录。"""
        video_id, part = resolve_target(url, page)
        entries = _client().get_subtitles(video_id, part)
        return SubtitlesResult(
            video_id=video_id,
            count=len(entries),
            subtitles=to_toon("subtitles", entries, SubtitleEntry),
        )

    # structured_output=False：FastMCP 默认会把返回值复制进 structuredContent，对本工具即把图片的
    # base64 又装一份；部分客户端会将它序列化为文本传给模型，同一张图被当文本重复计入上下文。
    @mcp.tool(structured_output=False, annotations=_READS_PLATFORM)
    @_timed
    def get_frame(
        url: Annotated[str, Field(description=_URL_DESC)],
        timestamp: Annotated[
            float, Field(description="视频内秒数，如 10.5。超过该段时长会报错。")
        ],
        page: Annotated[int | None, Field(description=_PAGE_DESC)] = None,
    ) -> list[ImageContent | TextContent]:
        """截取视频指定时刻的一帧，返回内联 JPEG 图片。需要登录，需本机安装 ffmpeg。"""
        video_id, part = resolve_target(url, page)
        jpeg = _client().get_frame(video_id, timestamp=timestamp, page=part)
        meta = {"video_id": video_id, "page": part}
        return [
            _png_block(jpeg, "image/jpeg"),
            TextContent(type="text", text=json.dumps(meta, ensure_ascii=False)),
        ]

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def search(
        query: Annotated[str, Field(description="搜索关键词。")],
        count: Annotated[
            int,
            Field(
                description=(
                    "本批取多少条，从 cursor 位置连续取。"
                    "平台单次分页有上限，超出会被平台拒绝并报错。"
                )
            ),
        ],
        cursor: Annotated[str | None, Field(description=_CURSOR_DESC)] = None,
    ) -> SearchResult:
        """按关键词搜索视频，每条附链接，可传给内容类工具继续取评论、字幕等。"""
        page = _client().search(query=query, count=count, cursor=cursor)
        return SearchResult(
            count=len(page.items),
            results=to_toon("results", page.items, SearchItem),
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    @mcp.tool(annotations=_READS_PLATFORM)
    def verify_login() -> LoginStatusResult:
        """查询当前是否已登录（联网核验本地凭据是否仍然有效）。"""
        return LoginStatusResult(is_login=_client().verify_login())

    @mcp.tool(annotations=_LOCAL_ONLY)
    def set_cookie(
        cookie: Annotated[
            str,
            Field(description="从浏览器复制的整段 Cookie，单行。这是敏感凭据，会出现在对话中。"),
        ],
    ) -> CookieSavedResult:
        """写入 B站登录 Cookie，即时生效。"""
        value = cookie.strip()
        if not value:
            raise PolylensError("cookie 不能为空；清除登录用 logout")
        path = save_cookie(value)
        return CookieSavedResult(
            file=str(path),
            message=f"Cookie 已写入 {path}，即时生效；可调 verify_login 确认。",
        )

    @mcp.tool(annotations=_LOCAL_ONLY)
    def logout() -> LogoutResult:
        """退出登录，删除本地保存的 Cookie。"""
        deleted = delete_cookie()
        return LogoutResult(
            deleted=deleted,
            message="已退出登录，本地凭据已删除。" if deleted else "本地没有保存的凭据。",
        )

    # structured_output=False 同 get_frame：二维码内联返回，不进结构化通道。
    @mcp.tool(structured_output=False, annotations=_READS_PLATFORM)
    def start_qr_login() -> list[ImageContent | TextContent]:
        """发起扫码登录，返回内联二维码图片。只发码，立即返回，不轮询。"""
        session = BilibiliClient().start_qr_login()
        meta = {
            "key": session.key,
            "message": "用 B站 App 扫描这张二维码，扫完并在手机上确认后调用 check_qr_login。",
            "next_action": {"tool": "check_qr_login", "args": {"key": session.key}},
        }
        return [
            _png_block(_make_qr_png(session.url), "image/png"),
            TextContent(type="text", text=json.dumps(meta, ensure_ascii=False)),
        ]

    @mcp.tool(annotations=_WRITES_CREDENTIAL)
    def check_qr_login(
        key: Annotated[str, Field(description="start_qr_login 返回的 key。")],
    ) -> QrCheckResult:
        """查询扫码结果。每次只查一次，成功时凭据自动写盘。"""
        result = BilibiliClient().check_qr_login(key)
        if result.status is LoginStatus.SUCCESS:
            path = save_cookie(result.cookie)
            return QrCheckResult(
                status="success",
                message=f"登录成功，凭据已写入 {path}，后续工具直接读取。",
            )
        return QrCheckResult(
            status=result.status.value,
            message=_STATUS_MSG.get(result.status, "未知状态"),
        )

    return mcp
