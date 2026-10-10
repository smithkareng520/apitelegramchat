#!/usr/bin/env python3
"""minidocx -- zero-dependency (stdlib only) .docx generator (pure OOXML/zip).

No docx-js / python-docx required. Builds a valid WordprocessingML package
([Content_Types].xml, rels, word/document.xml, styles, numbering, media) using
only zipfile + string templates. CJK-safe: default East-Asian font is set so
Chinese renders on the reader's device.

    import minidocx
    d = minidocx.Docx()
    d.add_heading("Report", 1)
    d.add_para("hello " , )
    d.add_para([("bold ", {"bold": True}), ("rest", {})])
    d.add_table(["A","B"], [["1","2"],["3","4"]], widths_pt=[60,120])
    d.add_image("chart.png", width_pt=452, caption="Fig 1")
    d.save("out.docx")
"""
import zipfile, os, re

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"

CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
REL_STYLES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
REL_NUMBERING = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering"
REL_IMAGE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

A4 = {"w": 11906, "h": 16838}
MARGIN = {"top": 1260, "right": 1180, "bottom": 1260, "left": 1180}
CONTENT_W = A4["w"] - MARGIN["left"] - MARGIN["right"]   # 9546 twips

def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))

def _png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        import struct
        w, h = struct.unpack(">II", head[16:24])
        return w, h
    return None, None

def _pt_to_halfpt(pt):
    return int(round(float(pt) * 2))

def _twips_from_pt(pt):
    return int(round(float(pt) * 20))

class _Rpr:
    @staticmethod
    def render(opts):
        x = ""
        if opts.get("bold"): x += "<w:b/>"
        if opts.get("italic"): x += "<w:i/>"
        if opts.get("color"): x += f'<w:color w:val="{opts["color"]}"/>'
        if opts.get("size"): x += f'<w:sz w:val="{_pt_to_halfpt(opts["size"])}"/><w:szCs w:val="{_pt_to_halfpt(opts["size"])}"/>'
        return f"<w:rPr>{x}</w:rPr>" if x else ""

class Docx:
    def __init__(self, title="Document", author="minidocx", page_size="A4"):
        self.title = title; self.author = author
        self._size = A4 if page_size == "A4" else {"w": 12240, "h": 15840}
        self.body = []            # list of xml strings (paragraphs / tables)
        self.images = []          # list of (media_name, rId, bytes)
        self._docpr = 0; self._img_counter = 0

    # ---- content helpers ----
    @staticmethod
    def _runs_xml(text):
        if isinstance(text, str):
            text = [(text, {})]
        parts = []
        for item in text:
            if isinstance(item, dict):
                t, o = item.get("t", ""), item
            else:
                t, o = item
            parts.append(f"<w:r>{_Rpr.render(o)}<w:t xml:space=\"preserve\">{_esc(t)}</w:t></w:r>")
        return "".join(parts)

    def add_heading(self, text, level=1):
        lvl = max(1, min(3, level))
        self.body.append(
            f'<w:p><w:pPr><w:pStyle w:val="Heading{lvl}"/></w:pPr>{self._runs_xml([(text, {"bold": True})])}</w:p>')
        return self

    def add_para(self, text, size=10.5, bold=False, italic=False, color=None,
                 align=None, before_pt=0, after_pt=6):
        o = {"size": size, "bold": bold, "italic": italic, "color": color}
        aligns = {"center": "center", "right": "right", "justify": "both"}
        jc = f'<w:jc w:val="{aligns.get(align, align)}"/>' if align else ""
        sp = f'<w:spacing w:before="{before_pt*20}" w:after="{after_pt*20}"/>'
        self.body.append(
            f'<w:p><w:pPr>{sp}{jc}</w:pPr>{self._runs_xml(text) if not isinstance(text, str) else self._runs_xml([(text, o)])}</w:p>')
        return self

    def add_rich(self, runs, align=None, before_pt=0, after_pt=6):
        aligns = {"center": "center", "right": "right", "justify": "both"}
        jc = f'<w:jc w:val="{aligns.get(align, align)}"/>' if align else ""
        sp = f'<w:spacing w:before="{before_pt*20}" w:after="{after_pt*20}"/>'
        self.body.append(f'<w:p><w:pPr>{sp}{jc}</w:pPr>{self._runs_xml(runs)}</w:p>')
        return self

    def add_caption(self, text, size=8.5, color="8A97A8"):
        return self.add_rich([(text, {"size": size, "italic": True, "color": color})], align="center", after_pt=8)

    def add_note(self, text, size=8.5, color="8A97A8"):
        return self.add_rich([(text, {"size": size, "color": color})], after_pt=8)

    def add_bullet(self, text, size=10.5, bold=False, color=None):
        o = {"size": size, "bold": bold, "color": color}
        runs = self._runs_xml(text) if not isinstance(text, str) else self._runs_xml([(text, o)])
        self.body.append(
            f'<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
            f'<w:spacing w:after="40"/></w:pPr>{runs}</w:p>')
        return self

    def add_page_break(self):
        self.body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
        return self

    def add_table(self, headers, rows, widths_pt=None, header_fill="D5E8F5",
                  zebra_fill="F2F6FB", zebra=True, header_color="152238",
                  size=9.5, border="C9D3DF"):
        ncols = len(headers)
        if widths_pt:
            tw = [_twips_from_pt(x) for x in widths_pt]
            s = sum(tw) or 1
            tw = [int(CONTENT_W * t / s) for t in tw]          # scale to content width
        else:
            base = CONTENT_W // ncols
            tw = [base] * (ncols - 1) + [CONTENT_W - base * (ncols - 1)]
        total = sum(tw)
        grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in tw)
        bd = f"<w:tblBorders>" + "".join(
            f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="{border}"/>'
            for s in ["top", "left", "bottom", "right", "insideH", "insideV"]) + "</w:tblBorders>"
        x = [f'<w:tbl><w:tblPr><w:tblW w:w="{total}" w:type="dxa"/>{bd}'
             f'<w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>']
        def cell(txt, w, fill, bold, colr):
            mar = ('<w:tcMar><w:top w:w="60" w:type="dxa"/><w:left w:w="90" w:type="dxa"/>'
                   '<w:bottom w:w="60" w:type="dxa"/><w:right w:w="90" w:type="dxa"/></w:tcMar>')
            shd = f'<w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>' if fill else ""
            rp = f'<w:rPr><w:b/>' + (f'<w:color w:val="{colr}"/>' if colr else "") \
                 + f'<w:sz w:val="{_pt_to_halfpt(size)}"/></w:rPr>'
            p = (f'<w:p><w:pPr><w:spacing w:after="0"/></w:pPr>'
                 f'<w:r>{rp}<w:t xml:space="preserve">{_esc(txt)}</w:t></w:r></w:p>')
            return (f'<w:tc><w:tcPr><w:tcW w:w="{w}" w:type="dxa"/>{shd}{mar}</w:tcPr>{p}</w:tc>')
        # header row
        x.append('<w:tr><w:trPr><w:tblHeader/></w:trPr>')
        for i, h in enumerate(headers):
            x.append(cell(h, tw[i], header_fill, True, header_color))
        x.append('</w:tr>')
        for ri, row in enumerate(rows):
            zfill = zebra_fill if (zebra and ri % 2 == 0) else None
            x.append('<w:tr>')
            for i, v in enumerate(row):
                x.append(cell(v, tw[i] if i < len(tw) else tw[-1], zfill, False, None))
            x.append('</w:tr>')
        x.append('</w:tbl>')
        self.body.append("".join(x))
        return self

    def add_image(self, path, width_pt=452, height_pt=None, caption=None, center=True):
        with open(path, "rb") as f:
            data = f.read()
        pw, ph = _png_size(path)
        self._img_counter += 1
        rId = f"rId{100 + self._img_counter}"
        media = f"image{self._img_counter}.png"
        self.images.append((media, rId, data))
        cx = _twips_from_pt(width_pt) * 10
        if height_pt is not None:
            cy = _twips_from_pt(height_pt) * 10
        elif pw and ph:
            cy = int(cx * ph / pw)
        else:
            cy = int(cx * 0.65)
        self._docpr += 1
        jc = '<w:jc w:val="center"/>' if center else ""
        drawing = (
            f'<w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
            f'<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
            f'<wp:docPr id="{self._docpr}" name="{media}"/><wp:cNvGraphicFramePr><a:graphicFrameLocks'
            f' xmlns:a="{A}" noChangeAspect="1"/></wp:cNvGraphicFramePr>'
            f'<a:graphic xmlns:a="{A}"><a:graphicData uri="{PIC}">'
            f'<pic:pic xmlns:pic="{PIC}"><pic:nvPicPr>'
            f'<pic:cNvPr id="{self._docpr}" name="{media}"/><pic:cNvPicPr><a:picLocks noChangeAspect="1"/>'
            f'</pic:cNvPicPr></pic:nvPicPr><pic:blipFill><a:blip r:embed="{rId}"/><a:stretch>'
            f'<a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/>'
            f'<a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            f'</pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing>')
        self.body.append(f'<w:p><w:pPr>{jc}</w:pPr><w:r>{drawing}</w:r></w:p>')
        if caption:
            self.add_caption(caption)
        return self

    # ---- persistence ----
    def _document_xml(self):
        sect = (f'<w:sectPr><w:pgSz w:w="{self._size["w"]}" w:h="{self._size["h"]}"/>'
                f'<w:pgMar w:top="{MARGIN["top"]}" w:right="{MARGIN["right"]}" '
                f'w:bottom="{MARGIN["bottom"]}" w:left="{MARGIN["left"]}" '
                f'w:header="708" w:footer="708" w:gutter="0"/>'
                f'<w:cols w:space="708"/></w:sectPr>')
        body = "".join(self.body) + sect
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document xmlns:w="{W}" xmlns:r="{R}" xmlns:wp="{WP}" '
                f'xmlns:a="{A}" xmlns:pic="{PIC}"><w:body>{body}</w:body></w:document>')

    def _styles_xml(self):
        def h(styleId, name, sz, color, lvl):
            return (f'<w:style w:type="paragraph" w:styleId="{styleId}"><w:name w:val="{name}"/>'
                    f'<w:basedOn w:val="Normal"/><w:next w:val="Normal"/>'
                    f'<w:pPr><w:keepNext/><w:spacing w:before="220" w:after="110"/>'
                    f'<w:outlineLvl w:val="{lvl}"/></w:pPr>'
                    f'<w:rPr><w:b/><w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/><w:color w:val="{color}"/></w:rPr>'
                    f'</w:style>')
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:styles xmlns:w="{W}">'
                '<w:docDefaults><w:rPrDefault><w:rPr>'
                '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri" w:eastAsia="Microsoft YaHei"/>'
                '<w:sz w:val="21"/><w:szCs w:val="21"/>'
                '</w:rPr></w:rPrDefault>'
                '<w:pPrDefault><w:pPr><w:spacing w:line="300" w:lineRule="auto" w:after="120"/></w:pPr></w:pPrDefault>'
                '</w:docDefaults>'
                '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
                + h("Heading1", "heading 1", 30, "1F3A5F", 0)
                + h("Heading2", "heading 2", 25, "3B82C4", 1)
                + h("Heading3", "heading 3", 22, "1F3A5F", 2)
                + '</w:styles>')

    def _numbering_xml(self):
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:numbering xmlns:w="{W}"><w:abstractNum w:abstractNumId="0">'
                '<w:multiLevelType w:val="hybridMultilevel"/>'
                '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/>'
                '<w:lvlText w:val="\u2022"/><w:lvlJc w:val="left"/>'
                '<w:pPr><w:ind w:left="560" w:hanging="300"/></w:pPr>'
                '<w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol" w:hint="default"/></w:rPr>'
                '</w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
                '</w:numbering>')

    def _content_types(self):
        has_png = bool(self.images)
        png_default = '<Default Extension="png" ContentType="image/png"/>' if has_png else ""
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<Types xmlns="{CONTENT_TYPES}">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                + png_default
                + '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                + '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                + '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
                + '</Types>')

    def _document_rels(self):
        rels = [f'<Relationship Id="rId1" Type="{REL_STYLES}" Target="styles.xml"/>',
                f'<Relationship Id="rId2" Type="{REL_NUMBERING}" Target="numbering.xml"/>']
        for media, rId, _ in self.images:
            rels.append(f'<Relationship Id="{rId}" Type="{REL_IMAGE}" Target="media/{media}"/>')
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<Relationships xmlns="{PKG_REL}">' + "".join(rels) + '</Relationships>')

    def save(self, out):
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", self._content_types())
            z.writestr("_rels/.rels",
                       '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       f'<Relationships xmlns="{PKG_REL}">'
                       f'<Relationship Id="rId1" Type="{OFFICE_DOC}" Target="word/document.xml"/>'
                       '</Relationships>')
            z.writestr("word/document.xml", self._document_xml())
            z.writestr("word/styles.xml", self._styles_xml())
            z.writestr("word/numbering.xml", self._numbering_xml())
            z.writestr("word/_rels/document.xml.rels", self._document_rels())
            for media, rId, data in self.images:
                z.writestr(f"word/media/{media}", data)
        return out

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Zero-dependency .docx generator")
    p.add_argument("out")
    a = p.parse_args()
    d = Docx(title="minidocx demo")
    d.add_heading("minidocx demo (0-dependency)", 1)
    d.add_para([("This document was built with ", {"size": 10.5}),
                ("pure Python stdlib", {"bold": True, "size": 10.5}),
                (" - no third-party libraries.", {"size": 10.5})])
    d.add_bullet("add_heading / add_para / add_rich")
    d.add_bullet("add_bullet / add_caption / add_note")
    d.add_bullet("add_table / add_image / add_page_break")
    d.add_table(["Col A", "Col B", "Col C"],
                [["1", "2", "3"], ["4", "5", "6"]], widths_pt=[40, 40, 40])
    d.add_note("Generated by minidocx.py")
    d.save(a.out)
    print("WROTE", a.out)
