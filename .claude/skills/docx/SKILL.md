---
name: docx
description: Create, edit, inspect, and deliver Word documents (.docx) with deterministic styles, CJK-safe text, images/tables, and mandatory render-based QA. Prefer the simplest native docx-js workflow for new documents and OOXML only when the high-level API cannot express the required feature.
license: Proprietary. LICENSE.txt has complete terms
---

# DOCX skill

Use this skill for `.docx` creation, editing, review, redlines/comments, templates, forms, or DOCX↔PDF workflows.

## The default workflow

**Create/edit → validate → render → inspect every page → fix → render again → deliver.**

Do not treat XML inspection or successful file creation as visual verification. Rendering catches clipped text, missing glyphs, bad page breaks, broken tables, image shifts, and header/footer drift.

### New document

Use `docx` (docx-js) unless the task clearly benefits from editing an existing OOXML structure.

```bash
npm install
node build-docx.js
python scripts/office/validate.py output.docx
python scripts/render_docx.py output.docx --output_dir .qa/docx
```

### Existing document

Prefer high-level editing for simple text/style changes. Use OOXML for tracked changes, comments, fields, complex numbering, or features the high-level library cannot preserve safely.

```bash
python scripts/office/unpack.py input.docx unpacked/
# edit with the smallest possible change
python scripts/office/pack.py unpacked/ output.docx
python scripts/office/validate.py output.docx
python scripts/render_docx.py output.docx --output_dir .qa/docx
```

## Text, fonts, and Unicode

### Chinese / CJK

Use `Noto Sans CJK SC` for Normal and heading styles in the Linux runtime. Do not assume Arial is installed.

For mixed Chinese + Latin text, using `Noto Sans CJK SC` for the whole document is the safest default unless a corporate font is required.

### Emoji and uncommon Unicode

Do not replace emoji manually and do not split a visible emoji sequence into separate runs just because it contains several code points. ZWJ sequences, skin-tone modifiers, variation selectors, and regional-indicator flags should remain intact.

For body text, pass the original Unicode string directly to `TextRun`. If the target Office/rendering environment does not contain a suitable emoji font, prefer an explicit emoji-capable font fallback or convert the emoji to an image; do not silently delete characters.

When a task is known to contain heavy emoji or symbol content, add a small fixture covering at least:

- `👍🏽`
- `❤️`
- `👨‍👩‍👧‍👦`
- `🇸🇬`
- mixed CJK + emoji

Then render it and inspect the actual pages.

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

Override built-in heading IDs so Word/LibreOffice/TOC see the same semantic hierarchy.

```javascript
const doc = new Document({
  styles: {
    default: {
      document: { run: { font: "Noto Sans CJK SC", size: 24 } }
    },
    paragraphStyles: [
      {
        id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal",
        quickFormat: true, run: { font: "Noto Sans CJK SC", size: 32, bold: true },
        paragraph: { spacing: { before: 240, after: 240 }, outlineLevel: 0 }
      },
      {
        id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal",
        quickFormat: true, run: { font: "Noto Sans CJK SC", size: 28, bold: true },
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

The skill package includes a canonical renderer at `scripts/render_docx.py` and Office XML validation under `scripts/office/`.

Render one or more documents:

```bash
python scripts/render_docx.py report.docx --output_dir .qa/report --emit_pdf
```

Inspect **every** generated `page-*.png` at 100% zoom. Check:

- no clipped or overlapping text
- no missing CJK/emoji glyphs or black squares
- tables fit and continue correctly across pages
- headings stay with the intended following content
- images are not stretched or shifted unexpectedly
- headers/footers and page numbers align correctly
- tracked changes/comments behave as intended (comments may require XML-level checks)

If any check fails, fix the source and render again. Do not ship an unverified DOCX.

## Specialized operations

The existing scripts are the preferred building blocks:

- `scripts/accept_changes.py` - accept tracked changes
- `scripts/comment.py` - comment operations
- `scripts/office/validate.py` - OOXML validation
- `scripts/office/unpack.py` / `pack.py` - deterministic OOXML editing

Use the narrowest tool that solves the task. Avoid unpack/repack for ordinary paragraph text edits when docx-js or a high-level editor can do the job more safely.

## Practical delivery rule

Do not include QA PNGs/PDFs in the user-facing deliverable unless explicitly requested. Keep them in a temporary `.qa/` directory and return only the requested DOCX.
