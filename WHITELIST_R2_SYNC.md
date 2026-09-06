# 白名单 R2 持久化 + 管理员权限边界修复

## 修改日期
2026-09-06

## 需求背景

1. **白名单不再依赖每轮手填**：白名单此前只存在本地文件（`whitelist.txt`），
   Render 等平台的部署环境磁盘是临时的——每次重新部署白名单就丢，管理员
   只能每个用户重新 `/adduser`。
2. **R2 作为持久层**：项目已接入 Cloudflare R2（S3 兼容 API）。R2 的
   `PutObject` 允许对同 key 覆盖写（即"编辑"该文件），因此：
   - **每次修改后**（`/adduser`、`/deluser`）把全量白名单推送到 R2；
   - **启动/部署时**从 R2 拉取全量白名单批量加载（管理员在 R2 控制台
     手动编辑过的名单也在此时生效）。
3. **权限边界修复**：这是**用户白名单**，不是管理员名单——
   - 管理员不能被 `/adduser` 加进白名单；
   - 管理员不能被 `/deluser` 删除（显式拒绝，而非提示"不存在"）。

---

## 存储模型：R2 权威 + 本地缓存

```
/adduser、/deluser（每次修改，全程持 _whitelist_lock）
    改内存 set → 原子写本地缓存文件 → 全量推送 R2（PutObject 覆盖写）

启动 / 部署（load_whitelist）
    R2 已配置 ── download 成功 ──> 以 R2 内容为准，回写本地缓存
              └ download 失败/异常 ──> 回退本地缓存文件（绝不清空）
    R2 未配置 ──> 直接用本地文件（whitelist.txt 即数据源）
    R2 有配置但对象不存在（首次部署/换 key）
              └ 本地有数据 ──> 把本地数据播种推上 R2（完成迁移）
```

- **R2 对象 key**：默认 `config/whitelist.txt`，环境变量
  `APITELEGRAMCHAT_WHITELIST_R2_KEY` 可覆盖（含 `..` 段会被拒绝并回退
  默认值，防路径穿越到本地缓存映射）。
- **全量推送**：每次推送的都是完整名单（排序后一行一个），不是增量。
  这样任何一次成功推送都会把之前失败的变更一并补齐（自愈）。
- **推送顺序保证**：推送在 `_whitelist_lock` 内同步完成，与加载互斥，
  R2 收到的版本顺序与操作顺序一致，不会旧盖新。
- **推送失败可见**：`add/remove` 返回 `*_sync_failed` 状态，Telegram
  回复会提示"⚠️ 推送到 R2 失败"，本地已写入，修复后任意一次成功
  推送自动补齐。

### 一致性语义（重要）

- **R2 是权威**。启动时 R2 内容直接覆盖内存与本地缓存：
  本地有、R2 没有的条目会被丢弃（localonly 场景）；反之 R2 有、
  本地没有的会加载进来。被 `/deluser` 删除的用户**不会在重启后复活**。
- 本地文件只保证"R2 故障时权限判断不中断"，不是第二数据源。

---

## 权限模型修复

### 1. 管理员与用户白名单严格分离

- `ADMIN_USERS`（`config.py`）是管理员名单；授权时
  `is_authorized = is_admin_identity || is_whitelisted_identity`，
  管理员**永远授权且与白名单无关**。
- `/adduser dearella`（含 `@DEARELLA`、`Dearella` 等任意大小写）→ 拒绝，
  回复"❌ 管理员不能加入用户白名单"。
- `/deluser dearella` → 拒绝（显式 `admin` 状态，不是"用户不存在"），
  回复"❌ 不能从用户白名单删除管理员"。
- **防御性过滤**：即使有人手改 R2 / 本地文件把管理员塞进白名单，
  `_parse_whitelist_bytes` 加载时也会剔除，杜绝绕过。

### 2. 用户名大小写语义修正（隐藏 bug 修复）

Telegram 用户名大小写不敏感（`@Alice` 与 `alice` 是同一账号），但旧版
本用**区分大小写**的原始字符串做存储与匹配：

- `/adduser @Alice` 后，实际用户名为 `alice` 的用户依然"未授权"；
- 管理员判断同理。

现在统一归一化（`_normalize_target`）：去空白、去 `@` 前缀、用户名转
小写；纯数字 user_id 按精确字符串比较。存储与匹配同源归一化，增删查
三个方向不再出现大小写漂移。

### 3. first_name 误判修复（pre-existing 漏洞）

`ctx["username"]` 在用户没有 Telegram 用户名时会回退成 `first_name`
（不唯一！），而 TIMER 主动消息的授权复检 `_is_chat_authorized` 曾直接
拿它匹配白名单——同名陌生人可能被误判为授权。现在上下文新增
`tg_username` 字段（只存真实用户名，可能为空），授权路径只用它；
`ctx["username"]` 的回退语义仅保留给展示用途。

### 4. 其他加固

- 本地白名单文件改为**临时文件 + `os.replace` 原子写**，进程崩溃不会
  留下残缺名单。
- `/adduser`、`/deluser` 回复对目标做 `html.escape`，杜绝 admin 输入
  被当作 HTML 注入到 Telegram 消息。
- R2 key 环境变量含 `..` 时拒绝并回退默认值（本地缓存镜像会把 key
  映射成磁盘路径，防穿越）。
- 解析时丢弃含空白字符的非法条目；容忍 BOM / CRLF。

---

## 改动文件

| 文件 | 改动 |
|------|------|
| `src/apitelegramchat/config.py` | 白名单管理重写：R2 拉取/推送/播种、状态常量、归一化、管理员过滤、原子写 |
| `src/apitelegramchat/app.py` | `/adduser` `/deluser` 状态映射与新回复文案、`html.escape`、授权函数委托 config、`_is_chat_authorized` 改用 `tg_username`、启动加载注释 |
| `src/apitelegramchat/state.py` | 上下文默认值新增 `tg_username` |
| `tests/test_whitelist_r2.py` | 新增：88 项回归测试（无需 pytest，`python tests/test_whitelist_r2.py` 直接运行） |

## 接口变化

`add_whitelist_user / remove_whitelist_user` 返回值从 `bool` 改为状态
字符串（唯一调用方 app.py 已同步更新）：

```text
add:    ADD_ADDED / ADD_EXISTS / ADD_ADMIN_REJECTED / ADD_SYNC_FAILED
remove: REMOVE_REMOVED / REMOVE_MISSING / REMOVE_ADMIN_REJECTED / REMOVE_SYNC_FAILED
```

新增可复用判断函数：`config.is_admin_identity(username, user_id)`、
`config.is_whitelisted_identity(username, user_id)`。

---

## 测试覆盖（tests/test_whitelist_r2.py，88 项全过）

- **A 归一化/管理员判断**：@ 前缀、大小写、数字 ID、非管理员不误伤
- **B 文件解析**：BOM、CRLF、空行、管理员条目过滤、非法条目、坏编码
- **C 本地模式**：文件即数据源、增删落盘、重启恢复、并发一致性
- **D R2 模式**（fake 后端）：
  - 启动拉取 + 管理员过滤 + 本地缓存回写
  - 修改即全量推送；管理员操作被拒时**零推送**
  - 重启无复活（删除在 R2 权威下生效）
  - R2 优先于本地残留数据
  - 播种迁移（首次部署）；双空不播种
  - 下载网络故障回退本地不清空；对象存在但不可读时不播种（防旧盖新）
  - 推送失败 → `*_sync_failed`；恢复后全量推送自动补齐（自愈）
  - 并发增删 / load 与 add 交叉后 内存 == 本地文件 == R2 三方一致
- **E 授权边界**：管理员授权不依赖白名单、手改 R2 注入管理员被过滤、
  大小写授权、内存 set 引用恒定（历史 from-import bug 回归）、tmp 不残留
- **F R2 key 防护**：穿越/空值/空白/前导斜杠（子进程验证）
- **G 真实 s3_utils 本地回退**：r2_cache 镜像写入与读回（不打补丁）

## 运维速查

- **批量加白名单**：直接在 R2 控制台编辑 `config/whitelist.txt`
  （一行一个用户名或数字 ID），等下一次重启/部署自动批量生效；
  或运行期间继续用 `/adduser`。
- **R2 环境变量**：沿用现有 `R2_ENDPOINT / R2_ACCESS_KEY /
  R2_SECRET_KEY / R2_BUCKET_NAME`，白名单无需额外凭据。
- **查看当前名单**：`/listusers`（管理员命令）。
