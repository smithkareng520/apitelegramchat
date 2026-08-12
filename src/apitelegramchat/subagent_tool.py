# subagent_tool.py
"""
子 Agent 工具。

定位
----
让主 agent 能派生一个子 agent 去处理一个独立的子任务。
典型场景：
  - 主 agent 接到大任务后，把"研究 X 部分"派给子 agent
  - 子 agent 拥有干净的上下文（不继承主对话历史），仅看到任务描述 + 可选上下文
  - 子 agent 可以调用一组受限的工具
  - 子 agent 跑完返回最终答复，主 agent 据此继续

实现要点
--------
- 子 agent 的 LLM 调用复用 api_client + SUPPORTED_MODELS（与主 agent 同一套基础设施）
- 走一个最小化的 OpenAI 兼容 agentic loop：
    1. 构造 system_prompt + user 任务消息
    2. 调用 LLM，若返回 tool_calls 则并发执行
    3. 把 tool 结果塞回 messages，继续下一轮
    4. 直到 LLM 不再返回 tool_calls，取最终 content 作为答复
- 工具白名单：默认允许所有 SEARCH_TOOLS，调用方可限制为子集
- 安全护栏：
    - 最大循环轮数：MAX_SUBAGENT_ROUNDS = 32（可通过环境变量 SUBAGENT_MAX_ROUNDS 调整）
    - 最大单次工具结果长度：MAX_SUBAGENT_RESULT_LEN = 8000（可通过环境变量 SUBAGENT_MAX_RESULT_LEN 调整）
    - 总体超时：DEFAULT_TIMEOUT = 900s（可通过环境变量 SUBAGENT_DEFAULT_TIMEOUT 调整）
    - 禁止子 agent 递归调用 subagent 工具（防爆炸）
- 不带流式输出（子 agent 是后台任务，用户不需要看 token 流），用普通 chat.completions.create

返回
----
JSON 字符串，包含：
  ok / subagent_model / rounds / tool_calls / answer / elapsed / error
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
from typing import Any, Optional

from apitelegramchat.api_client import api_client
from apitelegramchat.config import SUPPORTED_MODELS, DEFAULT_MODEL

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    """读取整数型环境变量，解析失败时回退默认值，并可选地夹紧范围。"""
    raw = os.getenv(name)
    try:
        value = int(str(raw).strip()) if raw is not None and str(raw).strip() else int(default)
    except (TypeError, ValueError):
        value = int(default)
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


# ---------- 安全护栏 ----------
MAX_SUBAGENT_ROUNDS = _env_int("SUBAGENT_MAX_ROUNDS", 32, min_value=1, max_value=128)
MAX_SUBAGENT_RESULT_LEN = _env_int("SUBAGENT_MAX_RESULT_LEN", 8000, min_value=1000, max_value=50000)
DEFAULT_TIMEOUT = _env_int("SUBAGENT_DEFAULT_TIMEOUT", 900, min_value=60, max_value=1800)  # 秒
MAX_TASK_LEN = _env_int("SUBAGENT_MAX_TASK_LEN", 8000, min_value=1000, max_value=50000)
MAX_CONTEXT_LEN = _env_int("SUBAGENT_MAX_CONTEXT_LEN", 16000, min_value=1000, max_value=100000)
MAX_ANSWER_LEN = _env_int("SUBAGENT_MAX_ANSWER_LEN", 12000, min_value=1000, max_value=100000)


# 子 agent 不允许调用的工具（防递归 / 防爆炸 / 防资源滥用）
FORBIDDEN_TOOLS = {"subagent", "memory", "skill", "ask_user"}
# 注意：todo / bash / text_editor 仍允许，因为子 agent 可能需要查 / 写工作区文件。
# ask_user 不允许由子 agent 调用，否则会让父 agent 陷入不可控的嵌套人工等待。
# 但 memory / skill 跨会话状态复杂，子 agent 不应触碰

SUBAGENT_LLM_TIMEOUT = _env_int("SUBAGENT_LLM_TIMEOUT", 180, min_value=30, max_value=900)
SUBAGENT_TOOL_TIMEOUT = _env_int("SUBAGENT_TOOL_TIMEOUT", 120, min_value=5, max_value=900)

# 子 agent 默认可用的工具白名单（如果调用方未指定）
DEFAULT_ALLOWED_TOOLS = {
    "web_search", "fetch_url", "wikipedia", "exchange_rate",
    "book_lookup", "weather", "news", "crypto_price", "ip_geo", "qr_code",
    "geocode", "route", "distance", "poi_keyword_search",
    "poi_nearby_search", "poi_details",
    "bash", "text_editor", "todo",
    # upload/download 显式跨边界工具：子 agent 也允许使用
    "fetch_download", "stage_upload", "list_download", "list_upload",
    "present_files",
    # 不含 generate_image / video / subagent / memory / skill
    # 注：elevation / traffic / isochrone 工具已随 amap_integration.py 迁移到
    # amap-maps MCP 而移除（MCP 不提供等价能力）。
}

SUBAGENT_SYSTEM_PROMPT_TEMPLATE = """\
你是一个被父 agent 派生出来的子 agent，负责独立完成一个子任务。

任务约束：
- 你只看到本任务描述和给定的上下文，看不到父对话历史。
- 专心完成本任务，不要扩展话题。
- 可以调用提供的工具来获取信息或操作文件。
- 如果有多个彼此独立的检索、查询或操作目标，请在同一轮中一次性发出多个工具调用，不要拆成串行多轮。
- 禁止调用 subagent 工具（不能递归派生子 agent），也不能调用 ask_user（人工交互必须由父 agent 负责）。
- 完成后用一段简洁的中文答复给父 agent，长度不超过 2000 字。
- 答复里直接给结论 / 数据 / 代码 / 步骤，不要寒暄。

输出格式：
- 用 Telegram HTML 标签（<b> <i> <code> <pre> <ul> <ol> <blockquote> 等），
  严禁使用 Markdown 语法（** __ # 等）。
- 如果调用了 web_search 等检索工具，在引用信息后附上来源链接。
"""


def _filter_tools(allowed: Optional[list[str]]) -> list[dict]:
    """根据白名单从 SEARCH_TOOLS 里挑出子 agent 可用的工具定义。"""
    try:
        from apitelegramchat.search_engine import SEARCH_TOOLS
    except Exception as e:
        logger.error(f"subagent: 无法导入 SEARCH_TOOLS: {e}")
        return []
    if allowed is None:
        # 用默认白名单
        names = DEFAULT_ALLOWED_TOOLS
    else:
        # 调用方指定；过滤掉禁止的工具
        names = set()
        for n in allowed or []:
            n = str(n).strip()
            if n and n not in FORBIDDEN_TOOLS:
                names.add(n)
    out = []
    for t in SEARCH_TOOLS:
        try:
            fn_name = t.get("function", {}).get("name", "")
        except Exception:
            continue
        if fn_name in FORBIDDEN_TOOLS:
            continue
        if fn_name in names:
            out.append(t)
    return out


def _truncate(s: str, limit: int = MAX_SUBAGENT_RESULT_LEN) -> str:
    if not s:
        return ""
    if len(s) <= limit:
        return s
    return s[:limit] + "\n…[子 agent 视野已截断]"


async def _execute_tool_for_subagent(
    name: str, arguments: dict, chat_id: int
) -> str:
    """执行单个工具调用，复用主 agent 的 dispatch_tool_call。"""
    # 二次防御：禁止递归 / 禁用工具
    if name in FORBIDDEN_TOOLS:
        return f"Error: tool '{name}' is forbidden in subagent context."
    try:
        from apitelegramchat.tool_executors import dispatch_tool_call, tool_semaphore
    except Exception as e:
        return f"Error: cannot import dispatch_tool_call: {e}"
    try:
        # 复用主 agent 同一个全局信号量：多个子 agent 并行运行时，
        # 各自发起的工具调用（web_search / bash 等）仍受总并发上限约束，
        # 避免 N 个子 agent 同时爆发出 N×M 个不受控的外部请求。
        async with tool_semaphore:
            result = await asyncio.wait_for(
                dispatch_tool_call(name, arguments or {}, chat_id=chat_id),
                timeout=SUBAGENT_TOOL_TIMEOUT,
            )
        return _truncate(str(result))
    except asyncio.TimeoutError:
        return f"Error: tool '{name}' timed out in subagent context."
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return f"Error: tool '{name}' failed: {str(e)[:200]}"


async def _subagent_agentic_loop(
    client,
    model: str,
    messages: list,
    tools: list,
    chat_id: int,
    timeout_overall: float,
    progress_callback=None,
) -> dict:
    """
    最小化的 OpenAI 兼容 agentic loop。
    返回 dict：{ok, rounds, tool_calls, answer, error}

    progress_callback: 可选的 async 回调，签名 async (status_text: str) -> None。
    每轮 LLM 调用前 / 工具执行前 / 完成后都会调用，让外层能实时刷新 UI 草稿。
    """
    start = time.monotonic()
    rounds = 0
    total_tool_calls = 0
    last_error = None

    async def _report(status_text: str):
        if progress_callback:
            try:
                await progress_callback(status_text)
            except Exception:
                pass  # 进度回调失败不能影响子 agent 主流程

    # 支持工具调用？
    model_info = SUPPORTED_MODELS.get(model)
    supports_tools = bool(model_info and model_info.supports_tools and tools)

    # 用于工具结果回填
    loop_messages = list(messages)

    await _report(f"启动子 agent（模型 {getattr(model_info, 'name', model)}，{len(tools)} 个工具可用）")

    while rounds < MAX_SUBAGENT_ROUNDS:
        rounds += 1
        elapsed = time.monotonic() - start
        if elapsed > timeout_overall:
            await _report(f"整体超时（{timeout_overall}s）")
            return {
                "ok": False,
                "error": f"子 agent 整体超时（{timeout_overall}s）",
                "rounds": rounds,
                "tool_calls": total_tool_calls,
                "elapsed": elapsed,
            }

        await _report(f"第 {rounds}/{MAX_SUBAGENT_ROUNDS} 轮：LLM 思考中…（已耗时 {elapsed:.0f}s）")

        try:
            create_params = {
                "model": model,
                "messages": loop_messages,
                "stream": False,
                "max_tokens": (model_info.max_output_tokens if model_info and model_info.max_output_tokens else 8192),
            }
            if supports_tools:
                create_params["tools"] = tools
                create_params["tool_choice"] = "auto"
                create_params["parallel_tool_calls"] = True

            resp = await asyncio.wait_for(
                client.chat.completions.create(**create_params),
                timeout=SUBAGENT_LLM_TIMEOUT,
            )
        except asyncio.TimeoutError:
            last_error = "LLM 调用超时"
            logger.warning(f"subagent: LLM call timed out (round {rounds})")
            await _report(f"第 {rounds} 轮 LLM 调用超时")
            break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_error = f"LLM 调用失败: {str(e)[:200]}"
            logger.exception(f"subagent: LLM call failed (round {rounds}): {e}")
            await _report(f"第 {rounds} 轮 LLM 调用失败")
            break

        # 解析返回
        try:
            choice = resp.choices[0]
            msg = choice.message
        except (AttributeError, IndexError, TypeError) as e:
            last_error = f"无法解析 LLM 返回: {e}"
            await _report(f"第 {rounds} 轮返回解析失败")
            break

        content = getattr(msg, "content", None) or ""
        tool_calls = getattr(msg, "tool_calls", None) or []
        try:
            tool_call_names = [
                getattr(getattr(tc, "function", None), "name", "") or ""
                for tc in tool_calls
            ]
            tool_call_ids = [getattr(tc, "id", "") or "" for tc in tool_calls]
            logger.info(
                f"subagent: round={rounds}, raw_tool_calls={len(tool_calls)}, ids={tool_call_ids}, "
                f"names={tool_call_names}, content_len={len(content.strip())}"
            )
        except Exception:
            logger.exception("subagent: tool_calls 日志记录失败")

        # 没有 tool_calls → 任务结束
        if not tool_calls:
            answer = (content or "").strip()
            if len(answer) > MAX_ANSWER_LEN:
                answer = answer[:MAX_ANSWER_LEN] + "\n…[子 agent 答复已截断]"
            await _report(f"完成：{rounds} 轮，{total_tool_calls} 次工具调用，{time.monotonic() - start:.0f}s")
            return {
                "ok": True,
                "rounds": rounds,
                "tool_calls": total_tool_calls,
                "answer": answer,
                "elapsed": time.monotonic() - start,
                "error": None,
            }

        # 有 tool_calls → 把 assistant 消息塞回去，然后并发执行所有工具
        # 构造 assistant message（OpenAI 格式）
        assistant_msg: dict = {"role": "assistant", "content": content or ""}
        tc_list: list[dict] = []
        # tool_calls 需要保留原结构供 API 识别
        try:
            for tc in tool_calls:
                tc_id = getattr(tc, "id", None) or ""
                fn = getattr(getattr(tc, "function", None), "name", "") or ""
                args_raw = getattr(getattr(tc, "function", None), "arguments", "") or "{}"
                tc_list.append({
                    "id": tc_id,
                    "type": "function",
                    "function": {"name": fn, "arguments": args_raw},
                })
            if tc_list:
                assistant_msg["tool_calls"] = tc_list
        except Exception:
            pass
        loop_messages.append(assistant_msg)

        # 报告即将执行的工具
        tool_names = [tc_entry["function"]["name"] for tc_entry in tc_list if tc_entry.get("function")]
        await _report(
            f"第 {rounds}/{MAX_SUBAGENT_ROUNDS} 轮：执行工具 {' + '.join(tool_names)}…"
            f"（已耗时 {time.monotonic() - start:.0f}s）"
        )

        # 并发执行
        async def _exec_one(tc_entry: dict) -> tuple[str, str]:
            tc_id = tc_entry["id"]
            fn_name = tc_entry["function"]["name"]
            try:
                fn_args = json.loads(tc_entry["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                fn_args = {}
            result = await _execute_tool_for_subagent(fn_name, fn_args, chat_id)
            return tc_id, result

        results = await asyncio.gather(
            *[_exec_one(tc) for tc in tc_list],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                # 不应该发生，_exec_one 已吞掉异常；防御性记录
                logger.error(f"subagent: tool exec returned exception: {r}")
                continue
            tc_id, result_str = r
            loop_messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result_str,
            })
            total_tool_calls += 1

    # 跑出循环仍未拿到最终答复
    await _report(f"结束：达到最大轮数或出错（{rounds} 轮，{total_tool_calls} 次工具调用）")
    return {
        "ok": False,
        "error": last_error or f"子 agent 达到最大轮数 {MAX_SUBAGENT_ROUNDS}",
        "rounds": rounds,
        "tool_calls": total_tool_calls,
        "elapsed": time.monotonic() - start,
        "answer": "",
    }


async def execute_subagent(
    chat_id: int,
    task: str,
    context: Optional[str] = None,
    model: Optional[str] = None,
    allowed_tools: Optional[list[str]] = None,
    timeout: Optional[int] = None,
    progress_callback=None,
) -> str:
    """
    subagent 工具主入口。返回 JSON 字符串供父 agent 阅读。

    参数：
      task          子任务描述（必填）
      context       给子 agent 的额外背景信息（可选）
      model         指定子 agent 使用的模型 ID（可选，默认与父同款 DEFAULT_MODEL）
      allowed_tools 工具白名单（可选，None=默认白名单，[]=不允许任何工具）
      timeout       整体超时秒数（可选，默认 DEFAULT_TIMEOUT=900，最大 1800）
      progress_callback  可选的 async 回调 async (status_text: str) -> None，
                         每轮 LLM 调用前 / 工具执行前 / 完成后都会调用，
                         让外层能实时刷新 UI 草稿。
    """
    task = (task or "").strip()
    if not task:
        return json.dumps({"ok": False, "error": "task 不能为空", "code": "empty_task"},
                          ensure_ascii=False)
    if len(task) > MAX_TASK_LEN:
        task = task[:MAX_TASK_LEN]
    if context and len(context) > MAX_CONTEXT_LEN:
        context = context[:MAX_CONTEXT_LEN]

    # 选模型
    chosen_model = (model or DEFAULT_MODEL).strip()
    model_info = SUPPORTED_MODELS.get(chosen_model)
    if not model_info:
        # 退回默认模型
        chosen_model = DEFAULT_MODEL
        model_info = SUPPORTED_MODELS.get(chosen_model)
    if not model_info:
        return json.dumps({"ok": False, "error": f"模型不可用：{chosen_model}", "code": "bad_model"},
                          ensure_ascii=False)

    # 拿 client
    try:
        client = api_client.get_client(model_info.provider)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"API client 创建失败: {e}", "code": "no_client"},
                          ensure_ascii=False)

    # 构造消息
    system_prompt = SUBAGENT_SYSTEM_PROMPT_TEMPLATE
    user_content_parts = [f"<task>\n{task}\n</task>"]
    if context:
        user_content_parts.append(f"\n<context>\n{context}\n</context>")
    user_content_parts.append(
        "\n\n请开始执行。完成后直接给出最终答复（一段中文，Telegram HTML 格式）。"
    )
    user_content = "".join(user_content_parts)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    # 工具白名单
    tools = _filter_tools(allowed_tools)

    timeout_s = max(60, min(int(timeout or DEFAULT_TIMEOUT), 1800))

    try:
        result = await asyncio.wait_for(
            _subagent_agentic_loop(
                client, chosen_model, messages, tools, chat_id, timeout_s,
                progress_callback=progress_callback,
            ),
            timeout=timeout_s + 30,  # 外层多给 30s 缓冲，避免内层刚完成时被外层提前取消
        )
    except asyncio.TimeoutError:
        return json.dumps({
            "ok": False,
            "error": f"子 agent 整体超时（{timeout_s}s）",
            "code": "timeout",
            "model": chosen_model,
        }, ensure_ascii=False)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"subagent: unexpected error: {e}")
        return json.dumps({
            "ok": False,
            "error": f"子 agent 异常: {str(e)[:200]}",
            "code": "exception",
            "traceback": traceback.format_exc()[:500],
            "model": chosen_model,
        }, ensure_ascii=False)

    # 加上模型信息再返回
    result["model"] = chosen_model
    result["model_name"] = getattr(model_info, "name", chosen_model)
    result["task_preview"] = task[:80]
    return json.dumps(result, ensure_ascii=False)


# ---------- 富文本渲染 ----------
def _esc(text: Any) -> str:
    s = "" if text is None else str(text)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_subagent_card(payload: dict) -> str:
    """把 execute_subagent 的返回渲染成父 agent 看到的工具结果卡片。"""
    if not isinstance(payload, dict):
        return f"<p>{_esc(payload)}</p>"

    model_name = payload.get("model_name") or payload.get("model") or "?"
    rounds = payload.get("rounds", 0)
    tool_calls = payload.get("tool_calls", 0)
    elapsed = payload.get("elapsed", 0)
    task_preview = payload.get("task_preview", "")

    if not payload.get("ok"):
        error = payload.get("error", "未知错误")
        return (
            f"<p>❌ <b>子 agent 执行失败</b></p>"
            f"<p><i>模型：</i><code>{_esc(model_name)}</code> · "
            f"<i>轮数：</i>{rounds} · <i>工具调用：</i>{tool_calls} · "
            f"<i>耗时：</i>{elapsed:.1f}s</p>"
            f"<p>任务：<blockquote>{_esc(task_preview)}</blockquote></p>"
            f"<blockquote>⚠️ {_esc(error)}</blockquote>"
        )

    answer = payload.get("answer", "") or ""
    return (
        f"<p>🤖 <b>子 agent 已完成</b></p>"
        f"<p><i>模型：</i><code>{_esc(model_name)}</code> · "
        f"<i>轮数：</i>{rounds} · <i>工具调用：</i>{tool_calls} · "
        f"<i>耗时：</i>{elapsed:.1f}s</p>"
        f"<p>任务：<blockquote>{_esc(task_preview)}</blockquote></p>"
        f"<hr/>"
        f"<p><b>子 agent 答复：</b></p>"
        f"<blockquote>{answer}</blockquote>"
    )


# ---------- 工具定义（OpenAI function-calling schema） ----------
# 注意：description 字段是给 AI 阅读的「工具说明书」，全部用纯文本，
# 不使用 Markdown 语法，与系统提示词风格保持一致。
SUBAGENT_TOOL = {
    "type": "function",
    "function": {
        "name": "subagent",
        "description": (
            "Spawn a sub-agent to handle an isolated sub-task with a fresh context. The sub-agent does NOT inherit the parent's conversation history — it only sees the task description and an optional context string you provide. It runs a mini agentic loop with a restricted tool whitelist and returns a final answer. Use for: research sub-tasks, parallelizable independent sub-problems, or any case where you want to delegate a self-contained piece of work. When multiple independent subtasks exist, call subagent multiple times in the same assistant turn instead of serializing them. The sub-agent CANNOT recursively call subagent / memory / skill."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "_description": {
                    "type": "string",
                    "description": "简述本次操作目的（≤60字）。示例：派子 agent 调研量子计算最新进展"
                },
                "task": {
                    "type": "string",
                    "description": "子任务描述。明确说明要让子 agent 产出什么。最长 8000 字符。"
                },
                "context": {
                    "type": "string",
                    "description": "可选的背景上下文。最长 16000 字符。可用来传递父对话中的相关信息。"
                },
                "model": {
                    "type": "string",
                    "description": "可选的模型 ID（来自 SUPPORTED_MODELS）。默认与父 agent 同款。"
                },
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选的工具白名单。缺省时使用安全默认集（web_search/fetch_url/wikipedia/weather/bash/text_editor/todo 等）。传空数组则禁用所有工具。无论是否指定，subagent/memory/skill/ask_user 始终禁用。"
                },
                "timeout": {
                    "type": "integer",
                    "description": "整体超时（秒）。默认 180，最大 600。",
                    "default": 180
                }
            },
            "required": ["task"]
        },
        "input_examples": [
            {"task": "调研 2025 年最热门的 3 个开源 LLM 项目，每个给出 stars / license / 一句话特色", "allowed_tools": ["web_search", "fetch_url"]},
            {"task": "把 workspace/report.md 翻译成英文并保存为 report_en.md", "allowed_tools": ["text_editor", "bash"]},
            {"task": "查北京今天的天气，并给出适合的穿搭建议", "allowed_tools": ["weather"]},
            {"task": "用一句话总结这段文字", "context": "……长文本……", "allowed_tools": []}
        ]
    }
}
