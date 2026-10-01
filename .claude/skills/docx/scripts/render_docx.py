#!/usr/bin/env python3
"""Render a DOCX to page PNGs using LibreOffice + pdftoppm.

Kept inside the skill so rendering does not depend on a globally installed
skill package path. This is intentionally a small wrapper; LibreOffice is the
renderer and pdftoppm is the rasterizer used for visual QA.
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_docx", type=Path)
    parser.add_argument("--output_dir", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=150)
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
        run([pdftoppm, "-png", "-r", str(max(72, args.dpi)), str(pdf), str(prefix)], env=env)

        pages = sorted(out.glob("page-*.png"))
        if not pages:
            raise RuntimeError("No page PNGs were produced")

        if args.emit_pdf:
            shutil.copy2(pdf, out / pdf.name)

        print(f"rendered {len(pages)} page(s) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
