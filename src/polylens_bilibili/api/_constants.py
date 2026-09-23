"""写死的平台常量。

均为普适常量（域名/endpoint/WBI 签名表/风控字段/UA），非 secret。
"""

from __future__ import annotations

API_BASE = "https://api.bilibili.com"
PASSPORT_BASE = "https://passport.bilibili.com"
DANMAKU_XML_URL = "https://api.bilibili.com/x/v1/dm/list.so"
WEB_HOME = "https://www.bilibili.com/"
SEARCH_REFERER = "https://search.bilibili.com/"

ENDPOINTS: dict[str, str] = {
    "nav": "/x/web-interface/nav",
    "video_info": "/x/web-interface/view",
    "replies_main": "/x/v2/reply/wbi/main",
    "replies_sub": "/x/v2/reply/reply",
    "player_v2": "/x/player/wbi/v2",
    "playurl": "/x/player/wbi/playurl",
    "qrcode_generate": "/x/passport-login/web/qrcode/generate",
    "qrcode_poll": "/x/passport-login/web/qrcode/poll",
    "search_type": "/x/web-interface/wbi/search/type",
    "suggest": "/x/web-interface/suggest",
    "space_videos": "/x/space/wbi/arc/search",
    "feed_rcmd": "/x/web-interface/wbi/index/top/feed/rcmd",
}

# 搜索结果总数封顶（前端翻页亦止于此）。
SEARCH_RESULT_CAP = 1000
SEARCH_PAGE_SIZE = 30

# 楼中楼单页条数，平台封顶 20。
REPLY_PAGE_SIZE = 20

# 截帧画质：720p。
FRAME_QUALITY_ID = 64

# WBI 签名：mixin key 重排表 + 过滤字符（algorithm 在 _signing.py，这里只放数值）。
WBI_MIXIN_KEY_ENC_TAB: list[int] = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
WBI_STRIP_CHARS = "!'()*"

# 评论接口的风控参数（dm_* 系列，缺了会被风控拦截）。
WBI_ANTI_RISK_PARAMS: dict[str, str] = {
    "dm_img_list": "[]",
    "dm_img_str": "__random_2",
    "dm_cover_img_str": "__random_2",
    "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
}

SHORT_LINK_HOSTS = frozenset({"b23.tv", "bili2233.cn"})

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
