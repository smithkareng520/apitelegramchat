"""健康检查成功的访问日志被过滤；失败与其他路径照常输出。"""

import logging

from core.logging_setup import _HealthCheckAccessFilter


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("hypercorn.access", logging.INFO, __file__, 1, msg, None, None)


def test_drops_health_200():
    f = _HealthCheckAccessFilter()
    assert not f.filter(_record("10.235.27.155:56060 GET /health 1.1 200 16 785"))
    assert not f.filter(_record("10.0.0.1:1 HEAD /health 1.1 200 0 120"))


def test_keeps_health_failure_and_other_paths():
    f = _HealthCheckAccessFilter()
    assert f.filter(_record("10.0.0.1:1 GET /health 1.1 503 21 800"))
    assert f.filter(_record("10.0.0.1:1 POST /webhook 1.1 200 2 900"))
    assert f.filter(_record("10.0.0.1:1 GET /healthz 1.1 200 2 900"))
