# -*- coding: utf-8 -*-
import requests
import random
import re
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import time
import logging
from typing import List, Dict, Optional
from pathlib import PurePosixPath

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# 搜索引擎配置（不变）
API_CONFIG = {
    "google": {
        "api_key": "AIzaSyAhnNDFupCb_gn5ZatjGS8xYDHcYpEl5TA",
        "cx": "5485297013e9949eb",
        "search_url": "https://www.googleapis.com/customsearch/v1",
        "enabled": True
    }
}

EXCLUDED_DOMAINS = [
    'csdn.net', 'blog.csdn.net',
    'zhihu.com', 'www.zhihu.com',
    'baidu.com', 'm.baidu.com',
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
]

def _is_excluded_url(url: str) -> bool:
    """检查URL是否属于排除列表"""
    try:
        parsed_url = urlparse(url)
        domain = parsed_url.netloc.lower()
        return any(excluded in domain for excluded in EXCLUDED_DOMAINS)
    except Exception as e:
        logger.warning(f"URL解析失败: {url}, 错误: {str(e)}")
        return False

def fetch_webpage_content(url: str, max_retries: int = 3, verify_ssl: bool = True) -> str:
    """抓取网页内容，带重试机制和SSL处理"""
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=10, verify=verify_ssl)
            response.raise_for_status()
            response.encoding = response.apparent_encoding
            soup = BeautifulSoup(response.text, "html.parser")
            content = _extract_main_content(soup)
            return _clean_content(content, max_length=5000)
        except requests.exceptions.SSLError as ssl_err:
            logger.error(f"SSL错误 (尝试 {attempt + 1}/{max_retries}): {str(ssl_err)}")
            if attempt == max_retries - 1:
                return "⚠️ SSL验证失败"
            verify_ssl = False
            time.sleep(2 ** attempt)
        except requests.exceptions.RequestException as e:
            logger.error(f"抓取失败 (尝试 {attempt + 1}/{max_retries}): {str(e)}")
            if attempt == max_retries - 1:
                return "⚠️ 无法抓取内容（可能是访问限制）"
            time.sleep(2 ** attempt)
    return "⚠️ 抓取失败: 重试次数耗尽"

def universal_search(query: str, num_results: int = 3) -> str:
    """通用搜索引擎接口，优化效率以返回指定数量的有效结果"""
    active_apis = [k for k, v in API_CONFIG.items() if v["enabled"]]
    if not active_apis:
        return "⚠️ 无可用搜索引擎服务"

    max_retries = 2
    for api_type in active_apis:
        config = API_CONFIG[api_type]
        for attempt in range(max_retries):
            try:
                if api_type == "google":
                    params = {
                        "key": config["api_key"],
                        "cx": config["cx"],
                        "q": query,
                        "num": 10,
                        "lr": "lang_zh-CN",
                        "safe": "active"
                    }
                    response = requests.get(config["search_url"], params=params, timeout=15)
                    response.raise_for_status()
                    results = _process_google_results(response.json())

                    valid_results = []
                    for result in results:
                        if len(valid_results) >= num_results:
                            break
                        logger.info(f"抓取内容: {result['link']}")
                        content = fetch_webpage_content(result['link'])
                        if content and not content.startswith("⚠️"):
                            result["full_content"] = content
                            valid_results.append(result)

                    if len(valid_results) < num_results:
                        logger.warning(f"仅找到 {len(valid_results)} 个有效结果，未达到 {num_results}")
                    return _format_search_results(valid_results[:num_results], api_type)

            except requests.exceptions.RequestException as e:
                logger.error(f"[{api_type}] 请求失败 (尝试 {attempt + 1}/{max_retries}): {str(e)}")
                if attempt == max_retries - 1:
                    API_CONFIG[api_type]["enabled"] = False
                time.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"[{api_type}] 未知错误: {str(e)}")
                break
        if not API_CONFIG[api_type]["enabled"]:
            continue
    return "⚠️ 所有搜索引擎服务不可用"

def _extract_main_content(soup: BeautifulSoup) -> str:
    """提取网页正文内容，避免无关信息"""
    for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'form']):
        tag.decompose()

    possible_selectors = [
        'article', '[role="main"]', '.content', '.article',
        '.post', '.main', '#content', '#main'
    ]
    main_content = None
    for selector in possible_selectors:
        main_content = soup.select_one(selector)
        if main_content:
            break

    if not main_content:
        main_content = soup

    paragraphs = []
    for p in main_content.find_all(['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
        text = p.get_text().strip()
        if len(text) >= 20 and not any(
            keyword in text.lower() for keyword in ["跳转", "导航", "关注", "直播", "上一页", "下一页"]
        ):
            paragraphs.append(text)

    return " ".join(paragraphs[:10]) if paragraphs else "未找到正文内容"

def _process_google_results(data: dict) -> List[Dict[str, str]]:
    """处理Google搜索结果并过滤排除域名"""
    items = data.get("items", [])
    filtered_items = []
    for item in items:
        link = item.get("link", "")
        if not _is_excluded_url(link):
            filtered_items.append({
                "title": item.get("title", "无标题"),
                "link": link
            })
    return filtered_items

def _format_search_results(items: List[Dict[str, str]], api_type: str) -> str:
    """格式化搜索结果为 HTML"""
    if not items:
        return f"⚠️ {api_type.capitalize()} 未找到相关结果"

    search_summary = f"🔍 <b>{api_type.capitalize()}搜索结果</b><br><br>"
    for i, item in enumerate(items, 1):
        title = _clean_content(item.get("title", "无标题"), 80)
        link = item.get("link", "")
        full_content = item.get("full_content", "未抓取内容")

        search_summary += (
            f"{i}. <b>{title}</b><br>"
            f"▸ 内容: {full_content[:500]}...<br>"
            f"🌐 <a href=\"{link}\">来源</a><br><br>"
        )
    return search_summary

def _clean_content(text: str, max_length: int) -> str:
    """清理文本内容，确保编码安全"""
    text = re.sub(r"广告|Sponsored|推荐|热门", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    try:
        text = text.encode('utf-8', 'ignore').decode('utf-8')
    except Exception as e:
        logger.warning(f"文本编码清理失败: {str(e)}")
        text = text.encode('utf-8', 'replace').decode('utf-8')
    if len(text) > max_length:
        last_valid_index = min(max_length, len(text) - 1)
        for i in range(last_valid_index, 0, -1):
            if ord(text[i]) < 128 or text[i] in "。！？；，、":
                last_valid_index = i
                break
        text = text[:last_valid_index] + "..."
    return text

if __name__ == "__main__":
    try:
        while True:
            query = input("请输入搜索内容（输入q退出）: ").strip()
            if query.lower() == "q":
                break
            if not query:
                print("请输入有效的搜索内容！")
                continue
            result = universal_search(query)
            print("\n" + result + "\n")
    except KeyboardInterrupt:
        logger.info("用户中断程序")
    except Exception as e:
        logger.error(f"程序异常: {str(e)}")