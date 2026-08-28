"""验证 sendRichMessage 反应式兜底链路。

覆盖：
  1. `_demote_all_media_to_links` 在用户实际触发的 broken HTML 上：
     - 把 `<figure><video src="...wikipedia.../vp9.webm"></video><figcaption>视频对比：…</figcaption></figure>`
       降级为 ``<a href="...webm"><b>视频对比：…</b></a>``；
     - 其它文本/表格/列表/footer 全部保留原样。
  2. 一个混合了 `<video>` / `<audio>` / `<img>` / 裸 media 的复杂 HTML 也能
     完全降级，所有媒体都变成 `<a>` 链接。
  3. 非法 src（如 file_id 形式 ``photo_AgACAgUA.jpg``）的 media 块直接删除，
     不会留下孤立的 ``<a href="photo_...">`` 锚点。
  4. `_rich_message_plain_text_fallback` 仍然能在所有媒体被剥掉之后兜底为
     纯文本段落。
  5. 完整 HTML 经过"媒体降级 + 纯文本兜底"双层 fallback 后仍包含关键正文
     文本（"海豚"、"镜子测试" 等）。

运行：
    python3 /home/z/my-project/scripts/verify_send_fallback.py
"""
from __future__ import annotations

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


# 用户日志里触发问题的那段 HTML（截取末段含 <video> 的部分）
USER_HTML_TAIL = """<h2>🐬 海豚的自我意识有多强？</h2>
<ul><li><b>镜子实验</b>：科学家给海豚画上标记后，它们会游到镜子前仔细检查标记位置</li><li><b>自我命名</b>：每只海豚有独特的"名字"（哨叫声），听到自己名字会回应</li><li><b>工具使用</b>：野生海豚会用海绵保护吻部觅食，并母系传承</li><li><b>情感共鸣</b>：会照顾受伤同伴，甚至帮助其他物种</li><li><b>脑结构</b>：大脑褶皱复杂度接近人类，有专门的镜像神经元</li></ul>
<figure>
<video src="https://upload.wikimedia.org/wikipedia/commons/transcoded/a/ab/Dog_mirror_test.ogv/Dog_mirror_test.ogv.480p.vp9.webm"></video>
<figcaption>视频对比：狗照镜子的反应（左）vs 海豚的自我识别行为</figcaption>
</figure>
<footer>简而言之：海洋里也藏着不输给陆地的智慧生命——海豚和它们的"灵魂"同样令人惊叹。</footer>"""


def test_demote_user_html():
    print("\n[1] _demote_all_media_to_links on user's real broken HTML")
    from apitelegramchat.utils import _demote_all_media_to_links

    demoted = _demote_all_media_to_links(USER_HTML_TAIL)

    # video 应被降级为 <a>
    check(
        "<video> tag removed",
        "<video" not in demoted.lower(),
        f"still contains <video>: {demoted[:300]!r}",
    )
    check(
        "<figure> wrapper removed",
        "<figure" not in demoted.lower(),
        f"still contains <figure>: {demoted[:300]!r}",
    )
    # figcaption 文本应保留作为 anchor 的 caption
    check(
        "figcaption text preserved as anchor caption",
        "视频对比：狗照镜子的反应" in demoted,
        f"caption missing in: {demoted[:400]!r}",
    )
    # 原始 URL 应作为 href 保留
    check(
        "wikipedia .webm URL preserved as href",
        'href="https://upload.wikimedia.org/wikipedia/commons/transcoded/a/ab/Dog_mirror_test.ogv/Dog_mirror_test.ogv.480p.vp9.webm"' in demoted,
        f"href missing in: {demoted[:400]!r}",
    )
    # 其它结构（h2 / ul / footer）应保持原样
    check("<h2> preserved", "<h2>🐬 海豚的自我意识有多强？</h2>" in demoted)
    check("<ul> preserved", "<ul><li><b>镜子实验</b>" in demoted)
    check("<footer> preserved", "<footer>简而言之：" in demoted)


def test_demote_mixed_media():
    print("\n[2] Mixed media (video + audio + img) all demoted")
    from apitelegramchat.utils import _demote_all_media_to_links

    mixed = """<p>Some text before.</p>
<figure><img src="https://example.com/pic1.jpg"/><figcaption>图1：海豚</figcaption></figure>
<video src="https://example.com/clip.mp4"></video>
<audio src="https://example.com/sound.mp3"></audio>
<figure><video src="https://example.com/v2.webm"></video><figcaption>另一段视频</figcaption></figure>
<p>Some text after.</p>"""

    demoted = _demote_all_media_to_links(mixed)
    check("no <video> left", "<video" not in demoted.lower())
    check("no <audio> left", "<audio" not in demoted.lower())
    check("no <img> left", "<img" not in demoted.lower())
    check("no <figure> left", "<figure" not in demoted.lower())
    # 四个媒体 URL 都应作为 <a href> 保留
    check("img URL preserved as href", 'href="https://example.com/pic1.jpg"' in demoted)
    check("video URL preserved as href", 'href="https://example.com/clip.mp4"' in demoted)
    check("audio URL preserved as href", 'href="https://example.com/sound.mp3"' in demoted)
    check("second video URL preserved as href", 'href="https://example.com/v2.webm"' in demoted)
    # figcaption 文本都应保留
    check("img caption preserved", "图1：海豚" in demoted)
    check("video caption preserved", "另一段视频" in demoted)
    # 其它文本保持原样
    check("non-media text preserved", "Some text before." in demoted and "Some text after." in demoted)


def test_demote_invalid_src():
    print("\n[3] Invalid src (file_id / no scheme) is dropped, not linked")
    from apitelegramchat.utils import _demote_all_media_to_links

    invalid = """<figure><img src="photo_AgACAgUA.jpg"/><figcaption>附件</figcaption></figure>
<video src="FILE_ID://not-a-url"></video>
<img src="/relative/path.jpg"/>"""
    demoted = _demote_all_media_to_links(invalid)
    check("no <img> left", "<img" not in demoted.lower())
    check("no <video> left", "<video" not in demoted.lower())
    check("invalid src not turned into anchor", "photo_AgACAgUA" not in demoted)
    check("relative path not turned into anchor", "/relative/path.jpg" not in demoted)
    # figcaption 文本应保留下来（虽然 media 块被删除）
    check("figcaption caption preserved as plain text", "附件" in demoted)


def test_plain_text_fallback():
    print("\n[4] _rich_message_plain_text_fallback still works")
    from apitelegramchat.utils import _rich_message_plain_text_fallback

    html = """<h2>海豚的自我意识</h2><ul><li>镜子实验</li><li>自我命名</li></ul>
<figure><video src="https://example.com/x.webm"></video><figcaption>视频</figcaption></figure>
<footer>简而言之：海洋智慧生命</footer>"""
    fallback = _rich_message_plain_text_fallback(html)
    check("fallback is a <p> paragraph", fallback.startswith("<p>") and fallback.endswith("</p>"))
    check("visible text preserved", "海豚的自我意识" in fallback and "镜子实验" in fallback)
    check("media URL stripped from fallback", "example.com/x.webm" not in fallback)
    check("no <video>/<figure> tags in fallback", "<video" not in fallback.lower() and "<figure" not in fallback.lower())


def test_end_to_end_degradation():
    print("\n[5] End-to-end: media-demote + plain-text-fallback preserves user content")
    from apitelegramchat.utils import (
        _demote_all_media_to_links,
        _rich_message_plain_text_fallback,
    )

    # 用户日志的完整末段
    full_tail = USER_HTML_TAIL
    demoted = _demote_all_media_to_links(full_tail)
    # 进一步降级为纯文本
    plain = _rich_message_plain_text_fallback(demoted)

    check("plain text still has 海豚", "海豚" in plain)
    check("plain text still has 镜子实验", "镜子实验" in plain)
    check("plain text still has footer text", "海洋里也藏着不输给陆地的智慧生命" in plain)
    check("plain text no longer has wikipedia video URL", "upload.wikimedia.org" not in plain)


def main():
    test_demote_user_html()
    test_demote_mixed_media()
    test_demote_invalid_src()
    test_plain_text_fallback()
    test_end_to_end_degradation()
    print(f"\nTotal: {PASS} passed, {FAIL} failed")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
