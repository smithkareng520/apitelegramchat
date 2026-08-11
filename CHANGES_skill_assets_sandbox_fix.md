# 修复：skill 资源在 Landlock 沙箱中不可见

## 根因

`skill_catalog` / `skill_read` / `skill_activate` 这几个工具本身没问题，模型确实能
"读完" `SKILL.md` 的正文。问题在于 **SKILL.md 正文里引用的配套资源
（`scripts/`、`REFERENCE.md`、`FORMS.md`、模板/schema 文件等）从未被搬进沙箱能访问
的路径**：

1. `sandbox.py` 的 `_apply_landlock()` 只对两类路径放行：
   - `workspace_root(chat_id)`（`/tmp/apitelegramchat_data/workspaces/<ns>`，读写）
   - `/usr /bin /sbin /lib /lib64 /etc /dev /proc /sys`（只读+可执行，为了让 bash 能跑）

   `.claude/skills/<id>/` 所在的应用源码树完全不在白名单里，Landlock 直接拒绝访问。

2. `text_editor` 工具（`execute_text_editor`）的所有路径也被限定在
   `workspace_root(chat_id)` 内，同样够不到 `.claude/skills/...`。

3. `skills.py` 的 `read_skill()` / `activate_skill()` 只把 SKILL.md **正文文本**注入
   对话上下文，从未把该目录下其余文件复制到 workspace。

结果：模型即使"知道"该运行 `scripts/office/unpack.py`，实际执行时要么被 Landlock
拒绝，要么因为路径在沙箱里根本不存在而报 `No such file or directory`。

## 修复方式

激活 skill 时，把该 skill 目录下除 `SKILL.md` 之外的全部资源，物理复制一份到
该 chat 的 `workspace_root(chat_id)/.skills/<skill_id>/` 下。这样资源天然落在
Landlock 已放行的目录树内，bash / text_editor 都能直接访问。

### 改动的文件

- **`src/apitelegramchat/skills.py`**
  - 新增 `sync_skill_assets_to_workspace(skill_id, workspace_root)`：增量复制
    skill 目录下除 `SKILL.md` 外的所有文件到 `<workspace>/.skills/<skill_id>/`，
    并清理源端已删除的陈旧文件。用 mtime/size 判断是否需要重写，避免每轮对话
    重复拷贝大文件（例如 docx skill 里的 XSD schema 集合）。
  - 新增 `skill_assets_workspace_relpath(skill_id)`：返回资源在 workspace 内的
    相对路径，供系统提示引用。
  - `build_skill_system_message()` 现在会在注入模型的正文末尾附加一段路径映射
    说明，明确告诉模型：SKILL.md 里写的 `scripts/xxx.py`、`REFERENCE.md` 等相对
    路径，实际应该在 `.skills/<skill_id>/` 下访问。
  - 顺手修了 `load_skill_records()` 的一个小 bug：当多个 skill root 里出现同名
    子目录时，之前会重复收录，现在只保留优先级最高（先发现）的一份。

- **`src/apitelegramchat/workspace_utils.py`**
  - `_sync_workspace_from_r2` / `_sync_workspace_to_r2`（R2 全量同步）现在会跳过
    workspace 根目录下的 `.skills/` 子目录：既不上传到 R2，也不会因为"远程没有"
    就被清理逻辑误删。这个子目录由 `sync_skill_assets_to_workspace()` 独立管理
    生命周期，不属于用户持久化数据。

- **`src/apitelegramchat/ai_handlers.py`**
  - 在自动匹配/沿用 active skill、确定 `skill_to_use` 之后，调用
    `sync_skill_assets_to_workspace()`（用 `asyncio.to_thread` 包装，避免阻塞
    事件循环）把资源同步到当前 chat 的 workspace。

- **`src/apitelegramchat/tool_executors.py`**
  - `skill_activate` 工具分支（模型主动调用 `skill_activate` 时）同样触发资源
    同步，保证无论是自动匹配还是模型手动激活，资源都会被同步。

### 未改动但确认过没问题的部分

- `sandbox.py` 的 Landlock 规则不需要改：`.skills/` 已经落在 `workspace_root`
  内部，原有规则已经覆盖。
- `BashSession.start()` 里的 `os.chmod(self.workspace, 0o700)` 只作用于 workspace
  顶层目录本身，不影响子目录/文件的可读性；`shutil.copy2` 复制出来的文件权限
  正常可读。

## 效果

以 docx skill 为例，激活后模型看到的系统提示会包含：

```
Skill assets path (in this workspace): .skills/docx/
...
IMPORTANT — resolving paths above: ... `scripts/office/unpack.py` is actually at
`.skills/docx/scripts/office/unpack.py` — use that full path when running bash
commands or reading files with the editor tool.
```

模型执行 `python .skills/docx/scripts/office/unpack.py document.docx unpacked/`
时，该路径已经在 Landlock 允许的 workspace 树内，可以正常执行。

## 验证

已用独立脚本验证 `sync_skill_assets_to_workspace()` 的核心行为（发现 skill、
复制 scripts/ 与 REFERENCE.md、增量更新、清理陈旧文件、系统提示路径映射），
以及去重逻辑，测试均通过。四个改动文件均通过 `python3 -m py_compile` 语法检查。

由于沙箱依赖 Linux 5.13+ Landlock（本开发环境不支持 Landlock 语法），未做端到端
的实际沙箱内执行验证，建议在目标部署环境（Render/Heroku 等已验证支持 Landlock
的 Linux 内核）中做一次真实的 skill 激活 + bash 调用 `.skills/<id>/scripts/...`
的冒烟测试。

---

# 第二轮修复（根据实际运行日志）：超时太短 + 路径仍需模型手动映射

第一轮修复解决了"资源根本不在沙箱可见范围内"的问题，但实际跑起来后日志暴露了
两个新问题：

## 问题 1：bash / text_editor 12 秒就被外层杀掉

日志里反复出现：

```
[tool] bash timed out after 12s ...
[tool] text_editor timed out after 12s ...
检测到工具连续相同错误熔断: "... timed out ..." x3
```

**根因**：`ai_handlers.py` 里包一层 `asyncio.wait_for(dispatch_tool_call(...), timeout=...)`，
`bash`/`text_editor` 都落在默认的 `TOOL_CALL_TIMEOUT = 12`（秒）这一档。而：
- Landlock 沙箱首次启动要 fork+exec+装规则；
- skill 工作流的典型命令（`pip install`、LibreOffice `soffice --headless --convert-to`、
  `pandoc`）冷启动经常就要 10~30 秒以上；
- `text_editor` 每次调用前还会先做一次 R2 全量同步（网络 IO）。

12 秒对这些操作来说太短，外层 `wait_for` 会在沙箱/命令还在正常执行时就抢先杀掉它，
连续 3 次相同超时还会触发"工具连续相同错误熔断"，模型永远看不到真实的命令输出，
只能靠猜测应付用户。

**修复**（`src/apitelegramchat/ai_handlers.py`）：
- `text_editor` 归入 `LONG_RUNNING_TOOLS`（和 `web_search`/`fetch_url` 同档，**45 秒**）。
- `bash` 单独设一档 `BASH_TOOLS`，超时提到 **130 秒**——刻意略高于沙箱内部自身的
  `SANDBOX_TIMEOUT_SEC`（默认 120 秒，见 `sandbox.py`），这样任何时候都是**内层沙箱
  自己先超时收尾**（会打印明确的 "Command timed out after Ns" 并重启会话），外层
  `wait_for` 只是最后兜底，不会出现"命令其实快跑完了，却被外层抢先杀掉"的情况。
- `run_one()` 里的超时选择逻辑加了 `elif fn_name in BASH_TOOLS: timeout = BASH_TOOL_CALL_TIMEOUT`
  分支，放在 `SUBAGENT_TOOLS` 之后、`LONG_RUNNING_TOOLS` 之前。

## 问题 2：模型仍然拼错路径 / 不确定该不该加 `.skills/docx/` 前缀

日志里模型的回复：

> bash 工具在这里好像遇到问题，无法直接查看脚本文件。不过，我可以告诉你技能文档中
> 提到的脚本都是放在 `.skills/docx/` 目录下...

说明第一轮修复只做了"文本提示模型自己在 SKILL.md 相对路径前面拼 `.skills/docx/`"，
这个纯靠模型自己字符串拼接的方案不够可靠——容易漏拼、错拼，而且不符合 Claude 官方
skill 渐进式披露的设计：**SKILL.md 里的相对路径，本来就应该假设"你已经站在这个
skill 包目录里"**，例如 docx 的 SKILL.md 直接写：

```bash
python scripts/office/soffice.py --headless --convert-to pdf document.docx
```

这条命令的 `scripts/office/soffice.py` 是相对 docx skill 包自身的路径，不应该要求
模型再去做任何路径改写。

**修复**（`src/apitelegramchat/tool_executors.py` + `src/apitelegramchat/skills.py`）：

1. `BashSession` 新增 `set_active_skill(skill_id)` / `_effective_cwd()`：
   - 有 active skill 且它的资源已经同步到 `workspace/.skills/<skill_id>/`（第一轮
     修复做的事）时，`execute()` 每次执行命令前会 **自动 `cd` 到该 skill 目录本身**，
     而不是像以前那样永远 `cd` 到 workspace 根。
   - 目录还不存在（同步失败等异常情况）时安全降级回 workspace 根，不会 `cd` 到
     不存在的路径导致命令直接报错。
   - `execute()` 返回结果里新增一行 `Cwd: <绝对路径>`，让模型随时知道自己当前
     实际所在目录。

2. `active skill` 状态在两条路径上都会驱动 cwd 切换：
   - 自动匹配（`ai_handlers.py` 确定 `skill_to_use` 后）→ `execute_bash(..., skill_id=...)`。
   - 模型手动调用 `skill_activate` 工具（`tool_executors.py`）→ 同样写入
     `state.user_contexts[chat_id]["active_skill"]`，并且用**规范化后的真实
     `skill_id`**（而不是用户可能传入的 `name` 别名）保证和 `.skills/<id>/` 目录名
     完全对上。
   - `dispatch_tool_call` 的 `bash` 分支会在每次调用时读取当前 `active_skill`，
     传给 `execute_bash`，保证 session 跨轮复用时 cwd 始终反映最新的 skill 状态。

3. `text_editor` 的路径解析**没有改**（依然是相对 workspace 根，这是它一直以来的
   语义，改路径解析风险面更大）。转而把系统提示（`build_skill_system_message()`）
   写得更明确、两套规则分开说：
   - **bash**：cwd 已经自动切到 skill 目录，SKILL.md 原文的相对路径直接抄用，
     不需要任何前缀，也不要自己再 `cd`。需要访问 workspace 根的用户文件时用
     `../` 或参考 `Cwd:` 那一行拼绝对路径。
   - **text_editor**：路径永远相对 workspace 根，访问 skill 资源必须写完整的
     `.skills/<id>/scripts/...`。

## 效果对比

第一轮修复后（问题仍存在）：
```
python scripts/office/unpack.py document.docx unpacked/
→ No such file or directory（cwd 被强制 cd 到 workspace 根，模型不确定要不要加前缀）
```

第二轮修复后：
```
# 有 docx skill 激活时，bash 执行的 cwd 已经是 workspace/.skills/docx/
python scripts/office/unpack.py document.docx unpacked/
→ 直接可用，和 SKILL.md 原文一字不差
```

同时 12s → 130s（bash）/ 45s（text_editor）的超时调整，让 LibreOffice 转换、
pip 安装等耗时命令不会被外层过早杀掉、触发熔断。

## 第二轮验证

- 改动文件（`skills.py` / `workspace_utils.py` / `tool_executors.py` / `ai_handlers.py`）
  全部通过 `python3 -m py_compile`。
- `_effective_cwd()` 的路径切换逻辑（无 active skill / active skill 但资源未同步 /
  资源已同步 / 清除 active skill 四种场景）用等价代码脱离项目重依赖单独验证，
  与实际写入文件的实现逐字核对一致，全部通过。
- 超时常量、`run_one()` 里的分支选择逻辑经人工核对，`BASH_TOOLS` 判断分支顺序正确
  （在 `SUBAGENT_TOOLS` 之后、`LONG_RUNNING_TOOLS` 之前，不会被更早的分支截胡）。
- 同样因为本地开发环境不支持 Landlock 语法，未做端到端的真实沙箱冒烟测试，建议
  部署后用一次真实的 "激活 docx skill → 上传/生成一个 .docx → 让模型跑
  `pandoc`/`soffice` 转换" 流程验证 cwd 切换和超时是否符合预期。

