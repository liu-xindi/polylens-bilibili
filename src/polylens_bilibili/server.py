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

from .client import BilibiliClient, resolve_video
from .credentials import delete_cookie, load_cookie, save_cookie
from .errors import BilibiliError
from .models import (
    Comment,
    Danmaku,
    FeedItem,
    QrStatus,
    SearchItem,
    SubtitleEntry,
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
    next_cursor: str | None = None
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


class FeedResult(BaseModel):
    count: int
    feed: str
    elapsed_s: float | None = None


class LoginStateResult(BaseModel):
    is_login: bool | None


class CookieSavedResult(BaseModel):
    message: str


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


_SERVER_INSTRUCTIONS = (
    "本服务从 B 站视频中提取信息：元信息、分段清单、评论、楼中楼、弹幕、字幕、视频帧，"
    "并支持按关键词搜索视频、刷首页推荐。"
    "内容类工具的 url 参数接受视频链接、b23.tv 短链或裸 BV/av 号；没有链接时用 "
    "search_videos 按关键词找，或用 get_feed 看平台推什么。"
    "评论、楼中楼、字幕、视频帧需要登录，未登录时会明确报错；"
    "元信息、分段、弹幕、搜索、首页推荐无需登录。登录用 set_cookie 写入浏览器 Cookie，"
    "或用 start_qr_login 扫码。"
    "翻页统一：has_more=true 时把同一处返回的 next_cursor 原样回传给 cursor 取下一批，"
    "=false 表示已到底；游标不透明，不要自造或解析。"
    "列表类数据以 TOON 表格串返回，表头固定，同一工具每次返回的列相同。"
    "日历时间按运行本机的时区呈现。内容类工具与搜索的返回附 elapsed_s，为服务端处理秒数。"
    "Bilibili video tools: video info, parts, comments and replies, danmaku (bullet comments), "
    "subtitles, video frames, search, homepage recommendation feed, login."
)

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

        统计口径：评论数含楼中楼回复；弹幕数与整片时长是全部分段之和，
        当前段时长只算这一段。弹幕数是稿件累计值，与 get_danmaku 能取到的条数不是
        同一口径，不能拿它估算能取多少条。

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
                    "想要的主评论条数，实际返回可能多于或少于这个数。"
                    "置顶评论排在第一页最前。数量决定这次调用的耗时，取几百条会明显变慢。"
                )
            ),
        ],
        cursor: Annotated[str | None, Field(description=_CURSOR_DESC)] = None,
        mode: Annotated[
            Literal["hot", "newest"],
            Field(
                description=(
                    "排序方式：hot 是平台的综合排序，newest 按时间倒序。"
                    "hot 不等于按点赞数排，结果里会混入点赞很少的新评论；"
                    "要按赞数取前几条，自己对返回的 like_count 排一遍。"
                    "两者的游标性质也不同：hot 的游标绑在一次翻页过程上，中断后无法从原处接续，"
                    "重复用同一个游标会继续往后走；newest 的游标是位置标识，可以重复取到同一批。"
                    "要完整抓取或需要断点续取时用 newest。"
                )
            ),
        ] = "hot",
    ) -> CommentsResult:
        """获取视频的主评论，不含楼中楼。需要登录。(video comments)"""
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
            int,
            Field(
                description=(
                    "每个楼取多少条回复，超出的截断，用 cursor 续取。"
                    "它与 comment_ids 的个数一起决定这次调用的耗时，两者都大时会明显变慢。"
                )
            ),
        ],
        cursor: Annotated[str | None, Field(description=_CURSOR_DESC)] = None,
    ) -> CommentRepliesResult:
        """按主评论 id 钻取楼中楼。需要登录。

        翻页状态在每个楼里各一份，本工具没有顶层的 has_more 与 next_cursor。
        withheld 是这个楼里平台不肯给出的回复条数，那些回复翻到底也取不到，
        却仍可能被返回结果里的 parent_id 指到。

        (comment replies, sub-replies, thread)
        """
        video_id, _ = _resolve(url)
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
                    "想要的弹幕条数。取该段里 heat 最高的这么多条，结果仍按时间轴排序；"
                    "达到或超过该段弹幕总数即返回全部。"
                    "heat 是平台给每条弹幕的标记，约 1-10 的档位，同档内不再细分；"
                    "弹幕没有点赞数，与评论的排序依据是两回事。"
                    "条数少于一档的规模时整批都落在最高档，此时按时间轴等距取，"
                    "结果铺满全段而不是挤在开头。"
                )
            ),
        ],
        page: Annotated[int | None, Field(description=_PAGE_DESC)] = None,
    ) -> DanmakuResult:
        """获取视频弹幕，按热度取一批，仍按时间轴排序。(danmaku, bullet comments)"""
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
                    "可选值见返回的 available_langs；该段没有字幕时返回的 lang 为 null。"
                )
            ),
        ] = None,
    ) -> SubtitlesResult:
        """获取视频字幕，逐句返回。字幕可能为 AI 生成或机器翻译，存在误差。需要登录。

        多段视频常只有一部分分段有字幕，与该段时长无关；没有的那些返回空表、
        lang 为 null、available_langs 为空。

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
        """列出多段视频（分 P）的全部分段。单段视频返回一项。(video parts, pages)"""
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
            float, Field(description="视频内秒数，如 10.5。超过该段时长会报错。")
        ],
        page: Annotated[int | None, Field(description=_PAGE_DESC)] = None,
    ) -> list[ImageContent | TextContent]:
        """截取视频指定时刻的一帧，返回内联 JPEG 图片。需要登录。(video frame, screenshot)"""
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
            Field(description="搜索关键词，可以是标题、UP 主名、内容主题。"),
        ],
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
        """在 B 站按关键词搜索视频、找视频、检索投稿。

        没有 BV 号或链接时用它入手：返回的每条都带链接，可直接传给内容类工具取评论、
        字幕、弹幕等。

        (search videos, find video by keyword)
        """
        page = _client().search(query=query, count=count, cursor=cursor)
        return SearchResult(
            count=len(page.items),
            results=to_toon("results", page.items, SearchItem),
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    @mcp.tool(annotations=_READS_PLATFORM)
    @_timed
    def get_feed(
        count: Annotated[
            int,
            Field(
                description=(
                    "想要的视频条数。平台单次有上限，超出会被平台拒绝并报错。"
                )
            ),
        ],
    ) -> FeedResult:
        """刷 B 站首页推荐流，看平台现在推什么。

        登录后按账号口味推，未登录给通用推荐。每次调用都是新的一批，想多刷就多调几次；
        这个流没有尽头也没有位置，既不能重放也不保证跨次调用不重复，要去重就按 url 自己去。

        (homepage feed, recommendations, browse)
        """
        items = _client().get_feed(count=count)
        return FeedResult(count=len(items), feed=to_toon("feed", items, FeedItem))

    @mcp.tool(annotations=_READS_PLATFORM)
    def get_login_status() -> LoginStateResult:
        """查询当前是否已登录（联网核验本地凭据是否仍然有效）。

        is_login 为 null 表示无法验证，与 false 不同。

        (login status)
        """
        return LoginStateResult(is_login=_client().get_login_status())

    @mcp.tool(annotations=_LOCAL_ONLY)
    def set_cookie(
        cookie: Annotated[
            str,
            Field(description="从浏览器复制的整段 Cookie，单行。这是敏感凭据，会出现在对话中。"),
        ],
    ) -> CookieSavedResult:
        """写入 B站登录 Cookie，即时生效。(set cookie, log in)"""
        value = cookie.strip()
        if not value:
            raise BilibiliError("cookie 不能为空；清除登录用 logout")
        save_cookie(value)
        return CookieSavedResult(message="Cookie 已保存，即时生效；可调 get_login_status 确认。")

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
            "next_action": {"tool": "complete_qr_login", "args": {"key": session.key}},
        }
        return [
            _png_block(_make_qr_png(session.url), "image/png"),
            TextContent(type="text", text=json.dumps(meta, ensure_ascii=False)),
        ]

    @mcp.tool(annotations=_WRITES_CREDENTIAL)
    def complete_qr_login(
        key: Annotated[str, Field(description="start_qr_login 返回的 key。")],
    ) -> QrCheckResult:
        """查询扫码结果，已确认则取回凭据并写入本地，登录即刻生效。

        status 为 waiting 未扫码、scanned 已扫待手机确认、success 登录成功、
        expired 二维码过期。每次只查一次。

        (finish QR code login, poll QR status)
        """
        result = BilibiliClient().check_qr_login(key)
        if result.status is QrStatus.SUCCESS:
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
