# 热修复记录（2026-09-08）：图像编辑"假成功"修复

## 故障现象（生产日志）

`edit_image_with_reference`（gpt-image-2）用户要求"移除场景中所有行人，
保持其他所有内容完全不变"，结果返回的图片**场景、风格整体重绘，与原图
完全无关**。关键日志：

```text
[NativeImage/OpenAICompat] https://xxtf.baby/v1 近期 /images/edits 404/405
已缓存，跳过直接走 /images/generations 兼容形状
[NativeImage/OpenAICompat] /images/edits 不可用 (status=404 detail='')，
回退 /images/generations JSON+image 形状
```

## 根因

带参考图的编辑请求本应走官方 `POST /v1/images/edits`（multipart，
`image[]` 文件字段）。但旧实现存在两层致命问题：

1. **隐式降级**：`/images/edits` 返回 404/405 后，把参考图塞进
   `{"image": "data:image/...;base64,..."}` 发到 `/images/generations`。
   官方 generations 端点**不接受 image 参数**——中转站忽略该未知字段后，
   这次请求等价于纯文生图 `{model, prompt}`。
2. **路由能力缓存**：404/405 按 base_url 缓存 1 小时（TTL 内连
   `/images/edits` 都不再尝试，直接走降级形状）。

于是出现最恶劣的"假成功"：HTTP 200 → 程序认为"编辑成功" → 实际模型
从未收到原图，产出一张与 prompt 匹配但与原图无关的全新图片。

## 修复内容（原则：编辑请求宁可明确失败，也绝不假成功）

### `src/ai/media_generation.py`

- **彻底删除** `/images/generations + image` 的隐式 fallback 及全部相关
  机制：`_EDITS_FALLBACK_STATUSES`、`_EDITS_FALLBACK_BODY_HINTS`、
  `_should_fallback_to_generations()`、`_EDITS_UNSUPPORTED_TTL_SECONDS`、
  `_edits_unsupported_until`、`_mark_edits_unsupported()`、
  `_edits_known_unsupported()`。
- `_request_openai_compat_image` 编辑分支改为**严格单端点**：有参考图
  只允许 `POST /images/edits` multipart（`model/prompt/n[/size]` +
  重复 `image[]` 文件字段）；失败时明确报错，错误详情注明"程序不会回退
  到 /images/generations"，让上层模型能看出该换端点/换提供商而非重试。
- **参考图真实性校验**（新增）：
  - `_download_reference_image_bytes`：http(s) 下载后检查 Content-Type
    并用 Pillow 验证 magic bytes——HTTP 200 + text/html 的错误页不再能
    伪装成参考图；data URL 同样过校验（且 `b64decode(validate=True)`）。
  - `_image_urls_to_data_urls`：data URL / 下载内容全部先验证再进入
    请求；无效参考图一律跳过（全失败时返回 400）。
  - `_bytes_to_data_url`：MIME 以 Pillow 实际识别格式为准，不再按扩展名
    猜测导致 PNG 字节标成 image/jpeg 之类错配。
- **不泄露图片内容的调试留痕**：multipart 发送前以 debug 日志记录
  `reference_count` 与每张参考图的 `bytes/mime/sha256[:16]`——能证明
  "HTTP body 里真的有图、每次是同一张图"，而不只是"程序准备了一张图"。
- 保留的生产鲁棒性（语义不变）：超大参考图（>3MB）先降采样再上传、
  "请求体未完整/请重试"类瞬态 400 同形状自动重试一次。
- **ModelScope 分支零改动**：其图生图本就走
  `/images/generations` + `X-ModelScope-Task-Type: image-to-image-generation`
  头（厂商自有协议，`image_url` 字段受官方支持），不能按 OpenAI 标准
  编辑逻辑误伤。

### `src/core/images.py` / `src/protocols/images.py`

文档注释同步（行为此前已符合"显式操作语义"：`ImageTask.edit/variation`
无参考图直接 `ValueError`，`generate` 清空参考图）。

### `tests/unit/test_media_generation_image_extraction.py`（新增 3 个回归测试）

- `test_openai_compat_edit_never_falls_back_to_generations`：
  `/images/edits` 返回 404 时，最终返回明确错误（含"不会回退"字样），
  且全程只请求过 `/images/edits` 一次——generations 一旦被调用测试即失败。
- `test_image_task_edit_requires_reference_image`：无参考图的 edit 任务
  直接 `ValueError`，不偷偷变文生图。
- `test_image_urls_to_data_urls_skips_html_payload`：HTML 错误页不进入
  参考图列表。

## 验证

```text
PYTHONPATH=src pytest -q tests/unit/test_media_generation_image_extraction.py
11 passed

PYTHONPATH=src pytest tests/
195 passed, 3 failed（3 个失败在修改前的原始包中同样失败，与本次改动无关：
workspace prompt namespace / draft manager rollover / markdown converter）
```

## 修复后行为

- 文生图：`/images/generations`（不变）。
- 编辑：下载参考图 → Pillow 验证 → multipart `/images/edits` →
  gpt-image-2。
- `/images/edits` 不可用（如当前 xxtf 中转站 404）：**明确报错**，日志
  `edit rejected ... NO fallback to /images/generations`，不再产生
  "200 OK + 实际生成另一张图"的假成功。

> 运维提示：当前 XXTF 中转站尚未实现 `/v1/images/edits`（404）。修复后
> 编辑请求会明确失败而不是返回无关新图。如需恢复编辑能力，需中转站侧
> 实现 OpenAI 官方 "Create image edit" multipart 端点，或换用支持的提供商。
