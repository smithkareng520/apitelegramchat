"""验证 fetch_url 重定向跟随修复（针对通用 splash page 的拼接 JS 跳转）。

覆盖：
  1. `_extract_js_redirect_targets` 能从真实的 battleofballs splash HTML 中
     解析出 `https://www.battleofballs.com/index/`（旧的 naive regex 只能
     捕获 `'https://'` 然后 urljoin 把它解析回原始 URL，误判为"重定向到
     自身"）。
  2. 通用写法都识别：`location.href = '...'`、`location.replace(...)`
     / `location.assign(...)`、`window.location = '...'`、`document.location`
     / `top.location` / `parent.location` / `self.location`、裸 `location =
     '...'`。
  3. 字符串拼接表达式能被正确重建：host 类变量替换为真实 host，search /
     hash / pathname 类变量直接丢弃。
  4. 多分支 if/else：所有候选都应被收集（按文档顺序、去重）。
  5. 仅捕获到裸 scheme 或目标规范化后等于当前 URL 时，跳过该候选——
     不再误报"重定向到自身"，让上层继续走 Meta Refresh 与根路径回退。
  6. Meta refresh 同时支持标准引号写法与非标准无引号写法。
  7. `_normalize_url_for_compare` 在仅尾斜杠 / 大小写差异时判定为同源
     同路径。
  8. 集成：直接调用 `execute_fetch_url('https://www.battleofballs.com/')`
     应当返回成功结果，结果中包含 `球球` 或 `battle` 之类的正文，且不以
     `失败：` 开头。

运行：
    python3 /home/z/my-project/scripts/verify_redirect_follow.py

需要联网（用于第 8 项的端到端验证）。如果离线，前 7 项断言仍可独立验证。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path("/home/z/my-project/apitelegramchat-optimized")
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


# ---------------------------------------------------------------------------
# 1. Helper extraction tests (no network needed)
# ---------------------------------------------------------------------------

def test_helpers():
    print("\n[1] Helper extraction (no network)")
    from apitelegramchat.search_engine import (
        _extract_js_redirect_targets,
        _extract_meta_refresh_targets,
        _normalize_url_for_compare,
    )

    # 1.1 Real splash HTML from https://www.battleofballs.com/ (saved during dev)
    splash_html = """<!DOCTYPE html>
<html><head lang="zh-CN"><meta charset="UTF-8">
<script type="text/javascript">
        const ua = window.navigator.userAgent;
        var _maq = _maq || [];
        var wlOrigin = window.location.host;
        var chackdown = window.setInterval(() => {
                uniqId = localStorage.getItem('gt-bury-point-uniqId');
                if (window._maq.length >= 2 && uniqId) {
                        window.clearInterval(chackdown)
                        setTimeout(() => {
                                if (ua.indexOf('Mobile') !== -1 ) {
                                        window.location.href = 'https://' + wlOrigin + '/index/' + window.location.search;
                                } else {
                                        window.location.href = 'https://' + wlOrigin + '/index/' + window.location.search;
                                }
                        }, 300)
                }
        })
</script>
</head><body></body></html>"""

    targets = _extract_js_redirect_targets(splash_html, "https://www.battleofballs.com/")
    check(
        "battleofballs splash → [/index/] (concatenation + dedup)",
        targets == ["https://www.battleofballs.com/index/"],
        f"got {targets!r}",
    )

    # 1.2 Simple single-string redirect (old regex already handled; ensure we still do)
    targets = _extract_js_redirect_targets(
        "<script>window.location.href='/index/';</script>", "https://example.com/")
    check("simple absolute path redirect", targets == ["https://example.com/index/"], f"got {targets!r}")

    # 1.3 location.replace() form
    targets = _extract_js_redirect_targets(
        "<script>location.replace('/home/');</script>", "https://example.com/old")
    check("location.replace('/home/') form", targets == ["https://example.com/home/"], f"got {targets!r}")

    # 1.4 location.assign() form (cross-origin)
    targets = _extract_js_redirect_targets(
        "<script>location.assign('https://other.example.com/page');</script>", "https://example.com/")
    check("location.assign(full URL) form", targets == ["https://other.example.com/page"], f"got {targets!r}")

    # 1.5 document.location (alias) prefix
    targets = _extract_js_redirect_targets(
        "<script>document.location.href='/newpath/';</script>", "https://example.com/")
    check("document.location.href prefix", targets == ["https://example.com/newpath/"], f"got {targets!r}")

    # 1.6 top.location.replace (frame-busting)
    targets = _extract_js_redirect_targets(
        "<script>top.location.replace('/unframed/');</script>", "https://example.com/")
    check("top.location.replace prefix", targets == ["https://example.com/unframed/"], f"got {targets!r}")

    # 1.7 parent.location.assign
    targets = _extract_js_redirect_targets(
        "<script>parent.location.assign('/parent-path/');</script>", "https://example.com/")
    check("parent.location.assign prefix", targets == ["https://example.com/parent-path/"], f"got {targets!r}")

    # 1.8 bare `location = '...'` (no .href)
    targets = _extract_js_redirect_targets(
        "<script>location = '/bare/';</script>", "https://example.com/")
    check("bare `location = '/bare/'`", targets == ["https://example.com/bare/"], f"got {targets!r}")

    # 1.9 multi-branch if/else: BOTH candidates collected (deduped by normalized form)
    multi_html = """<script>
        if (ua.indexOf('Mobile') !== -1) {
            location.href = '/m/';
        } else {
            location.href = '/index/';
        }
    </script>"""
    targets = _extract_js_redirect_targets(multi_html, "https://example.com/")
    check(
        "if/else mobile + desktop → both candidates in order",
        targets == ["https://example.com/m/", "https://example.com/index/"],
        f"got {targets!r}",
    )

    # 1.10 host-variable substitution
    host_var_html = "<script>location.href = 'https://' + window.location.host + '/home/';</script>"
    targets = _extract_js_redirect_targets(host_var_html, "https://example.com/")
    check("host-var substitution → /home/", targets == ["https://example.com/home/"], f"got {targets!r}")

    # 1.11 drop window.location.search (don't leak current query into new URL)
    drop_search_html = (
        "<script>location.href = 'https://' + window.location.host + '/p/' + window.location.search;</script>"
    )
    targets = _extract_js_redirect_targets(drop_search_html, "https://example.com/?utm=evil")
    check(
        "drop location.search (don't leak query)",
        targets == ["https://example.com/p/"],
        f"got {targets!r}",
    )

    # 1.12 bare scheme + bare path → empty list (no useful target)
    bare_html = "<script>window.location.href='https://' + wlOrigin + '/';</script>"
    targets = _extract_js_redirect_targets(bare_html, "https://example.com/")
    check("bare scheme + bare path → []", targets == [], f"got {targets!r}")

    # 1.13 self-redirect (path is exactly `/` resolving back to root) → empty
    self_html = "<script>window.location.href='https://example.com/';</script>"
    targets = _extract_js_redirect_targets(self_html, "https://example.com/")
    check("self-redirect → []", targets == [], f"got {targets!r}")

    # 1.14 No redirect at all
    plain_html = "<html><body><h1>Hello</h1></body></html>"
    targets = _extract_js_redirect_targets(plain_html, "https://example.com/")
    check("no redirect in HTML → []", targets == [], f"got {targets!r}")

    # 1.15 Meta refresh: standard quoted form
    meta_html = '<html><head><meta http-equiv="refresh" content="0;url=/index/"></head><body></body></html>'
    targets = _extract_meta_refresh_targets(meta_html, "https://example.com/")
    check("meta refresh (quoted) → /index/", targets == ["https://example.com/index/"], f"got {targets!r}")

    # 1.16 Meta refresh: unquoted content variant
    meta_unquoted = '<meta http-equiv=refresh content="0; url=/news/">'
    targets = _extract_meta_refresh_targets(meta_unquoted, "https://example.com/")
    check("meta refresh (unquoted http-equiv) → /news/", targets == ["https://example.com/news/"], f"got {targets!r}")

    # 1.17 Meta refresh: refresh to self root → empty (no longer errors)
    meta_self_html = '<html><head><meta http-equiv="refresh" content="0;url=/"></head><body></body></html>'
    targets = _extract_meta_refresh_targets(meta_self_html, "https://example.com/")
    check("meta refresh to self root → []", targets == [], f"got {targets!r}")

    # 1.18 URL normalization
    check(
        "normalize: trailing slash ignored",
        _normalize_url_for_compare("https://Example.com/") == _normalize_url_for_compare("https://example.com"),
    )
    check(
        "normalize: query/fragment dropped",
        _normalize_url_for_compare("https://example.com/path?x=1#frag")
        == _normalize_url_for_compare("https://example.com/path"),
    )


# ---------------------------------------------------------------------------
# 2. End-to-end fetch (network needed)
# ---------------------------------------------------------------------------

async def test_fetch_battleofballs():
    print("\n[2] End-to-end: execute_fetch_url('https://www.battleofballs.com/')")
    try:
        from apitelegramchat.search_engine import execute_fetch_url
    except Exception as e:
        check("import execute_fetch_url", False, f"import failed: {e}")
        return

    try:
        result = await asyncio.wait_for(
            execute_fetch_url("https://www.battleofballs.com/"),
            timeout=60,
        )
    except Exception as e:
        check("execute_fetch_url returns (no exception)", False, f"exception: {e}")
        return

    check("execute_fetch_url returns a string", isinstance(result, str), f"type={type(result)}")
    if isinstance(result, str):
        is_failure = result.startswith("失败：") or "页面重定向到自身" in result
        check(
            "result is NOT a failure / 'redirect to self' error",
            not is_failure,
            f"got failure: {result[:200]!r}",
        )
        has_content = "球球" in result or "battle" in result.lower() or "<a" in result
        check("result contains real content", has_content, f"preview: {result[:200]!r}")


async def main():
    test_helpers()
    await test_fetch_battleofballs()
    print(f"\nTotal: {PASS} passed, {FAIL} failed")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
