'''tests/unit/test_model_view_and_tool_status.py'''


import asyncio
import json

from ai.rich_message_builder import RichMessageBuilder
from ai.tool_summary import (
    _generate_action_description,
    _generate_initial_tool_summary,
    _generate_tool_summary_done,
)
from tool_result_format import format_tool_result


# ---------------------------------------------------------------------
# 折叠块进行态：对象信息进入标题 / 此前缺失状态的工具有专属文案
# ---------------------------------------------------------------------
def test_running_summaries_carry_object_context():
    assert _generate_initial_tool_summary("weather", {"city": "北京"}) == "Fetching weather for 北京"
    assert _generate_initial_tool_summary("weather", {}) == "Fetching weather"
    assert _generate_initial_tool_summary("exchange_rate", {"base": "usd", "target": "cny"}) \
        == "Checking exchange rate: USD → CNY"
    assert _generate_initial_tool_summary("exchange_rate", {"base": "USD"}) == "Checking USD exchange rates"
    assert _generate_initial_tool_summary("wikipedia", {"query": "可塑性记忆"}) \
        == "Looking up 可塑性记忆 on Wikipedia"
    assert _generate_initial_tool_summary("subagent", {"task": "调研新能源汽车市场"}) \
        == "Running a subagent: 调研新能源汽车市场"
    assert _generate_initial_tool_summary("subagent", {}) == "Running a subagent"
    assert _generate_initial_tool_summary("generate_video", {"prompt": "一只猫在弹钢琴"}) \
        == "Generating a video: 一只猫在弹钢琴"


def test_running_summaries_for_previously_statusless_tools():
    # bash 后台任务模式此前与前台命令共用 "Running command"
    assert _generate_initial_tool_summary("bash", {"task_action": "status", "task_id": "t"}) \
        == "Checking background task"
    assert _generate_initial_tool_summary("bash", {"task_action": "list"}) == "Listing background tasks"
    assert _generate_initial_tool_summary("bash", {"task_action": "stop", "task_id": "t"}) \
        == "Stopping background task"
    assert _generate_initial_tool_summary("bash", {"command": "npm test", "run_in_background": True}) \
        == "Starting background: npm test"
    assert _generate_initial_tool_summary("bash", {"command": "npm test"}) == "npm test"
    # maps_ip_location 此前落到通用 "Running..."
    assert _generate_initial_tool_summary("maps_ip_location", {"ip": "1.2.3.4"}) == "Locating IP origin"
    # 路线/距离工具带起讫点（坐标压缩到 4 位小数）
    assert _generate_initial_tool_summary(
        "maps_direction_driving",
        {"origin": "116.456789,39.912345", "destination": "116.589012,40.123456"},
    ) == "Planning driving route: 116.4568,39.9123 → 116.589,40.1235"
    assert _generate_initial_tool_summary(
        "maps_distance", {"origins": "116.45,39.91", "destinations": "116.58,40.12"}) \
        == "Measuring distance: 116.45,39.91 → 116.58,40.12"
    # present_files 带文件名
    assert _generate_initial_tool_summary("present_files", {"paths": ["upload/report.pdf"]}) \
        == "Presenting 1 file: report.pdf"
    assert _generate_initial_tool_summary(
        "present_files", {"paths": ["upload/a.pdf", "upload/b.csv", "upload/c.txt"]}) \
        == "Presenting 3 files: a.pdf、b.csv 等"


def test_action_description_includes_subagent_task():
    assert _generate_action_description("subagent", {"task": "写周报"}) \
        == "delegating to a subagent: 写周报"


# ---------------------------------------------------------------------
# 折叠块完成态：结果要点进入标题
# ---------------------------------------------------------------------
def test_done_weather_summary_from_result_payload():
    result = json.dumps({"city": "北京", "unit": "C",
                         "current": {"temp": "25", "condition": "多云"}}, ensure_ascii=False)
    assert _generate_tool_summary_done("weather", {"city": "北京"}, result) \
        == "Fetched weather: 北京 25°C 多云"
    assert _generate_tool_summary_done("weather", {}, "not-json") == "Fetched weather"
    error_result = json.dumps({"error": "天气查询超时"}, ensure_ascii=False)
    assert _generate_tool_summary_done("weather", {}, error_result) == "Fetched weather"


def test_done_exchange_rate_summary_extracts_rate():
    result = "<b>汇率查询成功</b><br/>1 USD = 7.2400 CNY<br/>更新时间：x"
    assert _generate_tool_summary_done("exchange_rate", {"base": "USD", "target": "CNY"}, result) \
        == "Checked exchange rate USD → CNY: 7.2400"
    assert _generate_tool_summary_done("exchange_rate", {"base": "USD", "target": "CNY"}, "no rate") \
        == "Checked exchange rate USD → CNY"
    assert _generate_tool_summary_done("exchange_rate", {"base": "USD"}, result) \
        == "Checked USD exchange rates"


def test_done_route_summaries_carry_distance_and_duration():
    route = json.dumps({"route": {"origin": "a", "destination": "b",
                                  "paths": [{"distance": "12300", "duration": "1500"}]}})
    assert _generate_tool_summary_done("maps_direction_driving", {}, route) \
        == "Planned a driving route · 12.3 km · 25 min"
    transit = json.dumps({"route": {"origin": "a", "destination": "b",
                                    "transits": [{"duration": "3300", "walking_distance": "800"}]}})
    assert _generate_tool_summary_done("maps_direction_transit_integrated", {}, transit) \
        == "Planned a transit route · 55 min"
    distance = json.dumps({"results": [{"distance": "12300", "duration": "1500"}]})
    assert _generate_tool_summary_done("maps_distance", {}, distance) \
        == "Measured a distance · 12.3 km · 25 min"
    # 非法 JSON：退回基础文案
    assert _generate_tool_summary_done("maps_direction_walking", {}, "not-json") \
        == "Planned a walking route"


def test_done_poi_detail_summary_carries_name():
    result = json.dumps({"id": "B0FFF", "name": "肯德基（朝阳门店）", "address": "某路1号"},
                        ensure_ascii=False)
    assert _generate_tool_summary_done("maps_search_detail", {"id": "B0FFF"}, result) \
        == "Fetched POI details: 肯德基（朝阳门店）"


def test_done_present_files_summary_reflects_real_outcome():
    result = json.dumps({"sent": ["report.pdf", "data.csv", "notes.txt"], "failed": []})
    assert _generate_tool_summary_done("present_files", {"paths": ["x"]}, result) \
        == "Sent 3 files (report.pdf, data.csv…)"
    partial = json.dumps({"sent": ["a.pdf"], "failed": ["b.zip (too large)"]})
    assert _generate_tool_summary_done("present_files", {"paths": ["a", "b"]}, partial) \
        == "Sent 1 file (a.pdf), 1 failed"


# ---------------------------------------------------------------------
# 工具组：单条目组标题复用条目详情摘要；bash 后台任务组标题
# ---------------------------------------------------------------------
def _builder_with_item(fn_name: str, fn_args: dict, summary: str = "") -> RichMessageBuilder:
    builder = RichMessageBuilder(chat_id=1)
    builder.add_tool_item("tc1", fn_name, summary or f"{fn_name} placeholder", fn_args=fn_args)
    return builder


def test_group_running_title_uses_object_context():
    builder = _builder_with_item("weather", {"city": "北京"})
    group = builder._tool_groups[0]
    assert group["outer_summary"] == "Fetching weather for 北京"

    builder = _builder_with_item("exchange_rate", {"base": "usd", "target": "cny"})
    assert builder._tool_groups[0]["outer_summary"] == "Checking exchange rate: USD → CNY"

    builder = _builder_with_item("subagent", {"task": "调研任务"})
    assert builder._tool_groups[0]["outer_summary"] == "Running a subagent: 调研任务"

    builder = _builder_with_item("bash", {"task_action": "status", "task_id": "t"})
    assert builder._tool_groups[0]["outer_summary"] == "Checking background task"

    builder = _builder_with_item("bash", {"command": "npm test", "run_in_background": True})
    assert builder._tool_groups[0]["outer_summary"] == "Starting background: npm test"


def test_group_done_title_reuses_single_item_summary():
    builder = _builder_with_item("weather", {"city": "北京"})
    result = json.dumps({"city": "北京", "unit": "C",
                         "current": {"temp": "25", "condition": "多云"}}, ensure_ascii=False)
    builder.update_tool_item("tc1", _generate_tool_summary_done("weather", {"city": "北京"}, result),
                             "<p>ok</p>", status="done")
    builder.finish_group(0)
    assert builder._tool_groups[0]["outer_summary"] == "Fetched weather: 北京 25°C 多云"


# ---------------------------------------------------------------------
# weather 工具源头瘦身 + UI 卡片兼容
# ---------------------------------------------------------------------
def test_weather_tool_payload_contract():
    # execute_weather 依赖网络；这里验证其载荷字段契约与消费者一致：
    # 模型视图 / UI 卡片消费的字段 ⊆ 工具生产的字段（源头瘦身不加无用字段）。
    from search.quick_lookup import _HOURLY_FIELDS, _DAILY_FIELDS
    assert set(_HOURLY_FIELDS) == {"time", "temp", "condition", "precip", "humidity",
                                   "wind_speed", "chance_of_rain"}
    assert set(_DAILY_FIELDS) == {"date", "max", "min", "condition", "uvIndex",
                                  "sunrise", "sunset", "chance_of_rain"}


def test_weather_ui_card_renders_lean_payload():
    lean_payload = json.dumps({
        "city": "北京", "unit": "C",
        "current": {"temp": "25", "feels_like": "26", "humidity": "40", "wind": "12",
                    "wind_gust": "20", "pressure": "1010", "visibility": "10",
                    "cloudcover": "50", "uvIndex": "5", "precip": "0.0",
                    "wind_dir": "NE", "wind_deg": "45", "condition": "多云",
                    "obs_time": "2026-10-02 14:30"},
        "hourly": [{"time": "14:00", "temp": "25", "condition": "多云", "precip": "0.1",
                    "humidity": "40", "wind_speed": "12", "chance_of_rain": "10"}],
        "daily": [{"date": "2026-10-02", "max": "28", "min": "19", "condition": "多云",
                   "uvIndex": "5", "sunrise": "06:12", "sunset": "18:04",
                   "chance_of_rain": "10"}],
    }, ensure_ascii=False)
    summary, details = asyncio.run(format_tool_result("weather", {"city": "北京"}, lean_payload))
    assert "北京" in summary and "25" in summary
    # 主表字段仍渲染
    assert "日出" in details and "06:12" in details
    assert "14:00" in details and "降水概率" in details
    # 瘦身后的载荷不应再渲染月相/露点等额外表格（字段已不存在）
    assert "月相" not in details and "露点" not in details
    # 错误路径不受影响
    summary, details = asyncio.run(format_tool_result(
        "weather", {"city": "北京"}, json.dumps({"error": "天气查询超时"}, ensure_ascii=False)))
    assert "失败" in summary or "天气查询失败" in summary
    assert "天气查询超时" in details
