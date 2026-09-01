import unittest

from scripts import pdf_visual_qa


class MermaidRasterDpiTests(unittest.TestCase):
    @staticmethod
    def binding(width: int, height: int) -> dict:
        return {
            "semantic_groups": [{"source_id": "flow", "type": "mermaid", "object_ids": ["p1-raster-0001"], "bbox_pt": [0, 0, 360, 180]}],
            "visible_inventory": [{
                "object_id": "p1-raster-0001", "kind": "raster", "bbox_pt": [0, 0, 360, 180],
                "paint": {"pixel_width": width, "pixel_height": height},
            }],
        }

    def test_300_dpi_passes_and_lower_or_incomplete_geometry_fails_closed(self) -> None:
        checks, findings = pdf_visual_qa.measure_mermaid_raster_quality(self.binding(1500, 750), ["flow"])
        self.assertEqual([], findings)
        self.assertEqual(300.0, checks[0]["effective_dpi_x"])
        self.assertEqual(300.0, checks[0]["effective_dpi_y"])
        _checks, findings = pdf_visual_qa.measure_mermaid_raster_quality(self.binding(1499, 750), ["flow"])
        self.assertEqual("flow", findings[0]["source_id"])
        malformed = {"semantic_groups": [{"source_id": "flow", "type": "mermaid", "object_ids": [], "bbox_pt": [0, 0, 360, 180]}], "visible_inventory": []}
        with self.assertRaises(pdf_visual_qa.QaError):
            pdf_visual_qa.measure_mermaid_raster_quality(malformed, ["flow"])

    def test_non_positive_intrinsic_dimensions_fail_closed(self) -> None:
        with self.assertRaises(pdf_visual_qa.QaError):
            pdf_visual_qa.measure_mermaid_raster_quality(self.binding(0, 750), ["flow"])

    def test_missing_expected_group_fails_closed(self) -> None:
        binding=self.binding(1500,750);binding["semantic_groups"]=[]
        with self.assertRaises(pdf_visual_qa.QaError):
            pdf_visual_qa.measure_mermaid_raster_quality(binding,["flow"])

    def test_wrong_type_fails_closed(self) -> None:
        binding=self.binding(1500,750);binding["semantic_groups"][0]["type"]="image"
        with self.assertRaises(pdf_visual_qa.QaError):
            pdf_visual_qa.measure_mermaid_raster_quality(binding,["flow"])

    def test_foreign_mermaid_id_fails_closed(self) -> None:
        binding=self.binding(1500,750);binding["semantic_groups"][0]["source_id"]="foreign"
        with self.assertRaises(pdf_visual_qa.QaError):
            pdf_visual_qa.measure_mermaid_raster_quality(binding,["flow"])

    def test_duplicate_or_ambiguous_raster_binding_fails_closed(self) -> None:
        binding=self.binding(1500,750)
        binding["semantic_groups"].append({"source_id":"other","type":"image","object_ids":["p1-raster-0001"],"bbox_pt":[0,0,360,180]})
        with self.assertRaises(pdf_visual_qa.QaError):
            pdf_visual_qa.measure_mermaid_raster_quality(binding,["flow"])
        binding=self.binding(1500,750);binding["semantic_groups"].append(dict(binding["semantic_groups"][0]))
        with self.assertRaises(pdf_visual_qa.QaError):
            pdf_visual_qa.measure_mermaid_raster_quality(binding,["flow"])

    def test_invalid_group_or_raster_geometry_fails_closed(self) -> None:
        binding=self.binding(1500,750);binding["semantic_groups"][0]["bbox_pt"]=[0,0,0,180]
        with self.assertRaises(pdf_visual_qa.QaError):
            pdf_visual_qa.measure_mermaid_raster_quality(binding,["flow"])
        binding=self.binding(1500,750);binding["visible_inventory"][0]["bbox_pt"]=[0,0,float("nan"),180]
        with self.assertRaises(pdf_visual_qa.QaError):
            pdf_visual_qa.measure_mermaid_raster_quality(binding,["flow"])

    def test_ast_domain_requires_unique_non_empty_mermaid_ids(self) -> None:
        self.assertEqual(["flow"],pdf_visual_qa.expected_mermaid_ids({"children":[{"type":"mermaid","id":"flow"}],"figure_registry":[{"type":"mermaid","id":"flow"}]}))
        with self.assertRaises(pdf_visual_qa.QaError):
            pdf_visual_qa.expected_mermaid_ids({"children":[{"type":"mermaid"}]})
        with self.assertRaises(pdf_visual_qa.QaError):
            pdf_visual_qa.expected_mermaid_ids({"children":[{"type":"mermaid","id":"flow"},{"type":"mermaid","id":"flow"}]})


if __name__ == "__main__":
    unittest.main()
