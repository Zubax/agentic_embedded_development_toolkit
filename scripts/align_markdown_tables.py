#!/usr/bin/env python3
"""
Align simple pipe-style Markdown tables in place.
"""

from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path


SEPARATOR_RE = re.compile(r"^\s*:?-{3,}:?\s*$")
FENCE_RE = re.compile(r"^(```+|~~~+)")


def parse_row(line: str) -> list[str]:
    body = line[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in body.split("|")]


def is_table_block(lines: list[str], start: int) -> bool:
    if start + 1 >= len(lines):
        return False
    if not lines[start].startswith("|") or not lines[start + 1].startswith("|"):
        return False
    separator_cells = parse_row(lines[start + 1])
    return bool(separator_cells) and all(SEPARATOR_RE.fullmatch(cell) for cell in separator_cells)


def format_table(block: list[str]) -> list[str]:
    rows = [parse_row(line) for line in block]
    column_count = max(len(row) for row in rows)
    for row in rows:
        row.extend([""] * (column_count - len(row)))

    widths = [0] * column_count
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    formatted = []
    for row in rows:
        padded = [cell.ljust(widths[index]) for index, cell in enumerate(row)]
        formatted.append("| " + " | ".join(padded) + " |")
    return formatted


def format_markdown(text: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    index = 0
    in_fence = False

    while index < len(lines):
        line = lines[index]
        if FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            output.append(line)
            index += 1
            continue

        if not in_fence and is_table_block(lines, index):
            block = []
            while index < len(lines) and lines[index].startswith("|"):
                block.append(lines[index])
                index += 1
            output.extend(format_table(block))
            continue

        output.append(line)
        index += 1

    trailing_newline = "\n" if text.endswith("\n") else ""
    return "\n".join(output) + trailing_newline


def process_path(path: Path, check: bool) -> bool:
    original = path.read_text()
    formatted = format_markdown(original)
    changed = formatted != original

    if check:
        if changed:
            print(path)
        return changed

    if changed:
        path.write_text(formatted)
        print(f"aligned {path}")
    else:
        print(f"unchanged {path}")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="list files that would be updated")
    parser.add_argument("paths", nargs="+", help="markdown files to align")
    args = parser.parse_args()

    changed_any = False
    for raw_path in args.paths:
        changed_any |= process_path(Path(raw_path), args.check)

    return 1 if args.check and changed_any else 0


if __name__ == "__main__":
    sys.exit(main())
