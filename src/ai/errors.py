"""AI 响应层的领域异常与响应边界校验。"""
from __future__ import annotations

from typing import Any


class AIStreamTimeoutError(TimeoutError):
    """流在应用层 idle / total 期限内没有完成。

    ``kind`` 标明触发的是哪道闸门（``"idle"`` / ``"total"``）：
    total 是整条流的硬期限，触发后绝不参与零输出重试；idle 表示
    两个真实事件之间隔太久，可按既有策略重试。
    """

    def __init__(self, message: str = "", *, kind: str = "idle") -> None:
        super().__init__(message)
        self.kind = kind


class AIResponseParseError(ValueError):
    """上游返回了 HTTP 成功但结构不可用的响应（例如 choices 为空）。"""


class AIResponseProtocolError(RuntimeError):
    """Responses 流违反了协议生命周期（例如缺少终态事件）。"""


class ResponsesProtocolError(AIResponseProtocolError):
    """Responses 请求协议不变量被破坏（例如 continuation input 为空）。

    在真正调用 SDK 之前抛出：绝不发送 ``input: []``，也绝不伪造一条
    假消息去满足供应商的传输契约。
    """


def first_choice(response: Any, *, label: str = "") -> Any:
    """取非流式响应的第一个 choice；缺失时抛带上游错误信息的 AIResponseParseError。

    部分网关会在内容审核 / 上游故障时返回 200 + ``choices: null`` 或 ``[]``，
    直接下标访问只会得到无信息量的 IndexError / TypeError。
    """
    choices = getattr(response, "choices", None)
    if choices:
        return choices[0]
    detail = getattr(response, "error", None)
    prefix = f"[{label}] " if label else ""
    suffix = f": {detail}" if detail else ""
    raise AIResponseParseError(f"{prefix}上游响应不含 choices{suffix}")
