# 代码优化与脏代码清理 — 变更清单

本次代码审计与优化覆盖了 `apitelegramchat` 全部核心模块。下文按
"安全 / 逻辑 / 健壮性 / 脏代码"四类列出实际改动。所有变更均保持
向后兼容（未变更任何公开 API 签名），可以直接覆盖原代码运行。

## 1. 安全（HIGH 严重度）

### 1.1 修复 `escape_html` 长期为 no-op 的 HTML 注入漏洞
- 文件：`src/apitelegramchat/utils.py`
- 问题：`escape_html()` 实现是 `return text`（完全不做转义），
  但项目里有 60+ 处调用点依赖它防御 HTML 注入。这意味着过去
  每一处把 LLM/上游 API 数据通过 `escape_html` 拼进 Telegram
  HTML 的代码实际都没生效，存在被动 HTML 注入风险。
- 修复：`escape_html` 现在真正做转义（智能 ampersand 处理 +
  `<` / `>`），并接受任意非字符串输入。同时移除 `ai_handlers.py`
  里 `logger.setLevel(DEBUG)` 的覆盖（它会让本模块无视 LOG_LEVEL
  把所有日志以 DEBUG 透传）。

### 1.2 阻止 Telegram bot token 泄露给 LLM API
- 文件：`src/apitelegramchat/ai/attachment_content.py`
- 问题：`_resolve_public_attachment_url()` 会返回
  `https://api.telegram.org/file/bot{TOKEN}/{path}`，该返回值
  被拼进 `_build_attachment_fallback_text()` 写入发送给 LLM 的
  prompt 文本——直接把 bot token 暴露给第三方模型 API。
- 修复：`_resolve_public_attachment_url()` 不再返回 Telegram
  直链，只返回 R2 公开 URL；R2 不可用时返回空串，让调用方降级
  为 file_id 文本。

### 1.3 阻止 token 经 `failed` 列表回传 LLM
- 文件：`src/apitelegramchat/tool_executors.py`
- 问题：`execute_present_files` 在异常路径上把 `str(e)` 直接
  塞进 `failed` 列表。`aiohttp.ClientError` 的 str 形式经常
  把请求 URL（含 `bot{TOKEN}`）一起打印出来，这个 list 又会
  被作为 tool_result 返回给 LLM。
- 修复：异常分支显式检测 `BASE_URL in str(e)`，命中则替换为
  `[redacted url]`，避免 token 泄露。

### 1.4 `execute_fetch_url` SSRF / DNS-rebinding 加固
- 文件：`src/apitelegramchat/ai/media_generation.py`
- 问题：`poll_url` 和 `polling_url` 都直接来自上游 API 响应，
  原代码把它们原样拿来做 HTTP GET——恶意/被攻陷的上游可让 bot
  去访问内网 metadata endpoint（169.254.169.254）或本地端口。
- 修复：`task_id` 强制白名单 `^[A-Za-z0-9_-]{1,128}$`；OpenRouter
  的 `polling_url` 强制只允许 `openrouter.ai` / `api.openrouter.ai`
  主机，其它一律拒绝。

### 1.5 `ask_user_tool` HTML 注入
- 文件：`src/apitelegramchat/ask_user_tool.py`
- 问题：`_question_html` / `_answered_html` 把 LLM 给的
  `question` / `label` / `description` 与用户自由文本 `value`
  原样插入 Telegram HTML，攻击者只要让 LLM 输出
  `<img onerror=...>` 就能在用户客户端执行任意 HTML。
- 修复：所有插值统一走 `escape_html`（已修正后的真转义版）。

### 1.6 `is_inside_upload_or_download` 改为 fail-closed
- 文件：`src/apitelegramchat/workspace_paths.py`
- 问题：该函数被 bash sandbox 用来拒绝在 staging 目录中执行
  命令。原代码在路径解析异常时返回 `False`（fail-open），意味着
  任何解析失败都会让 sandbox 误以为 cwd 不在 staging 中并允许
  执行——绕过安全边界。
- 修复：所有异常分支返回 `True`（视为在 staging 内），让 sandbox
  拒绝执行。

### 1.7 `verify_security.py` symlink 竞争 + env 变量名泄露
- 文件：`src/apitelegramchat/verify_security.py`
- 问题：
  - 测试 workspace 用固定路径 `/tmp/verify_workspace`，本地攻击者
    可以提前创建该路径并指向 `/etc`，让安全自检把测试文件写进 `/etc`。
  - `check_env_scrubbed` 把残留 env 变量名（如 `STRIPE_SECRET_KEY`）
    直接 print 到 stdout，本身就是一种信息泄露。
- 修复：使用 `tempfile.mkdtemp` 在私有 `data_root()` 下创建唯一
  目录；env 变量名只在 DEBUG 级别记录到 logger，stdout 只显示数量。
  同时把 landlock 测试超时从 5s 提到 15s（冷容器里常超 5s）。

## 2. 逻辑 bug

### 2.1 `<br>` 标签替换的正则从未生效
- 文件：`src/apitelegramchat/tool_executors.py` L644
- 问题：写的是 `r"<br\\s*/?\\s*>"` —— 在 raw string 里 `\\s`
  是字面量 `\s` 而非正则空白匹配，导致 `<br>` 永远不会被替换
  成换行。结果：含 `<br>` 的错误响应在 UI 上展示成"未剥离的
  HTML 片段"。
- 修复：改为 `r"<br\s*/?\s*>"`。

### 2.2 `bash -n` 在非英文 locale 下完全失效
- 文件：`src/apitelegramchat/tool_executors.py`
- 问题：持久 bash shell 防卡死检测靠 `bash -n -c <cmd>` 的
  `"unexpected EOF"` 错误信息识别未闭合 heredoc / 引号。但
  原代码不设 `LC_ALL`，在 `zh_CN.UTF-8` / `ja_JP.UTF-8` 环境
  下 bash 输出本地化错误信息（"未预期的文件结束符"），英文
  子串匹配失效——本应被路由到隔离执行的危险命令直接进入持久
  shell，触发 300s 卡死。
- 修复：在 `create_subprocess_exec` 时显式设置
  `LC_ALL=C.UTF-8` / `LANG=C.UTF-8`，保证错误信息为英文。

### 2.3 fetch_url 缓存"中毒"
- 文件：`src/apitelegramchat/search_engine.py`
- 问题：`set_fetch_cache` 把所有结果（包括 `失败：...` 开头的
  失败字符串）都写入缓存。一次网络抖动失败会让该 URL 在整个
  `FETCH_CACHE_TTL`（默认 1 小时）内对所有后续调用直接返回
  缓存的失败字符串，即使网络已恢复也不会重试。
- 修复：`set_fetch_cache` 现在拒绝缓存 `失败：` 开头的结果，
  失败结果仍返回给本次调用方，但不写入缓存。

### 2.4 `_format_image_generation_result` 用 ✅ 子串判断成功
- 文件：`src/apitelegramchat/tool_executors.py` L659
- 问题：判断逻辑是 `if "✅" in result_str`——任何错误信息
  中只要含 ✅ 字符（例如 LLM 把工具描述里 emoji 复制到失败
  文本中）都会被误判为成功。
- 修复：（审计标记后，配合 `escape_html` 的修复，至少避免
  显示层注入；建议后续把工具执行结果改成结构化
  `{"ok": bool, "data": ...}` 而不是字符串前缀判断。这条
  在本次优化里保留原行为，避免破坏 UI 协议——但加注释提示。）

### 2.5 `escape_text` 本地副本与全局 `escape_html` 行为不一致
- 文件：`src/apitelegramchat/tool_executors.py`
- 问题：`format_tool_result` 内部本地定义了 `escape_text`，
  它与模块顶部 import 的 `escape_html` 行为略有不同（本地版
  会重复转义已合法的实体）。两套实现容易飘移。
- 修复：删除本地 `escape_text` 定义，所有调用点统一改用
  `escape_html`（现在做了智能 ampersand 处理）。

### 2.6 `execute_crypto_price` URL 参数注入
- 文件：`src/apitelegramchat/search_engine.py`
- 问题：`coin_id` 直接来自 LLM 工具参数，未做白名单就拼到
  CoinGecko URL。LLM 传 `coin="btc&ids=ethereum"` 即可
  做查询参数注入。
- 修复：强制白名单 `^[a-z0-9-]+$`；currency 同理要求 3 字母。
  拼接时再 `quote(..., safe='')`。

### 2.7 汇率 `:.4f` 对字符串值抛 ValueError
- 文件：`src/apitelegramchat/search_engine.py` L1579
- 问题：上游 API 偶尔返回字符串形式的汇率（如 "0.1234"），
  直接 `f"{rates[cur]:.4f}"` 会触发 ValueError，被 outer
  except 吞成"汇率查询出错"。
- 修复：显式 `float(...)` 转换 + try/except。

### 2.8 `todo_tool._op_list` 在异常 priority 上抛 KeyError
- 文件：`src/apitelegramchat/todo_tool.py`
- 问题：`PRIORITY_META.get(t.get("priority", "medium"), {})["weight"]`
  在 store 含有未经验证 priority（旧数据 / LLM typo / 手改 JSON）
  时会触发 `KeyError`，让整个 `execute_todo` 直接异常退出。
- 修复：改用 `.get("weight", 2)` 链式兜底。

### 2.9 `memory_tool._op_clear` 空 tag 静默不删除
- 文件：`src/apitelegramchat/memory_tool.py`
- 问题：scope 是 `tag:`（空 tag）时，
  `[m for m in before_list if "" not in m.get("tags", [])]`
  对所有记忆都返回 True（空串不在任何 tag list 里），导致 clear
  不删除任何条目，却返回 `removed=0` 的成功响应——LLM 容易误以为
  已清空。
- 修复：显式拒绝空 tag。

### 2.10 `ai_handlers.py` 硬覆盖 logger 级别
- 文件：`src/apitelegramchat/ai_handlers.py` L64
- 问题：`logger.setLevel(logging.DEBUG)` 强制本模块无视
  `config.LOG_LEVEL`，在生产环境输出大量 debug 噪声。
- 修复：删除该行。

### 2.11 `clean_prompt` 计算了却不传入图像模型
- 文件：`src/apitelegramchat/ai/agentic_loops.py` L760
- 问题：`clean_prompt = _clean_prompt_for_image_model(prompt_text)`
  算出来了，但调 `_request_modelscope_native_image` 时传的还是
  `prompt_text`——`_clean_prompt_for_image_model` 想剥离的
  UI 元数据 / reasoning marker 全部原样泄漏到图像生成模型。
- 修复：改为传 `clean_prompt`。

### 2.12 `tool_summary._parse_tool_arguments` 手工反转义丢反斜杠
- 文件：`src/apitelegramchat/ai/tool_summary.py`
- 问题：兜底分支用 `.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')`
  手工反转义 JSON 字符串，遗漏 `\\u`、`\\r`、`\\\\`、`\\/` 等。
  对 `C:\\path` 输入会丢一个反斜杠。
- 修复：把正则捕获的字符串当作 JSON 字符串字面量
  （`json.loads(f'"{match.group(1)}"')`）解析，让 json 模块
  处理全部转义；解析失败时退回旧的简单反转义。

### 2.13 `_agentic_loop_native_video` 视频时长只认中文"秒"
- 文件：`src/apitelegramchat/ai/agentic_loops.py`
- 问题：`re.search(r'(\d+)\s*秒', prompt)` 只识别中文"秒"，
  英文 "5 seconds" / "5s" 静默落到默认 5s。
- 修复：扩展正则到中英文，删除冗余的本地 `import re`。

### 2.14 `_agentic_loop_native_video` `logger.exception` 丢异常对象
- 文件：`src/apitelegramchat/ai/agentic_loops.py` L1054
- 问题：原写法 `logger.exception("...: %s", str(video_url)[:200])`
  把 `%s` 占位符只填了 URL 字符串，e 本身没被传给 logger——异常
  栈被丢弃，只能看到 "视频下载/上传异常" 而不知道为什么异常。
- 修复：把 `e` 也作为参数传入。

### 2.15 `INTERACTION_TIMEOUT` 24 小时太长
- 文件：`src/apitelegramchat/ask_user_tool.py`
- 问题：一个未回答的 `ask_user` 会把 agent 循环挂起整整一天，
  期间 chat lock / 内存里的 prompt / 模型 cache 都不能释放。
- 修复：默认 10 分钟，可通过 `ASK_USER_TIMEOUT` 环境变量覆盖。

### 2.16 `resolve_callback` 在非数值 chat_id 上抛 500
- 文件：`src/apitelegramchat/ask_user_tool.py`
- 问题：`int(chat_id)` 在 Telegram 偶发传非数值 chat_id
  （channel post）时会抛 ValueError，整个 callback 500。
- 修复：先 try/except 校验类型，无效值返回友好错误。

### 2.17 `context_manager` 没有实际上限
- 文件：`src/apitelegramchat/context_manager.py`
- 问题：`DEFAULT_MAX_MESSAGES = None` / `DEFAULT_MAX_CHARS = None`
  让 `select_request_context` 把整段 history 原样塞进 prompt。长
  会话能轻易达到 100k+ tokens 请求体，触发 413 / 上下文超限 /
  费用失控。
- 修复：默认最近 50 条 / 200k 字符（约 50k tokens），均可通过
  `CONTEXT_MAX_MESSAGES` / `CONTEXT_MAX_CHARS` 覆盖。

### 2.18 `tool_context_compaction._archive_relative_path` 用 round_index 做 digest
- 文件：`src/apitelegramchat/tool_context_compaction.py`
- 问题：归档文件路径的 digest 用 `f"{round_index}:{call_id}"`
  生成。一旦前面的消息被 select_request_context 截尾，同一
  次 tool_call 的 round_index 就变了，重新归档会生成不同 digest，
  让旧 archive 文件永远孤儿化（无法被引用、占据 workspace）。
- 修复：只用 `call_id` 作为 digest 输入。

## 3. 健壮性 / 资源泄漏

### 3.1 `get_cached_image_data` 一次性失败永久标记 R2 attempted
- 文件：`src/apitelegramchat/ai/attachment_content.py`
- 问题：原代码在 *任何* 非 200 响应、任何 `Exception` 上都调用
  `state.mark_r2_attempted(file_id)`。这个标记是永久的，会让该
  file_id 在所有后续 turn 中直接短路返回 None——意味着一次
  Telegram 429 / 一次 DNS 抖动会让该图片永久从对话历史中
  消失（用户报告 "Gemini 只能看到最新图片"的根因之一）。
- 修复：仅 hard status（404/403/410）和 file_path 缺失才 mark；
  429/5xx/网络异常/未知异常都跳过 mark，让下次 turn 重试。

### 3.2 `attachment_content._append_history_async` 把附件元数据发给模型 API
- 文件：`src/apitelegramchat/ai/attachment_content.py`
- 问题：把 `file_id` / `file_ids` / `file_name` / `mime_type` /
  `type` / `attachments` 等内部字段拷到出站消息体——OpenAI /
  Anthropic / Gemini 等网关对未声明字段会直接返回 400。
- 修复：只拷 OpenAI 兼容协议认可的字段
  （`role` / `content` / `tool_calls` / `tool_call_id` /
  `name` / `reasoning_content`）。

### 3.3 `s3_utils.upload_bytes_to_r2` 重试循环是死代码
- 文件：`src/apitelegramchat/s3_utils.py` L113
- 问题：`max_attempts = 1` 让 `for attempt in range(1)` 只跑一次，
  下面的 `if attempt < max_attempts - 1` 永远进不去。
- 修复：改成 `max_attempts = 3` + 指数退避。

### 3.4 PIL Image 未 close + `img.convert("RGB")` 也未 close
- 文件：`src/apitelegramchat/ai/attachment_content.py` L423
  + `src/apitelegramchat/ai/error_formatting.py` L375
- 问题：在异常路径上 PIL Image 不会 close，反复处理大量图片
  会泄露文件描述符。`img.convert("RGB")` 返回新对象，也未被 close。
- 修复：都用 `with Image.open(...) as img:` + `with img.convert(...) as ...`。

### 3.5 `media_generation` 大 base64 / 远端图片无大小限制
- 文件：`src/apitelegramchat/ai/media_generation.py`
- 问题：`base64.b64decode(b64_json)` 直接对上游返回的 base64
  串做解码，恶意/失控的上游可返回多 GB 字符串触发 OOM；
  `await resp.read()` 同样把整个"图片"读进内存。
- 修复：base64 串超过 25MB 直接拒绝解码；远端图片同样用
  `resp.content.read(max+1)` + 大小检查。

### 3.6 `_agentic_loop_native_video` 视频下载无大小上限
- 文件：`src/apitelegramchat/ai/agentic_loops.py`
- 问题：`await dl_resp.read()` 把整个视频字节读进内存，
  无上限。一个失控上游返回 1GB+ 视频会把进程拖垮。
- 修复：限制 200MB，超限拒绝并回退到原始 URL。

### 3.7 `_safe_json_parse` 完全静默吞异常
- 文件：`src/apitelegramchat/ai/media_generation.py`
- 问题：所有 JSON parse 失败都 `except Exception: return None`，
  没有任何诊断痕迹。200 响应体不是合法 JSON 时排查非常困难。
- 修复：`JSONDecodeError` 在 debug 级别输出诊断信息。

### 3.8 `_post_or_get_json` 同时传 `data=` 和 `json=`
- 文件：`src/apitelegramchat/ai/media_generation.py`
- 问题：aiohttp 在两者都非 None 时行为未定义。原代码总是
  一起传，实际只有 `json=` 生效，但代码意图不清。
- 修复：显式二选一分支，提取 `_finalize_response` 复用。

### 3.9 `_rollover_history` 无上限
- 文件：`src/apitelegramchat/ai/rich_message_builder.py`
- 问题：每次 rollover 都 append 一条，没有上限，长会话会让
  该 list 无限增长。
- 修复：限制最近 50 条，老的 `del` 掉。

### 3.10 429 检测靠 `"429" in str(e)`
- 文件：`src/apitelegramchat/ai/rich_message_builder.py` L1182
- 问题：子串匹配，任何巧合含 "429" 字符串的异常（如 request_id）
  都会被误判为 rate limit。
- 修复：优先看异常的 `status_code` 属性，再回退到子串。

### 3.11 `_RICH_BLOCK_TAGS` / `_RICH_VOID_TAGS` 是可变 set
- 文件：`src/apitelegramchat/ai/rich_message_builder.py`
- 问题：模块级 mutable set，任何误操作会污染所有 builder。
- 修复：改为 `frozenset`。

### 3.12 `_draft_*` 全局 dict 永不清理
- 文件：`src/apitelegramchat/utils.py`
- 问题：`_last_sent_draft_cache` / `_draft_send_locks` /
  `_draft_failure_counts` / `_draft_last_send_time` 这 4 个
  module-level dict 在草稿生命周期结束后没有清理路径，长时间
  运行会让每个草稿的元数据永久驻留（内存泄漏）。
- 修复：`mark_draft_dead` 之后主动扫描并清理匹配的缓存项；
  失败计数加锁。

### 3.13 `retry_async` 不用 `functools.wraps`
- 文件：`src/apitelegramchat/utils.py`
- 问题：被装饰的协程失去 `__name__` / `__doc__`，影响
  introspection / help() / 日志可读性。
- 修复：加 `@functools.wraps`。

### 3.14 `setup_logging` 在 import 时无条件覆盖 root logger
- 文件：`src/apitelegramchat/utils.py`
- 问题：任何 import 都会触发 root logger 重置 + 安装
  console/file handler，让 MCP server / unit tests 等宿主
  失去对自己 logging 配置的控制；同时日志文件路径硬编码
  `/tmp/app.log`，只读 FS 上会失败。
- 修复：仅在 root logger 还没有 handler 或显式通过
  `APITELEGRAMCHAT_REQUIRE_LOGGING=1` 要求时才初始化；
  日志路径可通过 `LOG_FILE` 覆盖。

### 3.15 `_rich_message_plain_text_fallback` 重新注入风险
- 文件：`src/apitelegramchat/utils.py`
- 问题：`html.unescape(strip_html_tags(html))` 后直接拼回
  `<p>...</p>`，如果原 HTML 里含 `&lt;script&gt;`，
  unescape 出来的 `<script>` 会被当作 HTML 渲染。
- 修复：unescape 之后**再次转义**，避免重新注入。

### 3.16 `send_chat_action` 没设超时
- 文件：`src/apitelegramchat/utils.py`
- 问题：Telegram API stall 时会无限期挂起协程，间接阻塞
  整个 chat 的活跃任务。
- 修复：设 `total=5, connect=3`。

### 3.17 `file_handlers.get_file_path` 没设超时
- 文件：`src/apitelegramchat/file_handlers.py`
- 问题：同上。
- 修复：设 `total=15, connect=5`；下载用 `total=60, connect=10`；
  异常路径上脱敏 token（避免 `str(e)` 含 token 进 ERROR 日志）。

### 3.18 `todo_tool` / `memory_tool` 共用 tmp 文件名导致并发丢数据
- 文件：`src/apitelegramchat/todo_tool.py` / `memory_tool.py`
- 问题：原子写入用的 tmp 文件名固定为
  `<name>.json.tmp`，两个并发 writer 共用同一个 tmp 路径，
  后写的覆盖先写的。
- 修复：tmp 名加 PID + 8 字节随机后缀，确保唯一。

### 3.19 `subagent_tool` 把 traceback 直接返回给 LLM
- 文件：`src/apitelegramchat/subagent_tool.py` L474
- 问题：`"traceback": traceback.format_exc()[:500]` 直接放进
  tool_result JSON 返回给 LLM。traceback 里可能含文件路径、
  env var 名、甚至 URL 形态的 secret（如 API key 拼在 endpoint URL 里）。
- 修复：改成只返回 `error_id`（uuid 前 12 字节），完整 traceback
  留在后端 logger 里供运维查。

### 3.20 `DEFAULT_ALLOWED_TOOLS` 用 set 让 prompt cache 失效
- 文件：`src/apitelegramchat/subagent_tool.py`
- 问题：set 迭代顺序在 CPython 上由 hash 决定，不同进程可能
  顺序不同，传给 LLM 的 tool schema 顺序也会变，导致 prompt
  cache 命中率掉到 0。
- 修复：改用 `sorted(list)`，保证稳定顺序。

## 4. 脏代码 / 可维护性

- `search_engine.py`：删除未使用的 `import math` / `import shutil`
  / `clear_editor_file_state`；修正 `MAX_EDITOR_FILE_SIZE` 注释
  （原写"# 1MB"，值实际是 5MB）。
- `search_engine.py`：把 `asyncio.get_event_loop().time()` 换成
  `time.monotonic()`——前者在 Python 3.10+ 没有运行 loop 时
  会发 DeprecationWarning，且与 `time.monotonic` 不是同一个时钟。
- `tool_executors.py`：删除 `format_tool_result` 内的本地
  `escape_text` 定义，全部用 `escape_html`。
- `tool_executors.py`：把 `execute_present_files` 的
  `aiohttp.ClientSession` 提到循环外层，避免每个文件都做一次
  TLS 握手。
- `error_formatting.py`：`_CONTENT_SAFETY_KEYWORDS` 改为
  `frozenset` 并预计算 lower-case 版本，省去每次调用的 `.lower()`
  开销；`from io import BytesIO` 提到模块顶部。
- `verify_security.py`：补 `import logging`、`import tempfile`，
  与项目其它模块保持一致的 logger 命名。
- `mcp_client.py`：logger 名从硬编码字符串改成 `__name__`。
- `attachment_content.py`：`_get_cached_audio_data` /
  `_get_cached_document_data` / `_build_native_document_part` /
  `_resolve_multimodal_content` 的 `chat_id` 类型注解从 `int`
  改成 `int | None`，与实际调用一致（多处传 `None`）。
- `tool_context_compaction.py`：archive payload 加 1MB 上限，
  避免大 `fetch_url` 结果反复归档撑爆 workspace。
- `tool_summary.py`：补 `json.loads` 解析失败时记录 debug 痕迹
  （此前完全静默 `except Exception: pass`）。

## 5. 已知遗留项（建议后续单独处理）

以下问题影响较大但本次未改，因为修复方式会破坏现有协议或
需要重构：

- `tool_executors.format_tool_result` 是 600 行 if/elif 级联，
  按工具名分派。建议改成 `TOOL_FORMATTERS: dict[str, ToolFormatter]`
  注册表，每个工具一个 formatter 类，避免新增工具时改到全文件。
- `tool_executors.py` ↔ `search_engine.py` ↔ `subagent_tool.py`
  存在循环依赖（`subagent_tool` 内部 import `tool_executors`）。
  建议把 `dispatch_tool_call` / `tool_semaphore` /
  `_TOOL_TIMEOUT_MARKER` 拆到独立的 `tool_dispatch.py`。
- `_request_agnes_video` 与 `_request_openrouter_video` 是
  两个 180 行几乎重复的函数，建议提取
  `_submit_and_poll_video(submit_fn, poll_fn, status_extractor)`。
- `RichMessageBuilder` 是 god-object，`agentic_loops` /
  `tool_call_loop` 大量直接访问其 `_tool_groups` 私有属性。
  建议暴露 `is_current_group_finished()` /
  `finish_current_group()` 等公开 API。
- `sandbox._preexec_sandbox` 使用 `preexec_fn=`，在多线程程序
  上有 Python 文档明示的死锁风险。建议改用 `unshare` /
  `setpriv` 或父进程预置 `setrlimit`。
- `sandbox.watchdog` 每秒 walk 整个 `/proc` 找子进程，O(N×M)。
  建议用 cgroup 跟踪 descendant。
- 工具结果统一信封：不同工具的失败返回有的是 `"❌ ..."`，
  有的是 `"失败：..."`，有的是 `{"status":"error"}`。建议
  统一为 `{"ok": bool, "error": {"code","message"}}` 并让
  formatter 信任结构化字段而非字符串前缀。

---

# 补丁 2 — RICH_MESSAGE_PHOTO_URL_INVALID 修复（2026-08-24）

## 问题
用户上传图片后 AI 回复发送失败，Telegram 返回 400
`Bad Request: RICH_MESSAGE_PHOTO_URL_INVALID`，**整条**回复
（包括图片描述正文）都丢失。

## 根因
1. `app.py:1206` 把图片附件的 `file_name` 设为
   `photo_{file_id[:8]}.jpg`（如 `photo_AgACAgUA.jpg`），
   并写入 user_message 的 content 文本
   `📎 用户上传了图片「photo_AgACAgUA.jpg」`。
2. 走 vision 路径时图片字节通过 `image_url` 发给模型，
   但 user_text 仍带着这段含 `.jpg` 后缀的字符串。
3. 模型在回复时把它误当成 URL，输出
   `<figure><img src="photo_AgACAgUA.jpg"/></figure>`。
4. `photo_AgACAgUA.jpg` 不是合法 http(s) URL，Telegram
   拒绝整条消息。

## 三重防御修复

### 修复 A — system prompt 加约束
- 文件：`src/apitelegramchat/ai_handlers.py`
- 在「附件处理」段后新增「媒体 URL 严格规则」段，明确：
  - 附件占位符中的 `「...」` 文本只是文件名，不是 URL
  - `file_id：...` 后的字符串是 Telegram 内部 ID，不是 URL
  - 禁止把这两种字符串写入 `src` / `href`
  - 列出唯一允许写入 URL 的 4 种来源
  - 用户已上传附件无需在回复中回显

### 修复 B — 发送前兜底清理（最终防线）
- 文件：`src/apitelegramchat/utils.py`
- 新增 `_strip_invalid_media_urls(html)`：扫描
  `<img>/<video>/<audio>` 标签的 `src`，若不以
  `http(s)://` 开头则剥离整个标签；并清理剥离后留下的
  空 `<figure>`。
- `_rich_message_html_payload()` 改为先调用此函数清理，
  再交给 Telegram。剥离发生时打 WARNING 日志。
- 即使 AI 偶尔违反 system prompt 输出伪 URL，消息也
  能正常送达（只是少了那张图），避免整条回复丢失。

### 修复 C — fallback text 显式警告
- 文件：`src/apitelegramchat/ai/attachment_content.py`
- `_build_attachment_fallback_text()` 在「链接」字段为空
  （R2 未配置）时追加一条提示，明确告诉 LLM file_name /
  file_id 不是合法 URL，禁止写入 `<img src>`。
- 这一路径只在模型不支持原生 vision 时命中，作为
  A + B 之外的补充防御。

## 验证
- `scripts/test_strip_media.py` 单元测试：4 个场景全部通过
  （日志场景 / 合法 URL 保留 / 非法 video 整块删除 /
  全伪 URL 返回空）
- `scripts/test_e2e_log_scenario.py` 端到端：模拟日志中的
  完整 HTML，验证清理后伪 URL 被剥离、正文全部保留，
  不再触发 `RICH_MESSAGE_PHOTO_URL_INVALID`。

---

# 补丁 3 — 附件多模态路径开销优化（2026-08-24）

## 背景
用户追问："Agnes 用 URL、Gemini 用 base64，切换模型不失效的机制下
有没有不必要的开销？"审计后发现确实存在 3 个浪费点，本次彻底重构。

## 浪费点 1：Agnes 首访路径重复 HEAD 检查
- 位置：`_resolve_r2_public_url_for_vision` → `get_cached_image_data`
- 问题：外层已 `file_exists_in_r2(r2_key)=False`，紧接着调
  `get_cached_image_data` 内部又会做一次同样的 HEAD 检查。
  每次 Agnes 首访多一次 R2 HEAD 请求。
- 修复：`_resolve_r2_public_url_for_vision` 路径 3（R2 未有）
  改为直接调 `_fetch_from_telegram_and_cache`，绕过
  `get_cached_image_data` 的 HEAD 检查。

## 浪费点 2：Agnes 首访路径同一张图被 put_object 两次
- 位置：`get_cached_image_data` 末尾的 `_track_task(_upload_and_mark())`
- 问题：旧版 `get_cached_image_data` 在 Telegram getFile 成功后
  fire-and-forget 后台上传 R2。Agnes 路径紧接着又同步上传 R2。
  两个 PUT 写同一个 key，第二次纯浪费（带宽 + R2 API 配额）。
- 修复：把"后台上传"职责从 `get_cached_image_data` 剥离：
  * `get_cached_image_data` 现在职责单一，只取字节，不触发上传
  * Gemini 路径在 `process_one` 内**显式**调
    `_track_task(_upload_and_mark(...))` 触发预防性后台上传
  * Agnes 路径不经过 `process_one` 的 base64 分支（vision_prefer_url=True
    时早已 return），不会触发后台上传，由 `_resolve_r2_public_url_for_vision`
    自己负责唯一的同步上传

## 浪费点 3：R2 未配置时 Agnes 仍写本地 file:// 然后降级
- 位置：`_resolve_r2_public_url_for_vision`
- 问题：R2 未配置时，`upload_bytes_to_r2` 走本地兜底返回
  `file://` URL，本函数检测到 `file://` 又返回空串让调用方降级 base64。
  这次本地磁盘写入是纯浪费。
- 修复：`_resolve_r2_public_url_for_vision` 路径 1 早退检测
  `is_r2_configured()`，未配置直接返回空串，避免任何下游调用。

## 重构 — 函数职责单一化

### `get_cached_image_data(chat_id, file_id)`
- 旧版职责：取字节 + 后台上传 R2（耦合）
- 新版职责：只取字节
- 解析顺序：内存 TTLCache → 永久失败标记 → R2 download →
  Telegram getFile（拉到后只填内存缓存，不触发上传）

### `_fetch_from_telegram_and_cache(file_id)` （新增）
- 从 `get_cached_image_data` 拆出，作为两条路径共用的 Telegram getFile
  拉字节函数。临时失败不永久标记，hard failure (404/403/410) 才标记。

### `_resolve_r2_public_url_for_vision(file_id)`
- 旧版：3 个失败模式统一返回空串，但中间链路有重复 HEAD + 重复 PUT
- 新版：3 条清晰路径按开销从低到高
  1. R2 未配置 → 立即返回空串（0 下游调用）
  2. R2 已有 → 直接拿公开 URL（0 上传、0 拉字节）
  3. R2 未有 → 同步拉字节 + 同步上传（1 次拉、1 次传，唯一）

### `s3_utils._use_remote_r2` → `is_r2_configured`
- 公开化（去下划线前缀），让附件层据此早退。
- 同步修改 `s3_utils.py` 内部 7 处 `if not _use_remote_r2():` 引用。

## 验证
- `scripts/test_attachment_refactor.py` 覆盖 5 个场景全部通过：
  A. Agnes + R2 已有 key → 0 拉字节、0 上传
  B. Agnes + R2 没该 key → 1 拉字节、1 上传（旧版 2 次 PUT）
  C. Agnes + R2 未配置 → 0 下游调用（旧版会写本地再降级）
  D. Gemini 路径 _track_task + _upload_and_mark 契约正常
  E. Agnes 首访不触发 _track_task（旧版会，导致重复 PUT）
- 之前的两个回归测试仍然通过：
  - `test_strip_media.py`：4 场景全过
  - `test_e2e_log_scenario.py`：日志场景端到端 OK

## 收益
| 场景 | 旧版开销 | 新版开销 | 节省 |
|---|---|---|---|
| Agnes 首访 + R2 已有 | 1 HEAD + 0 PUT | 1 HEAD + 0 PUT | — |
| Agnes 首访 + R2 没该 key | 2 HEAD + 2 PUT | 1 HEAD + 1 PUT | 1 HEAD + 1 PUT |
| Agnes 首访 + R2 未配置 | 1 HEAD + 1 本地写 | 0 下游调用 | 1 HEAD + 1 本地写 |
| Gemini 首访 | 1 HEAD + 1 后台 PUT | 1 HEAD + 1 后台 PUT | — |

切换模型时的零延迟收益**保留**：Gemini 那轮的后台上传让 Agnes 那轮
直接命中 R2 已有路径。


---

# 视频输入模态（video understanding）— 变更清单

本次变更为 Telegram AI 助手加入**视频输入**能力，与既有的图片输入
（vision）实现完全对称：用户可直接发送视频 / 圆形视频 / 视频相册给
bot，支持视频理解的模型（`stealth/ox-alpha`、Gemini 系列等）会收到
OpenAI 兼容协议的 `video_url` content part；不支持的模型收到保留
元数据的文本占位。切换模型时历史消息按新模型能力**重新解析**，
信息不丢失。

## 1. API 格式调研结论（实现依据）

- **OpenRouter 官方文档**（/docs/guides/overview/multimodal/videos）：
  视频输入通过 `/api/v1/chat/completions` 的 `video_url` content type
  传递，`url` 可以是公开 URL 或 base64 data URL；支持容器为
  video/mp4、video/mpeg、video/mov、video/webm；大文件建议用 URL。
- **OpenRouter 模型元数据实测**：`stealth/ox-alpha` 与
  `google/gemini-3.7-flash` 的 `architecture.input_modalities` 均含
  `video`；`anthropic/claude-sonnet-5` 不含（[text, image, file]）。
- **事实标准**：vLLM / LiteLLM / Envoy AI Gateway / Venice / Grok
  等兼容生态均采用 `{"type": "video_url", "video_url": {"url": ...}}`。
- **设计取舍**：视频统一走 **R2 公开 URL（自定义域 / r2.dev / 预签名）**，
  不做 base64 内联 —— Telegram bot 视频上限 20MB，base64 膨胀 ~33%
  极易触发网关请求体上限。R2 不可用时降级为文本占位（信息不丢）。

## 2. `config.py`：新增 `video` 输入模态参数

- `ModelConfig` 新增 `video: Optional[bool]` 字段（视频输入理解能力）。
  与 `native_video`（视频**生成**输出）语义区分、互不影响。
- `_PROVIDER_DEFAULTS` 七个厂商均补 `"video": False` 默认值；
  `make_model_config` / `discover_model` 透传该字段。
- 标记支持视频输入的模型（依据 OpenRouter input_modalities 实测）：
  - `stealth/ox-alpha` → `video=True`
  - `gemini-3.7-flash`、`gemini-3.5-flash-lite` → `video=True`

## 3. `ai/attachment_content.py`：视频解析管线（对称图片实现）

新增函数（与图片路径一一对应）：

| 视频函数 | 对称的图片函数 | 职责 |
|---|---|---|
| `_video_cache` (TTLCache 50) | `_image_cache` | 内存缓存（条数少，防大文件占内存） |
| `get_cached_video_data` | `get_cached_image_data` | 内存 → 永久失败标记 → R2 → Telegram |
| `_fetch_video_from_telegram_and_cache` | `_fetch_from_telegram_and_cache` | 拉字节（超时 120s，仅 hard failure 永久标记） |
| `_upload_video_and_mark` | `_upload_and_mark` | 后台上传（用真实 mime，仅失败时标记） |
| `_resolve_r2_public_url_for_video` | `_resolve_r2_public_url_for_vision` | R2 有→URL；无→拉+同步上传→URL |
| `_ensure_video_persisted` | （图片无对应） | **视频专属**：降级路径也后台持久化 |
| `_normalize_video_mime_type` | （无） | quicktime→mov，未知→mp4 |

`_resolve_multimodal_content` 新增两个分支：

- **单视频**（`type == "video"`，`file_id` 单数）：`model_info.video`
  为真且 URL 可解析 → `[{"type": "video_url", ...}, {"type": "text"}]`；
  否则文本降级。降级时（无论模型是否支持视频）都 fire-and-forget 触发
  `_ensure_video_persisted` —— Telegram getFile 直链约 1 小时过期，
  必须先把字节落到 R2，之后切换到支持视频的模型才能恢复原生解析。
- **视频组**（`type == "video_group"`，`file_ids` 数组，对称
  `photo_group`）：并发解析多个 `video_url` part；部分失败时成功的
  照常发送、失败的触发后台持久化。

## 4. `app.py`：消息入口与视频相册聚合

- **新增直接上传视频入口**（此前完全没有：视频消息会被静默忽略）：
  - 单视频 / 圆形视频（`video` / `video_note`）→ `_handle_video_message`
    （对称 `_handle_audio_message`，upload_video chat action）。
  - **视频相册**：新增 `_video_group_tasks` / `_process_video_group_once` /
    `_schedule_video_group`（对称图片组聚合），等待 `MEDIA_GROUP_TIMEOUT`
    后合并为一条 `type="video_group"` 消息触发一轮 AI。
  - **混合相册（photo+video 同 media_group_id）**：图片组与视频组改用
    复合 key（`{mg}:photo` / `{mg}:video`）分流存储；互斥的 interrupt
    条件保证两组聚合任务不会互相取消，各自独立完成。
- **回复视频路径增强**：`_get_reply_media` 与 reply 分支补传
  `mime_type`（并支持回复 `video_note`）。

## 5. 切换模型不丢信息（核心诉求的机制保障）

沿用图片的"**历史存元数据、每轮按当前模型重解析**"架构：

1. 视频消息以 `{content, file_id, file_name, mime_type, type,
   attachments}` 形式存入 `conversation_history`（出站消息体只含
   OpenAI 协议字段，元数据不泄露）。
2. 每轮请求 `_append_history_async` → `_resolve_multimodal_content`
   按当前模型能力重新构造 content：支持视频 → `video_url` 数组；
   不支持 → 文本占位（含 R2 链接 / file_id / mime 元数据）。
3. 字节持久化到 R2 保证跨轮可恢复：
   - 支持视频的模型首访 → 同步上传（对称图片 Agnes 路径）；
   - 不支持视频的模型首访 → 后台上传（`_ensure_video_persisted`，
     图片没有这一步 —— 视频 Telegram 直链过期更紧迫）。
   - TTLCache 过期后从 R2 回填；预签名 URL（1h）每轮重解析时重新签发。

场景矩阵：
- 发视频时模型支持 → 原生 video_url；之后切到不支持 → 文本占位
  （链接仍在）；再切回支持的模型 → 恢复原生 video_url。
- 发视频时模型不支持 → 文本占位 + 后台已持久化；切到支持的模型
  → 直接恢复原生 video_url（R2 已有对象，零上传延迟）。

## 6. 验证

- `scripts/test_video_modality.py`：6 项断言通过 —— 配置标志、mime
  归一化、单视频三种解析路径、切换模型重解析端到端。
- `scripts/test_video_group.py`：3 项断言通过 —— 视频组原生解析 /
  文本降级 + 双持久化 / 部分失败补救。
- 出站消息体验证：`video_url` part 结构与 OpenRouter 官方文档一致；
  附件元数据（file_id 等）不泄露进出站消息体；JSON 可序列化。
- 全部改动文件 `py_compile` 语法通过；AST 结构分析确认 webhook
  分支顺序：图片组 → 视频组 → 文档组 → 单图 → 单文档 → 音频 →
  单视频 → 文本。

## 7. 部署提示

- **必须配置 R2**（`R2_ENDPOINT` / `R2_ACCESS_KEY` / `R2_SECRET_KEY` /
  `R2_BUCKET_NAME`，公开域名 `R2_PUBLIC_URL` 或预签名）才能走原生
  视频输入；未配置时视频自动降级为文本占位，消息不会失败。
- Telegram bot API 下载上限 20MB，超过上限的视频无法被 bot 下载
  （Telegram 平台限制，与本项目无关）。

---

# fetch_url 富媒体提取改造 — 变更清单

## 背景

`fetch_url` 此前用 trafilatura 的 `txt` 输出（纯文本，所有格式丢失），
返回形如 `✅ [成功] 🏷️ 标题\n🔗 URL\n📄 内容：…` 的纯文本结果。该格式
既不是系统提示词规定的 Telegram HTML 子集，也提取不到页面上的图片、
内嵌视频、iframe 播放器（YouTube/Bilibili 等）与音频，模型只能把链接
当纯文本转述。

## 改动总览

新增 `src/apitelegramchat/fetch_rich_content.py`（提取引擎，约 1000 行），
重写 `execute_fetch_url` 成功路径，并同步更新全部下游消费方。

### 新文件：`fetch_rich_content.py`

1. **trafilatura XML → Telegram Rich HTML 转换器**
   - `output_format='xml'`（`include_links/images/formatting/tables` 全开）
     保留全部结构；`<hi rend="#b">` → `<b>`、`<ref target>` → `<a href>`、
     `<list>` → `<ul>/<ol>`、`<table>` → `<table bordered striped>`（含
     `colspan/rowspan` 与表头加粗）、`<quote>` → `<blockquote>`、块级
     `<code>` → `<pre><code>`。
   - **中文页面退化检测**：中文等无空格语言会击穿 trafilatura 基于词数
     的启发式，走 justext 回退把多段合并成单段并丢失全部行内格式。
     `extract_body_blocks()` 在"结果块数 ≤1 而原始 HTML 明明有 ≥3 个块
     级元素"时用 `favor_precision=True` 重试，取结构更完整的一份。
   - **容器行内合并**：维基百科 `favor_precision` 输出会把 `<ref>` 直接
     挂在 `<main>` 下、尾巴文本散落成 `：`/`、` 碎片；`_render_container`
     将容器层连续行内子元素与尾巴合并成完整段落，并过滤纯标点碎片段落
     与空表格。
   - **媒体提升**：段落/列表项内的 `<graphic>` 一律提升为兄弟块级
     `<img/>`/`<figure>`，符合"媒体必须是独立块级元素、严禁嵌入行内
     容器"的 Rich Message 约束。

2. **嵌入媒体提取 `extract_embedded_media()`**（trafilatura 全部丢失的部分）
   - `<video>/<source>`（含 poster 封面）→ `<video src>`；
   - `<iframe>/<embed>` 播放器 → 规范化观看链接列表（YouTube/YouTube-
     nocookie/Vimeo/Dailymotion/Bilibili（bvid/aid）/优酷，其余站点按
     域名标注 X/Facebook/Instagram/TikTok/Spotify/网易云等）；
   - `<audio>/<source>` → `<audio src>`；
   - `<img>` 懒加载属性（data-src/data-original/data-actualsrc 等 9 种）
     与 `srcset`（取最大尺寸候选）；
   - Open Graph（og:video/og:audio/og:image/og:title/og:description）；
   - JSON-LD `VideoObject/AudioObject/ImageObject`（递归 @graph/hasPart）；
   - 安全与降噪：URL 仅允许 http/https（拒绝 javascript:/data:/blob:
     等），基于 base_url 补全相对路径并去 fragment；过滤装饰图（sprite/
     spacer/icon/logo/avatar/1x1 等文件名特征）；全类型去重；数量上限
     （图 8 / 视频 4 / 播放器 5 / 音频 2）。

3. **结果组装 `build_fetch_rich_result()`**
   - 结构：`<h3>` 标题（og:title 优先）→ `🔗 来源链接` → 正文块 →
     `🎬 视频` / `📺 内嵌播放器` / `🎵 音频` / `🖼️ 图片`（≥2 张用
     `<tg-slideshow>`，内部只放裸 `<img>`）媒体区；
   - **整块截断**：14000 字符总预算（低于 `MAX_TOOL_RESPONSE_LEN=16000`，
     朴素切片永远不会作用在 HTML 上），只会在完整块边界截断并追加
     "（正文过长，已截断）"，绝不产生未闭合标签；
   - 正文里已内联出现的媒体自动从媒体区去重；首个正文标题与页面标题
     重复时自动去重。

### `search_engine.py`：`execute_fetch_url` 重写

- 成功路径改为 `_build_rich_fetch_payload()`（CPU 密集，经
  `asyncio.to_thread` 调度）：标题 + 正文块 + 嵌入媒体 → 最终 HTML；
- `curl_cffi` 失败时不再直接走 trafilatura 纯文本提取，而是用
  trafilatura 下载器拿到原始 HTML 后仍走富 HTML 提取
  （`_download_html_with_trafilatura`）；
- SSRF 校验、缓存策略（失败结果不缓存）、JS/Meta-Refresh 重定向跟随、
  重试循环等行为全部保持不变；
- `fetch_url` 工具描述更新：明确告知模型结果为 Telegram Rich HTML、
  其中的媒体/链接 URL 可直接复用。

### 下游消费方同步更新

- `tool_executors.py / format_tool_result`：fetch_url 详情不再降级为
  "标题 + 域名链接"，而是原样透传富 HTML——用户在 Telegram 折叠面板里
  可直接预览网页正文、图片、视频与播放器链接；失败判断改为前缀匹配
  （避免把谈论"失败"的新闻正文误判为抓取失败）；标题解析优先 `<h3>`
  并保留旧 `🏷️` 格式兼容。
- `ai/tool_summary.py`：`_generate_tool_summary_done` 标题解析同步支持
  `<h3>`（旧格式兼容）；`_tool_result_is_failure` 补充中文"失败："前缀。
- `ai_handlers.py / build_system_prompt`：媒体 URL 白名单条款明确加入
  `fetch_url`，说明其返回的 `<img>/<video>/<a>` 标签中的 URL 均为
  合法来源，可直接复用。

## 验证

- 新增 `tests/test_fetch_rich_content.py`：47 项离线单测全绿，覆盖 URL
  安全过滤、播放器规范化、OG/JSON-LD/懒加载提取、装饰图过滤、去重与
  上限、XML→HTML 全元素转换、容器行内合并、碎片段落/空表格过滤、
  整块截断闭合性、execute_fetch_url 集成（成功/失败/SSRF/缓存）。
- `tests/test_consumers.py`：下游链路验证（format_tool_result /
  摘要 / 失败判定 / 旧格式兼容）全绿。
- 真实网页抽查（BBC / 中文维基百科 / GitHub）均产出合法 Telegram
  HTML：标题、来源链接、加粗/斜体、超链接、图片（含懒加载）、
  slideshow、表格、代码块齐全；中文维基百科触发 favor_precision
  回退并正确合并信息框碎片。

---

# fetch_url 面向模型重构（v2）— 展示与模型上下文分离 + 文档顺序忠实

## 背景

第一版富媒体改造把两类受众混在一起了：(a) Telegram 工具折叠面板的 UI 展示
直接透传了完整富 HTML（消息过长且与模型回复重复）；(b) 媒体被集中堆到结果
末尾的"🎬 视频 / 📺 内嵌播放器 / 🎵 音频 / 🖼️ 图片"聚合区，违背了页面
原始结构。本次按正确需求重构：

- **工具返回的 UI 展示保持原样不变**（标题 + 域名链接）；
- **模型看到的是忠实于原网页文档顺序的 Telegram HTML**：链接、图片、视频、
  播放器在它们的原始位置；轮播图识别为 `<tg-slideshow>`；绝不集中到一处。

## 改动明细

### fetch_rich_content.py（重写组装层）

- **删除聚合媒体区**：`build_fetch_rich_result`（含 PageMedia/MediaAsset/
  extract_embedded_media/OG/JSON-LD 元数据媒体注入）整体移除。
- **新增 `build_model_facing_html`**：
  - `_collect_dom_media`：DOM 单次文档序遍历，收集带 `order_idx`/`path`/
    `carousel` 位置的媒体（video/source、audio、iframe/embed 播放器、懒加载
    图片 data-*/srcset、figure figcaption 图注、隐藏元素过滤、装饰图过滤、
    去重与数量上限）；
  - `_anchor_entries`：把每个正文块锚定到 DOM 元素——文本前向贪心匹配
    （支持相等/前缀/包含，覆盖中文短标题与 trafilatura 合并段落场景），
    纯图片块用 src 反查 DOM 位置（精确原位锚定）；
  - `_sort_entries_by_anchor`：按锚点稳定排序正文块，修正 trafilatura 把
    `<graphic>` 挪到 XML 末尾导致的顺序偏移；
  - `_interleave`：dropped 媒体（trafilatura 丢弃的视频/音频/播放器/懒图）
    按文档位置插回正文流；
  - 轮播：`_find_carousel_ancestor`（取 body 以下最外层 swiper/carousel/
    gallery/slick/... 特征容器）+ `_group_carousel_runs`（连续同轮播图片块
    → `<tg-slideshow>`，保持原位置）+ 全懒加载轮播在原位置插入完整
    slideshow；无轮播特征的相邻图片保持独立 `<img>`（忠实原结构）；
  - **样板区域排除**：nav/footer/aside 祖先内的媒体不插入（替代路径前缀
    方案——公共 XPath 前缀在"正文只提取到单个小节"的新闻首页会过窄）；
  - OG/JSON-LD 元数据媒体不再进入结果（不在文档流中，无法原位呈现；标题
    仍用 og:title）。

### search_engine.py

- `_build_rich_fetch_payload` 改用 `build_model_facing_html`；注释明确
  "返回值只进入模型上下文，UI 展示由 format_tool_result 单独负责"。
- `fetch_url` 工具描述更新：说明结果镜像原页面结构与顺序、媒体在原始
  位置、轮播为 slideshow、HTML 片段可直接复用。

### tool_executors.py（UI 展示还原）

- `format_tool_result` 的 fetch_url 分支恢复历史展示样式：
  `details_html = f"{title} <a href=\"{url}\">{domain}</a>"`，不再透传富
  HTML；标题解析优先 `<h3>`（新格式）并保留 `🏷️`（旧格式）兼容；失败
  前缀判定与"超时"字样检查保留（正文谈论"失败"不误判）。

### ai_handlers.py

- 系统提示词 URL 白名单条款更新：fetch_url 结果为"按原页面文档顺序组织的
  Telegram Rich Message HTML"，媒体与链接可直接复用且保持原始位置。

## 验证

- `tests/test_fetch_rich_content.py`：57 项离线单测全绿，新增覆盖——
  DOM 媒体位置信息、轮播容器共享判定（swiper 外层容器而非 slide 项）、
  媒体原位插入顺序（第一段 < 视频 < 第二段）、embed 原位、懒图原位、
  已保留图片块按 DOM 重锚定、轮 slideshow 原位、全懒轮播 slideshow、
  非轮播相邻图不分组、样板区域排除、无聚合区断言、纯媒体页、空页面 None。
- `tests/test_consumers.py`：UI 展示断言改为历史样式（标题 + 域名链接），
  富 HTML 不出现在展示中；其余（失败识别/摘要/旧格式兼容）保持全绿。
- 真实网页抽查（BBC / 中文维基百科 / GitHub）：媒体全部在原位、图文顺序
  忠实、页脚/导航媒体被排除、无聚合区。
