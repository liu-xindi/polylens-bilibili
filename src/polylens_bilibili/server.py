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
from dataclasses import fields
from typing import Annotated, Any, Literal

import segno
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from .client import BilibiliClient, resolve_up, resolve_video
from .credentials import delete_cookie, load_cookie, save_cookie
from .jqfilter import encode_items
from .models import (
    Comment,
    Danmaku,
    FeedItem,
    QrStatus,
    SearchItem,
    SubtitleEntry,
    UpInfo,
    UpVideoItem,
    VideoInfo,
    VideoPart,
    to_toon,
)

# ── 参数说明 ────────────────────────────────────────────────────────────────

_URL_DESC = "视频链接、b23.tv 短链，或裸 BV/av 号；含链接的分享文案也可直接传入。"
_PAGE_DESC = (
    "分段序号，1 起。不传时取链接里的 ?p=N，两者都没有则第 1 段。单段视频忽略此项。"
)
_CURSOR_DESC = "续取游标：不传从头开始，回传上次返回的 next_cursor 取下一批。"

# 评论只有两层，二级评论的 reply_count 恒为 0
_REPLY_EXCLUDE = frozenset({"reply_count"})


def _jq_desc(
    item_type: type,
    *,
    paged: bool,
    scope: str | None = None,
    exclude: frozenset[str] = frozenset(),
) -> str:
    scope = scope or ("本批条目" if paged else "全部条目")
    shown = [f for f in fields(item_type) if f.name not in exclude]
    columns = ",".join(f.name for f in shown)
    bulky = "、".join(
        f.name + (f"（{f.metadata['bulky']}）" if f.metadata["bulky"] else "")
        for f in shown
        if "bulky" in f.metadata
    )
    paging = "分页字段不在输入里；只筛本批，筛完为空时仍以 has_more 判断有无下一批。"
    return (
        f"可选的 jq 表达式。输入是{scope}组成的数组，每条字段：{columns}。"
        + (f"体积大、多数任务用不到的字段：{bulky}；特定任务需要时照常使用。" if bulky else "")
        + (paging if paged else "")
        + "结果为单个字符串时原样返回。"
    )


# ── 返回模型 ────────────────────────────────────────────────────────────────
#
# 这些模型只声明结构，不写字段说明。outputSchema 的字段描述不进模型上下文
# （2026-08-24 用单标记三通道对照实测：description 与 inputSchema 的描述进，
# outputSchema 的不进），写在这里等于没写。模型需要知道的口径写在工具的
# description 与参数说明里；outputSchema 保留下来只作校验契约。


class _Timed(BaseModel):
    elapsed_s: float | None = None


class VideoInfoResult(VideoInfo, _Timed):
    pass


class CommentsResult(_Timed):
    video_id: str
    count: int
    comments: str
    jq_count: int | None = None
    has_more: bool
    next_cursor: str | None = None
    message: str | None = None


class ReplyThreadItem(BaseModel):
    comment_id: str
    replies: str
    jq_count: int | None = None
    has_more: bool
    withheld: int = 0
    error: str | None = None


class CommentRepliesResult(_Timed):
    video_id: str
    results: list[ReplyThreadItem]


class DanmakuResult(_Timed):
    video_id: str
    count: int
    danmaku: str


class SubtitlesResult(_Timed):
    video_id: str
    count: int
    lang: str | None = None
    available_langs: list[str] = Field(default_factory=list)
    subtitles: str
    jq_count: int | None = None


class PartsResult(_Timed):
    video_id: str
    count: int
    parts: str
    jq_count: int | None = None


class SearchResult(_Timed):
    count: int
    results: str
    jq_count: int | None = None
    has_more: bool
    next_cursor: str | None = None


class SuggestResult(_Timed):
    count: int
    suggestions: list[str]


class UpVideosResult(_Timed):
    author: str | None
    author_url: str
    total: int
    count: int
    videos: str
    jq_count: int | None = None
    has_more: bool
    next_cursor: str | None = None


class UpInfoResult(UpInfo, _Timed):
    pass


class FeedResult(_Timed):
    count: int
    feed: str
    jq_count: int | None = None


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
            meta = {"elapsed_s": elapsed_s, **json.loads(result[i].text)}
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
        """含标题、作者、发布时间、简介与各项统计。

        统计口径：评论数含二级评论；弹幕数与整片时长是全部分段之和，
        当前段时长只算这一段。

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
            Field(
                description=(
                    "至少取多少条主评论。平台按每页约 20 条整页返回，实际条数约为 20 的整数倍；"
                    "评论不够时返回剩余的全部。"
                )
            ),
        ],
        cursor: Annotated[str | None, Field(description=_CURSOR_DESC)] = None,
        mode: Annotated[
            Literal["hot", "newest"],
            Field(
                description=(
                    "排序方式：hot 是平台的综合排序。"
                    "hot 的 cursor 只标识浏览会话，进度记在平台侧，"
                    "同一 cursor 每次调用都返回下一批，不能重放某一批；"
                    "对同一视频不传 cursor 重新开始 hot，此前 hot cursor 的进度会退回开头附近，"
                    "之后返回的是已取过的内容。"
                    "newest 按时间倒序，cursor 含位置，可重复取同一批，不受新会话影响。"
                    "需要完整抓取或断点续取时用 newest。"
                )
            ),
        ] = "hot",
        jq: Annotated[
            str | None,
            Field(description=_jq_desc(Comment, paged=True)),
        ] = None,
    ) -> CommentsResult:
        """不含二级评论，二级评论通过 get_comment_replies 获取。需要登录。(video comments)"""
        video_id, _ = _resolve(url)
        page = _client().get_comments(video_id, count=count, cursor=cursor, sort=mode)
        comments, jq_count = encode_items("comments", page.items, Comment, jq)
        return CommentsResult(
            video_id=video_id,
            count=len(page.items),
            comments=comments,
            jq_count=jq_count,
            has_more=page.has_more,
            next_cursor=page.next_cursor,
            message=page.rate_limited,
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
                    "每条主评论只取最早的多少条二级评论，has_more 表示被截断。不传则全部取完。"
                )
            ),
        ] = None,
        jq: Annotated[
            str | None,
            Field(
                description=_jq_desc(
                    Comment, paged=False, scope="单条主评论下的二级评论", exclude=_REPLY_EXCLUDE
                )
            ),
        ] = None,
    ) -> CommentRepliesResult:
        """二级评论按时间正序排列。需要登录。

        withheld 是该主评论下平台未列出的二级评论条数（已删除或被折叠），不随 limit 变化。
        某条主评论取不到（评论不存在、不属于这个视频）时只在它的 error 里说明，其他照常返回。
        中途被风控或限流时，已取完的照常返回，其余的 error 里说明原因。
        parent_id 为空表示直接回复主评论，否则是所回复的那条二级评论的 id。
        parent_id 指向的二级评论不在列表里时，那条被平台隐藏了，取不到。

        (comment replies, sub-replies, thread)
        """
        video_id, _ = _resolve(url)
        threads = _client().get_comment_replies(video_id, comment_ids=comment_ids, limit=limit)
        results = []
        for t in threads:
            replies, jq_count = encode_items(
                "replies", t.page.items, Comment, jq, exclude=_REPLY_EXCLUDE
            )
            results.append(
                ReplyThreadItem(
                    comment_id=t.comment_id,
                    replies=replies,
                    jq_count=jq_count,
                    has_more=t.page.has_more,
                    withheld=t.withheld,
                    error=t.error,
                )
            )
        return CommentRepliesResult(video_id=video_id, results=results)

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def get_danmaku(
        url: Annotated[str, Field(description=_URL_DESC)],
        count: Annotated[
            int,
            Field(
                description=(
                    "取多少条弹幕。取该段里 heat 最高的这么多条，结果仍按时间轴排序；"
                    "达到或超过该段弹幕总数即返回全部。"
                    "heat 是平台给每条弹幕的标记，约 1-10 的档位，同档内不再细分。"
                )
            ),
        ],
        page: Annotated[int | None, Field(description=_PAGE_DESC)] = None,
    ) -> DanmakuResult:
        """(danmaku, bullet comments)"""
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
                    "轨道语种，如 zh-CN、en-US、ai-zh。不传则取平台给的第一条。"
                    "可选值见返回的 available_langs。"
                )
            ),
        ] = None,
        jq: Annotated[
            str | None,
            Field(description=_jq_desc(SubtitleEntry, paged=False)),
        ] = None,
    ) -> SubtitlesResult:
        """逐句返回。需要登录。字幕可能为 AI 生成或机器翻译，存在误差。

        (subtitles, captions, transcript)
        """
        video_id, part = _resolve(url, page)
        track = _client().get_subtitles(video_id, part, lang)
        subtitles, jq_count = encode_items("subtitles", track.entries, SubtitleEntry, jq)
        return SubtitlesResult(
            video_id=video_id,
            count=len(track.entries),
            lang=track.lang,
            available_langs=track.available_langs,
            subtitles=subtitles,
            jq_count=jq_count,
        )

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def get_parts(
        url: Annotated[str, Field(description=_URL_DESC)],
        jq: Annotated[
            str | None,
            Field(description=_jq_desc(VideoPart, paged=False)),
        ] = None,
    ) -> PartsResult:
        """即分 P。单段视频返回一项。(video parts, pages)"""
        video_id, _ = _resolve(url)
        parts = _client().get_parts(video_id)
        encoded, jq_count = encode_items("parts", parts, VideoPart, jq)
        return PartsResult(
            video_id=video_id,
            count=len(parts),
            parts=encoded,
            jq_count=jq_count,
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
        """返回内联 JPEG 图片。需要登录。

        (video frame, screenshot)
        """
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
            Field(description="排序。relevance 是 B 站的综合排序。"),
        ] = "relevance",
        jq: Annotated[
            str | None,
            Field(description=_jq_desc(SearchItem, paged=True)),
        ] = None,
    ) -> SearchResult:
        """每批最多 30 条，最多翻 30 批。

        结果已滤掉付费课程，一批可能不满 30 条。summary 是平台截断过的简介，
        完整简介通过 get_video_info 获取。

        (search videos, find video by keyword)
        """
        page = _client().search(query=query, cursor=cursor, order=order)
        results, jq_count = encode_items("results", page.items, SearchItem, jq)
        return SearchResult(
            count=len(page.items),
            results=results,
            jq_count=jq_count,
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def suggest_keywords(
        term: Annotated[str, Field(description="已输入的关键词，可以只是开头几个字。")],
    ) -> SuggestResult:
        """B 站搜索框的联想词，最多 10 条。

        平台联想时会忽略 + # 等符号。

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
            Field(description="按关键词筛选投稿，平台除标题外也会匹配简介等。"),
        ] = None,
        jq: Annotated[
            str | None,
            Field(description=_jq_desc(UpVideoItem, paged=True)),
        ] = None,
    ) -> UpVideosResult:
        """每批最多 40 条。total 是视频总数，带 keyword 时为匹配数。需要登录。

        (uploader videos, channel uploads, other videos by this author)
        """
        mid = resolve_up(author_url)
        author, total, page = _client().get_up_videos(
            mid, cursor=cursor, order=order, keyword=keyword
        )
        videos, jq_count = encode_items("videos", page.items, UpVideoItem, jq)
        return UpVideosResult(
            author=author,
            author_url=f"https://space.bilibili.com/{mid}",
            total=total,
            count=len(page.items),
            videos=videos,
            jq_count=jq_count,
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def get_up_info(
        author_url: Annotated[
            str, Field(description="UP 主空间链接（其他工具返回的 author_url），或数字 mid。")
        ],
    ) -> UpInfoResult:
        """含昵称、签名、等级、粉丝数、关注数、投稿数、总获赞、认证、大会员等。

        (uploader profile, channel info, followers)
        """
        info = _client().get_up_info(resolve_up(author_url))
        return UpInfoResult(**info.model_dump())

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def get_feed(
        jq: Annotated[
            str | None,
            Field(description=_jq_desc(FeedItem, paged=False)),
        ] = None,
    ) -> FeedResult:
        """B 站首页推荐流，每批最多 30 条。

        (homepage feed, recommendations, browse)
        """
        items = _client().get_feed()
        feed, jq_count = encode_items("feed", items, FeedItem, jq)
        return FeedResult(count=len(items), feed=feed, jq_count=jq_count)

    @mcp.tool(annotations=_READS_PLATFORM)
    def get_login_status() -> LoginStateResult:
        """联网向平台核验。

        is_login 为 null 表示无法验证，与 false 不同。

        (login status)
        """
        return LoginStateResult(is_login=_client().get_login_status())

    @mcp.tool(annotations=_LOCAL_ONLY)
    def logout() -> LogoutResult:
        """删除本地保存的凭据。(log out, sign out)"""
        deleted = delete_cookie()
        return LogoutResult(
            deleted=deleted,
            message="已退出登录，本地凭据已删除。" if deleted else "本地没有保存的凭据。",
        )

    # structured_output=False 同 get_frame：二维码内联返回，不进结构化通道。
    @mcp.tool(structured_output=False, annotations=_READS_PLATFORM)
    def start_qr_login() -> list[ImageContent | TextContent]:
        """返回内联二维码图片。不写入本地凭据，由 complete_qr_login 写入。

        (QR code login)
        """
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
        """取回凭据并写入本地。

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
