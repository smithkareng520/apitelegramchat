# 白名单用户添加失败问题修复

## 修复日期
2026-09-06

## 问题分析

### 核心 Bug：Python 模块变量引用失效

**问题描述：**
用户通过 `/adduser` 命令添加白名单后，显示"添加成功"，但实际上该用户仍无法使用机器人，权限检查一直失败。

**根本原因：**
这是一个经典的 Python 模块变量引用 bug，由以下两个因素共同导致：

1. **`config.py` 中的 `load_whitelist()` 使用 `global` 重新赋值：**
   ```python
   async def load_whitelist():
       global WHITELIST_USERS
       WHITELIST_USERS = {line.strip() for line in f if line.strip()}  # 重新绑定！
   ```

2. **`app.py` 中使用 `from config import WHITELIST_USERS` 导入：**
   ```python
   from apitelegramchat.config import WHITELIST_USERS
   ```

**执行流程：**
1. 模块加载时：`config.WHITELIST_USERS = set()`（空集合），`app.WHITELIST_USERS` 引用这个空集合对象
2. 启动时调用 `load_whitelist()`：`config.WHITELIST_USERS = {...}`（新集合对象）
3. **问题出现**：`app.WHITELIST_USERS` 仍然指向步骤 1 的空集合，看不到新对象
4. 调用 `add_whitelist_user()`：在新集合对象上执行 `.add()`
5. **权限检查失败**：`app.py` 中的 `is_authorized()` 检查的是旧的空集合

**验证代码：**
```python
# 启动前 app 看到: set()
cfg.load()
# load 后 config 看到: {'alice', 'bob'}
# load 后 app   看到: set()  <-- 仍是旧的空 set！
cfg.add("charlie")
# add 后 app   看到: set()  <-- 永远看不到新用户！
```

---

## 修复方案

### 修复 1：使用原地更新代替重新赋值

**文件：** `src/apitelegramchat/config.py`

**修改位置：** `load_whitelist()` 函数

**修改前：**
```python
async def load_whitelist():
    global WHITELIST_USERS
    async with _whitelist_lock:
        try:
            with open(_resolve_whitelist_path(), "r", encoding="utf-8") as f:
                WHITELIST_USERS = {line.strip() for line in f if line.strip()}
        except FileNotFoundError:
            WHITELIST_USERS = set()
```

**修改后：**
```python
async def load_whitelist():
    """从文件加载白名单，使用原地更新而非重新赋值，避免 from-import 引用失效。"""
    async with _whitelist_lock:
        try:
            with open(_resolve_whitelist_path(), "r", encoding="utf-8") as f:
                loaded = {line.strip() for line in f if line.strip()}
            # 关键修复：原地更新 set，而不是 global 重新赋值。
            # app.py 中的 "from config import WHITELIST_USERS" 引用的是启动时的 set 对象，
            # 如果这里用 "WHITELIST_USERS = loaded" 重新绑定，app.py 仍会看到旧的空 set。
            WHITELIST_USERS.clear()
            WHITELIST_USERS.update(loaded)
        except FileNotFoundError:
            # 文件不存在时清空集合（初始化为空白名单）
            WHITELIST_USERS.clear()
```

**关键变化：**
- 删除 `global WHITELIST_USERS` 声明（不再需要）
- 使用 `WHITELIST_USERS.clear()` + `WHITELIST_USERS.update(loaded)` 原地修改集合
- 保持 `WHITELIST_USERS` 变量始终指向同一个 set 对象

---

### 修复 2：确保白名单文件父目录存在

**文件：** `src/apitelegramchat/config.py`

**修改位置：** `_save_whitelist_unlocked()` 函数

**问题：** 当 `/tmp/apitelegramchat_data` 目录不存在时，直接写文件会抛出 `FileNotFoundError`。

**修改前：**
```python
def _save_whitelist_unlocked() -> None:
    try:
        with open(_resolve_whitelist_path(), "w", encoding="utf-8") as f:
            f.writelines(user + "\n" for user in sorted(WHITELIST_USERS))
```

**修改后：**
```python
def _save_whitelist_unlocked() -> None:
    """在已持有 _whitelist_lock 的前提下把 WHITELIST_USERS 写入磁盘。"""
    try:
        path = _resolve_whitelist_path()
        # 确保父目录存在
        import os
        from pathlib import Path
        parent = Path(path).parent
        parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(user + "\n" for user in sorted(WHITELIST_USERS))
```

---

### 修复 3：改进 `_resolve_whitelist_path()` 的回退逻辑

**文件：** `src/apitelegramchat/config.py`

**修改位置：** `_resolve_whitelist_path()` 函数

**问题：** 原代码在异常时直接返回 `WHITELIST_FILE`（相对路径），可能导致写入当前工作目录而非数据目录。

**修改前：**
```python
def _resolve_whitelist_path() -> str:
    if os.path.isabs(WHITELIST_FILE):
        return WHITELIST_FILE
    try:
        from apitelegramchat.workspace_paths import data_root
        return str(data_root() / WHITELIST_FILE)
    except Exception:
        logger.debug("_resolve_whitelist_path 内部忽略的异常", exc_info=True)
        return WHITELIST_FILE  # 可能是相对路径！
```

**修改后：**
```python
def _resolve_whitelist_path() -> str:
    """返回白名单文件路径，优先使用绝对路径，否则挂到 data_root 下。"""
    if os.path.isabs(WHITELIST_FILE):
        return WHITELIST_FILE
    try:
        from apitelegramchat.workspace_paths import data_root
        return str(data_root() / WHITELIST_FILE)
    except Exception:
        logger.warning("_resolve_whitelist_path 失败，使用回退路径", exc_info=True)
        # 回退到环境变量指定的数据目录
        data_dir = os.getenv("APITELEGRAMCHAT_DATA_DIR", "/tmp/apitelegramchat_data")
        return os.path.join(data_dir, WHITELIST_FILE)
```

---

## 测试验证

### 测试场景 1：添加用户
1. 管理员执行 `/adduser test_user`
2. 系统显示"✅ 添加成功"
3. `test_user` 发送任意消息
4. **预期结果：** 机器人正常响应（而不是"未授权访问"）

### 测试场景 2：重启后白名单保持
1. 添加用户 `user_a`
2. 重启应用
3. `user_a` 发送消息
4. **预期结果：** 仍然可以正常使用

### 测试场景 3：删除用户
1. 执行 `/deluser test_user`
2. 系统显示"✅ 移除成功"
3. `test_user` 发送消息
4. **预期结果：** 收到"未授权访问"提示

### 测试场景 4：列出白名单
1. 执行 `/listusers`
2. **预期结果：** 显示所有已添加的用户

---

## 技术细节

### Python 变量引用机制

Python 中的 `from module import name` 导入的是**对象引用**，而非动态绑定：

```python
# config.py
VAR = [1, 2, 3]

# app.py
from config import VAR  # VAR 现在指向 [1, 2, 3] 这个对象

# 如果在 config.py 中执行：
VAR = [4, 5, 6]  # 创建新对象并重新绑定

# app.py 中的 VAR 仍然指向 [1, 2, 3]，看不到新值！

# 正确做法：
VAR.clear()
VAR.extend([4, 5, 6])  # 原地修改，app.py 能看到
```

### 为什么不改用 `import config` 然后访问 `config.WHITELIST_USERS`？

虽然这样可以解决问题，但会：
1. 改动量大（需要修改 `app.py` 的所有引用点）
2. 代码冗长（`config.WHITELIST_USERS` vs `WHITELIST_USERS`）
3. 当前修复方案更优雅，保持了代码原有结构

---

## 兼容性说明

- **不影响现有功能：** 修复只改变内部实现，对外接口完全不变
- **向后兼容：** 现有的白名单文件格式和位置保持不变
- **无性能影响：** 原地更新的性能与重新赋值相当

---

## 总结

这次修复解决了三个问题：
1. **核心 Bug（变量引用失效）：** 使用原地更新代替重新赋值
2. **文件系统问题：** 确保父目录存在再写入
3. **路径回退问题：** 异常时回退到正确的绝对路径

修复后，`/adduser` 命令可以正常工作，用户添加后立即生效，重启后白名单也能正确加载。
