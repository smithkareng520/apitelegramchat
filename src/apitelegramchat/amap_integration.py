"""
amap_integration.py
===================

高德 Web 服务 API 全量封装 —— 用来替代 apitelegramchat 中
search_engine.py / tool_executors.py 默认使用的 OpenStreetMap (Nominatim + Overpass)
及部分第三方服务，在中国大陆场景下提供精确的地理数据。

覆盖能力（与原函数一一对应）：
    高德服务                  替代原服务                        对应原函数
    ----------------------  ------------------------------    -------------------------
    地理编码 /v3/geocode/geo        Nominatim                     execute_geocode
    逆地理编码 /v3/geocode/regeo    (新增能力)                    reverse_geocode (内部用)
    周边搜索 /v3/place/around       Overpass POI                  execute_search_poi
    关键字搜索 /v3/place/text       Overpass POI                  execute_place_details
    驾车路径 /v3/direction/driving  OSRM driving                  execute_route (profile=driving)
    步行路径 /v3/direction/walking  OSRM walking                  execute_route (profile=walking)
    骑行路径 /v4/direction/bicycling OSRM cycling                 execute_route (profile=cycling)
    公交路径 /v3/direction/transit  OSRM transit (原 fallback)    execute_route (profile=transit)
    IP 定位 /v3/ip                  ip-api.com                    execute_ip_geo
    静态地图 /v3/staticmap          Geoapify / OSM staticmap      _get_static_map_image

不替换（保留原服务）：
    海拔查询  open-elevation / opentopodata   —— 高德无对应 API
    等时圈    ORS                              —— 高德等时圈不在个人免费配额内
    交通态势  Overpass (其实原代码也没真正查)   —— 高德交通态势需企业认证

环境变量：
    AMAP_KEY            高德 Web 服务 Key（必填，未设置则全部降级回原服务）
    AMAP_POI_DAILY_LIMIT  POI 搜索每日配额上限，默认 140（个人月配额 5000，留余量）
    AMAP_CACHE_TTL      POI 搜索缓存有效期（秒），默认 3600

配额保护策略：
    地理编码 / 路径规划 / 距离 / IP / 静态地图：高德给到 150,000/月，宽松
    POI 搜索（关键字 / 周边 / 多边形 / ID）：高德只给 5,000/月
        → 实现 1 小时内存缓存（相同坐标 + 关键词命中缓存，不消耗配额）
        → 持久化每日计数到 ~/.apitelegramchat_amap_quota.json
        → 当日累计超过 AMAP_POI_DAILY_LIMIT 后自动降级到 Overpass，次月自动恢复

坐标系统说明：
    Telegram / Google Maps / OSM 使用 WGS-84
    高德使用 GCJ-02（火星坐标系）
    本模块内部完成所有坐标转换，对外接口一律 WGS-84
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

from apitelegramchat import config as app_config

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
AMAP_KEY = getattr(app_config, "AMAP_KEY", "") or os.getenv("AMAP_KEY", "").strip()
AMAP_POI_DAILY_LIMIT = int(os.getenv("AMAP_POI_DAILY_LIMIT", "140"))
AMAP_CACHE_TTL = int(os.getenv("AMAP_CACHE_TTL", "3600"))
AMAP_TIMEOUT = 12  # 单次请求超时

# 高德 Web 服务 endpoint
AMAP_GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"
AMAP_REGEO_URL = "https://restapi.amap.com/v3/geocode/regeo"
AMAP_PLACE_AROUND_URL = "https://restapi.amap.com/v5/place/around"
AMAP_PLACE_TEXT_URL = "https://restapi.amap.com/v5/place/text"
AMAP_DRIVING_URL = "https://restapi.amap.com/v3/direction/driving"
AMAP_WALKING_URL = "https://restapi.amap.com/v3/direction/walking"
AMAP_BICYCLING_URL = "https://restapi.amap.com/v4/direction/bicycling"
AMAP_TRANSIT_URL = "https://restapi.amap.com/v3/direction/transit/integrated"
AMAP_DISTANCE_URL = "https://restapi.amap.com/v3/distance"
AMAP_IP_URL = "https://restapi.amap.com/v3/ip"
AMAP_STATICMAP_URL = "https://restapi.amap.com/v3/staticmap"

USER_AGENT = "TelegramAIAssistant/1.0 (amap-integration)"


def is_enabled() -> bool:
    """是否启用高德（已配置 AMAP_KEY）"""
    return bool(_amap_key())




def _amap_key() -> str:
    """优先读取 config 模块缓存的 AMAP_KEY，兼容已清洗的 os.environ。"""
    key = getattr(app_config, "AMAP_KEY", "") or AMAP_KEY
    return (key or "").strip()

# ---------------------------------------------------------------------------
# GCJ-02 ↔ WGS-84 坐标转换
# 算法来自 https://github.com/wandergis/coordtransform
# ---------------------------------------------------------------------------

_PI = 3.1415926535897932384626
_A = 6378245.0
_EE = 0.00669342162296594323


def _out_of_china(lng: float, lat: float) -> bool:
    """粗略判断坐标是否在中国境外（境外不需要偏移）"""
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * _PI) + 40.0 * math.sin(y / 3.0 * _PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * _PI) + 320 * math.sin(y * _PI / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * _PI) + 40.0 * math.sin(x / 3.0 * _PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * _PI) + 300.0 * math.sin(x / 30.0 * _PI)) * 2.0 / 3.0
    return ret


def gcj02_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    """GCJ-02 (火星坐标) → WGS-84。返回 (lng, lat)"""
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * _PI
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * _PI)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * _PI)
    mglat = lat + dlat
    mglng = lng + dlng
    return lng * 2 - mglng, lat * 2 - mglat


def wgs84_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    """WGS-84 → GCJ-02 (火星坐标)。返回 (lng, lat)"""
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * _PI
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * _PI)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * _PI)
    return lng + dlng, lat + dlat


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

async def _amap_get(url: str, params: dict[str, Any]) -> Optional[dict]:
    """发起 GET 请求，返回 JSON dict 或 None"""
    key = _amap_key()
    if not key:
        return None
    params = {**params, "key": key, "output": "json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=AMAP_TIMEOUT),
                headers={"User-Agent": USER_AGENT},
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
    except Exception:
        return None


async def _amap_get_bytes(url: str, params: dict[str, Any]) -> Optional[bytes]:
    """发起 GET 请求，返回 bytes 或 None（用于静态地图）"""
    key = _amap_key()
    if not key:
        return None
    params = {**params, "key": key}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=AMAP_TIMEOUT),
                headers={"User-Agent": USER_AGENT},
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                # 高德出错时返回 JSON 文本而非图片，过滤掉
                if len(data) < 500 or data[:1] in (b"{", b"["):
                    return None
                return data
    except Exception:
        return None


def _haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """WGS-84 大圆距离（米）"""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    )
    return round(2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


def _gaode_marker_url(
    *,
    lat: float | None = None,
    lon: float | None = None,
    name: str = "",
    poiid: str | None = None,
    coordinate: str = "wgs84",
) -> str:
    """高德单点标注链接：优先用 POI ID，其次用坐标。"""
    safe_name = quote((name or "")[:40])
    if poiid:
        return f"https://uri.amap.com/marker?poiid={quote(str(poiid))}&name={safe_name}&src=apitelegramchat"
    if lat is None or lon is None:
        return ""
    return (
        f"https://uri.amap.com/marker?position={lon},{lat}"
        f"&coordinate={coordinate}&name={safe_name}&src=apitelegramchat"
    )


def _normalize_text(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).lower()


def _extract_poi_location(poi: dict[str, Any]) -> tuple[float, float, str] | None:
    """优先返回入口坐标，其次导航点，最后 POI 中心点。"""
    for key in ("entr_location", "location"):
        raw = poi.get(key) or ""
        if isinstance(raw, str) and "," in raw:
            try:
                lng_gcj, lat_gcj = (float(x) for x in raw.split(",", 1))
            except Exception:
                continue
            lng_wgs, lat_wgs = gcj02_to_wgs84(lng_gcj, lat_gcj)
            return lat_wgs, lng_wgs, key
    return None


def _poi_score(poi: dict[str, Any], query: str, lat: float | None = None, lon: float | None = None) -> float:
    """按名称精度 + 位置贴近度给 POI 打分。"""
    q = _normalize_text(query)
    name = _normalize_text(str(poi.get("name") or ""))
    alias = _normalize_text(str(poi.get("alias") or ""))
    brand = _normalize_text(str(poi.get("brand") or ""))
    score = 0.0

    if q and name == q:
        score += 1000
    elif q and (name.startswith(q) or q in name):
        score += 700
    elif q and (alias == q or q in alias or brand == q or q in brand):
        score += 500

    if poi.get("navi_poiid"):
        score += 60
    if poi.get("entr_location"):
        score += 40
    if poi.get("parent"):
        score -= 5

    if lat is not None and lon is not None:
        loc = poi.get("entr_location") or poi.get("location") or ""
        if isinstance(loc, str) and "," in loc:
            try:
                lng_gcj, lat_gcj = (float(x) for x in loc.split(",", 1))
                lng_wgs, lat_wgs = gcj02_to_wgs84(lng_gcj, lat_gcj)
                dist = _haversine_meters(lat, lon, lat_wgs, lng_wgs)
                score -= min(dist / 20.0, 300.0)
            except Exception:
                pass
    return score


def _poi_address(poi: dict[str, Any]) -> str:
    parts = [
        poi.get("pname", ""),
        poi.get("cityname", ""),
        poi.get("adname", ""),
        poi.get("address", ""),
    ]
    return " ".join(filter(None, parts))


# ---------------------------------------------------------------------------
# POI 搜索配额保护（每日计数 + 文件持久化）
# ---------------------------------------------------------------------------

_QUOTA_FILE = Path(
    os.getenv("AMAP_QUOTA_FILE")
    or (Path.home() / ".apitelegramchat_amap_quota.json")
)


def _load_quota_state() -> dict[str, Any]:
    """读取配额状态：{"date": "YYYY-MM-DD", "poi_count": N}"""
    try:
        if _QUOTA_FILE.exists():
            data = json.loads(_QUOTA_FILE.read_text(encoding="utf-8"))
            today = time.strftime("%Y-%m-%d")
            if data.get("date") != today:
                # 跨天重置
                data = {"date": today, "poi_count": 0}
                _QUOTA_FILE.write_text(json.dumps(data), encoding="utf-8")
            return data
    except Exception:
        pass
    return {"date": time.strftime("%Y-%m-%d"), "poi_count": 0}


def _save_quota_state(data: dict[str, Any]) -> None:
    try:
        _QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
        _QUOTA_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _poi_quota_exceeded() -> bool:
    """今日 POI 搜索是否已超配额"""
    state = _load_quota_state()
    return state.get("poi_count", 0) >= AMAP_POI_DAILY_LIMIT


def _poi_quota_remaining() -> int:
    state = _load_quota_state()
    return max(0, AMAP_POI_DAILY_LIMIT - state.get("poi_count", 0))


def _poi_quota_incr(n: int = 1) -> None:
    """成功调用一次 POI 搜索后递增计数"""
    state = _load_quota_state()
    state["poi_count"] = state.get("poi_count", 0) + n
    _save_quota_state(state)


# ---------------------------------------------------------------------------
# POI 搜索结果缓存（1 小时 TTL）
# ---------------------------------------------------------------------------

_poi_cache: dict[str, tuple[float, Any]] = {}


def _poi_cache_key(lat: float, lon: float, query: str, radius: int) -> str:
    # 坐标四舍五入到 0.001°（约 100m）以增加命中率
    return f"{round(lat, 3)},{round(lon, 3)},{query.lower().strip()},{radius}"


def _poi_cache_get(key: str) -> Optional[Any]:
    item = _poi_cache.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts > AMAP_CACHE_TTL:
        _poi_cache.pop(key, None)
        return None
    return value


def _poi_cache_set(key: str, value: Any) -> None:
    _poi_cache[key] = (time.time(), value)
    # 简单的容量限制
    if len(_poi_cache) > 500:
        # 丢掉最早的 100 条
        for k in list(_poi_cache.keys())[:100]:
            _poi_cache.pop(k, None)


# ---------------------------------------------------------------------------
# 地理编码缓存
# ---------------------------------------------------------------------------

_geocode_cache: dict[str, tuple[float, float, str]] = {}


# ===========================================================================
# 1. 地理编码
# ===========================================================================

async def _amap_geocode_coords(address: str) -> Optional[tuple[float, float, str]]:
    """
    地址 → (lat_wgs84, lon_wgs84, display_name)
    """
    key = address.lower().strip()
    if key in _geocode_cache:
        return _geocode_cache[key]

    data = await _amap_get(AMAP_GEOCODE_URL, {"address": address})
    if not data or data.get("status") != "1" or int(data.get("count", "0")) < 1:
        return None

    geo = data["geocodes"][0]
    lng_gcj, lat_gcj = (float(x) for x in geo["location"].split(","))
    lng_wgs, lat_wgs = gcj02_to_wgs84(lng_gcj, lat_gcj)

    display = geo.get("formatted_address", address)
    province = geo.get("province") or ""
    city = geo.get("city") or province
    if city and province and province not in display:
        display = f"{display}（{province}{city}）"

    result = (lat_wgs, lng_wgs, display)
    _geocode_cache[key] = result
    return result


async def execute_geocode_amap(address: str) -> str:
    """与 execute_geocode 返回 JSON 结构兼容"""
    if not address.strip():
        return json.dumps({"status": "error", "message": "地址为空"}, ensure_ascii=False)

    coords = await _amap_geocode_coords(address)
    if coords is None:
        return json.dumps(
            {"status": "error", "message": f"未找到地址：{address}"},
            ensure_ascii=False,
        )

    lat, lon, display_name = coords

    # 反查地址组成部分（用 regeo，开销很小，150k/月）
    lng_gcj, lat_gcj = wgs84_to_gcj02(lon, lat)
    regeo = await _amap_get(
        AMAP_REGEO_URL,
        {"location": f"{lng_gcj},{lat_gcj}", "extensions": "base"},
    )
    addr_comp = {}
    if regeo and regeo.get("status") == "1":
        addr_comp = (regeo.get("regeocode") or {}).get("addressComponent") or {}

    return json.dumps(
        {
            "status": "success",
            "lat": lat,
            "lon": lon,
            "display_name": display_name,
            "road": addr_comp.get("road", "") or "",
            "city": addr_comp.get("city", "") or addr_comp.get("province", "") or "",
            "county": addr_comp.get("district", "") or "",
            "state": addr_comp.get("province", "") or "",
            "country": "中国",
            "postcode": addr_comp.get("adcode", "") or "",
            "nav_links": {
                "google": f"https://maps.google.com/?q={lat},{lon}",
                "gaode": (
                    f"https://uri.amap.com/marker?position={lon},{lat}&coordinate=wgs84"
                    f"&name={quote(display_name[:40])}"
                ),
                "baidu": (
                    f"http://api.map.baidu.com/marker?location={lat},{lon}"
                    f"&title={quote(display_name[:40])}&output=html"
                ),
            },
        },
        ensure_ascii=False,
    )


# ===========================================================================
# 2. 逆地理编码
# ===========================================================================

async def reverse_geocode(lat: float, lon: float) -> Optional[dict[str, Any]]:
    """
    坐标 (WGS-84) → 中文地址
    返回 {"formatted": "...", "province": "...", "city": "...", "district": "...", "adcode": "...", "citycode": "..."}
    """
    lng_gcj, lat_gcj = wgs84_to_gcj02(lon, lat)
    data = await _amap_get(
        AMAP_REGEO_URL,
        {"location": f"{lng_gcj},{lat_gcj}", "extensions": "base"},
    )
    if not data or data.get("status") != "1":
        return None
    regeo = data.get("regeocode") or {}
    comp = regeo.get("addressComponent") or {}
    return {
        "formatted": regeo.get("formatted_address", "") or "",
        "province": comp.get("province", "") or "",
        "city": comp.get("city", "") or comp.get("province", "") or "",
        "district": comp.get("district", "") or "",
        "adcode": comp.get("adcode", "") or "",
        "citycode": comp.get("citycode", "") or "",
    }


# ===========================================================================
# 3. POI 周边搜索
# ===========================================================================

async def execute_search_poi_amap(
    lat: float,
    lon: float,
    query: str,
    radius: int = 1000,
    max_results: int = 15,
) -> str:
    """
    以 (lat, lon) [WGS-84] 为中心搜索周边 POI。
    返回 JSON：{"status": "success", "results": [...]}
    """
    if not _amap_key():
        return json.dumps(
            {"status": "error", "message": "AMAP_KEY 未配置"},
            ensure_ascii=False,
        )

    radius = min(max(int(radius), 100), 50000)
    max_results = min(max(int(max_results), 1), 25)

    # 1) 缓存命中
    cache_key = _poi_cache_key(lat, lon, query, radius)
    cached = _poi_cache_get(cache_key)
    if cached is not None:
        return cached

    # 2) 配额检查
    if _poi_quota_exceeded():
        return json.dumps(
            {
                "status": "quota_exceeded",
                "message": (
                    f"今日高德 POI 搜索配额已用尽（上限 {AMAP_POI_DAILY_LIMIT} 次/天，"
                    f"月配额 5000 次）。请明天再试，或缩小关键词范围。"
                ),
            },
            ensure_ascii=False,
        )

    # 3) 调用高德
    lng_gcj, lat_gcj = wgs84_to_gcj02(lon, lat)
    region_info = await reverse_geocode(lat, lon)
    params = {
        "location": f"{lng_gcj},{lat_gcj}",
        "radius": str(radius),
        "sortrule": "distance",
        "page_size": str(max_results),
        "page_num": "1",
        "show_fields": ",".join(["children", "adcode", "citycode", "alias", "business", "navi", "photos"]),
    }
    if query.strip():
        params["keywords"] = query.strip()
    if region_info and region_info.get("adcode"):
        params["region"] = region_info["adcode"]
        params["city_limit"] = "true"

    data = await _amap_get(AMAP_PLACE_AROUND_URL, params)

    if not data or data.get("status") != "1":
        # 不消耗配额的失败，不递增计数
        return json.dumps(
            {"status": "error", "message": data.get("info", "高德 POI 查询失败") if data else "高德无响应"},
            ensure_ascii=False,
        )

    # 成功调用，递增配额计数
    _poi_quota_incr(1)

    pois = data.get("pois") or []
    if not pois:
        result_json = json.dumps(
            {
                "status": "no_results",
                "message": "附近未找到符合条件的地点，尝试扩大搜索范围或更换关键词",
            },
            ensure_ascii=False,
        )
        _poi_cache_set(cache_key, result_json)
        return result_json

    results = []
    seen_names: set[str] = set()
    for poi in pois:
        name = poi.get("name") or ""
        if not name or name in seen_names:
            continue
        seen_names.add(name)

        loc = poi.get("entr_location") or poi.get("location") or ""
        if "," not in loc:
            continue
        try:
            lng_gcj_p, lat_gcj_p = (float(x) for x in loc.split(","))
        except Exception:
            continue
        lng_wgs, lat_wgs = gcj02_to_wgs84(lng_gcj_p, lat_gcj_p)

        # 距离（米）
        dist_str = poi.get("distance", "")
        try:
            dist = int(float(dist_str))
        except (ValueError, TypeError):
            dist = _haversine_meters(lat, lon, lat_wgs, lng_wgs)

        address = _poi_address(poi)
        business = poi.get("business") or {}
        navi = poi.get("navi") or {}
        poiid = (navi.get("navi_poiid", "") if isinstance(navi, dict) else "") or (poi.get("id", "") or "")

        results.append(
            {
                "name": name,
                "lat": lat_wgs,
                "lon": lng_wgs,
                "address": address,
                "phone": poi.get("tel", "") or "",
                "website": "",
                "opening_hours": business.get("opentime_today", "") if isinstance(business, dict) else "",
                "cuisine": poi.get("type", "") or "",
                "distance": dist,
                "amap_poiid": poi.get("id", "") or "",
                "navi_poiid": navi.get("navi_poiid", "") if isinstance(navi, dict) else "",
                "entr_location": poi.get("entr_location", "") or "",
                "nav_gaode": _gaode_marker_url(
                    lat=lat_wgs,
                    lon=lng_wgs,
                    name=name,
                    poiid=poiid,
                ),
                "nav_google": f"https://maps.google.com/?q={lat_wgs},{lng_wgs}",
            }
        )

        if len(results) >= max_results:
            break

    if not results:
        result_json = json.dumps(
            {
                "status": "no_results",
                "message": "附近未找到符合条件的地点，尝试扩大搜索范围或更换关键词",
            },
            ensure_ascii=False,
        )
    else:
        results.sort(key=lambda x: x["distance"])
        result_json = json.dumps(
            {"status": "success", "results": results, "quota_remaining": _poi_quota_remaining()},
            ensure_ascii=False,
        )

    _poi_cache_set(cache_key, result_json)
    return result_json


# ===========================================================================
# 4. 关键字搜索（用于 place_details）
# ===========================================================================

async def execute_place_details_amap(
    query: str,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
) -> str:
    """
    按名称查询地点详情。若提供 lat/lon，则优先在该坐标所在区域搜索。
    返回 JSON 与 execute_place_details 兼容。
    """
    if not _amap_key():
        return json.dumps(
            {"status": "error", "message": "AMAP_KEY 未配置"},
            ensure_ascii=False,
        )

    # 配额检查（POI text 搜索与 around 共享 5000/月配额）
    if _poi_quota_exceeded():
        return json.dumps(
            {
                "status": "quota_exceeded",
                "message": f"今日高德 POI 搜索配额已用尽（上限 {AMAP_POI_DAILY_LIMIT} 次/天）",
            },
            ensure_ascii=False,
        )

    region_info = None
    if lat is not None and lon is not None:
        region_info = await reverse_geocode(lat, lon)

    params = {
        "keywords": query,
        "page_size": "10",
        "page_num": "1",
        "show_fields": ",".join(["children", "business", "navi", "alias", "photos"]),
    }
    if region_info and region_info.get("adcode"):
        params["region"] = region_info["adcode"]
        params["city_limit"] = "true"

    data = await _amap_get(AMAP_PLACE_TEXT_URL, params)
    if not data or data.get("status") != "1":
        return json.dumps(
            {"status": "error", "message": data.get("info", "高德查询失败") if data else "高德无响应"},
            ensure_ascii=False,
        )

    _poi_quota_incr(1)

    pois = data.get("pois") or []
    if not pois:
        return json.dumps(
            {"status": "error", "message": f"未找到地点：{query}"},
            ensure_ascii=False,
        )

    ranked = sorted(
        pois,
        key=lambda p: _poi_score(p, query, lat=lat, lon=lon),
        reverse=True,
    )
    poi = ranked[0]

    loc_src = poi.get("entr_location") or poi.get("location") or ""
    if "," in loc_src:
        try:
            lng_gcj_p, lat_gcj_p = (float(x) for x in loc_src.split(","))
            lng_wgs, lat_wgs = gcj02_to_wgs84(lng_gcj_p, lat_gcj_p)
        except Exception:
            lat_wgs, lng_wgs = (lat or 0.0), (lon or 0.0)
    elif lat is not None and lon is not None:
        lat_wgs, lng_wgs = lat, lon
    else:
        lat_wgs, lng_wgs = 0.0, 0.0

    name = poi.get("name", query)
    address_parts = _poi_address(poi)
    navi = poi.get("navi") or {}
    nav_poiid = navi.get("navi_poiid", "") if isinstance(navi, dict) else ""
    poiid = nav_poiid or (poi.get("id", "") or "")

    return json.dumps(
        {
            "status": "success",
            "name": name,
            "lat": lat_wgs,
            "lon": lng_wgs,
            "amap_poiid": poi.get("id", "") or "",
            "navi_poiid": nav_poiid,
            "entr_location": poi.get("entr_location", "") or "",
            "phone": poi.get("tel", "") or "",
            "website": "",
            "opening_hours": ((poi.get("business") or {}).get("opentime_today", "") if isinstance(poi.get("business"), dict) else ""),
            "cuisine": poi.get("type", "") or "",
            "wheelchair": "",
            "smoking": "",
            "internet_access": "",
            "stars": "",
            "wikidata": "",
            "brand": poi.get("brand", "") or "",
            "operator": "",
            "email": "",
            "addr_full": address_parts,
            "description": poi.get("intro", "") or poi.get("tag", "") or "",
            "fee": "",
            "capacity": "",
            "nav_links": {
                "google": f"https://maps.google.com/?q={lat_wgs},{lng_wgs}",
                "gaode": _gaode_marker_url(
                    lat=lat_wgs,
                    lon=lng_wgs,
                    name=name,
                    poiid=poiid,
                ),
                "baidu": (
                    f"http://api.map.baidu.com/marker?location={lat_wgs},{lng_wgs}"
                    f"&title={quote(name[:40])}&output=html"
                ),
            },
        },
        ensure_ascii=False,
    )


# ===========================================================================
# 5. 路径规划
# ===========================================================================

_PROFILE_TO_AMAP = {
    "driving": "driving",
    "walking": "walking",
    "cycling": "bicycling",
    "bicycling": "bicycling",
    "transit": "transit",
}


async def _parse_location_for_route(loc: str) -> Optional[tuple[float, float, str]]:
    """支持 'lat,lon' 坐标字符串或地址字符串"""
    loc = loc.strip()
    # 坐标格式
    parts = loc.split(",")
    if len(parts) == 2:
        try:
            lat, lon = float(parts[0]), float(parts[1])
            return lat, lon, loc
        except ValueError:
            pass
    # 地址格式 → 走高德 geocode
    coords = await _amap_geocode_coords(loc)
    if coords is None:
        return None
    return coords  # (lat_wgs84, lon_wgs84, display)


def _format_route_step_amap(step: dict, mode: str = "driving") -> str:
    """把高德 step 转成可读的中文导航指令"""
    instruction = step.get("instruction", "").strip()
    if instruction:
        return instruction
    # 兜底：手动拼
    road = step.get("road", "")
    dist = step.get("distance", "0")
    try:
        dist_m = int(dist)
    except (ValueError, TypeError):
        dist_m = 0
    action = step.get("action", "")
    action_map = {
        "左转": "左转", "右转": "右转", "直行": "直行", "减速慢行": "直行",
        "进入环岛": "进入环岛", "离开环岛": "离开环岛", "到达途经点": "到达途经点",
        "进入服务区": "进入服务区", "到达终点": "到达终点", "到达起点": "出发",
    }
    action_cn = action_map.get(action, action)
    if road:
        return f"{action_cn}进入 {road}（{dist_m}m）" if action_cn else f"沿 {road} 行驶 {dist_m}m"
    return f"{action_cn}（{dist_m}m）" if action_cn else f"行驶 {dist_m}m"


async def execute_route_amap(start: str, end: str, profile: str = "driving") -> str:
    """与 execute_route 返回 JSON 结构兼容"""
    if not _amap_key():
        return json.dumps(
            {"status": "error", "message": "AMAP_KEY 未配置"},
            ensure_ascii=False,
        )

    prof = _PROFILE_TO_AMAP.get(profile, "driving")

    start_res, end_res = await asyncio.gather(
        _parse_location_for_route(start),
        _parse_location_for_route(end),
    )
    if start_res is None:
        return json.dumps({"status": "error", "message": f"无法解析起点：{start}"}, ensure_ascii=False)
    if end_res is None:
        return json.dumps({"status": "error", "message": f"无法解析终点：{end}"}, ensure_ascii=False)

    start_lat, start_lon, start_name = start_res
    end_lat, end_lon, end_name = end_res

    # 把 WGS-84 转 GCJ-02 给高德
    start_lng_gcj, start_lat_gcj = wgs84_to_gcj02(start_lon, start_lat)
    end_lng_gcj, end_lat_gcj = wgs84_to_gcj02(end_lon, end_lat)
    origin = f"{start_lng_gcj},{start_lat_gcj}"
    destination = f"{end_lng_gcj},{end_lat_gcj}"

    steps: list[str] = []
    distance_km = 0.0
    duration_min = 0.0

    if prof == "driving":
        data = await _amap_get(
            AMAP_DRIVING_URL,
            {"origin": origin, "destination": destination, "extensions": "base", "strategy": "0"},
        )
        if not data or data.get("status") != "1":
            return json.dumps({"status": "error", "message": (data or {}).get("info", "驾车规划失败")}, ensure_ascii=False)
        paths = (data.get("route") or {}).get("paths") or []
        if not paths:
            return json.dumps({"status": "error", "message": "驾车规划无路径"}, ensure_ascii=False)
        path = paths[0]
        distance_km = round(int(path.get("distance", 0)) / 1000, 2)
        duration_min = round(int(path.get("duration", 0)) / 60, 1)
        for step in path.get("steps", []):
            steps.append(_format_route_step_amap(step, "driving"))

    elif prof == "walking":
        data = await _amap_get(
            AMAP_WALKING_URL,
            {"origin": origin, "destination": destination},
        )
        if not data or data.get("status") != "1":
            return json.dumps({"status": "error", "message": (data or {}).get("info", "步行规划失败")}, ensure_ascii=False)
        paths = (data.get("route") or {}).get("paths") or []
        if not paths:
            return json.dumps({"status": "error", "message": "步行规划无路径"}, ensure_ascii=False)
        path = paths[0]
        distance_km = round(int(path.get("distance", 0)) / 1000, 2)
        duration_min = round(int(path.get("duration", 0)) / 60, 1)
        for step in path.get("steps", []):
            steps.append(_format_route_step_amap(step, "walking"))

    elif prof == "bicycling":
        # 骑行是 V4，返回结构在 data.paths（不是 route.paths）
        data = await _amap_get(
            AMAP_BICYCLING_URL,
            {"origin": origin, "destination": destination},
        )
        if not data or data.get("status") != "1":
            return json.dumps({"status": "error", "message": (data or {}).get("message", "骑行规划失败")}, ensure_ascii=False)
        paths = data.get("data", {}).get("paths") or []
        if not paths:
            return json.dumps({"status": "error", "message": "骑行规划无路径"}, ensure_ascii=False)
        path = paths[0]
        distance_km = round(int(path.get("distance", 0)) / 1000, 2)
        duration_min = round(int(path.get("duration", 0)) / 60, 1)
        for step in path.get("steps", []):
            steps.append(_format_route_step_amap(step, "bicycling"))

    elif prof == "transit":
        # 公交需要起点终点所在城市
        rev_start = await reverse_geocode(start_lat, start_lon)
        rev_end = await reverse_geocode(end_lat, end_lon)
        city = (rev_start or {}).get("city") or (rev_end or {}).get("city") or "北京"
        data = await _amap_get(
            AMAP_TRANSIT_URL,
            {
                "origin": origin,
                "destination": destination,
                "city": city,
                "cityd": (rev_end or {}).get("city") or city,
                "strategy": "0",  # 最快
                "nightflag": "0",
                "extensions": "base",
            },
        )
        if not data or data.get("status") != "1":
            return json.dumps({"status": "error", "message": (data or {}).get("info", "公交规划失败")}, ensure_ascii=False)
        transits = (data.get("route") or {}).get("transits") or []
        if not transits:
            return json.dumps({"status": "error", "message": "公交规划无路径"}, ensure_ascii=False)
        transit = transits[0]
        distance_km = round(int(transit.get("distance", 0)) / 1000, 2)
        duration_min = round(int(transit.get("duration", 0)) / 60, 1)

        # 解析 segments → 简化指令
        for seg in transit.get("segments", []):
            walking = seg.get("walking", {})
            bus = seg.get("bus", {})
            if walking and walking.get("distance"):
                try:
                    wd = int(walking["distance"])
                    if wd > 0:
                        steps.append(f"步行 {wd}m")
                except (ValueError, TypeError):
                    pass
            if bus and bus.get("buslines"):
                first_line = bus["buslines"][0]
                line_name = first_line.get("name", "")
                via_stops = first_line.get("via_num", "0")
                try:
                    stop_cnt = int(via_stops)
                except (ValueError, TypeError):
                    stop_cnt = 0
                if line_name:
                    steps.append(f"乘坐 {line_name}，经 {stop_cnt} 站")

    else:
        return json.dumps({"status": "error", "message": f"未知 profile: {profile}"}, ensure_ascii=False)

    if not steps:
        steps = [f"从 {start_name} 出发", f"到达 {end_name}"]

    center_lat = (start_lat + end_lat) / 2
    center_lon = (start_lon + end_lon) / 2

    nav_google = (
        f"https://maps.google.com/maps?saddr={start_lat},{start_lon}"
        f"&daddr={end_lat},{end_lon}&dirflg={'d' if prof == 'driving' else 'w'}"
    )
    nav_gaode = (
        f"https://uri.amap.com/navigation?from={start_lon},{start_lat},"
        f"{quote(start_name[:20])}&to={end_lon},{end_lat},{quote(end_name[:20])}"
        f"&mode={'car' if prof == 'driving' else ('walk' if prof == 'walking' else ('ride' if prof == 'bicycling' else 'bus'))}"
        f"&callnative=1"
    )

    return json.dumps(
        {
            "status": "success",
            "distance_km": distance_km,
            "duration_min": duration_min,
            "start_name": start_name,
            "end_name": end_name,
            "start_lat": start_lat,
            "start_lon": start_lon,
            "end_lat": end_lat,
            "end_lon": end_lon,
            "center_lat": center_lat,
            "center_lon": center_lon,
            "steps": steps[:15],
            "nav_links": {
                "google": nav_google,
                "gaode": nav_gaode,
            },
        },
        ensure_ascii=False,
    )


# ===========================================================================
# 6. IP 定位
# ===========================================================================

async def execute_ip_geo_amap(ip: str = "") -> str:
    """与 execute_ip_geo 返回 JSON 结构兼容"""
    if not _amap_key():
        return json.dumps(
            {"status": "error", "message": "AMAP_KEY 未配置"},
            ensure_ascii=False,
        )

    params = {}
    if ip:
        params["ip"] = ip

    data = await _amap_get(AMAP_IP_URL, params)
    if not data or data.get("status") != "1":
        return json.dumps(
            {"status": "error", "message": (data or {}).get("info", "IP 定位失败")},
            ensure_ascii=False,
        )

    province = data.get("province", "") or ""
    city = data.get("city", "") or ""
    adcode = data.get("adcode", "") or ""
    isp = data.get("isp", "") or ""
    rectangle = data.get("rectangle", "") or ""

    # rectangle 格式: "lng1,lat1;lng2,lat2" → 取中心点
    lat = lon = 0.0
    if rectangle and ";" in rectangle and "," in rectangle:
        try:
            p1, p2 = rectangle.split(";")
            lng1, lat1 = (float(x) for x in p1.split(","))
            lng2, lat2 = (float(x) for x in p2.split(","))
            lng_gcj = (lng1 + lng2) / 2
            lat_gcj = (lat1 + lat2) / 2
            lon, lat = gcj02_to_wgs84(lng_gcj, lat_gcj)
        except (ValueError, TypeError):
            pass

    return json.dumps(
        {
            "status": "success",
            "ip": ip or "本机",
            "country": "中国",
            "regionName": province,
            "city": city,
            "adcode": adcode,
            "isp": isp,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "rectangle": rectangle,
        },
        ensure_ascii=False,
    )


# ===========================================================================
# 7. 静态地图 URL 生成
# ===========================================================================

def static_map_url_amap(
    lat: float,
    lon: float,
    markers: Optional[list[dict]] = None,
    zoom: int = 15,
    width: int = 600,
    height: int = 400,
) -> Optional[str]:
    """
    生成高德静态地图 URL（直接返回图片）。
    markers: [{"lat": float, "lon": float, "label": "A", "color": "0xFF0000"}, ...]
              label 默认 A-Z，color 默认 0xFF3300
    返回的 URL 中坐标已是 GCJ-02。
    """
    if not _amap_key():
        return None

    zoom = max(1, min(17, int(zoom)))
    # 高德静态地图 zoom 范围 [1, 17]，但实际有用的是 [3, 17]

    # 构造 markers 参数
    if markers:
        groups = []
        colors = ["0xFF3300", "0x0080FF", "0x00B04F", "0xFF7E00", "0x9C27B0", "0x795548"]
        for idx, m in enumerate(markers[:10]):
            label = m.get("label") or chr(65 + idx)
            color = m.get("color") or colors[idx % len(colors)]
            lng_gcj, lat_gcj = wgs84_to_gcj02(m["lon"], m["lat"])
            groups.append(f"mid,{color},{label}:{lng_gcj},{lat_gcj}")
        markers_param = "|".join(groups)
    else:
        lng_gcj, lat_gcj = wgs84_to_gcj02(lon, lat)
        markers_param = f"mid,0xFF3300,:{lng_gcj},{lat_gcj}"

    # 中心点：如果有 markers 取第一个，否则用 lat/lon
    if markers:
        first = markers[0]
        center_lng_gcj, center_lat_gcj = wgs84_to_gcj02(first["lon"], first["lat"])
    else:
        center_lng_gcj, center_lat_gcj = wgs84_to_gcj02(lon, lat)

    from urllib.parse import urlencode
    params = {
        "location": f"{center_lng_gcj},{center_lat_gcj}",
        "zoom": str(zoom),
        "size": f"{width}x{height}",
        "scale": "2",
        "markers": markers_param,
    }
    return f"{AMAP_STATICMAP_URL}?{urlencode(params)}&key={_amap_key()}"


async def get_static_map_bytes_amap(
    lat: float,
    lon: float,
    markers: Optional[list[dict]] = None,
    zoom: int = 15,
    width: int = 600,
    height: int = 400,
) -> Optional[bytes]:
    """直接获取高德静态地图图片字节流"""
    url = static_map_url_amap(lat, lon, markers=markers, zoom=zoom, width=width, height=height)
    if not url:
        return None
    # _amap_get_bytes 不能用，因为 URL 已含 key
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=AMAP_TIMEOUT),
                headers={"User-Agent": USER_AGENT},
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                if len(data) < 500 or data[:1] in (b"{", b"["):
                    return None
                return data
    except Exception:
        return None


# ===========================================================================
# 8. 配额状态查询（供调试用）
# ===========================================================================

def get_quota_status() -> dict[str, Any]:
    """返回当前配额状态"""
    state = _load_quota_state()
    return {
        "enabled": is_enabled(),
        "today_date": state.get("date"),
        "poi_used_today": state.get("poi_count", 0),
        "poi_daily_limit": AMAP_POI_DAILY_LIMIT,
        "poi_remaining_today": max(0, AMAP_POI_DAILY_LIMIT - state.get("poi_count", 0)),
        "poi_monthly_quota": 5000,
        "lbs_monthly_quota": 150000,
        "cache_size": len(_poi_cache),
        "quota_file": str(_QUOTA_FILE),
    }
