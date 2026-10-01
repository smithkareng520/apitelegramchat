"""高德 MCP 原生工具名与暴露策略回归测试。"""
import json
from pathlib import Path


def test_gaode_tool_family_keeps_native_mcp_name():
    import tool_names as tn

    assert tn.tool_family(tn.MAPS_GEO) == "maps_geo"
    assert tn.tool_family(tn.MAPS_REGEOCODE) == "maps_regeocode"
    assert tn.tool_family(tn.MAPS_TEXT_SEARCH) == "maps_text_search"
    assert tn.tool_family(tn.MAPS_AROUND_SEARCH) == "maps_around_search"
    assert tn.tool_family(tn.MAPS_SEARCH_DETAIL) == "maps_search_detail"
    assert tn.tool_family(tn.MAPS_DISTANCE) == "maps_distance"
    assert tn.tool_family(tn.MAPS_DIRECTION_DRIVING) == "maps_direction_driving"
    assert tn.tool_family(tn.MAPS_DIRECTION_WALKING) == "maps_direction_walking"
    assert tn.tool_family(tn.MAPS_DIRECTION_BICYCLING) == "maps_direction_bicycling"
    assert tn.tool_family(tn.MAPS_DIRECTION_TRANSIT) == "maps_direction_transit_integrated"


def test_gaode_weather_is_disabled_in_mcp_policy():
    config = json.loads((Path(__file__).parents[2] / "mcp.json").read_text())
    policy = config["mcpServers"]["gaode_mcp"]["policy"]
    assert "maps_weather" in policy["disabled_tools"]
