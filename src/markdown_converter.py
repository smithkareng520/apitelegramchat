"""Markdown 到 Telegram Rich Message HTML 的转换层。

用于兜底处理模型输出的 Markdown 语法，确保即使不依赖提示词约束，
也能正确渲染为 Telegram 支持的 HTML 标签。
"""
import re
import html as html_lib
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)


# 只匹配「不是合法 HTML 实体开头」的裸 & ——即后面没有紧跟
# `name;` / `#123;` / `#x1F;` 形式的分号结尾序列。
# 用于避免把模型已经正确转义的 &amp; / &lt; / &#39; 二次转义成
# &amp;amp;（用户侧会看到字面量 "&amp;" 而不是 "&"）。
_BARE_AMP_RE = re.compile(r'&(?![A-Za-z][A-Za-z0-9]*;|#[0-9]+;|#[xX][0-9A-Fa-f]+;)')

# Telegram Rich HTML 的块级容器。
# 这些块一旦已经是 HTML，就应当作为“原子块”保留；只转换块与块之间的
# 自由文本/Markdown，而不能因为其中出现一个 <pre> 或 <details> 就把整条消息
# 短路。
# _RICH_BLOCK_SPLIT_RE 用于最终块级包装；_OPAQUE_HTML_SPLIT_RE 只用于
# Markdown 转换阶段。列表、段落、details、表格等“容器”并不需要整块冻结，
# 这样其中意外出现的 Markdown 仍能被修复；pre/media/button/math 等则视为
# 不可安全猜测其内部语义的 opaque block。
_RICH_BLOCK_SPLIT_RE = re.compile(
    r'('
    r'<p\b[^>]*>.*?</p\s*>'
    r'|<details\b[^>]*>.*?</details\s*>'
    r'|<ul\b[^>]*>.*?</ul\s*>'
    r'|<ol\b[^>]*>.*?</ol\s*>'
    r'|<pre\b[^>]*>.*?</pre\s*>'
    r'|<blockquote\b[^>]*>.*?</blockquote\s*>'
    r'|<table\b[^>]*>.*?</table\s*>'
    r'|<h[1-6]\b[^>]*>.*?</h[1-6]\s*>'
    r'|<aside\b[^>]*>.*?</aside\s*>'
    r'|<footer\b[^>]*>.*?</footer\s*>'
    r'|<figure\b[^>]*>.*?</figure\s*>'
    r'|<tg-slideshow\b[^>]*>.*?</tg-slideshow\s*>'
    r'|<video\b[^>]*>.*?</video\s*>'
    r'|<audio\b[^>]*>.*?</audio\s*>'
    r'|<tg-button\b[^>]*>.*?</tg-button\s*>'
    r'|<tg-math-block\b[^>]*>.*?</tg-math-block\s*>'
    r'|<hr\b[^>]*/?>'
    r'|<img\b[^>]*/?>'
    r'|<tg-map\b[^>]*/?>'
    r')',
    re.DOTALL | re.IGNORECASE,
)

_OPAQUE_HTML_SPLIT_RE = re.compile(
    r'('
    r'<pre\b[^>]*>.*?</pre\s*>'
    r'|<figure\b[^>]*>.*?</figure\s*>'
    r'|<tg-slideshow\b[^>]*>.*?</tg-slideshow\s*>'
    r'|<video\b[^>]*>.*?</video\s*>'
    r'|<audio\b[^>]*>.*?</audio\s*>'
    r'|<tg-button\b[^>]*>.*?</tg-button\s*>'
    r'|<tg-math-block\b[^>]*>.*?</tg-math-block\s*>'
    r'|<hr\b[^>]*/?>'
    r'|<img\b[^>]*/?>'
    r'|<tg-map\b[^>]*/?>'
    r')',
    re.DOTALL | re.IGNORECASE,
)

_BLOCK_START_PREFIXES = (
    '<p>', '<p ', '<details', '<h', '<ul>', '<ul ', '<ol>', '<ol ',
    '<pre>', '<pre ', '<blockquote', '<table>', '<table ', '<aside', '<footer',
    '<figure', '<tg-slideshow', '<video', '<audio', '<tg-button', '<tg-math-block',
    '<hr', '<img', '<tg-map',
)


def _escape_prose(text: str) -> str:
    """转义正文中裸露的 `<`、`>`、`&`，但保留已有的 HTML 实体。

    与 ``html.escape`` 的区别只在 ``&``：``html.escape`` 会把已经转义好的
    ``&amp;`` 再转成 ``&amp;amp;``。Telegram 不像浏览器那样对多余的实体
    「宽容还原」，它会忠实地把 ``&amp;amp;`` 渲染成可见的字面量
    ``&amp;``——正是用户报告的现象。这里改为只转义裸 ``&``，保证
    「转一次」和「转两次」结果一致（幂等）。
    """
    if not text:
        return text
    text = _BARE_AMP_RE.sub('&amp;', text)
    return text.replace('<', '&lt;').replace('>', '&gt;')


# ---- <tg-button> 强模式校验与降级 ----
# Telegram RichMessage 的 <tg-button> 是强模式标签：type 必填；
# type="url" 时 url 必填，type="copy_text" 时 text 必填；标签内必须有
# 可见文字；不允许嵌套。模型输出不保证模式正确——典型场景：用户要求
# “直接回复 <tg-button>”，模型就原样输出一个裸标签。残缺/非法按钮
# 一旦原样透传，Telegram 会以 BUTTON_URL_INVALID 等 400 拒绝整条消息，
# 且该错误不在发送层媒体/结构降级分支内，最终表现为“富文本发送失败、
# 不再降级”。因此这里在转换边界做代码级校验：非法按钮整体转义为
# 字面量文本（用户看到标签原文而不是整条消息发送失败），合法按钮
# 原样保留。这是结构兜底，不依赖提示词堆砌。
_TG_BUTTON_PRESENT_RE = re.compile(r"tg-button", re.IGNORECASE)
_TG_BUTTON_TAG_RE = re.compile(r"</?tg-button\b[^>]*>", re.IGNORECASE)
_TG_BUTTON_ATTR_RE = re.compile(
    r"""([A-Za-z_][\w-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""",
    re.IGNORECASE,
)
_TG_BUTTON_VALID_URL_RE = re.compile(r"^(?:https?|tg)://", re.IGNORECASE)
_TG_BUTTON_VALID_TYPES = ("url", "copy_text")


def _parse_tg_button_attrs(attrs_str: str) -> dict:
    """把 <tg-button ...> 的属性串解析为 dict（双引号/单引号均可）。"""
    attrs: dict = {}
    for key, dq, sq in _TG_BUTTON_ATTR_RE.findall(attrs_str):
        attrs[key.lower()] = dq if dq else sq
    return attrs


def _tg_button_invalid_reason(attrs: dict, inner: str) -> str | None:
    """返回 None 表示按钮合法，否则返回不合法原因（用于日志与测试）。"""
    btype = (attrs.get("type") or "").strip().lower()
    if btype not in _TG_BUTTON_VALID_TYPES:
        return f"type 必填且只能为 url/copy_text（实际：{btype!r}）"
    if btype == "url":
        url = (attrs.get("url") or "").strip()
        if not url:
            return "type=url 时 url 属性必填"
        if not _TG_BUTTON_VALID_URL_RE.match(url):
            return f"url 需以 http(s):// 或 tg:// 开头（实际：{url[:80]!r}）"
    else:
        text = (attrs.get("text") or "").strip()
        if not text:
            return "type=copy_text 时 text 属性必填"
    if not inner.strip():
        return "按钮显示文本为空"
    if "<" in inner:
        return "按钮显示文本不允许包含 HTML 标签（含嵌套 tg-button）"
    return None


def sanitize_tg_buttons(html: str) -> str:
    """校验并修复 Telegram <tg-button> 标签，非法按钮降级为字面量文本。

    - 合法按钮（type 必填、url/text 按类型必填、有可见文本、不嵌套）
      原样保留；
    - 非法/残缺按钮（裸标签、缺属性、非法 URL、空文本、嵌套）整体
      转义为字面量（&lt;tg-button ...&gt;），避免 BUTTON_URL_INVALID
      类 400 让整条消息发送失败；
    - 对已转义的 &lt;tg-button&gt; 幂等，不会二次转义。
    """
    if not html or not _TG_BUTTON_PRESENT_RE.search(html):
        return html
    tokens = [(m.start(), m.end(), m.group(0)) for m in _TG_BUTTON_TAG_RE.finditer(html)]
    if not tokens:
        return html

    # 用栈配对 <tg-button> 与 </tg-button>
    stack: List[int] = []
    pairs: dict = {}
    for i, (_, _, tag) in enumerate(tokens):
        if tag.startswith("</"):
            if stack:
                pairs[stack.pop()] = i
        else:
            stack.append(i)
    unclosed = set(stack)
    closed_opens = set(pairs)
    stray_close = {
        i for i, (_, _, tag) in enumerate(tokens)
        if tag.startswith("</") and i not in pairs.values()
    }

    # 非法配对整体跨度（用于吸收其内部被误判为独立按钮的嵌套配对）
    invalid_pair_spans: List[Tuple[int, int]] = []
    escape_tokens: set = set()

    for open_i in closed_opens:
        close_i = pairs[open_i]
        open_tag = tokens[open_i][2]
        attrs_str = re.sub(r"^<tg-button\b", "", open_tag, flags=re.IGNORECASE).rstrip(">").strip()
        inner = html[tokens[open_i][1]:tokens[close_i][0]]
        reason = _tg_button_invalid_reason(_parse_tg_button_attrs(attrs_str), inner)
        if reason is not None:
            escape_tokens.add(open_i)
            escape_tokens.add(close_i)
            invalid_pair_spans.append((tokens[open_i][0], tokens[close_i][1]))
            logger.warning(
                "tg-button 非法，已转义为字面量文本：%s（原文：%s…）",
                reason, open_tag[:120],
            )
    for i in unclosed:
        escape_tokens.add(i)
        logger.warning("tg-button 未闭合，已转义为字面量文本：%s…", tokens[i][2][:120])
    for i in stray_close:
        escape_tokens.add(i)
        logger.warning("tg-button 出现多余的闭合标签，已转义为字面量文本：%s…", tokens[i][2][:120])
    # 嵌套在非法按钮内部的“合法”配对并不是独立按钮，同样转义
    for open_i in closed_opens:
        s, e = tokens[open_i][0], tokens[pairs[open_i]][1]
        if any(ps <= s and e <= pe for ps, pe in invalid_pair_spans):
            escape_tokens.add(open_i)
            escape_tokens.add(pairs[open_i])

    if not escape_tokens:
        return html

    out: List[str] = []
    prev = 0
    for i, (s, e, tag) in enumerate(tokens):
        if i in escape_tokens:
            out.append(html[prev:s])
            out.append(_escape_prose(tag))
            prev = e
    out.append(html[prev:])
    return "".join(out)


def _convert_mixed_document(text: str) -> str:
    """转换 HTML/Markdown 混合文档，同时保持 HTML 容器嵌套结构。

    不能简单 split 掉 ``<pre>`` / ``<img>``：例如 ``<details>`` 内部有
    ``<pre>`` 时，split 会把内部块“抬到” details 外面，造成新的非法嵌套。
    这里采用占位符保护：只保护 opaque block，本身不拆容器，转换完成后再回填。
    """
    shelf: List[str] = []

    def _park_opaque(match: re.Match) -> str:
        shelf.append(match.group(1))
        return f'\x00RICH{len(shelf) - 1}\x00'

    protected = _OPAQUE_HTML_SPLIT_RE.sub(_park_opaque, text)
    converted = _convert(protected)
    for index, fragment in enumerate(shelf):
        converted = converted.replace(f'\x00RICH{index}\x00', fragment)
    return converted


def _readable_plaintext_fallback(text: str) -> str:
    """Markdown/HTML 转换异常时生成可发送的、可读的纯文本 HTML。

    发送层最不应该做的事情是为了格式化失败而让整条消息 400。这个回退保留
    链接 URL、媒体提示和换行，并把其它 HTML 标记安全转义，让用户至少拿到
    完整内容。
    """
    value = text or ''
    value = re.sub(
        r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a\s*>',
        lambda m: f'{m.group(2)} ({m.group(1)})',
        value, flags=re.IGNORECASE | re.DOTALL,
    )
    value = re.sub(
        r'<(?:img|video|audio)\b[^>]*?src=["\']([^"\']+)["\'][^>]*/?>',
        lambda m: f'[媒体: {m.group(1)}]',
        value, flags=re.IGNORECASE | re.DOTALL,
    )
    value = re.sub(r'<br\s*/?>', '\n', value, flags=re.IGNORECASE)
    value = re.sub(r'</(?:p|div|h[1-6]|li|blockquote|tr|details|figure|footer|aside)\s*>', '\n', value, flags=re.IGNORECASE)
    value = re.sub(r'<[^>]+>', '', value)
    value = html_lib.unescape(value)
    return html_lib.escape(value, quote=False).replace('\n', '<br/>')


def convert_markdown_to_telegram_html(text: str) -> str:
    """将 Markdown / 混合 HTML 转为 Telegram Rich Message HTML。

    处理策略：
    - 已有合法 Rich HTML 块按块保护，不再因为出现一个 ``<pre>`` 就短路整条消息；
    - HTML 块外的 Markdown 独立转换，因此 ``<pre>...</pre>`` 后面的 ``**粗体**``
      / 表格 / 列表仍会被处理；
    - 流式场景允许“未闭合 Markdown”暂时原样显示，下一帧累计完整后自动恢复格式；
    - 转换器自身发生异常时回退为可读纯文本 HTML，优先保证送达。
    """
    if not text or not text.strip():
        return text

    # 先做按钮结构兜底；即使整条消息已经是 HTML，也必须检查。
    text = sanitize_tg_buttons(text)

    try:
        if not _contains_markdown(text):
            # 没有 Markdown 时，仍然返回现有 HTML/纯文本原貌。
            return text
        return _convert_mixed_document(text)
    except Exception:
        logger.exception('Markdown → Telegram HTML 转换异常，已回退为可读纯文本')
        return _readable_plaintext_fallback(text)


def _contains_markdown(text: str) -> bool:
    """检测文本是否包含 Markdown 语法。"""
    markdown_patterns = [
        r'^#{1,6}\s+',  # 标题
        r'\*\*[^*]+\*\*',  # 粗体
        r'__[^_]+__',  # 粗体
        r'\*[^*]+\*',  # 斜体
        r'(?<!\w)_[^_\n]+_(?!\w)',  # 斜体
        r'~~[^~]+~~',  # 删除线
        r'\|\|[^|]+\|\|',  # 常见 spoiler Markdown 方言
        r'`[^`]+`',  # 行内代码
        r'```[\s\S]*?```',  # 代码块
        r'~~~[\s\S]*?~~~',  # alternative code fence
        r'!\[.*?\]\(.*?\)',  # 图片
        r'\[.*?\]\(.*?\)',  # 链接
        r'<https?://[^>]+>',  # Markdown 自动链接
        r'^\s*[-*+]\s+',  # 无序列表
        r'^\s*[-*+]\s+\[[ xX]\]\s+',  # task list
        r'^\s*\d+[.)]\s+',  # 有序列表
        r'^>\s+',  # 引用
        r'^[-*_]{3,}\s*$',  # 水平线
        r'^\|.+\|',  # 表格
    ]
    
    for pattern in markdown_patterns:
        if re.search(pattern, text, re.MULTILINE):
            return True
    
    return False


def _convert(text: str) -> str:
    """执行 Markdown 到 HTML 的转换。"""
    lines = text.split('\n')
    result = []
    i = 0
    
    while i < len(lines):
        line = lines[i]
        
        # 代码块（需要先处理，避免内部被转义）。流式阶段可能只有开始围栏
        # 没有结束围栏；此时不要擅自把半成品闭合成 <pre>，否则代码里的
        # **bold** / [link](...) 会被错误解析。保留围栏本身，等待下一帧补齐。
        if line.strip().startswith('```'):
            closing_index = None
            for j in range(i + 1, len(lines)):
                if lines[j].strip() == '```':
                    closing_index = j
                    break
            if closing_index is None:
                # 未闭合围栏意味着余下内容仍属于“可能是代码”的流式半成品。
                # 整段原样作为安全文本显示，绝不在其中执行 Markdown 替换；
                # 下一帧补齐 ``` 后，整个累计 buffer 会重新转换。
                result.append(_escape_prose('\n'.join(lines[i:])))
                break
            code_block, lines_consumed = _extract_code_block(lines[i:])
            result.append(code_block)
            i += lines_consumed
            continue
        
        # 表格（需要整体处理多行）。仅有一行 ``||文本||`` 不能算表格，
        # 否则会把 Markdown spoiler 误判为表格并导致 ``||`` 原样漏出。
        if _is_table_row(line) and i + 1 < len(lines) and _is_table_delimiter(lines[i + 1]):
            table_html, lines_consumed = _extract_table(lines[i:])
            result.append(table_html)
            i += lines_consumed
            continue
        
        # 标题
        if line.strip().startswith('#'):
            result.append(_convert_heading(line))
            i += 1
            continue
        
        # 水平线
        # Telegram/本项目的 Markdown 兼容层不把单独的 `***` 当水平线：
        # 这类输出很常见于模型测试星号数量（例如用户要求原样输出 `***`）。
        # 若把它转换成 `<hr/>`，消息虽然可能通过 API，但正文没有可见文本，
        # 用户会感觉“整条消息消失”。只有明确使用减号形式的 `---` 才转换为
        # Rich HTML 分隔线；`___` 也保持为普通文本，避免与 `__粗体__` 冲突。
        if re.match(r'^\s*-{3,}\s*$', line):
            result.append('<hr/>')
            i += 1
            continue
        
        # 引用
        if line.strip().startswith('>'):
            quote_block, lines_consumed = _extract_blockquote(lines[i:])
            result.append(quote_block)
            i += lines_consumed
            continue
        
        # 无序列表
        if re.match(r'^\s*[-*+]\s+', line):
            list_html, lines_consumed = _extract_unordered_list(lines[i:])
            result.append(list_html)
            i += lines_consumed
            continue
        
        # 有序列表
        if re.match(r'^\s*\d+[.)]\s+', line):
            list_html, lines_consumed = _extract_ordered_list(lines[i:])
            result.append(list_html)
            i += lines_consumed
            continue
        
        # 普通段落（处理行内格式）
        if line.strip():
            result.append(_convert_inline(line))
        else:
            # 保留空行
            result.append('')
        
        i += 1
    
    return '\n'.join(result)


def _convert_heading(line: str) -> str:
    """转换标题。"""
    match = re.match(r'^(#{1,6})\s+(.+)$', line.strip())
    if not match:
        return line
    
    level = len(match.group(1))
    content = _convert_inline(match.group(2))
    return f'<h{level}>{content}</h{level}>'


def _extract_code_block(lines: List[str]) -> Tuple[str, int]:
    """提取代码块。返回 (HTML, 消耗的行数)。"""
    first_line = lines[0].strip()
    lang_match = re.match(r'^```([\w+.-]+)?', first_line)
    lang = lang_match.group(1) if lang_match and lang_match.group(1) else ''
    
    code_lines = []
    i = 1
    while i < len(lines):
        if lines[i].strip() == '```':
            break
        code_lines.append(lines[i])
        i += 1
    
    # 转义代码内容。用 _escape_prose 而非 html_lib.escape：后者对
    # 「已按提示词输出合法实体」的代码（如 &lt;、&amp;）会二次转义成
    # &amp;lt;，Telegram 会把字面量 &amp;lt; 原样画给用户。
    # _escape_prose 只转义裸 &，对已转义实体幂等。
    code_content = '\n'.join(code_lines)
    escaped_code = _escape_prose(code_content)
    
    if lang:
        html = f'<pre><code class="language-{html_lib.escape(lang)}">{escaped_code}</code></pre>'
    else:
        html = f'<pre><code>{escaped_code}</code></pre>'
    
    return html, i + 1  # +1 for closing ```


def _is_table_row(line: str) -> bool:
    """检测是否为表格行。"""
    stripped = line.strip()
    return stripped.startswith('|') and stripped.endswith('|') and stripped.count('|') >= 2


def _is_table_delimiter(line: str) -> bool:
    """判断一行是否确实是 Markdown 表格分隔线。"""
    if not _is_table_row(line):
        return False
    cells = _parse_table_row(line)
    if not cells:
        return False
    return all(bool(re.fullmatch(r':?-{1,}:?', cell.strip())) for cell in cells)


def _extract_table(lines: List[str]) -> Tuple[str, int]:
    """提取表格。返回 (HTML, 消耗的行数)。"""
    table_lines = []
    i = 0
    
    while i < len(lines) and _is_table_row(lines[i]):
        table_lines.append(lines[i])
        i += 1
    
    if len(table_lines) < 2:
        # 至少需要标题行和分隔行
        return lines[0], 1
    
    # 解析表格
    header_cells = _parse_table_row(table_lines[0])
    
    # 第二行是分隔符，跳过
    data_rows = []
    for line in table_lines[2:]:
        cells = _parse_table_row(line)
        data_rows.append(cells)
    
    # 构建 HTML
    html_parts = ['<table bordered striped>']
    
    # 表头
    html_parts.append('<tr>')
    for cell in header_cells:
        html_parts.append(f'<th>{_convert_inline(cell)}</th>')
    html_parts.append('</tr>')
    
    # 数据行
    for row in data_rows:
        html_parts.append('<tr>')
        for cell in row:
            html_parts.append(f'<td>{_convert_inline(cell)}</td>')
        html_parts.append('</tr>')
    
    html_parts.append('</table>')
    
    return ''.join(html_parts), i


def _parse_table_row(line: str) -> List[str]:
    """解析表格行，返回单元格列表。"""
    # 移除首尾的 |
    stripped = line.strip()
    if stripped.startswith('|'):
        stripped = stripped[1:]
    if stripped.endswith('|'):
        stripped = stripped[:-1]
    
    # 分割单元格
    cells = [cell.strip() for cell in stripped.split('|')]
    return cells


def _extract_blockquote(lines: List[str]) -> Tuple[str, int]:
    """提取引用块。返回 (HTML, 消耗的行数)。"""
    quote_lines = []
    i = 0
    
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith('>'):
            # 移除 > 前缀
            content = re.sub(r'^\s*>\s?', '', line)
            quote_lines.append(content)
            i += 1
        elif not line.strip() and quote_lines:
            # 引用块内的空行
            quote_lines.append('')
            i += 1
        else:
            break
    
    # 递归处理引用内容（可能包含其他格式）
    quote_content = '\n'.join(quote_lines)
    # 对引用内容也进行行内转换
    converted_lines = [_convert_inline(l) if l.strip() else '' for l in quote_lines]
    
    return f'<blockquote>{" ".join(converted_lines) if converted_lines else ""}</blockquote>', i


def _extract_unordered_list(lines: List[str]) -> Tuple[str, int]:
    """提取无序列表。返回 (HTML, 消耗的行数)。"""
    list_items = []
    i = 0
    
    while i < len(lines):
        line = lines[i]
        match = re.match(r'^\s*([-*+])\s+(.+)$', line)
        if match:
            content = match.group(2)
            task = re.match(r'^\[[ xX]\]\s+(.+)$', content)
            if task:
                checked = content[1].lower() == 'x'
                content = ('☑ ' if checked else '☐ ') + task.group(1)
            content = _convert_inline(content)
            list_items.append(f'<li>{content}</li>')
            i += 1
        else:
            break
    
    html = '<ul>' + ''.join(list_items) + '</ul>'
    return html, i


def _extract_ordered_list(lines: List[str]) -> Tuple[str, int]:
    """提取有序列表。返回 (HTML, 消耗的行数)。"""
    list_items = []
    i = 0
    
    while i < len(lines):
        line = lines[i]
        match = re.match(r'^\s*\d+[.)]\s+(.+)$', line)
        if match:
            content = _convert_inline(match.group(1))
            list_items.append(f'<li>{content}</li>')
            i += 1
        else:
            break
    
    html = '<ol>' + ''.join(list_items) + '</ol>'
    return html, i


def _convert_inline(text: str) -> str:
    """转换行内格式（粗体、斜体、代码、链接等）。

    关键顺序：先把「不可再解析」的片段（已有 HTML 标签、行内代码、
    链接/图片）抽出为占位符加以保护，再对剩余纯文本做强调符号替换，
    最后回填。否则 `a*b*c` 里的星号、URL 里的下划线都会被误转成
    <i>，产出错乱且可能非法的 HTML。
    """
    if not text:
        return text

    shelf: List[str] = []

    def _park(fragment: str) -> str:
        """把 fragment 存入保护区，返回不可能与 Markdown 冲突的占位符。"""
        shelf.append(fragment)
        return f'\x00{len(shelf) - 1}\x00'

    # 1) 行内代码：必须先处理，内容整体转义并保护，内部星号/下划线/HTML 标签不再参与解析
    #    如果后处理，代码中的 `<b>` 会被第 2 步误认为真实标签而保护，导致无法转义
    #    用 _escape_prose 而非 html_lib.escape：sendRichMessage 在发送前会对
    #    已转换 HTML 再跑一遍本转换器（_rich_message_html_payload 第 0 步），
    #    此时行内代码内容往往已含第一遍转义出的实体（&lt; 等），
    #    html.escape 会二次转义成 &amp;lt;（用户看到字面量 "&lt;"）。
    #    _escape_prose 对已有实体幂等，两遍转换结果一致。
    text = re.sub(
        r'`([^`]+)`',
        lambda m: _park(f'<code>{_escape_prose(m.group(1))}</code>'),
        text,
    )

    # 2) Markdown 自动链接与 spoiler：先于 HTML 标签保护。
    text = re.sub(
        r'<(https?://[^>]+)>',
        lambda m: _park(f'<a href="{_escape_attr(m.group(1))}">{html_lib.escape(m.group(1))}</a>'),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r'\|\|([^|\n]+)\|\|',
        lambda m: _park(f'<tg-spoiler>{html_lib.escape(m.group(1))}</tg-spoiler>'),
        text,
    )

    # 3) 既有 HTML 标签原样保留（支持 HTML/Markdown 混排）。
    #    要求真实标签形状（<字母/!/开头），避免把比较表达式
    #    （如 `a < b && c > d`）误认成标签。
    #    注意：此时行内代码已被保护，代码中的 `<b>` 已转义为 &lt;b&gt; 并存入保护区，
    #    不会被此规则再次匹配。
    text = re.sub(r'<[a-zA-Z!/][^>]*>', lambda m: _park(m.group(0)), text)

    # 4) 图片（须先于链接，否则 ![]() 的 [] 会被链接规则吃掉）
    text = re.sub(
        r'!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)',
        lambda m: _park(f'<img src="{_escape_attr(m.group(2))}"/>'),
        text,
    )

    # 5) 链接：href 与文本分别转义后整体保护，URL 中的 _ 不会变斜体
    text = re.sub(
        r'\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)',
        lambda m: _park(
            f'<a href="{_escape_attr(m.group(2))}">{html_lib.escape(m.group(1))}</a>'
        ),
        text,
    )

    # 6) 剩下的是纯文本：转义裸露的 < > &，避免 "a < b" 被当成标签。
    #    用 _escape_prose 而非 html.escape：模型按提示词输出的正文里
    #    已包含合法实体（&amp;、&lt;、&#39;），二次转义会让用户看到字面量。
    text = _escape_prose(text)

    # 7) 强调符号（此时已无代码/URL 干扰）
    text = re.sub(r'\*\*\*([^*]+)\*\*\*', r'<b><i>\1</i></b>', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<![\w\\])__([^_]+)__(?!\w)', r'<b>\1</b>', text)
    text = re.sub(r'~~([^~]+)~~', r'<s>\1</s>', text)
    text = re.sub(r'(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)', r'<i>\1</i>', text)
    # 下划线斜体只在词边界生效，snake_case 标识符不受影响
    text = re.sub(r'(?<![\w\\])_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)', r'<i>\1</i>', text)

    # 8) 回填保护片段。嵌套场景（如行内代码内部又包含已保护的标签）
    #    需要迭代回填，否则内层占位符会以原始 \x00 字节残留。上限防呆：
    #    正常输入嵌套不超过 2-3 层；循环次数耗尽仍有残留时保持现状返回。
    def _unpark(m: re.Match) -> str:
        return shelf[int(m.group(1))]

    for _ in range(8):
        if "\x00" not in text:
            break
        text = re.sub(r"\x00(\d+)\x00", _unpark, text)
    return text


def _escape_attr(url: str) -> str:
    """转义要写入 href/src 属性的 URL。"""
    return html_lib.escape(url, quote=True)


def render_telegram_fragment(text: str) -> str:
    """统一入口（片段级）：Markdown 转换 + HTML 转义 + tg-button 校验。

    用于「转义结果会被嵌入调用方手写好的 <p>/<b>/<td>/<code> 骨架」的
    场景——例如 ``f"<b>轮次</b>：{render_telegram_fragment(x)}"``。
    不做块级包裹（不调用 ``wrap_mixed_content_as_blocks``），因为给
    短字段外面多包一层 ``<p>`` 会破坏调用方已有的标签结构。

    等价于目前散落在各模块的 ``convert_markdown_to_telegram_html`` 直接
    调用；新代码请统一使用本函数，不要再直接 import
    ``convert_markdown_to_telegram_html``。

    处理顺序（内部已保证，调用方无需关心）：
    1. Markdown → Telegram HTML 转换
    2. <tg-button> 强模式校验（非法按钮降级为字面量文本）
    """
    return convert_markdown_to_telegram_html(text)


def render_telegram_block(text: str) -> str:
    """统一入口（块级）：Markdown 转换 + tg-button 校验 + 块级结构修复。

    用于「整段自由文本要独立渲染成一块 Telegram Rich Message HTML」的
    场景——例如思考内容、用户可读的独立说明段落。在
    ``render_telegram_fragment`` 的基础上再做块级包裹修复，避免
    「文字 + Markdown 列表」混排被整体塞进单个 ``<p>`` 产出非法嵌套
    （``<p>…<ul>…</ul>…</p>``），从而被 Telegram 以结构类 400 拒收。

    处理顺序：
    1. Markdown → Telegram HTML 转换
    2. <tg-button> 强模式校验
    3. 按块级标签切段，纯文本段落分别包 <p>
    """
    return wrap_mixed_content_as_blocks(convert_markdown_to_telegram_html(text))


def wrap_mixed_content_as_blocks(converted: str) -> str:
    """把 Markdown 转换产物整理为 Telegram Rich Message 合法的块级序列。

    ``convert_markdown_to_telegram_html`` 的产物有三种形态：

    1. 纯行内文本（无块级标签）——包一个 ``<p>``（换行转 ``<br/>``）；
    2. 块级标签与文字混排（如"说明文字 + Markdown 列表"被转换成
       ``<ul>``）——绝不能整体包进单个 ``<p>``，否则产出
       ``<p>…<ul>…</ul>…</p>`` 非法嵌套。Telegram Rich Message
       解析器结构校验严格，会以 400（rich_message 结构类错误）拒绝
       整条消息，触发 plain-text fallback 后用户看到的是整条退化为
       无格式纯文本。这里按块级标签切段：块级段原样保留，纯文本段
       分别包 ``<p>``。
    3. 以块级标签开头但尾部带文字——同样按段切开，避免顶部块之后
       残留裸文本节点。

    本函数假定输入已经过 HTML 转义或为转换器产物，不再做转义。
    """
    stripped = (converted or "").strip()
    if not stripped:
        return ""
    segments: List[str] = []
    for part in _RICH_BLOCK_SPLIT_RE.split(converted):
        if not part or not part.strip():
            continue
        if part.lstrip().startswith(_BLOCK_START_PREFIXES):
            segments.append(part.strip())
        else:
            segments.append(f"<p>{part.strip().replace(chr(10), '<br/>')}</p>")
    if not segments:
        # 正常不会走到（空串已在上面返回）；防御性回退为单段落。
        return f"<p>{stripped.replace(chr(10), '<br/>')}</p>"
    return ''.join(segments)
