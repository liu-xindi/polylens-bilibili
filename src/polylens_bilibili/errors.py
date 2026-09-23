"""统一错误层：契约的一部分。"""

from __future__ import annotations


class BilibiliError(Exception):
    """所有可预期错误的基类。工具层据此映射为对模型友好的错误信息。"""


class AuthRequiredError(BilibiliError):
    """该能力需要登录，但当前未登录。"""

    def __init__(self, capability: str) -> None:
        super().__init__(
            f"能力 {capability} 需要登录。用 start_qr_login 扫码登录后重试。"
        )
        self.capability = capability


class RateLimitedError(BilibiliError):
    """请求触发风控限速。中断即整体失败，不返回半程结果。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
