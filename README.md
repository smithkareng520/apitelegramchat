# apitelegramchat 高德地图全量替换版

## 一、这是什么

把 `apitelegramchat-main` 原本依赖 OpenStreetMap (Nominatim + Overpass + OSRM) 的地图栈，
**全量替换成高德 Web 服务 API**，让"附近的 KFC"这类查询在中国大陆真正可用。

## 二、替换覆盖范围

| 原 API | 用途 | 替换为 | 月配额 |
|---|---|---|---|
| Nominatim (OSM) | 地理编码 | ✅ 高德 `/v3/geocode/geo` | 150,000 |
| Overpass (OSM) | POI 周边搜索 | ✅ 高德 `/v3/place/around` | 5,000 ⚠️ |
| Overpass (OSM) | 地点详情 | ✅ 高德 `/v3/place/text` | 5,000 ⚠️ |
| OSRM driving | 驾车路径 | ✅ 高德 `/v3/direction/driving` | 150,000 |
| OSRM walking | 步行路径 | ✅ 高德 `/v3/direction/walking` | 150,000 |
| OSRM cycling | 骑行路径 | ✅ 高德 `/v4/direction/bicycling` | 150,000 |
| OSRM transit | 公交路径 | ✅ 高德 `/v3/direction/transit/integrated` | 150,000 |
| ip-api.com | IP 定位 | ✅ 高德 `/v3/ip` | 150,000 |
| Geoapify / OSM staticmap | 静态地图 | ✅ 高德 `/v3/staticmap` | 150,000 |
| open-elevation | 海拔 | ⏸️ 保留（高德无） | - |
| ORS | 等时圈 | ⏸️ 保留（高德等时圈需企业认证） | - |

⚠️ 标的两个是"基础搜索服务"，**月配额仅 5000**，已加缓存 + 每日配额保护，超出自动降级回 Overpass。

## 三、关键设计

### 1. 坐标系自动转换

- Telegram / Google Maps / OSM 用 **WGS-84**
- 高德用 **GCJ-02（火星坐标系）**
- 所有进出高德的坐标都在 `amap_integration.py` 内部完成转换，对外接口一律 WGS-84

### 2. POI 搜索配额保护（防 5000/月 用爆）

- **缓存层**：同一坐标 ±100m + 同一关键词，1 小时内复用结果，不消耗配额
- **每日计数**：持久化到 `~/.apitelegramchat_amap_quota.json`，每日 0 点重置
- **阈值保护**：默认每日上限 140 次（月配额 5000 ÷ 30 天 ≈ 167，留余量）
- **降级机制**：超过阈值后自动 fallback 回 Overpass，不影响功能

### 3. 静态地图渲染优先级

```
1. 本地 R2 缓存命中（同坐标同参数复用）
2. 高德静态地图（中国路网密、POI 标注全）
3. Geoapify（如配置 GEOAPIFY_KEY）
4. OSM staticmap（兜底）
```

### 4. Telegram 原生 location 消息处理

原 `app.py` 不处理 `message.location`，用户给 bot 发定位 pin 它根本不认。
现已加入处理：用户发位置 → 反查中文地址 → 注入对话上下文 → AI 后续可直接用坐标调 search_poi / route。

## 四、安装步骤

### 1. 解压项目

```bash
unzip apitelegramchat-amap.zip
cd apitelegramchat-amap
```

### 2. 申请高德 Key

1. 访问 https://lbs.amap.com/
2. 控制台 → 我的应用 → 创建新应用
3. **服务平台选 "Web服务"**（不是 Web端 JS API，也不是 Android/iOS）
4. 复制生成的 Key

### 3. 设置环境变量

部署平台（Render / Docker / 本地）增加：

```
AMAP_KEY=你的高德Web服务Key
AMAP_POI_DAILY_LIMIT=140    # 可选，默认 140
AMAP_CACHE_TTL=3600         # 可选，POI 缓存秒数
```

### 4. 安装依赖 + 启动

```bash
pip install -r requirements.txt
python -m apitelegramchat.entrypoints.telegram_app
# 或
python app.py
```

### 5. 验证

在 Telegram 里：
1. 给 bot 发定位 pin（聊天框左下附件按钮 → 位置 → 发送当前位置）
2. 再发："附近的 KFC"
3. 标记点应落在真实 KFC 门店上

更直观的验证：
- 发 "geocode 北京天安门" → 应返回 `39.9087, 116.3975` 附近
- 发 "search_poi 39.9087,116.3975 KFC" → 应列出周边真实 KFC

## 五、文件改动清单

| 文件 | 改动 |
|---|---|
| `src/apitelegramchat/amap_integration.py` | **新增** 高德 API 全量封装（约 600 行） |
| `src/apitelegramchat/search_engine.py` | 加 7 处补丁：import + `_geocode_coords` / `execute_geocode` / `execute_search_poi` / `execute_route` / `execute_place_details` / `execute_ip_geo` 开头加"高德优先"分支 |
| `src/apitelegramchat/tool_executors.py` | 加 1 处补丁：`_get_static_map_image()` 在 R2 缓存后、Geoapify 前插入高德静态地图 |
| `src/apitelegramchat/app.py` | 加 1 处补丁：媒体组分支前插入 Telegram location 消息处理 |

所有补丁用 `# === [amap_integration patch] ===` ... `# === [/amap_integration patch] ===` 标记包裹，肉眼可识别。

## 六、可选：再次运行补丁脚本

如果你想在自己 fork 的项目上重新打补丁（比如想合并上游更新），可以：

```bash
# 把 apply_patches.py 和 amap_integration.py 放到项目根目录
python apply_patches.py
```

补丁脚本是**幂等**的，重复运行不会重复插入。

## 七、配额监控

查询当日 POI 用量：

```python
from apitelegramchat.amap_integration import get_quota_status
print(get_quota_status())
# {'enabled': True, 'today_date': '2026-08-05', 'poi_used_today': 12, 'poi_daily_limit': 140, ...}
```

或者直接看文件：

```bash
cat ~/.apitelegramchat_amap_quota.json
# {"date": "2026-08-05", "poi_count": 12}
```

## 八、回滚

补丁全部用标记包裹，可以一键回滚：

```bash
cd apitelegramchat-amap
# 用 sed 删除所有补丁块
for f in src/apitelegramchat/search_engine.py src/apitelegramchat/tool_executors.py src/apitelegramchat/app.py; do
    python -c "
import re, sys
text = open('$f', encoding='utf-8').read()
text = re.sub(r'\n?\s*# === \[amap_integration patch\][\s\S]*?# === \[/amap_integration patch\] ===\n?', '\n', text)
open('$f', 'w', encoding='utf-8').write(text)
"
done
rm src/apitelegramchat/amap_integration.py
```

或者从原 zip 重新解压覆盖。

## 九、API 文档参考

- 高德 Web 服务总览：https://lbs.amap.com/api/webservice/summary
- 地理编码/逆地理：https://lbs.amap.com/api/webservice/guide/api/georegeo
- POI 搜索：https://lbs.amap.com/api/webservice/guide/api/search
- 路径规划：https://lbs.amap.com/api/webservice/guide/api/direction
- 距离测量：https://lbs.amap.com/api/webservice/guide/api/distance
- IP 定位：https://lbs.amap.com/api/webservice/guide/api/ipre
- 静态地图：https://lbs.amap.com/api/webservice/guide/api/staticmaps
- 配额说明：https://lbs.amap.com/api/webservice/guide/tools/flowlevel


## Skills
This repository ships Anthropic-compatible skills under `.claude/skills/` and does not expose a custom prompt-skill registry.
