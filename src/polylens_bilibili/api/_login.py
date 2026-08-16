"""扫码登录：生成二维码 + 轮询结果 + 提取 cookie。

流程：
1. GET qrcode_generate → { qrcode_key, url }
2. 用 url 生成二维码，用户用 B站 App 扫码
3. 轮询 qrcode_poll?qrcode_key=xxx：
   data.code 86101 未扫；86090 已扫待确认；86038 已过期；0 成功
4. 成功时响应 Set-Cookie 头携带 SESSDATA / bili_jct / DedeUserID 等，解析为 cookie 字符串
"""

from __future__ import annotations

import json

from ..models import LoginCheckResult, QrLoginSession, QrStatus
from ._constants import ENDPOINTS, PASSPORT_BASE
from ._http import HttpClient

_CODE_TO_STATUS: dict[int, QrStatus] = {
    86101: QrStatus.WAITING,
    86090: QrStatus.SCANNED,
    86038: QrStatus.EXPIRED,
}


def start_qr_login(client: HttpClient) -> QrLoginSession:
    data = client.get_json(ENDPOINTS["qrcode_generate"], base_url=PASSPORT_BASE)
    return QrLoginSession(key=data["qrcode_key"], url=data["url"])


def check_qr_login(client: HttpClient, key: str) -> LoginCheckResult:
    url = f"{PASSPORT_BASE}{ENDPOINTS['qrcode_poll']}?qrcode_key={key}"
    body, set_cookie_headers = client.get_bytes_and_set_cookies(url)
    payload = json.loads(body.decode("utf-8"))
    data = payload.get("data") or {}
    sub_code = data.get("code", -1)

    if sub_code == 0:
        parts = [sc.split(";")[0].strip() for sc in set_cookie_headers]
        cookie = "; ".join(p for p in parts if p)
        return LoginCheckResult(status=QrStatus.SUCCESS, cookie=cookie)

    return LoginCheckResult(status=_CODE_TO_STATUS.get(sub_code, QrStatus.EXPIRED))
