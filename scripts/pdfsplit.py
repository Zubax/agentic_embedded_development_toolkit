#!/usr/bin/env python3

"""
Split a large PDF into one PDF per chapter and export whole-document text.
Mostly intended to make complex IC datasheets more accessible to LLM agents.

Examples:
    pdfsplit stm32h725vg.pdf stm32h725vg/
    python scripts/pdfsplit.py stm32h725vg.pdf stm32h725vg/
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-") or "chapter"


def warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def export_full_text(pdf_path: Path, out_dir: Path) -> None:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        warn("pdftotext was not found in PATH; skipping whole-document text export.")
        return

    out_path = out_dir / f"{pdf_path.stem}.txt"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=out_dir, prefix=f"{pdf_path.stem}_", suffix=".txt", delete=False) as tmp:
            temp_path = Path(tmp.name)

        result = subprocess.run(
            [pdftotext, str(pdf_path), str(temp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            details = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
            message = f"pdftotext failed with exit code {result.returncode}; skipping whole-document text export."
            if details:
                message = f"{message}\n{details}"
            warn(message)
            return

        temp_path.replace(out_path)
    except OSError as ex:
        warn(f"whole-document text export failed: {ex}")
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def split_pdf_by_toc(pdf_path: str, out_dir: str) -> None:
    try:
        import fitz
    except ImportError as ex:
        raise SystemExit(
            "Error: PyMuPDF is required for pdfsplit. Install the 'pdf' extra or install 'pymupdf' manually."
        ) from ex

    src = Path(pdf_path)
    dst = Path(out_dir)
    dst.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(src)
    written = 0
    try:
        toc = doc.get_toc(simple=True)
        if not toc:
            raise SystemExit("Error: PDF has no embedded table of contents / bookmarks.")
        # Top-level TOC entries are treated as chapters.
        chapters = [(title, page) for level, title, page in toc if level == 1]
        if not chapters:
            raise SystemExit("Error: PDF has a TOC, but no top-level chapter entries were found.")
        chapters.sort(key=lambda x: x[1])
        total_pages = doc.page_count
        for i, (title, start_page_1based) in enumerate(chapters):
            start = start_page_1based - 1
            end = (chapters[i + 1][1] - 2) if i + 1 < len(chapters) else total_pages - 1
            if start < 0 or start >= total_pages or end < start:
                continue
            out_name = f"{i + 1:02d}_{slugify(title)}.pdf"
            out_path = dst / out_name
            part = fitz.open()
            try:
                part.insert_pdf(doc, from_page=start, to_page=end)
                part.save(out_path, deflate=True, garbage=4, clean=True)
            finally:
                part.close()
            written += 1
    finally:
        doc.close()
    if written == 0:
        raise SystemExit("Error: no chapter PDFs were written.")
    export_full_text(src, dst)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdfsplit",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input_pdf", help="Input PDF file with an embedded table of contents.")
    parser.add_argument(
        "output_dir",
        help="Destination directory for per-chapter PDF files and a whole-document text file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    split_pdf_by_toc(args.input_pdf, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
