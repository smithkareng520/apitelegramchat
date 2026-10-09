---
name: pdf
description: Use this skill whenever the user wants to do anything with PDF files. This includes reading or extracting text/tables from PDFs, combining or merging multiple PDFs into one, splitting PDFs apart, rotating pages, adding watermarks, creating new PDFs, filling PDF forms, encrypting/decrypting PDFs, extracting images. OCR is not available in this environment. If the user mentions a .pdf file or asks to produce one, use this skill.
license: Proprietary. LICENSE.txt has complete terms
---

# PDF Processing Guide

## Overview

This guide covers essential PDF processing operations using Python libraries and command-line tools. For advanced features, JavaScript libraries, and detailed examples, see REFERENCE.md. If you need to fill out a PDF form, read FORMS.md and follow its instructions.

## Quick Start

```python
from pypdf import PdfReader, PdfWriter

# Read a PDF
reader = PdfReader("document.pdf")
print(f"Pages: {len(reader.pages)}")

# Extract text
text = ""
for page in reader.pages:
    text += page.extract_text()
```

## Python Libraries

### pypdf - Basic Operations

#### Merge PDFs
```python
from pypdf import PdfWriter, PdfReader

writer = PdfWriter()
for pdf_file in ["doc1.pdf", "doc2.pdf", "doc3.pdf"]:
    reader = PdfReader(pdf_file)
    for page in reader.pages:
        writer.add_page(page)

with open("merged.pdf", "wb") as output:
    writer.write(output)
```

#### Split PDF
```python
reader = PdfReader("input.pdf")
for i, page in enumerate(reader.pages):
    writer = PdfWriter()
    writer.add_page(page)
    with open(f"page_{i+1}.pdf", "wb") as output:
        writer.write(output)
```

#### Extract Metadata
```python
reader = PdfReader("document.pdf")
meta = reader.metadata
print(f"Title: {meta.title}")
print(f"Author: {meta.author}")
print(f"Subject: {meta.subject}")
print(f"Creator: {meta.creator}")
```

#### Rotate Pages
```python
reader = PdfReader("input.pdf")
writer = PdfWriter()

page = reader.pages[0]
page.rotate(90)  # Rotate 90 degrees clockwise
writer.add_page(page)

with open("rotated.pdf", "wb") as output:
    writer.write(output)
```

### pdfplumber - Text and Table Extraction

#### Extract Text with Layout
```python
import pdfplumber

with pdfplumber.open("document.pdf") as pdf:
    for page in pdf.pages:
        text = page.extract_text()
        print(text)
```

#### Extract Tables
```python
with pdfplumber.open("document.pdf") as pdf:
    for i, page in enumerate(pdf.pages):
        tables = page.extract_tables()
        for j, table in enumerate(tables):
            print(f"Table {j+1} on page {i+1}:")
            for row in table:
                print(row)
```

#### Advanced Table Extraction
```python
import pandas as pd

with pdfplumber.open("document.pdf") as pdf:
    all_tables = []
    for page in pdf.pages:
        tables = page.extract_tables()
        for table in tables:
            if table:  # Check if table is not empty
                df = pd.DataFrame(table[1:], columns=table[0])
                all_tables.append(df)

# Combine all tables
if all_tables:
    combined_df = pd.concat(all_tables, ignore_index=True)
    combined_df.to_excel("extracted_tables.xlsx", index=False)
```

### reportlab - Create PDFs

#### Basic PDF Creation
```python
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

c = canvas.Canvas("hello.pdf", pagesize=letter)
width, height = letter

# Add text
c.drawString(100, height - 100, "Hello World!")
c.drawString(100, height - 120, "This is a PDF created with reportlab")

# Add a line
c.line(100, height - 140, 400, height - 140)

# Save
c.save()
```

#### Create PDF with Multiple Pages
```python
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet

doc = SimpleDocTemplate("report.pdf", pagesize=letter)
styles = getSampleStyleSheet()
story = []

# Add content
title = Paragraph("Report Title", styles['Title'])
story.append(title)
story.append(Spacer(1, 12))

body = Paragraph("This is the body of the report. " * 20, styles['Normal'])
story.append(body)
story.append(PageBreak())

# Page 2
story.append(Paragraph("Page 2", styles['Heading1']))
story.append(Paragraph("Content for page 2", styles['Normal']))

# Build PDF
doc.build(story)
```

#### Subscripts and Superscripts

**IMPORTANT**: Never use Unicode subscript/superscript characters (₀₁₂₃₄₅₆₇₈₉, ⁰¹²³⁴⁵⁶⁷⁸⁹) in ReportLab PDFs. The built-in fonts do not include these glyphs, causing them to render as solid black boxes.

Instead, use ReportLab's XML markup tags in Paragraph objects:
```python
from reportlab.platypus import Paragraph
from reportlab.lib.styles import getSampleStyleSheet

styles = getSampleStyleSheet()

# Subscripts: use <sub> tag
chemical = Paragraph("H<sub>2</sub>O", styles['Normal'])

# Superscripts: use <super> tag
squared = Paragraph("x<super>2</super> + y<super>2</super>", styles['Normal'])
```

For canvas-drawn text (not Paragraph objects), manually adjust font the size and position rather than using Unicode subscripts/superscripts.


## Chinese / CJK text (no font files in the image)

The Docker image is slim: it installs **no font files and no OCR**. Do not download or install fonts during a user request.

ReportLab can still write Chinese/Japanese/Korean PDFs because it ships the Adobe CID fonts as *references*: the PDF stores character codes and the **reader's** viewer draws them with a CJK font installed on the reader's device. Nothing is embedded, so files stay small.

| Language | Built-in font | `lang` |
|---|---|---|
| Simplified Chinese | `STSong-Light` | `zh` (default) |
| Traditional Chinese | `MSung-Light` | `zh-tw` |
| Japanese | `HeiseiMin-W3` | `ja` |
| Korean | `HYSMyeongJo-Medium` | `ko` |

### Rules

- Always call `register_fonts()` from `scripts/cjk_font.py` and use the **returned name** as `fontName` (e.g. in `ParagraphStyle`, `TableStyle ('FONTNAME', ...)`, `canvas.setFont`). It also makes `<b>`/`<i>` keep the CJK font.
- Never leave Chinese text on ReportLab's default styles (`Normal`, `Title`, `Heading1`, ...): they use Helvetica and turn Chinese into black boxes.
- For PDFs with no CJK text, plain `Helvetica` is fine and gives nicer Latin glyphs.
- Emoji cannot be drawn (no emoji font). Pass any text that may contain emoji through `scripts/emoji_font.py`: common ones become plain symbols (`✅`→`√`, `❌`→`×`, `⚠️`→`(!)`), the rest are dropped and listed in `missing_report`. Tell the user if something was dropped.
- Glyph shapes depend on the viewer. The PDF is not self-contained: if the user needs a fully embedded font, they must provide a TrueType font file and set `APITELEGRAMCHAT_REPORTLAB_CJK_FONT` (plus `APITELEGRAMCHAT_REPORTLAB_CJK_SUBFONT_INDEX` for `.ttc`), which `register_fonts()` then embeds.

### Verifying output in this image

There is no CJK system font and no `poppler-data` here, so **page images and `pdftotext` cannot show Chinese from these PDFs** (blank glyphs or "Missing language pack" warnings). That is expected, not a bug in your PDF. Verify with Python instead:

```python
from pypdf import PdfReader
print(PdfReader("report.pdf").pages[0].extract_text())   # Chinese text should read back correctly
```

Use page images only to check layout (margins, table widths, page count).

### Paragraph example

```python
import sys
sys.path.insert(0, "/app/.claude/skills/pdf/scripts")

from reportlab.platypus import SimpleDocTemplate, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from cjk_font import register_fonts
from emoji_font import safe_paragraph

cjk = register_fonts()                      # -> "STSong-Light"
normal = ParagraphStyle("NormalCN", parent=getSampleStyleSheet()["Normal"],
                        fontName=cjk, fontSize=11, leading=18)

missing = []                                # emoji that had to be dropped
story = [
    safe_paragraph("项目进度：✅ 已完成 80% 🚀 预计下周交付", normal, missing_report=missing),
    Spacer(1, 12),
    # Paragraph tags are allowed only with allow_markup=True (escape &, <, > yourself)
    safe_paragraph("<b>重点</b>：按时验收", normal, allow_markup=True),
]
SimpleDocTemplate("report.pdf").build(story)
print("dropped:", missing)                  # e.g. ['🚀']
```

### Canvas example

```python
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from cjk_font import register_fonts
from emoji_font import draw_mixed_string, string_width_mixed

cjk = register_fonts()
c = canvas.Canvas("out.pdf", pagesize=letter)
text = "验收结果：✔ 通过 3 项 ❌ 未通过 1 项"
width = string_width_mixed(text, 12, cjk)
draw_mixed_string(c, (letter[0] - width) / 2, 700, text, 12, cjk)
c.save()
```

### Tables

```python
from reportlab.platypus import Table, TableStyle
table = Table([["姓名", "张三"], ["年龄", "25"]])
table.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), cjk)]))   # cells need the CJK font too
```

Run `python3 scripts/check_cjk_runtime.py` for a font-free smoke test of this setup.

## Command-Line Tools

### pdftotext (poppler-utils)
Works for Latin text. For PDFs with CJK text use `pypdf`/`pdfplumber` instead (the image has no `poppler-data`).
```bash
# Extract text
pdftotext input.pdf output.txt

# Extract text preserving layout
pdftotext -layout input.pdf output.txt

# Extract specific pages
pdftotext -f 1 -l 5 input.pdf output.txt  # Pages 1-5
```

### qpdf
```bash
# Merge PDFs
qpdf --empty --pages file1.pdf file2.pdf -- merged.pdf

# Split pages
qpdf input.pdf --pages . 1-5 -- pages1-5.pdf
qpdf input.pdf --pages . 6-10 -- pages6-10.pdf

# Rotate pages
qpdf input.pdf output.pdf --rotate=+90:1  # Rotate page 1 by 90 degrees

# Remove password
qpdf --password=mypassword --decrypt encrypted.pdf decrypted.pdf
```

### pdftk (if available)
```bash
# Merge
pdftk file1.pdf file2.pdf cat output merged.pdf

# Split
pdftk input.pdf burst

# Rotate
pdftk input.pdf rotate 1east output rotated.pdf
```

## Common Tasks

### Add Watermark
```python
from pypdf import PdfReader, PdfWriter

# Create watermark (or load existing)
watermark = PdfReader("watermark.pdf").pages[0]

# Apply to all pages
reader = PdfReader("document.pdf")
writer = PdfWriter()

for page in reader.pages:
    page.merge_page(watermark)
    writer.add_page(page)

with open("watermarked.pdf", "wb") as output:
    writer.write(output)
```

### Extract Images
```bash
# Using pdfimages (poppler-utils)
pdfimages -j input.pdf output_prefix

# This extracts all images as output_prefix-000.jpg, output_prefix-001.jpg, etc.
```

### Password Protection
```python
from pypdf import PdfReader, PdfWriter

reader = PdfReader("input.pdf")
writer = PdfWriter()

for page in reader.pages:
    writer.add_page(page)

# Add password
writer.encrypt("userpassword", "ownerpassword")

with open("encrypted.pdf", "wb") as output:
    writer.write(output)
```

## Quick Reference

| Task | Best Tool | Command/Code |
|------|-----------|--------------|
| Merge PDFs | pypdf | `writer.add_page(page)` |
| Split PDFs | pypdf | One page per file |
| Extract text | pdfplumber | `page.extract_text()` |
| Extract tables | pdfplumber | `page.extract_tables()` |
| Create PDFs | reportlab | Canvas or Platypus |
| Command line merge | qpdf | `qpdf --empty --pages ...` |
| Fill PDF forms | pdf-lib or pypdf (see FORMS.md) | See FORMS.md |

## Next Steps

- For advanced pypdfium2 usage, see REFERENCE.md
- For JavaScript libraries (pdf-lib), see REFERENCE.md
- If you need to fill out a PDF form, follow the instructions in FORMS.md
- For troubleshooting guides, see REFERENCE.md
