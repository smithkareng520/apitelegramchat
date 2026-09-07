# Image Tool Fixes

针对图像生成/编辑出现“请求 1 张、结果却上传 2 张，其中一张不可用”的问题。

## 修复内容

- 收紧 `_extract_image_items()`：不再对整个响应递归扫描 URL，不会再把 `fallback/error/debug/metadata` 等普通 URL 当成图片。
- 仅从明确的图片承载字段提取结果：`data`、`images`、`output_images`、`results`、`choices`、`output`、`message.images`，以及 `content` 中显式的 `image/image_url` part。
- `_response_items_to_bytes()` 增加 `max_images` 限制，调用方请求 `n=1` 时最多处理 1 张。
- 下载远程图片时检查 `Content-Type`（存在时必须为 `image/*`）。
- 所有进入 R2 的字节都使用 Pillow 做实际图片格式校验，HTML/JSON/错误页不会再被命名成 `.png` 上传。
- R2 上传根据实际格式使用正确的扩展名与 MIME：PNG/JPEG/WEBP/GIF。
- agentic 原生图像路径与 OpenRouter 兼容图像路径也采用同样的图片内容校验。
- 新增回归测试，覆盖“任意 URL 不得误提取”“非图片 base64 拒绝”“真实图片可接受”“n=1 数量限制”。

## 验证

通过：

```text
PYTHONPATH=src pytest -q tests/unit/test_media_generation_image_extraction.py
5 passed
```

同时对修改后的 Python 文件执行了 `py_compile`。

全仓 `pytest` 在当前执行环境中因已有可选/运行时依赖未安装而无法完成收集：`quart`、`tiktoken` 缺失。该问题与本次修改无关。
