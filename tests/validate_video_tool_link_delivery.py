"""Regression checks for generated-video link delivery and rich-message instructions."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLER_PATH = ROOT / "src" / "apitelegramchat" / "ai_handlers.py"
SEARCH_ENGINE_PATH = ROOT / "src" / "apitelegramchat" / "search_engine.py"


def _function_source(path: Path, function_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"未找到函数定义：{function_name}")


def main() -> None:
    prompt_source = _function_source(HANDLER_PATH, "build_system_prompt")
    video_tool_source = _function_source(SEARCH_ENGINE_PATH, "execute_generate_video")
    tool_catalog_source = SEARCH_ENGINE_PATH.read_text(encoding="utf-8")

    assert "视频工具结果处理（强制）" in prompt_source
    assert "&lt;figure&gt;&lt;video src=" in prompt_source
    assert "不得仅输出裸 URL、普通超链接" in prompt_source
    assert "仅使用工具返回的 HTTP/HTTPS URL" in prompt_source
    assert "&amp;amp;" in prompt_source

    assert "视频链接：{final_video_url}" in video_tool_source
    assert "return (" in video_tool_source
    assert "returns a stable HTTPS URL" in tool_catalog_source
    assert "just like image-generation tools return image URLs" in tool_catalog_source

    print("PASS: 视频工具返回链接，系统提示词要求用富文本视频块交付")


if __name__ == "__main__":
    main()
