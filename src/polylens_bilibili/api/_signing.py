"""WBI 签名 + nav 探测。

nav 一次请求同时给出签名密钥与登录态，需要签名的能力顺带拿到登录态，不额外发请求。
"""

from __future__ import annotations

import hashlib
import random
import re
import string
import time
from typing import Any, NamedTuple
from urllib.parse import urlencode

from ._constants import ENDPOINTS, WBI_ANTI_RISK_PARAMS, WBI_MIXIN_KEY_ENC_TAB, WBI_STRIP_CHARS
from ._http import HttpClient


class NavInfo(NamedTuple):
    img_key: str
    sub_key: str
    is_login: bool


def extract_mixin_key(img_key: str, sub_key: str) -> str:
    """img_key+sub_key 按重排表取前 32 位。"""
    mixed = img_key + sub_key
    return "".join(mixed[i] for i in WBI_MIXIN_KEY_ENC_TAB if i < len(mixed))[:32]


def fetch_nav(client: HttpClient) -> NavInfo:
    """取 WBI 密钥与登录态（未登录也能取到密钥）。"""
    data = client.get_json(ENDPOINTS["nav"], allow_codes={-101})
    img_url = data["wbi_img"]["img_url"]
    sub_url = data["wbi_img"]["sub_url"]
    img_key = img_url.rsplit("/", 1)[-1].split(".", 1)[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".", 1)[0]
    return NavInfo(img_key, sub_key, bool(data.get("isLogin")))


def _random_str(length: int) -> str:
    return "".join(random.choices(string.ascii_uppercase[:11], k=length))


def _build_anti_risk_params() -> dict[str, str]:
    return {
        key: (_random_str(2) if value == "__random_2" else value)
        for key, value in WBI_ANTI_RISK_PARAMS.items()
    }


def sign_params(
    params: dict[str, Any],
    img_key: str,
    sub_key: str,
    *,
    anti_risk: bool = False,
    wts: int | None = None,
) -> dict[str, Any]:
    """对参数做 WBI 签名，返回含 wts + w_rid 的新字典。"""
    strip_re = re.compile(f"[{re.escape(WBI_STRIP_CHARS)}]")
    signed: dict[str, Any] = dict(params)
    if anti_risk:
        signed.update(_build_anti_risk_params())
    signed["wts"] = int(time.time() if wts is None else wts)

    mixin_key = extract_mixin_key(img_key, sub_key)
    filtered = {k: strip_re.sub("", str(v)) for k, v in signed.items()}
    query = urlencode(sorted(filtered.items()))
    filtered["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return filtered
