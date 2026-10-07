# -*- coding: utf-8 -*-
"""协议适配器基座：所有协议适配器的公共契约。

协议适配器（Protocol Adapter）是"Model -> Protocol"路由的执行端：
ModelConfig 声明 protocol 标签，protocols/registry 据此把请求分发给
对应适配器。适配器对上暴露统一接口（chat agentic 循环 / 图像任务），
对下负责把内部消息（core/messages.Message）渲染为该协议的线上 JSON
并处理流式事件。

新增协议的步骤：
  1. 在 config._VALID_PROTOCOLS 加协议标签；
  2. 新建 protocols/<protocol>.py 实现 ChatProtocolAdapter；
  3. 在 protocols/registry 的 CHAT_PROTOCOLS 注册。
其余模块（ai_handlers / api_client / agentic_loops）零改动。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    # 仅供类型注解；运行时由调用方传入，避免循环导入。
    from ai.draft_manager import DraftManager
    from config import ModelConfig
    from responses_state import TurnState

def invalidate_responses_chain_for(builder: "DraftManager") -> None:
    """非 Responses 协议适配器在写入历史前作废 Responses 链头。

    三个非 Responses 适配器（openai_chat / anthropic_messages /
    gemini_native）路由到这里意味着本回合问答不在服务端 response
    chain 里；不作废的话，下次切回 Responses 协议会沿旧链续写，静默
    丢失本回合上下文。这是纯内存状态操作（responses_state 的进程内
    dict），不存在可合理忽略的失败模式——异常直接向上传播（导入失败
    属于部署错误，链头失效失败属于状态机错误，都应当立即暴露而不是
    静默降级后让下一回合丢上下文）。
    """
    from responses_state import mark_legacy_divergence

    chat_id = getattr(builder, "chat_id", None)
    if chat_id is not None:
        mark_legacy_divergence(chat_id)

class ChatProtocolAdapter(ABC):
    """聊天协议适配器：包一层该协议的 agentic 循环。

    返回 (final_content, final_usage, new_history_entries)。

    ``turn``（responses_state.TurnState，可选）：
    本回合的对话状态快照，由 get_ai_response 在回合登记时创建、经
    _call_api 透传到这里。目前只有 openai_responses 适配器会用它来
    判断是否可以复用服务端会话（见 ai/responses_bridge.py）；其余
    适配器（openai_chat / anthropic_messages / gemini_native）按基类
    默认忽略该参数即可——它们的协议本身没有等价的服务端会话概念，
    继续走"每轮全量重发 canonical history"的既有行为，不受影响。
    """

    #: 协议标签（与 config._VALID_PROTOCOLS 的取值一致）
    name: str = ""

    @abstractmethod
    async def run_agent_loop(
        self,
        *,
        current_model: str,
        model_info: "ModelConfig",
        messages: list,
        builder: "DraftManager",
        tools: Optional[list[Any]] = None,
        supports_tools: bool = True,
        journal: Optional[list[Any]] = None,
        turn: Optional["TurnState"] = None,
        workspace_namespace: Optional[str] = None,
    ) -> tuple[str | None, Any, list]:
        """执行该协议的 agentic 循环（含工具执行与历史追加）。"""
        raise NotImplementedError

__all__ = ["ChatProtocolAdapter"]
