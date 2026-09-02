#!/usr/bin/env python3
"""text_editor 回归测试（TEXT_EDITOR_FIXES.md 配套）。

用法（项目根目录）::

    PYTHONPATH=src python scripts/test_text_editor.py

直接驱动 ``execute_text_editor`` 走真实 workspace（隔离的
APITELEGRAMCHAT_DATA_DIR），覆盖：
  A. 四命令基本语义与行号 / view_range
  B. CRLF 行尾保真（str_replace / insert 后风格不变）
  C. 混合行尾的透明归一化（消息明说，不静默改写）
  D. 路径安全（遍历 / 绝对路径 / 空路径 / 符号链接逃逸不崩溃）
  E. 类型防御（bool 穿透 int、None 参数）
  F. 输出上限（超大文件按行截断 + view_range 续读指引 + 单行截断）
  G. 后台持久化（Task 引用被保存、直传 content_bytes 无竞态）
  H. 错误消息约定（Error: 前缀、相对路径、不泄漏服务器目录）
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# 隔离的 workspace 数据目录（必须在导入 search_engine 前设置）
DATA_DIR = tempfile.mkdtemp(prefix="te_test_data_")
os.environ["APITELEGRAMCHAT_DATA_DIR"] = DATA_DIR

import apitelegramchat.search_engine as se  # noqa: E402
from apitelegramchat.search_engine import execute_text_editor  # noqa: E402

# 保存真实实现供 finally 恢复（fake 只在 G 段内生效）
se_upload_restore = se.upload_bytes_to_r2

PASS = 0
FAIL = 0
FAILURES = []

CHAT = 987654321


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        FAILURES.append((label, detail))
        print(f"  FAIL {label}  {detail}")


async def run(**kwargs):
    return await execute_text_editor(chat_id=CHAT, **kwargs)


def ws_path(name: str) -> Path:
    return Path(DATA_DIR) / "workspaces" / str(CHAT) / name


async def drain_persist_tasks():
    tasks = set(se._editor_persist_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def main():
    # ---------- A. 基本语义 ----------
    print("== A. 四命令基本语义 ==")
    r = await run(command="create", path="a.txt", file_text="line1\nline2\nline3\n")
    check("A1 create 成功", r.startswith("Successfully created file"), repr(r[:80]))
    r = await run(command="view", path="a.txt")
    check("A2 view 带行号", r == "1: line1\n2: line2\n3: line3", repr(r))
    r = await run(command="view", path="a.txt", view_range=[2, -1])
    check("A3 view_range [2,-1]", r == "2: line2\n3: line3", repr(r))
    r = await run(command="view", path="a.txt", view_range=[3, 3])
    check("A4 view_range 单行", r == "3: line3", repr(r))
    r = await run(command="str_replace", path="a.txt", old_str="line2", new_str="LINE2")
    check("A5 str_replace 成功", r.startswith("Successfully replaced"), repr(r[:80]))
    r = await run(command="str_replace", path="a.txt", old_str="line", new_str="X")
    check("A6 多处匹配报错 + 恢复指引",
          r.startswith("Error: Found 2 matches"), repr(r[:120]))
    r = await run(command="str_replace", path="a.txt", old_str="nope", new_str="X")
    check("A7 零匹配报错 + 恢复指引",
          r.startswith("Error: No match found"), repr(r[:120]))
    r = await run(command="insert", path="a.txt", insert_line=0, insert_text="HEAD\n")
    check("A8 insert line 0 成功", r.startswith("Successfully inserted"), repr(r[:80]))
    r = await run(command="view", path="a.txt")
    check("A9 insert 0 后首行是 HEAD", r.splitlines()[0] == "1: HEAD", repr(r))

    await run(command="create", path="nonl.txt", file_text="aaa\nbbb")
    await run(command="insert", path="nonl.txt", insert_line=2, insert_text="ccc")
    r = await run(command="view", path="nonl.txt")
    check("A10 无尾换行 insert 语义正确", r == "1: aaa\n2: bbb\n3: ccc", repr(r))

    await run(command="create", path="empty.txt", file_text="")
    r = await run(command="view", path="empty.txt")
    check("A11 view 空文件", r == "(empty file)", repr(r))
    r = await run(command="insert", path="empty.txt", insert_line=0, insert_text="first\n")
    check("A12 空文件 insert 成功", not r.startswith("Error"), repr(r[:80]))
    r = await run(command="create", path="a.txt", file_text="dup")
    check("A13 create 已存在文件报错", r.startswith("Error: File already exists"), repr(r[:80]))
    r = await run(command="create", path="d/x.txt", file_text="nested")
    check("A14 create 嵌套目录自动创建", not r.startswith("Error"), repr(r[:80]))
    r = await run(command="view", path="missing.txt")
    check("A15 view 缺失文件报错", r == "Error: File not found", repr(r))
    r = await run(command="str_replace", path="missing.txt", old_str="a", new_str="b")
    check("A16 str_replace 缺失文件报错", r == "Error: File not found", repr(r))
    r = await run(command="frobnicate", path="a.txt")
    check("A17 未知命令报错并列出四个合法命令",
          r.startswith("Error: Unknown command") and "insert" in r, repr(r[:120]))
    ws_path("adir").mkdir(parents=True, exist_ok=True)
    r = await run(command="view", path="adir")
    check("A18 view 目录报错", r.startswith("Error: Path is a directory"), repr(r[:80]))

    # ---------- B. CRLF 行尾保真 ----------
    print("== B. CRLF 行尾保真 ==")
    ws_path("crlf.txt").write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    r = await run(command="view", path="crlf.txt")
    check("B1 view CRLF 文件显示为普通行",
          r == "1: alpha\n2: beta\n3: gamma", repr(r))
    # 模型从 view 输出复制 old_str（LF 连接）
    r = await run(command="str_replace", path="crlf.txt",
                  old_str="alpha\nbeta", new_str="A\nB")
    check("B2 CRLF 文件用 LF 版 old_str 可匹配", not r.startswith("Error"), repr(r[:150]))
    raw = ws_path("crlf.txt").read_bytes()
    check("B3 编辑后文件保持 CRLF（不再被静默改写为 LF）",
          raw == b"A\r\nB\r\ngamma\r\n", repr(raw))
    r = await run(command="str_replace", path="crlf.txt",
                  old_str="gamma", new_str="G")
    raw = ws_path("crlf.txt").read_bytes()
    check("B4 行内替换同样保持 CRLF", raw == b"A\r\nB\r\nG\r\n", repr(raw))
    # 模型直接给 CRLF 版 old_str 也能匹配
    ws_path("crlf2.txt").write_bytes(b"alpha\r\nbeta\r\n")
    r = await run(command="str_replace", path="crlf2.txt",
                  old_str="alpha\r\nbeta", new_str="A\r\nB")
    check("B5 CRLF 版 old_str 直接精确匹配", not r.startswith("Error"), repr(r[:150]))
    raw = ws_path("crlf2.txt").read_bytes()
    check("B6 CRLF 版参数写回仍保持 CRLF", raw == b"A\r\nB\r\n", repr(raw))
    # insert
    ws_path("crlf3.txt").write_bytes(b"one\r\ntwo\r\n")
    r = await run(command="insert", path="crlf3.txt", insert_line=1, insert_text="mid")
    check("B7 CRLF 文件 insert 成功", not r.startswith("Error"), repr(r[:80]))
    raw = ws_path("crlf3.txt").read_bytes()
    check("B8 CRLF 文件 insert 后整体仍为 CRLF",
          raw == b"one\r\nmid\r\ntwo\r\n", repr(raw))
    # create 写入 CRLF 内容原样落盘
    await run(command="create", path="crlf_new.txt", file_text="x\r\ny\r\n")
    check("B9 create 的 CRLF 内容逐字节落盘",
          ws_path("crlf_new.txt").read_bytes() == b"x\r\ny\r\n",
          repr(ws_path("crlf_new.txt").read_bytes()))

    # ---------- C. 混合行尾：透明归一化 ----------
    print("== C. 混合行尾透明归一化 ==")
    ws_path("mixed.txt").write_bytes(b"alpha\r\nbeta\ngamma\n")
    r = await run(command="str_replace", path="mixed.txt",
                  old_str="alpha\nbeta", new_str="A\nB")
    check("C1 混合行尾按 LF 归一化后可匹配", not r.startswith("Error"), repr(r[:150]))
    check("C2 结果消息明说发生了行尾归一化（不静默改写）",
          "mixed line endings" in r, repr(r[:150]))
    raw = ws_path("mixed.txt").read_bytes()
    check("C3 归一化后文件为 LF 风格", raw == b"A\nB\ngamma\n", repr(raw))
    # 纯 LF 文件行为与旧版一致（精确匹配）
    await run(command="create", path="lf.txt", file_text="a\nb\n")
    r = await run(command="str_replace", path="lf.txt", old_str="a\nb", new_str="A\nB")
    check("C4 纯 LF 文件精确匹配不受影响", not r.startswith("Error"), repr(r[:80]))
    r = await run(command="str_replace", path="lf.txt", old_str="nope\nb", new_str="X")
    check("C5 纯 LF 文件无 CR 不触发归一化回退（仍报零匹配）",
          r.startswith("Error: No match found"), repr(r[:100]))

    # ---------- D. 路径安全 ----------
    print("== D. 路径安全 ==")
    r = await run(command="view", path="../a.txt")
    check("D1 路径遍历被拒绝", r.startswith("Error:"), repr(r[:80]))
    r = await run(command="view", path="/etc/passwd")
    check("D2 绝对路径被拒绝", r.startswith("Error:"), repr(r[:80]))
    r = await run(command="view", path="")
    check("D3 空路径被拒绝", r.startswith("Error:"), repr(r[:80]))
    r = await run(command="create", path="a\x00b.txt", file_text="x")
    check("D4 null 字节路径被拒绝", r.startswith("Error:"), repr(r[:80]))

    outside = Path(DATA_DIR) / "outside.txt"
    outside.write_text("secret")
    link = ws_path("link.txt")
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(outside)
    try:
        r = await run(command="view", path="link.txt")
        ok = isinstance(r, str) and r.startswith("Error:")
    except Exception as exc:  # noqa: BLE001
        ok = False
        r = f"raised {exc!r}"
    check("D5 符号链接逃逸：返回 Error 消息而不是抛未捕获异常", ok, repr(r[:100]))
    try:
        r = await run(command="str_replace", path="link.txt", old_str="s", new_str="x")
        ok = isinstance(r, str) and r.startswith("Error:")
    except Exception as exc:  # noqa: BLE001
        ok = False
        r = f"raised {exc!r}"
    check("D6 符号链接逃逸（编辑命令）同样不崩溃", ok, repr(r[:100]))

    # ---------- E. 类型防御 ----------
    print("== E. 类型防御 ==")
    r = await run(command="insert", path="a.txt", insert_line=True, insert_text="X")
    check("E1 insert_line=True 被拒绝（不再当作 1）",
          r.startswith("Error:"), repr(r[:100]))
    r = await run(command="view", path="a.txt", view_range=[True, -1])
    check("E2 view_range 含 bool 被拒绝", r.startswith("Error:"), repr(r[:100]))
    r = await run(command="str_replace", path="a.txt", old_str="HEAD", new_str=None)
    check("E3 new_str=None 报错", r.startswith("Error:"), repr(r[:100]))
    r = await run(command="insert", path="a.txt", insert_line=2.5, insert_text="X")
    check("E4 insert_line 浮点被拒绝", r.startswith("Error:"), repr(r[:100]))
    r = await run(command="create", path="nl.bin")
    check("E5 file_text 缺失报错", r.startswith("Error: Missing file_text"), repr(r[:100]))

    # ---------- F. 输出上限 ----------
    print("== F. 输出上限 ==")
    big_line = "x" * 500 + "\n"
    ws_path("big.txt").write_text(big_line * 30000)  # 1.5 万行 × 500 字符 ≈ 15MB
    r = await run(command="view", path="big.txt")
    from apitelegramchat.token_budget import count_tokens
    check("F1 超大文件 view 被 token 预算截断（防上下文炸弹）",
          count_tokens(r) <= se._EDITOR_VIEW_TOKEN_BUDGET,
          f"tokens={count_tokens(r)}")
    check("F2 截断尾注含总行数与 view_range 续读指引",
          "30000 lines" in r and "view_range=[" in r, r.splitlines()[-1][:160])
    resume_line = int(r.splitlines()[-1].split("view_range=[")[1].split(",")[0])
    r2 = await run(command="view", path="big.txt", view_range=[resume_line, resume_line + 2])
    check("F3 按尾指引的 view_range 能精确续读",
          r2.split(":")[0].strip() == str(resume_line), repr(r2[:60]))
    # 单行截断
    ws_path("longline.txt").write_text("y" * 5000 + "\n")
    r = await run(command="view", path="longline.txt")
    check("F4 超长单行被截断到 2000 字符 + 标记",
          "…[line truncated]" in r and len(r) < 2200, f"len={len(r)}")
    # 大文件拒绝（读入体积上限）
    huge = ws_path("huge.txt")
    huge.write_bytes(b"z" * (17 * 1024 * 1024))
    r = await run(command="view", path="huge.txt")
    check("F5 超过 view 体积上限返回可操作错误（不读入内存）",
          r.startswith("Error: File too large") and "bash" in r, repr(r[:120]))

    # ---------- G. 后台持久化 ----------
    print("== G. 后台持久化 ==")
    uploads: list[tuple[str, bytes]] = []

    async def fake_upload(data, key, content_type):
        uploads.append((key, data))
        return f"https://fake/{key}"

    se.upload_bytes_to_r2 = fake_upload
    try:
        await run(command="create", path="persist.txt", file_text="hello\r\nworld\r\n")
        await run(command="str_replace", path="persist.txt",
                  old_str="hello\nworld", new_str="HELLO\nWORLD")
        await drain_persist_tasks()
        persisted = [u for u in uploads if u[0] == f"editor/{CHAT}/persist.txt"]
        check("G1 持久化任务被调度（引用被保存，可等待）", len(persisted) == 2, repr(persisted))
        if len(persisted) == 2:
            check("G2 create 直传原始字节（CRLF 原样）",
                  persisted[0][1] == b"hello\r\nworld\r\n", repr(persisted[0][1]))
            check("G3 str_replace 直传写入字节（CRLF 写回）",
                  persisted[1][1] == b"HELLO\r\nWORLD\r\n", repr(persisted[1][1]))
        check("G4 R2 key 按用户隔离", all(u[0].startswith(f"editor/{CHAT}/")
                                          for u in uploads), "")
    finally:
        se.upload_bytes_to_r2 = se_upload_restore

    # ---------- H. 错误消息约定 ----------
    print("== H. 错误消息约定 ==")
    r = await run(command="create", path="msg.txt", file_text="x")
    check("H1 成功消息使用相对路径（不泄漏服务器目录）",
          DATA_DIR not in r and not Path(r.split("\n")[0]).is_absolute(), repr(r[:100]))
    r = await run(command="str_replace", path="msg.txt", old_str="x", new_str="y")
    check("H2 str_replace 成功消息使用相对路径", DATA_DIR not in r, repr(r[:100]))

    # ---------- 二进制内容 ----------
    ws_path("bin.dat").write_bytes(b"\xff\xfe\x00binary")
    r = await run(command="view", path="bin.dat")
    check("I1 非 UTF-8 文件 view 报错",
          r.startswith("Error: File is not valid UTF-8"), repr(r[:80]))
    r = await run(command="str_replace", path="bin.dat", old_str="a", new_str="b")
    check("I2 非 UTF-8 文件编辑报错", r.startswith("Error: File is not valid"), repr(r[:80]))

    print(f"\n{'=' * 60}")
    print(f"通过 {PASS} 项 / 失败 {FAIL} 项")
    if FAILURES:
        print("失败清单：")
        for label, detail in FAILURES:
            print(f"  - {label}: {detail}")
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
