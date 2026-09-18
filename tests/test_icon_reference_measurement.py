"""Measuring an icon reference: the numbers a build rule is pinned to.

Task 011/001. A value without dispersion is no measurement, and a reference
measured over bytes other than the pinned ones is no reference.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import measure_icon_reference as measure  # noqa: E402


def outline(d: str, grid: int = 256) -> bytes:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {grid} {grid}" fill="currentColor"><path d="{d}"/></svg>'.encode()


class OutlineIconTests(unittest.TestCase):
    def test_a_bar_measures_its_ink_share_and_stroke(self) -> None:
        # 256 x 16 on a 256 grid: ink 4096 / 65536, the bar is 16 thick.
        result = measure.measure_outline_icon(outline("M0,0H256V16H0Z"), "bar")
        self.assertEqual(256, result["grid"])
        self.assertAlmostEqual(0.0625, result["ink_share"], places=6)
        self.assertAlmostEqual(16.0, result["stroke"], places=6)
        self.assertAlmostEqual(1.0, result["on_grid_share"], places=6)

    def test_a_counter_is_no_ink_and_off_grid_points_are_counted(self) -> None:
        # A 16-thick square frame: outer 192^2, inner 160^2 wound the other way.
        frame = measure.measure_outline_icon(outline("M32,32H224V224H32Z M48,48V208H208V48Z"), "frame")
        self.assertAlmostEqual((192 * 192 - 160 * 160) / 65536, frame["ink_share"], places=6)
        # The equal-rectangle estimate is exact for a bar, close for a frame.
        self.assertAlmostEqual(16.0, frame["stroke"], delta=0.5)
        # Phosphor equals: two separate bars wound in opposite directions are
        # both ink - a hole is a contour inside another, not a sense of rotation.
        equals = measure.measure_outline_icon(outline("M0,64H256V80H0Z M0,176V192H256V176Z"), "equals")
        self.assertAlmostEqual(2 * 256 * 16 / 65536, equals["ink_share"], places=6)
        self.assertAlmostEqual(16.0, equals["stroke"], delta=0.5)
        shifted = measure.measure_outline_icon(outline("M0,0.5H256V16.5H0Z"), "shifted")
        self.assertAlmostEqual(0.0, shifted["on_grid_share"], places=6)


class CompactInkTests(unittest.TestCase):
    def test_a_dot_has_ink_but_no_stroke(self) -> None:
        # Phosphor cell-signal-none: a capsule no rectangle of equal area and
        # perimeter exists for, so its stroke is undefined - not its ink.
        dot = measure.measure_outline_icon(outline("M48,192v8a8,8,0,0,1-16,0v-8a8,8,0,0,1,16,0Z"), "dot")
        self.assertIsNone(dot["stroke"])
        self.assertAlmostEqual((16 * 8 + 3.14159265 * 64) / 65536, dot["ink_share"], places=4)


class ReferenceTests(unittest.TestCase):
    """A pinned reference, measured with its dispersion."""

    def setUp(self) -> None:
        import tempfile
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "LICENSE").write_text("MIT License\n")

    def pin(self, thicknesses: list[int]) -> dict:
        files = []
        for index, thickness in enumerate(thicknesses):
            path = self.root / f"bar-{index}.svg"
            path.write_bytes(outline(f"M0,0H256V{thickness}H0Z"))
            files.append({"path": path.name, "sha256": measure.sha256_file(path)})
        return {"name": "Fixture Bars", "origin": "https://example.invalid/bars", "revision": "a" * 40,
                "license": {"spdx": "MIT", "path": "LICENSE", "sha256": measure.sha256_file(self.root / "LICENSE")},
                "files": files}

    def test_every_rule_is_reported_with_its_dispersion(self) -> None:
        result = measure.measure_reference(self.root, self.pin([12] * 10 + [16] * 10))
        self.assertEqual(20, result["n"])
        self.assertEqual({"median": 14.0, "min": 12.0, "max": 16.0}, {k: round(result["rules"]["stroke"][k], 6) for k in ("median", "min", "max")})
        self.assertEqual({"median": 256, "min": 256, "max": 256}, {k: result["rules"]["grid"][k] for k in ("median", "min", "max")})

    def test_the_dispersion_includes_the_quartiles(self) -> None:
        # Outliers such as solid fills make min/max say little; quartiles say it.
        stroke = measure.measure_reference(self.root, self.pin([12] * 5 + [14] * 5 + [16] * 5 + [18] * 5))["rules"]["stroke"]
        self.assertEqual((13.5, 16.5), (round(stroke["q1"], 6), round(stroke["q3"], 6)))

    def test_icons_without_a_stroke_are_counted_not_averaged(self) -> None:
        pin = self.pin([16] * 20)
        dot = self.root / "dot.svg"; dot.write_bytes(outline("M48,192v8a8,8,0,0,1-16,0v-8a8,8,0,0,1,16,0Z"))
        pin["files"].append({"path": "dot.svg", "sha256": measure.sha256_file(dot)})
        result = measure.measure_reference(self.root, pin)
        self.assertEqual((21, 20, 1), (result["n"], result["rules"]["stroke"]["n"], result["stroke_undefined"]))
        self.assertEqual(21, result["rules"]["ink_share"]["n"])

    def test_fewer_than_twenty_icons_are_no_measurement(self) -> None:
        with self.assertRaisesRegex(measure.MeasurementError, "at least 20"):
            measure.measure_reference(self.root, self.pin([16] * 19))

    def test_bytes_other_than_the_pinned_ones_are_refused(self) -> None:
        pin = self.pin([16] * 20)
        (self.root / "bar-3.svg").write_bytes(outline("M0,0H256V12H0Z"))
        with self.assertRaisesRegex(measure.MeasurementError, "pinned"):
            measure.measure_reference(self.root, pin)
        pin = self.pin([16] * 20); (self.root / "LICENSE").write_text("changed\n")
        with self.assertRaisesRegex(measure.MeasurementError, "pinned"):
            measure.measure_reference(self.root, pin)


if __name__ == "__main__":
    unittest.main()
