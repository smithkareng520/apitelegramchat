"""
apply_patches.py
================

把这个文件和 amap_integration.py 一起放到 apitelegramchat-main 项目根目录，
然后运行：

    python apply_patches.py

它会做四件事：
1. 把 amap_integration.py 复制到 src/apitelegramchat/amap_integration.py
2. 修改 search_engine.py：在以下函数开头加 "高德优先" 分支
   - _geocode_coords       (内部辅助函数)
   - execute_geocode       (地理编码)
   - execute_search_poi    (POI 周边搜索)
   - execute_route         (路径规划：驾车/步行/骑行/公交)
   - execute_place_details (地点详情)
   - execute_ip_geo        (IP 定位)
3. 修改 tool_executors.py：在 _get_static_map_image() 中加高德静态地图源
4. 修改 app.py：增加对 Telegram 原生 location 消息的处理

幂等：重复运行不会重复插入。所有补丁用 `# === [amap_integration patch] ===`
标记包裹，肉眼可识别，方便回滚。
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC_PKG = ROOT / "src" / "apitelegramchat"
SEARCH_ENGINE = SRC_PKG / "search_engine.py"
TOOL_EXECUTORS = SRC_PKG / "tool_executors.py"
APP_PY = SRC_PKG / "app.py"
AMAP_FILE = SRC_PKG / "amap_integration.py"


def _copy_amap_module() -> None:
    src = ROOT / "amap_integration.py"
    if not src.exists():
        print("[skip] amap_integration.py 不在同目录")
        return
    shutil.copyfile(src, AMAP_FILE)
    print(f"[ok]   写入 {AMAP_FILE.relative_to(ROOT)}")


# ---------------------------------------------------------------------------
# search_engine.py 补丁
# ---------------------------------------------------------------------------

AMAP_IMPORT_BLOCK = (
    "# === [amap_integration patch] 高德地图数据源 ===\n"
    "try:\n"
    "    from apitelegramchat import amap_integration as _amap\n"
    "except Exception:  # pragma: no cover\n"
    "    _amap = None\n"
    "# === [/amap_integration patch] ===\n"
)

GEOCODE_COORDS_PATCH = (
    '    # === [amap_integration patch] 高德优先 ===\n'
    '    if _amap is not None and _amap.is_enabled():\n'
    '        return await _amap._amap_geocode_coords(address)\n'
    '    # === [/amap_integration patch] ===\n'
)

EXECUTE_GEOCODE_PATCH = (
    '    # === [amap_integration patch] 高德优先 ===\n'
    '    if _amap is not None and _amap.is_enabled():\n'
    '        return await _amap.execute_geocode_amap(address)\n'
    '    # === [/amap_integration patch] ===\n'
)

EXECUTE_SEARCH_POI_PATCH = (
    '    # === [amap_integration patch] 高德优先 ===\n'
    '    if _amap is not None and _amap.is_enabled():\n'
    '        result = await _amap.execute_search_poi_amap(lat, lon, query, radius=radius, max_results=max_results)\n'
    '        # 配额耗尽时降级回 Overpass；其它情况（成功/无结果/错误）直接返回\n'
    '        try:\n'
    '            _r = json.loads(result)\n'
    '            if _r.get("status") != "quota_exceeded":\n'
    '                return result\n'
    '        except Exception:\n'
    '            return result\n'
    '    # === [/amap_integration patch] ===\n'
)

EXECUTE_ROUTE_PATCH = (
    '    # === [amap_integration patch] 高德优先 ===\n'
    '    if _amap is not None and _amap.is_enabled():\n'
    '        return await _amap.execute_route_amap(start, end, profile)\n'
    '    # === [/amap_integration patch] ===\n'
)

EXECUTE_PLACE_DETAILS_PATCH = (
    '    # === [amap_integration patch] 高德优先 ===\n'
    '    if _amap is not None and _amap.is_enabled():\n'
    '        result = await _amap.execute_place_details_amap(query, lat=lat, lon=lon)\n'
    '        try:\n'
    '            _r = json.loads(result)\n'
    '            if _r.get("status") != "quota_exceeded":\n'
    '                return result\n'
    '        except Exception:\n'
    '            return result\n'
    '    # === [/amap_integration patch] ===\n'
)

EXECUTE_IP_GEO_PATCH = (
    '    # === [amap_integration patch] 高德优先 ===\n'
    '    if _amap is not None and _amap.is_enabled():\n'
    '        return await _amap.execute_ip_geo_amap(ip)\n'
    '    # === [/amap_integration patch] ===\n'
)


def _patch_search_engine() -> None:
    text = SEARCH_ENGINE.read_text(encoding="utf-8")
    changed = False

    # 1) 加 import
    if "amap_integration patch" not in text:
        m = re.search(r"^from apitelegramchat\.[^\n]+\n", text, re.M)
        if not m:
            print("[skip] search_engine.py 找不到导入位置")
            return
        insert_at = m.end()
        text = text[:insert_at] + "\n" + AMAP_IMPORT_BLOCK + text[insert_at:]
        changed = True

    # 2) _geocode_coords
    if "_amap._amap_geocode_coords" not in text:
        text = re.sub(
            r"(async def _geocode_coords\(address: str\)[^\n]*\n(?:[^\n]*\n)*?\s+key = address\.lower\(\)\.strip\(\)\n)",
            lambda m: m.group(1) + GEOCODE_COORDS_PATCH,
            text,
            count=1,
        )
        changed = True

    # 3) execute_geocode
    if "_amap.execute_geocode_amap" not in text:
        text = re.sub(
            r"(async def execute_geocode\(address: str\) -> str:\n)",
            lambda m: m.group(1) + EXECUTE_GEOCODE_PATCH,
            text,
            count=1,
        )
        changed = True

    # 4) execute_search_poi
    if "_amap.execute_search_poi_amap" not in text:
        text = re.sub(
            r"(async def execute_search_poi\(lat: float, lon: float, query: str, radius: int = 1000, max_results: int = 15\) -> str:\n)",
            lambda m: m.group(1) + EXECUTE_SEARCH_POI_PATCH,
            text,
            count=1,
        )
        changed = True

    # 5) execute_route
    if "_amap.execute_route_amap" not in text:
        text = re.sub(
            r"(async def execute_route\(start: str, end: str, profile: str = \"driving\"\) -> str:\n)",
            lambda m: m.group(1) + EXECUTE_ROUTE_PATCH,
            text,
            count=1,
        )
        changed = True

    # 6) execute_place_details
    if "_amap.execute_place_details_amap" not in text:
        text = re.sub(
            r"(async def execute_place_details\(query: str,\n\s*lat: float = None,\n\s*lon: float = None\) -> str:\n)",
            lambda m: m.group(1) + EXECUTE_PLACE_DETAILS_PATCH,
            text,
            count=1,
        )
        changed = True

    # 7) execute_ip_geo
    if "_amap.execute_ip_geo_amap" not in text:
        # 先看一下原函数签名
        m = re.search(r"async def execute_ip_geo\([^)]*\)[^\n]*\n", text)
        if m:
            text = text.replace(
                m.group(0),
                m.group(0) + EXECUTE_IP_GEO_PATCH,
                1,
            )
            changed = True
        else:
            print("[warn] 未找到 execute_ip_geo 函数定义，跳过")

    if changed:
        SEARCH_ENGINE.write_text(text, encoding="utf-8")
        print(f"[ok]   已打补丁 {SEARCH_ENGINE.relative_to(ROOT)}")
    else:
        print(f"[skip] {SEARCH_ENGINE.relative_to(ROOT)} 已打过补丁")


# ---------------------------------------------------------------------------
# tool_executors.py 补丁：高德静态地图优先
# ---------------------------------------------------------------------------

TOOL_EXECUTORS_AMAP_BLOCK = (
    "    # === [amap_integration patch] 高德静态地图优先 ===\n"
    "    try:\n"
    "        from apitelegramchat import amap_integration as _amap\n"
    "        if _amap.is_enabled():\n"
    "            amap_url = _amap.static_map_url_amap(\n"
    "                lat, lon,\n"
    "                markers=[{'lat': m['lat'], 'lon': m['lon']} for m in markers] if markers else None,\n"
    "                zoom=zoom, width=width, height=height,\n"
    "            )\n"
    "            if amap_url:\n"
    "                try:\n"
    "                    async with aiohttp.ClientSession() as session:\n"
    "                        async with session.get(\n"
    "                            amap_url,\n"
    "                            timeout=aiohttp.ClientTimeout(total=12)\n"
    "                        ) as resp:\n"
    "                            if resp.status == 200:\n"
    "                                img_bytes = await resp.read()\n"
    "                                if len(img_bytes) > 500 and img_bytes[:1] not in (b'{', b'['):\n"
    "                                    uploaded_url = await upload_bytes_to_r2(\n"
    "                                        img_bytes, r2_key, 'image/png'\n"
    "                                    )\n"
    "                                    return uploaded_url\n"
    "                except Exception as e:\n"
    "                    logger.warning(f'高德静态地图失败: {e}')\n"
    "    except Exception:\n"
    "        pass\n"
    "    # === [/amap_integration patch] ===\n"
)


def _patch_tool_executors() -> None:
    text = TOOL_EXECUTORS.read_text(encoding="utf-8")
    if "amap_integration patch" in text:
        print(f"[skip] {TOOL_EXECUTORS.relative_to(ROOT)} 已打过补丁")
        return

    # 在 _get_static_map_image 函数中，"if await file_exists_in_r2(r2_key):"
    # 这一段的闭合之后，"备用来源列表" 之前插入高德分支
    # 锚点：marker_str = "" 这一行（在 sources 列表初始化前）
    anchor = "    # 备用来源列表\n"
    if anchor not in text:
        # 兼容其它缩进
        anchor2 = "# 备用来源列表"
        if anchor2 in text:
            anchor = anchor2
        else:
            print(f"[skip] {TOOL_EXECUTORS.relative_to(ROOT)} 找不到插入锚点（# 备用来源列表）")
            return

    text = text.replace(anchor, TOOL_EXECUTORS_AMAP_BLOCK + anchor, 1)
    TOOL_EXECUTORS.write_text(text, encoding="utf-8")
    print(f"[ok]   已打补丁 {TOOL_EXECUTORS.relative_to(ROOT)}")


# ---------------------------------------------------------------------------
# app.py 补丁：增加 location 消息处理
# ---------------------------------------------------------------------------

LOCATION_HANDLER_SNIPPET = (
    '            # ── Telegram 原生 location（用户分享位置 / 实时位置） ───────\n'
    '            # === [amap_integration patch] ===\n'
    '            if "location" in msg and "text" not in msg:\n'
    '                loc = msg["location"]\n'
    '                lat = loc.get("latitude")\n'
    '                lon = loc.get("longitude")\n'
    '                if lat is None or lon is None:\n'
    '                    return "OK", 200\n'
    '\n'
    '                # 反查中文地址（可选，需要 AMAP_KEY）\n'
    '                addr_text = f"{lat:.6f},{lon:.6f}"\n'
    '                try:\n'
    '                    from apitelegramchat import amap_integration as _amap\n'
    '                    if _amap.is_enabled():\n'
    '                        rev = await _amap.reverse_geocode(lat, lon)\n'
    '                        if rev and rev.get("formatted"):\n'
    '                            addr_text = rev["formatted"]\n'
    '                except Exception:\n'
    '                    pass\n'
    '\n'
    '                content_text = (\n'
    '                    f"📎 用户分享了当前位置\\n"\n'
    '                    f"坐标：{lat:.6f}, {lon:.6f} (WGS-84)\\n"\n'
    '                    f"地址：{addr_text}\\n\\n"\n'
    '                    f"如果用户问起『附近』『周边』等，请直接以此坐标作为中心点，"\n'
    '                    f"调用 search_poi / route / distance 等工具，无需再调用 geocode。"\n'
    '                ).replace("\\\\n", "\\n")\n'
    '                user_message = {"role": "user", "content": content_text}\n'
    '                await _interrupt_active_generation(chat_id)\n'
    '                task = asyncio.create_task(\n'
    '                    _handle_text_message(chat_id, content_text, username, user_message)\n'
    '                )\n'
    '                async with active_tasks_lock:\n'
    '                    active_tasks[chat_id] = task\n'
    '                task.add_done_callback(lambda t: asyncio.create_task(_cleanup_task(chat_id, t)))\n'
    '                return "OK", 200\n'
    '            # === [/amap_integration patch] ===\n'
    '\n'
)


def _patch_app_py() -> None:
    text = APP_PY.read_text(encoding="utf-8")
    if "amap_integration patch" in text:
        print(f"[skip] {APP_PY.relative_to(ROOT)} 已打过补丁")
        return

    # 插入点：放在 "── 媒体组（图片） ──" 之前
    # 注意：原 app.py 中这行有 12 个空格的前导缩进，marker 必须包含它，
    # 否则 str.replace 后第一行会多出 12 个空格的缩进。
    marker = "            # ── 媒体组（图片） ─────────────────────────────────────────────"
    if marker not in text:
        print(f"[skip] {APP_PY.relative_to(ROOT)} 找不到插入锚点")
        return

    text = text.replace(marker, LOCATION_HANDLER_SNIPPET + marker, 1)
    APP_PY.write_text(text, encoding="utf-8")
    print(f"[ok]   已打补丁 {APP_PY.relative_to(ROOT)}")


# ---------------------------------------------------------------------------
def main() -> int:
    if not SRC_PKG.exists():
        print(f"[err]  找不到包目录 {SRC_PKG}")
        print("       请把 apply_patches.py 放在 apitelegramchat-main 项目根目录下再运行")
        return 1

    _copy_amap_module()
    _patch_search_engine()
    _patch_tool_executors()
    _patch_app_py()

    print("\n下一步：")
    print("  1) 在部署平台/本地环境设置环境变量：")
    print("       AMAP_KEY=<你的高德 Web 服务 Key>")
    print("       AMAP_POI_DAILY_LIMIT=140     (可选，默认 140，月配额 5000 留余量)")
    print("       AMAP_CACHE_TTL=3600          (可选，POI 搜索缓存有效期秒数)")
    print("     申请地址：https://lbs.amap.com/ → 控制台 → 我的应用 → 添加 Key → 服务平台选『Web服务』")
    print("  2) 重启 bot")
    print("  3) 验证：给 bot 发个定位 pin（聊天框左下附件按钮 → 位置），再问『附近的 KFC』")
    print("  4) 配额查询：在 bot 里输入 /amap_quota 可查看当日 POI 用量（如未实现可忽略）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
