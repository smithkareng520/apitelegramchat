# 图片与视频工具预签名 URL 全链路审计与修复

## 结论

此前的修复只覆盖了**原生图片/视频模型路径**，但用户实际看到的“图片 1”来自另一条独立的**通用工具调用路径**。该路径在工具返回后，把原始 R2 预签名 URL 同时用于 HTML 媒体属性和用户可点击的 `<a href>`。前者必须转义 `&` 为 `&amp;`，后者在当前客户端展示/跳转路径中却可能把字符串按字面量使用，因此请求把 `amp;X-Amz-Credential` 当作参数名，R2 返回 `InvalidArgument / Authorization`。

> HTML 属性源码里的 `&amp;` 是正确的；它不是可直接传给浏览器、HTTP 客户端、模型接口或 URL 按钮的 URL。

## 已定位的原始故障点

| 生成类型 | 原始 URL 的来源 | 用户可见的故障点 | 原因 |
|---|---|---|---|
| 通用图片工具 | `search_engine.execute_generate_image()` | `tool_executors._format_image_generation_result()` 的“图片 1” `<a href>` | `href` 使用 HTML 编码值，工具卡片/客户端将其作为字面量 URL 使用 |
| 通用视频工具 | `search_engine.execute_generate_video()` | `tool_executors.format_tool_result()` 的“下载 / 查看视频” `<a href>` | 同上 |
| 二维码工具 | `search_engine.execute_qr_code()` | `tool_executors.format_tool_result()` 的“点击查看 / 下载二维码” `<a href>` | 同上 |
| 原生图片/视频模型 | `ai.agentic_loops` 上传 R2 后的 URL | 没有原始 URL 按钮，模型历史也没有完整原始 URL | 上次只修正了部分富媒体渲染位置，未统一数据边界 |

## 修复后的数据边界

| 场景 | 使用值 | 代码实现 |
|---|---|---|
| `<img src>`、`<video src>` 等 HTML 属性 | HTML 属性 URL，使用 `&amp;` | `media_url_html_attr()` |
| 工具消息给 AI、后续模型请求、HTTP 下载 | 原始 URL，使用真实 `&` | `raw_media_url()` / 原始 `safe_content` |
| 用户点击“打开原图/打开原视频/打开二维码” | 原始 URL，作为 JSON `reply_markup.inline_keyboard[].url` | `_send_media_open_buttons()` 与 `_media_open_keyboard()` |
| 工具结果卡片 | 仅显示内联媒体及说明；不再输出图片 1、下载视频或二维码的 `<a href>` | `tool_executors.py` |

## 本次修改

| 文件 | 修改内容 |
|---|---|
| `src/apitelegramchat/utils.py` | 新增 `raw_media_url()` 与 `media_url_html_attr()`；发送前的属性清洗器先解码后重新规范编码，避免双重转义。 |
| `src/apitelegramchat/tool_executors.py` | 删除生成图片、视频、二维码工具卡片中的用户 `<a href>` 链接；内联媒体仍使用正确的 HTML 属性编码。 |
| `src/apitelegramchat/ai/tool_call_loop.py` | 从**原始工具结果**提取媒体 URL，并通过独立永久消息的 URL 按钮发送给用户。按钮 JSON 中保留真实 `&`。写回模型上下文的 `safe_content` 保持原始 URL。 |
| `src/apitelegramchat/ai/agentic_loops.py` | 原生图片/视频路径同样使用双通道：内联媒体 HTML 属性、原始 URL 点击按钮、以及原始 URL 模型历史。 |
| `src/apitelegramchat/ai_handlers.py` | 增加模型指令：禁止在最终文本中重复输出签名 URL 或普通超链接；只允许独立媒体块，点击使用工具完成消息的按钮。 |
| `tests/test_media_url_contexts.py` | 新增无第三方依赖回归测试，覆盖图片、视频、二维码、模型历史、内联属性和 URL 按钮。 |

## 已完成验证

执行：

```bash
python3 -m compileall -q src tests
python3 tests/test_media_url_contexts.py
```

结果：

```text
PASS: image/video tool URL contexts are isolated
```

测试验证了以下性质：图片/视频内联 HTML 中仍有合法的 `&amp;`；工具卡片不再存在“图片 1”或“下载 / 查看视频”的 `<a href>`；模型及 URL 按钮均只得到真实 `&` 的原始预签名 URL。

## 部署后的验收方式

生成一张图片和一个视频，预期会出现内联媒体，以及独立的“打开原图”或“打开原视频”按钮。不要从富文本 HTML 源码复制 `src` 内容；如需外部访问，请使用该按钮。预签名 URL 仍受 `X-Amz-Expires=3600` 控制，约一小时后失效属于正常行为。
