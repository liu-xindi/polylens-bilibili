"""jq 过滤：对一批条目执行模型给的 jq 表达式，结果按形状重新编码。

表达式在子进程里执行：libjq 在进程内无法被打断，死循环与内存膨胀只能靠杀进程兜底。
用 forkserver 而非直接 fork 服务进程，避开多线程进程里 fork 的隐患。
"""

from __future__ import annotations

import json
import multiprocessing as mp
from dataclasses import asdict, fields
from multiprocessing.connection import Connection
from typing import Any

from .errors import BilibiliError
from .models import toon_table

TIMEOUT_S = 2.0
MEMORY_LIMIT = 256 * 1024 * 1024
MAX_OUTPUT_CHARS = 200_000


class JqError(BilibiliError):
    """jq 表达式无法执行：语法或运行错误、超时、超出内存或输出上限。"""


def _context() -> Any:
    methods = mp.get_all_start_methods()
    return mp.get_context("forkserver" if "forkserver" in methods else "spawn")


def _limit_memory() -> None:
    try:
        import resource
    except ImportError:  # Windows
        return
    try:
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))
    except (ValueError, OSError):  # macOS 不支持 RLIMIT_AS
        pass


def _child(expr: str, data: Any, conn: Connection) -> None:
    _limit_memory()
    try:
        import jq

        out: list[Any] = []
        size = 0
        for value in jq.compile(expr).input_value(data):
            size += len(json.dumps(value, ensure_ascii=False))
            if size > MAX_OUTPUT_CHARS:
                conn.send(("too_large", None))
                return
            out.append(value)
        conn.send(("ok", out))
    except MemoryError:
        conn.send(("memory", None))
    except ValueError as e:
        conn.send(("error", str(e)))
    finally:
        conn.close()


def run_jq(expr: str, data: Any) -> list[Any]:
    """在子进程里执行 jq，返回它输出的全部值。"""
    ctx = _context()
    recv, send = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(expr, data, send), daemon=True)
    proc.start()
    send.close()
    try:
        if not recv.poll(TIMEOUT_S):
            raise JqError(f"jq 执行超过 {TIMEOUT_S:g} 秒，已终止。")
        status, payload = recv.recv()
    except EOFError:
        raise JqError("jq 进程异常退出，可能超出内存上限。") from None
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join()
        recv.close()
    if status == "ok":
        return payload
    if status == "too_large":
        raise JqError(f"jq 输出超过 {MAX_OUTPUT_CHARS} 字符，收窄表达式后重试。")
    if status == "memory":
        raise JqError(f"jq 超出内存上限 {MEMORY_LIMIT // 1024 // 1024} MB。")
    raise JqError(str(payload))


def _is_table(value: list[Any]) -> bool:
    if not value or not all(isinstance(v, dict) for v in value):
        return False
    keys = list(value[0])
    return all(
        list(v) == keys and all(not isinstance(x, dict | list) for x in v.values())
        for v in value
    )


def encode(name: str, outputs: list[Any]) -> tuple[str, int | None]:
    """jq 输出 → (编码串, 数组元素数)。

    只有一个输出时取它本身，多个输出视为数组。同键扁平对象数组编成 TOON 表格，
    单个字符串原样返回，其他形状给紧凑 JSON。结果不是数组时元素数为 None。
    """
    value = outputs[0] if len(outputs) == 1 else outputs
    if isinstance(value, str):
        return value, None
    if isinstance(value, list):
        if _is_table(value):
            return toon_table(name, value, list(value[0])), len(value)
        if not value:
            return toon_table(name, [], []), 0
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")), len(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")), None


def encode_items(
    name: str,
    items: list[Any],
    item_type: type,
    expr: str | None,
    exclude: frozenset[str] = frozenset(),
) -> tuple[str, int | None]:
    """把数据类实例列表编码为 TOON 表格；给了 expr 时先过 jq。返回 (编码串, jq_count)。"""
    columns = [f.name for f in fields(item_type) if f.name not in exclude]
    rows = [{c: d[c] for c in columns} for d in map(asdict, items)]
    if expr is None:
        return toon_table(name, rows, columns), None
    try:
        outputs = run_jq(expr, rows)
    except JqError as e:
        raise JqError(f"{e} 可用字段：{','.join(columns)}。") from None
    return encode(name, outputs)
