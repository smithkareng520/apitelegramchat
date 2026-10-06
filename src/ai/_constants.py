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


# ---------- Agnes 图像能力（官方 size 档位 / ratio 集合，单一数据源） ----------
# media_generation（请求参数校验）与 media_wizard（卡片选项）共同消费；
# 厂商能力变更只需改这里。
AGNES_IMAGE_SIZE_TIERS = ("1K", "2K", "3K", "4K")
AGNES_IMAGE_RATIOS = ("1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9")


# ---------- 工具调用相关 ----------
# 每一轮用户请求最多执行 100 次真实工具调用；超过后进入无工具总结路径。
# 不依赖模型的单轮并发数量，调用预算按实际执行的工具数精确累计。
import tool_names as _tn

MAX_TOOL_CALLS = 100
MAX_PLAIN_TEXT_TOOL_CALL_RETRIES = 3
TOOL_ERROR_STREAK_LIMIT = 3
TOOL_CALL_TIMEOUT = 12
OPENROUTER_PROVIDER_PREFERENCES = get_openrouter_provider_preferences()
# 网络类工具：内部已有自己的超时控制（fetch_url 30s 总超时，web_search 多端点 + warmup），
# 但外层 12s 会过早杀掉它们，给一个更宽松的 45s 上限兜底。
#
# deliver_reply（/show off 静默模式交付最终回复）：sendRichMessage 带重试
# （最坏 ~45s+），45s 上限与之匹配。
#
# MCP 检索工具与高德地图工具：内部 stdio / HTTP 调用各自带超时与重试，
# 外层统一 45s 兜底（含 MCP 会话冷启动 ~2-3s）。
LONG_RUNNING_TOOLS = {
    _tn.WEB_SEARCH, _tn.FETCH_URL, _tn.WIKIPEDIA, _tn.EXCHANGE_RATE, _tn.WEATHER,
    _tn.TEXT_EDITOR, _tn.DELIVER_REPLY,
    *_tn.GAODE_TOOLS,
}
LONG_TOOL_CALL_TIMEOUT = 45
# bash 工具单独一档，比 LONG_RUNNING_TOOLS 更宽松：
#   - 沙箱首次启动要 fork+exec+安装 Landlock 规则（现于 internal_bash MCP
#     子进程内执行，冷启动含 python 子进程拉起）；
#   - skill 工作流常见的命令（pip/npm 安装、LibreOffice soffice 转换、pandoc）
#     冷启动经常需要 10~30s+，甚至更久。
#   - 内层沙箱默认允许单个命令运行 300s；外层给 310s，额外留 10s 清理缓冲，
#     确保不会出现外层先杀掉仍在正常运行的沙箱进程。
BASH_TOOLS = {_tn.BASH}
BASH_TOOL_CALL_TIMEOUT = 310
# 子 agent 工具：内部跑自己的多轮 agentic loop（每轮一次 LLM 调用 + 可能的工具调用）。
# 默认 900s，用户可配到 1800s。外层必须给足够长的超时，否则主工具层会提前杀掉它。
SUBAGENT_TOOLS = {_tn.SUBAGENT}
SUBAGENT_OUTER_TIMEOUT = _positive_env_int("SUBAGENT_OUTER_TIMEOUT", 930, minimum=1)  # 900s 子 agent 上限 + 30s 缓冲
# 并发安全工具：同一批里"连续"出现的这类调用并发执行，其余工具串行（见
# tool_call_loop 的分批屏障）。准入条件——不改动工作区/外部状态，或是互相
# 独立的长耗时任务：
#   - 搜索类 MCP（web_search / fetch_url / wikipedia / exchange_rate / weather）
#     与高德地图：纯只读网络查询，不碰工作区文件；
#   - 子 agent：独立的多轮任务，串行等待代价大。
# 默认串行（fail-closed）：新增工具不在此集合就不会并发；bash / text_editor /
# memory / todo / present_files / 生成类工具有写副作用或依赖前序产物，保持串行。
# text_editor view 虽只读，但会读到同批子 agent / bash 写出的文件，不放入。
CONCURRENT_SAFE_TOOLS = frozenset(_tn.SEARCH_TOOLS_MCP | _tn.GAODE_TOOLS | SUBAGENT_TOOLS)
# 统一图像工具 generate_image（image_url 缺省=文生图，提供=编辑），纳入超时豁免。
IMAGE_GEN_TOOLS = {_tn.GENERATE_IMAGE}
# 视频生成工具：内部已有 5 分钟轮询超时，外层 wait_for 必须不设超时，
# 否则会被 TOOL_CALL_TIMEOUT（当前 12s）过早杀掉（与 IMAGE_GEN_TOOLS 同样豁免）。
VIDEO_GEN_TOOLS = {_tn.GENERATE_VIDEO}
# 所有需要跳过外层超时的"长耗时生成类"工具集合
MEDIA_GEN_TOOLS = IMAGE_GEN_TOOLS | VIDEO_GEN_TOOLS

# ---------- 写操作工具（五阶段打断规范·阶段4b） ----------
# 一旦开始执行就不能安全中止的工具：中止 ≠ 回滚（写库 / 转账 / 文件
# 落盘 / 记忆与待办写入半途掐断，留下的不是"未执行"而是"结果未知"）。
# 打断/超时发生时 asyncio.shield 让执行脱离主进程在后台继续，确切终态
# （成功/失败/回滚）由后台任务死等并回写历史（见 tool_call_loop 的
# detach 机制与 turn_recovery.writeback_detached_tool_result）：
#   - bash          ：可执行任意写操作（写库 / 转账 curl / 文件修改），
#                     从命令文本无法可靠判断读写，统一按写操作处理；
#                     （internal_bash MCP 子进程内执行，shield 等待 MCP 调用）
#   - text_editor   ：文件创建 / 编辑 / 删除，落盘不可逆；
#   - memory        ：长期记忆写入；
#   - todo          ：待办事项写入。
# 只读工具（web_search / fetch_url 等）不在此列：取消传播直接掐断底层
# 网络请求（沉没成本只有一点流量），占位回执带 aborted 状态即可。
DETACHED_ON_INTERRUPT_TOOLS = frozenset({
    _tn.BASH, _tn.TEXT_EDITOR, _tn.MEMORY, _tn.TODO,
})
# 脱离工具的后台死等上限：转账 / 写库等操作必须拿到确切终态，但挂死
# 的执行（如卡死的沙箱命令）不能无限占用后台任务——到顶后按"结果未知"
# 记录并放弃等待（与子 agent 用户可配上限同量级）。
DETACHED_TOOL_FINAL_WAIT = _positive_env_int("DETACHED_TOOL_FINAL_WAIT", 1800, minimum=1)
# 流式请求不设 aiohttp total：整条流的总时长由 ai.streaming 的应用层 deadline 管理，
# 否则长思考 / 长输出会在第 300 秒被误杀。sock_read 只兜底"连接静默"。
STREAM_CLIENT_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10, sock_read=180)
# SSE 按行读取，aiohttp 的行长上限是 2×read_bufsize（默认 128KiB）；
# 大参数 functionCall 会超限并抛 ValueError('Chunk too big')，故放宽到 4MiB。
STREAM_READ_BUFSIZE = 2 * 1024 * 1024
