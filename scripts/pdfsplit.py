#!/usr/bin/env python3

"""
Split a large PDF into one PDF per chapter.
Mostly intended to make complex IC datasheets more accessible to LLM agents.
Example:
    pdfsplit.py stm32h725vg.pdf stm32h725vg/
"""

import re
import sys
from pathlib import Path
import fitz  # pip install pymupdf


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-") or "chapter"


def split_pdf_by_toc(pdf_path: str, out_dir: str) -> None:
    src = Path(pdf_path)
    dst = Path(out_dir)
    dst.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(src)
    toc = doc.get_toc(simple=True)
    if not toc:
        raise SystemExit("Error: PDF has no embedded table of contents / bookmarks.")
    # Top-level TOC entries are treated as chapters.
    chapters = [(title, page) for level, title, page in toc if level == 1]
    if not chapters:
        raise SystemExit("Error: PDF has a TOC, but no top-level chapter entries were found.")
    chapters.sort(key=lambda x: x[1])
    total_pages = doc.page_count
    written = 0
    for i, (title, start_page_1based) in enumerate(chapters):
        start = start_page_1based - 1
        end = (chapters[i + 1][1] - 2) if i + 1 < len(chapters) else total_pages - 1
        if start < 0 or start >= total_pages or end < start:
            continue
        out_name = f"{i + 1:02d}_{slugify(title)}.pdf"
        out_path = dst / out_name
        part = fitz.open()
        part.insert_pdf(doc, from_page=start, to_page=end)
        part.save(out_path, deflate=True, garbage=4, clean=True)
        part.close()
        written += 1
    doc.close()
    if written == 0:
        raise SystemExit("Error: no chapter PDFs were written.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(f"Usage: {sys.argv[0]} input.pdf output_dir")
    split_pdf_by_toc(sys.argv[1], sys.argv[2])
