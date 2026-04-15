"""Nox sessions for CI testing of the toolkit scripts."""

from __future__ import annotations

import nox

SCRIPTS_DIR = "scripts"
SCRIPT_FILES = [
    f"{SCRIPTS_DIR}/kicad2llm.py",
    f"{SCRIPTS_DIR}/pdfsplit.py",
    f"{SCRIPTS_DIR}/zubax_forum_export.py",
]

nox.options.sessions = ["mypy", "lint"]


@nox.session(python="3.12")
def mypy(session: nox.Session) -> None:
    """Run mypy in relaxed (non-strict) mode on every script."""
    session.install("mypy")
    # Install optional deps so type stubs are available where possible.
    session.install("pymupdf>=1.24", "cairosvg>=2.7")
    session.run("mypy", *SCRIPT_FILES)


@nox.session(python="3.12")
def lint(session: nox.Session) -> None:
    """Check code formatting with Black."""
    session.install("black")
    session.run("black", "--check", "--diff", *SCRIPT_FILES, "noxfile.py")
