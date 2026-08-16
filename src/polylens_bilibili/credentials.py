"""Cookie 的读写。凭据只存本地文件。"""

from __future__ import annotations

import os
from pathlib import Path


def cookie_file_path() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache / "polylens-bilibili" / "cookie"


def load_cookie() -> str:
    path = cookie_file_path()
    if path.exists():
        return path.read_text().strip()
    return ""


def save_cookie(cookie: str) -> Path:
    path = cookie_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cookie)
    return path


def delete_cookie() -> bool:
    """删除 cookie 文件。返回是否原本存在。"""
    path = cookie_file_path()
    if path.exists():
        path.unlink()
        return True
    return False
