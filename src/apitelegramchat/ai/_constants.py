"""ai_handlers 拆分后的共享常量。

这些常量原先定义在 ai_handlers.py 顶部，被多个拆分出的子模块共用，
集中放在这里作为单一数据源，避免多处重复定义导致后续修改遗漏。
"""
import os
import aiohttp

from apitelegramchat.config import get_openrouter_provider_preferences


def _positive_env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


# ---------- 工具调用相关 ----------
# 每一轮用户请求最多执行 100 次真实工具调用；超过后进入无工具总结路径。
# 不依赖模型的单轮并发数量，调用预算按实际执行的工具数精确累计。
MAX_TOOL_CALLS = 100
MAX_PLAIN_TEXT_TOOL_CALL_RETRIES = 3
TOOL_ERROR_STREAK_LIMIT = 3
TOOL_CALL_TIMEOUT = 12
OPENROUTER_PROVIDER_PREFERENCES = get_openrouter_provider_preferences()
# 网络类工具：内部已有自己的超时控制（fetch_url 30s 总超时，web_search 多端点 + warmup），
# 但外层 12s 会过早杀掉它们，给一个更宽松的 45s 上限兜底。
#
# text_editor 需要初始化一次隔离 workspace，但不再做工作区级 R2 全量同步。
# 编辑操作只持久化被编辑的具体文件。
#
# deliver_reply（/show off 静默模式交付最终回复）：sendRichMessage 带重试
# （最坏 ~45s+），45s 上限与之匹配。旧的 send_message_to_user 已移除。
LONG_RUNNING_TOOLS = {"web_search", "fetch_url", "text_editor", "deliver_reply"}
LONG_TOOL_CALL_TIMEOUT = 45
# bash 工具单独一档，比 LONG_RUNNING_TOOLS 更宽松：
#   - 沙箱首次启动要 fork+exec+安装 Landlock 规则；
#   - skill 工作流常见的命令（pip/npm 安装、LibreOffice soffice 转换、pandoc）
#     冷启动经常需要 10~30s+，甚至更久。
#   - 内层沙箱默认允许单个命令运行 300s；外层给 310s，额外留 10s 清理缓冲，
#     确保不会出现外层先杀掉仍在正常运行的沙箱进程。
BASH_TOOLS = {"bash"}
BASH_TOOL_CALL_TIMEOUT = 310
# "消费者"工具：依赖同批其他工具（bash/text_editor 等）已经先把文件 staged
# 到 upload/ 后才能正确工作。如果让它们和 bash 在同一批 asyncio.gather 里
# 并行执行，bash 的 cp/write 还没落盘时 present_files 就会读到空目录，
# 触发"file not found in upload/"的误导性错误（历史上发生过：模型需要
# 多花 2 轮才能补救）。tool_call_loop 在检测到同批同时存在 producer 和
# consumer 时，会显式串行化执行：先 gather 所有 producer，再 gather
# 所有 consumer。仅对"显式声明"的消费者生效，不影响其他工具对的并行性。
CONSUMER_TOOLS = {"present_files"}
# 子 agent 工具：内部跑自己的多轮 agentic loop（每轮一次 LLM 调用 + 可能的工具调用）。
# 默认 900s，用户可配到 1800s。外层必须给足够长的超时，否则主工具层会提前杀掉它。
SUBAGENT_TOOLS = {"subagent"}
SUBAGENT_OUTER_TIMEOUT = _positive_env_int("SUBAGENT_OUTER_TIMEOUT", 930, minimum=1)  # 900s 子 agent 上限 + 30s 缓冲
IMAGE_GEN_TOOLS = {"generate_image_from_text", "edit_image_with_reference"}
# 视频生成工具：内部已有 5 分钟轮询超时，外层 wait_for 必须不设超时，
# 否则会被 TOOL_CALL_TIMEOUT=10 秒杀掉（与 IMAGE_GEN_TOOLS 同样的处理）。
VIDEO_GEN_TOOLS = {"generate_video"}
# 所有需要跳过外层超时的"长耗时生成类"工具集合
MEDIA_GEN_TOOLS = IMAGE_GEN_TOOLS | VIDEO_GEN_TOOLS
TIMEOUT = aiohttp.ClientTimeout(total=300, connect=10, sock_read=180)
