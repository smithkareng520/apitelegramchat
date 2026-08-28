"""补充的端到端 sanity 检查：
   1. 完整预览 battleofballs.com 的 fetch 结果（确认拿到了真实正文）。
   2. 回归测试一个普通深层 URL（确保新逻辑没破坏既有路径）。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path("/home/z/my-project/apitelegramchat-optimized")
sys.path.insert(0, str(ROOT / "src"))

from apitelegramchat.search_engine import execute_fetch_url


async def main():
    # 1. battleofballs.com（用户报告的 splash page）
    print("=== fetch https://www.battleofballs.com/ ===")
    r1 = await asyncio.wait_for(
        execute_fetch_url("https://www.battleofballs.com/"), timeout=60,
    )
    print("type:", type(r1).__name__)
    print("starts with 失败：", r1.startswith("失败："))
    print("length:", len(r1))
    print("preview (first 600 chars):")
    print(r1[:600])
    print()

    # 2. 普通深层页面（应有正文）
    print("=== fetch https://example.com/ ===")
    r2 = await asyncio.wait_for(
        execute_fetch_url("https://example.com/"), timeout=60,
    )
    print("type:", type(r2).__name__)
    print("starts with 失败：", r2.startswith("失败："))
    print("length:", len(r2))
    print("preview (first 400 chars):")
    print(r2[:400])
    print()

    # 3. 一个明显会 404 的 URL（应该返回失败字符串，且不是"重定向到自身"）
    print("=== fetch https://www.battleofballs.com/this-path-does-not-exist-xyz123 ===")
    r3 = await asyncio.wait_for(
        execute_fetch_url("https://www.battleofballs.com/this-path-does-not-exist-xyz123"),
        timeout=60,
    )
    print("type:", type(r3).__name__)
    print("starts with 失败：", r3.startswith("失败："))
    print("contains 重定向到自身:", "重定向到自身" in r3)
    print("preview (first 400 chars):")
    print(r3[:400])


if __name__ == "__main__":
    asyncio.run(main())
