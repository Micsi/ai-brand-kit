#!/usr/bin/env python3
"""Adversarial regressions for the complete Markdown v1 trust boundary."""
from __future__ import annotations
import copy, hashlib, importlib.util, inspect, io, json, os, re, shutil, stat, subprocess, tempfile, unittest, zlib
from pathlib import Path
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from jsonschema import Draft202012Validator
from tests.render_signature_harness import sign_render, verify_render

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("markdown_contract",ROOT/"scripts/markdown_contract.py"); assert SPEC and SPEC.loader
mc=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(mc)
FIXTURE=ROOT/"fixtures/markdown/valid-report.md";ASSET_ROOT=ROOT/"fixtures/markdown"
RICH_FIXTURE=ROOT/"fixtures/pdf-renderer/e2e/rich-report.md";RICH_ROOT=ROOT/"fixtures/pdf-renderer/e2e"
INVALID=json.loads((ROOT/"fixtures/markdown/invalid-cases.v1.json").read_text())["cases"]

def png():
 def chunk(kind,payload):return len(payload).to_bytes(4,"big")+kind+payload+(zlib.crc32(kind+payload)&0xffffffff).to_bytes(4,"big")
 return b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",b"\0\0\0\1\0\0\0\1\x08\x06\0\0\0")+chunk(b"IDAT",zlib.compress(b"\0\0\0\0\0"))+chunk(b"IEND",b"")
def jpeg():
 buffer=io.BytesIO();mc.Image.new("RGB",(1,1),(127,127,127)).save(buffer,format="JPEG",quality=90,optimize=False,progressive=False);return buffer.getvalue()

class MarkdownContractTests(unittest.TestCase):
 def parse(self,source=None,root=ASSET_ROOT):
  parser=mc.Parser(source if source is not None else FIXTURE.read_text(),root);ast=parser.parse();parser.validate_assets();ast["rights_evidence"]=parser.rights_evidence;return parser,ast
 def key(self,root):p=root/"issuer.key";p.write_bytes(b"external-test-key-material-32-bytes-minimum!!");return p
 def baseline(self,ast,dtype,key):return mc.create_humanizer_baseline(ast,dtype,mc._brand_package_identity()[2],"test-render-issuer",key,"nonce-1234567890abcdef","2026-08-26T12:00:00Z")
 def trust(self,key):
  engine={"name":"brandkit-pdf-renderer","version":"1.0.0","lock_path":"toolchain/pdf-renderer.production.v1.lock.json","lock_sha256":mc.sha((ROOT/"toolchain/pdf-renderer.production.v1.lock.json").read_bytes())}
  return {"schema_version":"renderer-trust.v1","environment":"test-only","status":"installed","issuer":"test-render-issuer","issuer_key_sha256":mc.sha(mc._key_bytes(key)),"engine":engine}

 def test_v1_pin_is_byte_preserved_and_v2_migration_is_closed(self):
  migration=json.loads((ROOT/"markdown-contract-migration.v1.json").read_text())
  self.assertEqual("615b303be1ed6ee7316316a98570cfa38ff9d3d0cb296ad5a6e13592d3c13cf9",mc.sha((ROOT/"markdown-contract.v1.json").read_bytes()))
  self.assertEqual("edf17d1d5429f98676c3507ebcba86c0bc08fc6968be30ebb9a2dd782572874c",mc.sha((ROOT/"schemas/markdown-contract.v1.schema.json").read_bytes()))
  self.assertEqual(migration["from"]["sha256"],mc.sha((ROOT/migration["from"]["path"]).read_bytes()))
  self.assertEqual(migration["to"]["sha256"],mc.sha((ROOT/migration["to"]["path"]).read_bytes()))
  self.assertTrue(migration["compatibility"]["v1_documents_valid_under_v2"])
  _,ast=self.parse();self.assertEqual("markdown-contract.v2",ast["contract"])

 def test_complete_presentation_callout_matrix_and_closed_attributes(self):
  anchor='::: {.note #note-main title="Hinweis"}\nEin neutraler Hinweis.\n:::'
  blocks=[]
  for kind in ("note","tip","warning","caution","important"):
   for appearance in ("default","simple","minimal"):
    icon="false" if kind=="important" and appearance=="minimal" else "true"
    blocks.append(f'::: {{.{kind} #{kind}-{appearance} title="Eigener Titel {kind} {appearance}" appearance={appearance} icon={icon}}}\nLesbarer Inhalt fuer diese Darstellung.\n:::')
  source=FIXTURE.read_text().replace(anchor,"\n\n".join(blocks))
  _,ast=self.parse(source);matrix=[n for n in ast["children"] if n["type"]=="callout" and n["attrs"]["title"].startswith("Eigener Titel")]
  self.assertEqual(15,len(matrix));self.assertEqual(15,len({n["attrs"]["id"] for n in matrix}))
  self.assertTrue(any(n["attrs"]["icon"]=="false" for n in matrix));self.assertTrue(all(n["attrs"]["title"].startswith("Eigener Titel") for n in matrix))
  with self.assertRaisesRegex(mc.ContractError,"appearance"):
   self.parse(source.replace("appearance=default","appearance=collapsed",1))
  with self.assertRaisesRegex(mc.ContractError,"icon"):
   self.parse(source.replace("icon=true","icon=maybe",1))
 def pdf(self,texts):
  buffer=io.BytesIO();writer=mc.PdfWriter()
  font=DictionaryObject({NameObject("/Type"):NameObject("/Font"),NameObject("/Subtype"):NameObject("/Type1"),NameObject("/BaseFont"):NameObject("/Helvetica")});font_ref=writer._add_object(font)
  for item in texts:
   if isinstance(item,dict):fragments=item["fragments"]
   else:
    body,footer=(item if isinstance(item,tuple) else (item,None));fragments=[(body,72,720),(footer,72,60)]
   page=writer.add_blank_page(width=595,height=842);page[NameObject("/Resources")]=DictionaryObject({NameObject("/Font"):DictionaryObject({NameObject("/F1"):font_ref})});stream=DecodedStreamObject()
   commands=[]
   for text,x,y in fragments:
    if text is not None:
     safe=text.replace("\\","\\\\").replace("(","\\(").replace(")","\\)");commands.append(f"BT /F1 12 Tf {x} {y} Td ({safe}) Tj ET")
   stream.set_data("\n".join(commands).encode("ascii"));page[NameObject("/Contents")]=writer._add_object(stream)
  writer.write(buffer);return buffer.getvalue().removesuffix(b"\n")
 def rebuilt_pdf(self,pdf,replacement,version=b"1.4",size_floor=0):
  start=pdf.rfind(b"startxref\n");xref=pdf.rfind(b"xref\n",0,start);self.assertGreaterEqual(start,0);self.assertGreaterEqual(xref,0)
  objects={int(m.group(1)):m.group(0).rstrip(b"\r\n")+b"\n" for m in re.finditer(rb"(?ms)^([0-9]+) ([0-9]+) obj\r?\n.*?^endobj\r?$",pdf[:xref])}
  self.assertIn(1,objects);objects[1]=b"1 0 obj\n"+replacement+b"\nendobj\n"
  header=re.sub(rb"\A%PDF-[0-9]+\.[0-9]+",b"%PDF-"+version,pdf[:pdf.find(b"\n")+1]);body=bytearray(header);offsets={}
  for number in sorted(objects):offsets[number]=len(body);body.extend(objects[number])
  trailer=re.search(rb"(?s)trailer\n(<<.*?>>)\nstartxref\n",pdf[xref:]);self.assertIsNotNone(trailer);trailer_dict=trailer.group(1)
  size=max(max(offsets)+1,size_floor);trailer_dict=re.sub(rb"/Size\s+[0-9]+",b"/Size "+str(size).encode(),trailer_dict)
  xref_offset=len(body);maximum=max(offsets);body.extend(f"xref\n0 {maximum+1}\n0000000000 65535 f \n".encode())
  for number in range(1,maximum+1):body.extend((f"{offsets[number]:010d} 00000 n \n" if number in offsets else "0000000000 00000 f \n").encode())
  body.extend(b"trailer\n"+trailer_dict+b"\nstartxref\n"+str(xref_offset).encode()+b"\n%%EOF");return bytes(body)
 def render(self,root,ast,baseline,key,pages=None):
  pages=pages or {"count":2,"title":{"index":1,"label":None},"content":[{"index":2,"number":1,"label":"Seite 1 von 1"}]}
  texts=["Titel"]+[("Inhalt",item["label"]) for item in pages["content"]] if pages["content"] else ["Inhalt"]
  pdf=root/"output.pdf";pdf.write_bytes(self.pdf(texts))
  payload={"final_input_sha256":ast["source_sha256"],"output_pdf":{"path":"output.pdf","sha256":mc.sha(pdf.read_bytes())},"engine":self.trust(key)["engine"],"exit":{"code":0,"errors":[],"warnings":[]},"assets":[{k:a[k] for k in ("id","path","media_type","sha256")} for a in ast["frontmatter"]["assets"]],"figures":[],"pages":pages,"nonce":"render-1234567890abcdef","created_at":"2026-08-26T12:01:00Z"}
  result=sign_render("render-result.v1",baseline["issuer"],payload,mc._key_bytes(key));path=root/"render.json";path.write_text(json.dumps(result));return path,result

 def test_partition_is_disjoint_complete_utf8_and_stably_addressed(self):
  _,ast=self.parse();data=ast["source"].encode();parts=ast["humanizer_segments"]["partition"]
  self.assertEqual((0,len(data)),(parts[0]["start_byte"],parts[-1]["end_byte"]));self.assertTrue(all(a["end_byte"]==b["start_byte"] for a,b in zip(parts,parts[1:])));self.assertEqual(len(data),sum(p["end_byte"]-p["start_byte"] for p in parts));self.assertEqual(len(parts),len({p["segment_id"] for p in parts}))
  for part in parts:
   raw=data[part["start_byte"]:part["end_byte"]];self.assertEqual(part["old_hash"],hashlib.sha256(raw).hexdigest());self.assertEqual(raw.decode(),part["text"]);self.assertIn(part["classification"],{"protected","mutable"});self.assertGreater(part["ordinal"],0)
  self.assertTrue(any("Natürliche" in p["text"] for p in parts));self.assertTrue({"frontmatter","directive","number","fact","source","table-value","diagram-code","verbatim","footnote"}.issubset({p["kind"] for p in ast["humanizer_segments"]["protected"]}))

 def test_semantic_figure_references_number_reorder_insert_and_fail_closed(self):
  source=RICH_FIXTURE.read_text()
  def parse(value):
   parser=mc.Parser(value,RICH_ROOT);ast=parser.parse();parser.validate_assets();mc.validate_figure_registry(ast);return ast
  def block(value,kind):
   match=re.search(rf"```\{{{kind}\b.*?\n```",value,re.S);self.assertIsNotNone(match);return match.group(0)
  ast=parse(source);self.assertEqual([("fig-flow-rich",1),("fig-dependencies-rich",2)],[(x["id"],x["number"]) for x in ast["figure_registry"]])
  refs=[token for node in ast["children"] for token in node.get("inline",[]) if token["type"]=="figure-ref"]
  self.assertEqual([("fig-flow-rich",1,"Abbildung 1"),("fig-dependencies-rich",2,"Abbildung 2")]*2,[(x["target"],x["figure_number"],x["reference_text"]) for x in refs])
  mermaid,graphviz=block(source,"mermaid"),block(source,"graphviz")
  swapped=parse(source.replace(mermaid,"@@MERMAID@@",1).replace(graphviz,mermaid,1).replace("@@MERMAID@@",graphviz,1))
  self.assertEqual([("fig-dependencies-rich",1),("fig-flow-rich",2)],[(x["id"],x["number"]) for x in swapped["figure_registry"]])
  inserted='''```{graphviz #fig-inserted title="Zwischenschritt" description="Zusätzliche automatisch nummerierte Figur"}\ndigraph G {\n  start [label="Start"];\n  check [label="Prüfung"];\n  start -> check;\n}\n```\n\n'''
  shifted=parse(source.replace(mermaid,inserted+mermaid,1));self.assertEqual([("fig-inserted",1),("fig-flow-rich",2),("fig-dependencies-rich",3)],[(x["id"],x["number"]) for x in shifted["figure_registry"]])
  ordinary=parse(source.replace("@fig-flow-rich","[Ablauf](#fig-flow-rich)").replace("@fig-dependencies-rich","[Abhängigkeiten](#fig-dependencies-rich)"))
  self.assertFalse(any(token["type"]=="figure-ref" for node in ordinary["children"] for token in node.get("inline",[])))
  self.assertTrue(any(token["type"]=="link" for node in ordinary["children"] for token in node.get("inline",[])))
  with self.assertRaisesRegex(mc.ContractError,"unknown diagram figures"):parse(source.replace("@fig-flow-rich","@fig-unknown",1))
  with self.assertRaisesRegex(mc.ContractError,"unknown diagram figures"):parse(source.replace(mermaid,"",1))
  with self.assertRaisesRegex(mc.ContractError,"duplicate semantic id"):parse(source.replace("#fig-dependencies-rich","#fig-flow-rich",1))

 def test_only_rewrite_api_targets_mutable_hash_and_preserves_skeleton(self):
  _,before=self.parse();dtype=mc.load_base_types()["report"]
  with tempfile.TemporaryDirectory() as td:
   key=self.key(Path(td));baseline=self.baseline(before,dtype,key);target=next(p for p in before["humanizer_segments"]["mutable"] if "Fazit" in p["text"])
   source,after=mc.rewrite_humanizer(baseline,key,[(target["segment_id"],target["old_hash"],"Das Fazit bleibt präzise und natürlich formuliert.")],ASSET_ROOT,dtype);self.assertIn("präzise",source);mc.verify_humanizer_roundtrip(before,after);self.assertEqual([p["segment_id"] for p in before["humanizer_segments"]["protected"]],[p["segment_id"] for p in after["humanizer_segments"]["protected"]])
   with self.assertRaisesRegex(mc.ContractError,"only mutable"):mc.rewrite_humanizer(baseline,key,[(before["humanizer_segments"]["protected"][0]["segment_id"],before["humanizer_segments"]["protected"][0]["old_hash"],"x")],ASSET_ROOT,dtype)
   with self.assertRaisesRegex(mc.ContractError,"stale"):mc.rewrite_humanizer(baseline,key,[(target["segment_id"],"0"*64,"x")],ASSET_ROOT,dtype)
   with self.assertRaisesRegex(mc.ContractError,"single-line"):mc.rewrite_humanizer(baseline,key,[(target["segment_id"],target["old_hash"],"x\n\ny")],ASSET_ROOT,dtype)

 def test_structural_humanizer_mutations_all_fail(self):
  _,before=self.parse();mutations=[("1. Geordneter","- Geordneter"),("| --- | --- |","| :--- | ---: |"),("Das Fazit enthält ausschließlich natürliche Prosa.","Das Fazit enthält\n\nausschließlich natürliche Prosa."),("::: {.note","::: {.warning"),("```{mermaid","~~~{mermaid")]
  for old,new in mutations:
   with self.subTest(old=old):
    try:_,after=self.parse(FIXTURE.read_text().replace(old,new,1))
    except mc.ContractError:continue
    with self.assertRaises(mc.ContractError):mc.verify_humanizer_roundtrip(before,after)

 def test_duplicate_text_and_multibyte_rewrite_are_exact(self):
  _,before=self.parse(FIXTURE.read_text().replace("Das Fazit enthält ausschließlich natürliche Prosa.","Gleicher Text. Gleicher Text."));dtype=mc.load_base_types()["report"]
  with tempfile.TemporaryDirectory() as td:
   key=self.key(Path(td));baseline=self.baseline(before,dtype,key);target=next(p for p in before["humanizer_segments"]["mutable"] if p["text"]=="Gleicher Text. Gleicher Text.");rewritten,_=mc.rewrite_humanizer(baseline,key,[(target["segment_id"],target["old_hash"],"Äußerst klar. Gleicher Text.")],ASSET_ROOT,dtype);self.assertEqual(1,rewritten.count("Äußerst klar."));self.assertEqual(1,rewritten.count("Gleicher Text."))

 def test_yaml_links_tables_callouts_and_numbers_fail_closed(self):
  original=FIXTURE.read_text();mutations=[("title: Vertragsbericht","title: &shared Vertragsbericht","anchors"),("title: Vertragsbericht","title: !!str Vertragsbericht","tags"),("#findings)","#does-not-exist)","unknown global IDs"),("| --- | --- |","| --- | --- | --- |","separator"),("Ein neutraler Hinweis.\n:::","\n:::","non-empty"),("Das Fazit enthält ausschließlich natürliche Prosa.","Das Fazit nennt 1e3 Fälle.","bare numbers"),("Das Fazit enthält ausschließlich natürliche Prosa.","Das Fazit nennt -1.234,50 Fälle.","bare numbers")]
  for old,new,error in mutations:
   with self.subTest(error=error):
    with self.assertRaisesRegex(mc.ContractError,error):self.parse(original.replace(old,new,1))

 def test_mermaid_conservative_v1_rejects_every_external_or_active_form(self):
  payloads=["A[https"+"://example.invalid/x] --> B[End]","A[<img src=x>] --> B[End]","%%{init: {}}%%\nA-->B","%%{config: {}}%%\nA-->B","click A callback","A[image: foo] --> B[End]","A[icon foo] --> B[End]","A[data:text/plain,x] --> B[End]","A[javascript:alert] --> B[End]","A[&lt;img&gt;] --> B[End]","sequenceDiagram\nA->>B: X"];original="flowchart TD\n  A[Start] --> B[Ende]"
  for payload in payloads:
   with self.subTest(payload=payload):
    with self.assertRaises(mc.ContractError):self.parse(FIXTURE.read_text().replace(original,payload,1))
  for field,payload in (("title","HTTPS"+"://example.invalid/pixel"),("description","&lt;IMG src=x&gt;"),("title","ｊａｖａｓｃｒｉｐｔ%253Aalert"),("description","%%{CONFIG: {}}%%")):
   with self.subTest(field=field,payload=payload):
    source=FIXTURE.read_text().replace(f'{field}="Ablauf"' if field=="title" else f'{field}="Zwei verbundene neutrale Schritte"',f'{field}="{payload}"',1)
    with self.assertRaises(mc.ContractError):self.parse(source)

 def test_graphviz_v1_preserves_figure_fields_and_rejects_active_or_open_dot(self):
  fence='```{graphviz #dependencies title="Abhängigkeiten" description="Gerichtete Komponenten" source_id=source-one}\ndigraph G {\n  rankdir=LR;\n  input [label="Eingabe"];\n  output [label="Ausgabe"];\n  input -> output [label="Prüfung"];\n}\n```\n\n'
  source=FIXTURE.read_text().replace("## Fazit {#conclusion}",fence+"## Fazit {#conclusion}")
  _,ast=self.parse(source);node=next(item for item in ast["children"] if item["type"]=="graphviz")
  self.assertEqual(("dependencies","Abhängigkeiten","Gerichtete Komponenten","source-one"),(node["id"],node["caption"],node["description"],node["source_id"]))
  self.assertIn("input -> output",node["code"])
  payloads=(
   'input [label="https'+r'://example.invalid"];',
   'input [image="/etc/passwd"];',
   'input [URL="file'+r':///etc/passwd"];',
   'input [label=<<IMG SRC="x">>];',
   'subgraph cluster_x { input; }',
   '@include "secrets.dot"',
   'input -> output; // comment',
   'input [label="&#104;ref"];',
   'graph [fontpath="/private"];',
   'input -- output;',
  )
  baseline='  input [label="Eingabe"];'
  for payload in payloads:
   with self.subTest(payload=payload),self.assertRaises(mc.ContractError):self.parse(source.replace(baseline,"  "+payload,1))
  with self.assertRaisesRegex(mc.ContractError,"unknown diagram source"):self.parse(source.replace('description="Gerichtete Komponenten" source_id=source-one','description="Gerichtete Komponenten" source_id=foreign-source',1))
  with self.assertRaises(mc.ContractError):self.parse(source.replace("}\n```","```",1))

 def test_graphviz_lock_schema_closes_font_scripts_and_clean_room_evidence(self):
  schema=json.loads((ROOT/"schemas/graphviz-svg-toolchain.v1.schema.json").read_text())
  value=json.loads((ROOT/"toolchain/graphviz-svg.v1.lock.json").read_text())
  validator=Draft202012Validator(schema);self.assertEqual([],list(validator.iter_errors(value)))
  attacks=[]
  for section in ("font","scripts","clean_room_evidence"):
   for key in value[section]:
    missing=copy.deepcopy(value);missing[section].pop(key);attacks.append((f"{section}-missing-{key}",missing))
   extra=copy.deepcopy(value);extra[section]["untrusted_extra"]="x";attacks.append((f"{section}-extra",extra))
  wrong=copy.deepcopy(value);wrong["font"]["archive_sha256"]="x";attacks.append(("font-wrong-hash",wrong))
  wrong=copy.deepcopy(value);wrong["scripts"]["render_sha256"]="0"*63;attacks.append(("scripts-wrong-hash",wrong))
  wrong=copy.deepcopy(value);wrong["clean_room_evidence"]["fresh_directories"]=1;attacks.append(("clean-room-count",wrong))
  wrong=copy.deepcopy(value);wrong["clean_room_evidence"]["byte_identical"]=False;attacks.append(("clean-room-bytes",wrong))
  for label,candidate in attacks:
   with self.subTest(label=label):self.assertTrue(list(validator.iter_errors(candidate)))

 def test_mermaid_lock_hashes_and_offline_clean_room_runner(self):
  lock=mc.validate_mermaid_toolchain();self.assertTrue(lock["renderer_implemented"]);self.assertTrue(lock["clean_room_evidence"]["byte_identical"])
  missing=subprocess.run([str(ROOT/"toolchain/mermaid-svg/verify-clean-rooms"),str(ROOT/"does-not-exist")],text=True,capture_output=True);self.assertNotEqual(0,missing.returncode)
  cas=os.environ.get("BRANDKIT_MERMAID_CAS")
  self.assertEqual(lock["artifact_bundle"]["path"],cas,"full runner must set the exact locked bundle path")
  env={**os.environ,"PYTHON_BIN":os.environ.get("PYTHON_BIN",os.sys.executable)};result=subprocess.run([str(ROOT/"toolchain/mermaid-svg/verify-clean-rooms"),cas],text=True,capture_output=True,env=env);self.assertEqual(0,result.returncode,result.stderr);self.assertEqual(lock["clean_room_evidence"]["svg_sha256"],result.stdout.strip().splitlines()[-1])

 def test_mermaid_bundle_verifier_blocks_file_archive_manifest_component_lock_and_path_tampering(self):
  production=ROOT/"toolchain/mermaid-svg/verify-bundle";old=Path("/Volumes/Daten/Projekte/BrandKit/external-artifacts/mermaid-svg/833beab98c8a4837d4458a5e2b131b330eedec0708a5725e8362bd3f97fc25aa")
  denied=subprocess.run([str(production),str(old)],capture_output=True,text=True);self.assertNotEqual(0,denied.returncode)
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);script_dir=root/"toolchain/mermaid-svg";script_dir.mkdir(parents=True);verifier=script_dir/"verify-bundle";shutil.copy2(production,verifier);verifier.chmod(0o755)
   staging=root/"bundle-stage";(staging/"npm-cache").mkdir(parents=True);(staging/"browser").mkdir();(staging/"node").mkdir()
   files={"npm-cache/package.bin":b"package","browser/chrome.zip":b"browser-archive","node/node.tar.gz":b"node-archive"}
   for name,data in files.items():(staging/name).write_bytes(data)
   records=[{"path":name,"sha256":hashlib.sha256(files[name]).hexdigest(),"size":len(files[name])} for name in files]
   tree=hashlib.sha256(json.dumps(records,sort_keys=True,separators=(",",":")).encode()).hexdigest();bundle=root/tree;staging.rename(bundle)
   manifest={"schema_version":"mermaid-artifact-bundle.v2","files":records};manifest_path=bundle/"files.sha256.json";manifest_path.write_text(json.dumps(manifest,sort_keys=True,separators=(",",":")))
   lock=json.loads((ROOT/"toolchain/mermaid-svg.v1.lock.json").read_text());lock["artifact_bundle"]={"path":str(bundle),"tree_sha256":tree,"manifest_sha256":hashlib.sha256(manifest_path.read_bytes()).hexdigest(),"file_count":len(records)}
   lock["browser"].update({"archive_path":"browser/chrome.zip","archive_sha256":hashlib.sha256(files["browser/chrome.zip"]).hexdigest()});lock["node"].update({"archive_path":"node/node.tar.gz","archive_sha256":hashlib.sha256(files["node/node.tar.gz"]).hexdigest()})
   lock_path=root/"toolchain/mermaid-svg.v1.lock.json"
   def save():lock_path.write_text(json.dumps(lock))
   def run(path=bundle):return subprocess.run([str(verifier),str(path)],capture_output=True,text=True)
   save();self.assertEqual(0,run().returncode)
   original=(bundle/"npm-cache/package.bin").read_bytes();(bundle/"npm-cache/package.bin").write_bytes(b"tampered");self.assertNotEqual(0,run().returncode);(bundle/"npm-cache/package.bin").write_bytes(original)
   original=(bundle/"browser/chrome.zip").read_bytes();(bundle/"browser/chrome.zip").write_bytes(b"tampered");self.assertNotEqual(0,run().returncode);(bundle/"browser/chrome.zip").write_bytes(original)
   original=manifest_path.read_bytes();manifest_path.write_bytes(original+b" ");self.assertNotEqual(0,run().returncode);manifest_path.write_bytes(original)
   for component in ("browser","node"):
    original_hash=lock[component]["archive_sha256"];lock[component]["archive_sha256"]="0"*64;save();self.assertNotEqual(0,run().returncode);lock[component]["archive_sha256"]=original_hash
   original_schema=lock["schema_version"];lock["schema_version"]="attacker-lock.v1";save();self.assertNotEqual(0,run().returncode);lock["schema_version"]=original_schema;save()
   alias=root/"external-browser-path";alias.symlink_to(bundle,target_is_directory=True);self.assertNotEqual(0,run(alias).returncode)

 def test_assets_bind_extension_mime_decode_hash_rights_and_no_polyglots(self):
  mc.validate_image_bytes(Path("a.png"),"image/png",png());mc.validate_image_bytes(Path("a.jpg"),"image/jpeg",jpeg());mc.validate_image_bytes(Path("a.svg"),"image/svg+xml",b'<svg><title>x</title><rect width="1" height="1"/></svg>')
  invalid=[(Path("a.png"),"image/png",png()+b"polyglot"),(Path("a.jpg"),"image/jpeg",jpeg()+b"x"),(Path("a.svg"),"image/svg+xml",b'<svg><script/></svg>'),(Path("a.svg"),"image/svg+xml",b'<svg><image href="https'+b'://x"/></svg>'),(Path("a.svg"),"image/svg+xml",b'<svg mystery="x"></svg>'),(Path("a.svg"),"image/svg+xml",b'<svg>text</svg>'),(Path("a.svg"),"image/svg+xml",b'<svg></svg>\n'),(Path("a.svg"),"image/svg+xml",b'<svg></svg><!--polyglot-->'),(Path("a.svg"),"image/svg+xml",b'<?pi x?><svg></svg>'),(Path("a.png"),"image/jpeg",png()),(Path("a.svg"),"image/svg+xml",b'<svg')]
  for path,mime,data in invalid:
   with self.subTest(path=path,mime=mime):
    with self.assertRaises(mc.ContractError):mc.validate_image_bytes(path,mime,data)
  with self.assertRaisesRegex(mc.ContractError,"extension"):self.parse(FIXTURE.read_text().replace("media_type: image/svg+xml","media_type: image/png",1))
  with self.assertRaisesRegex(mc.ContractError,"hash mismatch"):self.parse(FIXTURE.read_text().replace("4b6886f23a350748f85bf941b7a0568aa7375bf806e354842c5bd61f3c053e6b","f"*64,1))
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);(root/"assets").mkdir();(root/"rights").mkdir();(root/"assets/diagram.svg").symlink_to(ASSET_ROOT/"assets/diagram.svg");(root/"rights/diagram.rights.json").write_bytes((ASSET_ROOT/"rights/diagram.rights.json").read_bytes());parser=mc.Parser(FIXTURE.read_text(),root);parser.parse()
   with self.assertRaisesRegex(mc.ContractError,"symlink"):parser.validate_assets()
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);(root/"assets").mkdir();(root/"rights").mkdir();(root/"assets/diagram.svg").write_bytes((ASSET_ROOT/"assets/diagram.svg").read_bytes());e=json.loads((ASSET_ROOT/"rights/diagram.rights.json").read_text());e["asset_id"]="different-asset";e_bytes=(json.dumps(e)+"\n").encode();(root/"rights/diagram.rights.json").write_bytes(e_bytes);source=FIXTURE.read_text().replace("0aed6a8f0ccaf360c75114251d64889dad4e34160bfed839384b51f9f531e61a",mc.sha(e_bytes));parser=mc.Parser(source,root);parser.parse()
   with self.assertRaisesRegex(mc.ContractError,"bind exactly"):parser.validate_assets()

 def test_document_types_reject_nan_in_direct_path(self):
  bases=mc.load_base_types();extension=copy.deepcopy(bases["report"]);extension.update({"id":"audit-report","extends":"report"});extension["minimum_margin_mm"]=float("nan")
  with self.assertRaisesRegex(mc.ContractError,"finite"):mc.validate_document_type(extension,bases)
  for dtype in bases.values():mc.validate_document_type(dtype,bases)

 def test_production_publication_has_no_test_trust_channel_and_rejects_unpinned_key(self):
  _,before=self.parse(FIXTURE.read_text().replace("status: review","status: release",1));dtype=mc.load_base_types()["report"]
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);key=self.key(root)
   with self.assertRaisesRegex(mc.ContractError,"brand hash"):mc.create_humanizer_baseline(before,dtype,"b"*64,"test-render-issuer",key,"nonce-1234567890abcdef","2026-08-26T12:00:00Z")
   baseline=self.baseline(before,dtype,key);target=next(p for p in before["humanizer_segments"]["mutable"] if "Fazit" in p["text"]);_,after=mc.rewrite_humanizer(baseline,key,[(target["segment_id"],target["old_hash"],"Das Fazit ist freigegeben und natürlich formuliert.")],ASSET_ROOT,dtype);path,result=self.render(root,after,baseline,key);trust=self.trust(key)
   self.assertNotIn("renderer_trust",inspect.signature(mc.publication_gate).parameters);self.assertNotIn("test_only_trust",inspect.signature(mc.publication_gate).parameters)
   with self.assertRaisesRegex(mc.ContractError,"fingerprint"):mc.publication_gate(after,path,baseline,key,dtype,ASSET_ROOT)
   attacker=root/"attacker.key";attacker.write_bytes(b"attacker-controlled-self-key-material-32bytes")
   with self.assertRaisesRegex(mc.ContractError,"fingerprint"):mc._renderer_trust(attacker)
   with self.assertRaises(TypeError):mc.publication_gate(after,path,baseline,key,dtype,ASSET_ROOT,renderer_trust=trust)
   self.assertEqual(result["payload"],verify_render(result,key,trust["issuer"],trust["engine"]))
   tampered=copy.deepcopy(result);tampered["payload"]["final_input_sha256"]="0"*64
   with self.assertRaisesRegex(ValueError,"signature"):verify_render(tampered,key,trust["issuer"],trust["engine"])

 def test_production_trust_profile_is_immutable_and_cross_file_bound(self):
  original=mc.PRODUCTION_RENDERER_TRUST
  renderer_lock=ROOT/"toolchain/pdf-renderer.production.v1.lock.json";original_lock=renderer_lock.read_bytes()
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);trust=json.loads(original.read_text());path=root/"renderer-trust.v1.json";key=self.key(root)
   mutations=[("status","not-installed"),("issuer","attacker"),("issuer_key_sha256",mc.sha(mc._key_bytes(key)))]
   for field,value in mutations:
    with self.subTest(field=field):
     changed=copy.deepcopy(trust);changed[field]=value;path.write_text(json.dumps(changed));mc.PRODUCTION_RENDERER_TRUST=path
     with self.assertRaises(mc.ContractError):mc._renderer_trust(key)
   for field,value in (("lock_sha256","0"*64),("name","attacker-renderer"),("version","2.0.0"),("lock_path","toolchain/mermaid-svg.v1.lock.json")):
    with self.subTest(engine_field=field):
     changed=copy.deepcopy(trust);changed["engine"][field]=value;path.write_text(json.dumps(changed));mc.PRODUCTION_RENDERER_TRUST=path
     with self.assertRaises(mc.ContractError):mc.validate_production_renderer_trust()
   mc.PRODUCTION_RENDERER_TRUST=original
   try:
    changed_lock=json.loads(original_lock);changed_lock["implementation_status"]="installed";renderer_lock.write_text(json.dumps(changed_lock))
    with self.assertRaisesRegex(mc.ContractError,"lock differs"):mc.validate_production_renderer_trust()
   finally: renderer_lock.write_bytes(original_lock)

 def test_pdf_is_single_revision_strict_and_pagination_is_extracted(self):
  one=self.pdf(["Freigabeblatt"]);one_text=mc._strict_pdf_pages(one);self.assertEqual({"count":1,"title":{"index":1,"label":None},"content":[]},mc._pdf_pagination(one_text,True))
  multi=self.pdf(["Titel",("Inhalt","Seite 1 von 2"),("Inhalt","Seite 2 von 2")]);texts=mc._strict_pdf_pages(multi);evidence={"count":3,"title":{"index":1,"label":None},"content":[{"index":2,"number":1,"label":"Seite 1 von 2"},{"index":3,"number":2,"label":"Seite 2 von 2"}]};self.assertEqual(evidence,mc._pdf_pagination(texts,False))
  start=multi.rfind(b"startxref")
  xref=multi.rfind(b"xref\n",0,start)
  for bad in (multi+b"GARBAGE%%EOF",multi+b"\nstartxref\n0\n%%EOF",multi[:-8],multi.replace(b"startxref",b"startxreX",1),multi[:start]+b"trailer\n<<>>\n"+multi[start:],multi[:start]+b"GARBAGE\n"+multi[start:],multi[:xref]+multi[xref:start]+multi[xref:]):
   with self.subTest(tail=bad[-30:]):
    with self.assertRaises(mc.ContractError):mc._strict_pdf_pages(bad)
  shifted=multi[:xref]+b"GARBAGE\n"+multi[xref:]
  shifted=shifted[:shifted.rfind(b"startxref\n")+len(b"startxref\n")]+str(xref+8).encode()+b"\n%%EOF"
  with self.assertRaisesRegex(mc.ContractError,"indirect object"):mc._strict_pdf_pages(shifted)
  first_object=re.search(rb"(?m)^[0-9]+ [0-9]+ obj$",multi);self.assertIsNotNone(first_object)
  gap_xref=xref+8
  gap_revision=re.sub(rb"(?m)^([0-9]{10})( [0-9]{5} n )$",lambda m:f"{int(m.group(1))+8:010d}".encode()+m.group(2),multi[xref:start])
  gap=multi[:first_object.start()]+b"GARBAGE\n"+multi[first_object.start():xref]+gap_revision
  gap+=b"startxref\n"+str(gap_xref).encode()+b"\n%%EOF"
  with self.assertRaisesRegex(mc.ContractError,"unreferenced bytes"):mc._strict_pdf_pages(gap)
  with self.assertRaisesRegex(mc.ContractError,"meaningful body"):mc._pdf_pagination(mc._strict_pdf_pages(self.pdf([""])),True)
  blank=self.pdf(["Titel",""])
  with self.assertRaisesRegex(mc.ContractError,"Seite X von Y"):mc._pdf_pagination(mc._strict_pdf_pages(blank),False)
  with self.assertRaisesRegex(mc.ContractError,"no Seite"):mc._pdf_pagination(mc._strict_pdf_pages(self.pdf(["Seite 1 von 1"])),True)
  with self.assertRaisesRegex(mc.ContractError,"unlabelled title"):mc._pdf_pagination(mc._strict_pdf_pages(self.pdf(["Seite 1 von 1","Seite 1 von 1"])),False)
  with self.assertRaisesRegex(mc.ContractError,"footer band"):mc._pdf_pagination(mc._strict_pdf_pages(self.pdf(["Titel",("Inhalt Seite 1 von 1",None)])),False)
  with self.assertRaisesRegex(mc.ContractError,"meaningful non-footer"):mc._pdf_pagination(mc._strict_pdf_pages(self.pdf(["Titel",("","Seite 1 von 1")])),False)
  offpage_footer=self.pdf(["Titel",{"fragments":[("Inhalt",72,720),("Seite 1 von 1",72,-1)]}])
  with self.assertRaisesRegex(mc.ContractError,"footer band"):mc._pdf_pagination(mc._strict_pdf_pages(offpage_footer),False)
  for x,y in ((72,843),(-1,720),(596,720)):
   with self.subTest(body=(x,y)):
    offpage_body=self.pdf(["Titel",{"fragments":[("Inhalt",x,y),("Seite 1 von 1",72,60)]}])
    with self.assertRaisesRegex(mc.ContractError,"meaningful non-footer"):mc._pdf_pagination(mc._strict_pdf_pages(offpage_body),False)

 def test_pdf_classic_xref_rejects_every_object_stream_spelling(self):
  classic=self.pdf(["Titel",("Inhalt","Seite 1 von 1")]);self.assertEqual(2,len(mc._strict_pdf_pages(classic)))
  self.assertEqual(2,len(mc._strict_pdf_pages(classic.replace(b"%PDF-1.3",b"%PDF-1.4",1))))
  for version in (b"1.5",b"1.7",b"2.0"):
   with self.subTest(version=version),self.assertRaisesRegex(mc.ContractError,"classic-xref contract version"):mc._strict_pdf_pages(classic.replace(b"%PDF-1.3",b"%PDF-"+version,1))
  nonempty=b"<< /Type /ObjStm /N 1 /First 4 /Length 28 >>\nstream\n9 0 << /Hidden (opaque) >>\nendstream"
  with self.assertRaisesRegex(mc.ContractError,"classic-xref contract version"):mc._strict_pdf_pages(self.rebuilt_pdf(classic,nonempty,version=b"1.5",size_floor=10))
  variants=[nonempty,b"<< /Type /ObjStm /N 0 /First 0 /Length 0 >>\nstream\n\nendstream",b"<< /ObjStm true >>",b"<< /Type/ObjStm >>",b"<< /Type % split name\n /ObjStm >>",b"<< /Type /Obj#53tm >>",b"<< /Type /#4f#62#6a#53#74#6d >>",b"<< /Type /X#52ef >>",b"<< /XRefStm 2 0 R >>"]
  for replacement in variants:
   with self.subTest(replacement=replacement),self.assertRaisesRegex(mc.ContractError,"forbids object/xref stream"):mc._strict_pdf_pages(self.rebuilt_pdf(classic,replacement,size_floor=10))
  malformed=b"<< /Type /ObjStm# /N 0 >>"
  with self.assertRaisesRegex(mc.ContractError,"malformed #xx escape"):mc._strict_pdf_pages(self.rebuilt_pdf(classic,malformed,size_floor=10))
  harmless=b"<< /Producer (/Type /ObjStm /XRefStm) /Note <2f4f626a53746d> % /Type /XRef\n>>"
  self.assertEqual(2,len(mc._strict_pdf_pages(self.rebuilt_pdf(classic,harmless,version=b"1.4"))))

 def test_pdf_comments_end_at_cr_or_lf_before_forbidden_names(self):
  classic=self.pdf(["Titel",("Inhalt","Seite 1 von 1")])
  names=(b"/ObjStm",b"/XRef",b"/XRefStm",b"/Obj#53tm",b"/X#52ef",b"/XRef#53tm")
  for name in names:
   replacement=b"<< /Marker % CR-only comment\r /Type "+name+b" >>"
   with self.subTest(name=name),self.assertRaisesRegex(mc.ContractError,"forbids object/xref stream"):mc._strict_pdf_pages(self.rebuilt_pdf(classic,replacement,version=b"1.4"))
  for ending in (b"\r",b"\n"):
   harmless=b"<< /Marker % /ObjStm /XRef /XRefStm"+ending+b" /Type /Catalog >>"
   with self.subTest(comment_ending=ending):self.assertEqual(2,len(mc._strict_pdf_pages(self.rebuilt_pdf(classic,harmless,version=b"1.4"))))

 def test_publication_placeholder_matrix_all_nested_fields(self):
  _,ast=self.parse(FIXTURE.read_text().replace("status: review","status: release",1))
  for marker in ("TODO","FIXME","TBC","TBD","XXX","[placeholder]","lorem ipsum"):
   with self.subTest(marker=marker):
    bad=copy.deepcopy(ast);bad["rights_evidence"]["diagram-rights"]["source"]=marker
    with self.assertRaisesRegex(mc.ContractError,"placeholder"):mc.publication_gate(bad,None)
    bad=copy.deepcopy(ast);bad["frontmatter"]["sources"][0]["locator"]=marker
    with self.assertRaisesRegex(mc.ContractError,"placeholder"):mc.publication_gate(bad,None)
    bad=copy.deepcopy(ast);bad["source"] += marker
    with self.assertRaisesRegex(mc.ContractError,"placeholder"):mc.publication_gate(bad,None)

 def test_full_invalid_matrix_and_standalone_invalid_document(self):
  original=FIXTURE.read_text()
  for case in INVALID:
   if case["id"]=="open-todo":continue
   with self.subTest(case=case["id"]):
    with self.assertRaises(mc.ContractError):
     _,ast=self.parse(original.replace(case["needle"],case["replacement"],1));mc.validate_ast_document(ast,mc.load_base_types()["report"])
  with self.assertRaisesRegex(mc.ContractError,"unknown global IDs"):mc.Parser((ROOT/"fixtures/markdown/invalid-local-link.md").read_text()).parse()

if __name__=="__main__":unittest.main()
