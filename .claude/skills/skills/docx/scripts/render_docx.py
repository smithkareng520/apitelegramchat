#!/usr/bin/env python3
"""Render a DOCX to page PNGs using LibreOffice + pdftoppm.

Kept inside the skill so rendering does not depend on a globally installed
skill package path. This is intentionally a small wrapper; LibreOffice is the
renderer and pdftoppm is the rasterizer used for layout QA.

The slim image ships no CJK/emoji fonts, so Chinese and emoji appear blank or
as boxes in these renders. Judge LAYOUT only (page count, tables, margins,
images, headers); the DOCX itself names its fonts and will display correctly
in the reader's Word. Use --pages to keep the output small.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def run(cmd: list[str], *, env: dict[str, str]) -> None:
    proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stdout[-5000:]}")


def _has_cjk(path: Path) -> bool:
    import re
    import zipfile

    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
    except Exception:
        return False
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\U0001F000-\U0001FAFF]", xml))


def _has_system_cjk_font(env: dict[str, str]) -> bool:
    fc_list = shutil.which("fc-list")
    if not fc_list:
        return False
    out = subprocess.run([fc_list, ":lang=zh", "family"], env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True).stdout
    return bool(out.strip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_docx", type=Path)
    parser.add_argument("--output_dir", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=80, help="render DPI (default 80, small files)")
    parser.add_argument("--pages", type=int, default=0, help="render only the first N pages (0 = all)")
    parser.add_argument("--emit_pdf", action="store_true")
    args = parser.parse_args()

    input_docx = args.input_docx.resolve()
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if not input_docx.is_file():
        raise SystemExit(f"DOCX not found: {input_docx}")

    soffice = shutil.which("libreoffice") or shutil.which("soffice")
    pdftoppm = shutil.which("pdftoppm")
    if not soffice:
        raise SystemExit("LibreOffice/soffice is required for DOCX rendering")
    if not pdftoppm:
        raise SystemExit("pdftoppm is required for DOCX page rendering")

    with tempfile.TemporaryDirectory(prefix="docx-render-") as tmp:
        tmp_path = Path(tmp)
        profile = tmp_path / "lo-profile"
        env = os.environ.copy()
        env["HOME"] = str(tmp_path / "home")
        env["UserInstallation"] = f"file://{profile}"
        Path(env["HOME"]).mkdir(parents=True, exist_ok=True)

        run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path), str(input_docx)], env=env)
        pdf = tmp_path / f"{input_docx.stem}.pdf"
        if not pdf.is_file() or pdf.stat().st_size == 0:
            raise RuntimeError("LibreOffice produced no PDF")

        prefix = out / "page"
        cmd = [pdftoppm, "-png", "-r", str(max(50, args.dpi))]
        if args.pages > 0:
            cmd += ["-l", str(args.pages)]
        run(cmd + [str(pdf), str(prefix)], env=env)

        pages = sorted(out.glob("page-*.png"))
        if not pages:
            raise RuntimeError("No page PNGs were produced")

        if args.emit_pdf:
            shutil.copy2(pdf, out / pdf.name)

        print(f"rendered {len(pages)} page(s) -> {out}")
        if _has_cjk(input_docx) and not _has_system_cjk_font(env):
            print("NOTE: no CJK/emoji system font in this image - Chinese/emoji look blank or boxed "
                  "in these PNGs. Check layout only; verify the text itself with: "
                  "unzip -p file.docx word/document.xml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
