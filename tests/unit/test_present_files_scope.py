'''present_files 只发 upload/ 下文件'''


from contextlib import asynccontextmanager

import pytest

import file_delivery
from workspace_paths import workspace_upload_root, workspace_workdir


PASS, FAIL = 0, 0
_failures = []


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        _failures.append(f"{name} {detail}")
        print(f"  [FAIL] {name} — {detail}")


# ---------------------------------------------------------------------
# Mock：绕开真实 Telegram HTTP（sendDocument / sendChatAction）
# ---------------------------------------------------------------------
class FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, *args, **kwargs):
        FakeSession.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, data=None):
        FakeSession.requests.append({"url": url, "data": data})
        return FakeResponse()


@asynccontextmanager
async def fake_chat_action_scope(chat_id, action):
    yield


def patch_http(monkeypatch):
    monkeypatch.setattr(file_delivery.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(file_delivery, "chat_action_scope", fake_chat_action_scope)


TEST_CHAT_ID = 9_918_247
TEST_NAMESPACE = "present-files-test"


async def present(paths):
    return await file_delivery.execute_present_files(TEST_CHAT_ID, paths, namespace=TEST_NAMESPACE)


def parse(result: str) -> dict:
    import json

    return json.loads(result)


def make_failed_map(data: dict) -> dict:
    # "out.txt (not under upload/: ...)" -> "out.txt" -> "not under upload/"
    out = {}
    for item in data.get("failed", []):
        reason = item.split(" ", 1)[1] if " " in item else ""
        key = item.split(" (")[0]
        out[key] = reason
    return out


async def _run_suite() -> tuple[int, int, list[str]]:
    ws = workspace_workdir(TEST_CHAT_ID, TEST_NAMESPACE)
    up = workspace_upload_root(TEST_CHAT_ID, TEST_NAMESPACE)
    (ws / "out.txt").write_text("root", encoding="utf-8")
    (ws / "download").mkdir(exist_ok=True)
    (ws / "download" / "x.txt").write_text("dl", encoding="utf-8")
    (up / "ok.txt").write_text("staged", encoding="utf-8")

    async def t_a_reject_outside_upload():
        print("\n=== A. upload/ 之外一律拒绝 ===")
        r = parse(await present(["out.txt"]))
        f = make_failed_map(r)
        check("A1 根目录文件拒绝", not r["sent"] and "not under upload/" in f.get("out.txt", ""), str(f))

        r = parse(await present(["download/x.txt"]))
        f = make_failed_map(r)
        check("A2 download/ 下拒绝", not r["sent"] and "not under upload/" in f.get("download/x.txt", ""), str(f))

        r = parse(await present([str(ws / "out.txt")]))
        f = make_failed_map(r)
        check("A3 工作区内绝对路径拒绝", not r["sent"] and "not under upload/" in f.get(str(ws / "out.txt"), ""), str(f))

        r = parse(await present(["../out.txt"]))
        f = make_failed_map(r)
        check("A4 .. 逃逸被拦", not r["sent"] and f.get("../out.txt", "") != "", str(f))

        r = parse(await present(["upload/../out.txt"]))
        f = make_failed_map(r)
        check("A5 upload/../ 归一化后仍拒绝", not r["sent"] and "not under upload/" in f.get("upload/../out.txt", ""), str(f))

        # 提示语可操作：包含 cp ... upload/ 的修复建议
        r = parse(await present(["out.txt"]))
        check("A6 错误提示含复制建议", "cp out.txt upload/" in r["failed"][0], r["failed"][0])

    async def t_b_accept_upload_paths():
        print("\n=== B. upload/ 下的路径放行 ===")
        # B1 不存在的文件：通过边界校验，报 file not found（而非 not under upload/）
        r = parse(await present(["upload/missing.txt"]))
        f = make_failed_map(r)
        check("B1 边界放行（不存在->not found）", "not under upload/" not in f.get("upload/missing.txt", "")
              and "file not found" in f.get("upload/missing.txt", ""), str(f))

        # B2 存在的文件：走完整发送流程（mock HTTP），计入 sent
        FakeSession.requests = []
        r = parse(await present(["upload/ok.txt"]))
        check("B2 upload/文件发送成功", r["sent"] == ["ok.txt"] and not r["failed"], str(r))
        check("B3 走 sendDocument", len(FakeSession.requests) == 1 and FakeSession.requests[0]["url"].endswith("/sendDocument"))

        # B4 绝对路径在 upload/ 内：同样放行
        FakeSession.requests = []
        r = parse(await present([str(up / "ok.txt")]))
        check("B4 upload/内绝对路径放行", r["sent"] == ["ok.txt"], str(r))

        # B5 upload/ 子目录里的文件：放行
        (up / "sub").mkdir(exist_ok=True)
        (up / "sub" / "deep.txt").write_text("deep", encoding="utf-8")
        FakeSession.requests = []
        r = parse(await present(["upload/sub/deep.txt"]))
        check("B5 upload/子目录文件放行", r["sent"] == ["deep.txt"], str(r))

    async def t_c_invariants():
        print("\n=== C. 不变式 ===")
        r = parse(await present([]))
        check("C1 空列表早退", r["sent"] == [] and "error" in r, str(r))

        r = parse(await present(["upload/../evil\x00.txt"]))
        f = make_failed_map(r)
        check("C2 null 字节拒绝", not r["sent"] and "invalid path" in f.get("upload/../evil\x00.txt", ""), str(r))

    await t_a_reject_outside_upload()
    await t_b_accept_upload_paths()
    await t_c_invariants()

    return PASS, FAIL, list(_failures)


@pytest.mark.asyncio
async def test_present_files_scope_regressions(monkeypatch):
    """present_files only sends staged files from ``upload/``."""
    global _failures, PASS, FAIL
    _failures = []
    PASS = FAIL = 0
    patch_http(monkeypatch)
    _, failed, failures = await _run_suite()
    assert failed == 0, "\n".join(failures)
