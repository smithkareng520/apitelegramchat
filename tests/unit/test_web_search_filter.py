# =====================================================================
# tests/unit/test_web_search_filter.py — 搜索域名黑名单规则引擎
# =====================================================================
# 被测关键路径：web_search 结果卫生管线（规则解析 → URL 匹配 → 结果过滤
#       → 候选数量补偿）。
# 覆盖：三种规则语法（精确 / [*.] 根+子域 / *. 仅子域）、非法与重复规则
#       过滤、子域名后缀安全（evil.com 反例）、过滤计数、候选数量计算。
# =====================================================================
import web_search_filter as wsf
from web_search_filter import (
    DomainRule,
    candidate_result_count,
    filter_blacklisted_search_results,
    is_blacklisted_search_url,
    parse_blacklist_rules,
)


# ---------------------------------------------------------------------
# 规则解析
# ---------------------------------------------------------------------
def test_exact_rule_matches_root_only():
    (rule,) = parse_blacklist_rules(["baijiahao.baidu.com"])
    assert rule.include_root is True
    assert rule.include_subdomains is False
    assert rule.matches("baijiahao.baidu.com") is True
    assert rule.matches("www.baijiahao.baidu.com") is False
    assert rule.matches("baidu.com") is False


def test_wildcard_bracket_rule_matches_root_and_subdomains():
    (rule,) = parse_blacklist_rules(["[*.]zhihu.com"])
    assert rule.include_root is True
    assert rule.include_subdomains is True
    assert rule.matches("zhihu.com") is True
    assert rule.matches("www.zhihu.com") is True
    assert rule.matches("a.b.zhihu.com") is True
    assert rule.matches("notzhihu.com") is False


def test_star_prefix_rule_matches_subdomains_only():
    (rule,) = parse_blacklist_rules(["*.example.com"])
    assert rule.include_root is False
    assert rule.include_subdomains is True
    assert rule.matches("example.com") is False
    assert rule.matches("www.example.com") is True
    assert rule.matches("deep.www.example.com") is True


def test_rules_normalized_lowercase_and_trailing_dot():
    rules = parse_blacklist_rules(["EXAMPLE.COM."])
    (rule,) = rules
    assert rule.domain == "example.com"
    assert rule.raw == "example.com"
    assert rule.matches("example.com") is True


def test_invalid_rules_filtered_out():
    invalid = [
        "",                      # 空
        "http://x.com",          # 带协议
        "x.com/path",            # 带路径
        "*.a.*",                 # 内部通配
        ".leading.dot",          # 前导点
        "trailing.",             # rstrip 后为合法？trailing. → rstrip('.') → trailing 合法！
        "double..dot",           # 连续点
    ]
    rules = parse_blacklist_rules(invalid)
    domains = [r.domain for r in rules]
    # 仅 trailing. 规范化后合法，其余全部被丢弃
    assert domains == ["trailing"]
    # 显式验证各类非法形态被拒
    for bad in ("", "http://x.com", "x.com/path", "*.a.*", ".leading", "double..dot"):
        assert parse_blacklist_rules([bad]) == ()


def test_duplicate_rules_deduped_after_normalization():
    rules = parse_blacklist_rules(["a.com", "A.COM.", " a.com "])
    assert len(rules) == 1
    assert rules[0].domain == "a.com"


def test_non_string_entries_skipped():
    rules = parse_blacklist_rules([1, None, True, "a.com"])
    assert len(rules) == 1


def test_unsupported_container_returns_empty():
    assert parse_blacklist_rules(12345) == ()
    assert parse_blacklist_rules({"a.com"}) != ()  # set 也支持
    assert parse_blacklist_rules(None) == ()


def test_string_input_treated_as_single_rule():
    (rule,) = parse_blacklist_rules("single.com")
    assert rule.domain == "single.com"


# ---------------------------------------------------------------------
# URL 匹配
# ---------------------------------------------------------------------
def test_is_blacklisted_url_by_settings_default_rules():
    # 使用 web_search_settings 出厂黑名单（[*.]zhihu.com 等）
    assert is_blacklisted_search_url("https://www.zhihu.com/question/123") is True
    assert is_blacklisted_search_url("https://zhuanlan.zhihu.com/p/456") is True
    assert is_blacklisted_search_url("https://baijiahao.baidu.com/s?id=1") is True
    assert is_blacklisted_search_url("https://www.example.org/open-source") is False


def test_suffix_spoofing_is_not_blocked():
    # 安全属性：后缀伪装域名不能被子域匹配误伤
    assert is_blacklisted_search_url("https://zhihu.com.evil.com/") is False
    assert is_blacklisted_search_url("https://fakezhihu.com/") is False


def test_hostname_missing_or_invalid_url():
    assert is_blacklisted_search_url("不是 URL") is False
    assert is_blacklisted_search_url("") is False


# ---------------------------------------------------------------------
# 结果过滤
# ---------------------------------------------------------------------
def _result(link: str) -> dict:
    return {"title": f"title of {link}", "link": link}


def test_filter_results_counts_blocked():
    items = [
        _result("https://www.zhihu.com/q/1"),
        _result("https://www.python.org/downloads/"),
        _result("https://baijiahao.baidu.com/s?id=9"),
        _result("https://docs.python.org/3/"),
    ]
    kept, count = filter_blacklisted_search_results(items)
    assert count == 2
    assert [i["link"] for i in kept] == [
        "https://www.python.org/downloads/",
        "https://docs.python.org/3/",
    ]


def test_filter_results_missing_link_kept():
    items = [{"title": "无 link 字段"}, _result("https://www.zhihu.com/x")]
    kept, count = filter_blacklisted_search_results(items)
    assert count == 1
    assert kept == [{"title": "无 link 字段"}]


# ---------------------------------------------------------------------
# 候选数量计算
# ---------------------------------------------------------------------
def test_candidate_count_multiplies_then_caps():
    # 出厂配置：倍率 2，候选上限 50
    assert candidate_result_count(10) == 20
    assert candidate_result_count(25) == 50    # 50 恰好等于上限
    assert candidate_result_count(40) == 50    # 80 → 截到 50
    assert candidate_result_count(10) == min(
        10 * wsf.SEARCH_CANDIDATE_MULTIPLIER, wsf.SEARCH_MAX_CANDIDATES
    )


def test_candidate_count_respects_monkeypatched_settings(monkeypatch):
    monkeypatch.setattr(wsf, "SEARCH_CANDIDATE_MULTIPLIER", 3)
    monkeypatch.setattr(wsf, "SEARCH_MAX_CANDIDATES", 100)
    assert candidate_result_count(10) == 30
    assert candidate_result_count(60) == 100


def test_domain_rule_direct_matching():
    rule = DomainRule(raw="x.com", domain="x.com",
                      include_root=True, include_subdomains=False)
    assert rule.matches("x.com") and not rule.matches("sub.x.com")
