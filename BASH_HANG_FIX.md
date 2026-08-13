# Bash hang fix

修复了持久 Bash session 在模型生成不完整 heredoc（例如 `python3 << 'EOF'` 但漏掉结束 `EOF`）时卡死的问题。

核心变化：

1. 包含 heredoc 的 Bash 命令改为一次性 `bash -lc` 执行，并让 stdin 使用 EOF 结束。
2. 不再依赖持久 shell 等待模型命令后的 synthetic end marker；不完整 heredoc 会在脚本输入结束时退出。
3. 一次性执行仍使用同一 workspace / Landlock sandbox，并回传 exit code / cwd。
4. Bash 工具开始执行时立即向 Rich Message draft 推送“Bash 运行中”，执行期间继续把工具输出作为 preview 推送，避免前端长时间没有可见变化。
5. Bash progress callback 的异常不会影响命令执行。

这样可以避免“模型第 N 轮调用 bash 后，后端没有新日志、前端一直刷新但内容不再变化，约 300 秒后才出现 Bash timeout”的卡死体验。
