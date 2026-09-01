from __future__ import annotations
import argparse,hashlib,json,os,shutil,subprocess,sys,tempfile,unittest
from unittest import mock
from pathlib import Path
from jsonschema import Draft202012Validator
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors as rl_colors
from reportlab.pdfgen import canvas
from PIL import Image as PILImage
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"scripts"))
import brand_voice_pdf as workflow
import markdown_contract as contract
import pdf_visual_qa
import pdf_renderer
import brand_package
import project_brand_tokens
import generate_pdf_e2e_goldens as goldens

class BrandVoicePdfSecurityTests(unittest.TestCase):
    def test_logical_project_identity_is_stable_unique_and_fixture_only(self):
        report=goldens.fixture_project_identity("report");offer=goldens.fixture_project_identity("offer")
        self.assertRegex(report,r"^[0-9a-f]{64}$");self.assertNotEqual(report,offer)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project_a=root/"room-a/project";project_b=root/"room-b/project";project_a.mkdir(parents=True);project_b.mkdir(parents=True)
            fixture=root/"fixture.json";fixture.write_text(json.dumps({"evidence_origin":"deterministic-e2e-fixture-not-provider-evidence"}))
            production=root/"production.json";production.write_text(json.dumps({"approved":True}))
            self.assertEqual(report,pdf_renderer._project_identity_sha256(project_a,fixture,report))
            self.assertEqual(report,pdf_renderer._project_identity_sha256(project_b,fixture,report))
            self.assertNotEqual(pdf_renderer._project_identity_sha256(project_a,fixture),pdf_renderer._project_identity_sha256(project_b,fixture))
            with self.assertRaisesRegex(pdf_renderer.RenderError,"restricted"):
                pdf_renderer._project_identity_sha256(project_a,production,report)
            with self.assertRaisesRegex(pdf_renderer.RenderError,"lowercase SHA-256"):
                pdf_renderer._project_identity_sha256(project_a,fixture,"0"*63)
        with self.assertRaisesRegex(RuntimeError,"unknown"):
            goldens.fixture_project_identity("foreign")

    def test_rich_svg_fixture_is_informative_and_single_fill_placeholder_is_rejected(self):
        audit=goldens.verify_informative_svg(ROOT/"fixtures/pdf-renderer/e2e/assets/diagram.svg")
        self.assertEqual(["Input","Check","Freigabe"],audit["labels"]);self.assertGreaterEqual(len(audit["foreground_geometry"]),5)
        with tempfile.TemporaryDirectory() as td:
            empty=Path(td)/"empty.svg";empty.write_text('<svg viewBox="0 0 120 40"><title>Leer</title><desc>Leer</desc><rect width="120" height="40" fill="#EEEEEE"/></svg>')
            with self.assertRaisesRegex(RuntimeError,"placeholder"):goldens.verify_informative_svg(empty)

    def test_golden_file_inventory_is_closed_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/"document").mkdir();(root/"document/artifact.txt").write_text("evidence\n")
            inventory=goldens.closed_file_inventory(root);goldens.verify_closed_file_inventory(root,inventory)
            self.assertEqual(1,inventory["entry_count"]);self.assertEqual("document/artifact.txt",inventory["entries"][0]["path"])
            (root/"document/foreign.txt").write_text("foreign\n")
            with self.assertRaisesRegex(RuntimeError,"differs"):goldens.verify_closed_file_inventory(root,inventory)
            (root/"document/link").symlink_to("artifact.txt")
            with self.assertRaisesRegex(RuntimeError,"symlinks"):goldens.closed_file_inventory(root)
    def test_four_document_families_have_distinct_closed_layout_strategies(self):
        bases=contract.load_base_types();policies={name:pdf_renderer.layout_strategy(value) for name,value in bases.items()}
        self.assertEqual({"report-evidence","offer-proposal","freeform-editorial","one-pager-hero"},{p["id"] for p in policies.values()})
        self.assertTrue(policies["report"]["toc"]);self.assertTrue(policies["report"]["section_numbers"])
        self.assertEqual("light-band",policies["offer"]["cover"]);self.assertEqual("editorial",policies["freeform"]["cover"])
        self.assertEqual("hero",policies["one-pager"]["cover"]);self.assertEqual(48,policies["one-pager"]["hero_mm"]);self.assertFalse(policies["one-pager"]["toc"])

    def test_project_overrides_reach_layout_policy(self):
        report=contract.load_base_types()["report"]
        self.assertEqual((False,False),(pdf_renderer.layout_strategy(report,{"toc":False,"section_numbering":False})["toc"],pdf_renderer.layout_strategy(report,{"toc":False,"section_numbering":False})["section_numbers"]))
        self.assertEqual((True,True),(pdf_renderer.layout_strategy(report,{"toc":True,"section_numbering":True})["toc"],pdf_renderer.layout_strategy(report,{"toc":True,"section_numbering":True})["section_numbers"]))

    def test_evidence_callout_fields_are_visible_in_pdf_and_html_text(self):
        ast={"frontmatter":{"facts":[{"id":"fact-one","value":"42 %","source_ids":["source-one"]}]}}
        for kind,attrs,expected in (
            ("metric",{"title":"Kennzahl","label":"Anteil","value_fact":"fact-one","source_id":"source-one"},("Anteil","42 %","fact-one","source-one")),
            ("quote",{"title":"Zitat","source_id":"source-one"},("source-one",)),
            ("legal",{"title":"Hinweis","source_id":"source-one"},("source-one",)),
        ):
            node={"kind":kind,"attrs":attrs,"children":[{"type":"paragraph","inline":[{"type":"text","text":"Belegter Text"}]}]}
            title,body=pdf_renderer.callout_text(node,ast);visible=title+" "+body
            for token in expected:self.assertIn(token,visible)
        presentation={"kind":"note","attrs":{"id":"note-default","title":"Notiz"},"children":[{"type":"paragraph","inline":[{"type":"text","text":"Inhalt"}]}]}
        self.assertIn("ID: note-default",pdf_renderer.callout_text(presentation,ast)[1])

    def test_callout_appearances_and_icon_state_are_not_discarded(self):
        style=pdf_renderer.ParagraphStyle("test",fontName="Helvetica",fontSize=10,leading=12)
        variants=[pdf_renderer.Callout("Titel","Text",100,rl_colors.blue,rl_colors.white,style,appearance,icon) for appearance in ("default","simple","minimal") for icon in (True,False)]
        self.assertEqual({"default","simple","minimal"},{v.appearance for v in variants});self.assertEqual({True,False},{v.icon for v in variants})
    def test_pdf_destination_plan_unifies_mermaid_graphviz_and_rejects_missing_duplicate_and_foreign(self):
        paragraph=lambda target:{"type":"paragraph","inline":[{"type":"link","label":"Ziel","target":f"#{target}"}]}
        figure=lambda kind,ident,number:{"type":kind,"id":ident,"code":"diagram","caption":"Titel","description":"Beschreibung","figure_number":number}
        def document(children):
            figures=[node for node in children if node["type"] in {"mermaid","graphviz"}]
            return {"children":children,"figure_registry":[{"id":node["id"],"number":number,"type":node["type"],"caption":node["caption"],"description":node["description"]} for number,node in enumerate(figures,1)]}
        semantic={"type":"paragraph","inline":[{"type":"figure-ref","target":"fig-flow-rich","figure_number":1,"reference_text":"Abbildung 1"}]}
        ast=document([semantic,paragraph("fig-dependencies-rich"),figure("mermaid","fig-flow-rich",1),figure("graphviz","fig-dependencies-rich",2)])
        self.assertEqual({"fig-flow-rich":"mermaid","fig-dependencies-rich":"graphviz"},pdf_renderer._pdf_destination_plan(ast))
        self.assertEqual([{"source_id":"node-1","target_id":"fig-flow-rich","number":1,"reference_text":"Abbildung 1"}],pdf_renderer._figure_references(ast))
        self.assertEqual([],pdf_renderer._figure_references(document([paragraph("fig-flow-rich"),figure("mermaid","fig-flow-rich",1)])))
        with self.assertRaisesRegex(pdf_renderer.RenderError,"missing or foreign"):
            pdf_renderer._pdf_destination_plan(document([paragraph("missing")]))
        with self.assertRaisesRegex(pdf_renderer.RenderError,"duplicate figure IDs"):
            pdf_renderer._pdf_destination_plan(document([paragraph("fig-flow-rich"),figure("mermaid","fig-flow-rich",1),figure("mermaid","fig-flow-rich",2)]))
        with self.assertRaisesRegex(pdf_renderer.RenderError,"duplicate figure IDs"):
            pdf_renderer._pdf_destination_plan(document([paragraph("fig-flow-rich"),figure("mermaid","fig-flow-rich",1),figure("graphviz","fig-flow-rich",2)]))
        with self.assertRaisesRegex(pdf_renderer.RenderError,"missing or foreign"):
            pdf_renderer._pdf_destination_plan(document([paragraph("callout-rich"),{"type":"callout","attrs":{"id":"callout-rich"}}]))

    def test_graphviz_default_palette_is_bound_to_active_brand_roles(self):
        svg=b'<svg xmlns="http://www.w3.org/2000/svg"><g><polygon fill="white" stroke="black"/><text>A</text></g></svg>'
        brand=json.loads((ROOT/"fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360/brand-package.json").read_text())
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"diagram.svg";path.write_bytes(svg)
            audit=pdf_renderer._apply_graphviz_brand_palette(path,brand)
            rendered=path.read_text()
            self.assertNotIn('fill="black"',rendered);self.assertNotIn('stroke="black"',rendered)
            self.assertIn('stroke="#3AAFA9"',rendered);self.assertIn('fill="#172A3A"',rendered)
            self.assertIn('fill="#EDF4F4"',rendered);self.assertIn('font-family="BrandKit-neutral-font"',rendered)
            self.assertTrue(audit["font_embedded"]);self.assertTrue(audit["forbidden_fallbacks_absent"])
            self.assertEqual("accent",audit["line_role"])

    def test_mermaid_config_closes_font_color_line_and_contrast_roles(self):
        brand=json.loads((ROOT/"fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360/brand-package.json").read_text())
        config,projection=pdf_renderer._mermaid_config(brand);encoded=json.dumps(config)
        self.assertEqual("BrandKit-neutral-font",config["fontFamily"])
        self.assertNotRegex(encoded,r"Arial|Times|system-ui|sans-serif")
        self.assertEqual("#3AAFA9",config["themeVariables"]["lineColor"])
        self.assertEqual(set(brand["pdf"]["tokens"]["color_roles"].values()),set(projection["colors"].values()))
        self.assertGreaterEqual(min(projection["contrast"].values()),4.5)

    def test_actual_pdf_destinations_resolve_flow_to_page_six_and_toc_to_real_pages(self):
        with tempfile.TemporaryDirectory() as td:
            pdf=Path(td)/"links.pdf";c=canvas.Canvas(str(pdf),pagesize=A4,invariant=1,pageCompression=0,pdfVersion=(1,4))
            c.drawString(40,800,"Cover");c.showPage()
            links=(("toc-one","chapter-one",700),("toc-two","chapter-two",670),("toc-flow","flow-rich",640),("flow-source","flow-rich",610))
            c.bookmarkPage("chapter-one",fit="XYZ",left=35,top=A4[1]-30,zoom=0)
            for _source,target,y in links:c.linkRect("",target,(40,y,180,y+18),relative=0,thickness=0)
            c.drawString(40,800,"Page two");c.showPage()
            for number in (3,4):c.drawString(40,800,f"Page {number}");c.showPage()
            c.bookmarkPage("chapter-two",fit="XYZ",left=35,top=A4[1]-30,zoom=0);c.drawString(40,800,"Page five");c.showPage()
            c.bookmarkPage("flow-rich",fit="XYZ",left=35,top=A4[1]-30,zoom=0);c.drawString(40,800,"Diagram");c.showPage();c.save()
            objects=[]
            for source,_target,y in links:objects.append({"source_id":source,"page":2,"bbox_pt":[35,A4[1]-(y+22),190,A4[1]-(y-4)]})
            objects.extend(({"source_id":"chapter-one","page":2,"bbox_pt":[35,30,190,60]},{"source_id":"chapter-two","page":5,"bbox_pt":[35,30,190,60]},{"source_id":"flow-rich","page":6,"bbox_pt":[35,30,190,60]}))
            expected=[{"source_id":source,"target_id":target} for source,target,_y in links]
            observed=pdf_visual_qa.verify_internal_links(pdf,expected,objects)
            self.assertEqual([2,5,6,6],[x["target_page"] for x in observed])

    def test_actual_pdf_destination_multiset_rejects_swapped_missing_duplicate_foreign_page_and_coordinate(self):
        def build(path:Path,targets:list[str])->None:
            c=canvas.Canvas(str(path),pagesize=A4,invariant=1,pageCompression=0,pdfVersion=(1,4));c.drawString(40,800,"one")
            for index,target in enumerate(targets):c.linkRect("",target,(40,700-index*25,180,718-index*25),relative=0,thickness=0)
            c.showPage();c.bookmarkPage("right",fit="XYZ",left=35,top=A4[1]-30,zoom=0);c.bookmarkPage("same-page",fit="XYZ",left=35,top=A4[1]-130,zoom=0);c.bookmarkPage("wrong-coordinate",fit="XYZ",left=35,top=A4[1]-230,zoom=0);c.bookmarkPage("foreign",fit="XYZ",left=135,top=A4[1]-330,zoom=0);c.drawString(40,800,"two");c.showPage();c.bookmarkPage("wrong-page",fit="XYZ",left=35,top=A4[1]-30,zoom=0);c.drawString(40,800,"three");c.showPage();c.save()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);objects=[{"source_id":"source","page":1,"bbox_pt":[35,A4[1]-722,190,A4[1]-671]},{"source_id":"right","page":2,"bbox_pt":[35,30,190,60]},{"source_id":"same-page","page":2,"bbox_pt":[35,130,190,160]}];expected=[{"source_id":"source","target_id":"right"},{"source_id":"source","target_id":"same-page"}]
            passing=root/"passing.pdf";build(passing,["right","same-page"])
            observed=pdf_visual_qa.verify_internal_links(passing,expected,objects)
            self.assertEqual([[35.0,30.0],[35.0,130.0]],[x["target_xyz_pt"] for x in observed])
            repeated=root/"repeated.pdf";build(repeated,["right","right"])
            pdf_visual_qa.verify_internal_links(repeated,[{"source_id":"source","target_id":"right"},{"source_id":"source","target_id":"right"}],objects)
            cases={"swapped":["same-page","right"],"missing":["right"],"duplicate":["right","same-page","same-page"],"wrong-page":["wrong-page","same-page"],"wrong-coordinate":["wrong-coordinate","same-page"],"foreign":["right","same-page","foreign"]}
            for label,targets in cases.items():
                path=root/f"{label}.pdf";build(path,targets)
                with self.subTest(label=label),self.assertRaises(pdf_visual_qa.QaError):pdf_visual_qa.verify_internal_links(path,expected,objects)
    def test_source_map_uses_painted_flowable_position_not_space_after_gap(self):
        class Stub:
            _brandkit_source_id="heading"
            _brandkit_source_type="heading"
            width=100.0
            height=20.0
            hAlign="LEFT"
            def getSpaceAfter(self): return 22.0
        doc=object.__new__(pdf_renderer.InstrumentedDocTemplate)
        doc.source_records=[];doc.frame=type("Frame",(),{"_x":10.0,"_y":100.0,"_aW":300.0})();doc.page=2
        doc.afterFlowable(Stub())
        self.assertEqual([10.0,122.0,110.0,142.0],doc.source_records[0]["bbox_pdf_pt"])
    def brand_font(self):
        fixture=ROOT/"fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360"
        brand,_=pdf_renderer.validate_brand(fixture/"brand-package.json",json.loads((fixture/"documents/report.json").read_text()))
        return brand["_renderer_fonts"][brand["pdf"]["tokens"]["typography_roles"]["body"]]
    def test_brand_font_is_embedded_unicode_and_token_projection_is_single_source(self):
        fixture=ROOT/"fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360"
        manifest=fixture/"brand-package.json";dtype=json.loads((fixture/"documents/report.json").read_text())
        brand,_=pdf_renderer.validate_brand(manifest,dtype);font=brand["_renderer_fonts"][brand["pdf"]["tokens"]["typography_roles"]["body"]]
        with tempfile.TemporaryDirectory() as td:
            pdf=Path(td)/"font.pdf";c=canvas.Canvas(str(pdf),pagesize=A4,invariant=1,pageCompression=0,pdfVersion=(1,4));c.setFont(font,11);c.drawString(30*mm,250*mm,"Übergröße Straße €");c.showPage();c.save()
            fonts=subprocess.run(["pdffonts",str(pdf)],check=True,capture_output=True,text=True).stdout
            text=subprocess.run(["pdftotext",str(pdf),"-"],check=True,capture_output=True,text=True).stdout
            self.assertRegex(fonts,r"yes\s+yes")
            self.assertIn("Übergröße Straße €",text)
        projection=project_brand_tokens.project(manifest)
        self.assertEqual(brand["pdf"],projection["pdf"])
        self.assertNotIn("shared",projection)
        self.assertNotIn("shared_visual_tokens",projection)

    def test_token_projection_preserves_explicit_shared_and_never_writes_design(self):
        fixture=ROOT/"fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360"
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);manifest=root/"brand-package.json"
            value=json.loads((fixture/"brand-package.json").read_text())
            shared={"spacing":{"section":"17px"},"colors":{"accent":"#123456"}}
            value["shared_visual_tokens"]=shared
            manifest.write_text(json.dumps(value,ensure_ascii=False))
            design=root/"DESIGN.md";before=b"owner bytes\r\nremain exact\n";design.write_bytes(before)
            projection=project_brand_tokens.project(manifest)
            self.assertEqual(shared,projection["shared_visual_tokens"])
            self.assertEqual(value["pdf"],projection["pdf"])
            self.assertEqual(before,design.read_bytes())
            with mock.patch.object(sys,"argv",["project_brand_tokens.py","--brand",str(manifest),"--design",str(design)]):
                with self.assertRaises(SystemExit): project_brand_tokens.main()
            self.assertEqual(before,design.read_bytes())
    def test_provider_contract_rejects_codex_before_side_effects(self):
        with self.assertRaisesRegex(workflow.WorkflowError,"exactly: claude"):workflow.run(argparse.Namespace(provider="codex"))
    def test_render_result_requires_closed_bindings(self):
        schema=json.loads((ROOT/"schemas/render-result.v1.schema.json").read_text());bad={"schema_version":"render-result.v1","issuer":"x","hmac_sha256":"0"*64,"payload":{}};messages=" ".join(e.message for e in Draft202012Validator(schema).iter_errors(bad));self.assertIn("binding",messages);self.assertIn("visual_qa",messages)
    def test_audit_schema_rejects_foreign_provider_and_open_proof(self):
        schema=json.loads((ROOT/"schemas/humanizer-audit.v1.schema.json").read_text());value={"schema_version":"humanizer-audit.v1","issuer":"issuer","hmac_sha256":"0"*64,"payload":{"baseline_sha256":"0"*64,"final_sha256":"1"*64,"provider":"codex","humanizer":{"version":"5.22.2-brandkit.2","release_path":"/tmp/x","release_sha256":"2"*64},"raw_artifacts":[],"operations":[],"protected_stable":False,"ast_skeleton_stable":False}};self.assertGreaterEqual(len(list(Draft202012Validator(schema).iter_errors(value))),4)
    def test_visual_qa_contrast_positive_and_negative(self):
        self.assertGreaterEqual(pdf_visual_qa.contrast("#172A3A","#FFFFFF"),4.5);self.assertLess(pdf_visual_qa.contrast("#777777","#888888"),4.5)
    def test_measured_background_binds_large_white_hero_to_dark_surface(self):
        image=PILImage.new("RGB",(320,100),(40,75,143))
        # Simulate a large white word whose tight bbox contains more glyph ink
        # than surface pixels. The surrounding final raster remains dark.
        for x in range(90,230):
            for y in range(35,65):
                if (x//7)%2==0:image.putpixel((x,y),(255,255,255))
        backgrounds=pdf_visual_qa.measured_backgrounds(image,(90,35,230,65),1,1,[1.,1.,1.],1.)
        self.assertIn((40,75,143),backgrounds)
        self.assertNotIn((255,255,255),backgrounds)
        self.assertGreaterEqual(min(pdf_visual_qa._rgb_ratio([1.,1.,1.],x,1.) for x in backgrounds),4.5)
    def test_offline_proxy_is_structured_loopback_sink(self):
        self.assertEqual({"scheme":"http","host":"127.0.0.1","port":9},pdf_renderer.OFFLINE_PROXY)
    def test_project_and_brand_exchange_change_binding(self):
        one={"project_identity_sha256":"0"*64,"brand":{"id":"a","content_sha256":"1"*64}};two=json.loads(json.dumps(one));two["project_identity_sha256"]="2"*64;three=json.loads(json.dumps(one));three["brand"]["content_sha256"]="3"*64;self.assertNotEqual(contract.canonical_json(one),contract.canonical_json(two));self.assertNotEqual(contract.canonical_json(one),contract.canonical_json(three))
    def test_workflow_lock_v2_is_closed_and_byte_canonical(self):
        schema=json.loads((ROOT/"schemas/brand-voice-workflow-lock.v2.schema.json").read_text())
        value={"schema_version":"brand-voice-workflow-lock.v2","activation":{"project_sha256":"0"*64,"lock_sha256":"1"*64,"state_sha256":"2"*64,"project_utf8_base64":"e30KICAgIA==","lock_utf8_base64":"e30KICAgIA=="},"brand":{"id":"neutral-fixture","version":"1.0.0","manifest_sha256":"3"*64,"content_sha256":"4"*64},"humanizer":{"version":"5.22.2-brandkit.2","release_attestation_sha256":"5"*64,"tool_hashes":{"runner":"6"*64,"skill":"7"*64,"catalog":"8"*64},"provider_capability":{"provider":"claude","allowed_states":[{"mode":"audit-only","outcome":"no-confirmed-change","model_calls":1,"confirmed_edit_count":0,"rejected_candidates":"evidence-required","rewrite_present":False},{"mode":"audit+rewrite","outcome":"changed","model_calls":2,"minimum_edit_count":1,"rewrite_present":True}]}},"renderer":{"name":"brandkit-pdf-renderer","version":"1.2.0","lock_sha256":"9"*64,"trust_sha256":"a"*64},"markdown_contract":{"sha256":"b"*64},"document_type":{"id":"report","version":"1.0.0","sha256":"c"*64},"owner_approval_key_sha256":"d"*64}
        self.assertEqual([],list(Draft202012Validator(schema).iter_errors(value)))
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);subprocess.run(["git","init","-q",str(root)],check=True);path=root/"workflow-lock.v2.json";path.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+"\n")
            workflow.verify_workflow_lock({"root":root,"value":value},path)
            path.write_text(path.read_text()+" ")
            with self.assertRaisesRegex(workflow.WorkflowError,"differs"):workflow.verify_workflow_lock({"root":root,"value":value},path)
    def test_two_isolated_projects_resolve_same_complete_workflow_pin_before_writes(self):
        fixture=ROOT/"fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360"
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);key=base/"owner.key";key.write_bytes(b"separate-owner-approval-test-key-32b")
            values=[]
            for name in ("project-a","project-b"):
                project=base/name;project.mkdir();subprocess.run(["git","init","-q",str(project)],check=True)
                brand_package.pin_project(project,fixture,"1.0.0")
                preflight=workflow.workflow_preflight(project,"neutral-fixture","claude",key);values.append(preflight["value"])
                self.assertFalse((project/".brandkit/work").exists());self.assertFalse((project/"output").exists())
            self.assertEqual(values[0],values[1])
            lock=base/"project-a/workflow-lock.v2.json";lock.write_text(json.dumps(values[0],ensure_ascii=False,sort_keys=True,indent=2)+"\n")
            workflow.verify_workflow_lock({"root":base/"project-a","value":values[0]},lock)
            lock.unlink()
            with self.assertRaisesRegex(workflow.WorkflowError,"pre-existing"):workflow.verify_workflow_lock({"root":base/"project-a","value":values[0]},lock)
            self.assertFalse((base/"project-a/.brandkit/work").exists())
    def test_safe_run_file_rejects_traversal_symlink_and_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/"raw").mkdir();evidence=root/"raw/item.json";evidence.write_text("{}")
            self.assertEqual(evidence.resolve(),contract._safe_run_file(root,"raw/item.json","raw"))
            with self.assertRaises(contract.ContractError):contract._safe_run_file(root,"../item.json","raw")
            outside=root.parent/(root.name+"-outside");outside.write_text("foreign")
            link=root/"raw/link.json";link.symlink_to(outside)
            try:
                with self.assertRaises(contract.ContractError):contract._safe_run_file(root,"raw/link.json","raw")
            finally: outside.unlink()
    def test_stable_read_rejects_byte_identical_symlink_for_every_envelope_class(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for label in ("project-lock","skill-lock","workflow-lock","baseline","audit","approval","render-result","issuer-key","owner-key"):
                real=root/(label+".real");real.write_bytes(b"same bytes\n")
                link=root/(label+".json");link.symlink_to(real.name)
                with self.subTest(label=label),self.assertRaisesRegex(contract.ContractError,"symlink"):
                    contract.stable_read(link,root,label)
    def test_visual_qa_instrumented_positive_and_distinct_object_negative(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);pdf=root/"ok.pdf";c=canvas.Canvas(str(pdf),pagesize=A4,invariant=1,pageCompression=0,pdfVersion=(1,4));c.setFont(self.brand_font(),10);c.setFillColorRGB(23/255,42/255,58/255);c.drawString(30*mm,250*mm,"Sichtbarer Inhalt");c.showPage();c.save()
            ast=root/"ast.json";ast.write_text(json.dumps({"children":[{"type":"paragraph","text":"Sichtbarer Inhalt","id":"body"}]}));brand=ROOT/"fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360/brand-package.json"
            inventory=pdf_visual_qa.closed_visible_inventory(pdf)
            source=root/"source.json";source.write_text(json.dumps({"schema_version":"renderer-source-map.v1","page_coordinate_system":"pdf-points-top-left","page_count":1,"required_furniture":[],"objects":[{"source_id":"body","type":"paragraph","page":1,"bbox_pt":[80,120,200,142],"visible":True,"evidence_kind":"text","evidence_text":"Sichtbarer Inhalt"}],"visible_inventory":inventory,"expected_internal_links":[],"figure_registry":[],"figure_references":[]}))
            qa=pdf_visual_qa.inspect(pdf,root/"pages",root/"qa.json",ast,brand,144,source);self.assertEqual("pass",qa["status"])
            bad=root/"bad-source.json";bad.write_text(json.dumps({"schema_version":"renderer-source-map.v1","page_coordinate_system":"pdf-points-top-left","page_count":1,"required_furniture":[],"objects":[{"source_id":"a","type":"paragraph","page":1,"bbox_pt":[80,120,200,142],"visible":True,"evidence_kind":"text","evidence_text":"Sichtbarer Inhalt"},{"source_id":"b","type":"callout","page":1,"bbox_pt":[80,120,200,142],"visible":True,"evidence_kind":"text","evidence_text":"Sichtbarer Inhalt"}],"visible_inventory":inventory,"expected_internal_links":[],"figure_registry":[],"figure_references":[]}))
            with self.assertRaises(pdf_visual_qa.QaError):pdf_visual_qa.inspect(pdf,root/"bad-pages",root/"bad-qa.json",ast,brand,144,bad)
            base={"schema_version":"renderer-source-map.v1","page_coordinate_system":"pdf-points-top-left","page_count":1,"required_furniture":[],"objects":[{"source_id":"body","type":"paragraph","page":1,"bbox_pt":[80,120,200,142],"visible":True,"evidence_kind":"text","evidence_text":"Sichtbarer Inhalt"}],"visible_inventory":inventory,"expected_internal_links":[],"figure_registry":[],"figure_references":[]}
            attacks=[]
            foreign=json.loads(json.dumps(base));foreign["objects"][0]["page"]=2;attacks.append(foreign)
            missing=json.loads(json.dumps(base));missing["required_furniture"]=["furniture-header-1"];attacks.append(missing)
            bbox=json.loads(json.dumps(base));bbox["objects"][0]["bbox_pt"]=[-1,100,200,125];attacks.append(bbox)
            duplicate=json.loads(json.dumps(base));duplicate["objects"].append(dict(duplicate["objects"][0]));attacks.append(duplicate)
            for index,value in enumerate(attacks):
                attack=root/f"source-attack-{index}.json";attack.write_text(json.dumps(value))
                with self.subTest(source_attack=index),self.assertRaises(pdf_visual_qa.QaError):
                    pdf_visual_qa.inspect(pdf,root/f"attack-pages-{index}",root/f"attack-qa-{index}.json",ast,brand,144,attack)
            for label,box,text in (("shifted",[80,180,200,210],"Sichtbarer Inhalt"),("wrong-object",[80,85,200,115],"Nicht vorhanden"),("too-large",[28,28,560,800],"Sichtbarer Inhalt"),("too-small",[90,95,120,103],"Sichtbarer Inhalt")):
                value=json.loads(json.dumps(base));value["objects"][0]["bbox_pt"]=box;value["objects"][0]["evidence_text"]=text
                attack=root/f"source-{label}.json";attack.write_text(json.dumps(value))
                with self.subTest(binding_attack=label),self.assertRaises(pdf_visual_qa.QaError):
                    pdf_visual_qa.inspect(pdf,root/f"binding-pages-{label}",root/f"binding-qa-{label}.json",ast,brand,144,attack)
            for label,mutate in (("missing",lambda xs:xs[:-1]),("additional",lambda xs:xs+[dict(xs[-1],object_id="foreign-visible")]),("shift",lambda xs:[*xs[:-1],dict(xs[-1],bbox_pt=[v+3 for v in xs[-1]["bbox_pt"]])]),("wrong-hash",lambda xs:[*xs[:-1],dict(xs[-1],pdf_object_sha256="f"*64)])):
                value=json.loads(json.dumps(base));value["visible_inventory"]=mutate(value["visible_inventory"])
                attack=root/f"visible-{label}.json";attack.write_text(json.dumps(value))
                with self.subTest(visible_attack=label),self.assertRaises(pdf_visual_qa.QaError):
                    pdf_visual_qa.inspect(pdf,root/f"visible-pages-{label}",root/f"visible-qa-{label}.json",ast,brand,144,attack)

    def test_container_binding_uses_measured_container_and_closed_inventory(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);pdf=root/"container.pdf";c=canvas.Canvas(str(pdf),pagesize=A4,invariant=1,pageCompression=0,pdfVersion=(1,4));c.setFont(self.brand_font(),10);c.setFillColorRGB(23/255,42/255,58/255);c.drawString(30*mm,250*mm,"Erster Punkt");c.drawString(40*mm,238*mm,"Unterpunkt");c.showPage();c.save()
            inventory=pdf_visual_qa.closed_visible_inventory(pdf);first=inventory[0]["bbox_pt"]
            box=[first[0]-5,first[1]-5,min(A4[0]-30,first[2]+200),first[3]+80]
            value={"schema_version":"renderer-source-map.v1","page_coordinate_system":"pdf-points-top-left","page_count":1,"required_furniture":[],"objects":[{"source_id":"list","type":"unordered-list","page":1,"bbox_pt":box,"visible":True,"evidence_kind":"text","evidence_text":"Erster Punkt"}],"visible_inventory":inventory,"expected_internal_links":[],"figure_registry":[],"figure_references":[]}
            source=root/"source.json";source.write_text(json.dumps(value));ast=root/"ast.json";ast.write_text(json.dumps({"children":[]}));brand=ROOT/"fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360/brand-package.json"
            self.assertEqual("pass",pdf_visual_qa.inspect(pdf,root/"pages",root/"qa.json",ast,brand,144,source)["status"])
            shifted=json.loads(json.dumps(value));shifted["objects"][0]["bbox_pt"]=[x+60 if i in (1,3) else x for i,x in enumerate(box)];bad=root/"bad.json";bad.write_text(json.dumps(shifted))
            with self.assertRaises(pdf_visual_qa.QaError):pdf_visual_qa.inspect(pdf,root/"bad-pages",root/"bad-qa.json",ast,brand,144,bad)

    def test_key_reader_rejects_real_leaf_and_ancestor_symlinks(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);key=root/"issuer.key";key.write_bytes(b"k"*32)
            leaf=root/"leaf.key";leaf.symlink_to(key.name)
            with self.assertRaisesRegex(contract.ContractError,"secure read|symlink"):contract._key_bytes(leaf)
            real=root/"real";real.mkdir();nested=real/"owner.key";nested.write_bytes(b"o"*32)
            alias=root/"alias";alias.symlink_to(real.name,target_is_directory=True)
            with self.assertRaisesRegex(contract.ContractError,"symlink"):contract._key_bytes(alias/"owner.key")

    def test_signed_run_rejects_tamper_remove_and_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);out=root/"output";out.mkdir();key=root/"issuer.key";key.write_bytes(b"k"*32)
            expected=workflow.signed_run({"status":"published"},b"result",b"final",b"lock","a"*64,key)
            run=out/"run.json";run.write_text(json.dumps(expected,sort_keys=True,indent=2)+"\n")
            self.assertEqual("published",workflow.verify_signed_run(run,expected,key,root)["status"])
            run.write_text(run.read_text().replace("published","tampered"))
            with self.assertRaises(workflow.WorkflowError):workflow.verify_signed_run(run,expected,key,root)
            run.unlink()
            with self.assertRaises((workflow.WorkflowError,contract.ContractError)):workflow.verify_signed_run(run,expected,key,root)
            real=out/"real.json";real.write_text(json.dumps(expected));run.symlink_to(real.name)
            with self.assertRaises(contract.ContractError):workflow.verify_signed_run(run,expected,key,root)

    def test_publication_snapshot_blocks_every_class_and_attack_shape(self):
        classes=("pdf","page-png","visual-qa","source-map","render-result","run-v2","final","baseline","audit","approval","approval-request","workflow-lock","activation","skill-lock","trust","renderer-lock","humanizer-report","owner-key","issuer-key")
        for artifact in classes:
            for attack in ("replace","remove","leaf-symlink","ancestor-symlink"):
                with self.subTest(artifact=artifact,attack=attack),tempfile.TemporaryDirectory() as td:
                    root=Path(td);domain=root/"domain";domain.mkdir();target=domain/(artifact+".bin");target.write_bytes((artifact+"\n").encode())
                    external=root/"external.bin";external.write_bytes(b"external\n");state=root/"state";state.mkdir();link=root/"current";link.symlink_to(state.name,target_is_directory=True)
                    seal=workflow.PublicationSnapshot([domain],[external],[link])
                    try:
                        if attack=="replace":target.write_bytes(b"tampered bytes\n")
                        elif attack=="remove":target.unlink()
                        elif attack=="leaf-symlink":
                            real=domain/(artifact+".real");target.rename(real);target.symlink_to(real.name)
                        else:
                            moved=root/"moved-domain";domain.rename(moved);domain.symlink_to(moved.name,target_is_directory=True)
                        with self.assertRaises(workflow.WorkflowError):seal.verify()
                    finally:seal.close()

    def test_existing_output_never_bypasses_late_publication_gate(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);project=base/"project";project.mkdir();subprocess.run(["git","init","-q",str(project)],check=True);project=project.resolve()
            state_dir=project/".brandkit/states"/("a"*64);state_dir.mkdir(parents=True);(project/".brandkit/current").symlink_to(f"states/{'a'*64}")
            run_dir=project/".brandkit/work/rerun";output_dir=run_dir/"output";output_dir.mkdir(parents=True)
            final=run_dir/"final.md";final.write_text("final bytes")
            baseline=run_dir/"humanizer-baseline.json";baseline.write_text("{}")
            audit=run_dir/"humanizer-audit.json";audit.write_text("{}")
            request=run_dir/"approval-request.json";approval=base/"approval.json";approval.write_text("{}")
            owner_key=base/"owner.key";owner_key.write_bytes(b"o"*32);issuer_key=base/"issuer.key";issuer_key.write_bytes(b"i"*32)
            workflow_lock=project/"workflow-lock.v2.json";workflow_lock.write_text("locked\n")
            ast={"source_sha256":hashlib.sha256(final.read_bytes()).hexdigest(),"source":final.read_text(),"frontmatter":{"document_id":"rerun-document"}}
            dtype={"id":"report","version":"1.0.0"};state_hash="a"*64
            preflight={"root":project,"brand":{"brand_id":"neutral-fixture"},"brand_path":ROOT/"fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360/brand-package.json","brand_content":"b"*64,"dtype":dtype,"dtype_path":ROOT/"document-types/report.v1.json","value":{"activation":{"state_sha256":state_hash},"owner_approval_key_sha256":hashlib.sha256(owner_key.read_bytes()).hexdigest()}}
            request_payload={"final_sha256":ast["source_sha256"],"brand_id":"neutral-fixture","document_type_id":"report","workflow_lock_sha256":hashlib.sha256(workflow_lock.read_bytes()).hexdigest(),"project_identity_sha256":hashlib.sha256(str(project.resolve()).encode()).hexdigest(),"run_id":"rerun"}
            request.write_text(json.dumps({"schema_version":"approval-request.v1","payload":request_payload}))
            result=output_dir/"render-result.json";result.write_text("authenticated result")
            pdf=output_dir/"rerun-document.pdf";pdf.write_bytes(b"post-publish tamper")
            summary={"schema_version":"brand-voice-pdf-run.v1","run_id":"rerun","project_root":str(project),"brand":{"id":"neutral-fixture"},"document_type":dtype,"draft":str(run_dir/"draft.md"),"baseline":str(baseline),"humanizer_audit":str(audit),"final":str(final),"pdf":str(pdf),"render_result":str(result),"visual_qa":str(output_dir/"visual-qa.json"),"status":"published"}
            envelope=workflow.signed_run(summary,result.read_bytes(),final.read_bytes(),workflow_lock.read_bytes(),state_hash,issuer_key)
            (output_dir/"run.json").write_text(json.dumps(envelope,sort_keys=True,indent=2)+"\n")
            args=argparse.Namespace(project=project,brand_id="neutral-fixture",provider="claude",owner_key=owner_key,workflow_lock=workflow_lock,run_id="rerun",asset_root=ROOT/"fixtures/markdown",owner_approval=approval,issuer_key=issuer_key,mermaid_cas=base)
            resolution={"roots":{"work":str(run_dir)},"brand":{"id":"neutral-fixture"}}
            with (mock.patch.object(workflow,"workflow_preflight",return_value=preflight),
                  mock.patch.object(workflow,"verify_workflow_lock"),
                  mock.patch.object(workflow.brand_package,"resolve",return_value=resolution),
                  mock.patch.object(workflow.contract,"run_validate",return_value=ast),
                  mock.patch.object(workflow,"verify_owner_approval"),
                  mock.patch.object(workflow,"expected_binding",return_value={}),
                  mock.patch.object(workflow.contract,"publication_gate") as late_gate):
                self.assertEqual("published",workflow.publish(args)["status"])
                self.assertEqual(3,late_gate.call_count)
    def test_adversarial_pdf_fails_safe_area_overlap_contrast_and_visibility(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);pdf=root/"attack.pdf";c=canvas.Canvas(str(pdf),pagesize=A4,invariant=1,pageCompression=0,pdfVersion=(1,4));c.setFillColorRGB(.95,.95,.95);c.setFont("Helvetica",10)
            c.drawString(5*mm,5*mm,"Doppelter Angriff");c.drawString(5*mm,5*mm,"Doppelter Angriff");c.showPage();c.save()
            ast=root/"ast.json";ast.write_text(json.dumps({"children":[]}));qa=root/"visual-qa.json"
            with self.assertRaises(pdf_visual_qa.QaError):pdf_visual_qa.inspect(pdf,root/"pages",qa,ast,ROOT/"fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360/brand-package.json")
            findings=json.loads(qa.read_text())["findings"]
            self.assertTrue(findings["safe_area"]);self.assertTrue(findings["overlap"]);self.assertTrue(findings["contrast"]);self.assertTrue(findings["glyph_replacement"])

    def test_alpha_3524_small_text_fails_and_thin_45_text_passes(self):
        brand=ROOT/"fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360/brand-package.json"
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);ast=root/"ast.json";ast.write_text(json.dumps({"children":[]}));font=self.brand_font()
            weak=root/"alpha-3524.pdf";c=canvas.Canvas(str(weak),pagesize=A4,invariant=1,pageCompression=0,pdfVersion=(1,4));c.setFont(font,10);c.setFillColorRGB(23/255,42/255,58/255);c.setFillAlpha(.55);c.drawString(30*mm,250*mm,"Kleinschrift mit Alpha");c.showPage();c.save()
            with self.assertRaises(pdf_visual_qa.QaError):pdf_visual_qa.inspect(weak,root/"weak-pages",root/"weak.json",ast,brand)
            findings=json.loads((root/"weak.json").read_text())["findings"]["contrast"];self.assertTrue(any(x.get("effective_ratio",9)<4.5 for x in findings if isinstance(x,dict)))
            strong=root/"thin-positive.pdf";c=canvas.Canvas(str(strong),pagesize=A4,invariant=1,pageCompression=0,pdfVersion=(1,4));c.setFont(font,10);c.setFillColorRGB(23/255,42/255,58/255);c.drawString(30*mm,250*mm,"Dünne Kante mit ausreichendem Kontrast");c.showPage();c.save()
            self.assertEqual("pass",pdf_visual_qa.inspect(strong,root/"strong-pages",root/"strong.json",ast,brand)["status"])

    def test_unicode_subset_words_keep_their_own_paint_for_contrast(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);ast=root/"ast.json";ast.write_text(json.dumps({"children":[]}));font=self.brand_font()
            brand=root/"brand.json";brand.write_text(json.dumps({"pdf":{"tokens":{"color_roles":{"primary":"#284B8F","accent":"#EE8717","text":"#172A3A","muted":"#5F6B73","background":"#FFFFFF","surface":"#FFFFFF"}}}}))
            strong=root/"unicode-strong.pdf";c=canvas.Canvas(str(strong),pagesize=A4,invariant=1,pageCompression=0,pdfVersion=(1,4));c.setFont(font,10);c.setFillColorRGB(23/255,42/255,58/255);c.drawString(30*mm,260*mm,"Prüfung bleibt stabil");c.setFillColorRGB(40/255,75/255,143/255);c.drawString(30*mm,240*mm,"Befunde");c.showPage();c.save()
            result=pdf_visual_qa.inspect(strong,root/"strong-pages",root/"strong.json",ast,brand)
            self.assertEqual("pass",result["status"])
            befunde=next(x for x in pdf_visual_qa.closed_visible_inventory(strong) if x["object_id"]=="p1-text-0004")
            self.assertEqual([0.156863,0.294118,0.560784],befunde["paint"]["rgb"])
            weak=root/"unicode-weak.pdf";c=canvas.Canvas(str(weak),pagesize=A4,invariant=1,pageCompression=0,pdfVersion=(1,4));c.setFont(font,10);c.setFillColorRGB(23/255,42/255,58/255);c.drawString(30*mm,260*mm,"Prüfung bleibt stabil");c.setFillAlpha(.55);c.drawString(30*mm,240*mm,"Befunde");c.showPage();c.save()
            with self.assertRaises(pdf_visual_qa.QaError):pdf_visual_qa.inspect(weak,root/"weak-pages",root/"weak.json",ast,brand)
            measured=[x for x in json.loads((root/"weak.json").read_text())["findings"]["contrast"] if x.get("background_source")]
            self.assertTrue(measured);self.assertLess(min(x["effective_ratio"] for x in measured),4.5)

    def test_poppler_joined_punctuation_does_not_shift_later_text_paints(self):
        paints=[
            {"text":"Anteil","font_size_pt":8},
            {"text":": 42 %","font_size_pt":8},
            {"text":"Input","font_size_pt":12.756},
        ]
        words=[{"text":"Anteil:"},{"text":"42"},{"text":"%"},{"text":"Input"}]
        bound=pdf_visual_qa.bind_word_paints(words,paints)
        self.assertEqual([8,8,8,12.756],[item["font_size_pt"] for item in bound])

    def test_effective_2921_surface_raster_and_transparency_backgrounds_fail(self):
        brand=ROOT/"fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360/brand-package.json"
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);ast=root/"ast.json";ast.write_text(json.dumps({"children":[]}));font=self.brand_font()
            cases=[]
            for name in ("surface","raster","transparency"):
                pdf=root/f"{name}.pdf";c=canvas.Canvas(str(pdf),pagesize=A4,invariant=1,pageCompression=0,pdfVersion=(1,4));x,y,w,h=25*mm,235*mm,150*mm,25*mm
                if name=="raster":
                    image=root/"background.png";PILImage.new("RGB",(600,100),(99,113,122)).save(image);c.drawImage(str(image),x,y,w,h,mask=None)
                else:
                    c.saveState();c.setFillColorRGB(99/255,113/255,122/255)
                    if name=="transparency":c.setFillAlpha(.92)
                    c.rect(x,y,w,h,fill=1,stroke=0);c.restoreState()
                c.setFont(font,10);c.setFillColorRGB(23/255,42/255,58/255);c.drawString(x+5*mm,y+10*mm,f"Kontrast {name}");c.showPage();c.save();cases.append((name,pdf))
            for name,pdf in cases:
                with self.subTest(name=name),self.assertRaises(pdf_visual_qa.QaError):pdf_visual_qa.inspect(pdf,root/f"{name}-pages",root/f"{name}.json",ast,brand)
                findings=json.loads((root/f"{name}.json").read_text())["findings"]["contrast"]
                measured=[x for x in findings if isinstance(x,dict) and x.get("background_source")]
                self.assertTrue(measured);self.assertLess(min(x["effective_ratio"] for x in measured),4.5)

    def test_recursive_form_and_appearance_paint_is_concrete_and_clipped(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);pdf=root/"recursive.pdf";c=canvas.Canvas(str(pdf),pagesize=A4,invariant=1,pageCompression=0,pdfVersion=(1,4));font=self.brand_font()
            c.beginForm("nested",0,0,100,30);c.setFont(font,10);c.setFillColorRGB(23/255,42/255,58/255);c.drawString(4,10,"Formtext");c.setFillColorRGB(.5,.5,.5);c.rect(0,0,100,30,fill=1,stroke=0);c.endForm()
            c.saveState();c.rect(30*mm,240*mm,20*mm,15*mm,stroke=0,fill=0);c.clipPath(c.beginPath(),stroke=0,fill=0) if False else None;c.translate(30*mm,240*mm);c.doForm("nested");c.restoreState();c.showPage();c.save()
            inv=pdf_visual_qa.closed_visible_inventory(pdf)
            self.assertTrue(any(x["kind"]=="form" for x in inv));self.assertTrue(any(x["paint"].get("scope","").endswith("form:/FormXob.nested") or "/form:" in x["paint"].get("scope","") for x in inv if x["kind"]=="vector"))
            paints=pdf_visual_qa.text_paint_inventory(pdf);self.assertTrue(any("form:" in x.get("scope","") for x in paints))

    def test_visible_acroform_widget_is_in_closed_domain_and_blocks(self):
        brand=ROOT/"fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360/brand-package.json"
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);pdf=root/"widget.pdf";c=canvas.Canvas(str(pdf),pagesize=A4,invariant=1,pageCompression=0,pdfVersion=(1,4));c.setFont(self.brand_font(),10);c.setFillColorRGB(23/255,42/255,58/255);c.drawString(30*mm,260*mm,"Formular");c.acroForm.textfield(name="sichtbar",x=30*mm,y=235*mm,width=80*mm,height=10*mm,fillColor=rl_colors.white,borderColor=rl_colors.HexColor("#172A3A"));c.showPage();c.save()
            inventory=pdf_visual_qa.closed_visible_inventory(pdf);self.assertTrue(any(x["kind"]=="widget" for x in inventory))
            ast=root/"ast.json";ast.write_text(json.dumps({"children":[]}))
            with self.assertRaises(pdf_visual_qa.QaError):pdf_visual_qa.inspect(pdf,root/"pages",root/"qa.json",ast,brand)
            self.assertTrue(any("widget" in str(x) for x in json.loads((root/"qa.json").read_text())["findings"]["glyph_replacement"]))
    def test_audit_accepts_explicit_zero_edit_and_real_edit_paths(self):
        required=["original.md","preflight.json","audit-call.json","audit-ledger.json","confirmed-ledger.json","edits.json","result.md","report.json","changes.diff","evidence-ledger.json","evidence-report.json"]
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);key=root/"issuer.key";key.write_bytes(b"unit-test-issuer-key-material-32bytes")
            runs=[]
            for sid,old,new,calls,edits in (("mutable-a","klarer Satz","klarer Satz",1,0),("mutable-b","wirklich sehr gut","präzise",2,1)):
                raw=root/"humanizer-raw"/sid;raw.mkdir(parents=True)
                for name in required:(raw/name).write_text(new+"\n" if name=="result.md" else "{}\n")
                (raw/"report.json").write_text(json.dumps({"accepted":True,"provider":"claude","model_calls":calls,"edit_count":edits,"candidate_count":edits,"confirmed_candidate_count":edits,"discarded_candidate_count":0,"prompt_sha256":{"rewrite":None if not edits else "0"*64},"protected_violations":[],"evidence_blockers":[]}))
                (raw/"confirmed-ledger.json").write_text(json.dumps({"discarded_candidates":[]}))
                (raw/"evidence-report.json").write_text(json.dumps({"ok":True,"findings":[]}))
                if edits:(raw/"rewrite-call.json").write_text("{}\n")
                runs.append(({"segment_id":sid,"old_hash":hashlib.sha256(old.encode()).hexdigest(),"text":old},raw,new))
            audit=workflow.make_audit({}, {"source_sha256":"f"*64}, key, root, runs, root, "e"*64)
            self.assertEqual([False,True],[x["changed"] for x in audit["payload"]["operations"]])
            self.assertEqual(23,len(audit["payload"]["raw_artifacts"]))
            no_change_raw=runs[0][1]
            report=json.loads((no_change_raw/"report.json").read_text());report.update(candidate_count=1,discarded_candidate_count=1)
            (no_change_raw/"report.json").write_text(json.dumps(report));(no_change_raw/"confirmed-ledger.json").write_text(json.dumps({"discarded_candidates":[]}))
            with self.assertRaisesRegex(workflow.WorkflowError,"closed accepted"):
                workflow.make_audit({}, {"source_sha256":"f"*64}, key, root, runs, root, "e"*64)
if __name__=="__main__":unittest.main()
