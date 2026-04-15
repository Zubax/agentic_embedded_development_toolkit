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

Split a large PDF into chapter PDFs using its embedded table of contents. Allows agents to read huge datasheets.

```bash
pdfsplit stm32h725vg.pdf stm32h725vg/
```

This creates one output PDF per top-level table-of-contents entry.

### `zubax_forum_export.py`

Export Zubax Forum threads and linked topics into local Markdown. Used for fetching design specifications and R&D notes.

```bash
export ZUBAX_FORUM_API_TOKEN='...'
zubax-forum-export --output-dir forum-cache https://forum.zubax.com/t/1234
```

This writes one Markdown file per fetched topic and downloads referenced attachments into the output directory.

## CI & Testing

Continuous integration is managed with [nox](https://nox.thea.codes/) and runs automatically on every push and pull request via GitHub Actions.

Available nox sessions:

| Session | Description |
|---------|-------------|
| `mypy`  | Type-check scripts with mypy in relaxed (non-strict) mode |
| `lint`  | Verify code formatting with Black |

Run all default sessions locally:

```bash
pip install nox
nox
```

Run a single session:

```bash
nox -s mypy
nox -s lint
```
