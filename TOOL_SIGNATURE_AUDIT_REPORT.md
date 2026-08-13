# 工具调用参数契约审计与统一修复报告

## 审计结论

视频生成路径确实存在与图片生成同类的参数契约漂移：调用方和被调用函数对于 `builder` 参数的约定不一致。该问题发生在 Python 本地函数调用阶段，早于任何视频 API 请求，因此会表现为视频工具直接失败，而不是供应商返回的 HTTP 错误。

本次审计还覆盖了应用源目录中所有直接调用的 `execute_*` 工具函数与 `_request_*` 请求辅助函数。修复后，静态审计通过，未发现其他同类的“调用方传入不支持参数”或“调用方遗漏必需参数”问题。

## 发现与修复

| 路径 | 原始问题 | 影响 | 修复 |
|---|---|---|---|
| `search_engine.py` → `_request_modelscope_native_image` | 调用传入 `builder=None`，函数不接受该参数。 | ModelScope 文生图、图生图工具在发起 API 前抛出 `unexpected keyword argument 'builder'`。 | 已删除该参数。 |
| `search_engine.py` → `_request_agnes_video` | 调用传入 `builder=None`，函数不接受该参数。 | Agnes 视频工具在发起 API 前抛出相同类型的 `TypeError`。 | 已删除该参数。 |
| `ai_handlers.py` → `_request_openrouter_video` | 函数声明了必需 `builder`，但函数体不使用它；原生视频路径调用时未传入该参数。 | OpenRouter 原生视频路径会抛出“缺少必需参数 `builder`”。 | 已从函数签名移除无用参数，统一为 `prompt`、`duration`、`model`。 |
| 其他直接工具和请求调用 | 无相同不匹配项。 | 无需代码改动。 | 新增自动审计，后续变更会被拦截。 |

> `builder` 是富消息/界面层概念，不属于图片或视频 HTTP 请求的业务参数。把它在不同调用链中以“占位参数”形式透传，造成了本次签名漂移。

## 新增回归保护

| 检查文件 | 覆盖内容 |
|---|---|
| `tests/validate_modelscope_image_signature.py` | 验证 ModelScope 图片请求的直接调用参数均存在于函数签名中。 |
| `tests/validate_tool_call_signatures.py` | 静态分析 `src/apitelegramchat` 内的顶层 `execute_*` 与 `_request_*` 定义和直接调用，拦截不支持参数与遗漏必需参数。 |

## 已执行验证

| 命令 | 结果 |
|---|---|
| `python3 tests/validate_modelscope_image_signature.py` | 通过。 |
| `python3 tests/validate_tool_call_signatures.py` | 通过。 |
| `python3 -m py_compile src/apitelegramchat/search_engine.py src/apitelegramchat/ai_handlers.py` | 通过。 |
| `grep -RIn 'builder=None' src/apitelegramchat` | 无匹配结果。 |

## 部署与后续定位

请部署本修复包，或应用随附的补丁后重新构建并重启服务。视频功能重试后，如果仍失败，新的日志应展示供应商侧的实际原因，例如提交阶段的 HTTP 状态、轮询超时、任务失败消息或媒体下载/上传问题；这类错误与本次 Python 参数签名异常不同，需要根据新的完整日志继续定位。

本次修复不修改 API Key、模型配置、请求端点、超时策略或 R2 上传逻辑，影响范围仅限于图片/视频工具调用函数之间的参数契约。
