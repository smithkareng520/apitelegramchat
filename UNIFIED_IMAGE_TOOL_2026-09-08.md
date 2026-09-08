# 统一图像工具：generate_image（2026-09-08）

## 背景

图像模型端点存在天然差异（OpenAI Images 协议下文生图走 `/images/generations`、
编辑走 `/images/edits`），此前因此拆出两个工具：

- `generate_image_from_text` —— 纯文生图
- `edit_image_with_reference` —— 带参考图编辑

两个工具的参数几乎完全重叠（prompt / model / aspect_ratio / image_size /
num_images），模型需要先判断"用户要不要改图"再挑工具，多了一步不必要的
决策，也偶发选错工具（有参考图却调了文生图工具）。

## 变更

合并为**单一工具 `generate_image`**，操作语义由 `image_url` 是否提供显式决定：

| 调用形状 | 语义 | 请求路径 |
|---|---|---|
| 不带 `image_url` | 文生图（CREATE） | `/images/generations` 或 chat.completions+modalities |
| 带 `image_url` | 编辑/图生图（EDIT） | `/images/edits`（multipart，失败明确报错，绝不降级） |

### 工具描述中的模型能力说明

描述内嵌两份动态清单（由 `SUPPORTED_MODELS` 能力位自动推导，配置增删模型
无需改工具定义）：

- **Edit-capable models**（可携带 `image_url`）：`native_image=True` 且 `vision=True`
- **Generate-only models**（仅文生图，勿传 `image_url`）：`native_image=True` 且 `vision=False`

### 执行层硬校验

仅文生图的模型被误传 `image_url` 时，请求不发往上游，直接返回可操作错误
（列出支持编辑的模型），引导模型改选模型或去掉 `image_url`。

### UI 折叠块自适应

沿用 text_editor 按 command 派生组类型的模式，工具折叠块与工具组折叠块
（进行态 + 完成态）均按 `image_url` 是否携带实时区分显示：

| 状态 | 不带 image_url | 带 image_url |
|---|---|---|
| 工具组/工具折叠块（进行时） | Generating an image / Generating {n} images | Editing an image |
| 工具组/工具折叠块（完成后） | Generated an image / Generated {n} images | Edited an image |
| 结果卡片标题 | 🎨 Generated N image(s) / 已生成 N 张图片 | 🎨 Edited N image(s) / 已编辑 N 张图片 |
| 失败标题 | 🎨 图片生成失败 | 🎨 图片编辑失败 |

### 旧工具名兼容（隐藏别名）

两个旧名**不再进入工具清单**，但 dispatch 层保留为隐藏别名：

- 历史会话上下文中的旧 tool_call 仍然有效（不会得到"未知工具"）；
- 模型偶发的旧名幻觉调用也能正常执行；
- `generate_image_from_text` 保持历史语义：即使误带 `image_url` 也强制丢弃（文生图）；
- 旧名同样纳入图像工具超时豁免集合（`IMAGE_GEN_TOOLS`）。

## 涉及文件

| 文件 | 变更 |
|---|---|
| `src/search/tool_schemas.py` | 两工具 schema 合并为 `generate_image`；新增 `GENERATE_ONLY_MODELS` 推导；描述写明双模式与模型能力边界 |
| `src/tool_dispatch.py` | 分发合并为单一分支；新增 `_IMAGE_TOOL_LEGACY_ALIASES` 旧名别名 |
| `src/search/media_tools.py` | `execute_generate_image` 增加"仅文生图模型 + image_url"硬校验；docstring 同步 |
| `src/ai/_constants.py` | `IMAGE_GEN_TOOLS` 纳入新名 + 两个旧名 |
| `src/ai/tool_summary.py` | 单工具折叠块进行态/完成态按 image_url 区分生成/编辑 |
| `src/ai/rich_message_builder.py` | 工具组折叠块进行态、组类型派生（image_generate / image_edit）、完成态模板同步 |
| `src/tool_result_format.py` | 结果卡片标题/失败标题按 image_url 区分；超时分支纳入新名 |
| `src/ai/attachment_content.py` | 上传图片提示改为指向 `generate_image.image_url` |
| `src/ai_handlers.py` | 富文本规范 2.4 节工具名更新 |
| `src/subagent_tool.py` | 子 agent 白名单注释更新 |
| `src/config.py` | gpt-image-2 能力注释更新（顺带修正已过时的"自动回退"描述，与 HOTFIX_2026-09-08_EDIT_FALLBACK 一致） |
| `docs/tool-ui-summaries.md` | 折叠块文案对照表更新 |
| `tests/unit/test_unified_image_tool.py` | 新增 15 个回归测试（schema/分发/硬校验/UI 四层） |

## 验证

- 新增 `tests/unit/test_unified_image_tool.py`：15 passed
- 全量测试：195 passed / 3 failed（3 个失败与原始包一致的历史遗留：
  workspace namespace 隔离、draft_manager rollover、markdown converter
  code span 嵌套，均与本次改动无关）
- 冒烟：SEARCH_TOOLS 中 `generate_image` 在列、旧名不在列；描述中
  Edit-capable / Generate-only 清单随配置正确展开
