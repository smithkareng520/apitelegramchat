"""Static regression checks for visible progress during long tool loops."""
import ast
from pathlib import Path

SOURCE = Path("src/apitelegramchat/ai_handlers.py").read_text(encoding="utf-8")
ast.parse(SOURCE)

start = SOURCE.index("async def _run_tool_calls_and_append")
end = SOURCE.index("class RichMessageBuilder", start)
body = SOURCE[start:end]

# Each model tool batch owns a finite UI group, preventing the fifteenth and
# later batches from being appended beyond an old group/UI truncation boundary.
assert "group_idx = builder.start_new_tool_group()" in body
assert "builder.finish_group(group_idx)" in body

# Repeated draft sends are not sufficient: heartbeat must alter visible content.
assert "正在执行 {escape_html(fn_name)}" in body
assert "已运行 {elapsed_seconds} 秒" in body
assert "工具批次仍在运行" in body
assert "开始工具:" in body
assert "工具完成:" in body

print("agent progress visibility validation passed")
