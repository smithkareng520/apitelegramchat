"""Markdown 到 Telegram Rich Message HTML 的转换层。

用于兜底处理模型输出的 Markdown 语法，确保即使不依赖提示词约束，
也能正确渲染为 Telegram 支持的 HTML 标签。
"""
import re
import html as html_lib
from typing import List, Tuple


def convert_markdown_to_telegram_html(text: str) -> str:
    """将 Markdown 语法转换为 Telegram Rich Message HTML。
    
    采用智能逐块转换策略：
    - 已经是完整 HTML 块的部分保持原样
    - 检测到 Markdown 语法的部分进行转换
    - 支持 HTML 和 Markdown 混合的内容
    
    Args:
        text: 可能包含 Markdown、HTML 或两者混合的文本
        
    Returns:
        转换后的 Telegram HTML
    """
    if not text or not text.strip():
        return text
    
    # 如果完全不包含 Markdown 语法，直接返回
    if not _contains_markdown(text):
        return text
    
    # 执行智能转换
    return _convert(text)


def _is_already_html(text: str) -> bool:
    """检测文本是否已经包含 HTML 标签。"""
    # Telegram 特有标签
    telegram_tags = [
        r'<tg-spoiler>', r'<tg-math>', r'<tg-math-block>', 
        r'<tg-slideshow>', r'<tg-map>', r'<tg-reference>',
    ]
    for tag in telegram_tags:
        if tag in text:
            return True
    
    # 常见的 HTML 块级标签（带属性或自闭合）
    html_patterns = [
        r'<(h[1-6]|p|div|pre|blockquote|details|ul|ol|table|figure|aside|footer)\b[^>]*>',
        r'<(img|video|audio|hr)\b[^>]*/?>'
    ]
    for pattern in html_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    
    return False


def _contains_markdown(text: str) -> bool:
    """检测文本是否包含 Markdown 语法。"""
    markdown_patterns = [
        r'^#{1,6}\s+',  # 标题
        r'\*\*[^*]+\*\*',  # 粗体
        r'__[^_]+__',  # 粗体
        r'\*[^*]+\*',  # 斜体
        r'_[^_]+_',  # 斜体
        r'~~[^~]+~~',  # 删除线
        r'`[^`]+`',  # 行内代码
        r'```[\s\S]*?```',  # 代码块
        r'!\[.*?\]\(.*?\)',  # 图片
        r'\[.*?\]\(.*?\)',  # 链接
        r'^\s*[-*+]\s+',  # 无序列表
        r'^\s*\d+\.\s+',  # 有序列表
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
        
        # 代码块（需要先处理，避免内部被转义）
        if line.strip().startswith('```'):
            code_block, lines_consumed = _extract_code_block(lines[i:])
            result.append(code_block)
            i += lines_consumed
            continue
        
        # 表格（需要整体处理多行）
        if _is_table_row(line):
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
        if re.match(r'^\s*[-*_]{3,}\s*$', line):
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
        if re.match(r'^\s*\d+\.\s+', line):
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
    lang_match = re.match(r'^```(\w+)?', first_line)
    lang = lang_match.group(1) if lang_match and lang_match.group(1) else ''
    
    code_lines = []
    i = 1
    while i < len(lines):
        if lines[i].strip() == '```':
            break
        code_lines.append(lines[i])
        i += 1
    
    # 转义代码内容
    code_content = '\n'.join(code_lines)
    escaped_code = html_lib.escape(code_content)
    
    if lang:
        html = f'<pre><code class="language-{html_lib.escape(lang)}">{escaped_code}</code></pre>'
    else:
        html = f'<pre><code>{escaped_code}</code></pre>'
    
    return html, i + 1  # +1 for closing ```


def _is_table_row(line: str) -> bool:
    """检测是否为表格行。"""
    stripped = line.strip()
    return stripped.startswith('|') and stripped.endswith('|') and stripped.count('|') >= 2


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
            content = _convert_inline(match.group(2))
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
        match = re.match(r'^\s*\d+\.\s+(.+)$', line)
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

    # 1) 既有 HTML 标签原样保留（支持 HTML/Markdown 混排）
    text = re.sub(r'<[^>]+>', lambda m: _park(m.group(0)), text)

    # 2) 行内代码：内容整体转义并保护，内部星号/下划线不再参与解析
    text = re.sub(
        r'`([^`]+)`',
        lambda m: _park(f'<code>{html_lib.escape(m.group(1))}</code>'),
        text,
    )

    # 3) 图片（须先于链接，否则 ![]() 的 [] 会被链接规则吃掉）
    text = re.sub(
        r'!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)',
        lambda m: _park(f'<img src="{_escape_attr(m.group(2))}"/>'),
        text,
    )

    # 4) 链接：href 与文本分别转义后整体保护，URL 中的 _ 不会变斜体
    text = re.sub(
        r'\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)',
        lambda m: _park(
            f'<a href="{_escape_attr(m.group(2))}">{html_lib.escape(m.group(1))}</a>'
        ),
        text,
    )

    # 5) 剩下的是纯文本：转义裸露的 < > &，避免 "a < b" 被当成标签
    text = html_lib.escape(text, quote=False)

    # 6) 强调符号（此时已无代码/URL 干扰）
    text = re.sub(r'\*\*\*([^*]+)\*\*\*', r'<b><i>\1</i></b>', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<![\w\\])__([^_]+)__(?!\w)', r'<b>\1</b>', text)
    text = re.sub(r'~~([^~]+)~~', r'<s>\1</s>', text)
    text = re.sub(r'(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)', r'<i>\1</i>', text)
    # 下划线斜体只在词边界生效，snake_case 标识符不受影响
    text = re.sub(r'(?<![\w\\])_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)', r'<i>\1</i>', text)

    # 7) 回填保护片段
    def _unpark(m: re.Match) -> str:
        return shelf[int(m.group(1))]

    return re.sub(r'\x00(\d+)\x00', _unpark, text)


def _escape_attr(url: str) -> str:
    """转义要写入 href/src 属性的 URL。"""
    return html_lib.escape(url, quote=True)
