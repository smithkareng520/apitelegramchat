"""网页搜索域名黑名单的规则解析、匹配与候选数量计算。"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from web_search_settings import (
    BLACKLIST_DOMAINS,
    WEB_SEARCH_CANDIDATE_MULTIPLIER,
    WEB_SEARCH_DEFAULT_RESULTS,
    WEB_SEARCH_DOMAIN_FILTER_ENABLED,
    WEB_SEARCH_MAX_CANDIDATES,
    WEB_SEARCH_MAX_RESULTS,
)


@dataclass(frozen=True)
class DomainRule:
    """一条已解析的域名黑名单规则。"""

    raw: str
    domain: str
    include_root: bool
    include_subdomains: bool

    def matches(self, hostname: str) -> bool:
        """判断主机名是否满足本规则。"""
        if self.include_root and hostname == self.domain:
            return True
        return self.include_subdomains and hostname.endswith(f".{self.domain}")


def _positive_int_setting(value: Any, default: int, *, minimum: int = 1) -> int:
    """读取可编辑配置中的正整数；非法值回退到安全默认值。"""
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def parse_blacklist_rules(domains: Any) -> tuple[DomainRule, ...]:
    """解析逐条黑名单规则，并忽略格式无效或重复的项目。

    支持三种规则：``example.com``（精确）、``[*.]example.com``（根域名及
    所有子域名）和 ``*.example.com``（仅子域名）。
    """
    if isinstance(domains, str):
        domains = (domains,)
    if not isinstance(domains, (tuple, list, set, frozenset)):
        return ()

    parsed: list[DomainRule] = []
    seen: set[str] = set()
    for entry in domains:
        if not isinstance(entry, str):
            continue
        raw = entry.strip().lower().rstrip(".")
        include_root = True
        include_subdomains = False
        domain = raw
        if raw.startswith("[*.]"):
            domain = raw[4:]
            include_subdomains = True
        elif raw.startswith("*."):
            domain = raw[2:]
            include_root = False
            include_subdomains = True

        # 规则仅接受主机名及上方定义的左侧通配前缀。
        if (
            not domain
            or "://" in domain
            or "/" in domain
            or "*" in domain
            or domain.startswith(".")
            or domain.endswith(".")
            or ".." in domain
        ):
            continue

        rule = DomainRule(
            raw=raw,
            domain=domain,
            include_root=include_root,
            include_subdomains=include_subdomains,
        )
        if rule.raw not in seen:
            seen.add(rule.raw)
            parsed.append(rule)
    return tuple(parsed)


SEARCH_MAX_RESULTS = _positive_int_setting(WEB_SEARCH_MAX_RESULTS, 50)
SEARCH_DEFAULT_RESULTS = min(
    _positive_int_setting(WEB_SEARCH_DEFAULT_RESULTS, 10),
    SEARCH_MAX_RESULTS,
)
SEARCH_MAX_CANDIDATES = max(
    SEARCH_MAX_RESULTS,
    _positive_int_setting(WEB_SEARCH_MAX_CANDIDATES, 50),
)
SEARCH_CANDIDATE_MULTIPLIER = _positive_int_setting(
    WEB_SEARCH_CANDIDATE_MULTIPLIER,
    2,
)
BLACKLIST_RULES = parse_blacklist_rules(BLACKLIST_DOMAINS)
BLACKLISTED_SEARCH_DOMAINS = tuple(rule.domain for rule in BLACKLIST_RULES)


def is_blacklisted_search_url(url: str) -> bool:
    """判断 URL 主机名是否命中任一逐条配置的黑名单规则。"""
    if not WEB_SEARCH_DOMAIN_FILTER_ENABLED or not BLACKLIST_RULES:
        return False
    try:
        hostname = urlsplit(url).hostname
    except (TypeError, ValueError):
        return False
    if not hostname:
        return False

    hostname = hostname.lower().rstrip(".")
    return any(rule.matches(hostname) for rule in BLACKLIST_RULES)


def filter_blacklisted_search_results(items: list[dict]) -> tuple[list[dict], int]:
    """过滤黑名单域名结果，并返回保留结果与过滤数量。"""
    if not WEB_SEARCH_DOMAIN_FILTER_ENABLED or not BLACKLIST_RULES:
        return items, 0

    kept: list[dict] = []
    filtered_count = 0
    for item in items:
        link = str(item.get("link") or "")
        if is_blacklisted_search_url(link):
            filtered_count += 1
            continue
        kept.append(item)
    return kept, filtered_count


def candidate_result_count(requested: int) -> int:
    """计算为补足过滤后的结果而向上游请求的候选数量。"""
    return min(
        requested * SEARCH_CANDIDATE_MULTIPLIER,
        SEARCH_MAX_CANDIDATES,
    )
