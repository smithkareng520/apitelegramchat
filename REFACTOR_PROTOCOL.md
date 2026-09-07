# 协议层重构说明（REFACTOR_PROTOCOL.md）

> 本次重构把项目从「支持多个 API 的 Agent」推进为「**一个模型无关的
> AI Gateway + Agent Runtime**」：协议选择是模型配置的一等字段，消息
> 表示与具体厂商协议解耦，图像生成由显式任务模型驱动。

## 一、重构目标与结果对照

| 目标 | 旧架构问题 | 重构后 |
|---|---|---|
| Model → Protocol | `dedicated_loop_kind` 分散在 Provider/Model/Endpoint 三处，api_client 与 ai_handlers 各自 if-else 分流 | `ModelConfig.protocol`（模型级，None=继承厂商默认）+ `ProviderConfig.protocol`（厂商默认=该厂商广泛支持的协议），路由收敛到 `protocols/registry` 一张表 |
| OpenAI 为缺省 | OpenAI 格式被当成内部格式，处处耦合 | `openai_chat` 只是**缺省协议**；内部表示是协议无关的 `core.messages.Message` |
| 内部消息结构 | OpenAI dict 即内部格式，多模态/附件元数据混在请求体字段里 | `Message(role, blocks, meta)`：多模态是一等公民（Text/Image/Audio/Video/Document/ToolCall/ToolResult/Reasoning 八种块）；Telegram 侧元数据进 `meta`，由**结构性保证**永不进出站请求 |
| 图像系统 | 「有参考图 = edit」的推断式设计，端点选择藏在请求函数里 | `ImageTask(operation=generate/edit/variation)` 显式任务模型；端点选择收敛在图像协议适配器 |
| 新增协议成本 | 改核心逻辑（两处分流 + 常量集合） | 实现 `ChatProtocolAdapter` + 注册一行，调用方零改动 |

## 二、目录与职责

```
src/
├── core/
│   ├── messages.py        # 【新】内部消息模型：Message + 8 种 Block
│   │                      #   + to/from_openai_dict 互转 + wire 渲染
│   ├── images.py          # 【新】ImageTask / ImageTaskResult / ImageRequestError
│   └── …（原有基础设施不动）
├── protocols/             # 【新】协议路由与适配器层（Model → Protocol 执行端）
│   ├── base.py            #   ChatProtocolAdapter 契约
│   ├── registry.py        #   聊天协议唯一路由出口 get_chat_adapter()
│   ├── openai_chat.py     #   openai_chat 适配器（缺省；99% 兼容模型零配置）
│   ├── anthropic_messages.py  # Anthropic 原生 Messages 适配器
│   ├── gemini_native.py   #   Gemini 原生 streamGenerateContent 适配器
│   └── images.py          #   图像适配器：openai_images / openai_chat(modalities)
│                          #   + dispatch_image_task() 统一分发
├── config.py              # protocol 字段（硬切换名）+ _VALID_PROTOCOLS 校验
├── api_client.py          # 按协议建客户端（anthropic_messages→AsyncAnthropic，
│                          #   其余带 base_url 的协议→AsyncOpenAI）
└── ai/
    ├── agentic_loops.py   # openai_chat 循环：Message →(每轮渲染)→ wire
    ├── anthropic_bridge.py# Message → Anthropic 块转换器 + 原生循环
    ├── gemini_bridge.py   # Message → Gemini contents 转换器 + 原生循环
    ├── media_generation.py# _request_openai_images_task / _request_chat_modalities_image_task
    └── attachment_content.py  # 多模态解析产出 Block（原 content parts）
```

## 三、协议取值与默认值

| protocol | 含义 | 厂商默认 | 模型级覆盖示例 |
|---|---|---|---|
| `openai_chat` | OpenAI 兼容 Chat Completions（**缺省**） | openrouter / modelscope / grok / deepseek / glm / agnes / xxtf | `gpt-5.6-sol` |
| `anthropic_messages` | Anthropic 原生 `/v1/messages` | anthropic | `claude-opus-5`（XXTF 中转，base_url 覆盖 + 协议覆盖） |
| `gemini_native` | Gemini 原生 streamGenerateContent | gemini | — |
| `openai_images` | OpenAI Images（generations / edits） | — | `gpt-image-2`、`Qwen/Qwen-Image-Edit`、`Tongyi-MAI/Z-Image-Turbo` |

**配置语义（保持并强化原有合并规则）**：

```python
ModelConfig.protocol = None          # 继承 ProviderConfig.protocol（厂商广泛支持的协议）
ModelConfig.protocol = "anthropic_messages"   # 模型级覆盖为任意协议
```

- `get_effective_endpoint()` 仍是唯一合并出口；`EffectiveEndpoint.dedicated_loop_kind`
  已改名为 `.protocol`。
- 旧字段 `dedicated_loop_kind` / `use_dedicated_loop` **一次性硬切**：
  `make_model_config()` 遇到即抛 `ValueError`，不留软别名。
- 非法协议在配置期（make_model_config）与路由期（get_chat_adapter）双重校验。

## 四、调用流程（重构后）

```
用户消息
   ↓
Agent（ai_handlers.get_ai_response 组装 Message 列表）
   ↓
查 ModelConfig.protocol（get_effective_endpoint 合并厂商默认与模型覆盖）
   ↓
protocols/registry.get_chat_adapter(protocol)     ← 唯一路由出口
   ↓
适配器.run_agent_loop(...)                         ← 内部取客户端、进协议循环
   ↓
openai_chat        → AsyncOpenAI      → /chat/completions（每轮把 Message 渲染为 wire）
anthropic_messages → AsyncAnthropic   → /v1/messages（Message → 原生块）
gemini_native      → aiohttp 直连     → streamGenerateContent（Message → contents）
```

图像：

```
工具层 / 原生图像循环
   ↓ 构造 ImageTask（operation 显式声明）
ImageTask(generate|edit|variation, prompt, input_images, model)
   ↓ protocols/images.dispatch_image_task
openai_images 适配器 → /images/generations（generate）
                     → /images/edits（edit/variation；路由 404/405 自动回退兼容形状）
                     → ModelScope 一律 generations + 任务轮询
openai_chat  适配器 → chat.completions + modalities（参考图作为消息内容）
   ↓
ImageTaskResult(images: list[bytes], text, refusal, endpoint, usage)
```

## 五、Internal Message 设计要点

1. **块类型**：`TextBlock` / `ReasoningBlock` / `ImageBlock`（http URL 与
   data: URL 统一为 `url`）/ `AudioBlock`（base64 + format）/ `VideoBlock` /
   `DocumentBlock`（`url` 或 `data_url`）/ `ToolCallBlock`（arguments 为
   结构化 dict；`extra` 承载协议特有元数据，如 Gemini thoughtSignature）/
   `ToolResultBlock`。
2. **meta 出站隔离**：Telegram 附件元数据（file_id/file_ids/type/…）与
   内部标记（turn_recovery 失败标记等）存 `Message.meta`，`to_openai_dict()`
   结构性不渲染——替代旧版"出站前手工剔除字段"的脆弱约定（该约定曾导致
   静默 400 BUG）。
3. **每轮渲染 + 出站装饰**：协议循环每轮把 Message 列表渲染为 wire 后再
   打 prompt-cache 断点（cache_control 是纯出站装饰，不进内部消息）。
4. **全链路替换**：历史存储（state.conversation_history）、journal（打断
   保全）、工具结果回写、上下文压缩、守卫裁剪统一持有 Message；用户消息
   的 Telegram 信封在持久化边界（turn_recovery / update_conversation_and_ledger）
   转为 Message。
5. **双形状过渡**：context_window / context_manager / gemini_cache 等纯逻辑
   同时接受 Message 与旧 dict，旧测试与调用方零成本迁移。
6. **历史 user 消息按模型能力重解析**：`_append_history_async` 对带 meta 的
   user 消息每轮重新解析为当前模型可用的块（多模态模型得原生块，其余得
   文本占位），语义与旧版一致。

## 六、ImageTask 设计要点

1. **操作显式化**：`operation` 是任务一等字段（generate/edit/variation），
   构造器校验——generate 必须有 prompt 且无参考图；edit/variation 至少一张
   参考图；variation 允许空 prompt（适配器补默认指令
   `DEFAULT_VARIATION_PROMPT`）。
2. **推断点唯一化**：原生图像循环里"带参考图 = edit"的判断只存在于任务
   构造处（`_extract_image_prompt_and_reference_urls` 之后），进入适配器后
   不再有"看图猜端点"逻辑。
3. **端点语义**：
   - generate → `/images/generations`（JSON）
   - edit → `/images/edits`（官方 multipart；XXTF 路由级 404/405 自动回退
     JSON 兼容形状——保留全部既有鲁棒性逻辑）
   - variation → 同 edit（ModelScope / XXTF 均无 /variations 端点）
   - ModelScope → 一律 `/images/generations` + `X-ModelScope-Task-Type` 头
   - openai_chat 模态路径 → 参考图作为消息内容输入
4. **全图像模型覆盖**：modelscope/xxtf 的 Images 模型、openrouter 的
   chat-modalities 模型（gemini-image / seedream）、agnes 图像模型、以及
   未注册的历史别名（flux 等，按 OpenRouter 兼容直连）统一经
   `dispatch_image_task` 分发；R2 上传与富媒体渲染仍由调用方统一后处理。

## 七、兼容性与迁移说明

- **硬切项**：`dedicated_loop_kind` 字段名与旧取值（openai_compat /
  anthropic_native）不再被接受；错误信息直接指引改用 `protocol`。
- **行为保持**：请求载荷形状（wire）与重构前逐字节兼容（含多模态 parts、
  tool_calls、reasoning_content、缓存断点位置），provider 端 prompt 缓存
  前缀不受影响。
- **内部 API 改名**（如 `_build_image_content_part` → `_build_image_block`、
  `_build_native_document_part` → `_build_native_document_block`）均为私有
  函数，外部调用方已同步更新。
- **subagent 的 Anthropic 分流**：从"provider == anthropic"扩展为
  "provider == anthropic 或有效协议 == anthropic_messages"，中转 Claude 模型
  的子 agent 自动走原生桥。

## 八、验证

- 既有单元/集成测试全量通过（216 passed；3 项失败为重构前即存在的
  环境相关问题，与本次改动无关）。
- 冒烟覆盖：
  1. 全部 19 个模型的协议解析（厂商默认 + 模型覆盖 + 图像模型 openai_images）；
  2. 旧字段/非法协议硬切报错；
  3. Message 全类型往返与三协议（OpenAI wire / Anthropic 块 / Gemini contents）渲染；
  4. meta 出站隔离、thoughtSignature 往返；
  5. ImageTask 构造校验 + 双适配器分发（干跑桩）；
  6. openai_chat / anthropic_messages 循环以桩客户端端到端运行（请求载荷
     形状、Message 历史追加、usage 透传）。

## 九、新增协议的操作指南

1. `config._VALID_PROTOCOLS` 加协议标签；
2. 新建 `src/protocols/<protocol>.py` 实现 `ChatProtocolAdapter.run_agent_loop`
   （或图像任务则实现 `ImageProtocolAdapter.run_image_task` 并注册进
   `IMAGE_PROTOCOLS`）；
3. `protocols/registry.CHAT_PROTOCOLS` 注册一行；
4. 需要原生 SDK 客户端时在 `api_client._NATIVE_SDK_PROTOCOLS` 加一行。
其余模块（ai_handlers / agentic_loops / bridges）零改动。
