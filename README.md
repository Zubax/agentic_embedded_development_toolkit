# Agentic Embedded Development Toolkit

A loose collection of simple tools that enable efficient AI-assisted development of embedded software.

## Repository layout

- `scripts/kicad2llm.py`: Export KiCad projects into an LLM-friendly bundle.
- `scripts/pdfsplit.py`: Split a PDF into chapter PDFs using its embedded table of contents.
- `scripts/zubax_forum_export.py`: Export Zubax Forum threads and linked topics into local Markdown.

## Development

Python formatting is configured in `pyproject.toml` and uses Black with a maximum line length of 120
characters.

To format the repository:

```bash
black .
```

Additional notes are available in `docs/development.md`.
