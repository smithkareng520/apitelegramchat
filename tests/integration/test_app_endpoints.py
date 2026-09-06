# =====================================================================
# tests/integration/test_app_endpoints.py — Quart 应用端点集成测试
# =====================================================================
# 被测关键路径：真实 app 模块（完整依赖图）实例化 Quart 应用 → HTTP 端点。
# 覆盖：
#   1. 扁平化重构后整个模块图可正常导入（app.py 及其全部传递依赖）；
#   2. /health 健康检查（对外不泄露内部统计）；
#   3. /webhook 鉴权：缺 token / 错误 token 一律 403，正确 token 放行；
#   4. 双路径鉴权：URL query ?token= 与 X-Telegram-Bot-Api-Secret-Token 头；
#   5. polling 模式下 webhook 投递被忽略且不入队（与生产部署一致）。
# =====================================================================
import asyncio
import json

import pytest

# conftest 已设置测试环境变量（WEBHOOK_TOKEN=it-webhook-secret-token）。
# 导入即验证：重构后全部模块的导入链完整可用。
import app as app_module
from config import INGEST_MODE, WEBHOOK_TOKEN

pytestmark = pytest.mark.filterwarnings(
    "ignore::DeprecationWarning"
)

EXPECTED_TOKEN = WEBHOOK_TOKEN


@pytest.fixture(scope="module")
def client():
    return app_module.app.test_client()


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------
# 模块图完整性
# ---------------------------------------------------------------------
def test_flattened_module_graph_imports():
    """重构后 app 模块图完整导入，Quart 应用实例就绪。"""
    assert app_module.app.name == "app"
    # 关键子模块也应可独立导入（顶层扁平布局）
    import config as _config          # noqa: F401
    import token_budget               # noqa: F401
    import context_window             # noqa: F401
    from mcpserver.server import main  # noqa: F401
    from version import __version__
    assert __version__ == "2.2.0"


# ---------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------
def test_health_returns_ok(client):
    resp = run(client.get("/health"))
    assert resp.status_code == 200
    data = asyncio.run(resp.get_json())
    assert data == {"status": "ok"}


def test_health_head_method(client):
    resp = run(client.head("/health"))
    assert resp.status_code == 200


def test_health_leaks_no_internal_stats(client):
    resp = run(client.get("/health"))
    body = asyncio.run(resp.get_data(as_text=True))
    # 健康端点不得暴露白名单数量/活跃任务数等侧信道信息
    for forbidden in ("whitelist", "task", "count", "worker"):
        assert forbidden not in body.lower()


# ---------------------------------------------------------------------
# /webhook 鉴权
# ---------------------------------------------------------------------
def test_webhook_without_token_forbidden(client):
    resp = run(client.get("/webhook"))
    assert resp.status_code == 403


def test_webhook_with_wrong_token_forbidden(client):
    resp = run(client.get("/webhook?token=definitely-wrong-token"))
    assert resp.status_code == 403


def test_webhook_empty_token_forbidden(client):
    resp = run(client.get("/webhook?token="))
    assert resp.status_code == 403


def test_webhook_correct_token_query_alive(client):
    resp = run(client.get(f"/webhook?token={EXPECTED_TOKEN}"))
    assert resp.status_code == 200
    assert "alive" in asyncio.run(resp.get_data(as_text=True))


def test_webhook_correct_token_via_secret_header(client):
    resp = run(client.get(
        "/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": EXPECTED_TOKEN},
    ))
    assert resp.status_code == 200
    assert "alive" in asyncio.run(resp.get_data(as_text=True))


def test_webhook_wrong_secret_header_forbidden(client):
    resp = run(client.get(
        "/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-header-token"},
    ))
    assert resp.status_code == 403


def test_webhook_token_via_header_wins_over_bad_query(client):
    # 双路径任一匹配即放行：query 错但头正确 → 放行
    resp = run(client.get(
        "/webhook?token=bad",
        headers={"X-Telegram-Bot-Api-Secret-Token": EXPECTED_TOKEN},
    ))
    assert resp.status_code == 200


# ---------------------------------------------------------------------
# polling 模式下的 webhook 投递处理
# ---------------------------------------------------------------------
def test_webhook_post_ignored_in_polling_mode(client):
    assert INGEST_MODE == "polling"
    queue_before = app_module.update_queue.qsize()
    payload = {"update_id": 999001,
               "message": {"chat": {"id": 1}, "text": "hi"}}
    resp = run(client.post(
        f"/webhook?token={EXPECTED_TOKEN}",
        json=payload,
    ))
    assert resp.status_code == 200
    body = asyncio.run(resp.get_data(as_text=True))
    assert "polling" in body
    # 不入队：两条摄取链路并行会造成重复处理
    assert app_module.update_queue.qsize() == queue_before


def test_webhook_get_with_token_and_polling_still_alive(client):
    resp = run(client.head(f"/webhook?token={EXPECTED_TOKEN}"))
    assert resp.status_code == 200
