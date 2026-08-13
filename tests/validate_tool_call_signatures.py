"""Static parameter-contract audit for project tool and request helper calls."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "apitelegramchat"
PREFIXES = ("execute_", "_request_")


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    path: Path
    line: int
    positional: tuple[str, ...]
    required_positional: tuple[str, ...]
    required_keyword_only: tuple[str, ...]
    accepted: frozenset[str]
    accepts_kwargs: bool


@dataclass(frozen=True)
class CallSite:
    name: str
    path: Path
    line: int
    positional_count: int
    keywords: frozenset[str]
    has_star_args: bool
    has_star_kwargs: bool


def _iter_python_files() -> list[Path]:
    return sorted(SOURCE_ROOT.rglob("*.py"))


def _function_specs(path: Path, tree: ast.Module) -> list[FunctionSpec]:
    specs: list[FunctionSpec] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith(PREFIXES):
            continue
        positional_nodes = tuple(node.args.posonlyargs + node.args.args)
        positional = tuple(arg.arg for arg in positional_nodes)
        required_positional_count = len(positional) - len(node.args.defaults)
        required_positional = positional[:required_positional_count]
        required_keyword_only = tuple(
            arg.arg
            for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults)
            if default is None
        )
        accepted = frozenset(positional + tuple(arg.arg for arg in node.args.kwonlyargs))
        specs.append(
            FunctionSpec(
                name=node.name,
                path=path,
                line=node.lineno,
                positional=positional,
                required_positional=required_positional,
                required_keyword_only=required_keyword_only,
                accepted=accepted,
                accepts_kwargs=node.args.kwarg is not None,
            )
        )
    return specs


def _call_sites(path: Path, tree: ast.Module) -> list[CallSite]:
    calls: list[CallSite] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if not node.func.id.startswith(PREFIXES):
            continue
        has_star_args = any(isinstance(arg, ast.Starred) for arg in node.args)
        has_star_kwargs = any(keyword.arg is None for keyword in node.keywords)
        calls.append(
            CallSite(
                name=node.func.id,
                path=path,
                line=node.lineno,
                positional_count=len(node.args),
                keywords=frozenset(keyword.arg for keyword in node.keywords if keyword.arg is not None),
                has_star_args=has_star_args,
                has_star_kwargs=has_star_kwargs,
            )
        )
    return calls


def main() -> None:
    specs_by_name: dict[str, list[FunctionSpec]] = {}
    calls: list[CallSite] = []
    for path in _iter_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for spec in _function_specs(path, tree):
            specs_by_name.setdefault(spec.name, []).append(spec)
        calls.extend(_call_sites(path, tree))

    errors: list[str] = []
    for call in calls:
        targets = specs_by_name.get(call.name, [])
        if len(targets) != 1:
            continue
        spec = targets[0]
        if call.has_star_args or call.has_star_kwargs:
            continue

        unexpected = call.keywords - spec.accepted
        if unexpected and not spec.accepts_kwargs:
            errors.append(
                f"{call.path.relative_to(ROOT)}:{call.line}: {call.name} 收到不支持参数 {sorted(unexpected)}；"
                f"定义位于 {spec.path.relative_to(ROOT)}:{spec.line}"
            )

        provided_positional = set(spec.positional[:call.positional_count])
        provided = provided_positional | call.keywords
        missing = (set(spec.required_positional) | set(spec.required_keyword_only)) - provided
        if missing:
            errors.append(
                f"{call.path.relative_to(ROOT)}:{call.line}: {call.name} 缺少必需参数 {sorted(missing)}；"
                f"定义位于 {spec.path.relative_to(ROOT)}:{spec.line}"
            )

    if errors:
        raise AssertionError("工具参数契约审计失败：\n" + "\n".join(errors))
    print("PASS: execute_* 与 _request_* 调用均匹配已定义参数契约")


if __name__ == "__main__":
    main()
