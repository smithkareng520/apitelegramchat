---
name: docx
description: Create, edit, inspect, and deliver Word documents (.docx) with deterministic styles, CJK-safe text (no server fonts needed), images/tables, and structural validation plus layout-only render checks. Prefer the simplest native docx-js workflow for new documents and OOXML only when the high-level API cannot express the required feature.
license: Proprietary. LICENSE.txt has complete terms
---

# DOCX skill

Use this skill for `.docx` creation, editing, review, redlines/comments, templates, forms, or DOCX↔PDF workflows.

## Slim-image constraints (read first)

This image installs **no fonts** and only LibreOffice Writer. Consequences:

- A DOCX only *names* its fonts; the reader's Word supplies the glyphs. Chinese and emoji therefore display correctly on the user's device even though this image has no CJK/emoji font. Never try to install or embed fonts.
- LibreOffice renders here (QA PNGs, DOCX→PDF) show Chinese and emoji as blanks or boxes. That is expected, not a document bug. Use renders only to check **layout**; verify text content by reading the XML or text, not the PNGs.
- A DOCX→PDF conversion made here has the same blank-glyph problem for CJK/emoji. If the user needs a PDF with Chinese, build it with the pdf skill (ReportLab CID fonts) instead of converting this DOCX.

## The default workflow

**Create/edit → validate → (layout render only when it matters) → deliver.**

Validation (`validate.py`) is mandatory. A render is worth doing when the document has tables, images, headers/footers, columns or tight page limits; skip it for plain text documents. Keep renders small (`--pages 2`, default 80 dpi) and keep them out of the deliverable.

### New document

Use `docx` (docx-js) unless the task clearly benefits from editing an existing OOXML structure.

```bash
npm install
node build-docx.js
python scripts/office/validate.py output.docx
python scripts/render_docx.py output.docx --output_dir .qa/docx --pages 2   # optional, layout only
```

### Existing document

Prefer high-level editing for simple text/style changes. Use OOXML for tracked changes, comments, fields, complex numbering, or features the high-level library cannot preserve safely.

```bash
python scripts/office/unpack.py input.docx unpacked/
# edit with the smallest possible change
python scripts/office/pack.py unpacked/ output.docx
python scripts/office/validate.py output.docx
```

## Zero-dependency fallback (stdlib only)

When the environment has **no `docx-js`/`python-docx` and no `matplotlib`** (offline / minimal /
forbidden installs), use the two pure-stdlib helpers in `scripts/`. They need **only the Python 3
standard library** (`zipfile`, `zlib`, `struct`, `math`) and are safe to reuse across runs.

- **`scripts/minichart.py`** — draws `bar / hbar / line / donut` charts into a pixel buffer and writes
  a real PNG (zlib) using a built-in 5x7 bitmap font. **English labels only** (no CJK in the PNG):
  ```bash
  python3 scripts/minichart.py bar out.png --labels Q1,Q2,Q3,Q4 --values 87,66,87,92 \
    --title "KFT Quarterly" --ylabel KRW
  python3 scripts/minichart.py donut out.png --values Mobile:1740:#3B82C4,#PC:1184:#4FB3A9 \
    --title "KFT Platforms"
  ```
  ```python
  import minichart as mc
  mc.bar_chart("out.png", ["A", "B"], [1, 2], title="T", ylabel="V")
  ```

- **`scripts/minidocx.py`** — builds a valid WordprocessingML `.docx` by hand (no third-party),
  embedding PNG images, tables, bullets, headings and page breaks. CJK-safe: the default East-Asian
  font is set, so Chinese in the **body** still renders on the reader's device:
  ```python
  import minidocx
  d = minidocx.Docx(title="Report")
  d.add_heading("Section", 1)
  d.add_para([("normal ", {}), ("bold", {"bold": True}), (" rest", {})])
  d.add_bullet("point one")
  d.add_table(["A", "B"], [["1", "2"]], widths_pt=[60, 120])
  d.add_image("chart.png", width_pt=452, caption="Fig 1")
  d.add_page_break()
  d.save("out.docx")
  ```

Combined 0-dependency pipeline (charts + doc), then validate as usual:
```bash
python3 scripts/minichart.py bar chart.png --labels X,Y --values 1,2 --title "T"
python3 - <<'PY'
import sys; sys.path.insert(0, "scripts")
import minidocx
d = minidocx.Docx()
d.add_heading("Report", 1); d.add_para("Body text.")
d.add_image("chart.png", width_pt=452, caption="Figure 1")
d.add_table(["k", "v"], [["a", "b"]])
d.save("report.docx")
PY
python3 scripts/office/validate.py report.docx
```

Notes:
- Keep document body in Chinese via `minidocx` (renders on the user's device); keep `minichart` PNG
  labels in English (no CJK font is installed on this image).
- `minidocx` auto-sizes images from the PNG header; for non-PNG, pass `height_pt` explicitly.
- Prefer the default `docx-js` + `matplotlib` workflow when available; use this fallback when they are
  not installed or not allowed.

## Text, fonts, and Unicode

### Chinese / CJK

Set the font as an object so Latin and East Asian text each get a sensible font, and the reader's Word picks the right glyphs. Do not name a server font (there is none).

```javascript
const FONT = { ascii: "Arial", hAnsi: "Arial", cs: "Arial", eastAsia: "Microsoft YaHei" };
// Word falls back automatically if a font is missing (PingFang SC on macOS, Noto/Droid on Android).
```

Use `eastAsia: "SimSun"` for a formal Song-style look. Put `FONT` on the default document style and on every heading style (see Styles below). Do not set `font: "Noto Sans CJK SC"`: it exists only in images that install it.

### Emoji and uncommon Unicode

Pass the original Unicode string straight to `TextRun`; do not split ZWJ sequences, skin-tone modifiers, variation selectors or flags into separate runs, and do not strip emoji. Word/Office/phones draw them with their own emoji font. Emoji will not show in renders made inside this image; that is expected.

## Page size and margins

Always set page size explicitly. docx-js uses DXA units (1440 = 1 inch).

```javascript
page: {
  size: { width: 11906, height: 16838 }, // A4
  margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 }
}
```

For US Letter use `12240 × 15840`.

For landscape, pass the portrait dimensions and set `orientation: PageOrientation.LANDSCAPE`; docx-js handles the swap.

## Styles

Override built-in heading IDs so Word/LibreOffice/TOC see the same semantic hierarchy. `FONT` is the object defined in the CJK section above.

```javascript
const doc = new Document({
  styles: {
    default: {
      document: { run: { font: FONT, size: 24 } }
    },
    paragraphStyles: [
      {
        id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal",
        quickFormat: true, run: { font: FONT, size: 32, bold: true },
        paragraph: { spacing: { before: 240, after: 240 }, outlineLevel: 0 }
      },
      {
        id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal",
        quickFormat: true, run: { font: FONT, size: 28, bold: true },
        paragraph: { spacing: { before: 180, after: 180 }, outlineLevel: 1 }
      }
    ]
  }
});
```

Keep title/heading colors restrained and readable. Use semantic headings instead of manually formatted bold paragraphs when navigation or a TOC matters.

## Lists

Never hand-type bullet glyphs into content. Use Word numbering definitions.

```javascript
numbering: {
  config: [
    {
      reference: "bullets",
      levels: [{
        level: 0,
        format: LevelFormat.BULLET,
        text: "•",
        alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 720, hanging: 360 } } }
      }]
    },
    {
      reference: "numbers",
      levels: [{
        level: 0,
        format: LevelFormat.DECIMAL,
        text: "%1.",
        alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 720, hanging: 360 } } }
      }]
    }
  ]
}
```

The same `reference` continues a list. A new `reference` starts a separate list sequence.

## Tables

For stable cross-renderer tables, specify all three dimensions:

1. table width,
2. `columnWidths`,
3. matching cell `width`.

Use `WidthType.DXA`, not percentages.

```javascript
new Table({
  width: { size: 9360, type: WidthType.DXA },
  columnWidths: [4680, 4680],
  rows: [
    new TableRow({ children: [
      new TableCell({
        width: { size: 4680, type: WidthType.DXA },
        margins: { top: 80, bottom: 80, left: 120, right: 120 },
        shading: { fill: "D5E8F0", type: ShadingType.CLEAR },
        children: [new Paragraph({ children: [new TextRun("Cell")] })]
      }),
      // ...
    ] })
  ]
})
```

Avoid overly narrow columns. Long URLs, CJK text without spaces, and code strings are the usual causes of ugly wrapping.

## Images

Always provide `type`, explicit dimensions, and descriptive alt text.

```javascript
new ImageRun({
  type: "png",
  data: fs.readFileSync("image.png"),
  transformation: { width: 400, height: 300 },
  altText: { title: "Chart", description: "Sales by month", name: "sales-chart" }
})
```

Prefer stable inline placement for normal business documents. Use floating/anchored objects only when layout requirements justify the extra complexity.

## Page breaks and links

A `PageBreak` belongs inside a `Paragraph`. For a section that must start on a new page, `pageBreakBefore` is usually simpler.

Use `ExternalHyperlink` for URLs and `InternalHyperlink` + bookmarks for in-document navigation. Avoid writing raw URL text when a meaningful link label is available.

## Validation and QA helpers

`scripts/office/validate.py` checks the OOXML structure. `scripts/render_docx.py` renders pages for a layout check:

```bash
python scripts/render_docx.py report.docx --output_dir .qa/report --pages 2
```

Look at the generated `page-*.png` for:

- clipped or overlapping text, and tables that overflow the page
- tables continuing correctly across pages, headings staying with their content
- images stretched or shifted; header/footer and page-number alignment
- tracked changes/comments behaving as intended (comments may need XML-level checks)

Ignore missing or boxed Chinese/emoji glyphs in these images (no fonts in this image). If a layout check fails, fix the source and render again.

## Specialized operations

The existing scripts are the preferred building blocks:

- `scripts/accept_changes.py` - accept tracked changes (needs LibreOffice Writer)
- `scripts/comment.py` - comment operations
- `scripts/office/validate.py` - OOXML validation
- `scripts/office/unpack.py` / `pack.py` - deterministic OOXML editing

Use the narrowest tool that solves the task. Avoid unpack/repack for ordinary paragraph text edits when docx-js or a high-level editor can do the job more safely.

## Practical delivery rule

Do not include QA PNGs/PDFs in the user-facing deliverable unless explicitly requested. Keep them in a temporary `.qa/` directory and return only the requested DOCX.
