# Bash hang fix

修复了持久 Bash session 在模型生成**语法不完整**的命令时卡死的问题。

## 背景

第一版修复只覆盖了 heredoc（`python3 << 'EOF'` 漏掉结束 `EOF`）。但持久 shell
会在**任何**未闭合的 shell 语法结构上挂起等待，不止 heredoc，还包括：

- 未闭合的双引号 / 单引号字符串，例如模型输出被截断的
  `python3 -c "\n多行代码...`（引号没写完就断了）
- 未闭合的反引号、`$(`、`((`、`{` 等

这类命令不含 `<<`，绕过了原来的 heredoc 正则检测，被当作普通命令送进持久 shell。
持久 shell 会一直等待未闭合的定界符结束，而我们追加在命令后面、用来标记执行
完成的 synthetic end marker（`echo '__END_xxx__ $?'`）会被当作还未闭合的
字符串内容读入，永远不会被真正 echo 出来。于是 `read_until_marker()` 一直
读不到 marker，直到外层 `asyncio.wait_for(..., timeout=timeout)` 超时
（默认约 300 秒）才会被强制 kill 并重启 session —— 这正是日志里
`Bash timeout` 前长时间没有新日志、前端卡住不动的原因。

## 核心变化

1. 新增 `BashSession._is_unterminated(command)`：用 `bash -n -c command`
   做纯语法检查（不执行），如果 stderr 报 `unexpected EOF while looking
   for matching` 一类错误，说明这条命令在持久 shell 里执行会挂起等待，
   返回 `True`。这个检测覆盖了未闭合引号/反引号/括号等所有会导致挂起的
   情况，不需要为每种定界符单独写正则。
2. `execute()` 中原来只判断 `has_heredoc` 的地方，现在是
   `has_heredoc or await self._is_unterminated(command)` —— 命中任意
   一种情况都会走一次性隔离执行（`_execute_heredoc_isolated`），不再
   走持久 shell。
   - 保留原有 heredoc 正则：`bash -n` 对**未闭合的 heredoc**只给
     warning、returncode 仍是 0（不算语法错误），所以 heredoc 检测不能
     被 `_is_unterminated` 取代，两者是"或"的关系，互为补充。
3. `_is_unterminated` 本身有 5 秒超时保护和异常兜底：探测超时或探测本身
   出错时，不会阻塞主流程，最多是退化为按原来的 heredoc-only 检测处理。
4. 一次性隔离执行（`bash -lc`，stdin=DEVNULL）本身逻辑不变：语法不完整
   的命令在这种模式下会立即因为 EOF 报 shell 错误退出，而不是挂起等待，
   仍然使用同一 workspace / Landlock sandbox，并回传 exit code / cwd。
5. Bash 工具开始执行时立即向 Rich Message draft 推送"Bash 运行中"，执行
   期间继续把工具输出作为 preview 推送，避免前端长时间没有可见变化。
6. Bash progress callback 的异常不会影响命令执行。

## 效果

避免了"模型第 N 轮调用 bash（例如生成了一段引号未闭合的 `python3 -c
"..."`）后，后端没有新日志、前端一直刷新但内容不再变化，约 300 秒后才
出现 Bash timeout，会话被强制重启"的卡死体验。语法检测本身开销约
1-2ms/次，可忽略不计。
