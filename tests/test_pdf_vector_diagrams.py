import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Spacer

from scripts import pdf_renderer, pdf_visual_qa


ROOT=Path(__file__).resolve().parents[1]
PACKAGE=ROOT/"fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360"
MERMAID_REAL=ROOT/"fixtures/pdf-renderer/goldens/v2-r22-release/rich-report/renderer-work/flow-rich-linux-worker/output.svg"
GRAPHVIZ_REAL=ROOT/"fixtures/pdf-renderer/goldens/v2-r22-release/rich-report/renderer-work/dependencies-rich.svg"


class VectorDiagramTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pdf_renderer.ROOT=ROOT
        dtype=json.loads((ROOT/"document-types/report.v1.json").read_text())
        cls.brand,_=pdf_renderer.validate_brand(PACKAGE/"brand-package.json",dtype)
        cls.projection=pdf_renderer._diagram_brand_projection(cls.brand)
        cls.font=cls.brand["_renderer_fonts"][cls.brand["pdf"]["tokens"]["typography_roles"]["body"]]

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT.parent)
        self.work=Path(self.temp.name)

    def tearDown(self):self.temp.cleanup()

    def test_real_worker_svg_lowers_to_complete_css_free_vector_geometry(self):
        output=self.work/"flow.svg"
        pdf_renderer._close_mermaid_svg(MERMAID_REAL,output,self.projection)
        raw=output.read_bytes();lower=raw.lower()
        for forbidden in (b"<style",b"@import",b"url(",b"foreignobject",b"filter",b"gradient",b"href=",b"http:",b"data:",b"file:"):
            self.assertNotIn(forbidden,lower)
        flowable=pdf_renderer.MermaidSvgFlowable(output,420,"flow",self.font)
        self.assertGreaterEqual(sum(shape[0]=="rect" for shape in flowable.shapes),3)
        self.assertEqual(["Start","Prüfung"],[shape[4] for shape in flowable.shapes if shape[0]=="text"])

    def test_css_entities_external_references_and_unpainted_elements_fail_closed(self):
        base='<svg viewBox="0 0 10 10"><rect x="0" y="0" width="10" height="10" fill="#FFFFFF"/><text x="5" y="5" fill="#172A3A" font-family="BrandKit-neutral-font" font-size="8" text-anchor="middle">X</text>{}</svg>'
        attacks=[
            '<style>@import "https://attacker.invalid/x.css";</style>',
            '<style>.x{fill:url(https://attacker.invalid/x)}</style>',
            '<style>.x{fill:u\\72l(https://attacker.invalid/x)}</style>',
            '<foreignObject><p>X</p></foreignObject>', '<filter id="x"/>', '<linearGradient id="x"/>',
            '<image href="data:image/png;base64,AA=="/>', '<script>1</script>',
            '<rect x="0" y="0" width="1" height="1" fill="#FFFFFF" onclick="x()"/>',
        ]
        for index,attack in enumerate(attacks):
            path=self.work/f"attack-{index}.svg";path.write_text(base.format(attack))
            with self.subTest(attack=attack),self.assertRaises(pdf_renderer.RenderError):
                pdf_renderer.MermaidSvgFlowable(path,100,"x",self.font)
        entity=self.work/"entity.svg";entity.write_text('<!DOCTYPE svg [<!ENTITY x "X">]><svg viewBox="0 0 10 10"><text x="5" y="5" fill="#172A3A" font-family="BrandKit-neutral-font" font-size="8" text-anchor="middle">&x;</text></svg>')
        with self.assertRaises(pdf_renderer.RenderError):pdf_renderer.MermaidSvgFlowable(entity,100,"x",self.font)

    def test_real_mermaid_and_graphviz_are_pdf_vectors_with_embedded_brand_font(self):
        mermaid=self.work/"flow.svg";graphviz=self.work/"graph.svg";shutil.copy2(GRAPHVIZ_REAL,graphviz)
        pdf_renderer._close_mermaid_svg(MERMAID_REAL,mermaid,self.projection)
        pdf_renderer._apply_graphviz_brand_palette(graphviz,self.brand)
        output=self.work/"vectors.pdf"
        SimpleDocTemplate(str(output),pagesize=A4).build([
            pdf_renderer.MermaidSvgFlowable(mermaid,420,"flow",self.font),Spacer(1,12),
            pdf_renderer.GraphvizSvgFlowable(graphviz,420,"graph",self.font),
        ])
        reader=PdfReader(str(output));images=[]
        for page in reader.pages:
            resources=page.get("/Resources",{});xobjects=resources.get("/XObject",{}) or {}
            if hasattr(xobjects,"get_object"):xobjects=xobjects.get_object()
            images.extend(ref.get_object() for ref in xobjects.values() if str(ref.get_object().get("/Subtype"))=="/Image")
        self.assertEqual([],images)
        text=subprocess.run(["pdftotext",str(output),"-"],check=True,capture_output=True,text=True).stdout
        for literal in ("Start","Prüfung","Eingabe","Ausgabe"):self.assertIn(literal,text)
        fonts=subprocess.run(["pdffonts",str(output)],check=True,capture_output=True,text=True).stdout
        self.assertRegex(fonts,r"Abel-Regular\s+TrueType\s+WinAnsi\s+yes")

    def test_source_binding_requires_vector_and_text_and_rejects_images(self):
        binding={"semantic_groups":[{"source_id":"flow","type":"mermaid","object_ids":["v","t"]}],"visible_inventory":[{"object_id":"v","kind":"vector"},{"object_id":"t","kind":"text"}]}
        self.assertEqual(0,pdf_visual_qa.measure_mermaid_vector_quality(binding,["flow"])[0]["image_xobjects"])
        for kind in ("raster","inline-image","form","shading","pattern"):
            attacked=json.loads(json.dumps(binding));attacked["visible_inventory"][0]["kind"]=kind
            with self.subTest(kind=kind),self.assertRaises(pdf_visual_qa.QaError):pdf_visual_qa.measure_mermaid_vector_quality(attacked,["flow"])


if __name__=="__main__":unittest.main()
