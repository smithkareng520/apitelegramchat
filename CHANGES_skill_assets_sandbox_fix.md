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
