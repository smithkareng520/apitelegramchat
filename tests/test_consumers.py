# test_consumers.py — 验证 fetch_url 新格式在下游（format_tool_result / 摘要）的表现
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def run_async(coro):
    return asyncio.run(coro)


def main():
    # 1) 通过 execute_fetch_url 生成新格式结果（monkeypatch 网络）
    import apitelegramchat.search_engine as se

    PAGE_HTML = """
    <html><head><title>消费端测试页面</title></head><body><article>
    <h1>消费端测试页面</h1>
    <p>这是一段足够长的正文内容，用来通过最短长度校验。包含<b>加粗</b>与
    <a href="https://example.com/more">更多阅读</a>链接，长度足够长足够长。</p>
    <p>第二段正文，同样足够长，确保 trafilatura 能稳定提取出有效正文内容来。</p>
    <iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"></iframe>
    <img src="https://cdn.example.com/photo.jpg" alt="新闻配图"/>
    </article></body></html>
    """

    async def fake_curl(url):
        return PAGE_HTML

    se._fetch_html_with_curl = fake_curl
    se._fetch_cache.clear()
    result = run_async(se.execute_fetch_url("https://example.com/consumer-test"))
    assert "<h3>" in result, "应为新格式（含 <h3> 标题）"
    print("[1] execute_fetch_url 输出格式 OK")

    # 2) format_tool_result 的 fetch_url 分支
    from apitelegramchat.tool_executors import format_tool_result

    summary, details_html = run_async(
        format_tool_result("fetch_url", {"url": "https://example.com/consumer-test"}, result)
    )
    assert summary == "🌐 Fetched: 消费端测试页面", f"summary 异常: {summary!r}"
    assert details_html == result.strip(), "详情应原样透传富 HTML"
    print(f"[2] format_tool_result summary OK → {summary!r}")

    # 3) 失败结果仍被正确识别
    fail_summary, fail_details = run_async(
        format_tool_result("fetch_url", {"url": "https://example.com/x"}, "失败：无法获取页面内容：https://example.com/x")
    )
    assert fail_summary.startswith("🌐 Failed to fetch"), f"失败 summary 异常: {fail_summary!r}"
    print(f"[3] 失败识别 OK → {fail_summary!r}")

    # 4) 谈论"失败"的正文不再被误判为抓取失败
    news_html = "<h3>某某公司年度财报：项目失败率下降</h3><p>正文讲述了 failure rate 下降……</p>"
    ok_summary, ok_details = run_async(
        format_tool_result("fetch_url", {"url": "https://example.com/news"}, news_html)
    )
    assert not ok_summary.startswith("🌐 Failed"), f"误判失败: {ok_summary!r}"
    assert ok_summary == "🌐 Fetched: 某某公司年度财报：项目失败率下降", f"summary 异常: {ok_summary!r}"
    print(f"[4] 含'失败'字样的正文不再误判 OK → {ok_summary!r}")

    # 5) _generate_tool_summary_done 的 <h3> 解析
    from apitelegramchat.ai.tool_summary import _generate_tool_summary_done, _tool_result_is_failure

    done_summary = _generate_tool_summary_done("fetch_url", {"url": "https://example.com/consumer-test"}, result)
    assert done_summary == "Fetched: 消费端测试页面", f"done summary 异常: {done_summary!r}"
    print(f"[5] _generate_tool_summary_done OK → {done_summary!r}")

    # 6) 失败判定：新格式成功结果不是失败
    assert not _tool_result_is_failure("fetch_url", {}, result), "成功结果不应判为失败"
    assert _tool_result_is_failure("fetch_url", {}, "失败：无法获取页面内容"), "失败结果应判为失败"
    print("[6] _tool_result_is_failure OK")

    # 7) 兼容旧格式（🏷️ 标记）的标题解析
    legacy = "✅ [成功] 🏷️ 旧版标题\n🔗 https://example.com/old\n📄 内容：\n\n旧正文"
    legacy_summary = _generate_tool_summary_done("fetch_url", {"url": "https://example.com/old"}, legacy)
    assert legacy_summary == "Fetched: 旧版标题", f"旧格式 summary 异常: {legacy_summary!r}"
    print(f"[7] 旧格式兼容 OK → {legacy_summary!r}")

    print("\n全部消费端验证通过 ✅")


if __name__ == "__main__":
    main()
