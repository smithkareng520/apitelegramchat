"""Regression check for ModelScope native image helper call signatures."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLER_PATH = ROOT / "src" / "apitelegramchat" / "ai_handlers.py"
SEARCH_ENGINE_PATH = ROOT / "src" / "apitelegramchat" / "search_engine.py"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function_keyword_only_names(tree: ast.Module, name: str) -> set[str]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return {arg.arg for arg in node.args.kwonlyargs} | {arg.arg for arg in node.args.args}
    raise AssertionError(f"未找到函数定义：{name}")


def _modelscope_helper_calls(tree: ast.Module) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "_request_modelscope_native_image":
                calls.append(node)
    return calls


def main() -> None:
    handler_tree = _parse(HANDLER_PATH)
    engine_tree = _parse(SEARCH_ENGINE_PATH)
    accepted = _function_keyword_only_names(handler_tree, "_request_modelscope_native_image")

    assert "builder" not in accepted, "builder 不是图片请求函数的有效参数"

    for call in _modelscope_helper_calls(handler_tree) + _modelscope_helper_calls(engine_tree):
        provided = {keyword.arg for keyword in call.keywords if keyword.arg is not None}
        unsupported = provided - accepted
        assert not unsupported, f"发现不受支持的图片请求参数：{sorted(unsupported)}"

    print("PASS: ModelScope 图片请求调用参数与函数签名一致")


if __name__ == "__main__":
    main()
