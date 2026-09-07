# 修复说明

## 问题1：新旧草稿交替导致内容分离

### 问题描述
在工具执行过程中，文本内容结束后立即调用 `rollover_at_turn_boundary`，导致工具的流式输出（如 "Viewing file utils.py"）被拆分到新草稿中，造成内容分离。

### 根本原因
在 `anthropic_bridge.py`、`agentic_loops.py` 和 `gemini_bridge.py` 中，模型返回文本内容后立即检查并切换草稿，但此时工具调用的流式内容可能还在更新。

### 修复方案
删除文本块结束后的过早 `rollover_at_turn_boundary` 调用，只在工具批次完全执行完毕后再检查是否需要切换草稿。

### 修改文件
1. `src/ai/anthropic_bridge.py` (第 872-877 行)
2. `src/ai/agentic_loops.py` (第 739-744 行)
3. `src/ai/gemini_bridge.py` (第 987-992 行)

修改前：
```python
if reasoning_acc:
    builder.finalize_reasoning_block()
    await builder.rollover_at_turn_boundary(start_next_draft=True)

if content_acc:
    await builder.rollover_at_turn_boundary(start_next_draft=True)

await builder.flush()
```

修改后：
```python
if reasoning_acc:
    builder.finalize_reasoning_block()

# 修复问题1：不在文本块结束后立即 rollover，而是等待工具执行完毕。
# 否则工具的流式输出（如 "Viewing file utils.py"）会被拆分到新草稿。
await builder.flush()
```

---

## 测试建议

### 问题1测试
1. 使用 text_editor 查看多个文件
2. 观察草稿是否在查看文件过程中过早切换
3. 确认 "Viewing file xxx.py" 等状态信息完整显示在一个草稿中

---

## 兼容性说明

这个修复是针对现有行为的缺陷修正，不会破坏任何现有功能：

1. 问题1的修复只是调整了草稿切换的时机，使其更符合设计意图

## 版本信息

- 修复日期：2026-09-06
- 基于版本：项目 my.zip 中的代码

---

# 修复说明（第三轮）：失败重试内容叠加 + gpt image 2 失败后参考图丢失

修复日期：2026-09-06

## 问题 1：请求失败后重发消息，文本/图片被反复合并、越积越多

### 现象
一轮请求失败（网关错误 / 媒体接口报错 / 空响应等）后，用户重发消息（哪怕原样
重发同一条图片消息），新消息会被**合并**进上一条未获回应的 user 消息：文本以
空行拼接、媒体附件合并。反复重试后，同一段 caption 出现 N 次、同一张参考图以
image[] 形式重复上传 N 次，请求越积越大。

### 根本原因
`turn_recovery.persist_user_message_entry` 对"历史末尾是未获回应 user 消息"
的情况一律走 `_merge_user_message` 合并。该合并是为"快速连发打断"设计的
（上一轮还在跑、尚无任何输出），但**请求失败**的轮次同样会让 user 消息留在
历史末尾——两种状态无法区分，失败重试被误当成打断合并。

### 修复方案
1. 新增 `TURN_FAILED_FLAG` 标记与 `turn_recovery.mark_failed_unanswered_user(chat_id)`：
   轮次以失败告终（历史末尾仍是本轮 user 消息、无任何 assistant 输出）时打标。
2. `get_ai_response` 的全部失败路径调用打标：顶层异常（salvage 之后，若末尾
   仍是 user）、`IMAGE_ERROR`、`VIDEO_ERROR`、空响应、`IMAGE_SENT` 且内容带
   ⚠️/❌ 前缀（安全拒绝，历史同样不会写入）。
3. `persist_user_message_entry` 读到末尾 user 消息带失败标记时改为**整体替换**
   （`_replace_failed_user_message`）：
   - 新消息的文本与媒体完全接管该历史槽位，不与旧内容拼接——每次重试后请求
     里每段文本、每张图片恰好一份；
   - 新消息**不带媒体**而失败轮带了（用户只回"再试一次"）时，把失败轮的媒体
     搬移一份过来（不搬旧文本）——用户上传的图片不因一次失败从历史消失；
   - 打断合并链（无失败标记）行为完全不变。

### 修改文件
- `src/turn_recovery.py`：新增标记 / `mark_failed_unanswered_user` /
  `_replace_failed_user_message` / `_apply_attachment_entries`，替换分支接入
  `persist_user_message_entry`
- `src/ai_handlers.py`：5 个失败返回路径打标

## 问题 2：gpt image 2 原生图片模型失败后重试，只发文本、参考图丢失

### 现象
gpt-image-2（Images 协议原生图像模型）带参考图生成失败后，用户发纯文本
（"再试一次"）重试，此时模型收到的是**纯文本 prompt**，参考图没有发给 AI，
请求退化为文生图。

### 根本原因
`_agentic_loop_native_image` 的 prompt 与参考图只从**最后一条 user 消息**
提取。图像轮以"模型仅返回文本"的方式失败时（网关 200 + 纯文本回复、非 ⚠️
前缀的提示文案），该提示会作为普通 assistant 消息写入历史；随后的纯文本重试
追加在后成为最后一条 user 消息——提取不到任何参考图。

### 修复方案
提取逻辑抽出为模块级 `_extract_image_prompt_and_reference_urls` 并增加
**参考图回溯**：最后一条 user 消息不带图时，向前找最近一条带图的 user 消息
沿用其参考图（prompt 仍取最后一条 user 消息，保证是本轮最新指令）。自带图时
行为不变；从未有过图片时回溯结果为空（纯文生图不受影响）。

### 修改文件
- `src/ai/agentic_loops.py`：新增
  `_extract_native_image_urls_from_user_message` /
  `_extract_image_prompt_and_reference_urls`，原嵌套函数移除

## 验证
- `scripts/verify_fixes.py`（开发验证脚本）：36 项断言覆盖失败替换 / 媒体搬运
  形态归一 / 打断合并链回归 / 成功轮追加 / 参考图回溯，全部通过；
- 既有 `tests/test_whitelist_r2.py` 回归：88 通过，0 失败。
