# 图像工具错误呈现修复说明

## 改进结果

图像工具失败时，不再将原始接口字段逐行直接显示给用户。界面现在分为两层：第一层使用简洁、可操作的中文提示；第二层用 Telegram Rich Message 支持的 `<blockquote>` 呈现诊断信息。这样保留排查所需的状态、模型和请求标识，同时避免重复显示 `Request ID` 或把原始错误当作用户提示。

| 场景 | 用户提示 | 引用式诊断信息 |
| --- | --- | --- |
| HTTP 429 | 当前图片服务请求较多，请稍后再试；无需修改你的描述。 | 服务繁忙、HTTP 429、模型、请求标识 |
| HTTP 400 / 422 | 请简化描述或调整表述后重试。 | 请求暂未被处理、HTTP 状态、模型、服务响应 |
| HTTP 401 / 403 | 图片服务暂时不可用，请稍后再试。 | 服务访问受限、HTTP 状态、模型、请求标识 |
| HTTP 5xx | 图片服务暂时不可用，请稍后再试。 | 服务异常、HTTP 状态、模型、服务响应 |

对于题述的 429 示例，前端将显示如下结构：

```html
<p><b>图片暂时无法生成</b></p>
<p>当前图片服务请求较多，请稍后再试；无需修改你的描述。</p>
<blockquote><b>诊断信息</b><br/>
<b>状态：</b>服务繁忙（HTTP 429）<br/>
<b>模型：</b>Tongyi-MAI/Z-Image-Turbo<br/>
<b>请求标识：</b>e12bf114-078b-4fd8-a7ec-99c5f640229f
</blockquote>
```

## 修改范围

`src/apitelegramchat/tool_executors.py` 会拦截 `generate_image_from_text` 与 `edit_image_with_reference` 的既有图片接口失败文本，并构造富文本卡片，而不是将整段原始文本做 HTML 转义。该路径覆盖通过工具调用生成图片的场景。

`src/apitelegramchat/ai_handlers.py` 的原生图片模型错误格式化也已同步为同样的“用户提示 + 引用式诊断”结构，因此不依赖工具调用的图片模型也会有一致的体验。

新增 `tests/validate_image_error_presentation.py`，覆盖 HTTP 429 限流和 HTTP 503 服务异常。测试同时验证：诊断块使用 `<blockquote>`、429 提示明确、重复 `Request ID` 只保留一次、非重复服务响应仍会保留在诊断信息中。

## 验证

以下命令已经通过：

```text
python3 -m py_compile src/apitelegramchat/ai_handlers.py src/apitelegramchat/tool_executors.py src/apitelegramchat/utils.py tests/validate_image_error_presentation.py tests/validate_rich_message_video_media.py tests/validate_rich_message_fallback.py
PYTHONPATH=src python3 tests/validate_image_error_presentation.py
PYTHONPATH=src python3 tests/validate_rich_message_video_media.py
PYTHONPATH=src python3 tests/validate_rich_message_fallback.py
```

所有三个验证脚本均报告 `PASS`。
