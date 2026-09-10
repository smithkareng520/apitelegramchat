---
name: pdf
description: Use this skill whenever the user wants to do anything with PDF files. This includes reading or extracting text/tables from PDFs, combining or merging multiple PDFs into one, splitting PDFs apart, rotating pages, adding watermarks, creating new PDFs, filling PDF forms, encrypting/decrypting PDFs, extracting images, and OCR on scanned PDFs to make them searchable. If the user mentions a .pdf file or asks to produce one, use this skill.
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


## Chinese / CJK font requirements (production)

The production Docker image installs **two different CJK font resources for two different renderers**. Treat both as image/runtime dependencies; do not install or download fonts during a normal user request.

### ReportLab: embedded Kaiti-style CJK font

ReportLab `TTFont` can embed TrueType fonts and TrueType collections (TTC). The production PDF path uses the first face of Debian's **AR PL UKai** collection, which is a Kaiti-style Unicode font. The CN face covers Hiragana and Katakana and has broad Han coverage, making it a better fit for mixed Chinese/Japanese text than the old Arphic Song fallback.

- Font file: `/usr/share/fonts/truetype/arphic/ukai.ttc`
- TTC subfont: `0` (AR PL UKai CN)
- Recommended registered name: `CJKKai`
- Package: `fonts-arphic-ukai`

Register it before drawing or laying out CJK text:

```python
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

font_path = "/usr/share/fonts/truetype/arphic/ukai.ttc"
pdfmetrics.registerFont(TTFont("CJKKai", font_path, subfontIndex=0))
```

Then use `fontName="CJKKai"` in Platypus styles/tables or `canvas.setFont("CJKKai", size)` for canvas text. This embeds the selected Kaiti face into the generated PDF. The bundled `scripts/cjk_font.py` helper already applies the configured TTC subfont index.

### LibreOffice / DOCX: Noto Sans CJK SC

For DOCX generation and server-side LibreOffice rendering, use **Noto Sans CJK SC**. The production image provides it through `fonts-noto-cjk`:

- Fontconfig family: `Noto Sans CJK SC`
- Font collection: `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`

Do not try to feed this TTC file to ReportLab `TTFont`; it uses CFF outlines.

### General rules

- Chinese/CJK text must never use ReportLab's built-in `Helvetica`, `Times-Roman`, or `Courier`.
- Do not use `Arial` as a server-side assumption; it is not guaranteed to exist in the Linux image.
- When a PDF contains Chinese/Japanese text, choose an actual CJK font and verify the output by rendering pages to images.
- If the required CJK font is missing, fail clearly instead of silently falling back to a non-CJK font.
- Emoji require the dedicated `EmojiMono` fallback font and the `emoji_font.py` helpers (see the "Emoji handling" section above). Never feed emoji to ReportLab with only the CJK font registered.
- OCR of simplified/traditional Chinese is supported by the preinstalled `chi_sim` and `chi_tra` Tesseract language data.

### Recommended helper

For scripts, prefer the bundled `scripts/cjk_font.py` helper so font paths and ReportLab registration stay consistent with the image.

## Emoji handling (built-in, low-friction)

ReportLab does not perform font fallback. The PDF skill therefore registers a
TrueType CJK font plus a monochrome Noto Emoji font and provides a small fallback
layer. The important design goal is that normal PDF code should not have to
manually build `<font>` markup.

### Preferred setup

Call the shared registration helper once during PDF setup:

```python
from cjk_font import register_fonts

register_fonts()  # idempotent: safe to call from shared setup code
```

For Platypus paragraphs, use `safe_paragraph()` and pass the same
`ParagraphStyle` you would normally pass to `Paragraph`. The helper resolves the
style's `fontName`, XML-escapes the text, and inserts emoji fallback markup when
needed:

```python
from reportlab.lib.styles import getSampleStyleSheet
from emoji_font import safe_paragraph

styles = getSampleStyleSheet()
styles["BodyText"].fontName = "CJKKai"
story.append(safe_paragraph("项目进度：✅ 已完成 80% 🚀", styles["BodyText"]))
```

Call `register_fonts()` during PDF setup before creating paragraphs. This is the recommended API for new code. Existing code can keep using
`to_fallback_markup()`, but it now accepts either a font name string or a
`ParagraphStyle`, so this common pattern is valid:

```python
markup = to_fallback_markup("你好 🎯", styles["BodyText"])
story.append(Paragraph(markup, styles["BodyText"]))
```

### Canvas text

For low-level canvas drawing, continue to use `draw_mixed_string()` and
`string_width_mixed()` instead of `canvas.drawString()` for mixed CJK/emoji text:

```python
from emoji_font import draw_mixed_string, string_width_mixed

text = "验收结果：✅ 通过 3 项 ❌ 未通过 1 项"
width = string_width_mixed(text, 12, "CJKKai")
draw_mixed_string(c, (letter[0] - width) / 2, 700, text, 12, "CJKKai")
```

Both helpers also accept a ReportLab `ParagraphStyle` in place of the base
font name.

### What the fallback layer does

- It examines the actual glyph coverage of the registered fonts rather than
  assuming that a Unicode block is fully supported.
- It keeps Unicode grapheme clusters together where possible. This avoids
  splitting common ZWJ emoji, variation selectors, skin-tone sequences, and
  flag sequences into visually broken pieces.
- Emoji-preferred characters use the monochrome Noto Emoji font when available;
  normal CJK, Latin, arrows, and math symbols stay in the base font.
- Characters unsupported by both fonts are dropped by default instead of
  emitting a PDF notdef box. Pass `missing_report=[]` when content fidelity is
  important so the caller can log exactly what was omitted.
- Color emoji are intentionally not embedded through ReportLab. The ReportLab
  path uses monochrome TrueType outlines; the LibreOffice/DOCX path can use the
  system color emoji font separately.

### Migration from older code

Old, verbose pattern:

```python
markup = to_fallback_markup(text, "CJKKai")
story.append(Paragraph(markup, styles["BodyText"]))
```

Preferred pattern:

```python
story.append(safe_paragraph(text, styles["BodyText"]))
```

Canvas code can stay on `draw_mixed_string()` / `string_width_mixed()`. There is
no longer a need to manually inspect each string for emoji or manually wrap
emoji runs.

### Font/runtime requirements

The production image installs:

- ReportLab CJK: `/usr/share/fonts/truetype/arphic/ukai.ttc`, subfont `0`
- LibreOffice/DOCX CJK: `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`
- ReportLab emoji: `/usr/share/fonts/truetype/noto-emoji-mono/NotoEmoji-Regular.ttf`

The emoji TTF is downloaded and checksum-verified during Docker image build and
is not committed to the repository. Override it for development with
`APITELEGRAMCHAT_REPORTLAB_EMOJI_FONT`. The runtime checker
`python3 .claude/skills/pdf/scripts/check_cjk_runtime.py` validates both CJK and
emoji resources.

## Command-Line Tools

### pdftotext (poppler-utils)
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

### Extract Text from Scanned PDFs
```python
# Requires: pip install pytesseract pdf2image
import pytesseract
from pdf2image import convert_from_path

# Convert PDF to images
images = convert_from_path('scanned.pdf')

# OCR each page
text = ""
for i, image in enumerate(images):
    text += f"Page {i+1}:\n"
    text += pytesseract.image_to_string(image)
    text += "\n\n"

print(text)
```

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
| OCR scanned PDFs | pytesseract | Convert to image first |
| Fill PDF forms | pdf-lib or pypdf (see FORMS.md) | See FORMS.md |

## Next Steps

- For advanced pypdfium2 usage, see REFERENCE.md
- For JavaScript libraries (pdf-lib), see REFERENCE.md
- If you need to fill out a PDF form, follow the instructions in FORMS.md
- For troubleshooting guides, see REFERENCE.md
