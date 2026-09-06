# =====================================================================
# tests/conftest.py — pytest 全局配置
# =====================================================================
# 把项目 src/ 目录加入 sys.path，使测试可以直接导入扁平化后的顶层模块
# （app.py / config.py / markdown_converter.py / ai/ / mcpserver/ 等）。
# 在导入任何项目模块之前执行，保证所有测试共享同一个导入根。
# =====================================================================
from pathlib import Path

import sys

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
