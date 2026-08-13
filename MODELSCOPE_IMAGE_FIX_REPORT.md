# ModelScope 图片生成 `builder` 参数报错修复说明

## 结论

本次错误并非 ModelScope 服务端、模型名称或 API Key 导致，而是项目内部 Python 函数调用的**关键字参数与函数签名不匹配**。`generate_image_from_text` 被分发到 `search_engine.execute_generate_image()` 后，在 ModelScope 分支向 `_request_modelscope_native_image()` 传入了 `builder=None`。然而后者的定义只接受 `prompt`、`image_urls`、`num_images` 和 `model`，不接受 `builder`。Python 在发起任何 HTTP 请求之前便抛出 `TypeError`，因此日志中会反复出现同一错误。

> `TypeError: _request_modelscope_native_image() got an unexpected keyword argument 'builder'`

## 修改内容

| 文件 | 位置 | 修复 |
|---|---:|---|
| `src/apitelegramchat/search_engine.py` | ModelScope 分支的 `_request_modelscope_native_image(...)` 调用 | 删除无效的 `builder=None`。 |
| `tests/validate_modelscope_image_signature.py` | 新增 | 解析函数签名和所有直接调用点，若再出现不支持的关键字参数即失败。 |

该修复采用**删除调用端遗留参数**的方式，而不是给底层函数增加一个未使用的 `builder` 参数。这样参数契约保持清晰，未来出现类似参数漂移也可由新增回归检查尽早发现。

## 已执行验证

| 检查 | 结果 |
|---|---|
| `python3 tests/validate_modelscope_image_signature.py` | 通过：调用参数与函数签名一致。 |
| `python3 -m py_compile src/apitelegramchat/search_engine.py src/apitelegramchat/ai_handlers.py` | 通过：两处 Python 源文件语法有效。 |
| 补丁差异检查 | 仅删除图片调用的 `builder=None`，并新增针对该参数契约的回归检查。 |

## 部署步骤

请将修复包中的 `src/apitelegramchat/search_engine.py` 覆盖到实际部署代码，或直接应用随附的 `modelscope_image_builder_fix.patch`，然后重建并重启应用服务。重启后重新触发一次 ModelScope 文生图或图生图请求；若仍有失败，日志应转为真实的 HTTP 状态、服务返回详情或图片上传错误，而不再出现本次 `unexpected keyword argument 'builder'` 的 Python 异常。

## 影响范围

修复只影响 `provider == "modelscope"` 的图片生成路径。其他图片提供方、视频生成调用以及工具分发器均无需修改。
