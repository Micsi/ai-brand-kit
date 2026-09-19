"""Icon build rules in the brand package (task 011/005, ADR 0001).

The package carries the rules, not the icons, and every number carries its
provenance as a file the package binds by digest.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import brand_package  # noqa: E402

FIXTURE = ROOT / "fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class IconTokensTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.package = Path(temp.name) / "package"; shutil.copytree(FIXTURE, self.package)
        (self.package / "icons").mkdir()
        for name, text in (("rules.md", "# Bauregeln\n"), ("pin.json", "{}\n"), ("measurement.json", "{}\n")):
            (self.package / "icons" / name).write_text(text)

    def icons(self) -> dict:
        def bound(name: str) -> dict:
            return {"path": f"icons/{name}", "sha256": digest(self.package / "icons" / name)}
        return {"rule_set": "brandkit-icon-basis",
                "build_rules": {"grid_units": 24, "stroke_width": 1.5, "stroke_linecap": "round", "stroke_linejoin": "round",
                                "color_count": 1, "filled_slots": ["stop", "play", "pause", "skip"]},
                "provenance": {"decision_record": bound("rules.md"), "reference_pin": bound("pin.json"), "measurement": bound("measurement.json"),
                               "tool": {"path": "scripts/measure_icon_reference.py", "commit": "a" * 40}}}

    def write(self, icons: dict) -> None:
        manifest = json.loads((self.package / "brand-package.json").read_text())
        manifest.setdefault("shared_visual_tokens", {})["icons"] = icons
        (self.package / "brand-package.json").write_text(json.dumps(manifest, indent=2) + "\n")

    def test_the_package_carries_icon_rules_with_their_provenance(self) -> None:
        self.write(self.icons())
        doc, _root, _tree = brand_package.validate_package(self.package)
        self.assertEqual(1.5, doc["shared_visual_tokens"]["icons"]["build_rules"]["stroke_width"])

    def test_a_provenance_file_that_changed_is_refused(self) -> None:
        self.write(self.icons())
        (self.package / "icons" / "measurement.json").write_text('{"changed": true}\n')
        with self.assertRaisesRegex(brand_package.ContractError, "provenance"):
            brand_package.validate_package(self.package)


if __name__ == "__main__":
    unittest.main()
