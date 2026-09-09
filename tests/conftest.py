# =====================================================================
# tests/conftest.py — pytest 全局配置
# =====================================================================
# 把项目 src/ 目录加入 sys.path，使测试可以直接导入扁平化后的顶层模块
# （app.py / config.py / markdown_converter.py / ai/ / mcpserver/ 等）。
# 在导入任何项目模块之前执行，保证所有测试共享同一个导入根。
# =====================================================================
from pathlib import Path

import os
import sys
import tempfile

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ---- 在导入任何项目模块之前，把工作空间根指到独立临时目录 ----
# workspace_paths.workspaces_root() 默认 /home 且带 lru_cache：不隔离的话，
# 未显式设置环境的测试会在真实 /home 下创建目录。这里给一个会话级临时
# 目录兑底；需要精确路径断言的测试自行 setenv 并清缓存。
os.environ.setdefault(
    "APITELEGRAMCHAT_WORKSPACES_DIR",
    str(Path(tempfile.mkdtemp(prefix="apitelegramchat_test_ws_"))),
)
