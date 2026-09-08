# Responses API 缓存断点：默认自动、显式断点可选开启 — 2026-09-09

## 背景

上一版把 Responses 请求硬编码为「3 个显式断点 + 1 个 implicit 自动断点」：
每个请求都在 `input` 文本块上写入 `prompt_cache_breakpoint: {"mode":
"explicit"}` 对象。但当前 XXTF 网关（gpt-5.6-sol）**尚未支持手动断点，
仅支持自动断点**——显式断点字段一旦下发就有 400 风险（此前
`"prompt_cache_breakpoint": true` 布尔形状即因此在网关侧暴露，虽然对象
形状已按官方 SDK 类型定义修正，但网关不支持时任何形状都会被拒）。

## 新策略：默认自动 + 参数开启手动

缓存层级现在分两档，与 Anthropic 桥接（Claude）的断点策略保持同构：

### 默认（所有 API，零配置）

请求只携带：

```json
"prompt_cache_key": "tg-chat-{chat_id}-{epoch}",
"prompt_cache_options": { "mode": "implicit", "ttl": "30m" }
```

断点完全由网关自动管理（implicit 模式保留 1 个自动断点），请求体里
**绝不出现任何 `prompt_cache_breakpoint` 字段**。主循环、超限合成请求、
subagent 非流式调用三条路径一致。

### 显式断点（可选增益，参数开启后生效）

开启后在自动断点**之外额外下发 3 个显式断点**，合计 3 显式 + 1 自动，
不超 Responses API 每请求 4 个的上限。3 个位置复刻
`anthropic_bridge.py` 的 Claude 断点策略：

| Claude（Anthropic Messages）          | Responses（GPT-5.6）                       |
| ------------------------------------- | ------------------------------------------ |
| 断点 0：顶层 system 段末尾（1h TTL）  | instructions 是纯字符串参数无法挂断点，由保留的 implicit 自动断点兜底 |
| 断点 1：第一条 user 消息末尾          | 显式 1：输入中第一个 `input_text` 块（前部固定） |
| 断点 2：倒数第二条消息末尾            | 显式 2：最后两个可用文本块（尾部滚动）      |
| 断点 3：最后一条消息末尾              | 同上                                        |

TTL 仍由 `prompt_cache_options.ttl` 统一为 `30m`（Responses 当前唯一档位，
不能像 Anthropic 那样逐断点 1h/5m 区分）。

## 开启方式（二选一，环境变量优先）

1. **环境变量（推荐，部署环境直接切换，无需改代码）**：

   ```bash
   RESPONSES_EXPLICIT_CACHE_BREAKPOINTS=1   # 三态强制开启
   RESPONSES_EXPLICIT_CACHE_BREAKPOINTS=0   # 三态强制关闭（优先级最高）
   ```

   读取逻辑见 `config.explicit_cache_breakpoints_enabled()`（调用时读取，
   支持 `1/true/yes/on` 与 `0/false/no/off`，无法识别的值不强制）。

2. **模型/厂商配置字段**（`config.py`）：

   ```python
   SUPPORTED_MODELS["gpt-5.6-sol"] = make_model_config(
       ...,
       explicit_cache_breakpoints=True,   # None = 继承厂商默认（False）
   )
   ```

   `ProviderConfig.explicit_cache_breakpoints` / `ModelConfig.
   explicit_cache_breakpoints` 均默认 `False`/`None`，只有 openai_responses
   协议链路读取该字段，对 Claude / Gemini / OpenAI 兼容模型零影响。

## 改动文件

- `src/config.py`：新增 `explicit_cache_breakpoints` 字段
  （ProviderConfig / ModelConfig / `_PROVIDER_DEFAULTS["xxtf"]` /
  `make_model_config` 透传）+ 三态环境变量开关
  `explicit_cache_breakpoints_enabled()`。
- `src/ai/responses_bridge.py`：
  - 主循环：`_apply_responses_cache_breakpoints(input_items)` 从「每轮
    无条件调用」改为「仅当 `prompt_cache_enabled` 且
    `explicit_cache_breakpoints_enabled(model_info)` 时调用」；
  - 超限合成请求（`_synth_stream`）：同上，与主循环保持同一层级；
  - 非流式一次性调用（`openai_responses_chat_completions_create`，
    subagent 路径）：从硬编码 `enabled=True` 改为按模型
    `supports_prompt_cache` 判定（未知模型保守保留旧行为），显式断点
    同样走开关；
  - `_add_responses_cache_options` 文档注释更新：默认只注入 key +
    implicit 自动断点。
- `tests/unit/test_responses_cache_breakpoints.py`（新增）：15 项覆盖
  默认零断点字段、三态开关优先级、断点位置/数量/对象形状（含布尔形状
  回归）、非流式路径开关行为、未知模型安全回退。

## 验证

- `tests/unit/test_responses_cache_breakpoints.py`：15 项全部通过。
- 全量单测中与本次改动相关的既有套件无回归（`python -m pytest
  tests/unit`；沙箱中 `tiktoken` BPE 下载被出站白名单拦截导致的既有
  失败与本次改动无关，原始压缩包同样复现）。

## 网关就绪后的切换清单

1. 部署环境设置 `RESPONSES_EXPLICIT_CACHE_BREAKPOINTS=1`（或按模型加
   `explicit_cache_breakpoints=True`）；
2. 观察日志 `ai.cache_usage` 的 `Hit_ratio` 与
   `usage.input_tokens_details.cached_tokens`；
3. 若网关对显式断点形状报 400（参数名/位置不收），先回滚为 `=0` 再排查
   ——默认档（自动断点）始终是安全基线。
