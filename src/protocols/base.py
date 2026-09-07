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


class ChatProtocolAdapter(ABC):
    """聊天协议适配器：包一层该协议的 agentic 循环。

    统一签名与旧的 ai_handlers._call_api 分发约定一致，返回
    (final_content, final_usage, new_history_entries)。
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
    ) -> tuple[str | None, Any, list]:
        """执行该协议的 agentic 循环（含工具执行与历史追加）。"""
        raise NotImplementedError


__all__ = ["ChatProtocolAdapter"]
