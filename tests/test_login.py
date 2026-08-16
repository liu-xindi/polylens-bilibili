"""登录流程单元测试：mock HTTP，不依赖网络。"""

from __future__ import annotations

import json
from unittest.mock import patch

from polylens_bilibili.api._http import HttpClient
from polylens_bilibili.api._login import check_qr_login, start_qr_login
from polylens_bilibili.credentials import delete_cookie, load_cookie, save_cookie
from polylens_bilibili.models import LoginStatus

# ── 凭据读写 ────────────────────────────────────────────────────────────────


def test_load_cookie_reads_file(tmp_path) -> None:
    path = tmp_path / "cookie"
    path.write_text("file_cookie")
    with patch("polylens_bilibili.credentials.cookie_file_path", return_value=path):
        assert load_cookie() == "file_cookie"


def test_load_cookie_empty_when_file_absent(tmp_path) -> None:
    with patch(
        "polylens_bilibili.credentials.cookie_file_path", return_value=tmp_path / "missing"
    ):
        assert load_cookie() == ""


def test_save_cookie_creates_parent_dirs(tmp_path) -> None:
    path = tmp_path / "sub" / "cookie"
    with patch("polylens_bilibili.credentials.cookie_file_path", return_value=path):
        result = save_cookie("my_cookie")
    assert result == path
    assert path.read_text() == "my_cookie"


def test_delete_cookie_reports_whether_it_existed(tmp_path) -> None:
    path = tmp_path / "cookie"
    with patch("polylens_bilibili.credentials.cookie_file_path", return_value=path):
        assert delete_cookie() is False
        save_cookie("x")
        assert delete_cookie() is True
        assert not path.exists()


# ── 扫码登录 ────────────────────────────────────────────────────────────────


def _client() -> HttpClient:
    return HttpClient()


def test_start_qr_login_returns_session() -> None:
    client = _client()
    payload = {"qrcode_key": "key123", "url": "https://example.com/qr"}
    with patch.object(client, "get_json", return_value=payload) as mock_get:
        session = start_qr_login(client)
    mock_get.assert_called_once()
    assert session.key == "key123"
    assert session.url == "https://example.com/qr"


def _poll(client: HttpClient, sub_code: int, set_cookies: list[str] | None = None):
    body = json.dumps({"code": 0, "data": {"code": sub_code}}).encode()
    with patch.object(
        client, "get_bytes_and_set_cookies", return_value=(body, set_cookies or [])
    ):
        return check_qr_login(client, "key123")


def test_check_qr_login_waiting() -> None:
    result = _poll(_client(), 86101)
    assert result.status is LoginStatus.WAITING
    assert result.cookie == ""


def test_check_qr_login_scanned() -> None:
    assert _poll(_client(), 86090).status is LoginStatus.SCANNED


def test_check_qr_login_expired() -> None:
    assert _poll(_client(), 86038).status is LoginStatus.EXPIRED


def test_check_qr_login_unknown_code_treated_as_expired() -> None:
    """未知状态码当作过期：让模型重新发码，是安全的降级。"""
    assert _poll(_client(), 99999).status is LoginStatus.EXPIRED


def test_check_qr_login_success_extracts_cookies() -> None:
    result = _poll(
        _client(),
        0,
        [
            "SESSDATA=abc; Domain=.bilibili.com; Path=/; HttpOnly",
            "bili_jct=xyz; Domain=.bilibili.com; Path=/",
            "DedeUserID=123; Domain=.bilibili.com; Path=/",
        ],
    )
    assert result.status is LoginStatus.SUCCESS
    assert "SESSDATA=abc" in result.cookie
    assert "bili_jct=xyz" in result.cookie
    assert "DedeUserID=123" in result.cookie
