"""Compile only the modified source files without importing application dependencies."""
from pathlib import Path

FILES = [
    Path("src/apitelegramchat/agent_context.py"),
    Path("src/apitelegramchat/app.py"),
    Path("src/apitelegramchat/ai_handlers.py"),
    Path("src/apitelegramchat/subagent_tool.py"),
]

for path in FILES:
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")
    print(f"compiled: {path}")

print("syntax validation passed")
