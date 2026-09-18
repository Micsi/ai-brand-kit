"""Measure an icon reference so a build rule can be pinned to numbers.

Task 011/001 of the icon domain.  The reference is an outline icon set (each
icon one filled path, the stroke already expanded), so the stroke is not an
attribute to read but a property of the geometry.  Everything here is exact
geometry through the fontTools pens - no rasterisation, nothing that depends
on a renderer.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import statistics
import xml.etree.ElementTree as ET
from typing import Any

from fontTools.pens.areaPen import AreaPen
from fontTools.pens.perimeterPen import PerimeterPen
from fontTools.pens.recordingPen import RecordingPen
from fontTools.svgLib.path import parse_path


class MeasurementError(ValueError):
    """A reference could not be measured as pinned."""


def _paths(data: bytes, label: str) -> tuple[int, list[str]]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise MeasurementError(f"{label} is not XML: {exc}") from exc
    values = (root.attrib.get("viewBox") or "").split()
    if len(values) != 4 or values[:2] != ["0", "0"] or values[2] != values[3] or not values[2].isdigit():
        raise MeasurementError(f"{label} has no square integer viewBox at the origin")
    paths = [element.attrib.get("d", "") for element in root.iter() if element.tag.split("}")[-1] == "path"]
    if not paths:
        raise MeasurementError(f"{label} has no path")
    return int(values[2]), paths


def measure_outline_icon(data: bytes, label: str) -> dict[str, Any]:
    """Ink share, stroke thickness and grid alignment of one outline icon.

    The stroke is the short side of the rectangle with the same area and
    perimeter as the ink: the smaller root of t^2 - (P/2) t + A = 0.  For a
    straight bar it is exact; 2A/P, the obvious alternative, is not.
    """
    grid, paths = _paths(data, label)
    area_pen, perimeter_pen, recording = AreaPen(), PerimeterPen(), RecordingPen()
    for d in paths:
        parse_path(d, area_pen); parse_path(d, perimeter_pen); parse_path(d, recording)
    area, perimeter = abs(area_pen.value), perimeter_pen.value
    half = perimeter / 2
    discriminant = half * half - 4 * area
    if area <= 0 or discriminant < 0:
        raise MeasurementError(f"{label} has no measurable ink")
    stroke = (half - math.sqrt(discriminant)) / 2
    points = [point for _operator, operands in recording.value for point in operands]
    on_grid = sum(1 for x, y in points if float(x).is_integer() and float(y).is_integer())
    return {"grid": grid, "ink_share": area / (grid * grid), "stroke": stroke,
            "on_grid_share": on_grid / len(points) if points else 0.0}


MINIMUM_ICONS = 20


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pinned_bytes(root: Path, binding: dict[str, str]) -> bytes:
    """The bytes the pin names, or a refusal - never other bytes under its name."""
    path = (root / binding["path"]).resolve()
    if root.resolve() not in path.parents or not path.is_file():
        raise MeasurementError(f"pinned file is outside the reference: {binding['path']}")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != binding["sha256"]:
        raise MeasurementError(f"file differs from its pinned digest: {binding['path']}")
    return data


def _spread(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {"median": statistics.median(ordered), "min": ordered[0], "max": ordered[-1], "n": len(ordered)}


def measure_reference(root: Path, pin: dict[str, Any]) -> dict[str, Any]:
    """Every rule of a pinned reference, each with its dispersion.

    The licence is part of the pin: a reference whose terms changed is not
    the reference that was chosen.
    """
    _pinned_bytes(root, pin["license"])
    if len(pin["files"]) < MINIMUM_ICONS:
        raise MeasurementError(f"a reference needs at least {MINIMUM_ICONS} icons, got {len(pin['files'])}")
    icons = [measure_outline_icon(_pinned_bytes(root, binding), binding["path"]) for binding in pin["files"]]
    rules = {name: _spread([icon[name] for icon in icons]) for name in ("grid", "ink_share", "stroke", "on_grid_share")}
    # The stroke on the 24-unit canvas the icon domain draws on.
    rules["stroke_at_24"] = _spread([icon["stroke"] * 24 / icon["grid"] for icon in icons])
    return {"schema_version": "brandkit-icon-reference-measurement.v1",
            "pin_sha256": hashlib.sha256(json.dumps(pin, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "n": len(icons), "rules": rules}


def main(argv: list[str] | None = None) -> int:
    """measure_icon_reference.py <reference-root> <pin.json>  ->  measurement JSON on stdout."""
    import sys
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print(main.__doc__, file=sys.stderr); return 2
    root, pin = Path(args[0]), json.loads(Path(args[1]).read_text(encoding="utf-8"))
    print(json.dumps(measure_reference(root, pin), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
