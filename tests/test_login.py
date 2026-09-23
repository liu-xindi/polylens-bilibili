"""登录流程单元测试：mock HTTP，不依赖网络。"""

from __future__ import annotations

import json
from unittest.mock import patch

from polylens_bilibili.api._http import HttpClient
from polylens_bilibili.api._login import check_qr_login, start_qr_login
from polylens_bilibili.credentials import delete_cookie, load_cookie, save_cookie
from polylens_bilibili.models import QrStatus

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
    assert result.status is QrStatus.WAITING
    assert result.cookie == ""


def test_check_qr_login_scanned() -> None:
    assert _poll(_client(), 86090).status is QrStatus.SCANNED


def test_check_qr_login_expired() -> None:
    assert _poll(_client(), 86038).status is QrStatus.EXPIRED


def test_check_qr_login_unknown_code_treated_as_expired() -> None:
    """未知状态码当作过期：让模型重新发码，是安全的降级。"""
    assert _poll(_client(), 99999).status is QrStatus.EXPIRED


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
    assert result.status is QrStatus.SUCCESS
    assert "SESSDATA=abc" in result.cookie
    assert "bili_jct=xyz" in result.cookie
    assert "DedeUserID=123" in result.cookie


def _mode(p) -> int:
    import stat
    return stat.S_IMODE(p.stat().st_mode)


def test_save_cookie_is_private_regardless_of_umask(tmp_path, monkeypatch) -> None:
    """明文 Cookie 可直接登录账号，不能跟着宽松的 umask 落成 0644。"""
    import os

    from polylens_bilibili import credentials

    path = tmp_path / "cache" / "polylens-bilibili" / "cookie"
    monkeypatch.setattr(credentials, "cookie_file_path", lambda: path)
    old = os.umask(0o022)
    try:
        credentials.save_cookie("SESSDATA=x")
    finally:
        os.umask(old)
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    assert path.read_text() == "SESSDATA=x"


def test_write_private_tightens_existing_file_and_dir(tmp_path) -> None:
    from polylens_bilibili.credentials import write_private

    d = tmp_path / "d"
    d.mkdir(mode=0o755)
    f = d / "secret"
    f.write_text("old")
    f.chmod(0o644)
    write_private(f, "new")
    assert _mode(f) == 0o600
    assert _mode(d) == 0o700
    assert f.read_text() == "new"
    assert [p.name for p in d.iterdir()] == ["secret"]  # 临时文件不残留
