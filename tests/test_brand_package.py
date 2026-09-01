#!/usr/bin/env python3
"""Committed real-filesystem regressions for all Brand Package v1 gates."""
from __future__ import annotations
import brotli, hashlib, io, json, os, shutil, struct, subprocess, sys, tempfile, unittest
from pathlib import Path
from fontTools.ttLib import TTFont

ROOT=Path(__file__).resolve().parents[1]; CLI=ROOT/"scripts/brand_package.py"; REGISTRY=ROOT/"fixtures/brand-registry"; INDEX=REGISTRY/"index.v1.json"
FIXTURE=REGISTRY/"releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360"
def digest(path:Path)->str:return hashlib.sha256(path.read_bytes()).hexdigest()
def tree_bytes(root:Path)->dict[str,str]:return {p.relative_to(root).as_posix():digest(p) for p in sorted(root.rglob("*")) if p.is_file()}

class BrandPackageTests(unittest.TestCase):
 def setUp(self): self.tmp=tempfile.TemporaryDirectory(); self.base=Path(self.tmp.name)
 def tearDown(self): self.tmp.cleanup()
 def cli(self,*args:object,ok=True):
  r=subprocess.run([sys.executable,str(CLI),*(str(x) for x in args)],cwd=ROOT,text=True,capture_output=True)
  if ok and r.returncode:self.fail(r.stderr)
  if not ok and not r.returncode:self.fail("unexpected pass: "+r.stdout)
  return r
 def project(self,name):
  p=self.base/name;p.mkdir();subprocess.run(["git","init","-q",str(p)],check=True);return p
 def package(self,name="package",brand="neutral-fixture",version="1.0.0"):
  p=self.base/name;shutil.copytree(FIXTURE,p);m=json.loads((p/"brand-package.json").read_text());m["brand_id"]=brand;m["version"]=version;(p/"brand-package.json").write_text(json.dumps(m,indent=2)+"\n");return p
 def manifest(self,p):return json.loads((p/"brand-package.json").read_text())
 def write_manifest(self,p,m): (p/"brand-package.json").write_text(json.dumps(m,indent=2)+"\n")
 def rehash(self,p,m):
  for kind in ("fonts","logos"):
   for a in m["assets"][kind]:a["sha256"]=digest(p/a["path"]);a["rights"]["evidence_sha256"]=digest(p/a["rights"]["evidence_path"])
  for d in m["document_types"]:d["contract_sha256"]=digest(p/d["contract"])
  self.write_manifest(p,m)
 def pin(self,project,package,engine="1.0.0"):return json.loads(self.cli("pin","--project",project,"--package",package,"--engine-version",engine).stdout)
 def set_activation(self,project,mutation):
  current=project/".brandkit/current";target=os.readlink(current);state=project/".brandkit"/target
  activation=json.loads((state/"project.json").read_text());lock_bytes=(state/"skill-lock.json").read_bytes();mutation(activation)
  activation_bytes=(json.dumps(activation,indent=2,sort_keys=True)+"\n").encode();identity=hashlib.sha256(b"brandkit-state.v1\0"+activation_bytes+b"\0"+lock_bytes).hexdigest();new=project/".brandkit/states"/identity;new.mkdir()
  (new/"project.json").write_bytes(activation_bytes);(new/"skill-lock.json").write_bytes(lock_bytes)
  replacement=project/".brandkit/.current-test";replacement.symlink_to(f"states/{identity}");os.replace(replacement,current)
 def resolve(self,project,engine="1.0.0",run="run-a",create=False,**kw):
  args=["resolve","--project",project,"--engine-version",engine,"--run-id",run]
  if create:args.append("--create-roots")
  for k,v in kw.items():args.extend(["--"+k.replace("_","-"),v])
  return json.loads(self.cli(*args).stdout)

 def test_real_registry_fixture_materializes_twice_byte_identically(self):
  a,b=self.base/"a",self.base/"b"
  self.cli("materialize","--index","fixtures/brand-registry/index.v1.json","--brand-id","neutral-fixture","--version","1.0.0","--destination",a)
  self.cli("materialize","--index",INDEX,"--brand-id","neutral-fixture","--version","1.0.0","--destination",b)
  self.assertEqual(tree_bytes(a),tree_bytes(b));self.assertEqual(self.cli("validate",a).stdout.replace(str(a),"X"),self.cli("validate",b).stdout.replace(str(b),"X"))
  idx=json.loads(INDEX.read_text());self.assertEqual({"brand_id","version","content_sha256","immutable_release_locator"},set(idx["entries"][0]))
  font=(a/"assets/fonts/neutral.woff2").read_bytes();self.assertEqual(b"wOF2",font[:4]);self.assertEqual(len(font),int.from_bytes(font[8:12],"big"))
  rights=json.loads((a/"rights/neutral-font.rights.json").read_text());self.assertEqual(("open-license","OFL-1.1"),(rights["status"],rights["license_id"]));self.assertRegex(rights["source_revision"],r"^[0-9a-f]{40}$");self.assertRegex(rights["license_evidence_sha256"],r"^[0-9a-f]{64}$")

 def test_woff2_magic_and_structure_fail_closed(self):
  for mutation in ("magic","length","directory"):
   p=self.package("woff2-"+mutation);m=self.manifest(p);f=p/m["assets"]["fonts"][0]["path"];data=bytearray(f.read_bytes())
   if mutation=="magic":data[:4]=b"wOFF"
   elif mutation=="length":data[8:12]=(len(data)+1).to_bytes(4,"big")
   else:data[12:14]=(0).to_bytes(2,"big")
   f.write_bytes(data);self.rehash(p,m);self.cli("validate",p,ok=False)

 def test_real_fonttools_decodable_woff2_private_data_is_rejected(self):
  p=self.package("woff2-private");m=self.manifest(p);f=p/m["assets"]["fonts"][0]["path"];data=bytearray(f.read_bytes());private=b"neutral foreign payload control"
  self.assertEqual(0,len(data)%4);data[8:12]=(len(data)+len(private)).to_bytes(4,"big");data[40:44]=len(data).to_bytes(4,"big");data[44:48]=len(private).to_bytes(4,"big");data.extend(private)
  decoded=TTFont(io.BytesIO(data));self.assertIn("cmap",decoded);decoded.close()
  f.write_bytes(data);self.rehash(p,m);self.cli("validate",p,ok=False)

 def test_real_fonttools_decodable_woff2_metadata_is_rejected(self):
  p=self.package("woff2-metadata");m=self.manifest(p);f=p/m["assets"]["fonts"][0]["path"];data=bytearray(f.read_bytes())
  metadata=b'<?xml version="1.0" encoding="UTF-8"?><metadata version="1.0"></metadata>';compressed=brotli.compress(metadata)
  self.assertEqual((0,0),struct.unpack(">II",data[40:48]));self.assertEqual(0,len(data)%4)
  data[8:12]=(len(data)+len(compressed)).to_bytes(4,"big");data[28:32]=len(data).to_bytes(4,"big");data[32:36]=len(compressed).to_bytes(4,"big");data[36:40]=len(metadata).to_bytes(4,"big");data.extend(compressed)
  self.assertEqual(metadata,brotli.decompress(bytes(data[-len(compressed):])))
  decoded=TTFont(io.BytesIO(data));self.assertIn("cmap",decoded);decoded.close()
  f.write_bytes(data);self.rehash(p,m);self.assertIn("forbidden WOFF2 metadata",self.cli("validate",p,ok=False).stderr)

 def test_registry_index_is_not_read_through_a_symlink(self):
  link=self.base/"index.v1.json";link.symlink_to(INDEX);self.cli("materialize","--index",link,"--brand-id","neutral-fixture","--version","1.0.0","--destination",self.base/"linked",ok=False)

 def test_every_writable_root_rejects_symlink_escape(self):
  outside=self.base/"outside";outside.mkdir()
  project=self.project("dot");(project/".brandkit").symlink_to(outside,target_is_directory=True);self.cli("pin","--project",project,"--package",FIXTURE,"--engine-version","1.0.0",ok=False);self.assertFalse((outside/"project.json").exists())
  for component in ("work","cache","tmp"):
   with self.subTest(component=component):
    p=self.project(component);self.pin(p,FIXTURE);(p/".brandkit"/component).symlink_to(outside,target_is_directory=True)
    self.cli("resolve","--project",p,"--engine-version","1.0.0","--run-id","r","--create-roots",ok=False)
  p=self.project("output");self.pin(p,FIXTURE);(p/"output").symlink_to(outside,target_is_directory=True);self.cli("resolve","--project",p,"--engine-version","1.0.0","--run-id","r","--create-roots",ok=False)

 def test_two_projects_brands_engines_and_runs_isolation_matrix(self):
  p1,p2=self.project("p1"),self.project("p2");other=self.package("other","neutral-other")
  self.pin(p1,FIXTURE,"1.0.0");self.pin(p2,other,"1.0.0")
  a=self.resolve(p1,run="r1");b=self.resolve(p1,run="r2");c=self.resolve(p2,run="r1")
  self.pin(p1,FIXTURE,"1.1.0");d=self.resolve(p1,engine="1.1.0",run="r1")
  for kind in ("work","temp","output"):self.assertNotEqual(a["roots"][kind],b["roots"][kind])
  self.assertEqual(a["roots"]["cache"],b["roots"]["cache"])
  for kind in a["roots"]:self.assertNotEqual(a["roots"][kind],c["roots"][kind]);self.assertNotEqual(a["roots"][kind],d["roots"][kind])
  self.cli("resolve","--project",p1,"--engine-version","1.1.0","--run-id","../escape",ok=False)

 def test_positive_structure_rejects_other_secret_customer_and_output_payloads(self):
  for mutation in ("other","secret-name","secret-bytes","customer-renamed","output-renamed"):
   with self.subTest(mutation=mutation):
    p=self.package(mutation,brand="neutral-"+mutation.replace("_","-"));m=self.manifest(p)
    if mutation=="other":m["assets"]["other"]=[];self.write_manifest(p,m)
    else:
     asset=m["assets"]["fonts"][0];f=p/asset["path"]
     if mutation=="secret-name": f.rename(f.with_name("private-key.woff2"));asset["path"]="assets/fonts/private-key.woff2"
     elif mutation=="secret-bytes":f.write_bytes(b"Bearer eyJhbGciOi renamed credential bytes")
     elif mutation=="customer-renamed":f.write_bytes(b"Jane Doe, 10 Main Street, account 983274")
     else:f.write_bytes(b"Quarterly report: approved final delivery")
     self.rehash(p,m)
    self.cli("validate",p,ok=False)

 def test_rights_evidence_is_dedicated_closed_and_asset_bound(self):
  for mutation in ("self","shared","mismatch","hash"):
   with self.subTest(mutation=mutation):
    p=self.package("rights-"+mutation,brand="neutral-rights-"+mutation);m=self.manifest(p);font=m["assets"]["fonts"][0];logo=m["assets"]["logos"][0]
    if mutation=="self":font["rights"]["evidence_path"]=font["path"];font["rights"]["evidence_sha256"]=font["sha256"];self.write_manifest(p,m)
    elif mutation=="shared":logo["rights"]=dict(font["rights"]);self.write_manifest(p,m)
    elif mutation=="mismatch":
     e=p/font["rights"]["evidence_path"];x=json.loads(e.read_text());x["asset_id"]="neutral-logo";e.write_text(json.dumps(x));self.rehash(p,m)
    else:font["rights"]["evidence_sha256"]="0"*64;self.write_manifest(p,m)
    self.cli("validate",p,ok=False)

 def test_closed_document_contract_and_override_intersection(self):
  p=self.project("override");self.pin(p,FIXTURE);self.set_activation(p,lambda a:a.__setitem__("overrides",{"toc":True,"section_numbering":False}));self.resolve(p)
  self.set_activation(p,lambda a:a.__setitem__("overrides",{"section_numbering":True}));self.cli("resolve","--project",p,"--engine-version","1.0.0","--run-id","r",ok=False)
  for forbidden in ({"margin_mm":0},{"rights_required":False},{"contract_sha256":"0"*64},{"engine_compatibility":"*"},{"forbidden_features":[]}):
   self.set_activation(p,lambda a,f=forbidden:a.__setitem__("overrides",f));self.cli("resolve","--project",p,"--engine-version","1.0.0","--run-id","r",ok=False)
  package=self.package("bad-contract","neutral-contract");m=self.manifest(package);contract=package/m["document_types"][0]["contract"];x=json.loads(contract.read_text());x["unknown"]=True;contract.write_text(json.dumps(x));self.rehash(package,m);self.cli("validate",package,ok=False)

 def test_brand_document_contract_rejects_nonfinite_json_numbers(self):
  for constant in ("NaN", "Infinity", "-Infinity"):
   with self.subTest(constant=constant):
    p=self.package("nonfinite-"+constant.lower().replace("-","neg"),"neutral-nonfinite");m=self.manifest(p);contract=p/m["document_types"][0]["contract"]
    raw=contract.read_text();self.assertIn('"minimum_margin_mm": 20',raw);contract.write_text(raw.replace('"minimum_margin_mm": 20','"minimum_margin_mm": '+constant,1));self.rehash(p,m)
    self.assertIn("non-finite JSON number",self.cli("validate",p,ok=False).stderr)

 def test_theme_contract_security_boundaries_are_semantic(self):
  for mutation in ("margin","font","compatibility","safety"):
   p=self.package("boundary-"+mutation,brand="neutral-boundary-"+mutation);m=self.manifest(p);contract=p/m["document_types"][0]["contract"]
   if mutation=="margin":m["pdf"]["tokens"]["page"]["margin_mm"]=19
   elif mutation=="font":m["pdf"]["tokens"]["typography_roles"]["body"]="undeclared-font"
   else:
    c=json.loads(contract.read_text())
    if mutation=="compatibility":c["engine_compatibility"]="~1.0.0"
    else:c["forbidden_features"].remove("active-content")
    contract.write_text(json.dumps(c))
   self.rehash(p,m);self.cli("validate",p,ok=False)
  p=self.package("boundary-unknown-builtin",brand="neutral-boundary-unknown-builtin");m=self.manifest(p);m["pdf"]["tokens"]["typography_roles"]["body"]="builtin:Times-Roman";self.rehash(p,m);self.cli("validate",p,ok=False)

 def test_svg_is_strict_xml_without_active_or_external_content(self):
  namespace=b"http"+b"://www.w3.org/2000/svg"; external=b"https"+b"://example.invalid/x"
  allowed=b'<svg><title>neutral-logo</title><desc>neutral-logo</desc><rect width="1" height="1"/></svg>';p=self.package("svg-semantic");m=self.manifest(p);(p/m["assets"]["logos"][0]["path"]).write_bytes(allowed);self.rehash(p,m);self.cli("validate",p)
  for payload in (b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg>&x;</svg>',b'<svg xmlns="'+namespace+b'"><script/></svg>',b'<svg xmlns="'+namespace+b'"><image href="'+external+b'"/></svg>',b'<svg><rect onclick="go()"/></svg>',b'<svg><title>foreign payload</title></svg>',b'<svg>foreign payload<rect/></svg>',b'<svg><g><title>neutral-logo</title></g></svg>',b'<svg><title>neutral-logo</title><title>neutral-logo</title></svg>',b'<svg><title>neutral-logo</title>foreign payload</svg>'):
   p=self.package("svg-"+hashlib.sha256(payload).hexdigest()[:8]);m=self.manifest(p);(p/m["assets"]["logos"][0]["path"]).write_bytes(payload);self.rehash(p,m);self.cli("validate",p,ok=False)

 def test_atomic_journal_restores_exact_bytes_and_rejects_tampering_or_missing_package(self):
  p=self.project("rollback");old=self.package("old","neutral-update","1.0.0");new=self.package("new","neutral-update","1.1.0");self.pin(p,old);old_project=(p/".brandkit/project.json").read_bytes();old_lock=(p/".brandkit/skill-lock.json").read_bytes()
  self.pin(p,new);self.cli("rollback","--project",p);self.assertEqual(old_project,(p/".brandkit/project.json").read_bytes());self.assertEqual(old_lock,(p/".brandkit/skill-lock.json").read_bytes())
  q=self.project("tampered");self.pin(q,old);self.pin(q,new);journal=q/".brandkit/project.journal.json";j=json.loads(journal.read_text());j["previous_state"]["project_sha256"]="0"*64;journal.write_text(json.dumps(j));self.cli("rollback","--project",q,ok=False)
  z=self.project("missing");self.pin(z,old);self.pin(z,new);j=json.loads((z/".brandkit/project.journal.json").read_text());prev=json.loads((z/".brandkit"/j["previous_state"]["locator"]/"project.json").read_text());shutil.rmtree(z/prev["package_path"]);self.cli("rollback","--project",z,ok=False)
  a,b=self.project("journal-a"),self.project("journal-b");self.pin(a,old);self.pin(a,new);self.pin(b,old);self.pin(b,new);shutil.copyfile(a/".brandkit/project.journal.json",b/".brandkit/project.journal.json");self.cli("rollback","--project",b,ok=False)

 def test_skill_lock_is_exact_content_addressed_and_checked(self):
  p=self.project("lock");self.pin(p,FIXTURE,"1.0.0");lock=json.loads((p/".brandkit/skill-lock.json").read_text());self.assertEqual({"schema_version","engine","skill"},set(lock));self.assertEqual({"name","version","source_locator","content_sha256","release"},set(lock["skill"]));self.assertEqual(f"snapshot:scripts/brand_package.py@sha256:{digest(CLI)}",lock["skill"]["source_locator"]);self.assertEqual(CLI.read_bytes(),(p/lock["skill"]["release"]/"brand_package.py").read_bytes());self.resolve(p)
  release=p/lock["skill"]["release"]/"brand_package.py";release.chmod(0o600);release.write_bytes(release.read_bytes()+b"tamper");self.cli("resolve","--project",p,"--engine-version","1.0.0","--run-id","r",ok=False)

 def test_precedence_and_migration_remain_fail_closed(self):
  p=self.project("precedence");self.pin(p,FIXTURE);self.assertEqual("explicit-cli-path",self.resolve(p,package=FIXTURE)["source"]);self.assertEqual("project-activation",self.resolve(p)["source"])
  materialized=self.base/"materialized";self.cli("materialize","--index",INDEX,"--brand-id","neutral-fixture","--version","1.0.0","--destination",materialized);r=self.project("registry-sequence");self.pin(r,materialized);self.assertEqual("project-activation",self.resolve(r)["source"]);self.cli("resolve","--project",r,"--registry",REGISTRY,"--engine-version","1.0.0","--run-id","r",ok=False)
  legacy=self.base/"legacy.json";legacy.write_text(json.dumps({"brand_id":"neutral-fixture","version":"1.0.0"}));q=self.project("migration");self.cli("migrate","--project",q,"--legacy",legacy,"--package",FIXTURE,"--engine-version","1.0.0")
  legacy.write_text(json.dumps({"brand_id":"wrong","version":"1.0.0"}));self.cli("migrate","--project",q,"--legacy",legacy,"--package",FIXTURE,"--engine-version","1.0.0",ok=False)

if __name__=="__main__":unittest.main()
