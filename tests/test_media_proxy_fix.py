# -*- coding: utf-8 -*-
"""验证「自托管稳定媒体代理 + 稳定 URL 解析链」修复的测试脚本（v4）。

背景（2026-09-05 02:02 线上日志 chat=7162243624 trace a710fbdc）：
  v3 的 &amp;/裸 & 双形态重试均被 RICH_MESSAGE_DOCUMENT_NO_MEDIA_FOUND 拒绝；
  同一预签名 URL 公网 GET 实测 HTTP 200（把 %2F 解码成 / 也仍是 200）。
  结论：问题不在 URL 形态，而在 Telegram 抓取器无法解析"长查询串 +
  R2 S3 API 端点"URL。v4 让媒体对外一律交付无查询参数的稳定 URL：
    1) R2_PUBLIC_URL（r2.dev / 自定义域）直连；
    2) 自托管 /media 代理（HMAC 路径签名，基地址按
       MEDIA_PROXY_BASE_URL → PUBLIC_BASE_URL → WEBHOOK_URL origin 推导）；
    3) 预签名 URL 只作最后兜底。

覆盖：
1. media_proxy HMAC 签名/校验（确定性、绑定 key、防篡改、空值拒绝、密钥覆盖）
2. 代理基地址推导优先级与 URL 形态
3. s3_utils.resolve_stable_delivery_url 解析链顺序（R2 公开域 → 代理 → None）
4. upload_bytes_to_r2 本地缓存路径的 URL 交付（代理可用 / file:// 兜底）
5. media_proxy.collect_media_bytes 回源（R2 命中 / telegram 回退 / 失败）
6. guess_content_type 推断
7. _rich_message_html_payload 端到端：稳定 URL 原样送达（不被转义/降级），
   v3 的 &amp; 转义对预签名 URL 仍生效（回归）
8. 预签名 URL 诊断提示：命中/不命中
9. attachment_content._resolve_public_attachment_url 交付稳定 URL（模型可见链接根治）

运行：python tests/test_media_proxy_fix.py（或 scripts/ 下的副本）
"""
import asyncio
import os
import re
import sys

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
from apitelegramchat.utils import _rich_message_html_payload

# 2026-09-05 02:02 日志中的真实 R2 预签名 URL（801_router_password，
# 模型实际写入 tg-document src 的那一条；v3 同款回归样本）
REAL_URL = (
    "https://a94baf2f82dbdc55044f54eb19838b0c.r2.cloudflarestorage.com/dearella/"
    "telegram/BQACAgUAAxkBAAJ1C2qb7l61BZ15QjZP76fKJsyiEUpAAKTJgACZbTZVNkKtQHt_OaBPQQ"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=5e1dc48ef14f62c26496ff38f2155b55%2F20260904%2Fauto%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260904T180241Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host"
    "&X-Amz-Signature=341875a1a890e50e0aefef3da7b1a515a6cd3b001821f679efd75bacd4949237"
)

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
section("1. media_proxy HMAC 签名/校验")

p = Patcher()
try:
    p.set(mp, "MEDIA_PROXY_SECRET", "")  # 强制走派生密钥分支
    t1 = mp.sign_media_key("telegram/BQACAgUAAxkBAAJ1C2qbTEST")
    t2 = mp.sign_media_key("telegram/BQACAgUAAxkBAAJ1C2qbTEST")
    check("签名确定性：同 key 两次签名一致", t1 == t2 and t1 != "")
    check("签名绑定 key：不同 key 签名不同",
          t1 != mp.sign_media_key("telegram/OTHER_KEY"))
    check("token 形态：16 位小写 hex", re.fullmatch(r"[0-9a-f]{16}", t1) is not None)
    check("校验通过：正确 token", mp.verify_media_token(
        "telegram/BQACAgUAAxkBAAJ1C2qbTEST", t1) is True)
    check("防篡改：改一位 hex 即拒绝",
          mp.verify_media_token("telegram/BQACAgUAAxkBAAJ1C2qbTEST",
                                ("0" if t1[0] != "0" else "1") + t1[1:]) is False)
    check("空 token 拒绝", mp.verify_media_token("telegram/x", "") is False)
    check("空 key 拒绝", mp.verify_media_token("", t1) is False)

    p.set(mp, "MEDIA_PROXY_SECRET", "explicit-secret")
    t3 = mp.sign_media_key("telegram/BQACAgUAAxkBAAJ1C2qbTEST")
    check("显式 MEDIA_PROXY_SECRET 覆盖派生密钥", t3 != t1 and t3 != "")
finally:
    p.restore_all()

# ---------------------------------------------------------------------
section("2. 代理基地址推导与 URL 形态")

p = Patcher()
try:
    p.set(mp, "MEDIA_PROXY_BASE_URL", "")
    p.set(mp, "PUBLIC_BASE_URL", "")
    p.set(mp, "PUBLIC_WEBHOOK_URL", "")
    check("全空 → None", mp.media_proxy_base_url() is None)

    p.set(mp, "PUBLIC_WEBHOOK_URL", "https://svc.example.com/webhook?token=abc")
    check("WEBHOOK_URL 只取 origin（去掉路径与 query）",
          mp.media_proxy_base_url() == "https://svc.example.com")

    p.set(mp, "PUBLIC_BASE_URL", "https://public.example.com/")
    check("PUBLIC_BASE_URL 次优先（去尾部斜杠）",
          mp.media_proxy_base_url() == "https://public.example.com")

    p.set(mp, "MEDIA_PROXY_BASE_URL", "https://cdn.example.com")
    check("MEDIA_PROXY_BASE_URL 最优先",
          mp.media_proxy_base_url() == "https://cdn.example.com")

    p.set(mp, "MEDIA_PROXY_BASE_URL", "ftp://bad.example.com")
    check("非法 scheme 候选被跳过、回落下一优先级",
          mp.media_proxy_base_url() == "https://public.example.com")

    p.set(mp, "MEDIA_PROXY_BASE_URL", "https://bot.example.com")
    url = mp.build_media_proxy_url("telegram/BQACAgUAAxkBAAJ1C2qb7l61BZ15QjZ")
    check("代理 URL 形态：{base}/media/<16hex>/<key>",
          re.fullmatch(
              r"https://bot\.example\.com/media/[0-9a-f]{16}"
              r"/telegram/BQACAgUAAxkBAAJ1C2qb7l61BZ15QjZ",
              url or "") is not None,
          f"actual={url}")
    check("代理 URL 不含查询参数", url is not None and "?" not in url)

    p.set(mp, "MEDIA_PROXY_BASE_URL", "")
    p.set(mp, "PUBLIC_BASE_URL", "")
    p.set(mp, "PUBLIC_WEBHOOK_URL", "")
    check("基地址不可得 → build 返回 None", mp.build_media_proxy_url("telegram/x") is None)
finally:
    p.restore_all()

# ---------------------------------------------------------------------
section("3. resolve_stable_delivery_url 解析链顺序")

KEY = "telegram/BQACAgUAAxkBAAJ1C2qbRESOLVE"
p = Patcher()
try:
    p.set(s3u, "_public_delivery_base_url", lambda: "https://pub.r2dev.example.com")
    p.set(s3u, "build_media_proxy_url",
          lambda k, filename="": (_ for _ in ()).throw(AssertionError("不应走到代理")))
    check("R2 公开域优先于代理",
          asyncio.run(s3u.resolve_stable_delivery_url(KEY))
          == f"https://pub.r2dev.example.com/{KEY}")

    p.set(s3u, "_public_delivery_base_url", lambda: None)
    p.set(s3u, "build_media_proxy_url", lambda k, filename="": f"https://bot.example.com/media/ff/{k}")
    check("无公开域时走自托管代理",
          asyncio.run(s3u.resolve_stable_delivery_url(KEY))
          == f"https://bot.example.com/media/ff/{KEY}")

    p.set(s3u, "build_media_proxy_url", lambda k, filename="": None)
    check("两者都不可得 → None",
          asyncio.run(s3u.resolve_stable_delivery_url(KEY)) is None)

    def _boom(k):
        raise RuntimeError("boom")
    p.set(s3u, "build_media_proxy_url", _boom)
    check("代理构造异常 → 防御性返回 None",
          asyncio.run(s3u.resolve_stable_delivery_url(KEY)) is None)
finally:
    p.restore_all()

# ---------------------------------------------------------------------
section("4. upload_bytes_to_r2 本地缓存路径的 URL 交付")

TEST_KEY = "telegram/V4TEST_KEY_CLEANUP"
p = Patcher()
try:
    p.set(s3u, "is_r2_configured", lambda: False)
    p.set(s3u, "_public_delivery_base_url", lambda: None)

    p.set(s3u, "build_media_proxy_url", lambda k, filename="": f"https://bot.example.com/media/aa/{k}")
    url = asyncio.run(s3u.upload_bytes_to_r2(b"v4-proxy-test", TEST_KEY, "text/plain"))
    check("本地缓存 + 代理可用 → 返回代理 URL", url == f"https://bot.example.com/media/aa/{TEST_KEY}",
          f"actual={url}")
    check("本地缓存文件已写入 r2_cache", s3u._safe_local_key_path(TEST_KEY).exists())

    p.set(s3u, "build_media_proxy_url", lambda k, filename="": None)
    url2 = asyncio.run(s3u.upload_bytes_to_r2(b"v4-local-test", TEST_KEY, "text/plain"))
    check("本地缓存 + 无代理基地址 → file:// 兜底",
          isinstance(url2, str) and url2.startswith("file://"), f"actual={url2}")

    p.set(s3u, "_public_delivery_base_url", lambda: "https://pub.example.com")
    url3 = asyncio.run(s3u.upload_bytes_to_r2(b"v4-pub-test", TEST_KEY, "text/plain"))
    check("本地缓存 + R2 公开域 → {base}/{key}",
          url3 == f"https://pub.example.com/{TEST_KEY}", f"actual={url3}")
finally:
    p.restore_all()
    try:
        asyncio.run(s3u.delete_r2_object(TEST_KEY))
    except Exception:
        pass

# ---------------------------------------------------------------------
section("5. collect_media_bytes 回源")

p = Patcher()
try:
    async def _r2_hit(key):
        return (b"PDFDATA", "application/pdf")

    p.set(s3u, "fetch_r2_object", _r2_hit)
    got = asyncio.run(mp.collect_media_bytes("telegram/ANY"))
    check("R2 命中：返回 (bytes, R2 ContentType)",
          got == (b"PDFDATA", "application/pdf"), f"actual={got}")

    async def _r2_miss(key):
        return None

    p.set(s3u, "fetch_r2_object", _r2_miss)
    check("R2 miss 且非 telegram/ 前缀 → None",
          asyncio.run(mp.collect_media_bytes("generated/xxx.png")) is None)
    check("空 key → None", asyncio.run(mp.collect_media_bytes("")) is None)

    async def _fake_download(file_id, file_path):
        with open(file_path, "wb") as f:
            f.write(b"TGDATA")
        return True

    async def _fake_remote_path(file_id):
        return "documents/file_1.pdf"

    p.set(fh, "download_file", _fake_download)
    p.set(fh, "get_file_path", _fake_remote_path)
    got2 = asyncio.run(mp.collect_media_bytes("telegram/BQACAgUAAxkBAAJ1FALLBACK"))
    check("R2 miss → Telegram 回源成功且按远端扩展名推 MIME",
          got2 == (b"TGDATA", "application/pdf"), f"actual={got2}")

    async def _fake_download_fail(file_id, file_path):
        return False

    p.set(fh, "download_file", _fake_download_fail)
    check("Telegram 回源失败 → None",
          asyncio.run(mp.collect_media_bytes("telegram/BQACAgUAAxkBAAJ1FAIL2")) is None)
finally:
    p.restore_all()

# ---------------------------------------------------------------------
section("6. guess_content_type")

check("R2 ContentType 优先透传",
      mp.guess_content_type("telegram/x", "application/pdf") == "application/pdf")
check("octet-stream 视为未记录，按扩展名推断",
      mp.guess_content_type("doc/report.pdf", "application/octet-stream") == "application/pdf")
check("无扩展名且未记录 → octet-stream 兜底",
      mp.guess_content_type("telegram/BQACAgUA") == "application/octet-stream")
check("上传端记录的同款映射（.docx）正确",
      mp.guess_content_type("a.docx", "") ==
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

# ---------------------------------------------------------------------
section("7. _rich_message_html_payload 端到端")

PROXY_URL = f"https://bot.example.com/media/{mp.sign_media_key('telegram/BQACAgUAAxkBAAJ1C2qb7l61BZ15QjZ')}/telegram/BQACAgUAAxkBAAJ1C2qb7l61BZ15QjZ"
doc_html = (
    '<details><summary>…</summary>\n<p>思考内容</p>\n</details>\n'
    "根据您的要求，我将通过内联文档方式发送此文件：\n"
    f'<figure><tg-document src="{PROXY_URL}"></tg-document>'
    "<figcaption>801_router_password</figcaption></figure>"
)

rich = _rich_message_html_payload(doc_html)
check("稳定代理 URL 原样保留（不被剥离/降级）", f'src="{PROXY_URL}"' in rich["html"])
check("稳定 URL 无 & ，不触发属性转义", "&amp;" not in rich["html"])
check("figcaption 保留", "<figcaption>801_router_password</figcaption>" in rich["html"])

rich2 = _rich_message_html_payload(rich["html"])
check("payload 幂等：二次清洗不变形", rich2["html"] == rich["html"])

presigned_html = (
    f'<figure><tg-document src="{REAL_URL}"></tg-document>'
    "<figcaption>801_router_password</figcaption></figure>"
)
rich3 = _rich_message_html_payload(presigned_html)
check("v3 回归：预签名 URL 仍被转义为 &amp; 形态", "&amp;X-Amz-Signature" in rich3["html"])
rich3_raw = _rich_message_html_payload(presigned_html, escape_attr_amp=False)
check("双形态重试前提仍成立：escaped 与 raw 形态确有差异",
      rich3["html"] != rich3_raw["html"])

# ---------------------------------------------------------------------
section("8. 预签名 URL 诊断提示")

from apitelegramchat.utils import _presigned_media_diagnostic_hint as hint

h1 = hint(presigned_html)
check("预签名 src 命中提示", bool(h1) and "R2" in h1)
check("提示包含可操作指引（/media 与 WEBHOOK_URL）",
      "/media" in h1 and "WEBHOOK_URL" in h1)
check("稳定代理 src 不提示", hint(doc_html) == "")
check("无媒体标签不提示", hint("<p>纯文本</p>") == "")

# ---------------------------------------------------------------------
section("9. _resolve_public_attachment_url 交付稳定 URL")

from apitelegramchat.ai import attachment_content as ac

p = Patcher()
try:
    async def _exists(key):
        return True

    async def _stable_hit(key, filename=""):
        return f"https://bot.example.com/media/bb/{key}"

    async def _stable_miss(key, filename=""):
        return None

    async def _presigned(key):
        return "https://presigned.example.com/fake"

    p.set(ac, "file_exists_in_r2", _exists)

    p.set(ac, "resolve_stable_delivery_url", _stable_hit)
    u1 = asyncio.run(ac._resolve_public_attachment_url("BQACAgUAAxkBAAJ1C2qbTEST01"))
    check("模型可见链接 → 稳定代理 URL", u1 == f"https://bot.example.com/media/bb/telegram/BQACAgUAAxkBAAJ1C2qbTEST01",
          f"actual={u1}")

    p.set(ac, "resolve_stable_delivery_url", _stable_miss)
    p.set(ac, "generate_presigned_url", _presigned)
    u2 = asyncio.run(ac._resolve_public_attachment_url("BQACAgUAAxkBAAJ1C2qbTEST02"))
    check("稳定链路不可得 → 预签名兜底", u2 == "https://presigned.example.com/fake",
          f"actual={u2}")

    async def _not_exists(key):
        return False

    p.set(ac, "file_exists_in_r2", _not_exists)
    u3 = asyncio.run(ac._resolve_public_attachment_url("BQACAgUAAxkBAAJ1C2qbMISS03"))
    check("R2 无对象 → 空串（调用方降级 file_id 文本）", u3 == "")

    u4 = asyncio.run(ac._resolve_public_attachment_url(""))
    check("空 file_id → 空串", u4 == "")
finally:
    p.restore_all()

# ---------------------------------------------------------------------
print(f"\n{'=' * 50}")
print(f"总计: {PASS} passed, {FAIL} failed")
if FAIL:
    sys.exit(1)
print("全部通过 ✔")
