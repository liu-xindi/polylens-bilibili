"""MCP server：内容工具 + 搜索 + 登录工具。

模型能看到的只有三处：工具 docstring、inputSchema 的参数说明、server instructions。
outputSchema 不进模型上下文，故返回模型只声明结构，字段口径写在 docstring 里。
"""

from __future__ import annotations

import base64
import functools
import io
import json
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal

import segno
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from .client import BilibiliClient, resolve_up, resolve_video
from .credentials import delete_cookie, load_cookie, save_cookie
from .models import (
    Comment,
    Danmaku,
    FeedItem,
    QrStatus,
    SearchItem,
    SubtitleEntry,
    UpVideoItem,
    VideoInfo,
    VideoPart,
    to_toon,
)

# ── 参数说明 ────────────────────────────────────────────────────────────────

_URL_DESC = "视频链接、b23.tv 短链，或裸 BV/av 号；含链接的分享文案也可直接传入。"
_PAGE_DESC = "分段序号，1 起。不传时取链接里的 ?p=N，两者都没有则第 1 段。"
_CURSOR_DESC = "续取游标：不传从头开始，回传上次返回的 next_cursor 取下一批。"

# ── 返回模型 ────────────────────────────────────────────────────────────────
#
# 这些模型只声明结构，不写字段说明。outputSchema 的字段描述不进模型上下文
# （2026-08-24 用单标记三通道对照实测：description 与 inputSchema 的描述进，
# outputSchema 的不进），写在这里等于没写。模型需要知道的口径写在工具的
# description 与参数说明里；outputSchema 保留下来只作校验契约。


class VideoInfoResult(VideoInfo):
    elapsed_s: float | None = None


class CommentsResult(BaseModel):
    video_id: str
    count: int
    comments: str
    has_more: bool
    next_cursor: str | None = None
    elapsed_s: float | None = None


class ReplyThreadItem(BaseModel):
    comment_id: str
    replies: str
    has_more: bool
    withheld: int = 0


class CommentRepliesResult(BaseModel):
    video_id: str
    results: list[ReplyThreadItem]
    elapsed_s: float | None = None


class DanmakuResult(BaseModel):
    video_id: str
    count: int
    danmaku: str
    elapsed_s: float | None = None


class SubtitlesResult(BaseModel):
    video_id: str
    count: int
    lang: str | None = None
    available_langs: list[str] = Field(default_factory=list)
    subtitles: str
    elapsed_s: float | None = None


class PartsResult(BaseModel):
    video_id: str
    count: int
    parts: str
    elapsed_s: float | None = None


class SearchResult(BaseModel):
    count: int
    results: str
    has_more: bool
    next_cursor: str | None = None
    elapsed_s: float | None = None


class SuggestResult(BaseModel):
    count: int
    suggestions: list[str]
    elapsed_s: float | None = None


class UpVideosResult(BaseModel):
    author: str | None
    author_url: str
    count: int
    videos: str
    has_more: bool
    next_cursor: str | None = None
    elapsed_s: float | None = None


class FeedResult(BaseModel):
    count: int
    feed: str
    elapsed_s: float | None = None


class LoginStateResult(BaseModel):
    is_login: bool | None


class LogoutResult(BaseModel):
    deleted: bool
    message: str


class QrCheckResult(BaseModel):
    status: str
    message: str


# ── 工具层辅助 ──────────────────────────────────────────────────────────────


def _client() -> BilibiliClient:
    return BilibiliClient(load_cookie())


def _resolve(url: str, page: int | None = None) -> tuple[str, int]:
    """解析出视频号与分段序号。短链展开要带 cookie 才能过平台对网页域名的风控。"""
    return resolve_video(url, page, cookie=load_cookie())


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


_SERVER_INSTRUCTIONS = "读取 B 站视频信息的工具集。"

_STATUS_MSG = {
    QrStatus.WAITING: (
        "尚未扫码，用 B站 App 扫描二维码。等用户确认已扫码后再调用本工具，不反复轮询。"
    ),
    QrStatus.SCANNED: (
        "已扫码，在手机上确认登录。等用户确认完成后再调用本工具，不反复轮询。"
    ),
    QrStatus.EXPIRED: "二维码已过期，重新调用 start_qr_login 获取新二维码。",
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
    def get_video_info(
        url: Annotated[str, Field(description=_URL_DESC)],
        page: Annotated[int | None, Field(description=_PAGE_DESC)] = None,
    ) -> VideoInfoResult:
        """获取视频的标题、作者、发布时间、简介与各项统计。

        统计口径：弹幕数与整片时长是全部分段之和，当前段时长只算这一段。

        (video info, metadata, stats)
        """
        video_id, part = _resolve(url, page)
        info = _client().get_video_info(video_id, part)
        return VideoInfoResult(**info.model_dump())

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def get_comments(
        url: Annotated[str, Field(description=_URL_DESC)],
        count: Annotated[
            int,
            Field(description="想要的主评论条数。"),
        ],
        cursor: Annotated[str | None, Field(description=_CURSOR_DESC)] = None,
        mode: Annotated[
            Literal["hot", "newest"],
            Field(
                description=(
                    "排序方式。hot 的游标中断后无法从原处接续，要翻完全部评论用 newest。"
                )
            ),
        ] = "hot",
    ) -> CommentsResult:
        """获取视频的主评论，不含楼中楼。(video comments)"""
        video_id, _ = _resolve(url)
        page = _client().get_comments(video_id, count=count, cursor=cursor, sort=mode)
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
            int | None,
            Field(
                description=(
                    "每个楼只取最早的多少条回复，has_more 表示被截断。不传则取完整个楼。"
                )
            ),
        ] = None,
    ) -> CommentRepliesResult:
        """按主评论 id 钻取楼中楼。

        parent_id 为空表示直接回复主评论，否则是所回复的那条楼中楼回复的 id。
        parent_id 指向的回复不在列表里时，那条被平台隐藏了，取不到。
        withheld 是整个楼里被隐藏的回复条数。

        (comment replies, sub-replies, thread)
        """
        video_id, _ = _resolve(url)
        threads = _client().get_comment_replies(video_id, comment_ids=comment_ids, limit=limit)
        return CommentRepliesResult(
            video_id=video_id,
            results=[
                ReplyThreadItem(
                    comment_id=t.comment_id,
                    # 评论只有两层，楼中楼回复的 reply_count 恒为 0
                    replies=to_toon(
                        "replies", t.page.items, Comment, exclude=frozenset({"reply_count"})
                    ),
                    has_more=t.page.has_more,
                    withheld=t.withheld,
                )
                for t in threads
            ],
        )

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def get_danmaku(
        url: Annotated[str, Field(description=_URL_DESC)],
        count: Annotated[
            int,
            Field(
                description=(
                    "想要的弹幕条数。取该段里 heat 最高的这么多条，"
                    "达到或超过该段弹幕总数即返回全部。"
                    "heat 是平台给每条弹幕的标记，约 1-10 的档位。"
                )
            ),
        ],
        page: Annotated[int | None, Field(description=_PAGE_DESC)] = None,
    ) -> DanmakuResult:
        """获取视频弹幕。(danmaku, bullet comments)"""
        video_id, part = _resolve(url, page)
        bullets = _client().get_danmaku(video_id, count=count, page=part)
        return DanmakuResult(
            video_id=video_id,
            count=len(bullets),
            danmaku=to_toon("danmaku", bullets, Danmaku),
        )

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def get_subtitles(
        url: Annotated[str, Field(description=_URL_DESC)],
        page: Annotated[int | None, Field(description=_PAGE_DESC)] = None,
        lang: Annotated[
            str | None,
            Field(
                description=(
                    "轨道语种，如 zh-CN、en-US、ai-zh。不传则取平台给的第一条，"
                    "而各段的轨道构成可能不同，要跨段拿同一语种就显式指定。"
                    "可选值见返回的 available_langs。"
                )
            ),
        ] = None,
    ) -> SubtitlesResult:
        """获取视频字幕，逐句返回。字幕可能为 AI 生成或机器翻译，存在误差。

        多段视频常只有一部分分段有字幕。

        (subtitles, captions, transcript)
        """
        video_id, part = _resolve(url, page)
        track = _client().get_subtitles(video_id, part, lang)
        return SubtitlesResult(
            video_id=video_id,
            count=len(track.entries),
            lang=track.lang,
            available_langs=track.available_langs,
            subtitles=to_toon("subtitles", track.entries, SubtitleEntry),
        )

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def get_parts(
        url: Annotated[str, Field(description=_URL_DESC)],
    ) -> PartsResult:
        """列出多段视频（分 P）的全部分段。(video parts, pages)"""
        video_id, _ = _resolve(url)
        parts = _client().get_parts(video_id)
        return PartsResult(
            video_id=video_id,
            count=len(parts),
            parts=to_toon("parts", parts, VideoPart),
        )

    # structured_output=False：FastMCP 默认会把返回值复制进 structuredContent，对本工具即把图片的
    # base64 又装一份；部分客户端会将它序列化为文本传给模型，同一张图被当文本重复计入上下文。
    @mcp.tool(structured_output=False, annotations=_READS_PLATFORM)
    @_timed
    def get_frame(
        url: Annotated[str, Field(description=_URL_DESC)],
        timestamp: Annotated[
            float, Field(description="视频内秒数，如 10.5。")
        ],
        page: Annotated[int | None, Field(description=_PAGE_DESC)] = None,
    ) -> list[ImageContent | TextContent]:
        """截取视频指定时刻的一帧，返回内联 JPEG 图片。(video frame, screenshot)"""
        video_id, part = _resolve(url, page)
        jpeg = _client().get_frame(video_id, timestamp=timestamp, page=part)
        meta = {"video_id": video_id, "page": part}
        return [
            _png_block(jpeg, "image/jpeg"),
            TextContent(type="text", text=json.dumps(meta, ensure_ascii=False)),
        ]

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def search_videos(
        query: Annotated[
            str,
            Field(description="搜索关键词。"),
        ],
        cursor: Annotated[str | None, Field(description=_CURSOR_DESC)] = None,
        order: Annotated[
            Literal["relevance", "newest", "most_viewed", "most_danmaku", "most_favorited"],
            Field(description="排序。"),
        ] = "relevance",
    ) -> SearchResult:
        """按关键词搜索 B 站视频，每批最多 30 条。

        (search videos, find video by keyword)
        """
        page = _client().search(query=query, cursor=cursor, order=order)
        return SearchResult(
            count=len(page.items),
            results=to_toon("results", page.items, SearchItem),
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def suggest_keywords(
        term: Annotated[str, Field(description="已输入的关键词，可以只是开头几个字。")],
    ) -> SuggestResult:
        """给出 B 站搜索框的联想建议词，最多 10 条。

        (search suggestions, autocomplete, related keywords)
        """
        suggestions = _client().suggest(term)
        return SuggestResult(count=len(suggestions), suggestions=suggestions)

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def list_up_videos(
        author_url: Annotated[
            str, Field(description="UP 主空间链接（其他工具返回的 author_url），或数字 mid。")
        ],
        cursor: Annotated[str | None, Field(description=_CURSOR_DESC)] = None,
        order: Annotated[
            Literal["newest", "most_viewed", "most_favorited"],
            Field(description="排序。"),
        ] = "newest",
        keyword: Annotated[
            str | None,
            Field(description="按关键词筛选投稿。"),
        ] = None,
    ) -> UpVideosResult:
        """列出 UP 主的投稿视频，每批 40 条。

        (uploader videos, channel uploads, other videos by this author)
        """
        mid = resolve_up(author_url)
        author, page = _client().get_up_videos(mid, cursor=cursor, order=order, keyword=keyword)
        return UpVideosResult(
            author=author,
            author_url=f"https://space.bilibili.com/{mid}",
            count=len(page.items),
            videos=to_toon("videos", page.items, UpVideoItem),
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def get_feed() -> FeedResult:
        """刷 B 站首页推荐流，每批最多 30 条。

        登录后按账号口味推，未登录给通用推荐。

        (homepage feed, recommendations, browse)
        """
        items = _client().get_feed()
        return FeedResult(count=len(items), feed=to_toon("feed", items, FeedItem))

    @mcp.tool(annotations=_READS_PLATFORM)
    def get_login_status() -> LoginStateResult:
        """查询当前是否已登录。

        is_login 为 null 表示无法验证，与 false 不同。

        (login status)
        """
        return LoginStateResult(is_login=_client().get_login_status())

    @mcp.tool(annotations=_LOCAL_ONLY)
    def logout() -> LogoutResult:
        """退出登录，删除本地保存的 Cookie。(log out, sign out)"""
        deleted = delete_cookie()
        return LogoutResult(
            deleted=deleted,
            message="已退出登录，本地凭据已删除。" if deleted else "本地没有保存的凭据。",
        )

    # structured_output=False 同 get_frame：二维码内联返回，不进结构化通道。
    @mcp.tool(structured_output=False, annotations=_READS_PLATFORM)
    def start_qr_login() -> list[ImageContent | TextContent]:
        """发起扫码登录，返回内联二维码图片。只发码，立即返回，不轮询。(QR code login)"""
        session = BilibiliClient().start_qr_login()
        meta = {
            "key": session.key,
            "message": "用 B站 App 扫描这张二维码，扫完并在手机上确认后调用 complete_qr_login。",
        }
        return [
            _png_block(_make_qr_png(session.url), "image/png"),
            TextContent(type="text", text=json.dumps(meta, ensure_ascii=False)),
        ]

    @mcp.tool(annotations=_WRITES_CREDENTIAL)
    def complete_qr_login(
        key: Annotated[str, Field(description="start_qr_login 返回的 key。")],
    ) -> QrCheckResult:
        """查询扫码结果，已确认则登录即刻生效。

        (finish QR code login, poll QR status)
        """
        result = BilibiliClient().check_qr_login(key)
        if result.status is QrStatus.SUCCESS:
            save_cookie(result.cookie)
            return QrCheckResult(status="success", message="登录成功。")
        return QrCheckResult(
            status=result.status.value,
            message=_STATUS_MSG.get(result.status, "未知状态"),
        )

    return mcp
