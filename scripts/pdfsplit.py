#!/usr/bin/env python3

"""
Split a large PDF into one directory per chapter with a chapter PDF, page PNGs,
and chapter text, plus export whole-document text.
Mostly intended to make complex IC datasheets more accessible to LLM agents.

Examples:
    pdfsplit stm32h725vg.pdf
    pdfsplit stm32h725vg.pdf stm32h725vg/
    python scripts/pdfsplit.py stm32h725vg.pdf
    python scripts/pdfsplit.py stm32h725vg.pdf stm32h725vg/
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PAGE_IMAGE_DPI = 250
PAGE_NUMBER_PADDING = 4


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-") or "chapter"


def warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def info(message: str) -> None:
    print(message, file=sys.stderr)


def resolve_pdftotext() -> str | None:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        warn("pdftotext was not found in PATH; skipping text export.")
    return pdftotext


def export_pdf_text(pdf_path: Path, out_path: Path, pdftotext: str | None, context: str) -> None:
    if not pdftotext:
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=out_path.parent, prefix=f"{out_path.stem}_", suffix=".txt", delete=False) as tmp:
            temp_path = Path(tmp.name)

        result = subprocess.run(
            [pdftotext, str(pdf_path), str(temp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            details = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
            message = f"pdftotext failed with exit code {result.returncode}; skipping {context} text export."
            if details:
                message = f"{message}\n{details}"
            warn(message)
            return

        temp_path.replace(out_path)
    except OSError as ex:
        warn(f"{context} text export failed: {ex}")
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def reset_output_dir(path: Path) -> None:
    remove_path(path)
    path.mkdir(parents=True, exist_ok=True)


def export_chapter_page_images(doc, chapter_dir: Path, start: int, end: int) -> None:
    total_page_padding = max(PAGE_NUMBER_PADDING, len(str(doc.page_count)))
    chapter_page_count = end - start + 1
    chapter_page_padding = max(PAGE_NUMBER_PADDING, len(str(chapter_page_count)))
    for chapter_page_number, page_index in enumerate(range(start, end + 1), start=1):
        png_name = (
            f"totalpage{page_index + 1:0{total_page_padding}d}_"
            f"chapterpage{chapter_page_number:0{chapter_page_padding}d}.png"
        )
        pixmap = doc.load_page(page_index).get_pixmap(dpi=PAGE_IMAGE_DPI, alpha=False)
        pixmap.save(chapter_dir / png_name)


def default_output_dir(pdf_path: Path) -> Path:
    return Path.cwd() / ".pdfsplit" / pdf_path.name


def split_pdf_by_toc(pdf_path: str, out_dir: str | None = None) -> None:
    try:
        import fitz
    except ImportError as ex:
        raise SystemExit(
            "Error: PyMuPDF is required for pdfsplit. Install the 'pdf' extra or install 'pymupdf' manually."
        ) from ex

    src = Path(pdf_path)
    dst = Path(out_dir) if out_dir is not None else default_output_dir(src)
    dst.mkdir(parents=True, exist_ok=True)
    pdftotext = resolve_pdftotext()
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
            chapter_stem = f"{i + 1:02d}_{slugify(title)}"
            chapter_dir = dst / chapter_stem
            remove_path(dst / f"{chapter_stem}.pdf")
            remove_path(dst / f"{chapter_stem}.txt")
            reset_output_dir(chapter_dir)
            out_name = f"{chapter_stem}.pdf"
            out_path = chapter_dir / out_name
            part = fitz.open()
            try:
                part.insert_pdf(doc, from_page=start, to_page=end)
                part.save(out_path, deflate=True, garbage=4, clean=True)
            finally:
                part.close()
            export_pdf_text(out_path, chapter_dir / f"{chapter_stem}.txt", pdftotext, f"chapter '{chapter_stem}'")
            export_chapter_page_images(doc, chapter_dir, start, end)
            info(f"Processed chapter {chapter_stem} (pages {start + 1}-{end + 1}).")
            written += 1
    finally:
        doc.close()
    if written == 0:
        raise SystemExit("Error: no chapter PDFs were written.")
    export_pdf_text(src, dst / f"{src.stem}.txt", pdftotext, "whole-document")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdfsplit",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input_pdf", help="Input PDF file with an embedded table of contents.")
    parser.add_argument(
        "output_dir",
        nargs="?",
        help="Optional destination directory. Defaults to '.pdfsplit/<input filename>/' in the current directory.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    split_pdf_by_toc(args.input_pdf, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
