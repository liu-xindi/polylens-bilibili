"""WBI 签名测试：锁住 mixin key 重排 + sign_params(golden 回归)。无网络。

签名是逆向出来、最易随平台变动的一环，故上一道 golden 回归锁：
- mixin key 用官方文档（bilibili-API-collect）公开的测试向量做已知答案，顺带验证 _constants 里
  的 WBI_MIXIN_KEY_ENC_TAB 重排表正确；
- sign_params 固定 params+wts（无 anti_risk 随机）→ 固定 w_rid，锁住整条签名不被静默改坏。
"""

from __future__ import annotations

from typing import Any

from polylens_bilibili.api._signing import extract_mixin_key, fetch_nav, sign_params

# 官方文档公开的 WBI 测试向量（独立标准答案，不依赖本仓实现）
_IMG_KEY = "7cd084941338484aae1ad9425b84077c"
_SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"
_OFFICIAL_MIXIN = "ea1db124af3c7062474693fa704f4ff8"

# 固定 params + wts、无 anti_risk → 固定 w_rid（独立 md5 算得，见模块 docstring）
_GOLDEN_PARAMS = {"foo": "114", "bar": "514", "baz": 1919810}
_GOLDEN_WTS = 1702204169
_GOLDEN_WRID = "6149fdadf571698ca7e6a567265cd0ee"


class _FakeClient:
    def __init__(self, is_login: bool = False) -> None:
        self._is_login = is_login

    def get_json(self, endpoint: str, allow_codes: set[int] | None = None) -> dict[str, Any]:
        return {
            "isLogin": self._is_login,
            "wbi_img": {
                "img_url": f"https://i0.hdslb.com/bfs/wbi/{_IMG_KEY}.png",
                "sub_url": f"https://i0.hdslb.com/bfs/wbi/{_SUB_KEY}.png",
            },
        }


def test_extract_mixin_key_official_vector() -> None:
    """官方 img_key+sub_key → 官方 mixin_key，同时锁死 _constants 的重排表。"""
    assert extract_mixin_key(_IMG_KEY, _SUB_KEY) == _OFFICIAL_MIXIN


def test_extract_mixin_key_is_32_chars() -> None:
    assert len(extract_mixin_key(_IMG_KEY, _SUB_KEY)) == 32


def test_sign_params_golden_regression() -> None:
    """固定 params+wts → 固定 w_rid，锁住整条 WBI 签名（排序+过滤+md5）不被改坏。"""
    signed = sign_params(_GOLDEN_PARAMS, _IMG_KEY, _SUB_KEY, wts=_GOLDEN_WTS)
    assert signed["wts"] == str(_GOLDEN_WTS)
    assert signed["w_rid"] == _GOLDEN_WRID


def test_sign_params_deterministic() -> None:
    """同输入（含固定 wts）→ 同 w_rid。"""
    a = sign_params(_GOLDEN_PARAMS, _IMG_KEY, _SUB_KEY, wts=_GOLDEN_WTS)
    b = sign_params(_GOLDEN_PARAMS, _IMG_KEY, _SUB_KEY, wts=_GOLDEN_WTS)
    assert a["w_rid"] == b["w_rid"]


def test_sign_params_query_sensitive() -> None:
    """params 变 → w_rid 变（签名确实覆盖业务参数）。"""
    base = sign_params({"aid": 6383}, _IMG_KEY, _SUB_KEY, wts=_GOLDEN_WTS)
    other = sign_params({"aid": 9999}, _IMG_KEY, _SUB_KEY, wts=_GOLDEN_WTS)
    assert base["w_rid"] != other["w_rid"]


def test_sign_params_strips_special_chars() -> None:
    """值里的 !'()* 在签名前被剔除：'1!1(4)' 与 '114' 签名结果一致。"""
    dirty = sign_params({"v": "1!1(4)"}, _IMG_KEY, _SUB_KEY, wts=_GOLDEN_WTS)
    clean = sign_params({"v": "114"}, _IMG_KEY, _SUB_KEY, wts=_GOLDEN_WTS)
    assert dirty["v"] == "114"
    assert dirty["w_rid"] == clean["w_rid"]


def test_sign_params_autofills_wts() -> None:
    """不传 wts → 自动用当前时间戳填入（出现在签名参数里）。"""
    signed = sign_params({"aid": 1}, _IMG_KEY, _SUB_KEY)
    assert "wts" in signed
    assert int(signed["wts"]) > 0


def test_sign_params_anti_risk_adds_dm_fields() -> None:
    """开 anti_risk 时补上评论接口要的 dm_* 参数。"""
    signed = sign_params({"aid": 1}, _IMG_KEY, _SUB_KEY, anti_risk=True, wts=_GOLDEN_WTS)
    assert {"dm_img_list", "dm_img_str", "dm_cover_img_str", "dm_img_inter"} <= set(signed)


def test_fetch_nav_extracts_keys_from_url_filenames() -> None:
    """从 nav 的 img_url/sub_url 文件名提取 img_key/sub_key（去目录、去扩展名）。"""
    nav = fetch_nav(_FakeClient())  # type: ignore[arg-type]
    assert (nav.img_key, nav.sub_key) == (_IMG_KEY, _SUB_KEY)


def test_fetch_nav_carries_login_state() -> None:
    """同一次 nav 请求顺带给出登录态，需要登录的能力据此判断，不另发请求。"""
    assert fetch_nav(_FakeClient(is_login=True)).is_login is True  # type: ignore[arg-type]
    assert fetch_nav(_FakeClient(is_login=False)).is_login is False  # type: ignore[arg-type]
