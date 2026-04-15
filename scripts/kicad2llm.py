#!/usr/bin/env python3
"""
Generate an LLM-friendly export bundle from a KiCad project.

The exporter keeps KiCad XML netlist output as the source of truth for electrical connectivity, then enriches it
with direct `.kicad_sch` parsing for hierarchy, provenance, labels, buses, no-connect markers, and repeated-sheet
instance structure.

By default the output is split into targeted artifacts under `.kicad2llm/` next to the KiCad project file:

- `bundle.json` manifest/index
- `components/*.json`
- `nets/*.json`
- `sheets/*.json`
- `source_sheets/*.json`
- `interfaces.json`
- `net_groups.json`
- `indexes/objects.json`
- `indexes/component_to_nets.json`
- `indexes/net_to_components.json`
- `indexes/component_to_components.json`
- `indexes/sheet_to_components.json`
- `indexes/sheet_to_nets.json`
- `schemas/*.json`
- schematic sheet renders in `png/`

Optional outputs:

- `jsonl/*.jsonl` streaming sidecars (`--jsonl`)

PNG sheet renders are attempted with CairoSVG first and automatically retried with Inkscape if CairoSVG is
unavailable or fails during rendering.

Examples:
    kicad2llm /path/to/project
    kicad2llm --jsonl /path/to/project
    python scripts/kicad2llm.py /path/to/project

Pavel Kirienko <pavel.kirienko@zubax.com>, MIT license.
"""

# NOTE TO AGENTS: WHEN CHANGING THE SCRIPT, YOU MUST UPDATE THE DOCUMENTATION AND USAGE EXAMPLES,
#                 INCLUDING THE GENERATED MARKDOWN DOCUMENT.

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any

OUTDIR_NAME = ".kicad2llm"
MANIFEST_NAME = "bundle.json"
GUIDE_NAME = "AGENTS_MUST_READ.md"
PNG_DIRNAME = "png"
PNG_SCALE = 2.0
COMPONENTS_DIRNAME = "components"
NETS_DIRNAME = "nets"
SHEETS_DIRNAME = "sheets"
SOURCE_SHEETS_DIRNAME = "source_sheets"
INDEXES_DIRNAME = "indexes"
SCHEMAS_DIRNAME = "schemas"
JSONL_DIRNAME = "jsonl"
INTERFACES_NAME = "interfaces.json"
NET_GROUPS_NAME = "net_groups.json"
OBJECT_INDEX_NAME = "objects.json"
ADJACENCY_INDEX_FILENAMES = {
    "component_to_nets": "component_to_nets.json",
    "net_to_components": "net_to_components.json",
    "component_to_components": "component_to_components.json",
    "sheet_to_components": "sheet_to_components.json",
    "sheet_to_nets": "sheet_to_nets.json",
}
SPLIT_LAYOUT_VERSION = "kicad2llm/v5"
COMPACT_INTERFACE_MAX_COMPONENTS_PER_NET = 10
NON_INTERFACE_PIN_TYPES = {
    "passive",
    "power_in",
    "power_out",
    "no_connect",
    "free",
    "unspecified",
}
BUS_MEMBER_RE = re.compile(r"^(.*?)(\d+)$")
BUS_RANGE_RE = re.compile(r"^([A-Za-z0-9_./:+-]+)\[(\d+)\.\.(\d+)\]$")
COMPOSITE_BUS_RE = re.compile(r"^([A-Za-z0-9_./:+-]+)\{(.+)\}$")
SEPR_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class KiCad2LLMError(RuntimeError):
    """Domain-specific error with user-readable text."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kicad2llm",
        description=textwrap.dedent(__doc__),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "project_dir",
        metavar="PROJECT_DIR",
        help="Directory containing exactly one KiCad project (*.kicad_pro).",
    )
    parser.add_argument(
        "--jsonl",
        action="store_true",
        help="Write JSONL sidecar files for streaming consumers.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    return parser


def configure_logging(verbose: bool) -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
        force=True,
    )
    return logging.getLogger("kicad2llm")


def require_executable_in_path(name: str) -> str:
    resolved = shutil.which(name)
    if resolved:
        return resolved
    raise KiCad2LLMError(f"required executable not found in PATH: {name}")


def autodetect_project_file(project_dir: Path) -> Path:
    if not project_dir.is_dir():
        raise KiCad2LLMError(f"project directory does not exist or is not a directory: {project_dir}")

    project_candidates = sorted(project_dir.glob("*.kicad_pro"))
    if not project_candidates:
        raise KiCad2LLMError(f"no KiCad project file (*.kicad_pro) found in directory: {project_dir}")
    if len(project_candidates) > 1:
        names = ", ".join(path.name for path in project_candidates)
        raise KiCad2LLMError(
            "multiple KiCad project files found in the directory; " f"expected exactly one. Candidates: {names}"
        )
    return project_candidates[0]


def remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    shutil.rmtree(path)


def prepare_output_dir(out_dir: Path, log: logging.Logger) -> None:
    if out_dir.exists() or out_dir.is_symlink():
        log.info("Removing existing output directory: %s", out_dir)
        remove_path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)


def shlex_quote(text: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=+-]+", text):
        return text
    return repr(text)


def run_subprocess(cmd: list[str], log: logging.Logger, cwd: Path | None = None) -> None:
    log.debug("Running: %s", " ".join(shlex_quote(part) for part in cmd))
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        details = "\n".join(part for part in [stdout, stderr] if part)
        raise KiCad2LLMError(
            f"command failed with exit code {result.returncode}: {' '.join(cmd)}" + (f"\n{details}" if details else "")
        )
    if result.stderr.strip():
        log.debug(result.stderr.strip())


def export_xml_netlist(root_schematic: Path, xml_file: Path, kicad_cli: str, log: logging.Logger) -> None:
    log.info("Exporting KiCad XML netlist")
    run_subprocess(
        [
            kicad_cli,
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadxml",
            "--output",
            str(xml_file),
            str(root_schematic),
        ],
        log,
    )
    if not xml_file.is_file():
        raise KiCad2LLMError(f"expected XML netlist was not created: {xml_file}")


def export_svg_sheets(root_schematic: Path, svg_dir: Path, kicad_cli: str, log: logging.Logger) -> list[Path]:
    log.info("Exporting schematic sheets to SVG")
    run_subprocess(
        [
            kicad_cli,
            "sch",
            "export",
            "svg",
            "--output",
            str(svg_dir),
            str(root_schematic),
        ],
        log,
    )

    svg_paths = sorted(svg_dir.glob("*.svg"))
    if not svg_paths:
        raise KiCad2LLMError(f"no SVG files were generated in: {svg_dir}")
    return svg_paths


def convert_svgs_to_png(svg_paths: list[Path], png_dir: Path, log: logging.Logger) -> list[Path]:
    png_paths = [png_dir / f"{svg_path.stem}.png" for svg_path in svg_paths]

    def convert_with_cairosvg() -> list[Path]:
        import cairosvg  # type: ignore

        log.info("Converting SVG sheets to PNG with CairoSVG")
        for svg_path, png_path in zip(svg_paths, png_paths):
            log.debug("Converting %s -> %s", svg_path.name, png_path.name)
            cairosvg.svg2png(url=str(svg_path), write_to=str(png_path), scale=PNG_SCALE)
        return png_paths

    def require_inkscape(cairo_failure: Exception | None = None) -> str:
        inkscape = shutil.which("inkscape")
        if inkscape:
            return inkscape
        if cairo_failure is None:
            raise KiCad2LLMError(
                "unable to convert SVG to PNG automatically: neither the Python package 'cairosvg' "
                "nor the 'inkscape' executable is available"
            ) from None
        raise KiCad2LLMError(
            "CairoSVG failed while converting schematic sheets to PNG "
            f"({type(cairo_failure).__name__}: {cairo_failure}) and the Inkscape fallback is unavailable because "
            "the 'inkscape' executable is not in PATH"
        ) from cairo_failure

    def convert_with_inkscape(inkscape: str) -> list[Path]:
        log.info("Converting SVG sheets to PNG with Inkscape")
        for svg_path, png_path in zip(svg_paths, png_paths):
            log.debug("Converting %s -> %s", svg_path.name, png_path.name)
            run_subprocess(
                [
                    inkscape,
                    str(svg_path),
                    "--export-type=png",
                    f"--export-filename={png_path}",
                    f"--export-dpi={96.0 * PNG_SCALE}",
                ],
                log,
            )
        return png_paths

    def remove_partial_pngs() -> None:
        for png_path in png_paths:
            if png_path.exists() or png_path.is_symlink():
                log.debug("Removing partial PNG output: %s", png_path)
                remove_path(png_path)

    try:
        convert_with_cairosvg()
    except ImportError:
        convert_with_inkscape(require_inkscape())
    except Exception as ex:
        log.warning(
            "CairoSVG failed while converting schematic sheets to PNG (%s: %s). Retrying with Inkscape.",
            type(ex).__name__,
            ex,
        )
        log.debug("CairoSVG conversion failure details", exc_info=ex)
        remove_partial_pngs()
        convert_with_inkscape(require_inkscape(ex))
    for png_path in png_paths:
        if not png_path.is_file():
            raise KiCad2LLMError(f"expected PNG was not created: {png_path}")
    return png_paths


def text_or_none(parent: ET.Element | None, tag: str) -> str | None:
    if parent is None:
        return None
    child = parent.find(tag)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def unique_preserving_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def pin_type_supports_interface(pin_type: str | None) -> bool:
    if not pin_type:
        return True
    return pin_type.lower() not in NON_INTERFACE_PIN_TYPES


def natural_ref_key(ref: str) -> tuple[str, int, str]:
    match = re.fullmatch(r"([A-Za-z#]+)(\d+)(.*)", ref)
    if match:
        return match.group(1), int(match.group(2)), match.group(3)
    return ref, 10**12, ""


def pin_sort_key(pin: str) -> tuple[int, str]:
    match = re.fullmatch(r"(\d+)(.*)", pin)
    if match:
        return int(match.group(1)), match.group(2)
    return 10**12, pin


def bundle_relative_path(path: Path, out_dir: Path) -> str:
    return path.relative_to(out_dir).as_posix()


def safe_filename(text: str, fallback: str) -> str:
    cleaned = SEPR_FILENAME_RE.sub("-", text).strip("-.").lower()
    return cleaned or fallback


def display_net_name(name: str) -> str:
    stripped = name.strip("/")
    return stripped or "root"


def artifact_slug_from_net_name(name: str) -> str:
    return safe_filename(display_net_name(name).replace("/", "--"), "net")


def artifact_slug_from_sheet_path(path_names: str, display_name: str) -> str:
    stripped = path_names.strip("/")
    if stripped:
        return safe_filename(stripped.replace("/", "--"), "sheet")
    display = display_name.strip("/") if display_name else ""
    return safe_filename(display or "root", "sheet")


def stable_id(kind: str, key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    slug = safe_filename(key.replace("/", "-"), kind)[:40]
    return f"{kind}_{slug}_{digest}"


def stable_component_id(ref: str) -> str:
    return stable_id("cmp", ref)


def stable_net_id(code: str | None, name: str) -> str:
    return stable_id("net", f"{code or ''}:{name}")


def stable_source_sheet_id(relative_path: str) -> str:
    return stable_id("srcsheet", relative_path)


def stable_sheet_instance_id(path_tstamps: str) -> str:
    if path_tstamps == "/":
        return "sheet_root"
    return stable_id("sheet", path_tstamps)


def stable_interface_id(component_id_a: str, component_id_b: str) -> str:
    return stable_id("iface", f"{component_id_a}:{component_id_b}")


def stable_net_group_id(origin: str, key: str) -> str:
    return stable_id("netgrp", f"{origin}:{key}")


def parse_number(value: str | None) -> float | int | None:
    if value is None:
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    try:
        return float(value)
    except ValueError:
        return None


def sexpr_head(node: Any) -> str | None:
    if isinstance(node, list) and node:
        head = node[0]
        return head if isinstance(head, str) else None
    return None


def iter_list_children(node: list[Any], head: str | None = None) -> list[list[Any]]:
    result: list[list[Any]] = []
    for child in node[1:]:
        if isinstance(child, list) and child:
            if head is None or sexpr_head(child) == head:
                result.append(child)
    return result


def first_list_child(node: list[Any], head: str) -> list[Any] | None:
    for child in node[1:]:
        if isinstance(child, list) and child and sexpr_head(child) == head:
            return child
    return None


def scalar_children(node: list[Any]) -> list[str]:
    return [item for item in node[1:] if isinstance(item, str)]


def scalar_child_value(node: list[Any], head: str) -> str | None:
    child = first_list_child(node, head)
    if child is None:
        return None
    for item in child[1:]:
        if isinstance(item, str):
            return item
    return None


def parse_at_node(node: list[Any] | None) -> dict[str, Any] | None:
    if node is None:
        return None
    scalars = scalar_children(node)
    if len(scalars) < 2:
        return None
    return {
        "x": parse_number(scalars[0]),
        "y": parse_number(scalars[1]),
        "rotation": parse_number(scalars[2]) if len(scalars) > 2 else None,
    }


def parse_size_node(node: list[Any] | None) -> dict[str, Any] | None:
    if node is None:
        return None
    scalars = scalar_children(node)
    if len(scalars) < 2:
        return None
    return {
        "width": parse_number(scalars[0]),
        "height": parse_number(scalars[1]),
    }


def parse_pts_node(node: list[Any] | None) -> list[dict[str, Any]]:
    if node is None:
        return []
    points: list[dict[str, Any]] = []
    for xy in iter_list_children(node, "xy"):
        scalars = scalar_children(xy)
        if len(scalars) >= 2:
            points.append(
                {
                    "x": parse_number(scalars[0]),
                    "y": parse_number(scalars[1]),
                }
            )
    return points


def parse_properties(node: list[Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for prop in iter_list_children(node, "property"):
        scalars = scalar_children(prop)
        if len(scalars) >= 2:
            result[scalars[0]] = scalars[1]
    return result


def parse_title_block(node: list[Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if node is None:
        return result
    comments: dict[str, str] = {}
    for child in iter_list_children(node):
        head = sexpr_head(child)
        if head == "comment":
            scalars = scalar_children(child)
            if len(scalars) >= 2:
                comments[scalars[0]] = scalars[1]
        else:
            value = scalar_child_value(node, head or "")
            if head and value is not None:
                result[head] = value
    if comments:
        result["comments"] = comments
    return result


def parse_instances_block(node: list[Any] | None) -> list[dict[str, Any]]:
    instances: list[dict[str, Any]] = []
    if node is None:
        return instances
    for project in iter_list_children(node, "project"):
        project_scalars = scalar_children(project)
        project_name = project_scalars[0] if project_scalars else None
        for path_node in iter_list_children(project, "path"):
            path_scalars = scalar_children(path_node)
            if not path_scalars:
                continue
            entry: dict[str, Any] = {
                "project": project_name,
                "path": path_scalars[0],
            }
            for child in iter_list_children(path_node):
                head = sexpr_head(child)
                if head == "path":
                    continue
                scalars = scalar_children(child)
                if scalars:
                    entry[head or "value"] = scalars[0]
            instances.append(entry)
    return instances


def parse_symbol_block(node: list[Any]) -> dict[str, Any]:
    properties = parse_properties(node)
    pins: list[dict[str, Any]] = []
    for pin in iter_list_children(node, "pin"):
        scalars = scalar_children(pin)
        entry: dict[str, Any] = {}
        if scalars:
            entry["pin"] = scalars[0]
        uuid_value = scalar_child_value(pin, "uuid")
        if uuid_value:
            entry["uuid"] = uuid_value
        pins.append(entry)

    return {
        "uuid": scalar_child_value(node, "uuid"),
        "lib_id": scalar_child_value(node, "lib_id"),
        "unit": parse_number(scalar_child_value(node, "unit")),
        "body_style": parse_number(scalar_child_value(node, "body_style")),
        "at": parse_at_node(first_list_child(node, "at")),
        "properties": properties,
        "pins": pins,
        "instances": parse_instances_block(first_list_child(node, "instances")),
    }


def parse_sheet_block(node: list[Any]) -> dict[str, Any]:
    properties = parse_properties(node)
    return {
        "uuid": scalar_child_value(node, "uuid"),
        "sheetname": properties.get("Sheetname"),
        "sheetfile": properties.get("Sheetfile"),
        "properties": properties,
        "at": parse_at_node(first_list_child(node, "at")),
        "size": parse_size_node(first_list_child(node, "size")),
        "instances": parse_instances_block(first_list_child(node, "instances")),
    }


def parse_label_block(node: list[Any], kind: str) -> dict[str, Any]:
    scalars = scalar_children(node)
    text = scalars[0] if scalars else None
    result = {
        "kind": kind,
        "text": text,
        "uuid": scalar_child_value(node, "uuid"),
        "at": parse_at_node(first_list_child(node, "at")),
    }
    shape = scalar_child_value(node, "shape")
    if shape is not None:
        result["shape"] = shape
    return result


def parse_graphical_line_block(node: list[Any], kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "uuid": scalar_child_value(node, "uuid"),
        "points": parse_pts_node(first_list_child(node, "pts")),
    }


def parse_bus_entry_block(node: list[Any]) -> dict[str, Any]:
    return {
        "uuid": scalar_child_value(node, "uuid"),
        "at": parse_at_node(first_list_child(node, "at")),
        "size": parse_size_node(first_list_child(node, "size")),
    }


def parse_point_block(node: list[Any]) -> dict[str, Any]:
    return {
        "uuid": scalar_child_value(node, "uuid"),
        "at": parse_at_node(first_list_child(node, "at")),
    }


def parse_bus_alias_block(node: list[Any]) -> dict[str, Any]:
    scalars = scalar_children(node)
    return {
        "uuid": scalar_child_value(node, "uuid"),
        "scalars": scalars,
        "name": scalars[0] if scalars else None,
        "value": scalars[1] if len(scalars) > 1 else None,
    }


def parse_sheet_instances_block(node: list[Any] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if node is None:
        return result
    for path_node in iter_list_children(node, "path"):
        scalars = scalar_children(path_node)
        if not scalars:
            continue
        entry: dict[str, Any] = {"path": scalars[0]}
        page = scalar_child_value(path_node, "page")
        if page is not None:
            entry["page"] = page
        result.append(entry)
    return result


def parse_sexpr_text(text: str) -> list[Any]:
    index = 0
    length = len(text)

    def skip_ws() -> None:
        nonlocal index
        while index < length and text[index].isspace():
            index += 1

    def parse_string() -> str:
        nonlocal index
        if text[index] != '"':
            raise KiCad2LLMError("internal parser error: expected string")
        index += 1
        chars: list[str] = []
        while index < length:
            ch = text[index]
            if ch == "\\":
                index += 1
                if index >= length:
                    break
                escaped = text[index]
                chars.append(
                    {
                        "n": "\n",
                        "r": "\r",
                        "t": "\t",
                        '"': '"',
                        "\\": "\\",
                    }.get(escaped, escaped)
                )
                index += 1
                continue
            if ch == '"':
                index += 1
                return "".join(chars)
            chars.append(ch)
            index += 1
        raise KiCad2LLMError("unterminated string while parsing schematic")

    def parse_atom() -> str:
        nonlocal index
        start = index
        while index < length and not text[index].isspace() and text[index] not in "()":
            index += 1
        return text[start:index]

    def parse_form() -> Any:
        nonlocal index
        skip_ws()
        if index >= length:
            raise KiCad2LLMError("unexpected end of input while parsing schematic")
        if text[index] == "(":
            index += 1
            items: list[Any] = []
            while True:
                skip_ws()
                if index >= length:
                    raise KiCad2LLMError("unterminated list while parsing schematic")
                if text[index] == ")":
                    index += 1
                    return items
                items.append(parse_form())
        if text[index] == '"':
            return parse_string()
        return parse_atom()

    parsed = parse_form()
    skip_ws()
    if index != length:
        raise KiCad2LLMError("unexpected trailing data while parsing schematic")
    if not isinstance(parsed, list):
        raise KiCad2LLMError("expected root schematic expression")
    return parsed


def parse_source_schematic(path: Path) -> dict[str, Any]:
    root = parse_sexpr_text(path.read_text(encoding="utf-8"))
    if sexpr_head(root) != "kicad_sch":
        raise KiCad2LLMError(f"unexpected root expression in schematic: {path}")

    labels: list[dict[str, Any]] = []
    sheets: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    buses: list[dict[str, Any]] = []
    bus_entries: list[dict[str, Any]] = []
    wires: list[dict[str, Any]] = []
    no_connects: list[dict[str, Any]] = []
    junctions: list[dict[str, Any]] = []
    bus_aliases: list[dict[str, Any]] = []
    top_level_tags: Counter[str] = Counter()

    for child in iter_list_children(root):
        head = sexpr_head(child)
        if not head:
            continue
        top_level_tags[head] += 1
        if head == "sheet":
            sheets.append(parse_sheet_block(child))
        elif head == "symbol":
            symbols.append(parse_symbol_block(child))
        elif head == "label":
            labels.append(parse_label_block(child, "label"))
        elif head == "global_label":
            labels.append(parse_label_block(child, "global_label"))
        elif head == "hierarchical_label":
            labels.append(parse_label_block(child, "hierarchical_label"))
        elif head == "wire":
            wires.append(parse_graphical_line_block(child, "wire"))
        elif head == "bus":
            buses.append(parse_graphical_line_block(child, "bus"))
        elif head == "bus_entry":
            bus_entries.append(parse_bus_entry_block(child))
        elif head == "junction":
            junctions.append(parse_point_block(child))
        elif head == "no_connect":
            no_connects.append(parse_point_block(child))
        elif head == "bus_alias":
            bus_aliases.append(parse_bus_alias_block(child))

    known_or_ignored_tags = {
        "version",
        "generator",
        "generator_version",
        "uuid",
        "paper",
        "title_block",
        "lib_symbols",
        "sheet_instances",
        "embedded_fonts",
        "text",
        "text_box",
        "image",
        "polyline",
        "rectangle",
        "circle",
        "arc",
        "bitmap",
        "wire",
        "bus",
        "bus_entry",
        "junction",
        "no_connect",
        "sheet",
        "symbol",
        "label",
        "global_label",
        "hierarchical_label",
        "bus_alias",
    }
    unsupported = {tag: count for tag, count in sorted(top_level_tags.items()) if tag not in known_or_ignored_tags}

    return {
        "file": str(path),
        "version": scalar_child_value(root, "version"),
        "generator": scalar_child_value(root, "generator"),
        "generator_version": scalar_child_value(root, "generator_version"),
        "uuid": scalar_child_value(root, "uuid"),
        "paper": scalar_child_value(root, "paper"),
        "title_block": parse_title_block(first_list_child(root, "title_block")),
        "sheet_instances": parse_sheet_instances_block(first_list_child(root, "sheet_instances")),
        "sheets": sheets,
        "symbols": symbols,
        "labels": labels,
        "bus_aliases": bus_aliases,
        "buses": buses,
        "bus_entries": bus_entries,
        "wires": wires,
        "junctions": junctions,
        "no_connects": no_connects,
        "top_level_tag_counts": dict(sorted(top_level_tags.items())),
        "unsupported_top_level_tags": unsupported,
    }


def parse_netlist_xml(xml_file: Path) -> dict[str, Any]:
    tree = ET.parse(xml_file)
    root = tree.getroot()

    design_elem = root.find("design")
    design_info: dict[str, Any] = {}
    if design_elem is not None:
        for child in design_elem:
            text = (child.text or "").strip()
            if text:
                design_info[child.tag] = text

    libparts: dict[str, dict[str, Any]] = {}
    for libpart in root.findall("./libparts/libpart"):
        lib = libpart.attrib.get("lib")
        part = libpart.attrib.get("part")
        key = f"{lib}:{part}" if lib and part else None

        pins: dict[str, dict[str, Any]] = {}
        for pin in libpart.findall("./pins/pin"):
            num = pin.attrib.get("num")
            if not num:
                continue
            pins[num] = {
                "num": num,
                "name": pin.attrib.get("name"),
                "type": pin.attrib.get("type"),
            }

        aliases = [
            alias.text.strip() for alias in libpart.findall("./aliases/alias") if alias.text and alias.text.strip()
        ]
        fields = {
            field.attrib.get("name", ""): (field.text or "").strip()
            for field in libpart.findall("./fields/field")
            if field.attrib.get("name")
        }

        item = {
            "lib": lib,
            "part": part,
            "description": text_or_none(libpart, "description"),
            "docs": text_or_none(libpart, "docs"),
            "aliases": aliases,
            "fields": fields,
            "pins": pins,
        }
        if key:
            libparts[key] = item

    components: dict[str, dict[str, Any]] = {}
    components_by_sheet_names: dict[str, list[str]] = defaultdict(list)
    components_by_sheet_tstamps: dict[str, list[str]] = defaultdict(list)
    missing_library_metadata: list[str] = []

    for comp in root.findall("./components/comp"):
        ref = comp.attrib.get("ref")
        if not ref:
            continue

        libsource = comp.find("libsource")
        lib = libsource.attrib.get("lib") if libsource is not None else None
        part = libsource.attrib.get("part") if libsource is not None else None
        lib_key = f"{lib}:{part}" if lib and part else None
        libpart = libparts.get(lib_key) if lib_key else None
        if lib_key and libpart is None:
            missing_library_metadata.append(ref)

        fields = {
            field.attrib.get("name", ""): (field.text or "").strip()
            for field in comp.findall("./fields/field")
            if field.attrib.get("name")
        }
        properties = {
            prop.attrib.get("name", ""): (prop.text or "").strip()
            for prop in comp.findall("./property")
            if prop.attrib.get("name")
        }
        sheetpath = comp.find("sheetpath")
        sheet_names = sheetpath.attrib.get("names") if sheetpath is not None else None
        sheet_tstamps = sheetpath.attrib.get("tstamps") if sheetpath is not None else None

        components_by_sheet_names[sheet_names or "/"].append(ref)
        components_by_sheet_tstamps[sheet_tstamps or "/"].append(ref)

        components[ref] = {
            "ref": ref,
            "value": text_or_none(comp, "value"),
            "footprint": text_or_none(comp, "footprint"),
            "datasheet": text_or_none(comp, "datasheet"),
            "description": libpart.get("description") if libpart else None,
            "docs": libpart.get("docs") if libpart else None,
            "libsource": {
                "lib": lib,
                "part": part,
                "description": libsource.attrib.get("description") if libsource is not None else None,
            },
            "fields": fields,
            "properties": properties,
            "sheetpath": {
                "names": sheet_names,
                "tstamps": sheet_tstamps,
            },
            "tstamp": text_or_none(comp, "tstamp"),
            "pins": {},
        }

    nets: dict[str, dict[str, Any]] = {}
    unnamed_counter = 0
    pins_missing_metadata = 0

    for net in root.findall("./nets/net"):
        net_name = net.attrib.get("name")
        if not net_name:
            unnamed_counter += 1
            net_name = f"<unnamed-{unnamed_counter}>"
        net_code = net.attrib.get("code")
        nodes: list[dict[str, Any]] = []

        for node in net.findall("node"):
            ref = node.attrib.get("ref")
            pin = node.attrib.get("pin")
            pin_function = node.attrib.get("pinfunction")
            pin_type = node.attrib.get("pintype")

            comp = components.get(ref or "")
            lib = comp["libsource"]["lib"] if comp else None
            part = comp["libsource"]["part"] if comp else None
            libpart = libparts.get(f"{lib}:{part}") if lib and part else None
            pin_meta = libpart.get("pins", {}).get(pin or "") if libpart else None

            if pin_meta:
                pin_function = pin_function or pin_meta.get("name")
                pin_type = pin_type or pin_meta.get("type")

            if not pin_function or not pin_type:
                pins_missing_metadata += 1

            nodes.append(
                {
                    "ref": ref,
                    "pin": pin,
                    "pinfunction": pin_function,
                    "pintype": pin_type,
                }
            )

            if ref and ref in components and pin:
                pin_map = components[ref]["pins"]
                pin_entry = pin_map.setdefault(
                    pin,
                    {
                        "pin": pin,
                        "pinfunction": pin_function,
                        "pintype": pin_type,
                        "nets": [],
                    },
                )
                if pin_function and not pin_entry.get("pinfunction"):
                    pin_entry["pinfunction"] = pin_function
                if pin_type and not pin_entry.get("pintype"):
                    pin_entry["pintype"] = pin_type
                pin_entry["nets"].append(net_name)

        nets[net_name] = {
            "name": net_name,
            "code": net_code,
            "nodes": nodes,
        }

    for comp in components.values():
        comp["pins"] = dict(sorted(comp["pins"].items(), key=lambda item: pin_sort_key(item[0])))

    components_sorted = {ref: components[ref] for ref in sorted(components.keys(), key=natural_ref_key)}
    nets_sorted = {name: nets[name] for name in sorted(nets.keys())}
    components_by_sheet_names_sorted = {
        sheet: sorted(refs, key=natural_ref_key)
        for sheet, refs in sorted(components_by_sheet_names.items(), key=lambda item: item[0])
    }
    components_by_sheet_tstamps_sorted = {
        sheet: sorted(refs, key=natural_ref_key)
        for sheet, refs in sorted(components_by_sheet_tstamps.items(), key=lambda item: item[0])
    }

    return {
        "design": design_info,
        "components": components_sorted,
        "nets": nets_sorted,
        "components_by_sheet_names": components_by_sheet_names_sorted,
        "components_by_sheet_tstamps": components_by_sheet_tstamps_sorted,
        "counts": {
            "component_count": len(components_sorted),
            "net_count": len(nets_sorted),
            "sheet_count": len(components_by_sheet_tstamps_sorted),
        },
        "xml_diagnostics": {
            "missing_library_metadata_refs": sorted(set(missing_library_metadata), key=natural_ref_key),
            "missing_library_metadata_count": len(set(missing_library_metadata)),
            "pins_missing_metadata_count": pins_missing_metadata,
        },
    }


def schematic_path_to_xml_path(schematic_path: str, root_uuid: str) -> str | None:
    parts = [part for part in schematic_path.split("/") if part]
    if not parts:
        return "/"
    if parts[0] != root_uuid:
        return None
    remainder = parts[1:]
    if not remainder:
        return "/"
    return "/" + "/".join(remainder) + "/"


def xml_path_depth(path_tstamps: str) -> int:
    return len([part for part in path_tstamps.split("/") if part])


def infer_bus_member(name: str) -> tuple[str, int] | None:
    if name.startswith("Net-(") or name.startswith("unconnected-"):
        return None
    match = BUS_MEMBER_RE.fullmatch(name)
    if not match:
        return None
    base, suffix = match.groups()
    if not base or base.endswith(("+", "-")):
        return None
    return base, int(suffix)


def infer_numbered_net_groups(nets: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    candidates: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for net_name in nets:
        member = infer_bus_member(net_name)
        if member is None:
            continue
        base, index = member
        candidates[base].append((index, net_name))

    groups: list[dict[str, Any]] = []
    memberships: dict[str, list[str]] = defaultdict(list)
    for base, members in sorted(candidates.items()):
        deduped = sorted({(index, net_name) for index, net_name in members})
        if len(deduped) < 2:
            continue
        indices = [index for index, _ in deduped]
        member_nets = [net_name for _, net_name in deduped]
        group = {
            "kind": "net_group",
            "origin": "inferred",
            "name": base,
            "base_name": base,
            "members": [
                {
                    "net_name": net_name,
                    "index": index,
                }
                for index, net_name in deduped
            ],
            "indices": indices,
            "index_min": min(indices),
            "index_max": max(indices),
            "width": len(member_nets),
            "is_contiguous": indices == list(range(min(indices), max(indices) + 1)),
            "provenance": {
                "derived_from": "numbered_net_names",
            },
        }
        groups.append(group)
    return groups, memberships


def join_bus_name(prefix: str | None, suffix: str) -> str:
    if prefix:
        return f"{prefix}.{suffix}"
    return suffix


def expand_bus_member_token(prefix: str | None, token: str) -> dict[str, Any]:
    token = token.strip()
    range_match = BUS_RANGE_RE.fullmatch(token)
    if range_match:
        local_name, start_raw, end_raw = range_match.groups()
        start = int(start_raw)
        end = int(end_raw)
        full_name = join_bus_name(prefix, local_name)
        step = 1 if end >= start else -1
        indices = list(range(start, end + step, step))
        return {
            "kind": "range",
            "name": full_name,
            "members": [
                {
                    "net_name": f"{full_name}{index}",
                    "index": index,
                }
                for index in indices
            ],
            "indices": indices,
            "width": len(indices),
            "is_contiguous": True,
        }
    full_name = join_bus_name(prefix, token)
    return {
        "kind": "scalar",
        "name": full_name,
        "members": [{"net_name": full_name, "index": None}],
        "indices": [],
        "width": 1,
        "is_contiguous": True,
    }


def parse_explicit_bus_labels(labels: list[dict[str, Any]], bus_aliases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for label in labels:
        text = label.get("text")
        if not text:
            continue
        composite_match = COMPOSITE_BUS_RE.fullmatch(text)
        if composite_match:
            prefix, body = composite_match.groups()
            tokens = [token for token in body.split() if token]
            members = [expand_bus_member_token(prefix, token) for token in tokens]
            groups.append(
                {
                    "kind": "bus_declaration",
                    "origin": "explicit",
                    "name": prefix,
                    "label_text": text,
                    "label_kind": label["kind"],
                    "members": members,
                    "source_label_uuid": label.get("uuid"),
                }
            )
            for member in members:
                if member["width"] >= 2:
                    groups.append(
                        {
                            "kind": "net_group",
                            "origin": "explicit",
                            "name": member["name"],
                            "base_name": member["name"],
                            "members": member["members"],
                            "indices": member["indices"],
                            "index_min": min(member["indices"]) if member["indices"] else None,
                            "index_max": max(member["indices"]) if member["indices"] else None,
                            "width": member["width"],
                            "is_contiguous": member["is_contiguous"],
                            "provenance": {
                                "label_text": text,
                                "label_kind": label["kind"],
                                "source_label_uuid": label.get("uuid"),
                                "parent_bus_name": prefix,
                            },
                        }
                    )
            continue

        if BUS_RANGE_RE.fullmatch(text):
            member = expand_bus_member_token(None, text)
            groups.append(
                {
                    "kind": "net_group",
                    "origin": "explicit",
                    "name": member["name"],
                    "base_name": member["name"],
                    "members": member["members"],
                    "indices": member["indices"],
                    "index_min": min(member["indices"]) if member["indices"] else None,
                    "index_max": max(member["indices"]) if member["indices"] else None,
                    "width": member["width"],
                    "is_contiguous": member["is_contiguous"],
                    "provenance": {
                        "label_text": text,
                        "label_kind": label["kind"],
                        "source_label_uuid": label.get("uuid"),
                    },
                }
            )

    for alias in bus_aliases:
        alias_name = alias.get("name")
        alias_value = alias.get("value")
        if alias_name and alias_value and BUS_RANGE_RE.fullmatch(alias_value):
            member = expand_bus_member_token(None, alias_value)
            groups.append(
                {
                    "kind": "net_group",
                    "origin": "explicit",
                    "name": alias_name,
                    "base_name": alias_name,
                    "members": member["members"],
                    "indices": member["indices"],
                    "index_min": min(member["indices"]) if member["indices"] else None,
                    "index_max": max(member["indices"]) if member["indices"] else None,
                    "width": member["width"],
                    "is_contiguous": member["is_contiguous"],
                    "provenance": {
                        "bus_alias_value": alias_value,
                        "source_alias_uuid": alias.get("uuid"),
                    },
                }
            )
    return groups


def collect_source_schematics(root_schematic: Path, project_dir: Path, log: logging.Logger) -> dict[str, Any]:
    parsed_by_path: dict[Path, dict[str, Any]] = {}
    warnings: list[dict[str, Any]] = []
    queue = [root_schematic.resolve()]

    while queue:
        current = queue.pop(0)
        if current in parsed_by_path:
            continue
        log.info("Parsing schematic source: %s", current)
        parsed = parse_source_schematic(current)
        parsed_by_path[current] = parsed

        for sheet in parsed["sheets"]:
            sheetfile = sheet.get("sheetfile")
            if not sheetfile:
                warnings.append(
                    {
                        "code": "sheet_missing_sheetfile",
                        "message": "Sheet is missing the Sheetfile property.",
                        "file": str(current),
                        "sheet_uuid": sheet.get("uuid"),
                    }
                )
                continue
            child_path = (current.parent / sheetfile).resolve()
            if not child_path.is_file():
                warnings.append(
                    {
                        "code": "missing_sheet_source_file",
                        "message": "Referenced child schematic file was not found.",
                        "file": str(current),
                        "sheet_uuid": sheet.get("uuid"),
                        "sheetfile": sheetfile,
                        "resolved_path": str(child_path),
                    }
                )
                continue
            if child_path not in parsed_by_path:
                queue.append(child_path)

    root_data = parsed_by_path[root_schematic.resolve()]
    root_uuid = root_data.get("uuid")
    if not root_uuid:
        raise KiCad2LLMError(f"root schematic UUID missing: {root_schematic}")

    source_sheets: dict[str, dict[str, Any]] = {}
    source_sheet_by_path: dict[Path, str] = {}
    unsupported_constructs: list[dict[str, Any]] = []
    explicit_groups_raw: list[dict[str, Any]] = []
    symbol_instance_map: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    for path in sorted(parsed_by_path.keys()):
        parsed = parsed_by_path[path]
        relative_path = path.relative_to(project_dir).as_posix()
        source_sheet_id = stable_source_sheet_id(relative_path)
        source_sheet_by_path[path] = source_sheet_id
        labels = parsed["labels"]
        explicit_groups = parse_explicit_bus_labels(labels, parsed["bus_aliases"])
        for group in explicit_groups:
            group["source_sheet_id"] = source_sheet_id
            explicit_groups_raw.append(group)

        for symbol in parsed["symbols"]:
            for instance in symbol["instances"]:
                reference = instance.get("reference") or symbol["properties"].get("Reference")
                schematic_path = instance.get("path")
                xml_path = schematic_path_to_xml_path(schematic_path, root_uuid) if schematic_path else None
                if not reference or xml_path is None:
                    continue
                symbol_instance_map[(reference, xml_path)].append(
                    {
                        "source_sheet_id": source_sheet_id,
                        "source_file": relative_path,
                        "symbol_uuid": symbol.get("uuid"),
                        "lib_id": symbol.get("lib_id"),
                        "schematic_instance_path": schematic_path,
                        "xml_sheet_path": xml_path,
                        "unit": instance.get("unit") or symbol.get("unit"),
                        "at": symbol.get("at"),
                    }
                )

        if parsed["unsupported_top_level_tags"]:
            unsupported_constructs.append(
                {
                    "source_sheet_id": source_sheet_id,
                    "file": relative_path,
                    "tags": parsed["unsupported_top_level_tags"],
                }
            )

        source_sheets[source_sheet_id] = {
            "id": source_sheet_id,
            "relative_file": relative_path,
            "absolute_file": str(path),
            "uuid": parsed.get("uuid"),
            "version": parsed.get("version"),
            "generator": parsed.get("generator"),
            "generator_version": parsed.get("generator_version"),
            "paper": parsed.get("paper"),
            "title_block": parsed.get("title_block", {}),
            "labels": labels,
            "bus_aliases": parsed["bus_aliases"],
            "explicit_bus_declarations": explicit_groups,
            "sheets": parsed["sheets"],
            "symbols": parsed["symbols"],
            "buses": parsed["buses"],
            "bus_entries": parsed["bus_entries"],
            "wires": parsed["wires"],
            "junctions": parsed["junctions"],
            "no_connects": parsed["no_connects"],
            "sheet_instances": parsed["sheet_instances"],
            "top_level_tag_counts": parsed["top_level_tag_counts"],
            "unsupported_top_level_tags": parsed["unsupported_top_level_tags"],
        }

    root_source_id = source_sheet_by_path[root_schematic.resolve()]
    root_page = "1"
    for entry in source_sheets[root_source_id]["sheet_instances"]:
        if entry.get("path") == "/" and entry.get("page"):
            root_page = entry["page"]
            break

    sheet_instances_by_path: dict[str, dict[str, Any]] = {}

    def build_sheet_instances(
        source_sheet_id: str,
        parent_id: str | None,
        schematic_path: str,
        path_tstamps: str,
        path_names: str,
        display_name: str,
        page: str | None,
        placement: dict[str, Any] | None,
        placement_sheet_uuid: str | None,
    ) -> None:
        source_sheet = source_sheets[source_sheet_id]
        sheet_id = stable_sheet_instance_id(path_tstamps)
        if sheet_id in sheet_instances_by_path:
            return

        sheet_instances_by_path[path_tstamps] = {
            "id": sheet_id,
            "source_sheet_id": source_sheet_id,
            "source_file": source_sheet["relative_file"],
            "source_uuid": source_sheet["uuid"],
            "schematic_instance_path": schematic_path,
            "path_tstamps": path_tstamps,
            "path_names": path_names,
            "display_name": display_name,
            "page": page,
            "parent_id": parent_id,
            "child_ids": [],
            "placement": placement,
            "placement_sheet_uuid": placement_sheet_uuid,
        }
        if parent_id:
            parent = next(item for item in sheet_instances_by_path.values() if item["id"] == parent_id)
            parent["child_ids"].append(sheet_id)

        for child_sheet in source_sheet["sheets"]:
            child_source_file = child_sheet.get("sheetfile")
            child_source_path = (
                (Path(source_sheet["absolute_file"]).parent / child_source_file).resolve()
                if child_source_file
                else None
            )
            child_source_id = source_sheet_by_path.get(child_source_path) if child_source_path else None

            matching_instance_entries = [
                entry for entry in child_sheet["instances"] if entry.get("path") == schematic_path
            ]
            for entry in matching_instance_entries:
                child_uuid = child_sheet.get("uuid")
                if not child_uuid:
                    warnings.append(
                        {
                            "code": "sheet_missing_uuid",
                            "message": "Sheet instance cannot be identified because the sheet UUID is missing.",
                            "file": source_sheet["relative_file"],
                            "sheetname": child_sheet.get("sheetname"),
                        }
                    )
                    continue
                child_schematic_path = f"{schematic_path}/{child_uuid}" if schematic_path else f"/{child_uuid}"
                child_path_tstamps = (
                    "/" + "/".join([part for part in [path_tstamps.strip("/"), child_uuid] if part]) + "/"
                )
                if child_path_tstamps == "//":
                    child_path_tstamps = "/"
                child_name = child_sheet.get("sheetname") or Path(child_sheet.get("sheetfile") or child_uuid).stem
                child_path_names = "/" + "/".join([part for part in [path_names.strip("/"), child_name] if part]) + "/"
                if not child_source_id:
                    warnings.append(
                        {
                            "code": "missing_sheet_source_file",
                            "message": "Sheet instance references a source file that was not parsed.",
                            "file": source_sheet["relative_file"],
                            "sheet_uuid": child_uuid,
                            "sheetfile": child_sheet.get("sheetfile"),
                        }
                    )
                    continue
                build_sheet_instances(
                    source_sheet_id=child_source_id,
                    parent_id=sheet_id,
                    schematic_path=child_schematic_path,
                    path_tstamps=child_path_tstamps,
                    path_names=child_path_names,
                    display_name=child_name,
                    page=entry.get("page"),
                    placement={
                        "at": child_sheet.get("at"),
                        "size": child_sheet.get("size"),
                        "properties": child_sheet.get("properties", {}),
                    },
                    placement_sheet_uuid=child_uuid,
                )

    build_sheet_instances(
        source_sheet_id=root_source_id,
        parent_id=None,
        schematic_path=f"/{root_uuid}",
        path_tstamps="/",
        path_names="/",
        display_name="/",
        page=root_page,
        placement=None,
        placement_sheet_uuid=None,
    )

    return {
        "root_uuid": root_uuid,
        "root_source_sheet_id": root_source_id,
        "source_sheets": source_sheets,
        "sheet_instances_by_path": sheet_instances_by_path,
        "symbol_instance_map": symbol_instance_map,
        "explicit_groups_raw": explicit_groups_raw,
        "warnings": warnings,
        "unsupported_constructs": unsupported_constructs,
    }


def build_png_manifest(
    sheet_instances: dict[str, dict[str, Any]],
    project_name: str,
    png_paths: list[Path],
    out_dir: Path,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    if not png_paths:
        return (
            {
                instance["id"]: {
                    "sheet_id": instance["id"],
                    "path": None,
                    "expected_path": None,
                    "exists": False,
                    "page": instance.get("page"),
                    "scale": PNG_SCALE,
                }
                for instance in sheet_instances.values()
            },
            [],
        )

    existing = {bundle_relative_path(path, out_dir): path for path in png_paths}
    warnings: list[dict[str, Any]] = []
    used_paths: set[str] = set()
    manifest: dict[str, dict[str, Any]] = {}

    for instance in sheet_instances.values():
        display_name = instance["display_name"]
        expected_rel = (
            f"{PNG_DIRNAME}/{project_name}.png"
            if instance["path_tstamps"] == "/"
            else f"{PNG_DIRNAME}/{project_name}-{display_name}.png"
        )
        exists = expected_rel in existing
        manifest[instance["id"]] = {
            "sheet_id": instance["id"],
            "path": expected_rel if exists else None,
            "expected_path": expected_rel,
            "exists": exists,
            "page": instance.get("page"),
            "scale": PNG_SCALE,
        }
        if exists:
            used_paths.add(expected_rel)
        else:
            warnings.append(
                {
                    "code": "missing_png_for_sheet",
                    "message": "Expected schematic PNG for sheet instance was not produced.",
                    "sheet_id": instance["id"],
                    "expected_path": expected_rel,
                }
            )

    for rel in sorted(set(existing.keys()) - used_paths):
        warnings.append(
            {
                "code": "unused_png_file",
                "message": "Generated PNG did not match any known sheet instance.",
                "path": rel,
            }
        )

    return manifest, warnings


def dedupe_explicit_groups(
    explicit_groups_raw: list[dict[str, Any]],
    nets_by_name: dict[str, dict[str, Any]],
    source_sheets: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    grouped: dict[tuple[str, str, tuple[str, ...]], list[dict[str, Any]]] = defaultdict(list)
    memberships: dict[str, list[str]] = defaultdict(list)

    for group in explicit_groups_raw:
        if group["kind"] != "net_group":
            continue
        member_names = tuple(member["net_name"] for member in group["members"])
        grouped[(group["origin"], group["name"], member_names)].append(group)

    results: list[dict[str, Any]] = []
    for (_, name, member_names), entries in sorted(grouped.items()):
        first = entries[0]
        group_id = stable_net_group_id("explicit", f"{name}:{','.join(member_names)}")
        members: list[dict[str, Any]] = []
        for member in first["members"]:
            net_name = member["net_name"]
            resolved_net = nets_by_name.get(net_name)
            member_entry = {
                "net_name": net_name,
                "index": member["index"],
                "resolved_net_id": resolved_net["id"] if resolved_net else None,
            }
            members.append(member_entry)
            if resolved_net:
                memberships[resolved_net["id"]].append(group_id)

        results.append(
            {
                "id": group_id,
                "kind": "net_group",
                "origin": "explicit",
                "name": name,
                "base_name": first["base_name"],
                "members": members,
                "indices": first["indices"],
                "index_min": first["index_min"],
                "index_max": first["index_max"],
                "width": first["width"],
                "is_contiguous": first["is_contiguous"],
                "provenance": {
                    "source_sheet_ids": sorted({entry["source_sheet_id"] for entry in entries}),
                    "label_texts": sorted(
                        {
                            entry["provenance"].get("label_text")
                            for entry in entries
                            if entry.get("provenance") and entry["provenance"].get("label_text")
                        }
                    ),
                    "label_kinds": sorted(
                        {
                            entry["provenance"].get("label_kind")
                            for entry in entries
                            if entry.get("provenance") and entry["provenance"].get("label_kind")
                        }
                    ),
                },
            }
        )
    return results, memberships


def build_label_index(labels: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for label in labels:
        text = label.get("text")
        if not text:
            continue
        grouped[text].append(
            {
                "kind": label.get("kind"),
                "uuid": label.get("uuid"),
                "at": label.get("at"),
            }
        )
    return {
        text: sorted(items, key=lambda item: (item.get("kind") or "", item.get("uuid") or ""))
        for text, items in sorted(grouped.items())
    }


def build_symbol_instance_index(symbols: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for symbol in symbols:
        default_ref = symbol.get("properties", {}).get("Reference") or symbol.get("uuid") or "<unknown>"
        for instance in symbol.get("instances", []) or [{}]:
            reference = instance.get("reference") or default_ref
            grouped[reference].append(
                {
                    "symbol_uuid": symbol.get("uuid"),
                    "lib_id": symbol.get("lib_id"),
                    "unit": instance.get("unit") or symbol.get("unit"),
                    "schematic_instance_path": instance.get("path"),
                    "at": symbol.get("at"),
                }
            )
    return {
        ref: sorted(items, key=lambda item: (item.get("schematic_instance_path") or "", item.get("symbol_uuid") or ""))
        for ref, items in sorted(grouped.items(), key=lambda item: natural_ref_key(item[0]))
    }


def distill_source_sheet_view(source_sheet: dict[str, Any]) -> dict[str, Any]:
    explicit_bus_declarations = [
        {
            "name": group.get("name"),
            "label_text": group.get("label_text"),
            "label_kind": group.get("label_kind"),
            "source_label_uuid": group.get("source_label_uuid"),
            "members": [
                {
                    "kind": member.get("kind"),
                    "name": member.get("name"),
                    "width": member.get("width"),
                    "indices": member.get("indices"),
                    "member_net_names": [item.get("net_name") for item in member.get("members", [])],
                }
                for member in group.get("members", [])
            ],
        }
        for group in source_sheet.get("explicit_bus_declarations", [])
        if group.get("kind") == "bus_declaration"
    ]
    explicit_net_groups = [
        {
            "name": group.get("name"),
            "base_name": group.get("base_name"),
            "width": group.get("width"),
            "indices": group.get("indices"),
            "source_label_uuid": group.get("provenance", {}).get("source_label_uuid"),
            "label_text": group.get("provenance", {}).get("label_text"),
        }
        for group in source_sheet.get("explicit_bus_declarations", [])
        if group.get("kind") == "net_group"
    ]
    child_sheets = [
        {
            "uuid": sheet.get("uuid"),
            "sheetname": sheet.get("sheetname"),
            "sheetfile": sheet.get("sheetfile"),
            "at": sheet.get("at"),
            "size": sheet.get("size"),
            "instances": [
                {
                    "path": entry.get("path"),
                    "page": entry.get("page"),
                }
                for entry in sheet.get("instances", [])
            ],
        }
        for sheet in sorted(
            source_sheet.get("sheets", []), key=lambda item: (item.get("sheetname") or "", item.get("uuid") or "")
        )
    ]
    return {
        "labels_by_text": build_label_index(source_sheet.get("labels", [])),
        "explicit_bus_declarations": explicit_bus_declarations,
        "explicit_net_groups": explicit_net_groups,
        "bus_aliases": sorted(
            [
                {
                    "name": alias.get("name"),
                    "value": alias.get("value"),
                    "uuid": alias.get("uuid"),
                }
                for alias in source_sheet.get("bus_aliases", [])
            ],
            key=lambda item: (item.get("name") or "", item.get("uuid") or ""),
        ),
        "child_sheets": child_sheets,
        "symbol_instances_by_ref": build_symbol_instance_index(source_sheet.get("symbols", [])),
        "no_connects": source_sheet.get("no_connects", []),
    }


def build_normalized_model(
    parsed_xml: dict[str, Any],
    schematic_model: dict[str, Any],
    project_dir: Path,
    project_file: Path,
    root_schematic: Path,
    png_manifest: dict[str, dict[str, Any]],
    png_warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    components_xml = parsed_xml["components"]
    nets_xml = parsed_xml["nets"]
    sheet_instances = dict(schematic_model["sheet_instances_by_path"])
    source_sheets = dict(schematic_model["source_sheets"])
    warnings = list(schematic_model["warnings"]) + list(png_warnings)

    for sheet in sheet_instances.values():
        sheet["png"] = png_manifest.get(sheet["id"], {"path": None, "exists": False})
        sheet["component_ids"] = []
        sheet["net_ids"] = []

    for path_tstamps, refs in parsed_xml["components_by_sheet_tstamps"].items():
        if path_tstamps not in sheet_instances:
            synthetic_id = stable_sheet_instance_id(path_tstamps)
            sheet_instances[path_tstamps] = {
                "id": synthetic_id,
                "source_sheet_id": None,
                "source_file": None,
                "source_uuid": None,
                "schematic_instance_path": None,
                "path_tstamps": path_tstamps,
                "path_names": next(
                    (components_xml[ref]["sheetpath"].get("names") or "/" for ref in refs if ref in components_xml),
                    "/",
                ),
                "display_name": next(
                    (
                        (components_xml[ref]["sheetpath"].get("names") or "/").strip("/").split("/")[-1] or "/"
                        for ref in refs
                        if ref in components_xml
                    ),
                    "/",
                ),
                "page": None,
                "parent_id": None,
                "child_ids": [],
                "placement": None,
                "placement_sheet_uuid": None,
                "png": {"path": None, "exists": False},
                "component_ids": [],
                "net_ids": [],
            }
            warnings.append(
                {
                    "code": "component_sheet_instance_unresolved",
                    "message": "A sheet instance from XML connectivity was not found in the schematic hierarchy and had to be synthesized.",
                    "path_tstamps": path_tstamps,
                }
            )

    components: dict[str, dict[str, Any]] = {}
    component_id_by_ref: dict[str, str] = {}
    component_sheet_lookup: dict[str, str] = {}
    component_source_map = schematic_model["symbol_instance_map"]

    for ref, component in components_xml.items():
        component_id = stable_component_id(ref)
        component_id_by_ref[ref] = component_id
        sheet_tstamps = component["sheetpath"].get("tstamps") or "/"
        sheet_instance = sheet_instances[sheet_tstamps]
        component_sheet_lookup[component_id] = sheet_instance["id"]
        source_symbols = component_source_map.get((ref, sheet_tstamps), [])
        if not source_symbols:
            warnings.append(
                {
                    "code": "component_source_symbol_unresolved",
                    "message": "Component could not be matched back to a symbol placement in the source schematic.",
                    "ref": ref,
                    "sheet_path": sheet_tstamps,
                }
            )

        pins = []
        for pin_name, pin_entry in component["pins"].items():
            pins.append(
                {
                    "pin": pin_name,
                    "pinfunction": pin_entry.get("pinfunction"),
                    "pintype": pin_entry.get("pintype"),
                    "nets": list(pin_entry.get("nets", [])),
                }
            )

        components[component_id] = {
            "id": component_id,
            "ref": ref,
            "imported": {
                "value": component.get("value"),
                "footprint": component.get("footprint"),
                "datasheet": component.get("datasheet"),
                "description": component.get("description"),
                "docs": component.get("docs"),
                "fields": component.get("fields", {}),
                "properties": component.get("properties", {}),
                "libsource": component.get("libsource", {}),
                "tstamp": component.get("tstamp"),
                "pins": pins,
            },
            "provenance": {
                "project_file": str(project_file),
                "root_schematic": str(root_schematic),
                "sheet_path_names": component["sheetpath"].get("names") or "/",
                "sheet_path_tstamps": sheet_tstamps,
                "source_sheet_id": sheet_instance.get("source_sheet_id"),
                "source_sheet_file": sheet_instance.get("source_file"),
                "source_symbols": source_symbols,
            },
            "relationships": {
                "sheet_id": sheet_instance["id"],
                "net_ids": [],
                "connected_component_ids": [],
                "interface_ids": [],
            },
            "derived": {
                "sheet_display_name": sheet_instance["display_name"],
                "sheet_depth": xml_path_depth(sheet_tstamps),
            },
        }
        sheet_instance["component_ids"].append(component_id)

    for sheet in sheet_instances.values():
        sheet["component_ids"] = sorted(
            sheet["component_ids"], key=lambda item: natural_ref_key(components[item]["ref"])
        )

    nets: dict[str, dict[str, Any]] = {}
    nets_by_name: dict[str, dict[str, Any]] = {}

    for net_name, net in nets_xml.items():
        net_id = stable_net_id(net.get("code"), net_name)
        nodes: list[dict[str, Any]] = []
        component_ids_on_net: list[str] = []
        sheet_ids_on_net: list[str] = []
        interface_nodes = 0

        for node in net["nodes"]:
            ref = node.get("ref")
            component_id = component_id_by_ref.get(ref) if ref else None
            sheet_id = component_sheet_lookup.get(component_id) if component_id else None
            if component_id:
                component_ids_on_net.append(component_id)
            if sheet_id:
                sheet_ids_on_net.append(sheet_id)
            if pin_type_supports_interface(node.get("pintype")):
                interface_nodes += 1
            nodes.append(
                {
                    "ref": ref,
                    "component_id": component_id,
                    "pin": node.get("pin"),
                    "pinfunction": node.get("pinfunction"),
                    "pintype": node.get("pintype"),
                    "sheet_id": sheet_id,
                }
            )

        component_ids_sorted = sorted(
            set(component_ids_on_net), key=lambda item: natural_ref_key(components[item]["ref"])
        )
        sheet_ids_sorted = sorted(set(sheet_ids_on_net))
        is_unconnected = net_name.startswith("unconnected-")
        nets[net_id] = {
            "id": net_id,
            "name": net_name,
            "imported": {
                "code": net.get("code"),
                "nodes": nodes,
            },
            "provenance": {
                "project_file": str(project_file),
                "root_schematic": str(root_schematic),
                "original_net_code": net.get("code"),
            },
            "relationships": {
                "component_ids": component_ids_sorted,
                "sheet_ids": sheet_ids_sorted,
                "net_group_ids": [],
                "interface_ids": [],
            },
            "derived": {
                "node_count": len(nodes),
                "component_count": len(component_ids_sorted),
                "sheet_count": len(sheet_ids_sorted),
                "interface_node_count": interface_nodes,
                "has_interface_pins": interface_nodes > 0 and not is_unconnected,
                "is_explicitly_unconnected": is_unconnected,
            },
        }
        nets_by_name[net_name] = nets[net_id]
        for component_id in component_ids_sorted:
            components[component_id]["relationships"]["net_ids"].append(net_id)
        for sheet_id in sheet_ids_sorted:
            for sheet in sheet_instances.values():
                if sheet["id"] == sheet_id:
                    sheet["net_ids"].append(net_id)
                    break

    for component in components.values():
        component["relationships"]["net_ids"].sort(key=lambda net_id: nets[net_id]["name"])
    for sheet in sheet_instances.values():
        sheet["net_ids"] = sorted(set(sheet["net_ids"]), key=lambda net_id: nets[net_id]["name"])

    explicit_groups, explicit_memberships = dedupe_explicit_groups(
        schematic_model["explicit_groups_raw"],
        nets_by_name,
        source_sheets,
    )
    inferred_group_templates, _ = infer_numbered_net_groups(nets_by_name)
    inferred_groups: list[dict[str, Any]] = []
    inferred_memberships: dict[str, list[str]] = defaultdict(list)
    for group in inferred_group_templates:
        group_id = stable_net_group_id("inferred", group["name"])
        members: list[dict[str, Any]] = []
        for member in group["members"]:
            net_name = member["net_name"]
            resolved_net = nets_by_name.get(net_name)
            member_entry = {
                "net_name": net_name,
                "index": member["index"],
                "resolved_net_id": resolved_net["id"] if resolved_net else None,
            }
            members.append(member_entry)
            if resolved_net:
                inferred_memberships[resolved_net["id"]].append(group_id)
        inferred_groups.append(
            {
                "id": group_id,
                "kind": group["kind"],
                "origin": group["origin"],
                "name": group["name"],
                "base_name": group["base_name"],
                "members": members,
                "indices": group["indices"],
                "index_min": group["index_min"],
                "index_max": group["index_max"],
                "width": group["width"],
                "is_contiguous": group["is_contiguous"],
                "provenance": group["provenance"],
            }
        )

    net_groups = sorted(
        explicit_groups + inferred_groups,
        key=lambda item: (item["origin"], item["name"], item["id"]),
    )
    for net_id, group_ids in explicit_memberships.items():
        nets[net_id]["relationships"]["net_group_ids"].extend(group_ids)
    for net_id, group_ids in inferred_memberships.items():
        nets[net_id]["relationships"]["net_group_ids"].extend(group_ids)
    for net in nets.values():
        net["relationships"]["net_group_ids"] = sorted(set(net["relationships"]["net_group_ids"]))

    suppressed_exact_interface_pair_count = 0
    high_fanout_interface_net_ids: list[str] = []
    for net in nets.values():
        component_count = len(net["relationships"]["component_ids"])
        exact_pairwise_interface_count = 0
        if net["derived"]["has_interface_pins"] and component_count >= 2:
            exact_pairwise_interface_count = component_count * (component_count - 1) // 2
        is_high_fanout_interface_net = (
            net["derived"]["has_interface_pins"] and component_count > COMPACT_INTERFACE_MAX_COMPONENTS_PER_NET
        )
        net["derived"]["exact_pairwise_interface_count"] = exact_pairwise_interface_count
        net["derived"]["is_high_fanout_interface_net"] = is_high_fanout_interface_net
        if is_high_fanout_interface_net:
            high_fanout_interface_net_ids.append(net["id"])
            suppressed_exact_interface_pair_count += exact_pairwise_interface_count

    compact_interface_map: dict[tuple[str, str], dict[str, Any]] = {}
    compact_component_neighbors: dict[str, set[str]] = defaultdict(set)
    for net in nets.values():
        if net["derived"]["is_explicitly_unconnected"]:
            continue
        component_ids = net["relationships"]["component_ids"]
        if len(component_ids) < 2:
            continue
        if len(component_ids) <= COMPACT_INTERFACE_MAX_COMPONENTS_PER_NET:
            for component_id_a, component_id_b in combinations(component_ids, 2):
                compact_component_neighbors[component_id_a].add(component_id_b)
                compact_component_neighbors[component_id_b].add(component_id_a)
        if not net["derived"]["has_interface_pins"] or net["derived"]["is_high_fanout_interface_net"]:
            continue
        for component_id_a, component_id_b in combinations(component_ids, 2):
            key = tuple(sorted((component_id_a, component_id_b)))
            entry = compact_interface_map.setdefault(
                key,
                {
                    "id": stable_interface_id(key[0], key[1]),
                    "component_id_a": key[0],
                    "component_id_b": key[1],
                    "shared_net_ids": [],
                },
            )
            entry["shared_net_ids"].append(net["id"])

    interfaces: list[dict[str, Any]] = []
    for entry in compact_interface_map.values():
        shared_net_ids = sorted(entry["shared_net_ids"], key=lambda net_id: nets[net_id]["name"])
        shared_group_ids = sorted(
            {group_id for net_id in shared_net_ids for group_id in nets[net_id]["relationships"]["net_group_ids"]}
        )
        direct_net_ids = [net_id for net_id in shared_net_ids if not nets[net_id]["relationships"]["net_group_ids"]]
        interface = {
            "id": entry["id"],
            "component_id_a": entry["component_id_a"],
            "component_id_b": entry["component_id_b"],
            "component_ref_a": components[entry["component_id_a"]]["ref"],
            "component_ref_b": components[entry["component_id_b"]]["ref"],
            "shared_net_count": len(shared_net_ids),
            "shared_net_ids": shared_net_ids,
            "shared_group_ids": shared_group_ids,
            "direct_net_ids": direct_net_ids,
        }
        interfaces.append(interface)
        components[entry["component_id_a"]]["relationships"]["interface_ids"].append(interface["id"])
        components[entry["component_id_b"]]["relationships"]["interface_ids"].append(interface["id"])
        for net_id in shared_net_ids:
            nets[net_id]["relationships"]["interface_ids"].append(interface["id"])

    interfaces.sort(
        key=lambda item: (
            -item["shared_net_count"],
            natural_ref_key(item["component_ref_a"]),
            natural_ref_key(item["component_ref_b"]),
        )
    )

    interface_by_id = {item["id"]: item for item in interfaces}
    for component in components.values():
        component["relationships"]["connected_component_ids"] = sorted(
            compact_component_neighbors.get(component["id"], set()),
            key=lambda item: natural_ref_key(components[item]["ref"]),
        )
        component["relationships"]["interface_ids"] = sorted(component["relationships"]["interface_ids"])
        component["relationships"]["high_fanout_net_ids"] = sorted(
            [
                net_id
                for net_id in component["relationships"]["net_ids"]
                if nets[net_id]["derived"]["is_high_fanout_interface_net"]
            ],
            key=lambda net_id: nets[net_id]["name"],
        )

        pin_entries: list[dict[str, Any]] = []
        for pin_entry in component["imported"]["pins"]:
            net_summaries = []
            for net_name in pin_entry["nets"]:
                net = nets_by_name.get(net_name)
                if not net:
                    continue
                peer_component_ids = sorted(
                    {
                        node["component_id"]
                        for node in net["imported"]["nodes"]
                        if node.get("component_id")
                        and not (node.get("component_id") == component["id"] and node.get("pin") == pin_entry["pin"])
                    }
                )
                summary = {
                    "net_id": net["id"],
                    "net_name": net["name"],
                    "net_group_ids": sorted(set(net["relationships"]["net_group_ids"])),
                    "peer_component_count": len(peer_component_ids),
                    "has_interface_pins": net["derived"]["has_interface_pins"],
                    "is_high_fanout_interface_net": net["derived"]["is_high_fanout_interface_net"],
                }
                if (
                    peer_component_ids
                    and not net["derived"]["is_high_fanout_interface_net"]
                    and len(net["relationships"]["component_ids"]) <= COMPACT_INTERFACE_MAX_COMPONENTS_PER_NET
                ):
                    summary["peer_component_ids"] = peer_component_ids
                    summary["peer_refs"] = sorted(
                        [components[component_id]["ref"] for component_id in peer_component_ids],
                        key=natural_ref_key,
                    )
                net_summaries.append(summary)

            pin_derived: dict[str, Any] = {"net_summaries": net_summaries}
            if len(net_summaries) == 1:
                for field in (
                    "net_id",
                    "net_name",
                    "net_group_ids",
                    "peer_component_count",
                    "has_interface_pins",
                    "is_high_fanout_interface_net",
                    "peer_component_ids",
                    "peer_refs",
                ):
                    if field in net_summaries[0]:
                        pin_derived[field] = net_summaries[0][field]
            pin_entries.append(
                {
                    **pin_entry,
                    "derived": pin_derived,
                }
            )
        component["imported"]["pins"] = sorted(pin_entries, key=lambda item: pin_sort_key(item["pin"]))
        component["derived"]["interface_net_ids"] = sorted(
            {
                net_id
                for interface_id in component["relationships"]["interface_ids"]
                for net_id in interface_by_id[interface_id]["shared_net_ids"]
            },
            key=lambda net_id: nets[net_id]["name"],
        )
        component["derived"]["interface_net_count"] = len(component["derived"]["interface_net_ids"])
        component["derived"]["high_fanout_net_count"] = len(component["relationships"]["high_fanout_net_ids"])

    for net in nets.values():
        net["relationships"]["interface_ids"] = sorted(set(net["relationships"]["interface_ids"]))
        net["relationships"]["component_ids"] = sorted(
            net["relationships"]["component_ids"],
            key=lambda item: natural_ref_key(components[item]["ref"]),
        )
        net["derived"]["compact_interface_count"] = len(net["relationships"]["interface_ids"])

    source_sheet_objects: dict[str, dict[str, Any]] = {}
    for source_sheet_id, source_sheet in source_sheets.items():
        instance_ids = sorted(
            [
                instance["id"]
                for instance in sheet_instances.values()
                if instance.get("source_sheet_id") == source_sheet_id
            ]
        )
        explicit_bus_declarations = [
            group for group in source_sheet["explicit_bus_declarations"] if group["kind"] == "bus_declaration"
        ]
        distilled_source = distill_source_sheet_view(source_sheet)
        source_sheet_objects[source_sheet_id] = {
            "id": source_sheet_id,
            "artifact_type": "source_sheet",
            "artifact_schema": f"{SPLIT_LAYOUT_VERSION}/source_sheet",
            "imported": {
                "relative_file": source_sheet["relative_file"],
                "absolute_file": source_sheet["absolute_file"],
                "uuid": source_sheet["uuid"],
                "version": source_sheet["version"],
                "generator": source_sheet["generator"],
                "generator_version": source_sheet["generator_version"],
                "paper": source_sheet["paper"],
                "title_block": source_sheet["title_block"],
                "top_level_tag_counts": source_sheet["top_level_tag_counts"],
            },
            "provenance": {
                "project_file": str(project_file),
                "root_schematic": str(root_schematic),
            },
            "relationships": {
                "sheet_instance_ids": instance_ids,
            },
            "derived": {
                "label_count": len(source_sheet["labels"]),
                "symbol_count": len(source_sheet["symbols"]),
                "child_sheet_count": len(source_sheet["sheets"]),
                "explicit_bus_declaration_count": len(explicit_bus_declarations),
                "no_connect_count": len(source_sheet["no_connects"]),
                "sheet_instance_count": len(instance_ids),
                "label_text_count": len(distilled_source["labels_by_text"]),
                "symbol_ref_count": len(distilled_source["symbol_instances_by_ref"]),
            },
            "source": distilled_source,
        }

    sheet_objects: dict[str, dict[str, Any]] = {}
    for path_tstamps, sheet in sorted(sheet_instances.items(), key=lambda item: (xml_path_depth(item[0]), item[0])):
        source_sheet_id = sheet.get("source_sheet_id")
        sheet_objects[sheet["id"]] = {
            "id": sheet["id"],
            "artifact_type": "sheet",
            "artifact_schema": f"{SPLIT_LAYOUT_VERSION}/sheet",
            "key": {
                "path_tstamps": path_tstamps,
                "path_names": sheet["path_names"],
                "display_name": sheet["display_name"],
            },
            "imported": {
                "page": sheet.get("page"),
                "placement": sheet.get("placement"),
            },
            "provenance": {
                "project_file": str(project_file),
                "root_schematic": str(root_schematic),
                "source_sheet_id": source_sheet_id,
                "source_sheet_file": sheet.get("source_file"),
                "source_sheet_uuid": sheet.get("source_uuid"),
                "placement_sheet_uuid": sheet.get("placement_sheet_uuid"),
                "schematic_instance_path": sheet.get("schematic_instance_path"),
            },
            "relationships": {
                "parent_id": sheet.get("parent_id"),
                "child_ids": sorted(sheet.get("child_ids", [])),
                "component_ids": list(sheet.get("component_ids", [])),
                "net_ids": list(sheet.get("net_ids", [])),
                "source_sheet_id": source_sheet_id,
            },
            "derived": {
                "component_count": len(sheet.get("component_ids", [])),
                "net_count": len(sheet.get("net_ids", [])),
                "depth": xml_path_depth(path_tstamps),
                "png": sheet.get("png"),
            },
        }

    component_objects: dict[str, dict[str, Any]] = {}
    for component_id, component in components.items():
        component_objects[component_id] = {
            "id": component_id,
            "artifact_type": "component",
            "artifact_schema": f"{SPLIT_LAYOUT_VERSION}/component",
            "key": {"ref": component["ref"]},
            "imported": component["imported"],
            "provenance": component["provenance"],
            "relationships": component["relationships"],
            "derived": component["derived"],
        }

    net_objects: dict[str, dict[str, Any]] = {}
    for net_id, net in nets.items():
        net_objects[net_id] = {
            "id": net_id,
            "artifact_type": "net",
            "artifact_schema": f"{SPLIT_LAYOUT_VERSION}/net",
            "key": {"name": net["name"]},
            "imported": net["imported"],
            "provenance": net["provenance"],
            "relationships": net["relationships"],
            "derived": net["derived"],
        }

    high_fanout_interface_nets = [
        {
            "net_id": net["id"],
            "net_name": net["name"],
            "component_count": len(net["relationships"]["component_ids"]),
            "exact_pairwise_interface_count": net["derived"]["exact_pairwise_interface_count"],
            "path": net_artifact_relpath(net_objects[net["id"]]),
        }
        for net in sorted(
            [nets[net_id] for net_id in high_fanout_interface_net_ids],
            key=lambda item: (-item["derived"]["exact_pairwise_interface_count"], item["name"]),
        )
    ]

    adjacency = {
        "component_to_nets": {
            component_id: component["relationships"]["net_ids"]
            for component_id, component in sorted(
                component_objects.items(), key=lambda item: natural_ref_key(item[1]["key"]["ref"])
            )
        },
        "net_to_components": {
            net_id: net["relationships"]["component_ids"]
            for net_id, net in sorted(net_objects.items(), key=lambda item: item[1]["key"]["name"])
        },
        "component_to_components": {
            component_id: component["relationships"]["connected_component_ids"]
            for component_id, component in sorted(
                component_objects.items(), key=lambda item: natural_ref_key(item[1]["key"]["ref"])
            )
        },
        "sheet_to_components": {
            sheet_id: sheet["relationships"]["component_ids"]
            for sheet_id, sheet in sorted(sheet_objects.items(), key=lambda item: item[1]["key"]["path_tstamps"])
        },
        "sheet_to_nets": {
            sheet_id: sheet["relationships"]["net_ids"]
            for sheet_id, sheet in sorted(sheet_objects.items(), key=lambda item: item[1]["key"]["path_tstamps"])
        },
    }

    counts = {
        "component_count": len(component_objects),
        "net_count": len(net_objects),
        "sheet_instance_count": len(sheet_objects),
        "source_sheet_count": len(source_sheet_objects),
        "interface_count": len(interfaces),
        "net_group_count": len(net_groups),
        "suppressed_high_fanout_interface_net_count": len(high_fanout_interface_nets),
        "suppressed_exact_interface_pair_count": suppressed_exact_interface_pair_count,
    }

    diagnostics = {
        "warnings": warnings,
        "unsupported_constructs": schematic_model["unsupported_constructs"],
        "interface_compaction": {
            "max_components_per_pairwise_net": COMPACT_INTERFACE_MAX_COMPONENTS_PER_NET,
            "suppressed_high_fanout_interface_nets": high_fanout_interface_nets,
        },
        "stats": {
            **counts,
            **parsed_xml["xml_diagnostics"],
        },
    }

    return {
        "bundle_schema": SPLIT_LAYOUT_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "project": {
            "project_name": project_file.stem,
            "project_dir": str(project_dir),
            "project_file": str(project_file),
            "root_schematic": str(root_schematic),
        },
        "design": parsed_xml.get("design", {}),
        "counts": counts,
        "diagnostics": diagnostics,
        "source_sheets": source_sheet_objects,
        "sheets": sheet_objects,
        "components": component_objects,
        "nets": net_objects,
        "interfaces": interfaces,
        "net_groups": net_groups,
        "indexes": adjacency,
    }


def validate_model(model: dict[str, Any]) -> None:
    component_ids = set(model["components"].keys())
    net_ids = set(model["nets"].keys())
    sheet_ids = set(model["sheets"].keys())
    source_sheet_ids = set(model["source_sheets"].keys())
    interface_ids = {item["id"] for item in model["interfaces"]}
    net_group_ids = {item["id"] for item in model["net_groups"]}

    def ensure(condition: bool, message: str) -> None:
        if not condition:
            raise KiCad2LLMError(message)

    for component in model["components"].values():
        ensure(
            component["relationships"]["sheet_id"] in sheet_ids,
            f"component references missing sheet: {component['id']}",
        )
        ensure(
            set(component["relationships"]["net_ids"]).issubset(net_ids),
            f"component references missing nets: {component['id']}",
        )
        ensure(
            set(component["relationships"]["connected_component_ids"]).issubset(component_ids),
            f"component references missing components: {component['id']}",
        )
        ensure(
            set(component["relationships"]["interface_ids"]).issubset(interface_ids),
            f"component references missing interfaces: {component['id']}",
        )
        provenance_source_sheet_id = component["provenance"].get("source_sheet_id")
        ensure(
            provenance_source_sheet_id is None or provenance_source_sheet_id in source_sheet_ids,
            f"component references missing source sheet: {component['id']}",
        )

    for net in model["nets"].values():
        ensure(
            set(net["relationships"]["component_ids"]).issubset(component_ids),
            f"net references missing components: {net['id']}",
        )
        ensure(
            set(net["relationships"]["sheet_ids"]).issubset(sheet_ids), f"net references missing sheets: {net['id']}"
        )
        ensure(
            set(net["relationships"]["net_group_ids"]).issubset(net_group_ids),
            f"net references missing net groups: {net['id']}",
        )
        ensure(
            set(net["relationships"]["interface_ids"]).issubset(interface_ids),
            f"net references missing interfaces: {net['id']}",
        )

    for sheet in model["sheets"].values():
        parent_id = sheet["relationships"]["parent_id"]
        ensure(parent_id is None or parent_id in sheet_ids, f"sheet references missing parent: {sheet['id']}")
        ensure(
            set(sheet["relationships"]["child_ids"]).issubset(sheet_ids),
            f"sheet references missing children: {sheet['id']}",
        )
        ensure(
            set(sheet["relationships"]["component_ids"]).issubset(component_ids),
            f"sheet references missing components: {sheet['id']}",
        )
        ensure(
            set(sheet["relationships"]["net_ids"]).issubset(net_ids), f"sheet references missing nets: {sheet['id']}"
        )
        source_sheet_id = sheet["relationships"]["source_sheet_id"]
        ensure(
            source_sheet_id is None or source_sheet_id in source_sheet_ids,
            f"sheet references missing source sheet: {sheet['id']}",
        )

    for source_sheet in model["source_sheets"].values():
        ensure(
            set(source_sheet["relationships"]["sheet_instance_ids"]).issubset(sheet_ids),
            f"source sheet references missing sheet instances: {source_sheet['id']}",
        )

    for interface in model["interfaces"]:
        ensure(
            interface["component_id_a"] in component_ids, f"interface references missing component: {interface['id']}"
        )
        ensure(
            interface["component_id_b"] in component_ids, f"interface references missing component: {interface['id']}"
        )
        ensure(
            set(interface["shared_net_ids"]).issubset(net_ids), f"interface references missing nets: {interface['id']}"
        )
        ensure(
            set(interface["shared_group_ids"]).issubset(net_group_ids),
            f"interface references missing net groups: {interface['id']}",
        )

    for group in model["net_groups"]:
        for member in group["members"]:
            resolved_net_id = member.get("resolved_net_id")
            ensure(
                resolved_net_id is None or resolved_net_id in net_ids,
                f"net group references missing net: {group['id']}",
            )

    indexes = model["indexes"]
    ensure(set(indexes["component_to_nets"].keys()) == component_ids, "component_to_nets keys do not match components")
    ensure(set(indexes["net_to_components"].keys()) == net_ids, "net_to_components keys do not match nets")
    ensure(
        set(indexes["component_to_components"].keys()) == component_ids,
        "component_to_components keys do not match components",
    )
    ensure(set(indexes["sheet_to_components"].keys()) == sheet_ids, "sheet_to_components keys do not match sheets")
    ensure(set(indexes["sheet_to_nets"].keys()) == sheet_ids, "sheet_to_nets keys do not match sheets")


def build_object_index(model: dict[str, Any]) -> dict[str, Any]:
    components = [
        {
            "id": component["id"],
            "ref": component["key"]["ref"],
            "sheet_id": component["relationships"]["sheet_id"],
            "path": component_artifact_relpath(component),
        }
        for component in sorted(model["components"].values(), key=lambda item: natural_ref_key(item["key"]["ref"]))
    ]
    nets = [
        {
            "id": net["id"],
            "name": net["key"]["name"],
            "path": net_artifact_relpath(net),
        }
        for net in sorted(model["nets"].values(), key=lambda item: item["key"]["name"])
    ]
    sheets = [
        {
            "id": sheet["id"],
            "path_tstamps": sheet["key"]["path_tstamps"],
            "path_names": sheet["key"]["path_names"],
            "display_name": sheet["key"]["display_name"],
            "png": sheet["derived"]["png"],
            "path": sheet_artifact_relpath(sheet),
        }
        for sheet in sorted(model["sheets"].values(), key=lambda item: item["key"]["path_tstamps"])
    ]
    source_sheets = [
        {
            "id": source_sheet["id"],
            "relative_file": source_sheet["imported"]["relative_file"],
            "path": source_sheet_artifact_relpath(source_sheet),
        }
        for source_sheet in sorted(model["source_sheets"].values(), key=lambda item: item["imported"]["relative_file"])
    ]
    return {
        "components": components,
        "nets": nets,
        "sheets": sheets,
        "source_sheets": source_sheets,
    }


def component_artifact_filename(component: dict[str, Any]) -> str:
    ref = component["key"]["ref"]
    if "/" not in ref and ref not in {".", ".."}:
        return f"{ref}.json"
    return f"{safe_filename(ref, 'component')}--{component['id']}.json"


def net_artifact_filename(net: dict[str, Any]) -> str:
    return f"{artifact_slug_from_net_name(net['key']['name'])}--{net['id']}.json"


def sheet_artifact_filename(sheet: dict[str, Any]) -> str:
    return (
        f"{artifact_slug_from_sheet_path(sheet['key']['path_names'], sheet['key']['display_name'])}--{sheet['id']}.json"
    )


def source_sheet_artifact_filename(source_sheet: dict[str, Any]) -> str:
    stem = Path(source_sheet["imported"]["relative_file"]).stem
    return f"{safe_filename(stem, 'source-sheet')}--{source_sheet['id']}.json"


def component_artifact_relpath(component: dict[str, Any]) -> str:
    return f"{COMPONENTS_DIRNAME}/{component_artifact_filename(component)}"


def net_artifact_relpath(net: dict[str, Any]) -> str:
    return f"{NETS_DIRNAME}/{net_artifact_filename(net)}"


def sheet_artifact_relpath(sheet: dict[str, Any]) -> str:
    return f"{SHEETS_DIRNAME}/{sheet_artifact_filename(sheet)}"


def source_sheet_artifact_relpath(source_sheet: dict[str, Any]) -> str:
    return f"{SOURCE_SHEETS_DIRNAME}/{source_sheet_artifact_filename(source_sheet)}"


def adjacency_index_relpath(index_name: str) -> str:
    return f"{INDEXES_DIRNAME}/{ADJACENCY_INDEX_FILENAMES[index_name]}"


def write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_jsonl_file(path: Path, rows: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_manifest(
    model: dict[str, Any],
    write_jsonl: bool,
) -> dict[str, Any]:
    object_index = build_object_index(model)
    manifest = {
        "bundle_schema": model["bundle_schema"],
        "artifact_type": "manifest",
        "artifact_schema": f"{SPLIT_LAYOUT_VERSION}/manifest",
        "generated_at_utc": model["generated_at_utc"],
        "project": model["project"],
        "design": model["design"],
        "counts": model["counts"],
        "diagnostics": model["diagnostics"],
        "artifacts": {
            "layout": "split",
            "optional_sidecars": {"jsonl": write_jsonl},
            "paths": {
                "agent_guide": GUIDE_NAME,
                "interfaces": INTERFACES_NAME,
                "net_groups": NET_GROUPS_NAME,
                "object_index": f"{INDEXES_DIRNAME}/{OBJECT_INDEX_NAME}",
                "adjacency_indexes": {
                    index_name: adjacency_index_relpath(index_name) for index_name in ADJACENCY_INDEX_FILENAMES
                },
            },
        },
        "indexes": object_index,
        "schemas": {
            "dir": SCHEMAS_DIRNAME,
            "files": {
                "manifest": f"{SCHEMAS_DIRNAME}/manifest.schema.json",
                "component": f"{SCHEMAS_DIRNAME}/component.schema.json",
                "net": f"{SCHEMAS_DIRNAME}/net.schema.json",
                "sheet": f"{SCHEMAS_DIRNAME}/sheet.schema.json",
                "source_sheet": f"{SCHEMAS_DIRNAME}/source_sheet.schema.json",
                "object_index": f"{SCHEMAS_DIRNAME}/object_index.schema.json",
                "adjacency_map": f"{SCHEMAS_DIRNAME}/adjacency_map.schema.json",
            },
        },
    }
    manifest["artifacts"]["paths"]["png_dir"] = PNG_DIRNAME
    if write_jsonl:
        manifest["artifacts"]["paths"]["jsonl_dir"] = JSONL_DIRNAME
    return manifest


def write_agent_guide(path: Path, manifest: dict[str, Any], verbose: bool, args: argparse.Namespace) -> None:
    project = manifest["project"]
    counts = manifest["counts"]
    command_bits = ["python", "kicad2llm.py"]
    if args.jsonl:
        command_bits.append("--jsonl")
    if verbose:
        command_bits.append("-v")
    command_bits.append(shlex_quote(args.project_dir))

    readme = f"""# Agents Must Read

This directory contains a source-traceable export of a KiCad project intended for coding and review agents.

## Source project

- Project name: `{project['project_name']}`
- Project file: `{project['project_file']}`
- Root schematic: `{project['root_schematic']}`

## Bundle layout

- `{MANIFEST_NAME}` — manifest/index for the split layout
- `{GUIDE_NAME}` — this file
- `{INTERFACES_NAME}` — component-to-component interface summaries
- `{NET_GROUPS_NAME}` — explicit and inferred bus/net-group summaries
- `{INDEXES_DIRNAME}/{OBJECT_INDEX_NAME}` — object lookup index
- `{INDEXES_DIRNAME}/component_to_nets.json` — component-to-net adjacency
- `{INDEXES_DIRNAME}/net_to_components.json` — net-to-component adjacency
- `{INDEXES_DIRNAME}/component_to_components.json` — component-to-component adjacency
- `{INDEXES_DIRNAME}/sheet_to_components.json` — sheet-to-component adjacency
- `{INDEXES_DIRNAME}/sheet_to_nets.json` — sheet-to-net adjacency
- `{COMPONENTS_DIRNAME}/` — per-component artifacts
- `{NETS_DIRNAME}/` — per-net artifacts
- `{SHEETS_DIRNAME}/` — per-sheet-instance artifacts
- `{SOURCE_SHEETS_DIRNAME}/` — per-source-schematic artifacts
- `{SCHEMAS_DIRNAME}/` — JSON Schema documents
- `{PNG_DIRNAME}/` — schematic sheet renders
- `{JSONL_DIRNAME}/` — optional JSONL sidecars when `--jsonl` is used

## Quick Start

1. Open `{MANIFEST_NAME}` first to see the available artifact paths, counts, and indexes.
2. For connectivity questions, start with `{INTERFACES_NAME}`, `{NET_GROUPS_NAME}`, and `{INDEXES_DIRNAME}/component_to_components.json`.
3. For a specific part or net, first try the human-readable filenames in `{COMPONENTS_DIRNAME}/`, `{NETS_DIRNAME}/`, and `{SHEETS_DIRNAME}/`; fall back to `{INDEXES_DIRNAME}/{OBJECT_INDEX_NAME}` if needed.
4. Use `{INDEXES_DIRNAME}/component_to_nets.json` and `{INDEXES_DIRNAME}/net_to_components.json` when you need exact traversal without opening many artifacts.
5. For hierarchy, labels, buses, repeated sheets, and no-connect markers, use `{SHEETS_DIRNAME}/` and `{SOURCE_SHEETS_DIRNAME}/`.
6. Use `{PNG_DIRNAME}/` only when visual page layout matters.

## Best Usage Practices

- Treat `imported` as source facts from KiCad exports.
- Treat `provenance` as the trace-back path into the original project.
- Treat `relationships` and `derived` as convenience structure for navigation and analysis.
- Prefer explicit net groups over inferred ones when both exist.
- Avoid scanning the whole export if the indexes already tell you where to look.
- Treat `{INTERFACES_NAME}` as a compact interface view. High-fanout rails are intentionally suppressed from pairwise expansion.
- Use `{INDEXES_DIRNAME}/{OBJECT_INDEX_NAME}` as the main resolver when you know a ref, net name, or sheet path but not the artifact filename.
- Use `{INDEXES_DIRNAME}/component_to_components.json` for fast “who talks to whom” questions, then open the corresponding component artifacts to inspect the actual pins and nets.
- Use `{INDEXES_DIRNAME}/component_to_nets.json` and `{INDEXES_DIRNAME}/net_to_components.json` for exact traversals when the compact interface view is too lossy.
- Start from `net_groups.json` when you suspect a bus, numbered interface, or grouped signal family.
- Open the sheet artifact before the source-sheet artifact when you care about one instantiated sheet in a repeated hierarchy.
- Open the source-sheet artifact before the sheet artifact when you care about reusable page-level structure such as labels, child sheets, or symbol placements.
- Expect power rails and other high-fanout nets to be present in net artifacts even when they are intentionally suppressed from pairwise interface summaries.
- Use the PNG together with the sheet artifact when label scope, symbol placement, or connector grouping is visually important.
- Trust the XML-derived connectivity over graphical impressions if there is any disagreement between the rendered sheet and the structured data.
- Follow provenance links when an object looks ambiguous: component -> sheet -> source sheet is usually the shortest path back to KiCad intent.
- Check `diagnostics.warnings` in `{MANIFEST_NAME}` before assuming the export is complete if something appears to be missing.
- If something seems ambiguous, compare the relevant object artifact, its sheet artifact, and the PNG for that sheet.

## Recommended Workflow

1. Open `{MANIFEST_NAME}` and skim `counts`, `diagnostics`, and `artifacts.paths`.
2. Resolve the object you care about through `{INDEXES_DIRNAME}/{OBJECT_INDEX_NAME}` unless the filename is already obvious.
3. For cross-component questions, inspect `{INTERFACES_NAME}` first, then confirm with the relevant adjacency index and object artifacts.
4. For “where is this signal used?” questions, open the net artifact and then walk outward through `{INDEXES_DIRNAME}/net_to_components.json`.
5. For hierarchy questions, move from component or net -> sheet artifact -> source-sheet artifact -> PNG.
6. If repeated sheets are involved, always distinguish the sheet instance artifact from the reusable source-sheet artifact.

## Common Query Patterns

- “What is this component connected to?”: use `{INDEXES_DIRNAME}/component_to_components.json`, then open the component artifact and relevant net artifacts.
- “Which pins of this component participate in an interface?”: open the component artifact and inspect `imported.pins[*].derived`.
- “What belongs to this bus?”: start with `{NET_GROUPS_NAME}`, then open member net artifacts.
- “Which sheet contains this component or net?”: use the object artifact `relationships` and then open the referenced sheet artifact.
- “Why is a rail not shown in the interface view?”: inspect the relevant net artifact and `bundle.json` diagnostics; high-fanout nets are intentionally compacted.
- “Where did this object come from in the original KiCad project?”: follow `provenance` fields back to the source sheet file, instance path, and symbol placement.

## Practical Caveats

- Interface summaries are intentionally optimized for signal-bearing relationships, not exhaustive pairwise expansion of every shared rail.
- Net groups can include explicit bus-derived groups and inferred numbered groups; explicit groups are usually the better starting point.
- Human-readable filenames are conveniences, not stable identifiers; use object IDs for durable cross-references.
- PNG filenames help with visual lookup, but the structured sheet artifacts are the authoritative way to resolve hierarchy and instance identity.
- When a component appears multiple times through hierarchy, the sheet instance path matters; do not assume the source sheet alone uniquely identifies it.

## Summary

- Components: {counts['component_count']}
- Nets: {counts['net_count']}
- Sheet instances: {counts['sheet_instance_count']}
- Source schematics: {counts['source_sheet_count']}
- Interfaces: {counts['interface_count']}
- Net groups: {counts['net_group_count']}

## Design metadata extracted from the XML netlist

```json
{json.dumps(manifest.get('design', {}), indent=2, ensure_ascii=False)}
```

## Notes

- XML netlist export remains the authoritative connectivity source.
- `.kicad_sch` parsing is used only to recover structure and provenance that XML omits.
- PNG sheet renders are generated with CairoSVG when possible and retried with Inkscape if CairoSVG is unavailable
  or fails during rendering.
- `net_groups.json` distinguishes explicit KiCad bus-derived groups from inferred numbered-net groups.
- `interfaces.json` is built from shared nets that include at least one non-passive, non-power pin type and are not high-fanout interface rails.
- Suppressed high-fanout interface nets are listed in `bundle.json` diagnostics and remain visible in their net artifacts.

## How this was generated

```bash
{' '.join(command_bits)}
```
"""
    path.write_text(readme, encoding="utf-8")


def build_schema_documents() -> dict[str, dict[str, Any]]:
    base_defs = {
        "id_string": {"type": "string", "minLength": 1},
        "relpath": {"type": "string", "minLength": 1},
    }

    artifact_base = {
        "type": "object",
        "required": ["id", "artifact_type", "artifact_schema", "imported", "provenance", "relationships", "derived"],
        "properties": {
            "id": {"$ref": "#/$defs/id_string"},
            "artifact_type": {"type": "string"},
            "artifact_schema": {"type": "string"},
            "key": {"type": "object"},
            "imported": {"type": "object"},
            "provenance": {"type": "object"},
            "relationships": {"type": "object"},
            "derived": {"type": "object"},
            "source": {"type": "object"},
        },
        "additionalProperties": True,
        "$defs": base_defs,
    }

    return {
        "manifest.schema.json": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{SPLIT_LAYOUT_VERSION}/manifest",
            "type": "object",
            "required": [
                "bundle_schema",
                "artifact_type",
                "artifact_schema",
                "generated_at_utc",
                "project",
                "counts",
                "diagnostics",
                "artifacts",
                "indexes",
                "schemas",
            ],
            "properties": {
                "bundle_schema": {"const": SPLIT_LAYOUT_VERSION},
                "artifact_type": {"const": "manifest"},
                "artifact_schema": {"type": "string"},
                "generated_at_utc": {"type": "string"},
                "project": {"type": "object"},
                "design": {"type": "object"},
                "counts": {"type": "object"},
                "diagnostics": {"type": "object"},
                "artifacts": {"type": "object"},
                "indexes": {"type": "object"},
                "schemas": {"type": "object"},
            },
            "additionalProperties": True,
        },
        "component.schema.json": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{SPLIT_LAYOUT_VERSION}/component",
            **artifact_base,
        },
        "net.schema.json": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{SPLIT_LAYOUT_VERSION}/net",
            **artifact_base,
        },
        "sheet.schema.json": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{SPLIT_LAYOUT_VERSION}/sheet",
            **artifact_base,
        },
        "source_sheet.schema.json": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{SPLIT_LAYOUT_VERSION}/source_sheet",
            **artifact_base,
        },
        "object_index.schema.json": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{SPLIT_LAYOUT_VERSION}/object_index",
            "type": "object",
            "properties": {
                "components": {"type": "array"},
                "nets": {"type": "array"},
                "sheets": {"type": "array"},
                "source_sheets": {"type": "array"},
            },
            "additionalProperties": True,
        },
        "adjacency_map.schema.json": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{SPLIT_LAYOUT_VERSION}/adjacency_map",
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "items": {"$ref": "#/$defs/id_string"},
            },
            "$defs": base_defs,
        },
    }


def write_split_artifacts(model: dict[str, Any], manifest: dict[str, Any], out_dir: Path, write_jsonl: bool) -> None:
    write_json_file(out_dir / MANIFEST_NAME, manifest)
    write_json_file(out_dir / INTERFACES_NAME, model["interfaces"])
    write_json_file(out_dir / NET_GROUPS_NAME, model["net_groups"])
    write_json_file(out_dir / INDEXES_DIRNAME / OBJECT_INDEX_NAME, manifest["indexes"])
    for index_name, filename in ADJACENCY_INDEX_FILENAMES.items():
        write_json_file(out_dir / INDEXES_DIRNAME / filename, model["indexes"][index_name])

    for component in model["components"].values():
        write_json_file(out_dir / component_artifact_relpath(component), component)
    for net in model["nets"].values():
        write_json_file(out_dir / net_artifact_relpath(net), net)
    for sheet in model["sheets"].values():
        write_json_file(out_dir / sheet_artifact_relpath(sheet), sheet)
    for source_sheet in model["source_sheets"].values():
        write_json_file(out_dir / source_sheet_artifact_relpath(source_sheet), source_sheet)

    if write_jsonl:
        write_jsonl_file(
            out_dir / JSONL_DIRNAME / "components.jsonl",
            list(sorted(model["components"].values(), key=lambda item: natural_ref_key(item["key"]["ref"]))),
        )
        write_jsonl_file(
            out_dir / JSONL_DIRNAME / "nets.jsonl",
            list(sorted(model["nets"].values(), key=lambda item: item["key"]["name"])),
        )
        write_jsonl_file(
            out_dir / JSONL_DIRNAME / "sheets.jsonl",
            list(sorted(model["sheets"].values(), key=lambda item: item["key"]["path_tstamps"])),
        )
        write_jsonl_file(
            out_dir / JSONL_DIRNAME / "source_sheets.jsonl",
            list(sorted(model["source_sheets"].values(), key=lambda item: item["imported"]["relative_file"])),
        )
        write_jsonl_file(out_dir / JSONL_DIRNAME / "interfaces.jsonl", model["interfaces"])
        write_jsonl_file(out_dir / JSONL_DIRNAME / "net_groups.jsonl", model["net_groups"])


def write_schema_documents(out_dir: Path) -> None:
    for filename, payload in build_schema_documents().items():
        write_json_file(out_dir / SCHEMAS_DIRNAME / filename, payload)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log = configure_logging(args.verbose)

    try:
        project_dir = Path(args.project_dir).expanduser().resolve()
        project_file = autodetect_project_file(project_dir)
        root_schematic = project_file.with_suffix(".kicad_sch")
        if not root_schematic.is_file():
            raise KiCad2LLMError(
                "could not locate the root schematic next to the project file; " f"expected: {root_schematic}"
            )

        out_dir = project_file.parent / OUTDIR_NAME
        guide_file = out_dir / GUIDE_NAME
        png_dir = out_dir / PNG_DIRNAME

        kicad_cli = require_executable_in_path("kicad-cli")
        log.debug("Resolved kicad-cli: %s", kicad_cli)
        log.info("Project file: %s", project_file)
        log.info("Root schematic: %s", root_schematic)
        log.info("Output directory: %s", out_dir)

        prepare_output_dir(out_dir, log)
        png_paths: list[Path] = []
        png_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="kicad2llm-") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            xml_file = temp_dir / "netlist.xml"
            svg_dir = temp_dir / "svg"
            svg_dir.mkdir(parents=True, exist_ok=True)

            export_xml_netlist(root_schematic, xml_file, kicad_cli, log)
            parsed_xml = parse_netlist_xml(xml_file)
            svg_paths = export_svg_sheets(root_schematic, svg_dir, kicad_cli, log)
            png_paths = convert_svgs_to_png(svg_paths, png_dir, log)

        schematic_model = collect_source_schematics(root_schematic, project_dir, log)
        png_manifest, png_warnings = build_png_manifest(
            schematic_model["sheet_instances_by_path"],
            project_file.stem,
            png_paths,
            out_dir,
        )
        model = build_normalized_model(
            parsed_xml=parsed_xml,
            schematic_model=schematic_model,
            project_dir=project_dir,
            project_file=project_file,
            root_schematic=root_schematic,
            png_manifest=png_manifest,
            png_warnings=png_warnings,
        )
        validate_model(model)
        manifest = build_manifest(
            model=model,
            write_jsonl=args.jsonl,
        )

        write_split_artifacts(model, manifest, out_dir, args.jsonl)

        write_schema_documents(out_dir)
        write_agent_guide(guide_file, manifest, args.verbose, args)

        log.info("Manifest: %s", out_dir / MANIFEST_NAME)
        log.info("Agent guide: %s", guide_file)
        log.info("PNG sheets: %s", png_dir)
        return 0
    except KiCad2LLMError as ex:
        log.error("error: %s", ex)
        return 2
    except KeyboardInterrupt:
        log.error("error: interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
