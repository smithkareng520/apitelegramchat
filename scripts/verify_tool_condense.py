"""工具返回「模型视图」精简的行为级验证脚本。

运行：python3 scripts/verify_tool_condense.py
覆盖：
  1. condense_for_model — weather：hours 参数生效（默认 6 / clamp 1-24）、
     逐时/逐日字段白名单（无月相、露点、热指数、风寒、低频概率）、
     错误 JSON / 非 JSON / 错误前缀文本原样透传
  2. condense_for_model — subagent：task_preview / model_name 回声字段被删，
     answer / rounds / tool_calls / elapsed / model 保留
  3. condense_for_model — 其他工具与非 JSON 内容原样返回（不误伤）
  4. condense_amap_payload — POI / 路线 / 距离结构：polyline / tmcs / 空值
     字段删除，UI 依赖字段全保留，photos 只留第一张 URL，错误状态可判定
  5. 源头集成 — _call_amap_mcp 走清洗、execute_present_files 无 error:null、
     死代码 _geocode_coords 已删除
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


# ---------------------------------------------------------------------
# 构造与 wttr.in / amap-maps MCP 返回形状一致的样例
# ---------------------------------------------------------------------

def _weather_payload(hours_count: int = 24) -> dict:
    hourly = []
    for i in range(hours_count):
        hourly.append({
            "time": f"{i:02d}:00",
            "temp": "20",
            "condition": "Sunny",
            "precip": "0.0",
            "humidity": "40",
            "pressure": "1015",
            "wind_gust": "12",
            "uvIndex": "3",
            "cloudcover": "10",
            "visibility": "10",
            "wind_speed": "8",
            "wind_dir": "NE",
            "chance_of_rain": "10",
            "chance_of_snow": "0",
            "chance_of_thunder": "0",
            "chance_of_fog": "0",
            "chance_of_frost": "0",
            "chance_of_overcast": "0",
            "chance_of_sunshine": "80",
            "chance_of_windy": "0",
            "chance_of_hightemp": "0",
            "chance_of_remdry": "90",
            "DewPointC": "7",
            "HeatIndexC": "20",
            "WindChillC": "19",
            "shortRad": "100",
            "diffRad": "50",
        })
    daily = []
    for d in range(3):
        daily.append({
            "date": f"2026-08-2{d + 6}",
            "max": "30",
            "min": "20",
            "avg": "25",
            "condition": "Partly cloudy",
            "uvIndex": "6",
            "sunrise": "05:40",
            "sunset": "18:45",
            "moonrise": "20:00",
            "moonset": "05:00",
            "moon_phase": "Waxing Gibbous",
            "moon_illumination": "80",
            "chance_of_rain": "20",
            "chance_of_snow": "0",
            "chance_of_thunder": "10",
            "chance_of_fog": "0",
            "chance_of_frost": "0",
        })
    return {
        "city": "Beijing",
        "unit": "C",
        "current": {
            "temp": "24", "feels_like": "25", "humidity": "42", "wind": "10",
            "wind_gust": "15", "pressure": "1012", "visibility": "10",
            "cloudcover": "20", "uvIndex": "5", "precip": "0.0",
            "wind_dir": "NE", "wind_deg": "45", "condition": "Clear",
            "weather_code": "113", "obs_time": "2026-08-29 10:00",
        },
        "hourly": hourly,
        "daily": daily,
    }


def _amap_poi_payload() -> dict:
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "count": "2",
        "pois": [
            {
                "parent": [],
                "biz_type": [],
                "poi_tag": [],
                "name": "全聚德烤鸭店(前门店)",
                "address": "北京市东城区前门大街30号",
                "location": "116.397,39.899",
                "distance": "123",
                "alias": "全聚德",
                "type": "餐饮服务;中餐厅;北京菜",
                "typecode": "050000",
                "id": "B000A7BD6C",
                "tel": [],
                "photos": [
                    {"url": "https://example.com/1.jpg", "title": []},
                    {"url": "https://example.com/2.jpg", "title": []},
                    {"url": "https://example.com/3.jpg", "title": []},
                ],
                "biz_ext": {"rating": "4.5", "cost": []},
            },
            {
                "name": "便宜坊烤鸭店",
                "address": "北京市东城区崇文门外大街",
                "location": "116.418,39.893",
                "distance": "850",
                "type": "餐饮服务;中餐厅",
                "typecode": "050000",
                "id": "B000A8N2MG",
                "photos": [],
            },
        ],
    }


def _amap_route_payload() -> dict:
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "route": {
            "origin": "116.397,39.899",
            "destination": "116.418,39.893",
            "paths": [
                {
                    "distance": "2500",
                    "duration": "520",
                    "steps": [
                        {
                            "instruction": "沿前门大街向北步行200米右转",
                            "road": "前门大街",
                            "distance": "200",
                            "duration": "150",
                            "polyline": "116.397,39.899;116.397,39.901;116.398,39.902;" * 50,
                            "tmcs": [
                                {"distance": "100", "status": "1", "speed": "40"},
                                {"distance": "100", "status": "2", "speed": "30"},
                            ],
                            "action": "右转",
                            "tolls": "0",
                        },
                        {
                            "instruction": "沿崇文门外大街向东步行300米到达终点",
                            "road": "崇文门外大街",
                            "distance": "300",
                            "duration": "240",
                            "polyline": "116.400,39.903;116.402,39.903;" * 50,
                            "tmcs": [],
                        },
                    ],
                }
            ],
        },
    }


# ---------------------------------------------------------------------
# 1. weather 模型视图
# ---------------------------------------------------------------------

def test_weather_condense():
    print("== 1. weather 模型视图 ==")
    from apitelegramchat.tool_result_condense import condense_for_model

    raw = _weather_payload()
    raw_json = json.dumps(raw, ensure_ascii=False)

    # 1.1 默认 hours=6
    out = json.loads(condense_for_model("weather", {"city": "Beijing"}, raw_json))
    check("默认 hours=6：逐时条数 = 6", len(out["hourly"]) == 6, f"got {len(out.get('hourly', []))}")
    check("默认 hours=6：hourly_omitted 提示省略条数", out.get("hourly_omitted") == 18)
    check("当前实况保留核心字段", out["current"].get("temp") == "24" and out["current"].get("condition") == "Clear")
    check("当前实况剔除 weather_code", "weather_code" not in out["current"])

    # 1.2 hours 参数生效与 clamp
    out12 = json.loads(condense_for_model("weather", {"city": "Beijing", "hours": 12}, raw_json))
    check("hours=12：逐时条数 = 12", len(out12["hourly"]) == 12, f"got {len(out12['hourly'])}")
    out100 = json.loads(condense_for_model("weather", {"city": "Beijing", "hours": 100}, raw_json))
    check("hours=100 被钳制到 24", len(out100["hourly"]) == 24, f"got {len(out100['hourly'])}")
    out0 = json.loads(condense_for_model("weather", {"city": "Beijing", "hours": 0}, raw_json))
    check("hours=0 被钳制到 1", len(out0["hourly"]) == 1, f"got {len(out0['hourly'])}")

    # 1.3 逐时字段白名单
    allowed_hourly = {"time", "temp", "condition", "precip", "chance_of_rain",
                      "humidity", "wind_speed", "wind_dir"}
    extra = set(out["hourly"][0].keys()) - allowed_hourly
    check("逐时条目只保留 8 个高价值字段", not extra, f"extra={extra}")
    check("逐时剔除 DewPointC/HeatIndexC/WindChillC",
          all(k not in out["hourly"][0] for k in ("DewPointC", "HeatIndexC", "WindChillC")))
    check("逐时剔除低频概率与辐射字段",
          all(k not in out["hourly"][0] for k in ("chance_of_fog", "chance_of_frost",
                                                  "chance_of_overcast", "chance_of_sunshine",
                                                  "chance_of_windy", "chance_of_hightemp",
                                                  "chance_of_remdry", "shortRad", "diffRad")))

    # 1.4 逐日字段白名单
    allowed_daily = {"date", "max", "min", "condition", "uvIndex",
                     "sunrise", "sunset", "chance_of_rain"}
    extra_d = set(out["daily"][0].keys()) - allowed_daily
    check("逐日条目只保留高价值字段", not extra_d, f"extra={extra_d}")
    check("逐日剔除月相字段",
          all(k not in out["daily"][0] for k in ("moonrise", "moonset", "moon_phase", "moon_illumination")))

    # 1.5 token 收益
    raw_len = len(raw_json)
    condensed_len = len(json.dumps(out100, ensure_ascii=False))
    check(f"精简后体积显著下降（{raw_len} → {condensed_len} 字符, {condensed_len * 100 // raw_len}%）",
          condensed_len < raw_len * 0.45)

    # 1.6 错误与异常输入透传
    err = json.dumps({"error": "天气查询超时"}, ensure_ascii=False)
    check("错误 JSON 的 error 字段保留", "error" in json.loads(condense_for_model("weather", {}, err)))
    check("非 JSON 文本原样返回",
          condense_for_model("weather", {}, "普通文本") == "普通文本")
    check("错误前缀文本原样返回",
          condense_for_model("weather", {}, "Error: tool weather failed") == "Error: tool weather failed")
    check("空字符串原样返回", condense_for_model("weather", {}, "") == "")


# ---------------------------------------------------------------------
# 2. subagent 模型视图
# ---------------------------------------------------------------------

def test_subagent_condense():
    print("== 2. subagent 模型视图 ==")
    from apitelegramchat.tool_result_condense import condense_for_model

    payload = {
        "ok": True,
        "rounds": 5,
        "tool_calls": 12,
        "answer": "<p>研究结果：……</p>",
        "elapsed": 123.4,
        "error": None,
        "model": "glm-4.7",
        "model_name": "GLM-4.7",
        "task_preview": "研究北京烤鸭的历史",
    }
    out = json.loads(condense_for_model("subagent", {}, json.dumps(payload, ensure_ascii=False)))
    check("task_preview 回声字段被删除", "task_preview" not in out)
    check("model_name 冗余字段被删除", "model_name" not in out)
    check("answer 保留", out.get("answer") == "<p>研究结果：……</p>")
    check("model / rounds / tool_calls / elapsed 保留",
          out.get("model") == "glm-4.7" and out.get("rounds") == 5
          and out.get("tool_calls") == 12 and out.get("elapsed") == 123.4)
    # UI 渲染需要 model_name/task_preview —— 完整返回仍在（由 format_tool_result 使用），
    # 模型视图只负责剔除，不反向校验完整版。


# ---------------------------------------------------------------------
# 3. 其他工具不误伤
# ---------------------------------------------------------------------

def test_passthrough():
    print("== 3. 其他工具 / 非 JSON 内容不误伤 ==")
    from apitelegramchat.tool_result_condense import condense_for_model

    web_search_result = "🔍 [成功: Serper / Google] 搜索「x」的结果（3/3）：\n1. 标题：…\n   链接：https://…"
    check("web_search 文本结果原样返回",
          condense_for_model("web_search", {}, web_search_result) == web_search_result)
    bash_result = "/home/u$ echo hi\nExit code: 0\nhi"
    check("bash 信封原样返回", condense_for_model("bash", {}, bash_result) == bash_result)
    memory_result = json.dumps({"ok": True, "action": "list", "memories": [], "total": 0}, ensure_ascii=False)
    check("memory JSON 原样返回", condense_for_model("memory", {}, memory_result) == memory_result)
    check("❌ 前缀错误文本原样返回",
          condense_for_model("weather", {}, "❌ 网页搜索服务暂未返回有效结果") == "❌ 网页搜索服务暂未返回有效结果")
    check("失败：前缀文本原样返回",
          condense_for_model("subagent", {}, "失败：未知工具: x。") == "失败：未知工具: x。")


# ---------------------------------------------------------------------
# 4. amap 输出清洗
# ---------------------------------------------------------------------

def test_amap_condense():
    print("== 4. amap-maps MCP 输出清洗 ==")
    from apitelegramchat.tool_result_condense import condense_amap_payload

    # 4.1 POI 结构
    raw = json.dumps(_amap_poi_payload(), ensure_ascii=False)
    out = json.loads(condense_amap_payload(raw))
    pois = out.get("pois", [])
    check("POI 数量保留", len(pois) == 2)
    first = pois[0]
    check("POI 名称/地址/坐标/ID 保留",
          first.get("name") and first.get("address") and first.get("location") and first.get("id"))
    check("POI type/typecode/alias 保留(UI 需要)",
          "type" in first and "typecode" in first and "alias" in first)
    check("photos 只保留第一张 URL",
          first.get("photos") == [{"url": "https://example.com/1.jpg"}], f"got {first.get('photos')}")
    check("空数组字段(tel/photos:[])删除", "tel" not in first and "photos" not in pois[1])
    check("parent/biz_type/poi_tag 删除",
          all(k not in first for k in ("parent", "biz_type", "poi_tag")))
    check("infocode 删除, status/info 保留", "infocode" not in out and out.get("status") == "1")
    check("biz_ext 空子键(cost:[])删除、rating 保留",
          first.get("biz_ext") == {"rating": "4.5"})

    # 4.2 路线结构
    raw_route = json.dumps(_amap_route_payload(), ensure_ascii=False)
    out_route = json.loads(condense_amap_payload(raw_route))
    path = out_route["route"]["paths"][0]
    check("路线 distance/duration 保留", path.get("distance") == "2500" and path.get("duration") == "520")
    check("origin/destination 保留", out_route["route"].get("origin") and out_route["route"].get("destination"))
    step = path["steps"][0]
    check("导航步骤 instruction/road/action 保留",
          step.get("instruction") and step.get("road") == "前门大街" and step.get("action") == "右转")
    check("polyline 坐标串删除", "polyline" not in step)
    check("tmcs 分段路况删除", "tmcs" not in step)
    raw_len, out_len = len(raw_route), len(json.dumps(out_route, ensure_ascii=False))
    check(f"路线输出体积大幅下降（{raw_len} → {out_len} 字符）", out_len < raw_len * 0.25)

    # 4.3 错误状态可判定（UI 依赖 status=="error"）
    err_raw = json.dumps({"status": "error", "message": "amap-maps MCP 调用失败（maps_geo）：boom"}, ensure_ascii=False)
    err_out = json.loads(condense_amap_payload(err_raw))
    check("错误状态结构保持可判定", err_out.get("status") == "error" and err_out.get("message"))

    # 4.4 非 JSON / 相邻 JSON
    check("非 JSON 原样返回", condense_amap_payload("plain text") == "plain text")
    adjacent = json.dumps({"pois": [{"name": "A", "tel": []}]}, ensure_ascii=False) + json.dumps(
        {"pois": [{"name": "B", "photos": [{"url": "http://x/1.jpg"}, {"url": "http://x/2.jpg"}]}]}, ensure_ascii=False)
    adj_out = condense_amap_payload(adjacent)
    check("相邻 JSON 对象均被清洗", "tel" not in adj_out and adj_out.count('"name"') == 2
          and "http://x/2.jpg" not in adj_out)

    # 4.5 transit 结构（segments/bus/buslines）
    transit = {
        "route": {
            "transits": [{
                "duration": "1800",
                "walking_distance": "800",
                "segments": [{
                    "bus": {"buslines": [{
                        "name": "地铁2号线",
                        "departure_stop": {"name": "前门站"},
                        "arrival_stop": {"name": "崇文门站"},
                        "via_stops": [],        # 途经站空数组 → 删除
                        "type": "地铁线路",
                    }]},
                }],
            }],
        }
    }
    t_out = json.loads(condense_amap_payload(json.dumps(transit, ensure_ascii=False)))
    line = t_out["route"]["transits"][0]["segments"][0]["bus"]["buslines"][0]
    check("公交线路上/下车站保留",
          line.get("departure_stop", {}).get("name") == "前门站"
          and line.get("arrival_stop", {}).get("name") == "崇文门站")
    check("途经站空数组删除", "via_stops" not in line)

    # 4.6 distance 结构
    dist = {"results": [{"origin_id": "1", "dest_id": "2", "distance": "2500", "duration": "520"}]}
    d_out = json.loads(condense_amap_payload(json.dumps(dist)))
    check("距离测量字段全保留", d_out["results"][0].get("distance") == "2500")

    # 4.7 geocode return 结构
    geo = {"return": [{"province": "北京市", "city": "北京市", "district": "东城区",
                       "location": "116.397,39.899", "level": "门址",
                       "citycode": "010", "empty_field": "", "empty_list": []}]}
    g_out = json.loads(condense_amap_payload(json.dumps(geo, ensure_ascii=False)))
    g_rec = g_out["return"][0]
    check("geocode 区域/坐标/级别保留",
          g_rec.get("province") == "北京市" and g_rec.get("location") and g_rec.get("level") == "门址")
    check("geocode 空值字段删除", "empty_field" not in g_rec and "empty_list" not in g_rec)


# ---------------------------------------------------------------------
# 5. 源头集成
# ---------------------------------------------------------------------

def test_ui_equivalence():
    """同一份数据，清洗前 vs 清洗后，UI 渲染必须逐字节等价。

    这是「源头清洗」安全性的核心证明：被删除的字段（polyline / tmcs /
    空值 / infocode）UI 本来就不读，用户可见的展示零影响。
    """
    print("== 5. UI 等价性（清洗前后 format_tool_result 输出一致）==")
    import asyncio
    from apitelegramchat.tool_result_condense import condense_amap_payload
    from apitelegramchat.tool_executors import format_tool_result

    def ui_eq(fn_name, fn_args, raw):
        before = asyncio.run(format_tool_result(fn_name, fn_args, raw))
        after = asyncio.run(format_tool_result(fn_name, fn_args, condense_amap_payload(raw)))
        return before, after

    # 5.1 POI（含顶层 rating，模拟部分 MCP 版本把 biz_ext 提升后的结构）
    poi_raw = json.dumps({
        "status": "1", "info": "OK", "infocode": "10000", "count": "2",
        "pois": [
            {"parent": [], "biz_type": [], "name": "全聚德烤鸭店(前门店)",
             "address": "北京市东城区前门大街30号", "location": "116.397,39.899",
             "distance": "123", "alias": "全聚德", "rating": "4.5",
             "type": "餐饮服务;中餐厅;北京菜", "typecode": "050000",
             "id": "B000A7BD6C", "tel": [], "opentime2": "10:00-22:00",
             "photos": [{"url": "https://example.com/1.jpg", "title": []},
                        {"url": "https://example.com/2.jpg", "title": []}]},
            {"name": "便宜坊烤鸭店", "address": "东城区崇文门外大街",
             "location": "116.418,39.893", "distance": "850",
             "type": "餐饮服务;中餐厅", "typecode": "050000",
             "id": "B000A8N2MG", "photos": []},
        ],
    }, ensure_ascii=False)
    b, a = ui_eq("poi_keyword_search", {"keywords": "烤鸭"}, poi_raw)
    check("POI 卡片清洗前后逐字节等价", b == a)
    check("POI 卡片含名称/地址/照片/评分/ID",
          all(x in a[1] for x in ("全聚德", "example.com/1.jpg", "4.5", "B000A7BD6C")))

    # 5.2 路线（polyline/tmcs 被删，导航步骤保留）
    route_raw = json.dumps(_amap_route_payload(), ensure_ascii=False)
    b, a = ui_eq("route", {"origin": "116.397,39.899", "destination": "116.418,39.893",
                           "mode": "walking"}, route_raw)
    check("路线卡片清洗前后逐字节等价", b == a)
    check("路线卡片含导航步骤", "前门大街向北步行200米右转" in a[1])

    # 5.3 geocode / distance / transit / 错误
    geo_raw = json.dumps({"return": [{"province": "北京市", "city": "北京市",
                                      "district": "东城区", "location": "116.397,39.899",
                                      "level": "门址", "citycode": "010", "empty": []}]},
                         ensure_ascii=False)
    b, a = ui_eq("geocode", {"address": "前门大街30号"}, geo_raw)
    check("geocode 卡片清洗前后逐字节等价", b == a)

    dist_raw = json.dumps({"results": [{"origin_id": "1", "dest_id": "2",
                                        "distance": "2500", "duration": "520"}]})
    b, a = ui_eq("distance", {"origin": "116.397,39.899", "destination": "116.418,39.893"}, dist_raw)
    check("distance 卡片清洗前后逐字节等价", b == a)

    transit_raw = json.dumps({
        "route": {"origin": "116.397,39.899", "destination": "116.418,39.893",
                  "transits": [{"duration": "1800", "walking_distance": "800",
                                "segments": [{"bus": {"buslines": [{
                                    "name": "地铁2号线",
                                    "departure_stop": {"name": "前门站"},
                                    "arrival_stop": {"name": "崇文门站"},
                                    "via_stops": [], "type": "地铁线路"}]}}]}]},
    }, ensure_ascii=False)
    b, a = ui_eq("route", {"origin": "116.397,39.899", "destination": "116.418,39.893",
                           "mode": "transit"}, transit_raw)
    check("transit 卡片清洗前后逐字节等价", b == a)

    err_raw = json.dumps({"status": "error",
                          "message": "amap-maps MCP 调用失败（maps_geo）：boom"}, ensure_ascii=False)
    b, a = ui_eq("geocode", {"address": "x"}, err_raw)
    check("错误路径 UI 等价（❌ 状态可判定）", b == a and "❌" in a[0])


def test_source_integration():
    print("== 6. 源头集成 ==")
    import apitelegramchat.search_engine as se

    check("search_engine 已导入 condense_amap_payload", hasattr(se, "condense_amap_payload"))
    check("死代码 _geocode_coords 已删除", not hasattr(se, "_geocode_coords"))

    # 工具描述与 hours 语义一致
    weather_tool = next(t for t in se.SEARCH_TOOLS
                        if t["function"]["name"] == "weather")
    hours_desc = weather_tool["function"]["parameters"]["properties"]["hours"]["description"]
    check("weather 工具描述不再声称「完整数据始终可用」", "完整数据始终可用" not in hours_desc, hours_desc)
    check("weather hours 描述说明 1-24 范围", "1-24" in hours_desc)

    # present_files 正常路径无 error:null
    import inspect
    from apitelegramchat.tool_executors import execute_present_files
    src = inspect.getsource(execute_present_files)
    check("execute_present_files 不再输出 error:null", '"error": None' not in src)


def test_loop_integration():
    print("== 7. tool_call_loop 集成 ==")
    src_path = ROOT / "src" / "apitelegramchat" / "ai" / "tool_call_loop.py"
    src = src_path.read_text(encoding="utf-8")
    check("run_one 对原始 result_str 调用 condense_for_model（先精简后截断）",
          "condense_for_model(fn_name, fn_args, result_str)" in src)
    check("精简后的 model_view 再走 token 预算截断",
          "_truncate_tool_result(model_view, fn_name=fn_name)" in src)
    check("tool_msg content 使用 llm_content(模型视图)",
          '"content": llm_content' in src)
    check("UI 路径仍用完整 safe_content 渲染 details_html",
          "format_tool_result(fn_name, fn_args, safe_content)" in src)

    sub_src = (ROOT / "src" / "apitelegramchat" / "subagent_tool.py").read_text(encoding="utf-8")
    check("子 agent 工具循环接入 condense_for_model",
          "condense_for_model(name, arguments or {}" in sub_src)


def main():
    test_weather_condense()
    test_subagent_condense()
    test_passthrough()
    test_amap_condense()
    test_ui_equivalence()
    test_source_integration()
    test_loop_integration()
    print()
    print(f"PASS={PASS} FAIL={FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
