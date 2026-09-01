#!/usr/bin/env python3
from __future__ import annotations
import hashlib,json,os,subprocess,sys,tempfile,time,unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];CLI=ROOT/"scripts/impeccable_adapter.py";BRAND=ROOT/"scripts/brand_package.py"
sys.path.insert(0,str(ROOT/"scripts"));import impeccable_adapter as adapter_module
FIXTURE=ROOT/"fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360"
CHECKOUT=ROOT.parents[1]/"external-sources/impeccable"
COMPONENTS=[{"id":"primary-action","name":"Primary action","category":"button","description":"Neutral primary action primitive.","tokens":{"background":"#222222","color":"#ffffff","padding":"0.75rem 1rem"}},{"id":"text-field","name":"Text field","category":"input","description":"Neutral text entry primitive.","tokens":{"border":"1px solid #777777","padding":"0.75rem"}},{"id":"main-navigation","name":"Main navigation","category":"nav","description":"Neutral navigation primitive.","tokens":{"display":"flex","gap":"1rem"}},{"id":"status-chip","name":"Status chip","category":"chip","description":"Neutral compact status primitive.","tokens":{"border-radius":"9999px","padding":"0.25rem 0.5rem"}},{"id":"content-card","name":"Content card","category":"card","description":"Neutral grouped-content primitive.","tokens":{"border":"1px solid #dddddd","padding":"1rem"}}]
PRODUCT={"schema_version":"brandkit-impeccable-product.v1","platform":"adaptive","users":"Design teams maintaining governed products.","purpose":"Project confirmed design work without fabricated product truth.","positioning":"Binds a reviewed brand release to a pinned design engine.","operating_context":"A version-controlled product repository.","capabilities_constraints":"Runtime capabilities stay off until explicitly granted.","brand_commitments":"Use only the activated Brand Package.","evidence":"The bound package and repository files are the available evidence.","principles":["Keep product truth explicit.","Keep provider behavior reversible.","Keep visual scope namespaced."],"accessibility":"Preserve platform accessibility conventions.","representative_components":COMPONENTS}

class ImpeccableTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.runtime_home=self.base/"runtime-home";self.runtime_home.mkdir()
 def tearDown(self):self.temp.cleanup()
 def cmd(self,args,expected=0,env=None,cwd=ROOT):
  actual_env=dict(os.environ);actual_env["HOME"]=str(self.runtime_home)
  if env:actual_env.update(env)
  r=subprocess.run(args,cwd=cwd,text=True,capture_output=True,env=actual_env)
  self.assertEqual(expected,r.returncode,f"stdout={r.stdout}\nstderr={r.stderr}");return r
 def project(self,name="project"):
  p=self.base/name;p.mkdir();self.cmd(["git","init","-q",str(p)]);self.cmd([sys.executable,str(BRAND),"pin","--project",str(p),"--package",str(FIXTURE),"--engine-version","1.0.0"])
  target=p/".brandkit/impeccable-product.json";target.write_text(json.dumps(PRODUCT,indent=2)+"\n");self.cmd(["git","-C",str(p),"add","."]);self.cmd(["git","-C",str(p),"-c","user.name=Test","-c","user.email=test@example.invalid","commit","-qm","fixture"]);return p
 def adapter(self,p,command="sync",expected=0,env=None):return self.cmd([sys.executable,str(CLI),command,"--project",str(p)],expected,env).stdout
 def approve(self,p,home,caps=("live",),ttl=60):
  args=[sys.executable,str(CLI),"approve","--project",str(p),"--home",str(home),"--evidence","current explicit test instruction","--ttl",str(ttl)]
  for cap in caps:args.extend(["--capability",cap])
  return json.loads(self.cmd(args).stdout)["receipt_id"]
 def inventory(self,p,files):
  content=json.loads((p/".brandkit/project.json").read_text())["brand"]["content_sha256"]
  value={"schema_version":"brandkit-ui-token-inventory.v1","project_root":str(p.resolve()),"project_commit":subprocess.run(["git","-C",str(p),"rev-parse","HEAD"],text=True,capture_output=True,check=True).stdout.strip(),"brand_content_sha256":content,"files":[{"path":x.relative_to(p).as_posix(),"sha256":hashlib.sha256(x.read_bytes()).hexdigest(),"surface":"web" if x.suffix==".astro" else "css","tokens_complete":True} for x in files],"tokens":[t for x in files for t in adapter_module.token_lexemes(x,p)],"review":{"complete":True,"reverse_sync":False}}
  value["inventory_sha256"]=hashlib.sha256(adapter_module.canonical(value)).hexdigest();return value
 def rotation_fixture(self,name):
  home=self.base/f"{name}-home";home.mkdir();release=Path(json.loads(self.cmd([sys.executable,str(CLI),"materialize","--checkout",str(CHECKOUT),"--home",str(home)]).stdout)["release"]);self.cmd([sys.executable,str(CLI),"activate","--home",str(home)]);p=self.project(f"{name}-project");receipt=self.approve(p,home,caps=("live","browser","network"));self.cmd([sys.executable,str(CLI),"grant","--project",str(p),"--home",str(home),"--approval-receipt",receipt]);return home,p,release
 def test_verify_real_detached_pin_and_complete_inventory(self):
  value=json.loads(self.cmd([sys.executable,str(CLI),"verify","--checkout",str(CHECKOUT)]).stdout);self.assertEqual({"providers":17,"routes":23,"references":39,"scripts":108,"roles":5,"blobs":155},value["inventory"])
 def test_real_materialize_activate_discovery_guards_and_rollback(self):
  home=self.base/"home";home.mkdir();mat=json.loads(self.cmd([sys.executable,str(CLI),"materialize","--checkout",str(CHECKOUT),"--home",str(home)]).stdout);release=Path(mat["release"])
  self.assertEqual(hashlib.sha256((CHECKOUT/"LICENSE").read_bytes()).hexdigest(),hashlib.sha256((release/"LICENSE").read_bytes()).hexdigest());self.assertTrue((release/"NOTICE.md").is_file())
  active=json.loads(self.cmd([sys.executable,str(CLI),"activate","--home",str(home)]).stdout);status=json.loads(self.cmd([sys.executable,str(CLI),"status","--home",str(home)]).stdout)
  self.assertTrue(all(x["discovered"] for x in status["providers"].values()));self.assertEqual({release.resolve()},{Path(active[x]).resolve() for x in ("claude","codex","hermes")})
  p=self.project("guard");guard=release/"scripts/capability_guard.py";self.cmd([sys.executable,str(guard),"live"],77,cwd=p)
  receipt=self.approve(p,home);self.cmd([sys.executable,str(CLI),"grant","--project",str(p),"--home",str(home),"--approval-receipt",receipt])
  self.cmd([sys.executable,str(guard),"live"],cwd=p,env={"HOME":str(home)});self.cmd([sys.executable,str(guard),"network"],77,cwd=p,env={"HOME":str(home)})
  self.assertEqual("rolled-back",json.loads(self.cmd([sys.executable,str(CLI),"rollback","--home",str(home)]).stdout)["status"]);self.assertEqual("inactive",json.loads(self.cmd([sys.executable,str(CLI),"status","--home",str(home)]).stdout)["status"])
 def test_greenfield_pin_exact_product_design_and_upstream_context(self):
  p=self.project();first=json.loads(self.adapter(p));self.assertEqual("ok",first["status"]);self.assertEqual([],json.loads(self.adapter(p))["written"])
  product=(p/"PRODUCT.md").read_text();self.assertIn("<!-- impeccable:product-schema 1 -->",product);self.assertIn("\n\nadaptive\n",product);self.assertNotIn("TODO",product)
  design=(p/"DESIGN.md").read_text();self.assertNotIn("#777777",design);self.assertNotIn("Normative tokens",design)
  release_home=self.base/"ctx-home";release_home.mkdir();release=Path(json.loads(self.cmd([sys.executable,str(CLI),"materialize","--checkout",str(CHECKOUT),"--home",str(release_home)]).stdout)["release"])
  result=self.cmd(["node",str(release/"scripts/context.mjs")],cwd=p);output=result.stdout+result.stderr;self.assertNotIn("CONTEXT_STALE",output);self.assertIn('"platform": "adaptive"',output);self.assertNotIn("product-schema-legacy",output)
 def test_missing_product_input_blocks_without_runtime_todo(self):
  p=self.project();(p/".brandkit/impeccable-product.json").unlink();self.adapter(p,expected=1);self.assertFalse((p/"PRODUCT.md").exists())
 def test_existing_ui_requires_complete_bound_inventory(self):
  p=self.project();(p/"app.css").write_text(":root{--accent:oklch(50% .2 20)} body{color:rgb(1 2 3)}")
  value=json.loads(self.adapter(p,expected=3));self.assertEqual(1,value["ui_findings"]);self.assertFalse((p/"PRODUCT.md").exists());self.assertIn("inventory",(p/".brandkit/review/impeccable-design-contract.md").read_text())
  content=json.loads((p/".brandkit/project.json").read_text())["brand"]["content_sha256"];ui={"schema_version":"brandkit-ui-token-inventory.v1","project_root":str(p.resolve()),"project_commit":subprocess.run(["git","-C",str(p),"rev-parse","HEAD"],text=True,capture_output=True,check=True).stdout.strip(),"brand_content_sha256":content,"files":[{"path":"app.css","sha256":hashlib.sha256((p/"app.css").read_bytes()).hexdigest(),"surface":"css","tokens_complete":True}],"tokens":adapter_module.token_lexemes(p/"app.css",p),"review":{"complete":True,"reverse_sync":False}}
  ui["inventory_sha256"]=hashlib.sha256(adapter_module.canonical(ui)).hexdigest()
  (p/".brandkit/ui-token-inventory.json").write_text(json.dumps(ui));self.assertEqual("ok",json.loads(self.adapter(p))["status"])
 def test_parent_symlink_escape_is_rejected(self):
  p=self.project();outside=self.base/"outside";outside.mkdir();(p/".impeccable").symlink_to(outside,target_is_directory=True);self.adapter(p,expected=1);self.assertEqual([],list(outside.iterdir()))
 def test_injected_failure_restores_all_bytes_and_clears_journal(self):
  p=self.project();env=dict(os.environ);env["BRANDKIT_IMPECCABLE_FAIL_AFTER"]="2";before={x.relative_to(p).as_posix():x.read_bytes() for x in p.rglob("*") if x.is_file()}
  self.adapter(p,expected=1,env=env);after={x.relative_to(p).as_posix():x.read_bytes() for x in p.rglob("*") if x.is_file()};self.assertEqual(before,after);self.assertFalse((p/".brandkit/transactions/impeccable-sync.json").exists())
 def test_collision_gate_rejects_distinct_source(self):
  home=self.base/"collision-home";home.mkdir();self.cmd([sys.executable,str(CLI),"materialize","--checkout",str(CHECKOUT),"--home",str(home)]);other=home/".claude/skills/impeccable";other.mkdir(parents=True);(other/"SKILL.md").write_text("---\nname: impeccable\n---\n");self.cmd([sys.executable,str(CLI),"activate","--home",str(home)],1)

 def test_late_activation_history_collision_leaves_no_links_or_current(self):
  home=self.base/"late-home";home.mkdir();self.cmd([sys.executable,str(CLI),"materialize","--checkout",str(CHECKOUT),"--home",str(home)]);history=home/".agents/skill-state/impeccable/history";history.parent.mkdir(parents=True);history.write_text("collision");self.cmd([sys.executable,str(CLI),"activate","--home",str(home)],1)
  self.assertFalse((home/".agents/skill-store/impeccable/current").exists());self.assertTrue(all(not (x.exists() or x.is_symlink()) for x in adapter_module.provider_paths(home)))

 def test_lifecycle_rejects_current_collisions_external_previous_and_tampered_state(self):
  for kind in ("regular","external"):
   home=self.base/f"home-{kind}";home.mkdir();self.cmd([sys.executable,str(CLI),"materialize","--checkout",str(CHECKOUT),"--home",str(home)]);current=home/".agents/skill-store/impeccable/current"
   if kind=="regular":current.write_text("foreign")
   else:
    outside=self.base/"outside-release";outside.mkdir(exist_ok=True);current.symlink_to(outside,target_is_directory=True)
   self.cmd([sys.executable,str(CLI),"activate","--home",str(home)],1)
   self.assertEqual("foreign",current.read_text()) if kind=="regular" else self.assertEqual(outside.resolve(),current.resolve())
  home=self.base/"state-home";home.mkdir();self.cmd([sys.executable,str(CLI),"materialize","--checkout",str(CHECKOUT),"--home",str(home)]);self.cmd([sys.executable,str(CLI),"activate","--home",str(home)])
  victim=home/"victim";victim.symlink_to(home/".agents/skill-store/impeccable/current");state=home/".agents/skill-state/impeccable/current.json";value=json.loads(state.read_text());value["created"]=["victim"];state.chmod(0o644);state.write_text(json.dumps(value));self.cmd([sys.executable,str(CLI),"rollback","--home",str(home)],1);self.assertTrue(victim.is_symlink())

 def test_release_pin_rejects_mutation_even_with_recomputed_marker(self):
  home=self.base/"release-home";home.mkdir();release=Path(json.loads(self.cmd([sys.executable,str(CLI),"materialize","--checkout",str(CHECKOUT),"--home",str(home)]).stdout)["release"])
  for p in [release,release/"reference",release/"reference/init.md",release/"ADAPTER-RELEASE.json"]:p.chmod(0o755 if p.is_dir() else 0o644)
  (release/"reference/init.md").write_text((release/"reference/init.md").read_text()+"\ntampered adapted release\n")
  marker=json.loads((release/"ADAPTER-RELEASE.json").read_text());marker["materialized_tree_sha256"]=adapter_module.tree_hash(release,True);(release/"ADAPTER-RELEASE.json").write_text(json.dumps(marker))
  self.cmd([sys.executable,str(CLI),"materialize","--checkout",str(CHECKOUT),"--home",str(home)],1)

 def test_grant_tamper_expiry_copy_revoke_and_exact_capability(self):
  home=self.base/"grant-home";home.mkdir();release=Path(json.loads(self.cmd([sys.executable,str(CLI),"materialize","--checkout",str(CHECKOUT),"--home",str(home)]).stdout)["release"]);p=self.project("grant-a");q=self.project("grant-b");guard=release/"scripts/capability_guard.py"
  receipt=self.approve(p,home,ttl=2);self.cmd([sys.executable,str(CLI),"grant","--project",str(p),"--home",str(home),"--approval-receipt",receipt]);grant=p/".brandkit/impeccable-capabilities.json";self.cmd([sys.executable,str(guard),"live"],cwd=p,env={"HOME":str(home)});self.cmd([sys.executable,str(guard),"network"],77,cwd=p,env={"HOME":str(home)})
  original=grant.read_bytes();v=json.loads(original);v["capabilities"].append("network");grant.write_text(json.dumps(v));self.cmd([sys.executable,str(guard),"network"],77,cwd=p,env={"HOME":str(home)});grant.write_bytes(original)
  (q/".brandkit/impeccable-capabilities.json").write_bytes(original);self.cmd([sys.executable,str(guard),"live"],77,cwd=q,env={"HOME":str(home)});self.cmd([sys.executable,str(CLI),"revoke","--project",str(p),"--home",str(home)]);self.assertFalse(grant.exists());grant.write_bytes(original);self.cmd([sys.executable,str(guard),"live"],77,cwd=p,env={"HOME":str(home)})

 def test_all_runtime_entrypoints_are_guarded_and_update_has_owner(self):
  home=self.base/"routes-home";home.mkdir();release=Path(json.loads(self.cmd([sys.executable,str(CLI),"materialize","--checkout",str(CHECKOUT),"--home",str(home)]).stdout)["release"]);p=self.project("routes")
  matrix=json.loads((ROOT/"impeccable-adapter.v1.json").read_text())["capability_matrix"]
  for relative in list(matrix["direct_entrypoints"])+list(matrix["import_safe_direct_checks"]):self.cmd(["node",str(release/"scripts"/relative)],77,cwd=p,env={"HOME":str(home)})
  self.assertIn("update",matrix["direct_entrypoints"]["pin.mjs"]);self.assertEqual("serve-question.mjs",matrix["indirect_owned"]["lib/open-system-browser.mjs"]["owner"])
  module=(release/"scripts/lib/open-system-browser.mjs").as_uri();probe=f'import {{openSystemBrowser}} from {json.dumps(module)}; openSystemBrowser("about:blank", {{spawnImpl:()=>{{throw new Error("must not spawn")}}}});';self.cmd(["node","--input-type=module","-e",probe],77,cwd=p,env={"HOME":str(home)})

 def test_browser_iifes_fail_closed_and_real_live_server_bundle_dom_happy_path(self):
  import re,socket,urllib.request
  home=self.base/"browser-home";home.mkdir();release=Path(json.loads(self.cmd([sys.executable,str(CLI),"materialize","--checkout",str(CHECKOUT),"--home",str(home)]).stdout)["release"]);scripts=release/"scripts";p=self.project("browser-project")
  for name in ("live-browser-session.js","live-browser-dom.js","live-browser.js"):
   source=(scripts/name).read_text();self.assertIn("(function",source);self.assertNotIn("node:",source);self.assertFalse((scripts/f".brandkit-upstream-{name}").exists())
  assembler=(scripts/"live/browser-script-parts.mjs").as_uri();denied=f'''import vm from "node:vm"; import {{resolveLiveBrowserScriptParts,readLiveBrowserScriptParts}} from {json.dumps(assembler)};
const parts=readLiveBrowserScriptParts(resolveLiveBrowserScriptParts({json.dumps(str(scripts))})); const dom=parts.find(p=>p.name==="dom-helpers"); const win={{document:{{body:{{appendChild(){{throw new Error("mutation")}}}}}}}}; win.window=win; vm.runInNewContext(dom.source,win); if(win.__IMPECCABLE_LIVE_DOM__) throw new Error("DOM export without authenticated capability");'''
  self.cmd(["node","--input-type=module","-e",denied],cwd=p,env={"HOME":str(home)})
  receipt=self.approve(p,home,caps=("live","browser","network"),ttl=4);self.cmd([sys.executable,str(CLI),"grant","--project",str(p),"--home",str(home),"--approval-receipt",receipt]);grant=json.loads((p/".brandkit/impeccable-capabilities.json").read_text())
  with socket.socket() as sock:sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
  env=dict(os.environ);env["HOME"]=str(home);proc=subprocess.Popen(["node",str(release/"scripts/.brandkit-upstream-live-server.mjs"),f"--port={port}"],cwd=p,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
  try:
   info_path=p/".impeccable/live/server.json"
   for _ in range(100):
    if info_path.is_file():break
    if proc.poll() is not None:self.fail("live server exited: "+(proc.stdout.read() if proc.stdout else ""))
    time.sleep(.05)
   self.assertTrue(info_path.is_file());info=json.loads(info_path.read_text());origin="http"+"://127.0.0.1:5173";request=urllib.request.Request(("http"+f"://127.0.0.1:{port}/live.js?token={info['token']}"),headers={"Referer":origin+"/app"});body=urllib.request.urlopen(request,timeout=5).read().decode();bundle=p/"served-live.js";bundle.write_text(body)
   self.assertEqual(3,body.count("// --- impeccable live script part:"));self.assertIn("session-state",body);self.assertIn("dom-helpers",body);self.assertIn("browser-ui",body)
   match=re.search(r"window\.__BRANDKIT_IMPECCABLE_BUNDLE_CAPABILITY__ = (\{[^\n]+\});",body);self.assertIsNotNone(match);context=json.loads(match.group(1));self.assertEqual(info["token"],context["route_token"]);self.assertEqual(grant["nonce"],context["grant_nonce"]);self.assertNotEqual(context["route_token"],context["grant_nonce"])
   probe=f'''import fs from "node:fs"; import vm from "node:vm"; import {{spawnSync}} from "node:child_process";
const body=fs.readFileSync({json.dumps(str(bundle))},"utf8"); let mutations=0,validations=0;
class XHR {{ open(method,url,async){{if(method!=="POST"||async!==false)throw new Error("validation must be synchronous POST");this.url=url}} setRequestHeader(name,value){{(this.headers??={{}})[name]=value}} send(payload){{validations++;const r=spawnSync("curl",["-sS","-o","/dev/null","-w","%{{http_code}}","-H","Origin: {origin}","-H","Content-Type: application/json","--data-binary",payload,this.url],{{encoding:"utf8"}});this.status=Number(r.stdout)}} }}
const document={{body:{{appendChild(){{mutations++}}}},head:{{appendChild(){{mutations++}}}},getElementById(){{return null}}}}; const win={{XMLHttpRequest:XHR,document,__IMPECCABLE_LIVE_INIT__:true}};win.window=win;vm.runInNewContext(body,win);
if(!win.__IMPECCABLE_LIVE_SESSION__||!win.__IMPECCABLE_LIVE_DOM__)throw new Error("served IIFE exports missing");
const context=win.__BRANDKIT_IMPECCABLE_BUNDLE_CAPABILITY__;if(win.__IMPECCABLE_TOKEN__!==context.route_token||context.route_token===context.grant_nonce)throw new Error("wire token domains collapsed");
const helpers=win.__IMPECCABLE_LIVE_DOM__.createLiveBrowserDomHelpers({{prefix:"imp-",document}});helpers.uiAppend({{}});if(mutations!==1||validations<5)throw new Error("authenticated same-origin DOM happy path failed");
function blocked(label){{let failed=false;try{{helpers.uiAppend({{}})}}catch{{failed=true}}if(!failed||mutations!==1)throw new Error(label+" mutated")}}
win.__BRANDKIT_IMPECCABLE_BUNDLE_CAPABILITY__={{...context,grant_nonce:"0".repeat(64)}};blocked("grant nonce tamper");win.__BRANDKIT_IMPECCABLE_BUNDLE_CAPABILITY__=context;
win.__IMPECCABLE_TOKEN__="tampered-route-token";blocked("route token tamper");win.__IMPECCABLE_TOKEN__=context.route_token;
const revoked=spawnSync({json.dumps(sys.executable)},[{json.dumps(str(CLI))},"revoke","--project",{json.dumps(str(p))},"--home",{json.dumps(str(home))}],{{encoding:"utf8"}});if(revoked.status!==0)throw new Error("revoke failed: "+revoked.stderr);blocked("revoked context");
const wait=Math.max(0,context.expiresAt-Date.now()+50);Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,wait);blocked("expired context");'''
   self.cmd(["node","--input-type=module","-e",probe],cwd=p,env={"HOME":str(home)})
  finally:
   proc.terminate()
   try:proc.wait(timeout=5)
   except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
   if proc.stdout:proc.stdout.close()

 def test_live_server_authenticates_browser_binding_on_every_validation(self):
  import re,socket,urllib.error,urllib.request
  home=self.base/"browser-http-home";home.mkdir();release=Path(json.loads(self.cmd([sys.executable,str(CLI),"materialize","--checkout",str(CHECKOUT),"--home",str(home)]).stdout)["release"]);p=self.project("browser-http-project");receipt=self.approve(p,home,caps=("live","browser","network"));self.cmd([sys.executable,str(CLI),"grant","--project",str(p),"--home",str(home),"--approval-receipt",receipt])
  with socket.socket() as sock:sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
  env=dict(os.environ);env["HOME"]=str(home);proc=subprocess.Popen(["node",str(release/"scripts/.brandkit-upstream-live-server.mjs"),f"--port={port}"],cwd=p,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
  try:
   info_path=p/".impeccable/live/server.json"
   for _ in range(100):
    if info_path.is_file():break
    if proc.poll() is not None:self.fail("live server exited: "+(proc.stdout.read() if proc.stdout else ""))
    time.sleep(.05)
   info=json.loads(info_path.read_text());origin="http"+"://127.0.0.1:5173";request=urllib.request.Request(("http"+f"://127.0.0.1:{port}/live.js?token={info['token']}"),headers={"Referer":origin+"/app"});body=urllib.request.urlopen(request,timeout=5).read().decode();match=re.search(r"window\.__BRANDKIT_IMPECCABLE_BUNDLE_CAPABILITY__ = (\{[^\n]+\});",body);self.assertIsNotNone(match);context=json.loads(match.group(1))
   def validate(value):
    data=json.dumps(value,separators=(",",":")).encode();req=urllib.request.Request(context["validationUrl"],data=data,headers={"Origin":origin,"Content-Type":"application/json"},method="POST")
    try:return urllib.request.urlopen(req,timeout=5).status
    except urllib.error.HTTPError as exc:return exc.code
   self.assertEqual(info["token"],context["route_token"]);self.assertEqual(64,len(context["grant_nonce"]));self.assertEqual(204,validate(context))
   for field,value in (("route_token","wrong-route-token"),("grant_nonce","0"*64),("projectRoot","/foreign/project")):
    self.assertEqual(403,validate({**context,field:value}),field)
   self.cmd([sys.executable,str(CLI),"revoke","--project",str(p),"--home",str(home)]);self.assertEqual(403,validate(context))
  finally:
   proc.terminate()
   try:proc.wait(timeout=5)
   except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
   if proc.stdout:proc.stdout.close()

 def test_receipt_one_time_and_rotation_preserves_rollback_but_kills_old_grant(self):
  home=self.base/"rotate-home";home.mkdir();release=Path(json.loads(self.cmd([sys.executable,str(CLI),"materialize","--checkout",str(CHECKOUT),"--home",str(home)]).stdout)["release"]);self.cmd([sys.executable,str(CLI),"activate","--home",str(home)]);p=self.project("rotate-project");receipt=self.approve(p,home);self.cmd([sys.executable,str(CLI),"grant","--project",str(p),"--home",str(home),"--approval-receipt",receipt]);self.cmd([sys.executable,str(CLI),"grant","--project",str(p),"--home",str(home),"--approval-receipt",receipt],1);old=(p/".brandkit/impeccable-capabilities.json").read_bytes();self.cmd([sys.executable,str(CLI),"rotate-secret","--home",str(home)]);(p/".brandkit/impeccable-capabilities.json").write_bytes(old);self.cmd([sys.executable,str(release/"scripts/capability_guard.py"),"live"],77,cwd=p,env={"HOME":str(home)});self.cmd([sys.executable,str(CLI),"rollback","--home",str(home)])

 def test_rotation_injected_error_after_every_write_rolls_back_immediately(self):
  # Seven targets in this activated/granted fixture: journal + images + secret commit + six state replaces.
  for point in range(1,21):
   home,p,_=self.rotation_fixture(f"rotate-fail-{point}");root=adapter_module.state_root(home);before={x.relative_to(root).as_posix():x.read_bytes() for x in root.rglob("*") if x.is_file()}
   self.cmd([sys.executable,str(CLI),"rotate-secret","--home",str(home)],1,env={"BRANDKIT_IMPECCABLE_ROTATE_FAIL_AFTER":str(point)})
   after={x.relative_to(root).as_posix():x.read_bytes() for x in root.rglob("*") if x.is_file() and "rotation/" not in x.relative_to(root).as_posix()};self.assertEqual(before,after);self.assertFalse((root/"rotation.json").exists());self.cmd([sys.executable,str(CLI),"status","--home",str(home)]);self.cmd([sys.executable,str(CLI),"revoke","--project",str(p),"--home",str(home)]);self.cmd([sys.executable,str(CLI),"rollback","--home",str(home)])

 def test_rotation_os_exit_after_every_write_recovers_status_revoke_and_rollback(self):
  for point in range(1,21):
   home,p,_=self.rotation_fixture(f"rotate-crash-{point}");root=adapter_module.state_root(home);self.cmd([sys.executable,str(CLI),"rotate-secret","--home",str(home)],86,env={"BRANDKIT_IMPECCABLE_ROTATE_CRASH_AFTER":str(point)});status=json.loads(self.cmd([sys.executable,str(CLI),"status","--home",str(home)]).stdout);self.assertEqual("active",status["status"]);self.assertFalse((root/"rotation.json").exists());secret=adapter_module.secret_path(home).read_bytes();self.assertTrue(all(secret not in x.read_bytes() for x in p.rglob("*") if x.is_file()));self.cmd([sys.executable,str(CLI),"revoke","--project",str(p),"--home",str(home)]);self.cmd([sys.executable,str(CLI),"rollback","--home",str(home)])

 def test_rotation_journal_is_closed_and_tampering_never_mutates_state(self):
  home,_,_=self.rotation_fixture("rotate-tamper");root=adapter_module.state_root(home);self.cmd([sys.executable,str(CLI),"rotate-secret","--home",str(home)],86,env={"BRANDKIT_IMPECCABLE_ROTATE_CRASH_AFTER":"1"});before={x.relative_to(root).as_posix():x.read_bytes() for x in root.rglob("*") if x.is_file()};journal=root/"rotation.json";value=json.loads(journal.read_text());value["foreign"]="victim";journal.write_text(json.dumps(value));snapshot={x.relative_to(root).as_posix():x.read_bytes() for x in root.rglob("*") if x.is_file()};self.cmd([sys.executable,str(CLI),"status","--home",str(home)],1);self.assertEqual(snapshot,{x.relative_to(root).as_posix():x.read_bytes() for x in root.rglob("*") if x.is_file()});self.assertNotEqual(before["rotation.json"],journal.read_bytes())

 def test_nested_design_yaml_and_exact_v2_sidecar(self):
  manifest={"brand_id":"nested","version":"1.0.0","shared_visual_tokens":{"typography":{"body":{"fontFamily":"Inter, sans-serif","fontWeight":400}},"components":{"button":{"backgroundColor":"{colors.primary}","padding":"8px"}}},"assets":{"fonts":[],"logos":[]}}
  text=adapter_module.design_md(manifest).decode();self.assertIn("  body:\n    fontFamily: \"Inter, sans-serif\"\n    fontWeight: 400",text);self.assertNotIn('{"fontFamily"',text)
  value=json.loads(adapter_module.sidecar(manifest,"0"*64,PRODUCT));self.assertEqual(2,value["schemaVersion"]);self.assertEqual("2026-08-25T12:17:17Z",value["generatedAt"]);self.assertEqual(5,len(value["components"]));self.assertNotIn("tokens",value);adapter_module.validate_json(value,"impeccable-design-sidecar.v2.schema.json","sidecar")

 def test_astro_full_inventory_empty_tokens_and_ui_symlinks_fail_closed(self):
  p=self.project("astro");astro=p/"src/App.astro";astro.parent.mkdir();astro.write_text('<div style="color: oklch(50% .2 20)">x</div>');self.adapter(p,expected=3)
  inv=self.inventory(p,[astro]);inv["tokens"]=[];unsigned={k:v for k,v in inv.items() if k!="inventory_sha256"};inv["inventory_sha256"]=hashlib.sha256(adapter_module.canonical(unsigned)).hexdigest();(p/".brandkit/ui-token-inventory.json").write_text(json.dumps(inv));self.adapter(p,expected=3)
  (p/".brandkit/ui-token-inventory.json").write_text(json.dumps(self.inventory(p,[astro])));self.assertEqual("ok",json.loads(self.adapter(p))["status"])
  q=self.project("ui-link");outside=self.base/"outside.css";outside.write_text("body{color:#fff}");(q/"linked.css").symlink_to(outside);self.adapter(q,expected=1);self.assertFalse((q/"PRODUCT.md").exists())
  r=self.project("server-template");(r/"view.php").write_text('<div style="color:#fff">x</div>');self.adapter(r,expected=3);self.assertFalse((r/"PRODUCT.md").exists())

 def test_theme_token_kinds_and_duplicate_files_are_exactly_rejected(self):
  p=self.project("theme");theme=p/"theme.ts";theme.write_text('export const theme = { fontFamily: "Inter", borderRadius: "12%", boxShadow: "none", duration: 200, primary: "#fff" };\n');self.adapter(p,expected=3);tokens=adapter_module.token_lexemes(theme,p);self.assertTrue({"font","radius","shadow","motion","color"}<=set(x["kind"] for x in tokens));inv=self.inventory(p,[theme]);inv["files"].append(dict(inv["files"][0]));inv["inventory_sha256"]=hashlib.sha256(adapter_module.canonical({k:v for k,v in inv.items() if k!="inventory_sha256"})).hexdigest();(p/".brandkit/ui-token-inventory.json").write_text(json.dumps(inv));self.adapter(p,expected=3)

 def test_theme_alternative_keys_are_nonempty_and_comments_prose_strings_do_not_match(self):
  p=self.project("theme-alt");theme=p/"theme.ts";theme.write_text('''// font: "Fake", spacing: 99, colorScheme: "fake"\nconst prose = "font: Fake, gap: 8, colorPrimary: #fff";\nexport const theme = { font: "Inter", spacing: 4, gap: 2, colorScheme: "brand", colorPrimary: "#fff", radius: 12, shadow: "none", motionDuration: 180 };\n''');tokens=adapter_module.token_lexemes(theme,p);self.assertTrue(tokens);self.assertEqual({"font","spacing","color","radius","shadow","motion"},set(x["kind"] for x in tokens));self.assertFalse(any("Fake" in x["value"] or x["value"]=="99" for x in tokens));inv=self.inventory(p,[theme]);(p/".brandkit/ui-token-inventory.json").write_text(json.dumps(inv));self.assertEqual("ok",json.loads(self.adapter(p))["status"])
  theme.write_text('export const theme = { mysteryThemeScale: 3 };\n');inv=self.inventory(p,[theme]);(p/".brandkit/ui-token-inventory.json").write_text(json.dumps(inv));self.adapter(p,expected=3)

 def test_quoted_theme_keys_are_scanned_and_computed_keys_require_review(self):
  p=self.project("theme-quoted");theme=p/"theme.ts";theme.write_text('''// "font": "Fake"
const prose = '"spacing": 99';
export const theme = { "font": "Inter", 'spacing': 4, "colorScheme": "brand", "mysteryThemeScale": 3 };
''');tokens=adapter_module.token_lexemes(theme,p);self.assertEqual({"font","spacing","color"},set(x["kind"] for x in tokens));self.assertFalse(any(x["value"]=="99" or "Fake" in x["value"] for x in tokens));self.assertIn("mysteryThemeScale",json.dumps(adapter_module.plausible_unknowns(theme)))
  inv=self.inventory(p,[theme]);(p/".brandkit/ui-token-inventory.json").write_text(json.dumps(inv));self.adapter(p,expected=3)
  theme.write_text('export const theme = { [dynamicThemeKey]: 3, ["font"]: "Inter" };\n');self.assertEqual([],adapter_module.token_lexemes(theme,p));self.assertTrue(all("computed property requires review" in x for x in adapter_module.plausible_unknowns(theme)));inv=self.inventory(p,[theme]);(p/".brandkit/ui-token-inventory.json").write_text(json.dumps(inv));self.adapter(p,expected=3)

 def test_foreign_tampered_and_unconfined_journals_never_mutate(self):
  for field,value in (("path","victim.txt"),("backup","../../outside"),("staging","/tmp/outside")):
   p=self.project(f"journal-{field}");victim=p/"victim.txt";victim.write_text("keep")
   tx={"schema_version":"brandkit-impeccable-transaction.v2","transaction_id":"a"*32,"run_id":"b"*32,"project_root":str(p),"project_commit":"0"*40,"brand_content_sha256":"0"*64,"backup":".brandkit/transactions/preimages/"+"a"*32,"staging":".brandkit/transactions/staging/"+"a"*32,"entries":[{"path":"PRODUCT.md","preimage_sha256":None,"postimage_sha256":"0"*64}],"hmac_sha256":"0"*64}
   if field=="path":tx["entries"][0]["path"]=value
   else:tx[field]=value
   journal=p/".brandkit/transactions/impeccable-sync.json";journal.parent.mkdir(parents=True,exist_ok=True);journal.write_text(json.dumps(tx));self.adapter(p,expected=1);self.assertEqual("keep",victim.read_text())

 def test_real_process_crash_recovers_partial_replaces_byte_exactly(self):
  p=self.project("crash");before={x.relative_to(p).as_posix():x.read_bytes() for x in p.rglob("*") if x.is_file()};self.adapter(p,expected=86,env={"BRANDKIT_IMPECCABLE_CRASH_AFTER":"2"});self.assertTrue((p/".brandkit/transactions/impeccable-sync.json").exists());self.cmd([sys.executable,str(CLI),"rotate-secret","--home",str(self.runtime_home)],1);self.adapter(p,"check",expected=2)
  after={x.relative_to(p).as_posix():x.read_bytes() for x in p.rglob("*") if x.is_file()};self.assertEqual(before,after);self.assertFalse((p/".brandkit/transactions/impeccable-sync.json").exists())

 def test_stage_hash_tamper_blocks_recovery_without_further_mutation(self):
  p=self.project("stage-tamper");self.adapter(p,expected=86,env={"BRANDKIT_IMPECCABLE_CRASH_AFTER":"1"});journal=json.loads((p/".brandkit/transactions/impeccable-sync.json").read_text());stage=p/journal["staging"]
  remaining=next(x for x in stage.rglob("*") if x.is_file());remaining.write_text("tampered stage");snapshot={x.relative_to(p).as_posix():x.read_bytes() for x in p.rglob("*") if x.is_file()};self.adapter(p,expected=1);self.assertEqual(snapshot,{x.relative_to(p).as_posix():x.read_bytes() for x in p.rglob("*") if x.is_file()})

if __name__=="__main__":unittest.main()
