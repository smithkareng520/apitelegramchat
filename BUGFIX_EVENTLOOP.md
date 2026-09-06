# 事件循环阻塞 / 草稿刷新迟滞 修复说明

修复日期：2026-09-06

## 现象

```
CRITICAL 🚨 EVENT LOOP BLOCKED: 期望休眠 10.0s 实际 20.2s (lag=10.21s)
WARNING  sendChatAction exception:            ← 异常信息为空
```
伴随：日志被同一行 "sendRichMessage 兜底转换" 刷屏（同一长度 6590 重复十余次），
最终消息迟迟刷不出来。

---

## 根因 1（主因）：草稿容量扫描是 O(块数 × 全文) 的同步 CPU 热路径

`ai/rich_message_builder.py` 的 `_scan_rich_html_boundaries` 在**每一个块边界**
都对「累计到该处的全部可见文本」重新做一次 tiktoken 全量编码：

```python
def append_boundary(source_end):
    visible_text = "".join(visible_parts)      # 累计全文
    boundaries.append((source_end, count_tokens(visible_text), ...))
```

120 个块的草稿 = 120 次全文编码。实测：

| 场景 | 单次扫描耗时 |
|---|---|
| 旧算法（每边界全文编码） | **401 ms** |
| 新算法（增量累加） | **19 ms** |

而 `flush()` 每帧调用它两次（锁外 `_arm_rollover_if_needed` + 锁内），
按 `STREAM_FLUSH_INTERVAL=0.65s` 一帧计算：

> 旧：401ms × 2 ÷ 650ms ≈ **123% CPU** —— 超过 100%，事件循环必然被饿死。
> 新：19ms × 1 ÷ 650ms ≈ **3%**

这直接解释了 `lag=10.21s`：该窗口内 webhook / 健康检查 / 日志全部无法调度。

### 修复
改为**增量累加**：只对「自上个边界以来新增的片段」编码一次并累加，
复杂度降为 O(全文)。函数末尾仍对全文精确编码一次作为对外返回值。

BPE 跨片段合并会带来 ±个位数偏差，但这些值只用于 3000/6000 token 的
容量阈值判断，偏差远小于裕度。实测 120 块场景末边界 token 与全文精确值
**偏差为 0**，边界数、块数完全一致。

---

## 根因 2：flush 每帧重复构建 + 重复扫描

```python
html_content = self._build_html()        # 锁外：构建 + 扫描
self._arm_rollover_if_needed(html_content)
async with self._flush_lock:
    html_content = self._build_html()    # 锁内：又构建一次，锁外那次全废
```

锁外的构建与扫描结果在拿到锁后被立即丢弃，是纯浪费的同步 CPU 工作。

### 修复
移除锁外的构建与扫描，`_arm_rollover_if_needed` 移入锁内，复用同一份 HTML。
每帧的 O(全文) 工作量从 2 次降为 1 次。

---

## 根因 3：静默保活帧在异常/限流路径下漏更新时间戳

```python
await self.flush(force=silent_too_long)
if not silent_too_long:
    self._last_flush_time = now        # 保活帧不更新
```

`time_elapsed` 基于 `_last_flush_time` 计算。正常路径下 `flush()` 内部会更新它
（所以线上观察到的保活节奏是 ~2.3s，并未失控），**但** `flush()` 在这些分支
会直接返回而不更新：

- `RateLimitError` 冷却分支
- 通用 `Exception` 分支
- 入口处 `now < self._rate_limited_until` 的冷却短路

一旦走到这些路径，`time_elapsed` 会持续 ≥ 阈值，循环退化为**每 0.1s 一次
`force=True` 全帧重发**。而 `force=True` 会绕过「内容相同」短路与 250ms
最小发送间隔，与真正携带新内容的帧争抢 `_flush_lock` 和每 chat 的 draft
发送锁 —— 这正是「最后一条消息刷不出来」的竞态来源之一。

### 修复
无条件兜底更新 `_last_flush_time`，使保活节奏在任何分支下都严格等于
`STREAM_SILENT_FORCE_FLUSH`。

---

## 根因 4：`sendChatAction exception:` 日志信息为空

原代码 `logger.warning(f"sendChatAction exception: {e}")` 有两个问题：

1. **`CancelledError` 被当作错误吞掉**。`chat_actions` 的 4 秒保活循环在任务
   收尾时会 cancel 这个协程，`CancelledError` 的 `str()` 恰好是**空串** ——
   日志里那条空消息就是它。更严重的是吞掉取消信号会破坏协作式取消语义，
   导致调用方 `await task` 挂到超时。
2. aiohttp 的 `ServerTimeoutError` / `ClientOSError` 等 `str()` 也常为空。

### 修复
- `CancelledError` 单独捕获并 `raise` 原样传播（这是正常的取消信号，不是错误）；
- 其余异常补上 `type(e).__name__`、chat_id、action，`str(e)` 为空时回退 `repr(e)`。

用户判断「这应该只是警告」是对的 —— 它确实不是故障，但**不该被记为异常**，
修复后取消路径不再打警告日志。

---

## 附带修复：日志刷屏

`sendRichMessage 兜底转换` 在流式草稿路径上每帧都会命中（每 0.65s 一次），
用 INFO 记录会淹没有效信息，logging 本身也成为热路径开销。已降级为 DEBUG。

（该转换实测仅 5.2ms / 帧，约 0.8% CPU，**不是**阻塞主因，故只调日志级别，
不改动转换逻辑。）

---

## 修改文件清单

| 文件 | 修改点 |
|---|---|
| `ai/rich_message_builder.py` | `_scan_rich_html_boundaries` 增量累加；`flush` 去重复构建/扫描；`_stream_flush_loop` 兜底更新时间戳 |
| `utils.py` | `send_chat_action` 区分 CancelledError 并补全异常信息；兜底转换日志降级 DEBUG |

## 验证

- 三个文件 `py_compile` 全部通过
- 扫描函数边界用例回归：空串 / 纯文本 / 未闭合标签 / 嵌套块 / 自闭合 / HTML 实体
  —— token 单调性、边界数、块数全部正确
- 新旧算法结果一致性：120 块场景边界数 120 vs 120，末边界 token 2040 vs 2040

## 预期效果

- 事件循环 CPU 占用从 ~123% 降至 ~3%，`EVENT LOOP BLOCKED` 消除
- 健康检查不再因 lag 失败，Render 不再触发重启
- 草稿刷新不再被 force 帧挤占，最终消息能正常刷出
- 日志可读性恢复，`sendChatAction` 异常可定位
