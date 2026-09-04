# -*- coding: utf-8 -*-
"""验证「媒体 src 属性裸 & 转义 + 裸 & 备用重试」修复的测试脚本。

背景（2026-09-05 线上日志 chat=7162243624 trace a9cb0e36）：
  模型输出 <tg-document src="R2 预签名 URL（含 5 个裸 &）"></tg-document>，
  payload 层未做属性级 & 转义，Telegram Rich Message 服务端解析出的 src
  在 & 处损坏/被判非法，服务端媒体抓取失败 → 草稿与正式发送均被
  RICH_MESSAGE_DOCUMENT_* 拒绝 → 草稿降级为链接、正式消息定向降级为链接。
  文档自始至终未能以内联形式送达。
  s3_utils.generate_presigned_url 的注释早已约定"调用方会在 HTML 属性中
  将查询参数的 & 幂等转义为 &amp;"，但该转义从未实现——本次补齐。

覆盖：
1. _escape_media_src_ampersands 单元测试（真实日志 URL / 幂等 / 归一化
   &amp; / 负向环视防 data-src 误判 / <a href> 与纯文本不受影响）
2. _rich_message_html_payload 端到端：日志真实内容复现，src 全部转义、
   思考块实体不受影响；escape_attr_amp=False 保留裸 & 形态
3. 与前几道清理的协同：Markdown 解包后仍会被转义
4. 降级链路回归：定向降级产生的 <a href> 保持裸 &（既有行为不回归）
5. 裸 & 备用重试判定模拟：escaped payload 与 raw payload 确有差异时才重试
"""
import os
import re
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src"),
)

from apitelegramchat.utils import (
    _escape_media_src_ampersands,
    _demote_all_media_to_links,
    _rich_message_html_payload,
)

# 2026-09-05 01:28 日志中的真实 R2 预签名 URL（801_router_password 文件，
# 模型实际写入 tg-document src 的那一条）
REAL_URL = (
    "https://a94baf2f82dbdc55044f54eb19838b0c.r2.cloudflarestorage.com/dearella/"
    "telegram/BQACAgEuAxCvAAJ1BWqa_8SFgzXIHiAtmC26lr7QfZSHAAKOJgACZbTZVPcX88dCqSe3PQQ"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=5e1dc48ef14f62c26496ff38f2155b55%2F20260904%2Fauto%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260904T172843Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host"
    "&X-Amz-Signature=01286f2c2b09d149f45624ee65fc6953438a310184402f0f6b4e22789e9745a0"
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


def bare_amp_outside_entities(html: str) -> int:
    """统计不属于合法实体的裸 & 数量（即服务端解析后会损坏的部分）。"""
    return len(re.findall(r'&(?![a-zA-Z0-9#]+;)', html))


print("== 1. _escape_media_src_ampersands 单元测试 ==")

tag = f'<tg-document src="{REAL_URL}"></tg-document>'
out = _escape_media_src_ampersands(tag)
check("真实 URL：5 个裸 & 全部转义", out.count("&amp;") == 5, out)
check("真实 URL：src 内不再有裸 &", bare_amp_outside_entities(out) == 0, out)
check("真实 URL：URL 其余部分原样保留", REAL_URL.replace("&", "") in out.replace("&amp;", ""), out)
check("幂等：二次转义结果不变", _escape_media_src_ampersands(out) == out)

img_tag = '<img src="https://x.com/a?b=1&c=2&d=%E5%9B%BE"/>'
img_out = _escape_media_src_ampersands(img_tag)
check("自闭合 img：裸 & 转义", img_out == '<img src="https://x.com/a?b=1&amp;c=2&amp;d=%E5%9B%BE"/>', img_out)

vid_tag = '<video src="https://x.com/v.mp4?t=1&x=2"></video>'
vid_out = _escape_media_src_ampersands(vid_tag)
check("video 容器：裸 & 转义", vid_out.count("&amp;") == 1, vid_out)

single_quoted = "<audio src='https://x.com/a.mp3?t=1&x=2'></audio>"
sq_out = _escape_media_src_ampersands(single_quoted)
check("单引号属性：裸 & 转义", sq_out.count("&amp;") == 1, sq_out)

already = '<tg-document src="https://x.com/a?b=1&amp;c=2"></tg-document>'
check("已转义 &amp;：不二次处理", _escape_media_src_ampersands(already) == already)

mixed = '<tg-document src="https://x.com/a?b=1&amp;c=2&d=3"></tg-document>'
mixed_out = _escape_media_src_ampersands(mixed)
check("混合形态：&amp; 归一化后统一为单个 &amp;",
      mixed_out == '<tg-document src="https://x.com/a?b=1&amp;c=2&amp;d=3"></tg-document>', mixed_out)

anchor = '<a href="https://x.com/dl?f=1&sig=abc"><b>📄 下载</b></a>'
check("<a href> 不受影响（裸 & 保持）", _escape_media_src_ampersands(anchor) == anchor)

text_case = "<p>Powered by AT&amp;T &amp; Tom&Jerry 1<2&3&gt;4</p>"
check("纯文本不受影响", _escape_media_src_ampersands(text_case) == text_case)

data_src = '<div data-src="https://x.com/a?b=1&c=2"></div>'
check("data-src 复合属性不被误转义", _escape_media_src_ampersands(data_src) == data_src)

no_amp = '<tg-document src="https://example.com/document.pdf"></tg-document>'
check("无 & 的 URL：原样返回", _escape_media_src_ampersands(no_amp) == no_amp)

print("== 2. _rich_message_html_payload 端到端（日志真实内容复现） ==")

# 复刻 01:28 日志的最终消息结构：思考块（模型思考里照抄了含 &amp; 的 URL
# 文本）+ 真实 tg-document 标签（裸 &）
model_output = (
    '<details><summary>用户发送了一个文档，文件名是"801_router_pass…</summary>'
    f"<p>思考文本：链接为 {REAL_URL.replace('&', '&amp;')}，需要转义处理。</p>"
    "</details>"
    f'<tg-document src="{REAL_URL}"></tg-document>'
)
payload = _rich_message_html_payload(model_output)
html_out = payload["html"]
check("端到端：媒体 src 的 5 个 & 全部转义", html_out.count("&amp;") >= 5, html_out)
src_m = re.search(r'<tg-document src="([^"]+)"', html_out)
check("端到端：src 属性可提取且不含裸 &",
      bool(src_m) and bare_amp_outside_entities(src_m.group(1)) == 0, html_out)
check("端到端：src 解码后与原始 URL 一致",
      bool(src_m) and src_m.group(1).replace("&amp;", "&") == REAL_URL)
check("端到端：思考块中的既有实体保持不变（思考文本与 src 各出现一次）",
      html_out.count("&amp;X-Amz-Credential=5e1dc48ef14f62c26496ff38f2155b55") == 2, html_out)
check("端到端：skip_entity_detection 标志保留", payload.get("skip_entity_detection") is True)
check("端到端：幂等", _rich_message_html_payload(html_out)["html"] == html_out)

raw_payload = _rich_message_html_payload(model_output, escape_attr_amp=False)["html"]
check("escape_attr_amp=False：src 保持裸 &（备用重试形态）",
      f'src="{REAL_URL}"' in raw_payload, raw_payload)

print("== 3. 与前几道清理的协同 ==")

md_wrapped = f'<tg-document src="[URL](URL)"></tg-document>'.replace("URL", REAL_URL)
协同_out = _rich_message_html_payload(md_wrapped)["html"]
src_m2 = re.search(r'<tg-document src="([^"]+)"', 协同_out)
check("Markdown 解包 + & 转义协同：解包出的 URL 被正确转义",
      bool(src_m2) and src_m2.group(1).count("&amp;") == 5
      and bare_amp_outside_entities(src_m2.group(1)) == 0, 协同_out)

print("== 4. 降级链路回归（<a href> 保持裸 &） ==")

figure_doc = (
    "<figure>"
    f'<tg-document src="{REAL_URL}"></tg-document>'
    "<figcaption>801_router_password</figcaption>"
    "</figure>"
)
demoted = _demote_all_media_to_links(figure_doc, {"tg-document"})
check("定向降级：tg-document 变 <a href>，href 为裸 &",
      "<a" in demoted and f'href="{REAL_URL}"' in demoted and "tg-document" not in demoted, demoted)
demoted_payload_html = _rich_message_html_payload(demoted)["html"]
check("降级结果再过 payload：<a href> 的裸 & 仍不被转义",
      f'href="{REAL_URL}"' in demoted_payload_html, demoted_payload_html)

print("== 5. 裸 & 备用重试判定模拟（sendRichHtmlMessage / Draft 共用逻辑） ==")

first_html = _rich_message_html_payload(model_output)["html"]
alt_html = _rich_message_html_payload(model_output, escape_attr_amp=False)["html"]
check("两种形态确有差异 → 应触发备用重试", first_html != alt_html)
check("首选拒绝形态为 &amp; 转义形态", bare_amp_outside_entities(
    re.search(r'<tg-document src="([^"]+)"', first_html).group(1)) == 0)
check("备用形态与模型原始输出一致",
      re.search(r'<tg-document src="([^"]+)"', alt_html).group(1) == REAL_URL)

plain_doc = '<tg-document src="https://example.com/document.pdf"></tg-document>'
first_plain = _rich_message_html_payload(plain_doc)["html"]
alt_plain = _rich_message_html_payload(plain_doc, escape_attr_amp=False)["html"]
check("无 & 的 URL：两形态一致 → 跳过备用重试", first_plain == alt_plain)

print()
print(f"总计: PASS={PASS} FAIL={FAIL}")
if FAIL:
    print("存在失败项！")
    sys.exit(1)
print("全部通过 ✔")
