# Agentic Embedded Development Toolkit

A loose collection of simple tools that enable efficient AI-assisted development of embedded software.

## Installation

The repo can be installed directly from GitHub.

```bash
python3 -m pip install "agentic-embedded-development-toolkit[all] @ git+https://github.com/Zubax/agentic_embedded_development_toolkit.git@main"
```

If you prefer a local checkout instead of direct Git install:

```bash
git clone https://github.com/Zubax/agentic_embedded_development_toolkit.git
cd agentic_embedded_development_toolkit
python3 -m pip install -e ".[all]"
```

For reproducible automation, pin a tag or commit SHA instead of `@main`.

## Tools

All are located under `scripts/`. After installation they should become invokable from `PATH`.

### `kicad2llm.py`

Export KiCad projects into an LLM-friendly bundle. Allows agents to read schematics efficiently.

```bash
kicad2llm /path/to/kicad-project/
```

This writes a `.kicad2llm/` directory next to the KiCad project file with JSON artifacts, indexes, and PNG sheet renders.

### `pdfsplit.py`

Split a large PDF into separate per-chapter directories using its embedded table of contents.
Each chapter directory contains the chapter PDF, one 250 DPI PNG per page, and a chapter-local text dump
generated with `pdftotext`.
The output root also contains one whole-document text file generated with `pdftotext`.
Helps agents search huge datasheets.

```bash
pdfsplit stm32h725vg.pdf
pdfsplit stm32h725vg.pdf stm32h725vg/
```

If no output directory is given, the default destination is `.pdfsplit/<input filename>/` in the current working directory.

Default output layout example:

```text
./.pdfsplit/stm32h725vg.pdf/
├── 01_introduction/
│   ├── 01_introduction.pdf
│   ├── 01_introduction.txt
│   ├── totalpage0001_chapterpage0001.png
│   └── ...
├── 02_memory-and-bus-architecture/
│   ├── 02_memory-and-bus-architecture.pdf
│   ├── 02_memory-and-bus-architecture.txt
│   ├── totalpage0156_chapterpage0023.png
│   └── ...
└── stm32h725vg.txt
```

### `zubax_forum_export.py`

Export Zubax Forum threads and linked topics into local Markdown. Used for fetching design specifications and R&D notes.

```bash
export ZUBAX_FORUM_API_TOKEN='...'
zubax-forum-export --output-dir forum-cache https://forum.zubax.com/t/1234
```

This writes one Markdown file per fetched topic and downloads referenced attachments into the output directory.

### `align_markdown_tables.py`

Align simple pipe-style Markdown tables in place. Useful after manual edits or AI-generated Markdown that left columns jagged.

```bash
align-markdown-tables spec.md
```
