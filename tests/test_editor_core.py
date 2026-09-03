import os
import shutil
import tempfile
import unittest
from pathlib import Path

# 从源码直接抽测纯函数逻辑
import re

_ANSI_ESCAPE_RE = re.compile(
    r'\x1B(?:'
    r'\][^\x07\x1b]*(?:\x07|\x1b\\)|'
    r'\[[0-?]*[ -/]*[@-~]|'
    r'[@-Z\\-_]'
    r')'
)

def _strip_ansi(text: str) -> str:
    if not text:
        return ""
    return _ANSI_ESCAPE_RE.sub('', text)


def _editor_safe_path(path: str, allow_root: bool = False) -> str:
    if not path or not isinstance(path, str):
        raise ValueError("Invalid path: empty or non-string path not allowed")
    if "\x00" in path:
        raise ValueError("Invalid path: null byte not allowed")

    cleaned = path.strip()
    if cleaned in ("", ".", "/", "./"):
        if allow_root:
            return "."
        raise ValueError("Invalid path: empty or root path not allowed for file editing")

    if cleaned.startswith("/workspace/"):
        cleaned = cleaned[len("/workspace/"):]
    elif cleaned.startswith("workspace/"):
        cleaned = cleaned[len("workspace/"):]
    elif cleaned.startswith("/"):
        cleaned = cleaned.lstrip("/")

    norm = os.path.normpath(cleaned)
    if norm in ("", "."):
        if allow_root:
            return "."
        raise ValueError("Invalid path: root path not allowed for file editing")
    if norm.startswith("..") or norm.startswith("/") or os.path.isabs(norm):
        raise ValueError("Invalid path: directory traversal not allowed")
    return norm


def _format_editor_line(index: int, line: str, width: int) -> str:
    return f"{str(index).rjust(width)}\t{line}"


def _format_editor_snippet(
    content: str,
    target_line: int,
    snippet_lines: int = 4,
    total_new_lines: int = 0,
) -> str:
    lines = content.splitlines()
    if not lines:
        return "(empty file)"
    total_lines = len(lines)
    start_line = max(1, target_line - snippet_lines)
    end_line = min(total_lines, target_line + snippet_lines + max(0, total_new_lines))
    width = max(len(str(total_lines)), 4)
    snippet = "\n".join(
        _format_editor_line(idx, lines[idx - 1], width)
        for idx in range(start_line, end_line + 1)
    )
    return (
        f"Here's the result of running `cat -n` on a snippet of the file (lines {start_line}-{end_line}):\n"
        f"{snippet}\n"
        "Review the changes and make sure they are as expected. Edit the file again if necessary."
    )


def _list_directory_contents(dir_path: Path, display_name: str, max_depth: int = 2) -> str:
    entries = []
    root_level = len(dir_path.parts)
    try:
        for current_root, dirs, files in os.walk(dir_path):
            current_path = Path(current_root)
            depth = len(current_path.parts) - root_level
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if depth >= max_depth:
                dirs.clear()

            for f in sorted(files):
                if f.startswith("."):
                    continue
                try:
                    rel_file = (current_path / f).relative_to(dir_path)
                    entries.append(str(rel_file))
                except ValueError:
                    entries.append(f)
            for d in sorted(dirs):
                try:
                    rel_dir = (current_path / d).relative_to(dir_path)
                    entries.append(str(rel_dir) + "/")
                except ValueError:
                    entries.append(d + "/")
    except Exception as exc:
        return f"Error listing directory: {exc}"

    entries.sort()
    header_name = display_name if display_name and display_name != "." else "the workspace root"
    if not entries:
        return f"Directory {header_name} is empty (excluding hidden items)."
    listing = "\n".join(entries)
    return (
        f"Here's the files and directories up to 2 levels deep in {header_name}, "
        f"excluding hidden items:\n{listing}\n"
    )


class TestCoreToolsLogic(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_strip_ansi(self):
        colored = "\x1b[31mRed Text\x1b[0m and \x1b[1;32mBold Green\x1b[0m"
        self.assertEqual(_strip_ansi(colored), "Red Text and Bold Green")
        cursor = "Hello\x1b[2K\x1b[1GWorld"
        self.assertEqual(_strip_ansi(cursor), "HelloWorld")
        osc = "\x1b]0;Terminal Title\x07Prompt$ "
        self.assertEqual(_strip_ansi(osc), "Prompt$ ")

    def test_editor_safe_path(self):
        self.assertEqual(_editor_safe_path(".", allow_root=True), ".")
        self.assertEqual(_editor_safe_path("/", allow_root=True), ".")
        self.assertEqual(_editor_safe_path("/workspace/", allow_root=True), ".")
        self.assertEqual(_editor_safe_path("/workspace/src/app.py"), "src/app.py")
        self.assertEqual(_editor_safe_path("/src/app.py"), "src/app.py")
        self.assertEqual(_editor_safe_path("src/app.py"), "src/app.py")

        with self.assertRaises(ValueError):
            _editor_safe_path(".", allow_root=False)
        with self.assertRaises(ValueError):
            _editor_safe_path("../secret")

    def test_snippet_generation(self):
        content = "\n".join(f"line {i}" for i in range(1, 21))
        snippet = _format_editor_snippet(content, target_line=10, snippet_lines=2)
        self.assertIn("cat -n", snippet)
        self.assertIn("line 8", snippet)
        self.assertIn("line 10", snippet)
        self.assertIn("line 12", snippet)
        self.assertNotIn("line 7", snippet)

    def test_dir_listing(self):
        sub = self.workspace / "sub"
        sub.mkdir()
        (sub / "child.py").write_text("ok")
        (self.workspace / "test.txt").write_text("content")
        (self.workspace / ".hidden").write_text("hidden")

        res = _list_directory_contents(self.workspace, "root")
        self.assertIn("test.txt", res)
        self.assertIn("sub/", res)
        self.assertNotIn(".hidden", res)


if __name__ == "__main__":
    unittest.main()
