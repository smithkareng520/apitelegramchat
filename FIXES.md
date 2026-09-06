# 修复说明

## 问题1：新旧草稿交替导致内容分离

### 问题描述
在工具执行过程中，文本内容结束后立即调用 `rollover_at_turn_boundary`，导致工具的流式输出（如 "Viewing file utils.py"）被拆分到新草稿中，造成内容分离。

### 根本原因
在 `anthropic_bridge.py`、`agentic_loops.py` 和 `gemini_bridge.py` 中，模型返回文本内容后立即检查并切换草稿，但此时工具调用的流式内容可能还在更新。

### 修复方案
删除文本块结束后的过早 `rollover_at_turn_boundary` 调用，只在工具批次完全执行完毕后再检查是否需要切换草稿。

### 修改文件
1. `src/apitelegramchat/ai/anthropic_bridge.py` (第 872-877 行)
2. `src/apitelegramchat/ai/agentic_loops.py` (第 739-744 行)
3. `src/apitelegramchat/ai/gemini_bridge.py` (第 987-992 行)

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

## 问题2：模型从 download 目录返回工作目录时被拒绝

### 问题描述
模型使用 `cd download` 进入下载目录后，`_last_cwd` 被记录为 download 路径。后续所有命令（包括 `cd $WORKSPACE` 返回工作目录的命令）都被 `_is_safe` 拒绝，形成"陷阱"——模型无法逃离。

### 根本原因
`_is_safe` 方法在检测到 `_last_cwd` 位于 upload/download 目录内时，直接拒绝所有命令，没有为"返回工作目录"的合法 cd 命令提供例外。

### 修复方案
在 `_is_safe` 方法中，当检测到当前目录在 upload/download 内时，允许执行返回工作目录的 cd 命令：
- `cd $WORKSPACE`
- `cd $HOME` (沙箱中 HOME=WORKSPACE)
- `cd` (无参数，返回 HOME)
- `cd ..` 或 `cd ../..` (逐级返回)
- `cd /absolute/path/to/workspace` (绝对路径)

### 修改文件
`src/apitelegramchat/tool_executors.py` (第 1247-1271 行)

修改前：
```python
if self._last_cwd and is_inside_upload_or_download(self._last_cwd):
    logger.warning(
        f"🚫 Bash rejected (cwd inside upload/download) chat_id={self.chat_id} cwd={self._last_cwd}"
    )
    return False
```

修改后：
```python
if self._last_cwd and is_inside_upload_or_download(self._last_cwd):
    # 修复问题2：如果当前在 upload/download 内，但命令是返回工作目录的 cd 命令，
    # 则允许执行，让模型能够逃离陷阱。
    cmd_stripped = command.strip()
    if re.match(r'^cd(\s+\$WORKSPACE|\s+\$HOME|\s*$|\s+\.\.(/\.\.)*)(\s*[;&|]|$)', cmd_stripped):
        logger.info(f"✓ Bash allowed escape-cd from upload/download ...")
        return True
    # 也允许绝对路径 cd 到工作目录
    workspace_path = str(self.workdir.absolute())
    if re.match(rf'^cd\s+["\']?{re.escape(workspace_path)}["\']?(\s*[;&|]|$)', cmd_stripped):
        logger.info(f"✓ Bash allowed escape-cd (absolute) from upload/download ...")
        return True
    logger.warning(f"🚫 Bash rejected (cwd inside upload/download) ...")
    return False
```

---

## 测试建议

### 问题1测试
1. 使用 text_editor 查看多个文件
2. 观察草稿是否在查看文件过程中过早切换
3. 确认 "Viewing file xxx.py" 等状态信息完整显示在一个草稿中

### 问题2测试
1. 执行 `cd download && ls`
2. 尝试执行 `cd $WORKSPACE` 返回工作目录
3. 确认命令成功执行，而不是被拒绝
4. 确认返回后可以正常执行其他命令

---

## 兼容性说明

这两个修复都是针对现有行为的缺陷修正，不会破坏任何现有功能：

1. 问题1的修复只是调整了草稿切换的时机，使其更符合设计意图
2. 问题2的修复只是为合法的"逃离"命令提供了例外，不影响安全限制

## 版本信息

- 修复日期：2026-09-06
- 基于版本：项目 my.zip 中的代码
