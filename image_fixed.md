# 「两张照片只收到一张」根因分析与修复说明

> 事故现场：2026-09-12 15:48 生产日志 `[6c15a092]`
> `统一管道预检: input=[text=35字符+photo×2]`，模型回复只描述了 1 张图。
> 此前补丁（`_build_image_block` 失败告警 + 相册回复整组补齐）均未命中本例根因。

---

## 一、结论（TL;DR）

本轮日志里**根本没有两张图同时到达模型管道**。两个互相叠加的缺陷造成了
"两张只拿了其中一张"的最终表现：

| # | 缺陷 | 性质 | 后果 |
|---|------|------|------|
| 1 | **预检输入组合双重计数**：`resolve_input_combination` 对信封里的 `file_ids` 数组和 `attachments` 列表各数一遍，1 张照片被计成 `photo×2` | 日志失真 | 把排查方向带偏——日志"证明"两张图都进了管道，于是前几轮补丁都在修下游解析，而真正丢失发生在**管道上游** |
| 2 | **回合派发时消息尚未落库**：user 消息的持久化发生在回合任务内部的 `get_ai_response`（派发后 1~2 秒），而 `spawn_turn_task` 派发新回合时会先**打断并取消**旧回合任务。快速连发两条消息时，第一条的消息体在落库前就被取消 → **静默消失**（无历史、无日志） | 真实丢消息 | 第二条消息单独成回合，模型只见最后一张图；`turn_recovery` 精心设计的"连发合并"机制因持久化太晚而完全失效 |

### 用日志时间线还原现场

```
15:48:47.9  [cb64c79f] 照片A到达（单发，非相册）→ 派发回合任务 TA
            （0.534s = 打断上一条消息正在生成的回合 + 保全）
15:48:48~55 TA 运行中：媒体卡片消费检查 / pre_flight / 草稿首帧……
            ——尚未执行到 get_ai_response 内的持久化步骤
15:48:55.08 [6c15a092] 照片B到达（update 439417396）
            spawn_turn_task(B) → _interrupt_active_generation
            → 取消 TA（其 user 消息 A 尚未入库，无任何日志）✗ 丢失
15:48:55.08 B 的回合派发（0.000s）；持久化 B 时历史末尾不是
            未回应 user 消息（A 从未入库）→ B 独立追加
15:48:56.4  B 的预检：photo×2 ← 双重计数把 B 这 1 张图数成 2 ✗ 日志说谎
15:49:40    模型回复只描述 B（枫叶少女）——"两张只拿了其中的一张"
```

佐证：
- 预检 `photo×2` 出现在 B 到达后 **1.3s**，而相册聚合窗口是 5s —— 排除
  "相册聚合漏收分片"路径，B 必然是单图直发；
- 若 A 已落库，B 的持久化会走 `_merge_user_message` 合并，B 的预检应显示
  `photo×4`（两张图 × 双重计数），实际是 `photo×2`（= 1 张 × 双重计数）
  —— 铁证：**A 从未入库**；
- 全程无 `图片解析失败` / `打断保全：已沉淀` / `合并进上一条未回应的 user`
  日志 —— 与"A 在落库前被无声取消"完全吻合；
- 15:49:01 心跳 `active_tasks=1` —— 只有 B 的回合在跑，无相册聚合任务。

---

## 二、缺陷 1：预检双重计数（protocols/pipeline.py）

### 原代码

```python
if msg_type in kind_map:
    # 按 file_ids / file_id 数一遍
    ...
atts = user_message.get("attachments")
if isinstance(atts, list):
    # 再按 attachments 数一遍 ← 同一附件被第二次计数
    ...
```

所有消息生产者（单图、相册聚合、文本引用、打断合并）写入的信封都**同时**
携带两种表示。结果：1 张图 → `photo×2`，2 张图 → `photo×4`。
预检日志是排查多模态问题的第一入口，这个失真直接把前三轮补丁全部带偏。

### 修复

按 `(模态, file_id)` **联合去重计数**：同一物理附件无论以哪种表示出现
（或同时出现），恰好计 1 次。Telegram 对同一文件的每次发送分配唯一
file_id，去重不会误合并用户真正重复发送的两张图。

修复后 `photo×N` 恒等于"本轮实际到达管道的照片数"，日志重新可信。

---

## 三、缺陷 2：派发时消息未落库（核心丢消息点）

### 原时序（有丢失窗口）

```
process_update(照片A)
  └─ spawn_turn_task
       ├─ _interrupt_active_generation   ← 打断上一条消息的回合
       └─ create_task(TA)
            └─ TA: _handle_photo_message
                 ├─ try_consume_media_message   （wizard 检查）
                 ├─ pre_flight_context_check    （chat lock / 压缩）
                 └─ get_ai_response
                      └─ persist_user_message_entry  ← 落库点，距离派发 1~2s+
```

在这 1~2 秒+ 的窗口内，任何一条新消息（第三者视角的"快速连发"）都会
`spawn_turn_task` → 取消 TA → 照片 A **从未进入历史**。更糟的是：

- 取消不打任何日志（`_cancel_old_task` 静默）；
- `pre_flight_context_check` 超时告警、`_log_stage` 慢阶段告警全部在
  落库点之后 —— 整条链路对丢失**零痕迹**。

`turn_recovery.persist_user_message_entry` 的合并语义本身是正确的，但
它被调用得太晚：合并的前提（历史末尾是未回应 user 消息）永远轮不到
快速连发场景生效。

### 修复（turn_recovery / app_turns / app / ai_handlers 四处协同）

1. **`spawn_turn_task` 新增 `user_message` 关键字参数**：在打断旧回合
   **之后**、创建新任务**之前**直接落库。次序有讲究：
   - 必须在打断之后 —— 旧回合的 journal 保全先把已完成的
     assistant/tool 消息写入历史，新 user 消息才能排在它们后面；
   - 落库自带 `EARLY_PERSIST_FLAG`；
   - 落库失败不阻断派发（`get_ai_response` 内的既有路径自动兜底）。
2. **`get_ai_response` 看到标记跳过重复落库** —— 否则同一信封会被
   merge 进自己，图片翻倍。
3. **`app.py` 全部 8 个派发点**（location/photo/document/audio×2/video/
   sticker/text）传入 `user_message`。
4. **回滚机制 `undo_early_persist`**：提前落库改变了两个分支的语义，
   需要显式还原：
   - 媒体参数卡片消费（`try_consume_media_message` /
     `try_consume_text_message` 返回 True）—— 素材不应进入对话历史；
   - `pre_flight_context_check` 拒绝（超预算）—— 原行为不入库。
   回滚以 `EARLY_PERSIST_TS`（随 `_wrap_envelope` 进入存储消息 meta，
   永不出站）做身份校验：只有 `mode=appended` 且历史末尾恰好是本次
   写入的那条才 pop；`merged`（已并入旧消息）与 TS 不命中（并发写入
   顶掉）一律保守不删。
5. **可观测性**：`media_wizard` 的全部消费分支补 INFO 日志
   （`[media-wizard] chat=… 已消费媒体消息…`）。今后再遇"少了一张图"，
   日志可直接区分三种情况：被卡片消费 / 被打断（现已不可能丢）/ 解析失败。

### 修复后的时序

```
process_update(照片A)
  └─ spawn_turn_task(user_message=A)
       ├─ _interrupt_active_generation     （旧回合收尾、journal 保全）
       ├─ persist_user_message_entry(A)    ← 落库点提前到派发时刻，窗口归零
       └─ create_task(TA)

process_update(照片B)     ← 无论多快到达
  └─ spawn_turn_task(user_message=B)
       ├─ _interrupt_active_generation     （取消 TA —— A 已安全在历史中）
       ├─ persist_user_message_entry(B)    → 历史末尾是 A（未回应 user）
       │                                     → _merge_user_message 合并
       │                                     合并信封 file_ids=[A,B] ✓
       └─ create_task(TB) → 模型请求包含两张图 ✓
```

---

## 四、修复范围清单

| 文件 | 改动 |
|------|------|
| `src/protocols/pipeline.py` | `resolve_input_combination` / `_count_kind`：按 `(模态, file_id)` 联合去重计数，修复双重计数 |
| `src/turn_recovery.py` | 新增 `EARLY_PERSIST_MODE` / `EARLY_PERSIST_TS` 标记与 `undo_early_persist()`；`persist_user_message_entry` 记录落库方式与身份标记 |
| `src/app_turns.py` | `spawn_turn_task` 新增 `user_message` 参数（打断后、建任务前落库）；新增 `undo_early_persist_safe()`；6 个 handler 在"卡片消费 / pre_flight 拒绝"分支回滚 |
| `src/app.py` | 8 个 `spawn_turn_task` 派发点传入 `user_message` |
| `src/ai_handlers.py` | `get_ai_response` 对带 `EARLY_PERSIST_FLAG` 的信封跳过重复落库 |
| `src/media_wizard.py` | 消费分支补 INFO 日志 |
| `tests/unit/test_early_persist_and_input_count.py` | 新增 12 个回归测试（计数不变量 + 持久化/回滚/合并不变量 + spawn 接线） |

## 五、验证

- 新增测试 12/12 通过（含"连发两张图合并后 file_ids=[A,B]"核心场景）；
- 既有相关套件全部通过：`test_unified_pipeline` / `test_reply_media_full_album`
  / `test_photo_group_partial_failure` / `test_media_wizard` 及全部单测；
- 全量 `pytest tests/unit` 中仅 2 个与本次改动无关的既有失败
  （`test_media_base64_fallback::test_file_id_cache_reuse…` 与
  `test_unified_image_tool::test_dual_mode_example…`——用原始未修改代码
  复测同样失败，属上传快照自带问题，建议另行排查）。

## 六、部署后如何自证

复现原场景（快速连发两张图 + "回答"），日志应出现：

```
[turn-recovery] chat=… 新 user 消息合并进上一条未回应的 user 消息
统一管道预检: chat=… input=[…+photo×2]   ← 现在是真实计数：两张图
```

且模型回复应同时描述两张图。若再次出现少图，日志必有三类痕迹之一：
`[media-wizard] 已消费…`（卡片收走了）/ `图片解析失败`（下载/存储）/
`打断保全：已沉淀…`（旧回合进度）—— 每条路都有迹可循。

## 七、遗留建议（未在本次改动范围）

1. 相册分片间隔超过 `MEDIA_GROUP_TIMEOUT=5s` 时，迟到分片会按设计
   追加为独立回合（`_reschedule_if_late_shards`）——不丢消息，但用户
   会看到两条回复。若体验不佳，可在聚合 pop 前检查"最近 1s 内仍有新
   分片"时延长一次等待（需权衡回复延迟）。
2. 两个既有失败测试（见第五节）与本缺陷无关，建议单独立项。
3. `_merge_user_message` 合并两条单图消息时，`content` 是两段
   "📎 用户上传了图片…"占位文案的拼接，模型侧观感略冗余，可考虑合并
   时去重占位行（纯文案优化，不影响功能）。
