"""MCP server：内容工具 + 搜索 + 登录工具。

模型能看到的只有三处：工具 docstring、inputSchema 的参数说明、server instructions。
outputSchema 不进模型上下文，故返回模型只声明结构，字段口径写在 docstring 里。
"""

from __future__ import annotations

import base64
import functools
import io
import json
import logging
import time
from collections.abc import Callable
from dataclasses import fields
from typing import Annotated, Any, Literal
from urllib.request import urlopen

import segno
from anyio import to_thread
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__
from .client import BilibiliClient, resolve_up, resolve_video
from .credentials import delete_cookie, load_cookie, save_cookie
from .errors import BilibiliError
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
)

# ── 参数说明 ────────────────────────────────────────────────────────────────

_log = logging.getLogger(__name__)

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
        f"必填的 jq 表达式。输入是{scope}组成的数组，每条字段：{columns}。"
        + (f"体积大、多数任务用不到的字段：{bulky}；特定任务需要时照常使用。" if bulky else "")
        + (paging if paged else "")
        + "结果为字符串时原样返回，其他结果编码为表格或 JSON；结果为数组时 jq_count 是其长度。"
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
    from_cache: bool = False
    cached_at: str | None = None
    message: str | None = None


class ReplyThreadItem(BaseModel):
    comment_id: str
    replies: str
    jq_count: int | None = None
    has_more: bool
    total: int | None = None
    withheld: int = 0
    error: str | None = None


class CommentRepliesResult(_Timed):
    video_id: str
    results: list[ReplyThreadItem]
    from_cache: bool = False
    cached_at: str | None = None


class DanmakuResult(_Timed):
    video_id: str
    count: int
    danmaku: str
    jq_count: int | None = None


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


class VersionResult(BaseModel):
    running: str
    latest: str | None


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


def _in_thread(fn: Callable[..., Any]) -> Callable[..., Any]:
    """同步工具放到工作线程里跑，免得一个慢调用（如评论限速排队）卡住整个服务。

    评论请求之间仍由限速器的锁排队，其他工具互不等待。
    mcp 库把工具异常转成错误结果返回而不记日志，所以在这里记。参数不记，其中有扫码登录的 key。
    """
    name = fn.__name__

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.monotonic()
        try:
            result = await to_thread.run_sync(functools.partial(fn, *args, **kwargs))
        except BilibiliError as e:
            _log.warning("工具 %s 失败（%.1f 秒）：%s", name, time.monotonic() - start, e)
            raise
        except Exception:
            _log.exception("工具 %s 异常（%.1f 秒）", name, time.monotonic() - start)
            raise
        _log.info("工具 %s 完成（%.1f 秒）", name, time.monotonic() - start)
        return result

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

_PYPI_JSON = "https://pypi.org/pypi/polylens-bilibili-mcp/json"


def _latest_release() -> str | None:
    try:
        with urlopen(_PYPI_JSON, timeout=5) as resp:  # noqa: S310
            return json.load(resp)["info"]["version"]
    except (OSError, ValueError, KeyError):
        return None

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

    # mcp 1.30 起默认回收空闲 30 分钟的会话。claude.ai 隔久了仍拿旧会话 ID 来请求，
    # 先吃 404 再重连，用户看到的是第一次调用落空。关掉回收，与 1.29 及以前一致。
    mcp = FastMCP(
        "polylens-bilibili",
        instructions=_SERVER_INSTRUCTIONS,
        session_idle_timeout=None,
        **net,
    )
    # FastMCP 1.x 不收 version，不设时握手报的是 mcp 库自身的版本。
    mcp._mcp_server.version = __version__

    def tool(**kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
        return lambda fn: mcp.tool(**kwargs)(_in_thread(fn))

    if oauth_provider is not None and auth_secret:
        from .oauth import register_consent_route
        register_consent_route(mcp, oauth_provider, auth_secret)

    @tool(annotations=_READS_PLATFORM)
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

    @tool(annotations=_READS_PLATFORM)
    @_timed
    def get_comments(
        url: Annotated[str, Field(description=_URL_DESC)],
        count: Annotated[
            int,
            Field(
                description=(
                    "至少取多少条主评论。平台按每页约 20 条整页返回，实际条数可能多于 count；"
                    "评论不够时返回剩余的全部。"
                    "主评论与二级评论合计限速每分钟约 30 页（约 600 条）。"
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
                    "翻页与重放靠 batch_id；"
                    "同一视频同一时间只用一个 hot cursor："
                    "新开会话（包括其他对话里的）后，旧 cursor 会重复取过的内容或提前返回空，"
                    "混用时新的也会回退；遇到时从 cursor 为空重新开始。"
                    "newest 按时间倒序，cursor 含位置，可重复取同一批，不受新会话影响。"
                    "需要完整抓取或断点续取时用 newest。"
                    "缓存：除 jq 外参数相同的调用返回同一结果，from_cache 表示是否来自缓存；"
                    "被限速的不完整结果不缓存。"
                    "newest 要新数据用 refresh，hot 换 batch_id 取下一批。"
                    "调用没收到结果时服务端仍会取完，之后用相同参数再取直接用缓存。"
                )
            ),
        ] = "hot",
        batch_id: Annotated[
            str | None,
            Field(
                description=(
                    "mode=hot 时必填。自定的短字符串：其他参数相同时，同一个值返回同一批，"
                    "换新值取下一批。"
                    "重放结果与之前不同时，原批次已丢失，需要就从 cursor 为空重新开始。"
                )
            ),
        ] = None,
        refresh: Annotated[
            bool, Field(description="跳过缓存重新取，并更新缓存。仅用于 newest。")
        ] = False,
        *,
        jq: Annotated[str, Field(description=_jq_desc(Comment, paged=True))],
    ) -> CommentsResult:
        """不含二级评论，二级评论通过 get_comment_replies 获取。需要登录。

        image_urls、link_titles 是字符串，多个时以换行分隔。

        (video comments)
        """
        if mode == "hot" and not batch_id:
            raise BilibiliError("mode=hot 需要 batch_id：传一个自定的短字符串，重放同一批时沿用。")
        video_id, _ = _resolve(url)
        page = _client().get_comments(
            video_id, count=count, cursor=cursor, sort=mode, batch_id=batch_id, refresh=refresh
        )
        comments, jq_count = encode_items("comments", page.items, Comment, jq)
        return CommentsResult(
            video_id=video_id,
            count=len(page.items),
            comments=comments,
            jq_count=jq_count,
            has_more=page.has_more,
            next_cursor=page.next_cursor,
            from_cache=page.from_cache,
            cached_at=page.cached_at,
            message=page.rate_limited,
        )

    @tool(annotations=_READS_PLATFORM)
    @_timed
    def get_comment_replies(
        url: Annotated[str, Field(description=_URL_DESC)],
        comment_ids: Annotated[
            list[str],
            Field(description="主评论 id 列表，从 get_comments 返回的 comments 表里取。"),
        ],
        pages: Annotated[
            int,
            Field(
                description=(
                    "每条主评论取几页，每页 20 条。has_more 表示后面还有。"
                    "与主评论合计限速每分钟约 30 页。"
                )
            ),
        ],
        start_page: Annotated[
            int,
            Field(description="每条主评论从第几页开始，1 起。"),
        ] = 1,
        refresh: Annotated[bool, Field(description="跳过缓存重新取，并更新缓存。")] = False,
        *,
        jq: Annotated[
            str,
            Field(
                description=_jq_desc(
                    Comment, paged=False, scope="单条主评论下的二级评论", exclude=_REPLY_EXCLUDE
                )
            ),
        ],
    ) -> CommentRepliesResult:
        """二级评论按时间正序排列。需要登录。

        total 是该主评论下平台能列出的二级评论总数，可用来算页数。
        withheld 是该主评论下平台未列出的二级评论条数（已删除或被折叠）。
        某条主评论取不到（评论不存在、不属于这个视频）时只在它的 error 里说明，其他照常返回。
        中途被风控或限流时，已取完的照常返回，其余的 error 里说明原因。
        parent_id 为空表示直接回复主评论，否则是所回复的那条二级评论的 id。
        parent_id 指向的二级评论不在列表里时，那条被平台隐藏了，取不到。
        除 jq 外参数相同的调用返回同一结果，from_cache 表示是否来自缓存；
        被限速的不完整结果不缓存，要新数据用 refresh。
        调用没收到结果时服务端仍会取完，之后用相同参数再取直接用缓存。
        image_urls、link_titles 是字符串，多个时以换行分隔。

        (comment replies, sub-replies, thread)
        """
        video_id, _ = _resolve(url)
        batch = _client().get_comment_replies(
            video_id, comment_ids=comment_ids, start_page=start_page, pages=pages, refresh=refresh
        )
        results = []
        for t in batch.threads:
            replies, jq_count = encode_items(
                "replies", t.page.items, Comment, jq, exclude=_REPLY_EXCLUDE
            )
            results.append(
                ReplyThreadItem(
                    comment_id=t.comment_id,
                    replies=replies,
                    jq_count=jq_count,
                    has_more=t.page.has_more,
                    total=t.total,
                    withheld=t.withheld,
                    error=t.error,
                )
            )
        return CommentRepliesResult(
            video_id=video_id, results=results,
            from_cache=batch.from_cache, cached_at=batch.cached_at,
        )

    @tool(annotations=_READS_PLATFORM)
    @_timed
    def get_danmaku(
        url: Annotated[str, Field(description=_URL_DESC)],
        count: Annotated[
            int,
            Field(
                description=(
                    "取多少条弹幕。取该段里 heat 最高的这么多条，结果仍按时间轴排序；"
                    "达到或超过平台给出的条数即全部返回。"
                    "heat 是平台给每条弹幕的标记，约 1-10 的档位，同档内不再细分。"
                )
            ),
        ],
        page: Annotated[int | None, Field(description=_PAGE_DESC)] = None,
        *,
        jq: Annotated[
            str,
            Field(description=_jq_desc(Danmaku, paged=False, scope="按 count 选出的弹幕")),
        ],
    ) -> DanmakuResult:
        """timestamp 是弹幕在视频中的秒数。

        平台可能只给出部分弹幕，少于视频信息里的弹幕数。

        (danmaku, bullet comments)
        """
        video_id, part = _resolve(url, page)
        bullets = _client().get_danmaku(video_id, count=count, page=part)
        danmaku, jq_count = encode_items("danmaku", bullets, Danmaku, jq)
        return DanmakuResult(
            video_id=video_id, count=len(bullets), danmaku=danmaku, jq_count=jq_count
        )

    @tool(annotations=_READS_PLATFORM)
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
        *,
        jq: Annotated[
            str,
            Field(description=_jq_desc(SubtitleEntry, paged=False)),
        ],
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

    @tool(annotations=_READS_PLATFORM)
    @_timed
    def get_parts(
        url: Annotated[str, Field(description=_URL_DESC)],
        *,
        jq: Annotated[
            str,
            Field(description=_jq_desc(VideoPart, paged=False)),
        ],
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
    @tool(structured_output=False, annotations=_READS_PLATFORM)
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

    @tool(annotations=_READS_PLATFORM)
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
        *,
        jq: Annotated[
            str,
            Field(description=_jq_desc(SearchItem, paged=True)),
        ],
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

    @tool(annotations=_READS_PLATFORM)
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

    @tool(annotations=_READS_PLATFORM)
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
        *,
        jq: Annotated[
            str,
            Field(description=_jq_desc(UpVideoItem, paged=True)),
        ],
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

    @tool(annotations=_READS_PLATFORM)
    @_timed
    def get_up_info(
        author_url: Annotated[
            str, Field(description="UP 主空间链接（其他工具返回的 author_url），或数字 mid。")
        ],
    ) -> UpInfoResult:
        """含昵称、签名、等级、粉丝数、关注数、总获赞、认证、大会员等。

        (uploader profile, channel info, followers)
        """
        info = _client().get_up_info(resolve_up(author_url))
        return UpInfoResult(**info.model_dump())

    @tool(annotations=_READS_PLATFORM)
    @_timed
    def get_feed(
        *,
        jq: Annotated[
            str,
            Field(description=_jq_desc(FeedItem, paged=False)),
        ],
    ) -> FeedResult:
        """B 站首页推荐流，每批最多 30 条。

        (homepage feed, recommendations, browse)
        """
        items = _client().get_feed()
        feed, jq_count = encode_items("feed", items, FeedItem, jq)
        return FeedResult(count=len(items), feed=feed, jq_count=jq_count)

    @tool(annotations=_READS_PLATFORM)
    def get_login_status() -> LoginStateResult:
        """联网向平台核验。

        is_login 为 null 表示无法验证，与 false 不同。

        (login status)
        """
        return LoginStateResult(is_login=_client().get_login_status())

    # 网页端会缓存工具说明，说明里写死启动时的版本，与 running 对比即可看出缓存是否过期。
    @tool(
        annotations=_READS_PLATFORM,
        description=(
            f"本说明对应版本 {__version__}。running 与之不同表示工具说明是旧缓存，需重连；"
            "running 低于 latest 表示服务未升级。latest 查不到时为 null。(server version)"
        ),
    )
    def get_version() -> VersionResult:
        return VersionResult(running=__version__, latest=_latest_release())

    @tool(annotations=_LOCAL_ONLY)
    def logout() -> LogoutResult:
        """删除本地保存的凭据。(log out, sign out)"""
        deleted = delete_cookie()
        return LogoutResult(
            deleted=deleted,
            message="已退出登录，本地凭据已删除。" if deleted else "本地没有保存的凭据。",
        )

    # structured_output=False 同 get_frame：二维码内联返回，不进结构化通道。
    @tool(structured_output=False, annotations=_READS_PLATFORM)
    def start_qr_login() -> list[ImageContent | TextContent]:
        """返回内联二维码图片和对应的链接 url。不写入本地凭据，由 complete_qr_login 写入。

        (QR code login)
        """
        session = BilibiliClient().start_qr_login()
        meta = {
            "key": session.key,
            "url": session.url,
            "message": (
                "把 url 给用户，手机上点开会跳转 B站 App 确认登录。"
                "用户看不到这张二维码时，用 url 生成二维码给用户。"
                "用户确认后，调用 complete_qr_login。"
            ),
        }
        return [
            _png_block(_make_qr_png(session.url), "image/png"),
            TextContent(type="text", text=json.dumps(meta, ensure_ascii=False)),
        ]

    @tool(annotations=_WRITES_CREDENTIAL)
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
