"""Cookie 的读写。凭据只存本地文件。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def cookie_file_path() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache / "polylens-bilibili" / "cookie"


def load_cookie() -> str:
    path = cookie_file_path()
    if path.exists():
        return path.read_text().strip()
    return ""


def write_private(path: Path, text: str) -> None:
    """只有本人可读写地落盘：目录 0700，文件从创建起就是 0600，原子替换。

    不依赖进程 umask，也不走"先写后 chmod"，避免文件以宽权限存在过哪怕一瞬。
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)  # 目录已存在时 mkdir 的 mode 不生效
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")  # mkstemp 即 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_cookie(cookie: str) -> Path:
    path = cookie_file_path()
    write_private(path, cookie)
    return path


def delete_cookie() -> bool:
    """删除 cookie 文件。返回是否原本存在。"""
    path = cookie_file_path()
    if path.exists():
        path.unlink()
        return True
    return False
