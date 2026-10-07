"""ai_handlers 拆分后的共享常量。

这些常量原先定义在 ai_handlers.py 顶部，被多个拆分出的子模块共用，
集中放在这里作为单一数据源，避免多处重复定义导致后续修改遗漏。
"""
import os
import aiohttp

from config import get_openrouter_provider_preferences


def _positive_env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


# Agnes 图像能力：请求校验与 UI 选项共用的单一数据源。
AGNES_IMAGE_SIZE_TIERS = ("1K", "2K", "3K", "4K")
AGNES_IMAGE_RATIOS = ("1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9")


# 工具调用限制。
# 单轮最多执行 100 次工具调用；超出后进入无工具总结路径。
import tool_names as _tn

MAX_TOOL_CALLS = 100
MAX_PLAIN_TEXT_TOOL_CALL_RETRIES = 3
TOOL_ERROR_STREAK_LIMIT = 3
TOOL_CALL_TIMEOUT = 12
OPENROUTER_PROVIDER_PREFERENCES = get_openrouter_provider_preferences()
# 网络、MCP 和交付类工具使用更宽松的外层超时；内部调用仍负责自己的超时与重试。
LONG_RUNNING_TOOLS = {
    _tn.WEB_SEARCH, _tn.FETCH_URL, _tn.WIKIPEDIA, _tn.EXCHANGE_RATE, _tn.WEATHER,
    _tn.TEXT_EDITOR, _tn.DELIVER_REPLY,
    *_tn.GAODE_TOOLS,
}
LONG_TOOL_CALL_TIMEOUT = 45
# bash 单独使用更长的外层超时，以覆盖沙箱冷启动和较慢的 skill 工作流。
# 外层略高于沙箱单命令上限，为清理留出缓冲。
BASH_TOOLS = {_tn.BASH}
BASH_TOOL_CALL_TIMEOUT = 310
# 子 agent 包含独立的多轮 agentic loop，外层超时必须覆盖其完整执行窗口。
SUBAGENT_TOOLS = {_tn.SUBAGENT}
SUBAGENT_OUTER_TIMEOUT = _positive_env_int("SUBAGENT_OUTER_TIMEOUT", 930, minimum=1)  # 900s 子 agent 上限 + 30s 缓冲
# 仅允许无写副作用且彼此独立的工具并发执行。
# 默认 fail-closed：未加入集合的新工具保持串行；依赖前序结果或修改状态的工具也保持串行。
CONCURRENT_SAFE_TOOLS = frozenset(_tn.SEARCH_TOOLS_MCP | _tn.GAODE_TOOLS | SUBAGENT_TOOLS)
# 统一图像工具 generate_image（image_url 缺省=文生图，提供=编辑），纳入超时豁免。
IMAGE_GEN_TOOLS = {_tn.GENERATE_IMAGE}
# 视频生成工具：内部已有 5 分钟轮询超时，外层 wait_for 必须不设超时，
# 否则会被 TOOL_CALL_TIMEOUT（当前 12s）过早杀掉（与 IMAGE_GEN_TOOLS 同样豁免）。
VIDEO_GEN_TOOLS = {_tn.GENERATE_VIDEO}
# 所有需要跳过外层超时的"长耗时生成类"工具集合
MEDIA_GEN_TOOLS = IMAGE_GEN_TOOLS | VIDEO_GEN_TOOLS

# 写操作工具：打断时不能简单取消，必须等待或记录最终状态。
# bash / text_editor / memory / todo 的执行可能已经产生外部副作用，不能把取消视为回滚。
# 打断后通过 asyncio.shield 让任务继续运行，并由恢复流程记录最终状态。
# 只读网络工具可直接取消。
DETACHED_ON_INTERRUPT_TOOLS = frozenset({
    _tn.BASH, _tn.TEXT_EDITOR, _tn.MEMORY, _tn.TODO,
})
# 脱离任务的最大等待时间。超时后按“结果未知”记录，避免后台任务无限占用资源。
DETACHED_TOOL_FINAL_WAIT = _positive_env_int("DETACHED_TOOL_FINAL_WAIT", 1800, minimum=1)
# 流式请求由应用层 deadline 控制总时长；aiohttp 只负责连接和长时间无数据的兜底超时。
STREAM_CLIENT_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10, sock_read=180)
# 放大 SSE 缓冲区，避免大型 functionCall 行超过 aiohttp 的默认行长限制。
STREAM_READ_BUFSIZE = 2 * 1024 * 1024
