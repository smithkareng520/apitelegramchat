# Telegram 富消息视频修复说明

## 结论

项目现在支持以 Telegram Rich Message 的**规范媒体数组**内嵌视频。模型仍可生成独立的 `<video src="HTTPS_URL"></video>` 或带图注的 `<figure><video ...></video><figcaption>...</figcaption></figure>`；发送层会将该 HTML 自动改写为 `tg://video?id=...`，并在 `rich_message.media` 中提供对应的 `InputMediaVideo` 对象。

该做法解决了日志中的 `RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND` 与 `RICH_MESSAGE_VIDEO_URL_INVALID` 两类问题：先前代码只发送直接 URL 的 HTML 视频标签，且在 `InputRichMessage` 中额外发送了非规范的 `content` 字段；现在媒体对象被显式声明，且只使用 API 所允许的 `html` 字段加可选 `media` 字段。

| 范围 | 修复前 | 修复后 |
| --- | --- | --- |
| 永久富消息视频 | 直接 `<video src="URL">`，依赖服务端从 HTML 推断媒体 | `html` 通过 `tg://video?id=...` 引用，`media` 显式携带 `InputMediaVideo` |
| URL 的 `&` 参数 | 转义后仍混入 HTML 媒体 URL | HTML 实体还原后存入 `media[].media`，保留真实预签名 URL |
| 视频媒体错误 | 400 后整个富消息失败 | 仅重试一次：转换为“查看视频”安全链接，确保消息可送达 |
| 草稿消息 | 可能提前发送视频块 | 因草稿不支持直接上传新文件，自动降级为安全视频链接 |
| R2 上传 | 5 秒读超时、仅一次尝试 | 30 秒读超时、最多 3 次显式重试（1 秒、2 秒退避） |

## 修改内容

`src/apitelegramchat/utils.py` 新增统一的富消息构造器。它会为每个 HTTP(S) 视频 URL 创建合法、稳定的 `video_...` 媒体 ID，并将 HTML 中的 `src` 改写为 `tg://video?id=<ID>`。对于已确认的媒体抓取错误，会移除视频块并保留可点击链接作为降级内容。

`src/apitelegramchat/ai_handlers.py` 中的模型输出指令已经更新为使用可公开访问的 HTTP(S) 直链和标准 `<video>` 标签；模型不应自行创建 `tg://video` ID，避免与发送层的媒体数组不一致。

`src/apitelegramchat/s3_utils.py` 调整为 5 秒连接超时、30 秒读超时及 3 次显式重试，以减少视频字节上传到 R2 时的瞬态 `ReadTimeoutError`。

`tests/validate_rich_message_video_media.py` 为新增离线回归测试；既有 `tests/validate_rich_message_fallback.py` 也已调整为校验规范 `html` 载荷。

## 验证结果

已通过以下离线验证：

```text
python3 -m py_compile src/apitelegramchat/utils.py src/apitelegramchat/ai_handlers.py src/apitelegramchat/s3_utils.py tests/validate_rich_message_video_media.py tests/validate_rich_message_fallback.py
PYTHONPATH=src python3 tests/validate_rich_message_video_media.py
PYTHONPATH=src python3 tests/validate_rich_message_fallback.py
```

两个回归测试均报告 `PASS`。测试覆盖了视频 HTML 到 `rich_message.media` 的转换、预签名 URL 参数保留、草稿链接降级、`RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND` 的一次性安全回退，以及既有 `RICH_MESSAGE_CONTENT_REQUIRED` 回退路径。

## 部署后的验收建议

部署后，请让机器人生成一个短视频，并检查应用日志。成功路径应包含 R2 上传成功（或原始 URL 的显式媒体声明）及 `sendRichMessage` 成功；不应再出现因 HTML 直接视频 URL 导致的 `RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND`。若媒体源在 Telegram 拉取时仍不可访问，机器人会发送可点击的“查看视频”链接，而不是丢失最终回复。

## 参考依据

Telegram Bot API 的 `InputRichMessage` 要求在使用 `html` 时，通过可选的 `media` 字段指定 HTML 中 `tg://video?id=...` 所引用的媒体；`InputRichMessageMedia` 将该 ID 映射到 `InputMediaVideo`。`InputMediaVideo.media` 可使用 HTTP URL。草稿接口明确不支持直接上传新文件。[1]

[1]: https://core.telegram.org/bots/api#inputrichmessage "Telegram Bot API — InputRichMessage"
