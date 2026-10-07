# -*- coding: utf-8 -*-
'''ModelScope 图像接口"软失败"识别回归测试（2026-10-07 生产事故）。'''

import asyncio
import base64
import json


import ai.media_generation as mg
from core.images import ImageTaskResult

# 1x1 透明 PNG
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_1X1).decode("ascii")


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": "application/json"}

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """按脚本返回 (status, body_dict) 的最小 aiohttp 会话替身，记录全部调用。"""

    def __init__(self, script):
        self._script = script          # callable(method, url) -> (status, dict|str)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        status, body = self._script(method, url)
        text = body if isinstance(body, str) else json.dumps(body)
        return _FakeResp(status, text)


def _run_modelscope(monkeypatch, script, image_urls=None, prompt="edit this"):
    """跑 _request_modelscope_native_image（session 已打桩），返回五元组与假会话。

    必须用 monkeypatch.setattr 打桩 aiohttp.ClientSession：直接赋值会
    污染全局 aiohttp 模块，泄漏到后续测试（test_stream_robustness 等）。
    """
    session = _FakeSession(script)

    class _SessionFactory:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(mg.aiohttp, "ClientSession", _SessionFactory)
    result = asyncio.run(mg._request_modelscope_native_image(
        prompt=prompt,
        image_urls=image_urls if image_urls is not None else [PNG_DATA_URL],
        num_images=1,
        model="Qwen/Qwen-Image-Edit",
    ))
    return result, session


def test_post_200_error_payload_surfaces_real_message(monkeypatch):
    """POST 200 + errors 对象（无 task_id）：必须透出真实错误信息。"""
    monkeypatch.setattr(mg, "_shrink_data_url", lambda u: u)

    def script(method, url):
        if method == "POST":
            return 200, {"errors": {"code": "STI_POLICY_REJECT", "message": "图片涉嫌违规内容，已拒绝处理"}}
        return 500, {"error": "unexpected GET"}

    (response_json, endpoint, detail, status, request_id), session = _run_modelscope(monkeypatch, script)

    assert response_json is None
    assert status == 200
    assert "图片涉嫌违规内容" in detail
    # 只打了一趟 POST（编辑路径），绝不继续轮询
    assert [c[0] for c in session.calls] == ["POST"]


def test_post_200_code_message_payload_surfaces_real_message(monkeypatch):
    """POST 200 + {code, message} 形状（无 task_id）：同样透出真实原因。"""
    monkeypatch.setattr(mg, "_shrink_data_url", lambda u: u)

    def script(method, url):
        return 200, {"code": "InvalidParameter", "message": "image_url format not supported"}

    (response_json, _endpoint, detail, status, _req), _session = _run_modelscope(monkeypatch, script)

    assert response_json is None
    assert status == 200
    assert "InvalidParameter" in detail
    assert "image_url format" in detail


def test_post_200_failed_status_without_task_id(monkeypatch):
    """POST 200 + task_status=FAILED（无 task_id）：报任务失败而非空响应。"""
    monkeypatch.setattr(mg, "_shrink_data_url", lambda u: u)

    def script(method, url):
        return 200, {"task_status": "FAILED"}

    (_rj, _ep, detail, _status, _req), _session = _run_modelscope(monkeypatch, script)

    assert "任务执行失败" in detail


def test_post_200_unrecognized_payload_reports_shape(monkeypatch):
    """POST 200 + 无法识别的无任务形状：显式失败并携带响应形状预览。"""
    monkeypatch.setattr(mg, "_shrink_data_url", lambda u: u)

    def script(method, url):
        return 200, {"foo": "bar", "created": 123}

    (_rj, _ep, detail, _status, _req), _session = _run_modelscope(monkeypatch, script)

    assert "响应未包含任务ID与图片数据" in detail
    assert "foo=bar" in detail
    assert "created=123" in detail


def test_text_to_image_soft_failure_also_surfaces_message(monkeypatch):
    """纯文生图路径同样识别 200 错误载荷（无参考图分支）。"""
    def script(method, url):
        return 200, {"errors": {"code": "THROTTLED", "message": "请求过于频繁"}}

    session = _FakeSession(script)

    class _SessionFactory:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(mg.aiohttp, "ClientSession", _SessionFactory)
    response_json, _ep, detail, status, _req = asyncio.run(
        mg._request_modelscope_native_image(
            prompt="a golden cat", image_urls=[], num_images=1, model="Qwen/Qwen-Image"))

    assert response_json is None
    assert status == 200
    assert "请求过于频繁" in detail


def test_happy_path_poll_succeed_with_output_images(monkeypatch):
    """正常链路不回归：POST task_id -> 轮询 SUCCEED + output_images -> 成功。"""
    monkeypatch.setattr(mg, "_shrink_data_url", lambda u: u)

    def script(method, url):
        if method == "POST":
            return 200, {"task_id": "task-ok-1", "task_status": "PENDING"}
        assert url.endswith("/tasks/task-ok-1")
        return 200, {"task_id": "task-ok-1", "task_status": "SUCCEED",
                     "output_images": ["https://sns-img.modelscope.cn/result.png"]}

    async def _fast_sleep(*a, **k):
        return None

    monkeypatch.setattr(mg.asyncio, "sleep", _fast_sleep)
    (response_json, endpoint, detail, status, _req), session = _run_modelscope(monkeypatch, script)

    assert response_json["task_status"] == "SUCCEED"
    assert response_json["output_images"] == ["https://sns-img.modelscope.cn/result.png"]
    assert detail == ""
    assert status == 200
    assert [c[0] for c in session.calls] == ["POST", "GET"]


def test_poll_succeed_without_images_fails_fast(monkeypatch):
    """轮询 SUCCEED 但无图片：短暂补查（3 次内）快速失败，不空转到超时。"""
    monkeypatch.setattr(mg, "_shrink_data_url", lambda u: u)

    def script(method, url):
        if method == "POST":
            return 200, {"task_id": "task-empty-1", "task_status": "PENDING"}
        return 200, {"task_id": "task-empty-1", "task_status": "SUCCEED", "metrics": {"total_time": 12.3}}

    async def _fast_sleep(*a, **k):
        return None

    monkeypatch.setattr(mg.asyncio, "sleep", _fast_sleep)
    (response_json, _ep, detail, status, _req), session = _run_modelscope(monkeypatch, script)

    assert response_json is None
    assert status == 200
    assert "未返回图片" in detail
    assert "SUCCEED" in detail
    gets = [c for c in session.calls if c[0] == "GET"]
    assert len(gets) == 3          # 首次 + 2 次补查，绝不轮到 240s 超时


def test_poll_timeout_returns_explicit_error(monkeypatch):
    """轮询超时：显式报"轮询超时 + 最后状态"，不再把 last_poll_json 当成功。"""
    monkeypatch.setattr(mg, "_shrink_data_url", lambda u: u)
    monkeypatch.setattr(mg, "_MODELSCOPE_POLL_TIMEOUT_SECONDS", 0)

    def script(method, url):
        if method == "POST":
            return 200, {"task_id": "task-slow-1", "task_status": "PENDING"}
        return 200, {"task_id": "task-slow-1", "task_status": "RUNNING"}

    async def _fast_sleep(*a, **k):
        return None

    monkeypatch.setattr(mg.asyncio, "sleep", _fast_sleep)
    (response_json, _ep, detail, status, _req), _session = _run_modelscope(monkeypatch, script)

    assert response_json is None
    assert status == 200
    assert "轮询超时" in detail
    # deadline=0 时循环未执行，last_status 取 POST 初始响应的 PENDING
    assert "PENDING" in detail


def test_edit_reference_images_are_shrunk(monkeypatch):
    """编辑路径的参考图 data URL 必须经过 _shrink_data_url（对齐通用路径）。"""
    seen = []

    def fake_shrink(data_url):
        seen.append(data_url)
        return data_url

    monkeypatch.setattr(mg, "_shrink_data_url", fake_shrink)

    def script(method, url):
        if method == "POST":
            return 200, {"errors": {"code": "X", "message": "stop early"}}
        return 500, {}

    (_rj, _ep, detail, _status, _req), _session = _run_modelscope(monkeypatch, script)

    assert seen == [PNG_DATA_URL]   # 参考图进入请求前经过压缩管道
    assert "stop early" in detail   # 提前短路不影响断言链路


def test_openai_images_task_sets_no_items_detail(monkeypatch):
    """Images 协议出口：空响应必须携带 no_items_detail（响应形状可诊断）。"""
    async def fake_request(*args, **kwargs):
        return {"task_status": "PENDING"}, "/images/generations", "", 200, "req-1"

    monkeypatch.setattr("ai.media_generation._request_images_generations", fake_request)

    from core.images import ImageTask
    task = ImageTask.generate("test", model="Qwen/Qwen-Image")
    result = asyncio.run(mg._request_openai_images_task(task))

    assert result.images == []
    assert result.diagnostics == []
    assert "task_status=PENDING" in result.no_items_detail
    detail = result.empty_detail()
    assert "没有图片数据" in detail
    assert "响应顶层字段" in detail
    assert "内容审核" in detail


def test_empty_detail_precedence_and_back_compat():
    """no_items_detail 优先于 diagnostics；默认文案向后兼容。"""
    assert ImageTaskResult().empty_detail() == "接口返回成功，但响应里没有图片数据。"
    assert ImageTaskResult(no_items_detail="X", diagnostics=["Y"]).empty_detail() == "X"
    detail = ImageTaskResult(diagnostics=["图片 #1（oss.example）：下载失败，HTTP 403"]).empty_detail()
    assert "下载/校验失败" in detail and "HTTP 403" in detail


def test_tool_path_error_carries_real_reason(monkeypatch):
    """端到端（工具路径）：execute_generate_image 报错带真实原因，不再黑盒。"""
    import search.media_tools as tools

    def script(method, url):
        if method == "POST":
            return 200, {"errors": {"code": "STI_POLICY_REJECT", "message": "图片涉嫌违规内容，已拒绝处理"}}
        return 500, {}

    session = _FakeSession(script)

    class _SessionFactory:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(mg.aiohttp, "ClientSession", _SessionFactory)

    out = asyncio.new_event_loop().run_until_complete(
        tools.execute_generate_image(
            "把两个人改成倾城倾国的美女", "Qwen/Qwen-Image-Edit", image_url=PNG_DATA_URL))

    assert "图片涉嫌违规内容" in out
    assert "没有图片数据" not in out


def test_shape_preview_never_leaks_base64():
    """形状预览绝不展开 data: / 超长值，避免把 base64 塞进报错与日志。"""
    huge = "data:image/png;base64," + "A" * 100000
    preview = mg._response_shape_preview({"image_url": [huge], "b64_json": huge, "small": "ok"})
    assert "base64" not in preview
    assert "A" * 20 not in preview
    assert "small=ok" in preview
