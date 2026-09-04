# -*- coding: utf-8 -*-
"""验证「文件名/类型元数据 + 原生 sendDocument 兜底」修复的测试脚本（v5）。

背景（2026-09-05 02:31 线上日志 chat=7162243624 trace cc963c9b）：
  v4 稳定代理 URL 交付后，Telegram 富文本抓取器对
  /media/<hmac>/telegram/<file_id> 成功下载了全部字节（代理侧两次
  200 + 89257B，时间与两次发送尝试一一对应），却仍报
  RICH_MESSAGE_DOCUMENT_NO_MEDIA_FOUND —— 抓取成功不等于建档成功：
  URL/响应均无文件名与真实类型（application/octet-stream + 裸
  base64url file_id），抓取器无法把字节归类为文档媒体。

v5 修复（本测试覆盖）：
  1. 交付 URL 末段携带原始文件名（build_media_proxy_url(filename=...)），
     路由按"末段为展示名"兼容解析（resolve_proxy_key，验签仍针对真实 key）；
  2. 代理 Content-Type 优先按展示文件名扩展名推断
     （guess_content_type_from_filename），Content-Disposition 走
     RFC 6266 filename*（content_disposition_inline）；
  3. URL 构造链透传 file_name（s3_utils.resolve_stable_delivery_url →
     attachment_content._resolve_public_attachment_url）；
  4. 上传侧透传 Telegram 真实 mime_type
     （file_handlers._resolve_upload_content_type）；
  5. 兜底：document 媒体错误时优先 sendDocument(file_id) 原生送达文件、
     从富文本移除标签（utils._rescue_documents_via_native_send）。

运行：python tests/test_tgdoc_v5_filename_rescue.py
"""
import asyncio
import os
import sys
from urllib.parse import quote

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (
    os.path.join(_HERE, os.pardir, "src"),
    os.path.join(_HERE, os.pardir, "work", "src"),
):
    _rp = os.path.realpath(_p)
    if os.path.isdir(_rp):
        sys.path.insert(0, _rp)

import apitelegramchat.media_proxy as mp
import apitelegramchat.s3_utils as s3u
import apitelegramchat.file_handlers as fh
import apitelegramchat.utils as u
from apitelegramchat.utils import (
    _rich_message_html_payload,
    _demote_all_media_to_links,
    _rescue_documents_via_native_send,
    _find_document_blocks,
    _match_own_telegram_file_url,
    _maybe_native_send_document,
)

# 2026-09-05 02:31 日志中的真实 file_id（.htm 教程文档）
FID = "BQACAgUAAxkBAAJ1H2qbDp04_0NYA0G9DIfmU3T7xgABEQAClyYAAmW02VS1x6glHA7_jj0E"
KEY = f"telegram/{FID}"
FNAME = "Claude_iOS_卡在_Something_went_wrong_的_Charles_处理教程_青松笔记.htm"

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class Patcher:
    """最小 monkeypatch 助手：set() 记录旧值，restore_all() 恢复。"""

    def __init__(self):
        self._undo = []

    def set(self, module, name, value):
        old = getattr(module, name)
        setattr(module, name, value)
        self._undo.append((module, name, old))

    def restore_all(self):
        for module, name, old in reversed(self._undo):
            setattr(module, name, old)
        self._undo.clear()


def section(title):
    print(f"\n==== {title} ====")


# =====================================================================
print(__doc__)

# ---------------------------------------------------------------------
section("1. build_media_proxy_url：末段携带原始文件名")

p = Patcher()
try:
    p.set(mp, "MEDIA_PROXY_BASE_URL", "https://apitelegramchat.onrender.com")
    p.set(mp, "PUBLIC_BASE_URL", "")

    # v4 兼容：不带 filename 时 URL 与旧形态一致
    url_v4 = mp.build_media_proxy_url(KEY)
    token = mp.sign_media_key(KEY)
    check("v4 形态不变：/media/<token>/<key>",
          url_v4 == f"https://apitelegramchat.onrender.com/media/{token}/{KEY}")

    # v5：带 filename → 末段为 %XX 编码的原始文件名，且以扩展名结尾
    url_v5 = mp.build_media_proxy_url(KEY, FNAME)
    quoted = quote(FNAME, safe="")
    check("v5 URL 末段 = 编码后的原始文件名", url_v5.endswith("/" + quoted),
          f"url={url_v5[-80:]}")
    check("URL 以扩展名 .htm 结尾（抓取器类型线索）", url_v5.endswith(".htm"))
    check("中文名已被 % 编码（路径段内无裸中文）",
          "青松" not in url_v5 and "%E9%9D%92" in url_v5)
    check("token 仍只绑定真实 key（v4 与 v5 token 相同）",
          f"/media/{token}/" in url_v5)
    check("空 filename 等价 v4 形态", mp.build_media_proxy_url(KEY, "") == url_v4)
    check("key 为空返回 None", mp.build_media_proxy_url("", FNAME) is None)

    # 纯 ASCII 文件名不引入多余编码
    url_ascii = mp.build_media_proxy_url(KEY, "report.pdf")
    check("ASCII 文件名保持字面（report.pdf）", url_ascii.endswith("/report.pdf"))
finally:
    p.restore_all()

# ---------------------------------------------------------------------
section("2. resolve_proxy_key：末段展示名兼容解析 + 验签安全")

p = Patcher()
try:
    token = mp.sign_media_key(KEY)

    # v4 形态：整体验签通过，无展示名
    r = mp.resolve_proxy_key(KEY, token)
    check("v4 形态解析为 (key, '')", r == (KEY, ""), f"got {r}")

    # v5 形态：整体 = key + "/" + 编码文件名，验签失败后剥末段重验
    tail = quote(FNAME, safe="")
    r2 = mp.resolve_proxy_key(f"{KEY}/{tail}", token)
    check("v5 形态解析为 (key, 原始文件名)",
          r2 == (KEY, FNAME), f"got {r2[:1]} name={r2[1] if r2 else None}")

    # ASCII 展示名
    r3 = mp.resolve_proxy_key(f"{KEY}/report.pdf", token)
    check("ASCII 展示名还原", r3 == (KEY, "report.pdf"), f"got {r3}")

    # 安全性：错误 token / 篡改 / 无斜杠都拒绝
    check("错误 token 拒绝", mp.resolve_proxy_key(f"{KEY}/{tail}", "0" * 16) is None)
    bad = mp.sign_media_key(KEY + "x")
    check("签名对应其他 key 时拒绝",
          mp.resolve_proxy_key(f"{KEY}/{tail}", bad) is None)
    check("无斜杠 + 错 token 拒绝", mp.resolve_proxy_key(KEY, "f" * 16) is None)
    check("末段为空拒绝", mp.resolve_proxy_key(f"{KEY}/", token) is None)

    # 伪造末段不能越权：含 / 的多段末段被整体拒绝（fail-closed，
    # 合法文件名经 quote(safe='') 编码后段内不可能含字面 /）。
    r4 = mp.resolve_proxy_key(f"{KEY}/telegram/FAKE", token)
    check("多段末段 fail-closed 拒绝（不可作伪造载体）", r4 is None, f"got {r4}")
finally:
    p.restore_all()

# ---------------------------------------------------------------------
section("3. guess_content_type_from_filename / content_disposition_inline")

check(".htm → text/html",
      mp.guess_content_type_from_filename("a.htm") == "text/html")
check(".html → text/html",
      mp.guess_content_type_from_filename("x.html") == "text/html")
check(".PDF 大小写不敏感 → application/pdf",
      mp.guess_content_type_from_filename("a.PDF") == "application/pdf")
check("中文名 .docx 仍可识别",
      mp.guess_content_type_from_filename("教程.docx") ==
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
check("无扩展名 → fallback",
      mp.guess_content_type_from_filename("BQACAgUA",
                                          fallback="application/octet-stream")
      == "application/octet-stream")
check("空名 → fallback", mp.guess_content_type_from_filename("", fallback="a/b") == "a/b")

disp_ascii = mp.content_disposition_inline("report.pdf")
check("ASCII：filename= 为字面名", 'filename="report.pdf"' in disp_ascii)
disp_cn = mp.content_disposition_inline("教程.htm")
check("中文：filename= 退化为 ASCII 安全值", 'filename="file"' in disp_cn)
check("中文：filename*=UTF-8'' 携带编码原名",
      "filename*=UTF-8''" + quote("教程.htm", safe="") in disp_cn)
check("CRLF/引号被清洗",
      mp.content_disposition_inline('a"b\r\nc.htm')
      == 'inline; filename="a\'bc.htm"; filename*=UTF-8\'\'a%27bc.htm')

# v4 兼容：guess_content_type 行为不变
check("guess_content_type 兼容：显式 CT 优先",
      mp.guess_content_type(KEY, "image/png") == "image/png")
check("guess_content_type 兼容：octet-stream 视为未记录",
      mp.guess_content_type("a/b.pdf", "application/octet-stream") == "application/pdf")

# ---------------------------------------------------------------------
section("4. resolve_stable_delivery_url：文件名仅在代理路径附加")

p = Patcher()
try:
    p.set(s3u, "R2_PUBLIC_URL", "")
    p.set(mp, "MEDIA_PROXY_BASE_URL", "https://bot.example.com")
    p.set(mp, "PUBLIC_BASE_URL", "")

    url = asyncio.run(s3u.resolve_stable_delivery_url(KEY, FNAME))
    token = mp.sign_media_key(KEY)
    check("代理路径：URL = /media/<token>/<key>/<编码文件名>",
          url == f"https://bot.example.com/media/{token}/{KEY}/{quote(FNAME, safe='')}",
          f"url={url[-90:]}")
    url2 = asyncio.run(s3u.resolve_stable_delivery_url(KEY))
    check("无文件名时保持 v4 形态",
          url2 == f"https://bot.example.com/media/{token}/{KEY}")

    # R2 公开域：不附加伪路径段（对象 key 不含文件名，附加会 404）
    p.set(s3u, "R2_PUBLIC_URL", "https://pub-abc.r2.dev")
    url3 = asyncio.run(s3u.resolve_stable_delivery_url(KEY, FNAME))
    check("R2 公开域：URL 不带文件名段", url3 == f"https://pub-abc.r2.dev/{KEY}",
          f"url={url3}")
finally:
    p.restore_all()

# ---------------------------------------------------------------------
section("5. 上传侧 ContentType：真实 mime 优先 + 扩展名推断兜底")

check("显式 mime 优先（Telegram 报 text/html）",
      fh._resolve_upload_content_type("text/html", "/tmp/x.htm") == "text/html")
check("显式 mime 大小写归一",
      fh._resolve_upload_content_type("Text/HTML ", "/tmp/x") == "text/html")
check("无 mime 时 .htm → text/html",
      fh._resolve_upload_content_type("", "/ws/教程.htm") == "text/html")
check("octet-stream 视为未提供，走扩展名推断",
      fh._resolve_upload_content_type("application/octet-stream", "/tmp/a.png")
      == "image/png")
check("未知扩展名 → octet-stream",
      fh._resolve_upload_content_type("", "/tmp/a.xyz123") == "application/octet-stream")

# ---------------------------------------------------------------------
section("6. 发送管线端到端：v5 URL 不被任何清理道破坏")

p = Patcher()
try:
    p.set(mp, "MEDIA_PROXY_BASE_URL", "https://apitelegramchat.onrender.com")
    p.set(mp, "PUBLIC_BASE_URL", "")
    proxy_url = mp.build_media_proxy_url(KEY, FNAME)

    # 02:31 日志真实形态：裸自闭合 <tg-document src="..."/>（本轮日志）
    bare_html = f'<p>文档已成功发送给用户。</p>\n<tg-document src="{proxy_url}"/>'
    out1 = _rich_message_html_payload(bare_html)
    check("裸自闭合标签保留", '<tg-document src="' in out1["html"])
    check("src 原样保留（无转义/改写）", proxy_url in out1["html"])
    check("无 Markdown 解包/游离开标签副作用",
          "&lt;" not in out1["html"] and "[" not in out1["html"])

    # 01:28 日志形态：figure 包裹 + figcaption
    fig_html = (
        f'<figure><tg-document src="{proxy_url}"></tg-document>'
        f"<figcaption>{FNAME}</figcaption></figure>"
    )
    out2 = _rich_message_html_payload(fig_html)
    check("figure 形态保留", "<figure>" in out2["html"] and "</figure>" in out2["html"])
    check("figcaption 保留", FNAME in out2["html"])
    check("幂等：二次清理不再变化",
          _rich_message_html_payload(out2["html"])["html"] == out2["html"])

    # 带 & 的旧形态回归：amp 转义仍生效（v3 行为不回归）
    presign = (
        "https://a94baf2f82dbdc55044f54eb19838b0c.r2.cloudflarestorage.com/dearella/"
        + KEY + "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=3600"
    )
    out3 = _rich_message_html_payload(f'<tg-document src="{presign}"/>')
    check("v3 回归：&amp; 转义仍生效", "&amp;" in out3["html"])

    # 定向降级在 v5 URL 上仍可用（救援失败时的后备路径）
    demoted = _demote_all_media_to_links(fig_html, {"tg-document"})
    check("降级回归：figure → <a>，caption 保留",
          '<a href="' in demoted and FNAME in demoted and "<tg-document" not in demoted)
finally:
    p.restore_all()

# ---------------------------------------------------------------------
section("7. _match_own_telegram_file_url：三种历史形态识别")

check("v5 代理 URL（带文件名）",
      _match_own_telegram_file_url(
          f"https://apitelegramchat.onrender.com/media/{mp.sign_media_key(KEY)}/{KEY}/{quote(FNAME, safe='')}"
      ) == FID)
check("v4 代理 URL（无文件名）",
      _match_own_telegram_file_url(
          f"https://apitelegramchat.onrender.com/media/{mp.sign_media_key(KEY)}/{KEY}"
      ) == FID)
check("R2 公开域形态",
      _match_own_telegram_file_url(f"https://pub-abc.r2.dev/{KEY}") == FID)
check("历史预签名回显（带查询串）",
      _match_own_telegram_file_url(
          f"https://acct.r2.cloudflarestorage.com/bucket/{KEY}?X-Amz-Expires=3600&sig=ab"
      ) == FID)
check("&amp; 残留先还原再匹配",
      _match_own_telegram_file_url(
          f"https://acct.r2.cloudflarestorage.com/bucket/{KEY}?a=1&amp;b=2"
      ) == FID)
check("外部 URL 不匹配", _match_own_telegram_file_url("https://example.com/doc.pdf") == "")
check("短 id 过滤（<16）",
      _match_own_telegram_file_url("https://x.com/telegram/ABC123") == "")
check("非 telegram 路径不匹配",
      _match_own_telegram_file_url(f"https://x.com/media/abc/{FID}") == "")
check("空值安全", _match_own_telegram_file_url("") == "")

# ---------------------------------------------------------------------
section("8. _rescue_documents_via_native_send：原生送达 + 标签移除")

p = Patcher()
try:
    p.set(mp, "MEDIA_PROXY_BASE_URL", "https://apitelegramchat.onrender.com")
    p.set(mp, "PUBLIC_BASE_URL", "")
    proxy_url = mp.build_media_proxy_url(KEY, FNAME)
    other_key = "telegram/BQACAgUAAxkBAAJ1C2qbOtherFileId01234567890ABCDE"
    other_url = mp.build_media_proxy_url(other_key, "801_router_password.txt")

    send_calls = []

    async def fake_send_ok(chat_id, file_id):
        send_calls.append((chat_id, file_id))
        return True

    async def fake_send_fail(chat_id, file_id):
        return False

    # 清空去重缓存，保证各用例独立
    u._native_rescued_recent.clear()

    p.set(u, "_native_send_document", fake_send_ok)

    # 8.1 figure 形态：文件原生送达，figure → figcaption 文本段落
    send_calls.clear()
    u._native_rescued_recent.clear()
    html_fig = (
        f'<p>这是教程文档。</p><figure><tg-document src="{proxy_url}"></tg-document>'
        f"<figcaption>Charles 处理教程</figcaption></figure>"
    )
    got = asyncio.run(_rescue_documents_via_native_send(7162243624, html_fig))
    check("figure：返回改写后的 HTML", got is not None and got != html_fig)
    check("figure：figcaption 文本保留为 <p> 段落",
          got is not None and "<p>Charles 处理教程</p>" in got, f"got={got!r}")
    check("figure：<tg-document> 已移除", got is not None and "<tg-document" not in got)
    check("figure：无 <a> 链接残留", got is not None and "<a " not in got)
    check("figure：正文其余部分保留", got is not None and "这是教程文档。" in got)
    check("figure：sendDocument 以正确 file_id 调用一次",
          send_calls == [(7162243624, FID)], f"calls={send_calls}")

    # 8.2 02:31 日志真实形态：裸自闭合标签
    send_calls.clear()
    u._native_rescued_recent.clear()
    html_bare = f'<p>正文。</p>\n<tg-document src="{proxy_url}"/>'
    got_bare = asyncio.run(_rescue_documents_via_native_send(7162243624, html_bare))
    check("裸标签：整块移除且正文保留",
          got_bare is not None and "<tg-document" not in got_bare and "<p>正文。</p>" in got_bare,
          f"got={got_bare!r}")
    check("裸标签：sendDocument 调用一次", len(send_calls) == 1)

    # 8.3 去重窗口：同 chat+file_id 第二次不重发、仍剥标签
    send_calls.clear()
    got_dedup = asyncio.run(_rescue_documents_via_native_send(7162243624, html_bare))
    check("去重：窗口内不再调用 sendDocument", send_calls == [], f"calls={send_calls}")
    check("去重：仍返回剥标签后的 HTML",
          got_dedup is not None and "<tg-document" not in got_dedup)

    # 8.4 不同 chat 不受去重影响（TTLCache key 含 chat_id）
    send_calls.clear()
    u._native_rescued_recent.clear()
    asyncio.run(_rescue_documents_via_native_send(111, html_bare))
    asyncio.run(_rescue_documents_via_native_send(222, html_bare))
    check("不同 chat 各发送一次", len(send_calls) == 2)

    # 8.5 混合：own 文档救援 + 外部文档保留（交给降级链）
    send_calls.clear()
    u._native_rescued_recent.clear()
    html_mixed = (
        f'<figure><tg-document src="{proxy_url}"></tg-document>'
        f"<figcaption>内部文档</figcaption></figure>"
        f'<figure><tg-document src="https://example.com/外部.pdf"></tg-document>'
        f"<figcaption>外部文档</figcaption></figure>"
    )
    got_mixed = asyncio.run(_rescue_documents_via_native_send(7162243624, html_mixed))
    check("混合：own 文档被移除",
          got_mixed is not None and proxy_url not in got_mixed)
    check("混合：外部 URL 原样保留（后续降级处理）",
          got_mixed is not None and "https://example.com/外部.pdf" in got_mixed)
    check("混合：只发送 own 文档一次", send_calls == [(7162243624, FID)])

    # 8.6 发送失败：返回 None，html 原样（进入既有降级链）
    p.set(u, "_native_send_document", fake_send_fail)
    u._native_rescued_recent.clear()
    got_fail = asyncio.run(_rescue_documents_via_native_send(7162243624, html_fig))
    check("发送失败：返回 None", got_fail is None)

    # 8.7 无 tg-document 内容：直接 None
    got_none = asyncio.run(_rescue_documents_via_native_send(7162243624, "<p>纯文本</p>"))
    check("无标签内容：返回 None", got_none is None)

    # 8.8 全外部 URL：None（不误发 sendDocument）
    send_calls.clear()
    p.set(u, "_native_send_document", fake_send_ok)
    got_ext = asyncio.run(_rescue_documents_via_native_send(
        7162243624, '<tg-document src="https://example.com/a.pdf"/>'))
    check("全外部 URL：None 且不调用 sendDocument", got_ext is None and send_calls == [])

    # 8.9 两个 own 文档：各发送一次、各剥各的
    send_calls.clear()
    u._native_rescued_recent.clear()
    html_two = (
        f'<figure><tg-document src="{proxy_url}"></tg-document>'
        f"<figcaption>文档一</figcaption></figure>"
        f'<figure><tg-document src="{other_url}"></tg-document>'
        f"<figcaption>文档二</figcaption></figure>"
    )
    got_two = asyncio.run(_rescue_documents_via_native_send(7162243624, html_two))
    check("两个文档：各原生发送一次", len(send_calls) == 2)
    check("两个文档：标签全部移除、caption 各自保留",
          got_two is not None and "<tg-document" not in got_two
          and "<p>文档一</p>" in got_two and "<p>文档二</p>" in got_two,
          f"got={got_two!r}")
finally:
    p.restore_all()

# ---------------------------------------------------------------------
section("9. _find_document_blocks：figure/裸块收集与去重")

p = Patcher()
try:
    p.set(mp, "MEDIA_PROXY_BASE_URL", "https://bot.example.com")
    p.set(mp, "PUBLIC_BASE_URL", "")
    u1 = mp.build_media_proxy_url(KEY, FNAME)
    mixed = (
        f'<figure><tg-document src="{u1}"></tg-document>'
        f"<figcaption>cap</figcaption></figure>"
        f"<p>between</p>"
        f'<tg-document src="{u1}"/>'
    )
    blocks = _find_document_blocks(mixed)
    check("figure 块覆盖整个 <figure>…</figure>",
          len(blocks) == 2 and mixed[blocks[0]["start"]:blocks[0]["end"]].startswith("<figure>"))
    check("figcaption 文本进入 cap_html", blocks[0]["cap_html"] == "cap")
    check("裸块独立收集（不被 figure 二次计入）",
          blocks[1]["cap_html"] == "" and mixed[blocks[1]["start"]:blocks[1]["end"]].startswith("<tg-document"))
    check("块按出现顺序排序", blocks[0]["start"] < blocks[1]["start"])
finally:
    p.restore_all()

# ---------------------------------------------------------------------
section("10. 发送链路接入点：救援候选先于链接降级（静态检查）")

import inspect
_src = inspect.getsource(u.send_rich_html_message)
_rescue_pos = _src.find("_rescue_documents_via_native_send(chat_id")
_targeted_pos = _src.find("targeted = _demote_all_media_to_links(html_content, media_kinds)")
_alt_pos = _src.find('("media src ampersands re-sent as raw & form"')
check("救援调用位于 send_rich_html_message 内", _rescue_pos > 0)
check("顺序：裸 & 重试 → 原生救援 → 定向降级",
      0 < _alt_pos < _rescue_pos < _targeted_pos,
      f"alt={_alt_pos} rescue={_rescue_pos} targeted={_targeted_pos}")
check("仅当 tg-document 命中时才尝试救援",
      'if "tg-document" in media_kinds:' in _src)
check("救援候选标记语义正确",
      "documents delivered natively via sendDocument(file_id), media tags removed" in _src)

# 草稿路径不受影响（不在草稿流里发文件）
_draft_src = inspect.getsource(u.send_rich_message_draft)
check("草稿路径保持既有降级（不引入原生发送）",
      "_rescue_documents_via_native_send" not in _draft_src)

# ---------------------------------------------------------------------
print(f"\n===== 结果: {PASS} passed, {FAIL} failed =====")
sys.exit(1 if FAIL else 0)
