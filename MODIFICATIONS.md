# 激进并发 Research 模式修改说明

本版本针对“6 个 subagent 长时间深度检索”进行了并发架构调整。

## 核心参数

- `MAX_CONCURRENT_TOOLS`: 16（支持 6 个 subagent 同时运行及其并行工具调用）
- `SUBAGENT_MAX_ROUNDS`: 32（默认，可通过环境变量覆盖，范围 1~128）
- `SUBAGENT_DEFAULT_TIMEOUT`: 900 秒（默认，可覆盖，范围 60~1800）
- `SUBAGENT_LLM_TIMEOUT`: 180 秒/轮
- `SUBAGENT_TOOL_TIMEOUT`: 120 秒/工具
- 外层 `subagent` 工具超时：930 秒（可通过环境变量 `SUBAGENT_TOOL_TIMEOUT` 覆盖）
- `DDG_MAX_CONCURRENCY`: 8

## 关键行为变化

1. 不再用全局 DDG Lock 把所有搜索串行化为 1 路。
2. DDG 改为有限并发 semaphore，默认 8 路；6 个 research agent 可以真正并行搜索。
3. DDG 的 429 / 5xx 不再触发全局 cooldown；失败请求由现有 retry/backoff 机制自行重试，不阻断其它 agent。
4. 保留 subagent 的 `parallel_tool_calls=True`，允许单轮同时发出多个独立检索。
5. subagent 的总预算从默认 180 秒 / 16 轮提升到 900 秒 / 32 轮，满足深度研究任务。
6. 内层 timeout 上限提升到 1800 秒，并给外层额外 30 秒缓冲，避免内外层 timeout 互相误杀。

## 验证

- Python `compileall` 通过。
- DDG semaphore 并发测试通过：6 个并发查询的峰值并发数为 6，不再被全局锁压成 1。

## 2026-08-11 hotfix
- 修复 `ai_handlers.py` 缺少 `import os` 导致线上启动时 `NameError`。
- 拆分 `SUBAGENT_OUTER_TIMEOUT` 与 `SUBAGENT_TOOL_TIMEOUT`：前者控制主工具层包裹整个 subagent 的 930 秒超时，后者控制子 agent 单次工具调用的 120 秒超时，避免环境变量语义冲突。
- 更新 `render.yaml`：`SUBAGENT_MAX_ROUNDS=32`、`SUBAGENT_DEFAULT_TIMEOUT=900`、`SUBAGENT_LLM_TIMEOUT=180`、`SUBAGENT_TOOL_TIMEOUT=120`、`SUBAGENT_OUTER_TIMEOUT=930`、`DDG_MAX_CONCURRENCY=8`、`MAX_CONCURRENT_TOOLS=16`。
