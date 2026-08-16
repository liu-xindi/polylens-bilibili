"""视频帧截取：本地 HTTP 代理透传 Cookie，ffmpeg 按需 Range 请求 CDN 截帧。

未登录时平台只给到 480P 而不报错，与"这个视频只有 480P"无法区分，故取播放地址前判登录态。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from urllib.request import Request

from ..errors import AuthRequiredError, PolylensError
from ._constants import ENDPOINTS, FRAME_QUALITY_ID, USER_AGENT, WEB_HOME
from ._http import HttpClient
from ._signing import fetch_nav, sign_params


def _fetch_playurl(client: HttpClient, bvid: str, cid: int) -> dict[str, Any]:
    """通过 player/wbi/playurl 获取 DASH manifest。"""
    nav = fetch_nav(client)
    if not nav.is_login:
        raise AuthRequiredError("frame")
    params = sign_params(
        {
            "bvid": bvid, "cid": cid, "fnval": 4048,
            "qn": FRAME_QUALITY_ID, "platform": "pc", "fnver": 0,
        },
        nav.img_key,
        nav.sub_key,
    )
    return client.get_json(ENDPOINTS["playurl"], params)


def _list_streams(data: dict[str, Any]) -> list[dict[str, Any]]:
    return (data.get("dash") or {}).get("video") or []


# quality_id: 120=4K, 116=1080P+, 80=1080p, 64=720p, 32=480p, 16=360p
def _pick_stream(streams: list[dict[str, Any]], quality_id: int) -> dict[str, Any]:
    """选指定画质的 AVC 流；该档不在返回列表里时取最接近的（平分取更高），优先 AVC 编码。"""
    for s in streams:
        if s.get("id") == quality_id and "avc1" in (s.get("codecs") or ""):
            return s
    for s in streams:
        if s.get("id") == quality_id:
            return s
    # 请求档不在返回列表里（如视频本身没这么高）：取最接近请求档的一条，而非一律最低。
    avc = [s for s in streams if "avc1" in (s.get("codecs") or "")]
    pool = avc or streams
    return min(pool, key=lambda s: (abs(s.get("id", 0) - quality_id), -s.get("id", 0)))


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def _make_proxy_handler(client: HttpClient, cdn_url: str) -> type:
    """返回一个 Handler 类，将所有请求代理到 cdn_url，透传 Range 头并附上 Cookie。"""
    combined_cookie = client.combined_cookie()

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _proxy(self) -> None:
            headers: dict[str, str] = {
                "User-Agent": USER_AGENT,
                "Referer": WEB_HOME,
                "Origin": "https://www.bilibili.com",
            }
            if combined_cookie:
                headers["Cookie"] = combined_cookie
            if "Range" in self.headers:
                headers["Range"] = self.headers["Range"]

            req = Request(cdn_url, headers=headers)
            try:
                with client._opener.open(req, timeout=30) as resp:
                    self.send_response(resp.status)
                    for key in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                        val = resp.headers.get(key)
                        if val:
                            self.send_header(key, val)
                    self.end_headers()
                    while chunk := resp.read(65536):
                        try:
                            self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            break  # ffmpeg 已拿到所需数据，主动断开，正常
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                try:
                    self.send_error(502, str(exc))
                except Exception:
                    pass

        do_GET = _proxy
        do_HEAD = _proxy

    return _Handler


def _capture_frame(client: HttpClient, cdn_url: str, timestamp: float, output_path: str) -> None:
    """启动本地代理，让 ffmpeg 按需 Range 请求截帧，完成后关闭代理。"""
    handler_cls = _make_proxy_handler(client, cdn_url)
    server = _ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]

    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(timestamp),
            "-i", f"http://127.0.0.1:{port}/",
            "-frames:v", "1",
            "-q:v", "2",
            output_path,
        ]
        blocked = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
        env = {k: v for k, v in os.environ.items() if k.upper() not in blocked}
        result = subprocess.run(cmd, capture_output=True, timeout=120, env=env)  # noqa: S603
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg 失败:\n{result.stderr.decode(errors='replace')[-800:]}")
    finally:
        server.shutdown()


def fetch_frame(client: HttpClient, bvid: str, cid: int, timestamp: float) -> bytes:
    """截取指定时刻的帧，返回内存中的 JPEG 字节。"""
    if shutil.which("ffmpeg") is None:
        raise PolylensError("未检测到 ffmpeg：视频帧截取需要本机安装 ffmpeg 并加入 PATH")
    client.ensure_buvid()
    data = _fetch_playurl(client, bvid, cid)
    streams = _list_streams(data)
    if not streams:
        raise PolylensError("播放信息里没有视频流")
    stream = _pick_stream(streams, FRAME_QUALITY_ID)

    candidates: list[str] = []
    base = stream.get("baseUrl") or stream.get("base_url") or ""
    if base:
        candidates.append(base)
    candidates.extend(stream.get("backupUrl") or stream.get("backup_url") or [])
    if not candidates:
        raise PolylensError("视频流没有可用地址")

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        output_path = Path(f.name)  # 占位路径，交给 ffmpeg 写
    try:
        last_err: Exception = RuntimeError("无候选 URL")
        for url in candidates:
            try:
                _capture_frame(client, url, timestamp, str(output_path))
                return output_path.read_bytes()
            except (RuntimeError, OSError) as exc:
                last_err = exc
        raise last_err
    finally:
        output_path.unlink(missing_ok=True)  # ffmpeg 产物读入内存后删
