"""网页搜索的可编辑配置。

部署者可直接修改本文件，无需改动搜索逻辑。修改后请重启应用，使配置重新加载。
"""

# 是否在 web_search 的搜索结果中启用域名黑名单过滤。
WEB_SEARCH_DOMAIN_FILTER_ENABLED = True

# 黑名单规则。每一项自行决定匹配范围，不再使用全局匹配模式：
#
# 1. "example.com"：只匹配精确主机名 example.com。
# 2. "[*.]example.com"：匹配 example.com 及其全部子域名，例如
#    www.example.com、news.example.com。这是屏蔽整个站点时推荐的写法。
# 3. "*.example.com"：只匹配 example.com 的子域名，不匹配 example.com 本身。
#    适合仅需屏蔽某类二级/多级子域名的情况。
#
# 仅填写域名规则，不要填写协议、端口、路径、查询参数或其他通配符。
BLACKLIST_DOMAINS = (
    # 问答与社交平台：屏蔽根域名与所有子域名。
    "[*.]zhihu.com",
    "[*.]weixin.qq.com",
    "[*.]xiaohongshu.com",
    "[*.]douyin.com",
    "[*.]kuaishou.com",
    "[*.]bilibili.com",
    "[*.]weibo.com",

    # 泛技术采集与低质博客：屏蔽根域名与所有子域名。
    "[*.]csdn.net",
    "[*.]jb51.net",
    "[*.]360doc.com",

    # 低质自媒体与问答聚合：只屏蔽写明的精确主机名。
    "baijiahao.baidu.com",
    "zhidao.baidu.com",

    # 示例：只屏蔽 example.com 的子域名，不屏蔽根域名。
    # "*.example.com",
)

# True：对 [*.]example.com 类型的规则，生成 `-site:example.com` 并发送给
# 支持 exclude 参数的 marcopesani/mcp-server-serper，在上游阶段预先排除。
# 精确规则（example.com）和仅子域名规则（*.example.com）不发送 -site:，以免
# 过度过滤；它们由本地 URL 主机名匹配作为最终保证。
# 如果实际连接的 google_search MCP 不接受 exclude 参数，请改为 False。
WEB_SEARCH_UPSTREAM_DOMAIN_EXCLUDE_ENABLED = True

# fetch_url 的根路径首页回退。仅当请求 URL 没有查询参数或片段且路径为 `/` 时，
# 在常规抓取与正文提取均失败后，按顺序尝试下列同站点路径。
# 例如 https://www.battleofballs.com/ 失败时，会尝试
# https://www.battleofballs.com/index/。每项必须是以 `/` 开头的站内路径。
FETCH_URL_ROOT_FALLBACK_ENABLED = True
FETCH_URL_ROOT_FALLBACK_PATHS = (
    "/index/",
)

# web_search 未传 num_results 时返回的默认结果条数。
WEB_SEARCH_DEFAULT_RESULTS = 10

# 单次 web_search 允许返回的最大结果条数。
WEB_SEARCH_MAX_RESULTS = 50

# 为弥补黑名单过滤后的空缺，向上游额外获取候选结果的倍率。
# 例如请求 10 条、倍率为 2 时，最多先获取 20 条，再过滤并保留前 10 条。
WEB_SEARCH_CANDIDATE_MULTIPLIER = 2

# 上游搜索请求的候选结果硬上限。设为 50 可保持原有 Serper/Google 查询规模。
WEB_SEARCH_MAX_CANDIDATES = 50

# 传给上游搜索服务的地区与界面语言参数。
WEB_SEARCH_REGION = "cn"
WEB_SEARCH_LANGUAGE = "zh-cn"
