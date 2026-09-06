# =====================================================================
# tests/unit/test_tool_result_condense.py — 工具返回「模型视图」精简层
# =====================================================================
# 被测关键路径：工具原始返回 → LLM 上下文的瘦身管线。
# 覆盖：weather 模型视图（hours 参数/字段白名单/截断计数）、subagent 字段
#       清理、amap MCP 输出源头清洗（大字段/空字段/photos）、错误语义逐字
#       保留（熔断依赖前缀匹配，绝不能被改写）。
# =====================================================================
import json

from tool_result_condense import (
    _HOURLY_KEEP,
    condense_amap_payload,
    condense_for_model,
)


def _weather_payload(hourly_count: int = 24) -> dict:
    return {
        "city": "上海",
        "unit": "metric",
        "current": {"temp": 25, "condition": "晴", "humidity": 60,
                    "weather_code": "113", "pressure": 1015},
        "hourly": [
            {"time": f"{h:02d}:00", "temp": 20 + h % 5, "condition": "多云",
             "precip": 0.1, "chance_of_rain": 10, "humidity": 55,
             "wind_speed": 12, "wind_dir": "NE",
             # 以下为应被删除的低价值字段
             "DewPointC": 14, "HeatIndexC": 26, "WindChillC": 23,
             "shortRad": 0.0, "diffRad": 0.0, "chance_of_fog": 0,
             "frost": 0, "overcast": 3, "sunshine": 5, "windy": 0,
             "hightemp": 0, "remdry": 2, "wind_gust": 18, "cloudcover": 50,
             "visibility": 10, "pressure": 1015, "uvIndex": 4}
            for h in range(hourly_count)
        ],
        "daily": [
            {"date": "2026-09-07", "max": 30, "min": 22, "condition": "晴",
             "uvIndex": 5, "sunrise": "05:30", "sunset": "18:10",
             "chance_of_rain": 10,
             # 以下为应被删除的字段
             "moonrise": "23:10", "moonset": "13:45", "moon_phase": "Waxing",
             "moon_illumination": 62, "avg": 26, "chance_of_snow": 0,
             "chance_of_thunder": 0, "chance_of_fog": 0, "frost": 0}
        ],
    }


# ---------------------------------------------------------------------
# 错误语义逐字保留（最高优先级约束）
# ---------------------------------------------------------------------
def test_error_texts_pass_through_verbatim():
    for content in (
        "Error: upstream timeout",
        "Exception: boom",
        "❌ 请求失败",
        "失败：API 限流",
        "失败: bad gateway",
        "⚠️ 部分数据缺失",
    ):
        assert condense_for_model("weather", {"hours": 6}, content) == content
        assert condense_for_model("subagent", None, content) == content


def test_non_json_weather_content_unchanged():
    content = "今天晴，25 度。"
    assert condense_for_model("weather", None, content) == content


def test_empty_content_unchanged():
    assert condense_for_model("weather", None, "") == ""


def test_unknown_tool_unchanged():
    payload = json.dumps({"big": "x" * 1000}, ensure_ascii=False)
    assert condense_for_model("text_editor", None, payload) == payload


# ---------------------------------------------------------------------
# weather 模型视图
# ---------------------------------------------------------------------
def _condense_weather(payload, hours=None):
    return json.loads(condense_for_model(
        "weather", {"hours": hours} if hours is not None else None,
        json.dumps(payload, ensure_ascii=False),
    ))


def test_weather_default_hours_is_six_with_omitted_counter():
    out = _condense_weather(_weather_payload(24))
    assert len(out["hourly"]) == 6
    assert out["hourly_omitted"] == 18


def test_weather_hours_string_and_bounds():
    base = _weather_payload(24)
    assert len(_condense_weather(base, hours="10")["hourly"]) == 10
    assert len(_condense_weather(base, hours=0)["hourly"]) == 1      # 下限 1
    assert len(_condense_weather(base, hours=99)["hourly"]) == 24    # 上限 24
    assert len(_condense_weather(base, hours="abc")["hourly"]) == 6  # 非法回退


def test_weather_hourly_keeps_only_whitelisted_fields():
    out = _condense_weather(_weather_payload())
    for hour in out["hourly"]:
        assert set(hour.keys()) <= set(_HOURLY_KEEP)
        assert "time" in hour and "temp" in hour


def test_weather_current_drops_internal_code():
    out = _condense_weather(_weather_payload())
    assert "weather_code" not in out["current"]
    assert out["current"]["temp"] == 25


def test_weather_daily_drops_astronomy_fields():
    out = _condense_weather(_weather_payload())
    day = out["daily"][0]
    for dropped in ("moon_phase", "moonrise", "moonset", "avg", "chance_of_snow"):
        assert dropped not in day
    for kept in ("date", "max", "min", "chance_of_rain"):
        assert kept in day


def test_weather_city_and_error_preserved():
    payload = _weather_payload(2)
    payload["error"] = "upstream busy"
    out = _condense_weather(payload)
    assert out["city"] == "上海" and out["error"] == "upstream busy"


def test_weather_unrecognized_schema_falls_back_to_original():
    payload = {"unexpected": "shape"}
    out = _condense_weather(payload)
    assert out == payload  # 保底：宁可多给 token 也不能丢数据


def test_weather_error_field_not_hidden_by_schema_fallback():
    payload = {"error": "quota exceeded", "note": "x"}
    assert _condense_weather(payload) == payload


# ---------------------------------------------------------------------
# subagent 模型视图
# ---------------------------------------------------------------------
def test_subagent_drops_echo_fields():
    payload = {
        "task_id": "t-123",
        "task_preview": "父 agent 自己的任务前 80 字回声……",
        "model_name": "GLM-4.6（展示名）",
        "model": "glm-4.6",
        "status": "done",
        "result": "子任务执行结果",
    }
    out = json.loads(condense_for_model("subagent", None, json.dumps(payload, ensure_ascii=False)))
    assert "task_preview" not in out
    assert "model_name" not in out
    assert out["model"] == "glm-4.6"
    assert out["status"] == "done"
    assert out["result"] == "子任务执行结果"


def test_subagent_non_dict_unchanged():
    content = json.dumps([1, 2, 3])
    assert condense_for_model("subagent", None, content) == content


def test_subagent_broken_json_unchanged():
    content = '{"task_id": "t-1", "status": '  # 截断的 JSON
    assert condense_for_model("subagent", None, content) == content


# ---------------------------------------------------------------------
# amap MCP 输出源头清洗
# ---------------------------------------------------------------------
def test_amap_drops_navigation_bulk_fields():
    raw = json.dumps({
        "status": "1", "info": "OK", "infocode": "10000",
        "route": {"paths": [{
            "distance": "12500", "duration": "1800",
            "polyline": "116.48,39.99;116.49,40.00;" * 500,
            "tmcs": [{"distance": "100", "lcode": "G6"}],
        }]},
    }, ensure_ascii=False)
    out = json.loads(condense_amap_payload(raw))
    assert out["status"] == "1" and out["info"] == "OK"
    assert "infocode" not in out
    path = out["route"]["paths"][0]
    assert path["distance"] == "12500"
    assert "polyline" not in path and "tmcs" not in path


def test_amap_removes_empty_values_recursively():
    raw = json.dumps({
        "name": "某餐厅", "biz_ext": {"rating": "", "cost": None},
        "children": [], "parent": [], "photos": [],
        "alias": [], "type": "餐饮",
    }, ensure_ascii=False)
    out = json.loads(condense_amap_payload(raw))
    assert out == {"name": "某餐厅", "type": "餐饮"}


def test_amap_photos_keep_first_url_only():
    raw = json.dumps({
        "pois": [{
            "name": "POI-A",
            "photos": [
                {"url": "https://img.amap.com/first.jpg", "title": "门头"},
                {"url": "https://img.amap.com/second.jpg", "title": "内景"},
            ],
        }],
    }, ensure_ascii=False)
    out = json.loads(condense_amap_payload(raw))
    assert out["pois"][0]["photos"] == [{"url": "https://img.amap.com/first.jpg"}]


def test_amap_non_json_passthrough():
    for raw in ("Error: quota exceeded", "", "x", "```json\n{}\n```"):
        assert condense_amap_payload(raw) == raw


def test_amap_concatenated_json_docs():
    raw = '{"status": "1"}{"status": "0"}'
    out = condense_amap_payload(raw)
    lines = out.split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"status": "1"}
    assert json.loads(lines[1]) == {"status": "0"}


def test_amap_never_raises_on_weird_shapes():
    # 防御性：未知但含有效数据的深层结构原样保留，绝不抛异常
    raw = json.dumps({"deep": {"deeper": [[[{"note": "x"}]]]}}, ensure_ascii=False)
    out = json.loads(condense_amap_payload(raw))
    assert out["deep"]["deeper"][0][0][0] == {"note": "x"}


def test_amap_all_empty_structure_collapses():
    # 全部内容由空值/已删字段构成时递归折叠为 {}（不抛异常）
    raw = json.dumps({"deep": {"deeper": [[[{"polyline": "x"}]]]}} , ensure_ascii=False)
    out = json.loads(condense_amap_payload(raw))
    assert out == {}
