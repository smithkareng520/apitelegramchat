'''tests/unit/test_tool_result_condense.py — 工具返回「模型视图」精简层
被测关键路径：工具原始返回 → LLM 上下文的去 JSON 化管线。
覆盖：weather / todo / memory / subagent / message_user / present_files /
gaode maps_* 的纯文本模型视图（只给模型回答问题所需信息）、
错误语义保留（熔断依赖前缀匹配，绝不能被改写）、
非法/非 JSON 输入原样透传（宁多勿缺）。'''

import json

from tool_result_condense import condense_for_model


WEATHER = "mcp__internal_search__weather"
TODO = "mcp__internal_todo__todo"
MEMORY = "mcp__internal_memory__memory"
SUBAGENT = "subagent"
GAODE_GEO = "mcp__gaode_mcp__maps_geo"
GAODE_TEXT_SEARCH = "mcp__gaode_mcp__maps_text_search"
GAODE_DETAIL = "mcp__gaode_mcp__maps_search_detail"
GAODE_DRIVE = "mcp__gaode_mcp__maps_direction_driving"
GAODE_DISTANCE = "mcp__gaode_mcp__maps_distance"
GAODE_IP = "mcp__gaode_mcp__maps_ip_location"


def _view(fn_name, fn_args, payload):
    return condense_for_model(fn_name, fn_args, json.dumps(payload, ensure_ascii=False))


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
        assert condense_for_model(GAODE_TEXT_SEARCH, None, content) == content


def test_non_json_weather_content_unchanged():
    content = "今天晴，25 度。"
    assert condense_for_model("weather", None, content) == content


def test_empty_content_unchanged():
    assert condense_for_model("weather", None, "") == ""


def test_broken_json_unchanged():
    # 截断的 JSON：解析失败必须原样透传，绝不改写错误语义。
    content = '{"city": "上海", "current": '
    assert condense_for_model("weather", None, content) == content
    assert condense_for_model(SUBAGENT, None, content) == content
    assert condense_for_model(TODO, None, content) == content


def test_unknown_tool_unchanged():
    payload = json.dumps({"big": "x" * 1000}, ensure_ascii=False)
    assert condense_for_model("text_editor", None, payload) == payload


# ---------------------------------------------------------------------
# weather 模型视图（纯文本）
# ---------------------------------------------------------------------
def _weather_payload(hourly_count: int = 24) -> dict:
    return {
        "city": "上海",
        "unit": "C",
        "current": {"temp": 25, "feels_like": 26, "condition": "晴", "humidity": 60,
                    "wind": 12, "wind_dir": "NE", "precip": 0.0, "visibility": 10,
                    "uvIndex": 5},
        "hourly": [
            {"time": f"{h:02d}:00", "temp": 20 + h % 5, "condition": "多云",
             "precip": 0.1, "chance_of_rain": 10, "humidity": 55,
             "wind_speed": 12}
            for h in range(hourly_count)
        ],
        "daily": [
            {"date": "2026-09-07", "max": 30, "min": 22, "condition": "晴",
             "uvIndex": 5, "sunrise": "05:30", "sunset": "18:10",
             "chance_of_rain": 10}
        ],
    }


def test_weather_text_view_contains_high_value_fields():
    out = condense_for_model(WEATHER, None, json.dumps(_weather_payload(24), ensure_ascii=False))
    assert isinstance(out, str) and "{" not in out  # 去 JSON 化
    assert "上海" in out
    assert "25" in out and "晴" in out            # 当前实况
    assert "逐时" in out and "00:00" in out       # 逐时
    assert "2026-09-07" in out                    # 逐日
    assert "05:30" in out and "18:10" in out      # 日出日落


def test_weather_hours_parameter_limits_hourly_lines():
    out = condense_for_model(WEATHER, {"hours": 3}, json.dumps(_weather_payload(10), ensure_ascii=False))
    hourly_lines = [l for l in out.splitlines() if l.strip().startswith(("0", "1", "2"))
                    and ":00" in l]
    assert len(hourly_lines) == 3
    assert "其余 7 条" in out  # 省略计数，模型知道还有更多


def test_weather_default_hours_is_six():
    out = condense_for_model(WEATHER, None, json.dumps(_weather_payload(9), ensure_ascii=False))
    assert "其余 3 条" in out


def test_weather_error_envelope_becomes_failure_text():
    out = _view(WEATHER, None, {"error": "天气查询超时"})
    assert out == "失败：天气查询超时"


def test_weather_unrecognized_schema_falls_back_to_original():
    payload = {"unexpected": "shape"}
    out = _view(WEATHER, None, payload)
    assert out == json.dumps(payload, ensure_ascii=False)  # 保底：宁可多给 token 也不能丢数据


# ---------------------------------------------------------------------
# todo 模型视图（纯文本）
# ---------------------------------------------------------------------
def test_todo_list_text_view():
    payload = {
        "ok": True, "action": "list", "filter": "all", "total": 3, "pending": 2, "done": 1,
        "todos": [
            {"id": "a1b2c3d4", "title": "归还图书馆书籍", "done": False, "priority": "high",
             "tags": ["学习"], "note": "罚款 5 元", "due_at": "2026-10-08T10:00",
             "due_status": "upcoming", "created_at": 1, "completed_at": None},
            {"id": "e5f6a7b8", "title": "买牛奶", "done": True, "priority": "medium",
             "tags": [], "note": "", "due_at": None, "due_status": "none",
             "created_at": 1, "completed_at": 2},
        ],
    }
    out = _view(TODO, {"action": "list"}, payload)
    assert "{" not in out
    assert "当前共 3 项，未完成 2 项" in out
    assert "id=a1b2c3d4" in out          # id 是模型后续 done/delete 的句柄
    assert "[high]" in out               # 非 medium 优先级保留
    assert "截止 2026-10-08T10:00" in out
    assert "备注：罚款 5 元" in out
    assert "created_at" not in out       # 内部字段不给模型
    assert "completed_at" not in out


def test_todo_add_text_view():
    payload = {
        "ok": True, "action": "add", "changed": ["title"],
        "todo": {"id": "new1", "title": "写周报", "done": False, "priority": "high",
                 "tags": [], "note": "", "due_at": "2026-10-03", "due_status": "due_soon"},
        "total": 4, "pending": 3,
    }
    out = _view(TODO, {"action": "add"}, payload)
    assert "已添加待办" in out and "写周报" in out and "id=new1" in out
    assert "优先级 high" in out and "截止 2026-10-03" in out
    assert "当前共 4 项，未完成 3 项" in out
    assert "changed" not in out          # 修改字段列表对 add 是噪音


def test_todo_toggle_done_text_view():
    payload = {
        "ok": True, "action": "toggle", "changed": True,
        "todo": {"id": "t1", "title": "买牛奶", "done": True, "priority": "medium",
                 "tags": [], "note": "", "due_at": None, "due_status": "none"},
        "total": 5, "pending": 2,
    }
    out = _view(TODO, {"action": "done"}, payload)
    assert "已完成待办" in out and "买牛奶" in out and "id=t1" in out
    assert "当前共 5 项，未完成 2 项" in out


def test_todo_error_envelope_becomes_failure_text():
    out = _view(TODO, {"action": "done"}, {"ok": False, "error": "找不到 id 为 x 的待办", "code": "not_found"})
    assert out == "失败：找不到 id 为 x 的待办"


# ---------------------------------------------------------------------
# memory 模型视图（纯文本）
# ---------------------------------------------------------------------
def test_memory_list_text_view():
    payload = {
        "ok": True, "action": "search", "total": 12, "shown": 1,
        "memories": [{"id": "m1", "content": "用户对花生过敏", "category": "fact",
                      "tags": ["健康", "过敏"], "importance": "high",
                      "created_at": 1, "updated_at": 2, "source": "agent"}],
    }
    out = _view(MEMORY, {"action": "search", "query": "过敏"}, payload)
    assert "{" not in out
    assert "用户对花生过敏" in out and "id=m1" in out
    assert "[high][fact]" in out
    assert "标签 #健康 #过敏" in out
    assert "created_at" not in out and "source" not in out


def test_memory_add_text_view():
    payload = {
        "ok": True, "action": "add",
        "memory": {"id": "m2", "content": "用户偏好简洁回复", "category": "preference",
                   "tags": [], "importance": "medium", "created_at": 1, "updated_at": 1,
                   "source": "agent"},
        "total": 13,
    }
    out = _view(MEMORY, {"action": "add"}, payload)
    assert "已保存记忆" in out and "用户偏好简洁回复" in out and "id=m2" in out
    assert "当前共 13 条记忆" in out


def test_memory_clear_text_view():
    payload = {"ok": True, "action": "clear", "scope": "all", "removed": 5,
               "message": "已清空全部 5 条记忆", "total": 0}
    out = _view(MEMORY, {"action": "clear"}, payload)
    assert "已清空全部 5 条记忆" in out and "当前共 0 条记忆" in out


# ---------------------------------------------------------------------
# subagent 模型视图（纯文本）
# ---------------------------------------------------------------------
def test_subagent_success_text_view_drops_echo_fields():
    payload = {
        "ok": True, "rounds": 3, "tool_calls": 5, "elapsed": 42.2,
        "model": "glm-4.6", "model_name": "GLM-4.6（展示名）",
        "task_preview": "父 agent 自己的任务回声……",
        "answer": "结论：这样那样。",
    }
    out = _view(SUBAGENT, None, payload)
    assert "{" not in out
    assert "task_preview" not in out and "model_name" not in out
    assert "glm-4.6" in out and "3 轮" in out and "5 次工具调用" in out and "42s" in out
    assert "结论：这样那样。" in out


def test_subagent_failure_text_view():
    payload = {"ok": False, "error": "子 agent 整体超时（900s）", "code": "timeout",
               "rounds": 2, "tool_calls": 3, "model": "glm-4.6"}
    out = _view(SUBAGENT, None, payload)
    assert out.startswith("失败：")
    assert "超时" in out and "2 轮" in out


def test_subagent_non_dict_unchanged():
    content = json.dumps([1, 2, 3])
    assert condense_for_model(SUBAGENT, None, content) == content


# ---------------------------------------------------------------------
# message_user 回答模型视图（纯文本）
# ---------------------------------------------------------------------
def test_message_user_choice_answer():
    payload = {"type": "choice", "question": "选哪个？",
               "selected": [{"label": "方案A", "description": ""}, {"label": "方案B"}]}
    out = condense_for_model("message_user", {}, json.dumps(payload, ensure_ascii=False))
    assert out == "用户选择了：方案A、方案B"


def test_message_user_custom_and_cancelled():
    assert condense_for_model(
        "message_user", {}, json.dumps({"type": "custom", "value": "都不行，换个思路"})) \
        == "用户的回答：都不行，换个思路"
    assert condense_for_model(
        "message_user", {}, json.dumps({"type": "cancelled"})).startswith("用户取消")
    expired = condense_for_model("message_user", {}, json.dumps({"type": "expired"}))
    assert "没有回复" in expired


# ---------------------------------------------------------------------
# present_files 模型视图（纯文本）
# ---------------------------------------------------------------------
def test_present_files_success_and_failure():
    payload = {"sent": ["report.pdf", "data.csv"],
               "failed": ["big.zip (file too large: 999 bytes)"]}
    out = condense_for_model("present_files", {}, json.dumps(payload))
    assert "已发送 2 个文件：report.pdf、data.csv" in out
    assert "发送失败 1 个" in out and "big.zip" in out


def test_present_files_error_envelope():
    payload = {"sent": [], "failed": [], "error": "No paths provided."}
    out = condense_for_model("present_files", {}, json.dumps(payload))
    assert out == "失败：No paths provided."


# ---------------------------------------------------------------------
# gaode maps 模型视图（纯文本；渲染/遥测字段绝不出现）
# ---------------------------------------------------------------------
def test_amap_geo_text_view_drops_admin_codes():
    payload = {"status": "1", "geocodes": [{
        "formatted_address": "广东省深圳市南山区", "country": "中国",
        "province": "广东省", "city": "深圳市", "district": "南山区",
        "level": "区县", "location": "113.93029,22.53332", "adcode": "440305",
        "pcode": "440000", "citycode": "0755", "gridcode": "x"}]}
    out = _view(GAODE_GEO, {"address": "深圳南山"}, payload)
    assert "{" not in out
    assert "南山区" in out and "113.9303,22.5333" in out
    for dropped in ("adcode", "pcode", "citycode", "gridcode"):
        assert dropped not in out


def test_amap_text_search_text_view():
    payload = {"status": "1", "count": "24", "pois": [{
        "id": "B000A8UIN8", "name": "肯德基（朝阳门店）", "address": "朝阳区建国路1号",
        "type": "餐饮服务;快餐厅", "location": "116.456789,39.912345",
        "tel": "010-12345678", "typecode": "050300",
        "photos": [{"url": "https://img.example.com/a.jpg"}],
        "biz_ext": {"rating": "4.5", "cost": "35", "meal_ordering": "0"}}]}
    out = _view(GAODE_TEXT_SEARCH, {"keywords": "肯德基"}, payload)
    assert "肯德基（朝阳门店）" in out and "朝阳区建国路1号" in out
    assert "id=B000A8UIN8" in out            # 后续 maps_search_detail 需要 id
    assert "4.5" in out and "35" in out      # biz_ext 高价值字段提升
    assert "photos" not in out and "typecode" not in out and "meal_ordering" not in out
    assert "116.4568,39.9123" in out         # 坐标压缩到 4 位小数


def test_amap_poi_list_caps_at_ten_with_omitted_counter():
    payload = {"status": "1", "pois": [
        {"id": f"P{i}", "name": f"POI-{i}", "location": "116.4,39.9"} for i in range(13)]}
    out = _view(GAODE_TEXT_SEARCH, {"keywords": "x"}, payload)
    assert "其余 3 条已省略" in out


def test_amap_detail_text_view():
    payload = {"id": "B0FFF", "name": "某餐厅", "address": "某路1号",
               "location": "116.48,39.99", "biz_ext": {"rating": "4.7", "cost": "55"},
               "photos": [{"url": "https://img.example.com/a.jpg"}]}
    out = _view(GAODE_DETAIL, {"id": "B0FFF"}, payload)
    assert "POI 详情" in out and "某餐厅" in out and "4.7" in out
    assert "photos" not in out


def test_amap_direction_text_view_drops_polyline():
    payload = {"status": "1", "route": {
        "origin": "116.456789,39.912345", "destination": "116.589012,40.123456",
        "paths": [{"distance": "12300", "duration": "1500",
                   "polyline": "116.48,39.99;116.49,40.00;" * 500,
                   "tmcs": [{"distance": "100"}],
                   "steps": [{"instruction": "沿建国路向东行驶500米右转", "road": "建国路",
                              "distance": "500", "polyline": "x;y"}]}]}}
    out = _view(GAODE_DRIVE, {}, payload)
    assert "驾车路线" in out
    assert "12.3km" in out and "25分钟" in out
    assert "沿建国路向东行驶500米右转" in out
    assert "polyline" not in out and "tmcs" not in out


def test_amap_direction_failure_keeps_status_zero_reason():
    payload = {"status": "0", "info": "INVALID_USER_KEY"}
    out = _view(GAODE_DRIVE, {}, payload)
    assert out.startswith("失败：") and "INVALID_USER_KEY" in out


def test_amap_distance_text_view():
    payload = {"status": "1", "results": [
        {"origin_id": "116.4,39.9", "dest_id": "116.5,40.0",
         "distance": "12300", "duration": "1500"}]}
    out = _view(GAODE_DISTANCE, {}, payload)
    assert "12.3km" in out and "25分钟" in out


def test_amap_ip_location_text_view():
    out = _view(GAODE_IP, {}, {"province": "北京市", "city": "北京市",
                               "adcode": "110105", "rectangle": "116.1,39.7;116.6,40.1"})
    assert out == "IP 归属地：北京市 北京市"
    assert "rectangle" not in out and "adcode" not in out


def test_amap_non_json_passthrough():
    for raw in ("Error: quota exceeded", "", "x", "```json\n{}\n```"):
        assert condense_for_model(GAODE_TEXT_SEARCH, None, raw) == raw


def test_amap_never_raises_on_weird_shapes():
    # 防御性：未知形状退回 JSON 文本，绝不抛异常
    raw = json.dumps({"deep": {"deeper": [[[{"note": "x"}]]]}}, ensure_ascii=False)
    out = condense_for_model(GAODE_TEXT_SEARCH, None, raw)
    assert out == raw
