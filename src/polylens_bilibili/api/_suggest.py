"""搜索框联想：web-interface/suggest，返回建议词。

浏览器会带 WBI 签名与一串埋点参数，实测只传 term 就能取到同样的结果，未登录也行。
没有建议时（含空白输入）平台回 code 3 而不是空列表。
"""

from __future__ import annotations

from ..errors import BilibiliError
from ._constants import ENDPOINTS
from ._http import HttpClient

_NO_SUGGESTION = 3


def fetch_suggest(client: HttpClient, term: str) -> list[str]:
    """取输入词的联想建议，平台最多给 10 条。"""
    query = term.strip()
    if not query:
        raise BilibiliError("关键词不能为空")
    data = client.get_json(
        ENDPOINTS["suggest"], {"term": query}, allow_codes={_NO_SUGGESTION}
    )
    if data is None:
        return []
    return [tag["value"] for tag in data["result"]["tag"]]
