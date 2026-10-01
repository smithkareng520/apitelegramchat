"""pyproject 的 py-modules 清单必须与 src/ 下的顶层模块一致（防止漏列 / 残留已删除模块）。"""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_py_modules_match_src_layout():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    listed = set(cfg["tool"]["setuptools"]["py-modules"])
    actual = {p.stem for p in (ROOT / "src").glob("*.py")}
    assert listed == actual, {"missing": sorted(actual - listed), "stale": sorted(listed - actual)}
