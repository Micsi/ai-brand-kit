#!/usr/bin/env python3
"""Pinned Impeccable lifecycle and fail-closed BrandKit context adapter."""
from __future__ import annotations
import argparse, difflib, hashlib, hmac, html, json, os, re, secrets, shutil, stat, subprocess, sys, tempfile, time
from pathlib import Path, PurePosixPath
from typing import Any
import brand_package

ROOT=Path(__file__).resolve().parents[1]
CONTRACT=json.loads((ROOT/"impeccable-adapter.v1.json").read_text())
DEFAULT_CHECKOUT=ROOT.parents[1]/"external-sources/impeccable"
VERSION=CONTRACT["adapted_release"]["marker"]["version"]
PRODUCT_INPUT=".brandkit/impeccable-product.json"; UI_INVENTORY=".brandkit/ui-token-inventory.json"; JOURNAL=".brandkit/transactions/impeccable-sync.json"
UI_SUFFIXES={".css",".scss",".sass",".less",".html",".htm",".jsx",".tsx",".vue",".svelte",".astro",".ex",".heex",".eex",".php",".erb",".twig",".liquid",".hbs",".handlebars",".njk",".jinja",".jinja2",".pug",".jade",".cshtml",".razor",".mustache",".js",".mjs",".cjs",".ts",".mts",".cts",".swift",".kt",".kts",".dart",".xml",".storyboard",".xib",".xaml"}
UI_NAMES={"tailwind.config.js","tailwind.config.cjs","tailwind.config.mjs","tailwind.config.ts","theme.js","theme.ts","tokens.js","tokens.ts"}
SKIP_PARTS={".git",".brandkit",".impeccable","node_modules","output","dist","build",".next"}
ARTIFACTS={"PRODUCT.md","DESIGN.md",".impeccable/design.json",".impeccable/config.json",".brandkit/impeccable-policy.json",".brandkit/review/impeccable-design-contract.md"}
CAPABILITIES={"hooks","update","browser","live","image_generation","network"}
LEGACY_RELEASE_HASHES={"4.1.1+fcd7622.brandkit2":"ec05883837f6b26496ea7ab7cdadabc17ff4a08faa6d44584820aa6b28b40ce6","4.1.1+fcd7622.brandkit3":"9db0867e912966b51b7a557da882ecc77be0ed043ba493b7c769aebcb56583ea","4.1.1+fcd7622.brandkit4":"05af06b42dce9db7d06f8305e041f7b6678615b0717a5767d39dc83d5c540073","4.1.1+fcd7622.brandkit5":"de2b5d49350a801125426682da6bf29c6d7cff7001ea3c116d1882bc35375edf"}
class AdapterError(RuntimeError): pass

def json_bytes(v:Any)->bytes:return (json.dumps(v,indent=2,sort_keys=True,ensure_ascii=False)+"\n").encode()
def sha(v:bytes)->str:return hashlib.sha256(v).hexdigest()
def canonical(v:Any)->bytes:return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
def state_root(home:Path)->Path:return home.resolve()/".agents/skill-state/impeccable"
def secret_path(home:Path)->Path:return state_root(home)/"auth.key"
def key_id(secret:bytes)->str:return sha(secret)[:16]
def auth_secret(home:Path,create:bool=False)->bytes:
 path=secret_path(home)
 if create and not path.exists():
  path.parent.mkdir(parents=True,exist_ok=True);fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  try:os.write(fd,secrets.token_bytes(32))
  finally:os.close(fd)
 try:mode=path.lstat().st_mode
 except FileNotFoundError:raise AdapterError("adapter authentication secret missing")
 if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or stat.S_IMODE(mode)!=0o600:raise AdapterError("adapter authentication secret must be a 0600 regular file")
 value=path.read_bytes()
 if len(value)!=32:raise AdapterError("adapter authentication secret must contain exactly 32 bytes")
 return value
def seal(value:dict[str,Any],secret:bytes)->dict[str,Any]:
 raw={**value,"key_id":key_id(secret)};return {**raw,"hmac_sha256":hmac.new(secret,canonical(raw),hashlib.sha256).hexdigest()}
def unseal(value:dict[str,Any],secret:bytes,label:str)->dict[str,Any]:
 if not isinstance(value,dict) or set(value).difference({"hmac_sha256"})==set(value):raise AdapterError(f"{label} is not sealed")
 raw={k:v for k,v in value.items() if k!="hmac_sha256"}
 if raw.get("key_id")!=key_id(secret):raise AdapterError(f"{label} key identifier is stale")
 expected=hmac.new(secret,canonical(raw),hashlib.sha256).hexdigest()
 if not hmac.compare_digest(str(value.get("hmac_sha256","")),expected):raise AdapterError(f"{label} authentication failed")
 return raw
def unseal_activation(value:dict[str,Any],secret:bytes)->dict[str,Any]:
 if isinstance(value,dict) and value.get("schema_version")=="brandkit-impeccable-activation.v2" and "key_id" not in value:
  raw={k:v for k,v in value.items() if k!="hmac_sha256"};expected=hmac.new(secret,canonical(raw),hashlib.sha256).hexdigest()
  if not hmac.compare_digest(str(value.get("hmac_sha256","")),expected):raise AdapterError("legacy activation state authentication failed")
  return raw
 return unseal(value,secret,"activation state")
def sealed_ledger(home:Path,name:str,secret:bytes)->dict[str,Any]:
 path=state_root(home)/name
 if not path.exists():return {"schema_version":f"brandkit-impeccable-{name[:-5]}.v1","entries":[]}
 if path.is_symlink() or not path.is_file():raise AdapterError(f"unsafe {name} ledger")
 value=unseal(json.loads(path.read_text()),secret,name);return {k:v for k,v in value.items() if k!="key_id"}
def write_ledger(home:Path,name:str,value:dict[str,Any],secret:bytes)->None:
 path=state_root(home)/name;atomic_json(path,seal(value,secret));path.chmod(0o600)
def transaction_ledger_add(home:Path,root:Path,transaction_id:str,secret:bytes)->None:
 ledger=sealed_ledger(home,"active-transactions.json",secret);ledger["entries"].append({"project_root":str(root),"transaction_id":transaction_id});write_ledger(home,"active-transactions.json",ledger,secret)
def transaction_ledger_remove(home:Path,root:Path,transaction_id:str,secret:bytes)->None:
 ledger=sealed_ledger(home,"active-transactions.json",secret);ledger["entries"]=[x for x in ledger["entries"] if not (x.get("project_root")==str(root) and x.get("transaction_id")==transaction_id)];write_ledger(home,"active-transactions.json",ledger,secret)
def git(checkout:Path,*args:str)->str:
 r=subprocess.run(["git","-C",str(checkout),*args],text=True,capture_output=True)
 if r.returncode: raise AdapterError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
 return r.stdout.strip()
def validate_json(value:Any,schema_name:str,label:str)->None:
 try: from jsonschema import Draft202012Validator
 except ImportError as exc: raise AdapterError("jsonschema is required") from exc
 schema=json.loads((ROOT/"schemas"/schema_name).read_text());errors=sorted(Draft202012Validator(schema).iter_errors(value),key=lambda e:list(e.absolute_path))
 if errors: raise AdapterError(f"{label} is invalid: "+"; ".join(f"{'.'.join(map(str,e.absolute_path)) or '<root>'}: {e.message}" for e in errors))

def checkout_inventory(checkout:Path)->dict[str,Any]:
 u=CONTRACT["upstream"]
 if git(checkout,"rev-parse","HEAD")!=u["commit"]:raise AdapterError("checkout commit differs from pin")
 if git(checkout,"rev-parse","HEAD^{tree}")!=u["tree"]:raise AdapterError("checkout tree differs from pin")
 if git(checkout,"rev-parse",f"HEAD:{u['canonical_skill_subtree']}")!=u["canonical_skill_tree"]:raise AdapterError("skill tree differs from pin")
 symbolic=subprocess.run(["git","-C",str(checkout),"symbolic-ref","-q","HEAD"],capture_output=True)
 if symbolic.returncode==0:raise AdapterError("checkout must be detached")
 if git(checkout,"status","--porcelain"):raise AdapterError("checkout must be clean")
 if git(checkout,"config","--get","remote.origin.pushurl")!="DISABLED":raise AdapterError("checkout push URL must be DISABLED")
 if sha((checkout/"LICENSE").read_bytes())!=u["license_sha256"]:raise AdapterError("LICENSE hash differs")
 if sha((checkout/u["notice_path"]).read_bytes())!=u["notice_sha256"]:raise AdapterError("NOTICE hash differs")
 raw=git(checkout,"ls-tree","-r","--full-tree","HEAD",u["canonical_skill_subtree"],"LICENSE",u["notice_path"])
 blobs=[]
 for line in raw.splitlines():
  meta,path=line.split("\t",1);blobs.append((path,meta.split()[2]))
 payload="".join(f"{p}\0{o}\n" for p,o in sorted(blobs)).encode();skill=checkout/u["canonical_skill_subtree"]
 providers=sorted(p.relative_to(checkout).as_posix() for p in checkout.rglob("skills/impeccable/SKILL.md"))
 refs=sorted(p.relative_to(checkout).as_posix() for p in (skill/"reference").rglob("*") if p.is_file())
 scripts=sorted(p.relative_to(checkout).as_posix() for p in (skill/"scripts").rglob("*") if p.is_file())
 roles=sorted(p.relative_to(checkout).as_posix() for p in (skill/"agents").rglob("*") if p.is_file())
 routes=[line.split("`",2)[1].split()[0] for line in (skill/"SKILL.md").read_text().splitlines() if line.startswith("| `") and "](" in line]
 inv=CONTRACT["inventory"];manifest=json.loads((ROOT/inv["path_manifest"]).read_text())
 if set(manifest)!={"schema_version","commit","references","scripts"} or manifest["schema_version"]!="impeccable-upstream-inventory.v1" or manifest["commit"]!=u["commit"] or manifest["references"]!=refs or manifest["scripts"]!=scripts:raise AdapterError("upstream path inventory drift")
 actual={"provider_skill_outputs":providers,"routes":routes,"roles":roles,"references":refs,"scripts":scripts,"blob_count":len(blobs),"blob_set_sha256":sha(payload)}
 for key in ("provider_skill_outputs","routes","roles","blob_count","blob_set_sha256"):
  if actual[key]!=inv[key]:raise AdapterError(f"inventory drift at {key}")
 if len(refs)!=inv["reference_count"] or len(scripts)!=inv["script_count"]:raise AdapterError("inventory count drift")
 for feature,paths in inv["runtime_features"].items():
  for path in paths:
   if not (checkout/path).is_file():raise AdapterError(f"runtime feature {feature} missing {path}")
 for path in inv["hook_manifests"]:
  if not (checkout/path).is_file():raise AdapterError(f"hook manifest missing {path}")
 return actual
def verify(checkout:Path)->dict[str,Any]:
 c=checkout.resolve(strict=True);a=checkout_inventory(c)
 return {"status":"verified","checkout":str(c),"commit":CONTRACT["upstream"]["commit"],"tree":CONTRACT["upstream"]["tree"],"skill_tree":CONTRACT["upstream"]["canonical_skill_tree"],"inventory":{"providers":len(a["provider_skill_outputs"]),"routes":len(a["routes"]),"references":len(a["references"]),"scripts":len(a["scripts"]),"roles":len(a["roles"]),"blobs":a["blob_count"]}}

def tree_hash(root:Path,skip_marker:bool=False)->str:
 d=hashlib.sha256()
 for p in sorted(root.rglob("*"),key=lambda x:x.relative_to(root).as_posix()):
  rel=p.relative_to(root).as_posix().encode()
  if skip_marker and p.name=="ADAPTER-RELEASE.json":continue
  if p.is_symlink():d.update(b"L\0"+rel+b"\0"+os.readlink(p).encode()+b"\0")
  elif p.is_file():d.update(b"F\0"+rel+b"\0"+p.read_bytes()+b"\0")
  elif p.is_dir():d.update(b"D\0"+rel+b"\0")
 return d.hexdigest()
GUARD_PY='''#!/usr/bin/env python3
import hashlib,hmac,json,os,stat,subprocess,sys,time
from pathlib import Path
EXPECTED="fcd7622cd2d8e2b09344ba8ede9fcac82cec4e70"
CAPS={"hooks","update","browser","live","image_generation","network"}
def canonical(v):return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
def tree_hash(root):
 d=hashlib.sha256()
 for p in sorted(root.rglob("*"),key=lambda x:x.relative_to(root).as_posix()):
  rel=p.relative_to(root).as_posix().encode()
  if p.name=="ADAPTER-RELEASE.json":continue
  if p.is_symlink():d.update(b"L\\0"+rel+b"\\0"+os.readlink(p).encode()+b"\\0")
  elif p.is_file():d.update(b"F\\0"+rel+b"\\0"+p.read_bytes()+b"\\0")
  elif p.is_dir():d.update(b"D\\0"+rel+b"\\0")
 return d.hexdigest()
def main():
 root=Path.cwd().resolve();path=root/".brandkit/impeccable-capabilities.json"
 try:v=json.loads(path.read_text())
 except Exception as exc:print(f"impeccable capability gate: explicit grant missing: {exc}",file=sys.stderr);return 77
 keys={"schema_version","grant_id","nonce","issued_at","expires_at","approval_receipt_id","project_root","project_commit","brand_content_sha256","upstream_commit","adapted_release_sha256","capabilities","key_id","hmac_sha256"}
 release=Path(__file__).resolve().parents[1]
 if set(v)!=keys or v.get("schema_version")!="brandkit-impeccable-capabilities.v1" or v.get("project_root")!=str(root) or v.get("upstream_commit")!=EXPECTED or v.get("adapted_release_sha256")!=tree_hash(release):print("impeccable capability gate: identity/schema mismatch",file=sys.stderr);return 77
 if not isinstance(v.get("capabilities"),list) or not v["capabilities"] or set(v["capabilities"])-CAPS or len(v["capabilities"])!=len(set(v["capabilities"])):print("impeccable capability gate: map not closed",file=sys.stderr);return 77
 now=int(time.time())
 if type(v.get("issued_at")) is not int or type(v.get("expires_at")) is not int or not v["issued_at"]<=now<v["expires_at"]:print("impeccable capability gate: grant expired/not-yet-valid",file=sys.stderr);return 77
 try:
  commit=subprocess.run(["git","-C",str(root),"rev-parse","HEAD"],text=True,capture_output=True,check=True).stdout.strip()
  brand=json.loads((root/".brandkit/project.json").read_text())["brand"]["content_sha256"]
  key=Path.home()/".agents/skill-state/impeccable/auth.key";mode=key.lstat().st_mode
  if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or stat.S_IMODE(mode)!=0o600:raise ValueError("unsafe secret")
  secret=key.read_bytes()
  if len(secret)!=32:raise ValueError("unsafe secret length")
  if v.get("key_id")!=hashlib.sha256(secret).hexdigest()[:16]:raise ValueError("stale key")
  raw={k:x for k,x in v.items() if k!="hmac_sha256"};mac=hmac.new(secret,canonical(raw),hashlib.sha256).hexdigest()
  ledger_path=Path.home()/".agents/skill-state/impeccable/revocations.json"
  ledger=json.loads(ledger_path.read_text()) if ledger_path.exists() else {"schema_version":"brandkit-impeccable-revocations.v1","entries":[],"key_id":v["key_id"],"hmac_sha256":""}
  if ledger_path.exists():
   ledger_raw={k:x for k,x in ledger.items() if k!="hmac_sha256"};ledger_mac=hmac.new(secret,canonical(ledger_raw),hashlib.sha256).hexdigest()
   if ledger_raw.get("key_id")!=v["key_id"] or not hmac.compare_digest(ledger_mac,ledger.get("hmac_sha256","")):raise ValueError("revocation ledger authentication failed")
   if any(x.get("grant_id")==v["grant_id"] and x.get("nonce")==v["nonce"] for x in ledger.get("entries",[])):raise ValueError("grant revoked")
 except Exception as exc:print(f"impeccable capability gate: binding unavailable: {exc}",file=sys.stderr);return 77
 if commit!=v["project_commit"] or brand!=v["brand_content_sha256"] or not hmac.compare_digest(mac,v["hmac_sha256"]):print("impeccable capability gate: binding/authentication failed",file=sys.stderr);return 77
 args=sys.argv[1:];binding_json=False
 if args and args[0]=="--binding-json":binding_json=True;args=args[1:]
 denied=[x for x in args if x not in v["capabilities"]]
 if denied:print("impeccable capability gate: denied "+",".join(denied),file=sys.stderr);return 77
 if binding_json:
  fields=("project_root","project_commit","brand_content_sha256","grant_id","key_id","adapted_release_sha256","capabilities","issued_at","expires_at","nonce")
  print(json.dumps({x:v[x] for x in fields},sort_keys=True,separators=(",",":")))
 return 0
if __name__=="__main__":raise SystemExit(main())
'''
def wrapper(original:str,caps:list[str])->str:
 args=", ".join(json.dumps(x) for x in caps)
 return f'''#!/usr/bin/env node
import {{spawnSync}} from "node:child_process";import {{dirname,join}} from "node:path";import {{fileURLToPath}} from "node:url";
const here=dirname(fileURLToPath(import.meta.url));const g=spawnSync("python3",[join(here,"capability_guard.py"),{args}],{{stdio:"inherit",cwd:process.cwd()}});if(g.status!==0)process.exit(g.status??77);
const r=spawnSync(process.execPath,[join(here,{json.dumps(original)}),...process.argv.slice(2)],{{stdio:"inherit",cwd:process.cwd(),env:process.env}});process.exit(r.status??1);
'''
def import_safe_guard(caps:list[str])->str:
 args=", ".join(json.dumps(x) for x in caps)
 return f'''import {{spawnSync as brandkitSpawnSync}} from "node:child_process";
import {{realpathSync as brandkitRealpathSync}} from "node:fs";
import {{dirname as brandkitDirname,resolve as brandkitResolve,join as brandkitJoin}} from "node:path";
import {{fileURLToPath as brandkitFileURLToPath}} from "node:url";
function brandkitCapabilityGuard() {{ const h=brandkitDirname(brandkitFileURLToPath(import.meta.url)); const g=brandkitSpawnSync("python3",[brandkitJoin(h,"capability_guard.py"),{args}],{{stdio:"inherit",cwd:process.cwd()}}); if(g.status!==0)process.exit(g.status??77); }}
if (process.argv[1] && brandkitRealpathSync(process.argv[1]) === brandkitRealpathSync(brandkitFileURLToPath(import.meta.url))) brandkitCapabilityGuard();
'''
def indirect_guard(caps:list[str])->str:
 args=", ".join(json.dumps(x) for x in caps)
 return f'''import {{spawnSync as brandkitSpawnSync}} from "node:child_process";
import {{dirname as brandkitDirname,join as brandkitJoin}} from "node:path";
import {{fileURLToPath as brandkitFileURLToPath}} from "node:url";
function brandkitCapabilityGuard() {{ const h=brandkitDirname(brandkitFileURLToPath(import.meta.url)); const g=brandkitSpawnSync("python3",[brandkitJoin(h,"..","capability_guard.py"),{args}],{{stdio:"inherit",cwd:process.cwd()}}); if(g.status!==0)process.exit(g.status??77); }}
'''
def browser_iife_guard()->str:
 return '''
  const brandkitNativeRoot = typeof globalThis !== 'undefined' ? globalThis : null;
  const brandkitContext = brandkitNativeRoot && brandkitNativeRoot.__BRANDKIT_IMPECCABLE_BUNDLE_CAPABILITY__;
  const brandkitExpectedRouteToken = brandkitNativeRoot && brandkitNativeRoot.__IMPECCABLE_TOKEN__;
  const brandkitExpectedContext = brandkitContext && JSON.stringify(brandkitContext);
  const brandkitNativeXhr = brandkitNativeRoot && brandkitNativeRoot.XMLHttpRequest;
  function brandkitAssertBrowserCapability() {
    const caps = brandkitContext && brandkitContext.capabilities;
    if (!brandkitContext || brandkitNativeRoot.__BRANDKIT_IMPECCABLE_BUNDLE_CAPABILITY__ !== brandkitContext ||
        brandkitNativeRoot.__IMPECCABLE_TOKEN__ !== brandkitExpectedRouteToken || brandkitContext.route_token !== brandkitExpectedRouteToken ||
        typeof brandkitContext.grant_nonce !== 'string' || brandkitContext.grant_nonce.length !== 64 ||
        !Number.isFinite(brandkitContext.issuedAt) || !Number.isFinite(brandkitContext.expiresAt) ||
        Date.now() < brandkitContext.issuedAt || Date.now() >= brandkitContext.expiresAt ||
        !Array.isArray(caps) || caps.length !== 3 || caps[0] !== 'browser' || caps[1] !== 'live' || caps[2] !== 'network' ||
        JSON.stringify(brandkitContext) !== brandkitExpectedContext || typeof brandkitNativeXhr !== 'function') throw new Error('BrandKit browser capability unavailable');
    const request = new brandkitNativeXhr();
    request.open('POST', brandkitContext.validationUrl, false);
    request.setRequestHeader('Content-Type', 'application/json');
    try { request.send(brandkitExpectedContext); } catch (_) { throw new Error('BrandKit browser capability unavailable'); }
    if (request.status !== 204) throw new Error('BrandKit browser capability unavailable');
  }
  const brandkitObjectProxies = new WeakMap();
  const brandkitCallbackProxies = new WeakMap();
  function brandkitGuardCallback(fn) {
    if (brandkitCallbackProxies.has(fn)) return brandkitCallbackProxies.get(fn);
    const guarded = function (...args) { brandkitAssertBrowserCapability(); return fn.apply(this, args.map(brandkitGuardObject)); };
    brandkitCallbackProxies.set(fn, guarded); return guarded;
  }
  function brandkitGuardObject(value) {
    if ((typeof value !== 'object' || value === null) && typeof value !== 'function') return value;
    if (brandkitObjectProxies.has(value)) return brandkitObjectProxies.get(value);
    const proxy = new Proxy(value, {
      get(target, property) {
        const member = Reflect.get(target, property, target);
        if (typeof member === 'function') return function (...args) {
          brandkitAssertBrowserCapability();
          const guardedArgs = args.map(arg => typeof arg === 'function' ? brandkitGuardCallback(arg) : arg);
          return brandkitGuardObject(Reflect.apply(member, target, guardedArgs));
        };
        return brandkitGuardObject(member);
      },
      set(target, property, next) { brandkitAssertBrowserCapability(); return Reflect.set(target, property, next, target); },
      deleteProperty(target, property) { brandkitAssertBrowserCapability(); return Reflect.deleteProperty(target, property); },
      defineProperty(target, property, descriptor) { brandkitAssertBrowserCapability(); return Reflect.defineProperty(target, property, descriptor); }
    });
    brandkitObjectProxies.set(value, proxy); return proxy;
  }
  try { brandkitAssertBrowserCapability(); } catch (_) { return; }
'''
def inject_browser_guard(source:str)->str:
 guarded,count=re.subn(r"(\(function\s*\([^)]*\)\s*\{\s*(?:'use strict';)?)",lambda m:m.group(1)+browser_iife_guard(),source,count=1)
 if count!=1:raise AdapterError("browser IIFE capability guard injection failed")
 if re.search(r"\(function\s*\(root\)",guarded):guarded=guarded.replace("  if (!root) return;","  root = brandkitGuardObject(root);\n  if (!root) return;",1)
 else:guarded=guarded.replace("  if (typeof window === 'undefined') return;","  const window = brandkitGuardObject(brandkitNativeRoot);\n  const document = window && window.document;\n  const history = window && window.history;\n  const location = window && window.location;\n  if (!window) return;",1)
 if "function createLiveBrowserDomHelpers" in guarded:
  guarded=guarded.replace("    return {\n      own,", "    const helpers = {\n      own,",1)
  guarded=guarded.replace("      defangOutsideHandlers,\n    };", "      defangOutsideHandlers,\n    };\n    return new Proxy(helpers, { get(target, property) { const member = target[property]; return typeof member === 'function' ? function (...args) { brandkitAssertBrowserCapability(); return member(...args); } : member; } });",1)
 return guarded
def patch_live_assembler(source:str)->str:
 source=source.replace("import path from 'node:path';", "import path from 'node:path';\nimport { spawnSync as brandkitSpawnSync } from 'node:child_process';\nimport { fileURLToPath as brandkitFileURLToPath } from 'node:url';\n\nfunction assertBrandkitBundleGrant() {\n  const guard = brandkitFileURLToPath(new URL('../capability_guard.py', import.meta.url));\n  const result = brandkitSpawnSync('python3', [guard, 'live', 'browser', 'network'], { cwd: process.cwd(), stdio: 'ignore' });\n  if (result.status !== 0) throw new Error('valid server capability grant required');\n}\n")
 source=source.replace("  mountContract = LIVE_CHROME_MOUNT_CONTRACT,\n}) {", "  mountContract = LIVE_CHROME_MOUNT_CONTRACT,\n  bundleCapability,\n}) {\n  assertBrandkitBundleGrant();\n  if (!bundleCapability || !bundleCapability.sessionId || !bundleCapability.route_token || !bundleCapability.grant_nonce || !Number.isFinite(bundleCapability.expiresAt)) throw new Error('valid bundle capability required');")
 source=source.replace("  const prelude =\n    `window.__IMPECCABLE_TOKEN__", "  const prelude =\n    `window.__BRANDKIT_IMPECCABLE_BUNDLE_CAPABILITY__ = ${JSON.stringify(bundleCapability)};\\n` +\n    `window.__IMPECCABLE_TOKEN__")
 if "assertBrandkitBundleGrant();" not in source or "valid bundle capability required" not in source or "__BRANDKIT_IMPECCABLE_BUNDLE_CAPABILITY__" not in source:raise AdapterError("live browser assembler capability injection failed")
 return source
def patch_live_server_route(source:str)->str:
 helper='''\nimport { spawnSync as brandkitSpawnSync } from 'node:child_process';
const BRANDKIT_BROWSER_CAPS = Object.freeze(['browser', 'live', 'network']);
function brandkitReadBundleGrant() {
  const result = brandkitSpawnSync('python3', [new URL('./capability_guard.py', import.meta.url).pathname, '--binding-json', 'live', 'browser', 'network'], { cwd: process.cwd(), encoding: 'utf8' });
  if (result.status !== 0) throw new Error('valid server capability grant required');
  const binding = JSON.parse(result.stdout);
  if (JSON.stringify([...binding.capabilities].sort()) !== JSON.stringify(BRANDKIT_BROWSER_CAPS)) throw new Error('browser grant capabilities must be exact');
  return binding;
}
function brandkitRequestOrigin(req) {
  if (req.headers.origin) return req.headers.origin;
  try { return req.headers.referer ? new URL(req.headers.referer).origin : null; } catch { return null; }
}
function brandkitPublicContext(binding, sessionId, port, now = Date.now()) {
  return Object.freeze({ schemaVersion: 'brandkit-browser-capability.v1', sessionId,
    validationUrl: ['http:', `//127.0.0.1:${port}/__brandkit/capability/validate?token=${encodeURIComponent(state.token)}`].join(''),
    route_token: state.token, grant_nonce: binding.nonce,
    projectRoot: binding.project_root, projectCommit: binding.project_commit, brandContentSha256: binding.brand_content_sha256,
    grantId: binding.grant_id, keyId: binding.key_id, adaptedReleaseSha256: binding.adapted_release_sha256,
    capabilities: Object.freeze(BRANDKIT_BROWSER_CAPS.slice()), issuedAt: now, expiresAt: Math.min(binding.expires_at * 1000, now + 30000) });
}
function brandkitBindingMatchesContext(binding, context) {
  return context.route_token === state.token && context.grant_nonce === binding.nonce &&
    context.projectRoot === binding.project_root && context.projectCommit === binding.project_commit &&
    context.brandContentSha256 === binding.brand_content_sha256 && context.grantId === binding.grant_id && context.keyId === binding.key_id &&
    context.adaptedReleaseSha256 === binding.adapted_release_sha256 &&
    JSON.stringify([...binding.capabilities].sort()) === JSON.stringify(BRANDKIT_BROWSER_CAPS);
}
'''
 if source.startswith("#!"):
  first,rest=source.split("\n",1);source=first+"\n"+helper+rest
 else:source=helper+source
 source=source.replace("  nextManualEditSeq: 1,", "  nextManualEditSeq: 1,\n  brandkitBrowserCapabilities: new Map(),",1)
 validate_route='''    if (p === '/__brandkit/capability/validate') {
      if (req.method !== 'POST' || url.searchParams.get('token') !== state.token) { res.writeHead(401); res.end(); return; }
      let body = ''; req.setEncoding('utf8');
      req.on('data', chunk => { body += chunk; if (body.length > 8192) req.destroy(); });
      req.on('end', () => {
        try {
          const context = JSON.parse(body); const record = state.brandkitBrowserCapabilities.get(context.sessionId);
          const origin = brandkitRequestOrigin(req); const now = Date.now(); const binding = brandkitReadBundleGrant();
          if (!record || !origin || record.origin !== origin || record.expiresAt <= now || context.expiresAt <= now ||
              JSON.stringify(context) !== record.serialized || !brandkitBindingMatchesContext(binding, context)) throw new Error('invalid');
          res.writeHead(204, { 'Cache-Control': 'no-store' }); res.end();
        } catch { res.writeHead(403); res.end(); }
      });
      return;
    }

'''
 source=source.replace("    // --- Scripts ---\n",validate_route+"    // --- Scripts ---\n",1)
 source,count=re.subn(r"(if \(p === '/live\.js'\) \{)",r"\1\n      let brandkitBinding; try { brandkitBinding = brandkitReadBundleGrant(); } catch { res.writeHead(403, { 'Content-Type': 'text/plain' }); res.end('Capability grant unavailable'); return; }",source,count=1)
 replacement=r'''\1
        bundleCapability: (() => { const sessionId = randomUUID(); const context = brandkitPublicContext(brandkitBinding, sessionId, state.port); const origin = brandkitRequestOrigin(req); if (!origin) throw new Error('browser origin unavailable'); state.brandkitBrowserCapabilities.set(sessionId, { origin, expiresAt: context.expiresAt, serialized: JSON.stringify(context) }); return context; })(),'''
 source,count2=re.subn(r"(appRoot: process\.cwd\(\),\s*parts,)",replacement,source,count=1)
 if count!=1 or count2!=1:raise AdapterError("live server route capability injection failed")
 return source
def materialize(checkout:Path,home:Path)->dict[str,Any]:
 verify(checkout);store=home.resolve()/".agents/skill-store/impeccable";release=store/VERSION;expected_hash=CONTRACT["adapted_release"]["tree_sha256"]
 if release.exists():
  if release.is_symlink() or not release.is_dir():raise AdapterError("release path collision")
  marker=json.loads((release/"ADAPTER-RELEASE.json").read_text())
  if marker!=CONTRACT["adapted_release"]["marker"] or tree_hash(release,True)!=expected_hash:raise AdapterError("immutable release integrity failure")
  return {"status":"present","release":str(release),"version":VERSION}
 store.mkdir(parents=True,exist_ok=True);stage=Path(tempfile.mkdtemp(prefix=f".{VERSION}.stage-",dir=store));skill=stage/"skill"
 try:
  shutil.copytree(checkout.resolve()/CONTRACT["upstream"]["canonical_skill_subtree"],skill)
  shutil.copy2(checkout/"LICENSE",skill/"LICENSE");shutil.copy2(checkout/CONTRACT["upstream"]["notice_path"],skill/"NOTICE.md")
  (skill/"scripts/capability_guard.py").write_text(GUARD_PY)
  guards=CONTRACT["capability_matrix"]["direct_entrypoints"]
  for relative,caps in guards.items():
   path=skill/"scripts"/relative
   if not path.is_file():raise AdapterError(f"guard target missing: {relative}")
   original=f".brandkit-upstream-{path.name}";path.rename(path.with_name(original));path.write_text(wrapper(original,caps))
  server=skill/"scripts/.brandkit-upstream-live-server.mjs"
  if not server.is_file():raise AdapterError("guarded live server source missing")
  server.write_text(patch_live_server_route(server.read_text()))
  for relative,caps in CONTRACT["capability_matrix"]["import_safe_direct_checks"].items():
   path=skill/"scripts"/relative
   if not path.is_file():raise AdapterError(f"guard target missing: {relative}")
   original_source=path.read_text();guard_source=import_safe_guard(caps)
   if original_source.startswith("#!"):
    first,rest=original_source.split("\n",1);source=first+"\n"+guard_source+rest
   else:source=guard_source+original_source
   for name in re.findall(r"^export\s+(?:async\s+)?function\s+(\w+)",source,flags=re.M):
    source=re.sub(rf"(export\s+(?:async\s+)?function\s+{name}\s*\([^)]*\)\s*\{{)",r"\1 brandkitCapabilityGuard();",source,count=1)
   path.write_text(source)
  for relative,route in CONTRACT["capability_matrix"]["indirect_owned"].items():
   path=skill/"scripts"/relative
   if not path.is_file():raise AdapterError(f"indirect guard target missing: {relative}")
   source=path.read_text()
   if relative=="lib/open-system-browser.mjs":
    source=indirect_guard(route["capabilities"])+re.sub(r"(export function openSystemBrowser\([^)]*\}\s*=\s*\{\}\)\s*\{)",r"\1 brandkitCapabilityGuard();",source,count=1,flags=re.S)
    if "brandkitCapabilityGuard();" not in source.split("export function openSystemBrowser",1)[1]:raise AdapterError("openSystemBrowser export guard injection failed")
   elif relative in {"live-browser.js","live-browser-session.js","live-browser-dom.js"}:
    source=inject_browser_guard(source)
   path.write_text(source)
  assembler=skill/"scripts/live/browser-script-parts.mjs";assembler.write_text(patch_live_assembler(assembler.read_text()))
  text=(skill/"SKILL.md").read_text();end=text.find("---",4)
  gate='''\n\n## BrandKit capability gate (mandatory)\n\nThis adapted release is default-off for hooks, updates, browser control, live mode, image generation, and network. Never invoke those routes or scripts unless `scripts/capability_guard.py <capability...>` exits 0 in the project root. A provider is never required. Do not create or broaden the grant; only a human-supplied, schema-valid `.brandkit/impeccable-capabilities.json` may enable it. Safe design routes remain available.\n'''
  (skill/"SKILL.md").write_text(text[:end+3]+gate+text[end+3:])
  meta=CONTRACT["adapted_release"]["marker"]
  (skill/"ADAPTER-RELEASE.json").write_bytes(json_bytes(meta))
  actual_hash=tree_hash(skill,True)
  if actual_hash!=expected_hash:raise AdapterError(f"deterministic adapted release differs from committed pin: {actual_hash}")
  os.replace(skill,release)
  for p in sorted(release.rglob("*"),reverse=True):
   if p.is_file():p.chmod(0o555 if p.suffix in {".mjs",".js",".py"} else 0o444)
   elif p.is_dir():p.chmod(0o555)
  release.chmod(0o555)
 finally:shutil.rmtree(stage,ignore_errors=True)
 return {"status":"materialized","release":str(release),"version":VERSION,"license":str(release/"LICENSE"),"notice":str(release/"NOTICE.md")}

def atomic_json(path:Path,value:Any)->None:
 path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name(f".{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}");tmp.write_bytes(json_bytes(value));os.replace(tmp,path)
def atomic_bytes(path:Path,value:bytes,mode:int=0o600)->None:
 path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name(f".{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}");fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
 try:os.write(fd,value);os.fsync(fd)
 finally:os.close(fd)
 os.replace(tmp,path);path.chmod(mode)
def atomic_link(path:Path,target:str)->None:
 path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name(f".{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}");os.symlink(target,tmp);os.replace(tmp,path)
def frontmatter_name(path:Path)->str|None:
 try:lines=(path/"SKILL.md").read_text().splitlines()
 except OSError:return None
 if not lines or lines[0]!="---":return None
 for line in lines[1:]:
  if line=="---":break
  if line.startswith("name:"):return line.split(":",1)[1].strip()
 return None
def provider_paths(home:Path)->list[Path]:return [home/".agents/skills/impeccable",home/".claude/skills/impeccable",home/".hermes/skills/impeccable"]
def release_link(store:Path,path:Path)->str|None:
 if not path.is_symlink():return None
 raw=os.readlink(path);posix=PurePosixPath(raw)
 if posix.is_absolute() or len(posix.parts)!=1 or raw in {".","..","current"}:raise AdapterError("current link is not confined to the release store")
 target=store/raw
 if not target.is_dir() or target.is_symlink() or target.parent.resolve()!=store.resolve():raise AdapterError("current link target is not a confined release")
 return raw
def verify_release(release:Path)->None:
 if release.is_symlink() or not release.is_dir():raise AdapterError("release not materialized")
 marker=json.loads((release/"ADAPTER-RELEASE.json").read_text())
 if marker!=CONTRACT["adapted_release"]["marker"] or tree_hash(release,True)!=CONTRACT["adapted_release"]["tree_sha256"]:raise AdapterError("immutable release integrity failure")
def verify_named_release(release:Path,name:str)->None:
 if name==VERSION:return verify_release(release)
 expected=LEGACY_RELEASE_HASHES.get(name)
 if not expected or release.is_symlink() or not release.is_dir():raise AdapterError("release is not an approved migration source")
 marker=json.loads((release/"ADAPTER-RELEASE.json").read_text())
 if marker.get("version")!=name or marker.get("upstream_commit")!=CONTRACT["upstream"]["commit"] or tree_hash(release,True)!=expected:raise AdapterError("legacy release integrity failure")
def activate(home:Path)->dict[str,Any]:
 home=home.resolve();store=home/".agents/skill-store/impeccable";release=store/VERSION;verify_release(release);secret=auth_secret(home,True)
 current=store/"current";paths=provider_paths(home)
 if current.exists() and not current.is_symlink():raise AdapterError("collision at canonical current pointer")
 previous=release_link(store,current) if current.is_symlink() else None
 if previous==VERSION and all(p.is_symlink() and p.resolve()==release for p in paths):return {"status":"already-active","release":str(release),"canonical":str(current),"claude":str(paths[1]),"codex":str(paths[0]),"hermes":str(paths[2])}
 state_file=home/".agents/skill-state/impeccable/current.json"
 if state_file.is_symlink():raise AdapterError("preexisting activation state must be a regular authenticated file")
 if state_file.exists():
  prior=unseal_activation(json.loads(state_file.read_text()),secret);expected=[p.relative_to(home).as_posix() for p in paths]
  history=PurePosixPath(prior.get("history",""))
  if prior.get("schema_version")!="brandkit-impeccable-activation.v2" or prior.get("home")!=str(home) or prior.get("current")!=previous or prior.get("providers")!=expected or not set(prior.get("created",[]))<=set(expected):raise AdapterError("preexisting activation state binding invalid")
  if history.is_absolute() or ".." in history.parts or history.parts[:4]!=(".agents","skill-state","impeccable","history"):raise AdapterError("preexisting activation history path invalid")
  verify_named_release(store/previous,previous)
  for raw in prior.get("created",[]):
   p=home/raw
   if not p.is_symlink() or p.resolve()!=store/previous:raise AdapterError("preexisting provider path differs from signed activation state")
 for path in paths:
  if path.is_symlink() and (path.resolve()==release or os.readlink(path)==str(current)):continue
  if path.exists() or path.is_symlink():raise AdapterError(f"collision at {path} (frontmatter.name={frontmatter_name(path.resolve())!r})")
 state={"schema_version":"brandkit-impeccable-activation.v2","activation_id":secrets.token_hex(16),"issued_at":int(time.time()),"home":str(home),"previous":previous,"current":VERSION,"providers":[p.relative_to(home).as_posix() for p in paths],"created":[]}
 history_rel=f".agents/skill-state/impeccable/history/{state['activation_id']}.json";history_path=home/history_rel
 try:
  for path in paths:
   if not path.is_symlink():atomic_link(path,str(current));state["created"].append(path.relative_to(home).as_posix())
  signed=seal({**state,"history":history_rel},secret);atomic_json(history_path,signed);atomic_link(current,VERSION);atomic_json(state_file,signed)
 except Exception:
  if state_file.is_file() and not state_file.is_symlink():state_file.unlink()
  if previous is None:
   if current.is_symlink():current.unlink()
  else:atomic_link(current,previous)
  if history_path.is_file() and not history_path.is_symlink():history_path.unlink()
  for raw in reversed(state["created"]):
   rel=PurePosixPath(raw)
   if rel.is_absolute() or ".." in rel.parts:continue
   p=home/rel
   if p.is_symlink() and p in paths:p.unlink()
  raise
 return {"status":"active","release":str(release),"canonical":str(current),"claude":str(paths[1]),"codex":str(paths[0]),"hermes":str(paths[2])}
def status(home:Path)->dict[str,Any]:
 home=home.resolve();recover_secret_rotation(home);current=home/".agents/skill-store/impeccable/current";store=current.parent
 try:active=(store/release_link(store,current)).resolve() if current.is_symlink() else None
 except AdapterError:active=None
 return {"status":"active" if active else "inactive","version":active.name if active else None,"canonical":str(current),"providers":{name:{"path":str(path),"realpath":str(path.resolve()) if path.exists() else None,"discovered":bool(active and path.exists() and path.resolve()==active)} for name,path in zip(("codex","claude","hermes"),provider_paths(home))}}
def rollback(home:Path)->dict[str,Any]:
 home=home.resolve();recover_secret_rotation(home);sf=home/".agents/skill-state/impeccable/current.json"
 if sf.is_symlink() or not sf.is_file():raise AdapterError("no activation state")
 state=unseal_activation(json.loads(sf.read_text()),auth_secret(home));current=home/".agents/skill-store/impeccable/current";store=current.parent;previous=state.get("previous");active_name=state.get("current")
 expected_providers=[p.relative_to(home).as_posix() for p in provider_paths(home)]
 if state.get("schema_version")!="brandkit-impeccable-activation.v2" or state.get("home")!=str(home) or not isinstance(active_name,str) or state.get("providers")!=expected_providers or not set(state.get("created",[]))<=set(expected_providers):raise AdapterError("activation state binding invalid")
 if release_link(store,current)!=active_name:raise AdapterError("active current pointer differs from signed state")
 verify_named_release(store/active_name,active_name)
 history=PurePosixPath(state.get("history",""))
 if history.is_absolute() or ".." in history.parts or history.parts[:4]!=(".agents","skill-state","impeccable","history"):raise AdapterError("activation history path invalid")
 for raw in state.get("created",[]):
  p=home/raw
  if not p.is_symlink() or p.resolve()!=store/active_name:raise AdapterError("provider path differs from signed activation state")
 if previous:
  if PurePosixPath(previous).is_absolute() or len(PurePosixPath(previous).parts)!=1 or not (store/previous).is_dir() or (store/previous).is_symlink():raise AdapterError("previous release missing or unconfined")
  atomic_link(current,previous)
 else:
  if current.is_symlink():current.unlink()
  for raw in state.get("created",[]):
   p=home/raw
   p.unlink()
 sf.unlink();return {"status":"rolled-back","active":previous}

def resolve_package(project:Path)->tuple[Path,dict[str,Any],str]:
 root=brand_package.git_root(project);activation,lock=brand_package.read_closed_activation(root);manifest,_pkg,content=brand_package.validate_bound_state(root,activation,lock);return root,manifest,content
def project_commit(root:Path)->str:
 value=git(root,"rev-parse","HEAD")
 if not re.fullmatch(r"[0-9a-f]{40}",value):raise AdapterError("project must have a committed HEAD")
 return value
def safe_target(root:Path,relative:str,create:bool=False)->Path:
 posix=PurePosixPath(relative)
 if posix.is_absolute() or ".." in posix.parts or posix.as_posix()!=relative:raise AdapterError("unsafe target")
 cursor=root
 for part in posix.parts[:-1]:
  cursor/=part
  try:mode=cursor.lstat().st_mode
  except FileNotFoundError:
   if not create:continue
   os.mkdir(cursor,0o700);mode=cursor.lstat().st_mode
  if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):raise AdapterError(f"unsafe parent component: {cursor}")
  real=cursor.resolve(strict=True)
  if root!=real and root not in real.parents:raise AdapterError(f"target escapes Git root: {cursor}")
 target=root/posix
 if target.exists() or target.is_symlink():
  mode=target.lstat().st_mode
  if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):raise AdapterError(f"target must be regular: {target}")
 return target
def read_product(root:Path)->dict[str,Any]:
 path=safe_target(root,PRODUCT_INPUT)
 if not path.is_file():raise AdapterError(f"explicit product input missing: {PRODUCT_INPUT}")
 value=json.loads(path.read_text());validate_json(value,"impeccable-product-input.v1.schema.json","product input");return value
def approve(project:Path,home:Path,capabilities:list[str],evidence:str,ttl:int)->dict[str,Any]:
 recover_secret_rotation(home);root,manifest,content=resolve_package(project)
 if not capabilities or len(capabilities)!=len(set(capabilities)) or set(capabilities)-CAPABILITIES:raise AdapterError("grant capabilities must be a non-empty unique supported set")
 if not evidence.strip():raise AdapterError("approval receipt evidence is required")
 if ttl<1 or ttl>86400:raise AdapterError("grant ttl must be 1..86400 seconds")
 now=int(time.time());rid=secrets.token_hex(16);secret=auth_secret(home,True);value={"schema_version":"brandkit-impeccable-approval.v1","receipt_id":rid,"nonce":secrets.token_hex(32),"issued_at":now,"expires_at":now+ttl,"evidence":evidence,"project_root":str(root),"project_commit":project_commit(root),"brand_id":manifest["brand_id"],"brand_content_sha256":content,"upstream_commit":CONTRACT["upstream"]["commit"],"adapted_release_sha256":CONTRACT["adapted_release"]["tree_sha256"],"capabilities":sorted(capabilities)}
 path=state_root(home)/"approvals"/f"{rid}.json";atomic_json(path,seal(value,secret));path.chmod(0o600)
 return {"status":"approved","receipt_id":rid,"expires_at":value["expires_at"],"path":str(path)}
def grant(project:Path,home:Path,receipt_id:str)->dict[str,Any]:
 recover_secret_rotation(home);root,manifest,content=resolve_package(project);secret=auth_secret(home);path=state_root(home)/"approvals"/f"{receipt_id}.json"
 if not re.fullmatch(r"[0-9a-f]{32}",receipt_id) or path.is_symlink() or not path.is_file():raise AdapterError("sealed approval receipt missing")
 receipt=unseal(json.loads(path.read_text()),secret,"approval receipt");now=int(time.time())
 expected={"project_root":str(root),"project_commit":project_commit(root),"brand_id":manifest["brand_id"],"brand_content_sha256":content,"upstream_commit":CONTRACT["upstream"]["commit"],"adapted_release_sha256":CONTRACT["adapted_release"]["tree_sha256"]}
 if receipt.get("schema_version")!="brandkit-impeccable-approval.v1" or any(receipt.get(k)!=v for k,v in expected.items()) or not receipt.get("issued_at",0)<=now<receipt.get("expires_at",0):raise AdapterError("approval receipt binding/expiry invalid")
 capabilities=receipt.get("capabilities",[])
 if not capabilities or len(capabilities)!=len(set(capabilities)) or set(capabilities)-CAPABILITIES:raise AdapterError("approval capability map is not closed")
 consumed=sealed_ledger(home,"consumed.json",secret)
 if any(x.get("receipt_id")==receipt_id and x.get("nonce")==receipt.get("nonce") for x in consumed["entries"]):raise AdapterError("approval receipt already consumed")
 consumed["entries"].append({"receipt_id":receipt_id,"nonce":receipt["nonce"],"consumed_at":now});write_ledger(home,"consumed.json",consumed,secret)
 value={"schema_version":"brandkit-impeccable-capabilities.v1","grant_id":secrets.token_hex(16),"nonce":secrets.token_hex(32),"issued_at":now,"expires_at":receipt["expires_at"],"approval_receipt_id":receipt_id,"project_root":str(root),"project_commit":expected["project_commit"],"brand_content_sha256":content,"upstream_commit":CONTRACT["upstream"]["commit"],"adapted_release_sha256":CONTRACT["adapted_release"]["tree_sha256"],"capabilities":sorted(capabilities)}
 signed=seal(value,auth_secret(home,True));validate_json(signed,"impeccable-capabilities.v1.schema.json","capability grant");path=safe_target(root,".brandkit/impeccable-capabilities.json",True);atomic_json(path,signed);path.chmod(0o600)
 issued=sealed_ledger(home,"grants.json",secret);issued["entries"].append({"grant_id":value["grant_id"],"nonce":value["nonce"],"project_root":str(root)});write_ledger(home,"grants.json",issued,secret)
 return {"status":"granted","grant_id":value["grant_id"],"capabilities":value["capabilities"],"expires_at":value["expires_at"],"path":str(path)}
def revoke(project:Path,home:Path)->dict[str,Any]:
 recover_secret_rotation(home);root=brand_package.git_root(project);path=safe_target(root,".brandkit/impeccable-capabilities.json")
 if not path.exists():return {"status":"absent","path":str(path)}
 secret=auth_secret(home);encoded=json.loads(path.read_text())
 if encoded.get("key_id")!=key_id(secret):path.unlink();return {"status":"revoked-stale","path":str(path)}
 grant_value=unseal(encoded,secret,"capability grant");ledger=sealed_ledger(home,"revocations.json",secret);ledger["entries"].append({"grant_id":grant_value["grant_id"],"nonce":grant_value["nonce"],"revoked_at":int(time.time())});write_ledger(home,"revocations.json",ledger,secret);path.unlink();return {"status":"revoked","path":str(path)}
ROTATION_JOURNAL="rotation.json";ROTATION_FIXED={"auth.key","revocations.json","consumed.json","grants.json","active-transactions.json"}
def rotation_failpoint()->None:
 count=int(os.environ.get("BRANDKIT_IMPECCABLE_ROTATE_WRITE_COUNT","0"))+1;os.environ["BRANDKIT_IMPECCABLE_ROTATE_WRITE_COUNT"]=str(count)
 crash=os.environ.get("BRANDKIT_IMPECCABLE_ROTATE_CRASH_AFTER");fail=os.environ.get("BRANDKIT_IMPECCABLE_ROTATE_FAIL_AFTER")
 if crash and count==int(crash):os._exit(86)
 if fail and count==int(fail):raise OSError(f"injected rotation write failure {count}")
def rotation_write(path:Path,value:bytes)->None:atomic_bytes(path,value);rotation_failpoint()
def rotation_rel_path(root:Path,raw:str,prefix:str)->Path:
 posix=PurePosixPath(raw)
 if posix.is_absolute() or ".." in posix.parts or not raw.startswith(prefix) or len(posix.parts)!=4:raise AdapterError("rotation path is unconfined")
 path=root/posix
 if path.is_symlink() or (path.exists() and not path.is_file()):raise AdapterError("rotation file is unsafe")
 if path.exists() and root not in path.resolve(strict=True).parents:raise AdapterError("rotation file escapes state root")
 return path
def validate_rotation_journal(home:Path,sealed:dict[str,Any])->tuple[dict[str,Any],bytes]:
 root=state_root(home);required={"schema_version","rotation_id","old_key_id","new_key_id","preimages","staging","entries","key_id","hmac_sha256"}
 if not isinstance(sealed,dict) or set(sealed)!=required:raise AdapterError("rotation journal schema is not closed")
 rid=sealed.get("rotation_id")
 if sealed.get("schema_version")!="brandkit-impeccable-secret-rotation.v1" or not isinstance(rid,str) or not re.fullmatch(r"[0-9a-f]{32}",rid):raise AdapterError("rotation journal identity invalid")
 pre_prefix=f"rotation/preimages/{rid}/";stage_prefix=f"rotation/staging/{rid}/"
 if sealed.get("preimages")!=pre_prefix[:-1] or sealed.get("staging")!=stage_prefix[:-1]:raise AdapterError("rotation directory binding invalid")
 entries=sealed.get("entries")
 if not isinstance(entries,list) or not 5<=len(entries)<=7:raise AdapterError("rotation journal entries invalid")
 paths=[x.get("path") for x in entries if isinstance(x,dict)]
 allowed=set(ROTATION_FIXED);sf=root/"current.json"
 history=None
 if "current.json" in paths:
  allowed.add("current.json")
  current=next(x for x in entries if x.get("path")=="current.json")
  # History is the sole variable target and remains under the closed history namespace.
  for candidate in paths:
   if isinstance(candidate,str) and candidate.startswith("history/"):history=candidate;allowed.add(candidate)
 if len(paths)!=len(entries) or len(paths)!=len(set(paths)) or set(paths)!=allowed:raise AdapterError("rotation target allowlist violation")
 for item in entries:
  if set(item)!={"path","existed","preimage_sha256","postimage_sha256","preimage","staged"} or type(item["existed"]) is not bool:raise AdapterError("rotation entry schema invalid")
  if item["path"].startswith("history/") and (PurePosixPath(item["path"]).parts[:1]!=("history",) or len(PurePosixPath(item["path"]).parts)!=2):raise AdapterError("rotation history target invalid")
  if item["path"]!="auth.key" and item["path"] not in allowed:raise AdapterError("rotation target invalid")
  if item["preimage"]!=pre_prefix+sha(item["path"].encode())+".bin" or item["staged"]!=stage_prefix+sha(item["path"].encode())+".bin":raise AdapterError("rotation staging map invalid")
  for key in ("preimage_sha256","postimage_sha256"):
   if item[key] is not None and not isinstance(item[key],str) or isinstance(item[key],str) and not re.fullmatch(r"[0-9a-f]{64}",item[key]):raise AdapterError("rotation hash invalid")
 pre_path=rotation_rel_path(root,next(x["preimage"] for x in entries if x["path"]=="auth.key"),pre_prefix)
 pre_secret=pre_path.read_bytes() if pre_path.is_file() else secret_path(home).read_bytes()
 if len(pre_secret)!=32 or key_id(pre_secret)!=sealed["old_key_id"]:raise AdapterError("rotation old key preimage invalid")
 value=unseal(sealed,pre_secret,"rotation journal")
 if value["old_key_id"]!=key_id(pre_secret) or value["new_key_id"]==value["old_key_id"]:raise AdapterError("rotation key binding invalid")
 return value,pre_secret
def recover_secret_rotation(home:Path,force_rollback:bool=False)->bool:
 home=home.resolve();root=state_root(home);journal=root/ROTATION_JOURNAL
 if not journal.exists() and not journal.is_symlink():return False
 if journal.is_symlink() or not journal.is_file():raise AdapterError("rotation journal is not a trusted regular file")
 sealed=json.loads(journal.read_text());v,old=validate_rotation_journal(home,sealed);entries=v["entries"]
 for item in entries:
  target=root/item["path"]
  current=sha(target.read_bytes()) if target.is_file() and not target.is_symlink() else None
  if current not in {item["preimage_sha256"],item["postimage_sha256"]}:raise AdapterError("rotation target hash is outside journal")
  for field,expected in (("preimage",item["preimage_sha256"]),("staged",item["postimage_sha256"])):
   p=rotation_rel_path(root,item[field],f"rotation/{'preimages' if field=='preimage' else 'staging'}/{v['rotation_id']}/")
   if p.exists() and sha(p.read_bytes())!=expected:raise AdapterError(f"rotation {field} hash mismatch")
 current_secret=secret_path(home).read_bytes();current_id=key_id(current_secret)
 rollback=force_rollback or current_id==v["old_key_id"]
 if current_id not in {v["old_key_id"],v["new_key_id"]}:raise AdapterError("rotation secret is outside journal")
 selected="preimage" if rollback else "staged"
 for item in entries:
  target=root/item["path"]
  desired=item["preimage_sha256"] if selected=="preimage" else item["postimage_sha256"]
  current=sha(target.read_bytes()) if target.is_file() and not target.is_symlink() else None
  if current==desired:continue
  if selected=="preimage" and not item["existed"]:
   if target.exists():target.unlink()
  else:
   source=rotation_rel_path(root,item[selected],f"rotation/{'preimages' if selected=='preimage' else 'staging'}/{v['rotation_id']}/")
   if not source.is_file():raise AdapterError("rotation recovery image missing")
   atomic_bytes(target,source.read_bytes())
 journal.unlink();shutil.rmtree(root/"rotation/preimages"/v["rotation_id"],ignore_errors=True);shutil.rmtree(root/"rotation/staging"/v["rotation_id"],ignore_errors=True)
 return True
def rotate_secret(home:Path)->dict[str,Any]:
 home=home.resolve();recover_secret_rotation(home);old=auth_secret(home);grants=sealed_ledger(home,"grants.json",old);active=sealed_ledger(home,"active-transactions.json",old)
 if active["entries"]:raise AdapterError("secret rotation blocked by open Document journal")
 new=secrets.token_bytes(32);root=state_root(home);sf=root/"current.json";now=int(time.time());values={"auth.key":new}
 state=None
 if sf.exists():
  state=unseal_activation(json.loads(sf.read_text()),old);raw={k:v for k,v in state.items() if k!="key_id"};values["current.json"]=json_bytes(seal(raw,new))
  history=PurePosixPath(state.get("history","")).relative_to(PurePosixPath(".agents/skill-state/impeccable"))
  if history.is_absolute() or ".." in history.parts or history.parts[:1]!=("history",):raise AdapterError("activation history path invalid during rotation")
  values[history.as_posix()]=json_bytes(seal(raw,new))
 revoked={"schema_version":"brandkit-impeccable-revocations.v1","entries":[{"grant_id":x["grant_id"],"nonce":x["nonce"],"revoked_at":now} for x in grants["entries"]]}
 for name,value in (("revocations.json",revoked),("consumed.json",{"schema_version":"brandkit-impeccable-consumed.v1","entries":[]}),("grants.json",{"schema_version":"brandkit-impeccable-grants.v1","entries":[]}),("active-transactions.json",{"schema_version":"brandkit-impeccable-active-transactions.v1","entries":[]})):values[name]=json_bytes(seal(value,new))
 rid=secrets.token_hex(16);prebase=f"rotation/preimages/{rid}/";stagebase=f"rotation/staging/{rid}/";entries=[]
 for relative,post in sorted(values.items()):
  target=root/relative;pre=target.read_bytes() if target.is_file() and not target.is_symlink() else None;digest=sha(relative.encode())+".bin"
  entries.append({"path":relative,"existed":pre is not None,"preimage_sha256":sha(pre) if pre is not None else None,"postimage_sha256":sha(post),"preimage":prebase+digest,"staged":stagebase+digest})
 journal=seal({"schema_version":"brandkit-impeccable-secret-rotation.v1","rotation_id":rid,"old_key_id":key_id(old),"new_key_id":key_id(new),"preimages":prebase[:-1],"staging":stagebase[:-1],"entries":entries},old)
 try:
  rotation_write(root/ROTATION_JOURNAL,json_bytes(journal))
  for item in entries:
   target=root/item["path"]
   if item["existed"]:rotation_write(root/item["preimage"],target.read_bytes())
   rotation_write(root/item["staged"],values[item["path"]])
  # The only commit point: before this replace every authoritative file verifies with old;
  # after it, recovery rolls forward every new-key state before any command proceeds.
  rotation_write(secret_path(home),new)
  for item in entries:
   if item["path"]!="auth.key":rotation_write(root/item["path"],values[item["path"]])
  (root/ROTATION_JOURNAL).unlink();shutil.rmtree(root/"rotation/preimages"/rid,ignore_errors=True);shutil.rmtree(root/"rotation/staging"/rid,ignore_errors=True)
 except Exception:
  recover_secret_rotation(home,True);raise
 return {"status":"rotated","old_key_id":key_id(old),"key_id":key_id(new),"revoked_grants":len(grants["entries"])}
def product_md(v:dict[str,Any])->bytes:
 sections=["# Product\n\n<!-- impeccable:product-schema 1 -->\n",f"## Platform\n\n{v['platform']}\n"]
 for key,heading in (("stack","Stack"),("users","Users"),("purpose","Product Purpose"),("positioning","Positioning"),("operating_context","Operating Context"),("capabilities_constraints","Capabilities and Constraints"),("brand_commitments","Brand Commitments"),("evidence","Evidence on Hand"),("principles","Product Principles"),("accessibility","Accessibility & Inclusion")):
  value=v.get(key)
  if value:
   body="\n".join(f"- {x}" for x in value) if isinstance(value,list) else value;sections.append(f"## {heading}\n\n{body}\n")
 return "\n".join(sections).encode()
def shared_tokens(manifest:dict[str,Any])->dict[str,Any]:return manifest.get("shared_visual_tokens",{})
def yaml_lines(value:Any,indent:int)->list[str]:
 lines=[]
 for key,item in sorted(value.items()):
  if isinstance(item,dict):lines.append(" "*indent+f"{key}:");lines.extend(yaml_lines(item,indent+2))
  else:lines.append(" "*indent+f"{key}: {json.dumps(item,ensure_ascii=False)}")
 return lines
def design_md(manifest:dict[str,Any])->bytes:
 tokens=shared_tokens(manifest);front=["---",f"name: {json.dumps(manifest['brand_id'])}",f"description: {json.dumps('Brand package '+manifest['version'])}"]
 for key in ("colors","typography","rounded","spacing","components"):
  if tokens.get(key):
   front.append(f"{key}:")
   front.extend(yaml_lines(tokens[key],2))
 front += ["---","",f"# Design System: {manifest['brand_id']}","","## Overview","",f"This portable design record is bound to Brand Package `{manifest['brand_id']}@{manifest['version']}`.","Only explicitly shared visual tokens are normative in frontmatter. PDF renderer tokens remain PDF-only.",""]
 return "\n".join(front).encode()
def sidecar(manifest:dict[str,Any],content:str,product:dict[str,Any])->bytes:
 assets=[]
 for kind in ("fonts","logos"):
  for item in manifest["assets"][kind]:assets.append({"id":item["id"],"kind":kind[:-1],"path":item["path"],"sha256":item["sha256"],"rights":item["rights"]})
 inputs=product["representative_components"];ids=[x["id"] for x in inputs];categories=[x["category"] for x in inputs]
 if len(ids)!=len(set(ids)) or len(categories)!=len(set(categories)):raise AdapterError("representative component ids and primitive categories must be unique")
 tags={"button":"button","input":"input","nav":"nav","chip":"span","card":"article","custom":"section"};components=[]
 for item in inputs:
  tag=tags[item["category"]];cls=f"ds-{item['id']}";name=html.escape(item["name"]);body=f'<{tag} class="{cls}">{name}</{tag}>' if tag!="input" else f'<input class="{cls}" aria-label="{name}" placeholder="{name}">';declarations="; ".join(f"{k}: {v}" for k,v in sorted(item["tokens"].items()));css=f".{cls} {{ {declarations}; }} .{cls}:hover {{ filter: brightness(0.96); }} .{cls}:focus-visible {{ outline: 2px solid currentColor; outline-offset: 2px; }}";components.append({"name":item["name"],"kind":item["category"],"refersTo":item["id"],"description":item["description"],"html":body,"css":css})
 return json_bytes({"schemaVersion":2,"generatedAt":CONTRACT["upstream"]["committed_at"],"title":f"Design System: {manifest['brand_id']}","extensions":{"brandkitAdapter":{"schemaVersion":1,"source":{"brandId":manifest["brand_id"],"brandVersion":manifest["version"],"contentSha256":content},"assets":sorted(assets,key=lambda x:(x["kind"],x["id"]))}},"components":components,"narrative":{}})
def expected_files(manifest:dict[str,Any],content:str,product:dict[str,Any])->dict[str,bytes]:
 v={"PRODUCT.md":product_md(product),"DESIGN.md":design_md(manifest),".impeccable/design.json":sidecar(manifest,content,product),".impeccable/config.json":json_bytes({"hook":{"enabled":False},"updateCheck":False}),".brandkit/impeccable-policy.json":json_bytes({"schema_version":"brandkit-impeccable-policy.v1","brand":{"id":manifest["brand_id"],"version":manifest["version"],"content_sha256":content},"impeccable":{"commit":CONTRACT["upstream"]["commit"],"plugin_version":CONTRACT["upstream"]["plugin_version"]},"capabilities":{k:False for k in ("hooks","update","browser","live","image_generation","network")},"required_provider":None,"reverse_sync":"human-review-only"})}
 validate_json(json.loads(v[".impeccable/design.json"]),"impeccable-design-sidecar.v2.schema.json","design sidecar");validate_json(json.loads(v[".brandkit/impeccable-policy.json"]),"impeccable-policy.v1.schema.json","policy");return v
def ui_files(root:Path)->list[Path]:
 result=[];configured=set()
 cfg=root/".impeccable/config.json"
 if cfg.is_file() and not cfg.is_symlink():
  try:
   for item in json.loads(cfg.read_text()).get("detector",{}).get("extensions",[]):
    ext=item if isinstance(item,str) else item.get("ext")
    if isinstance(ext,str) and re.fullmatch(r"\.?[a-z0-9.]+",ext.lower()):configured.add(ext.lower() if ext.startswith(".") else "."+ext.lower())
  except (OSError,ValueError,AttributeError):raise AdapterError("detector extension configuration is invalid")
 for base,dirs,names in os.walk(root,followlinks=False):
  basep=Path(base);relbase=basep.relative_to(root)
  dirs[:]=[d for d in dirs if d not in SKIP_PARTS]
  for d in list(dirs):
   p=basep/d
   if p.is_symlink():raise AdapterError(f"untrusted symlinked UI subtree: {p.relative_to(root)}")
  if any(x in SKIP_PARTS for x in relbase.parts):continue
  for name in names:
   path=basep/name;low=name.lower()
   matched=path.suffix.lower() in UI_SUFFIXES or name in UI_NAMES or any(len(low)>len(ext) and low.endswith(ext) for ext in configured)
   if not matched:continue
   mode=path.lstat().st_mode
   if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):raise AdapterError(f"UI source must be a regular confined file: {path.relative_to(root)}")
   if root not in path.resolve(strict=True).parents:raise AdapterError("UI source escapes Git root")
   result.append(path)
 return sorted(result,key=lambda p:p.relative_to(root).as_posix())
def mask_js(text:str,strings:bool=True)->str:
 chars=list(text);i=0;quote=None
 while i<len(chars):
  if quote:
   if chars[i]=="\\":
    if strings:chars[i]=" ";i+=1
    if i<len(chars) and strings:chars[i]=" "
   elif chars[i]==quote:quote=None
   elif strings and chars[i]!="\n":chars[i]=" "
  elif chars[i] in "'\"`":quote=chars[i]
  elif chars[i:i+2]==["/","/"]:
   while i<len(chars) and chars[i]!="\n":chars[i]=" ";i+=1
   continue
  elif chars[i:i+2]==["/","*"]:
   chars[i]=chars[i+1]=" ";i+=2
   while i+1<len(chars) and chars[i:i+2]!=["*","/"]:
    if chars[i]!="\n":chars[i]=" "
    i+=1
   if i+1<len(chars):chars[i]=chars[i+1]=" ";i+=1
  i+=1
 return "".join(chars)
def js_value(text:str,start:int)->str:
 i=start;quote=None;depth=0
 while i<len(text):
  c=text[i]
  if quote:
   if c=="\\":i+=2;continue
   if c==quote:quote=None
  elif c in "'\"`":quote=c
  elif c in "([{":depth+=1
  elif c in ")]}":
   if depth==0:break
   depth-=1
  elif depth==0 and c in ",\n":break
  i+=1
 return text[start:i].strip().strip("'\"")
def mask_js_property_keys(text:str)->str:
 chars=list(text);i=0
 while i<len(chars):
  if chars[i:i+2]==["/","/"]:
   while i<len(chars) and chars[i]!="\n":chars[i]=" ";i+=1
   continue
  if chars[i:i+2]==["/","*"]:
   chars[i]=chars[i+1]=" ";i+=2
   while i+1<len(chars) and chars[i:i+2]!=["*","/"]:
    if chars[i]!="\n":chars[i]=" "
    i+=1
   if i+1<len(chars):chars[i]=chars[i+1]=" ";i+=2
   continue
  if chars[i] in "'\"`":
   start=i;quote=chars[i];i+=1;escaped=False
   while i<len(chars):
    if chars[i]=="\\":escaped=True;i+=2;continue
    if chars[i]==quote:break
    i+=1
   end=min(i,len(chars)-1);raw=text[start+1:end];j=end+1
   while j<len(chars) and chars[j].isspace():j+=1
   keep=quote!="`" and not escaped and re.fullmatch(r"[A-Za-z_$][\w$]*",raw) is not None and j<len(chars) and chars[j]==":"
   if not keep:
    for k in range(start,end+1):
     if chars[k]!="\n":chars[k]=" "
   i=end+1;continue
  i+=1
 return "".join(chars)
def js_theme_properties(text:str)->list[tuple[str,str,int]]:
 masked=mask_js_property_keys(text);result=[]
 pattern=re.compile(r"(?<![\w$])(?:(['\"])([A-Za-z_$][\w$]*)\1|([A-Za-z_$][\w$]*))\s*:")
 for m in pattern.finditer(masked):
  name=m.group(2) or m.group(3);result.append((name,js_value(text,m.end()),m.start(2) if m.group(2) else m.start(3)))
 return result
def js_computed_properties(text:str)->list[str]:
 masked=mask_js_property_keys(text);result=[]
 for m in re.finditer(r"\[([^\]\n]+)\]\s*:\s*",masked):
  raw=text[m.start():m.end()].strip();result.append(re.sub(r"\s+"," ",raw))
 return result
def theme_kind(name:str)->str|None:
 low=name.lower()
 if re.search(r"font|typeface|typography",low):return "font"
 if re.search(r"spacing|space|gap|gutter|padding|margin|inset",low):return "spacing"
 if re.search(r"radius|rounded|corner",low):return "radius"
 if re.search(r"shadow|elevation",low):return "shadow"
 if re.search(r"duration|motion|transition|animation|easing|delay",low):return "motion"
 if re.search(r"color|colour|scheme",low) or low in {"primary","secondary","accent","surface","background","foreground","fill","stroke"}:return "color"
 if re.search(r"breakpoint|lineheight|fontsize|zindex|opacity",low):return "theme-value"
 return None
def token_lexemes(path:Path,root:Path)->list[dict[str,Any]]:
 text=path.read_text(errors="replace");found=[];rel=path.relative_to(root).as_posix();is_js=path.suffix.lower() in {".js",".mjs",".cjs",".jsx",".ts",".mts",".cts",".tsx"}
 if is_js:
  for name,value,start in js_theme_properties(text):
   kind=theme_kind(name)
   if not kind:continue
   line=text.count("\n",0,start)+1;last=text.rfind("\n",0,start);column=start-(last+1)+1;found.append({"path":rel,"kind":kind,"name":name,"value":value,"location":{"line":line,"column":column}})
  patterns=[("utility",re.compile(r"(?:class|className)\s*=\s*[\"'`](?P<value>(?:[A-Za-z0-9_:\-\[\]/.%]+\s*)+)[\"'`]"))];scan=mask_js(text,False)
 else:
  scan=re.sub(r"/\*.*?\*/",lambda m:"\n"*m.group(0).count("\n"),text,flags=re.S)
  patterns=[("custom-property",re.compile(r"(?P<name>--[A-Za-z0-9_-]+)\s*:\s*(?P<value>[^;}{\n]+)")),("color",re.compile(r"(?P<value>#[0-9A-Fa-f]{3,8}\b|(?:rgb|rgba|hsl|hsla|oklch|oklab|lab|lch|color)\([^\n;{}]+?\))")),("font",re.compile(r"(?P<name>font-family)\s*:\s*[\"']?(?P<value>[^;\n,}\"']+(?:\s+[^;\n,}\"']+)*)")),("radius",re.compile(r"(?P<name>border-radius)\s*:\s*(?P<value>[^;\n}]+)")),("shadow",re.compile(r"(?P<name>box-shadow)\s*:\s*(?P<value>[^;\n}]+)")),("motion",re.compile(r"(?P<name>transition(?:-duration)?|animation-duration)\s*:\s*(?P<value>[^;\n}]+)")),("utility",re.compile(r"(?:class|className)\s*=\s*[\"'`](?P<value>(?:[A-Za-z0-9_:\-\[\]/.%]+\s*)+)[\"'`]")),("spacing",re.compile(r"(?P<value>-?(?:\d*\.)?\d+(?:px|rem|em|vh|vw|vmin|vmax))"))]
 for kind,pattern in patterns:
  for m in pattern.finditer(scan):
   start=m.start("name") if "name" in m.groupdict() else m.start("value");line=scan.count("\n",0,start)+1;last=scan.rfind("\n",0,start);column=start-(last+1)+1
   found.append({"path":rel,"kind":kind,"name":m.groupdict().get("name") or f"{kind}@{line}:{column}","value":m.group("value").strip(),"location":{"line":line,"column":column}})
 return found
def plausible_unknowns(path:Path)->list[str]:
 text=path.read_text(errors="replace")
 if path.suffix.lower() not in {".js",".mjs",".cjs",".jsx",".ts",".mts",".cts",".tsx"}:return []
 vocabulary=re.compile(r"(?:token|style|theme|scale|size|width|height|palette|tone|weight|leading|tracking|density)",re.I)
 unknown=[f"{name}: {value}" for name,value,_ in js_theme_properties(text) if theme_kind(name) is None and vocabulary.search(name)]
 unknown.extend(f"computed property requires review: {item}" for item in js_computed_properties(text))
 return unknown
def ui_findings(root:Path,files:list[Path],content:str)->list[str]:
 if not files:return []
 path=safe_target(root,UI_INVENTORY)
 if not path.is_file():return [f"- UI code exists but schema-valid inventory `{UI_INVENTORY}` is missing"]
 try:value=json.loads(path.read_text());validate_json(value,"ui-token-inventory.v1.schema.json","UI token inventory")
 except (OSError,ValueError,AdapterError) as exc:return [f"- UI inventory rejected: {exc}"]
 unsigned={k:v for k,v in value.items() if k!="inventory_sha256"}
 if value["inventory_sha256"]!=sha(canonical(unsigned)):return ["- UI inventory self-hash is invalid"]
 if value["project_root"]!=str(root) or value["project_commit"]!=project_commit(root) or value["brand_content_sha256"]!=content:return ["- UI inventory project/commit/brand binding is stale"]
 listed=[x["path"] for x in value["files"]]
 if len(listed)!=len(set(listed)):return ["- UI inventory contains duplicate file entries"]
 expected={p.relative_to(root).as_posix():sha(p.read_bytes()) for p in files};actual={x["path"]:x["sha256"] for x in value["files"]}
 if actual!=expected:return [f"- UI inventory incomplete/stale; expected `{json.dumps(expected,sort_keys=True)}`"]
 scanned=[x for p in files for x in token_lexemes(p,root)]
 unknown=[f"{p.relative_to(root)}: {item}" for p in files for item in plausible_unknowns(p)]
 if unknown:return ["- UI source contains plausible unclassified token constructs: "+"; ".join(unknown)]
 norm=lambda x:(x["path"],x["kind"],x["name"],x["value"],x["location"]["line"],x["location"]["column"])
 if sorted(map(norm,value["tokens"]))!=sorted(map(norm,scanned)):return ["- UI token inventory does not exactly match extracted source lexemes and locations"]
 return []
def diff(relative:str,actual:bytes,expected:bytes)->str:return "".join(difflib.unified_diff(actual.decode(errors="replace").splitlines(True),expected.decode(errors="replace").splitlines(True),fromfile=f"current/{relative}",tofile=f"brand-package/{relative}"))
def review_bytes(diffs:list[str],findings:list[str])->bytes:
 blocks=["# Impeccable Design Contract Review\n","- [ ] Human review required; no reverse sync was performed.\n"]
 if diffs:blocks += ["\n## Artifact diffs\n",*[f"\n```diff\n{x}```\n" for x in diffs]]
 if findings:blocks += ["\n## UI inventory blockers\n",*[x+"\n" for x in findings]]
 return "".join(blocks).encode()
def confined_tx_dir(root:Path,relative:str,prefix:str)->Path:
 posix=PurePosixPath(relative)
 if posix.is_absolute() or ".." in posix.parts or not relative.startswith(prefix) or len(posix.parts)!=4:raise AdapterError("transaction directory is unconfined")
 path=root/posix
 if path.is_symlink() or (path.exists() and not path.is_dir()):raise AdapterError("transaction directory is unsafe")
 if path.exists() and root not in path.resolve(strict=True).parents:raise AdapterError("transaction directory escapes Git root")
 return path
def recover(root:Path,content:str)->bool:
 journal=root/JOURNAL
 if not journal.exists() and not journal.is_symlink():return False
 if journal.is_symlink() or not journal.is_file():raise AdapterError("transaction journal is not a trusted regular file")
 sealed=json.loads(journal.read_text());validate_json(sealed,"impeccable-transaction.v2.schema.json","transaction journal");v=unseal(sealed,auth_secret(Path.home()),"transaction journal")
 if v["project_root"]!=str(root) or v["project_commit"]!=project_commit(root) or v["brand_content_sha256"]!=content:raise AdapterError("foreign transaction journal")
 paths=[x["path"] for x in v["entries"]]
 if len(paths)!=len(set(paths)) or not set(paths)<=ARTIFACTS:raise AdapterError("transaction artifact allowlist violation")
 backup=confined_tx_dir(root,v["backup"],".brandkit/transactions/preimages/");staging=confined_tx_dir(root,v["staging"],".brandkit/transactions/staging/")
 restore=[]
 for item in v["entries"]:
  target=safe_target(root,item["path"],True)
  current=sha(target.read_bytes()) if target.exists() else None
  if current not in {item["preimage_sha256"],item["postimage_sha256"]}:raise AdapterError("transaction target hash is neither signed preimage nor postimage")
  staged=safe_target(root,f"{v['staging']}/{item['path']}")
  if staged.exists() and sha(staged.read_bytes())!=item["postimage_sha256"]:raise AdapterError("staged postimage hash mismatch")
  if item["preimage_sha256"] is None:
   restore.append((target,None))
  else:
   source=safe_target(root,f"{v['backup']}/{item['path']}");data=source.read_bytes()
   if sha(data)!=item["preimage_sha256"]:raise AdapterError("preimage hash mismatch")
   restore.append((target,data))
 for target,data in reversed(restore):
  if data is None:
   if target.exists():target.unlink()
  else:
   tmp=target.with_name(f".{target.name}.recover-{os.getpid()}");tmp.write_bytes(data);os.replace(tmp,target)
 journal.unlink();shutil.rmtree(backup,ignore_errors=True);shutil.rmtree(staging,ignore_errors=True);transaction_ledger_remove(Path.home(),root,v["transaction_id"],auth_secret(Path.home()));return True
def transact(root:Path,changes:dict[str,bytes],content:str)->list[str]:
 if not changes or not set(changes)<=ARTIFACTS:raise AdapterError("transaction contains an unauthorized artifact")
 safe_target(root,JOURNAL,True);tx=secrets.token_hex(16);run=secrets.token_hex(16);backup_rel=f".brandkit/transactions/preimages/{tx}";stage_rel=f".brandkit/transactions/staging/{tx}";backup=confined_tx_dir(root,backup_rel,".brandkit/transactions/preimages/");staging=confined_tx_dir(root,stage_rel,".brandkit/transactions/staging/");backup.mkdir(parents=True);staging.mkdir(parents=True);entries=[]
 for relative in changes:
  target=safe_target(root,relative,True);before=target.read_bytes() if target.exists() else None;entries.append({"path":relative,"preimage_sha256":sha(before) if before is not None else None,"postimage_sha256":sha(changes[relative])})
  if before is not None:
   copy=backup/relative;copy.parent.mkdir(parents=True,exist_ok=True);copy.write_bytes(before)
  staged=staging/relative;staged.parent.mkdir(parents=True,exist_ok=True);staged.write_bytes(changes[relative])
 secret=auth_secret(Path.home(),True);journal={"schema_version":"brandkit-impeccable-transaction.v2","transaction_id":tx,"run_id":run,"project_root":str(root),"project_commit":project_commit(root),"brand_content_sha256":content,"backup":backup_rel,"staging":stage_rel,"entries":entries};transaction_ledger_add(Path.home(),root,tx,secret);atomic_json(root/JOURNAL,seal(journal,secret));written=[]
 try:
  fail=int(os.environ.get("BRANDKIT_IMPECCABLE_FAIL_AFTER","0"))
  crash=int(os.environ.get("BRANDKIT_IMPECCABLE_CRASH_AFTER","0"))
  for index,relative in enumerate(changes,1):
   target=safe_target(root,relative,True);item=entries[index-1];current=sha(target.read_bytes()) if target.exists() else None
   if current!=item["preimage_sha256"] or sha((staging/relative).read_bytes())!=item["postimage_sha256"]:raise AdapterError("transaction pre/post hash changed before replace")
   os.replace(staging/relative,target);written.append(relative)
   if crash and index==crash:os._exit(86)
   if fail and index==fail:raise OSError("injected transaction failure")
  (root/JOURNAL).unlink();shutil.rmtree(backup);shutil.rmtree(staging);transaction_ledger_remove(Path.home(),root,tx,secret)
 except BaseException:recover(root,content);raise
 return written
def sync(project:Path,check_only:bool)->int:
 root,manifest,content=resolve_package(project);recovered=recover(root,content);product=read_product(root);expected=expected_files(manifest,content,product);diffs=[];missing=[]
 for relative,data in expected.items():
  target=safe_target(root,relative)
  if target.exists():
   actual=target.read_bytes()
   if actual!=data:diffs.append(diff(relative,actual,data))
  else:missing.append(relative)
 findings=ui_findings(root,ui_files(root),content)
 if diffs or findings:
  if not check_only:transact(root,{CONTRACT["artifacts"]["review"]:review_bytes(diffs,findings)},content)
  print(json.dumps({"status":"conflict","review":CONTRACT["artifacts"]["review"],"artifact_diffs":len(diffs),"ui_findings":len(findings),"recovered":recovered},sort_keys=True));return 3
 if check_only and missing:print(json.dumps({"status":"missing","files":missing,"recovered":recovered},sort_keys=True));return 2
 changes={r:expected[r] for r in missing};written=transact(root,changes,content) if changes else []
 print(json.dumps({"status":"ok","written":written,"brand":f"{manifest['brand_id']}@{manifest['version']}","recovered":recovered},sort_keys=True));return 0
def main()->int:
 p=argparse.ArgumentParser();sub=p.add_subparsers(dest="command",required=True)
 for name in ("sync","check"):c=sub.add_parser(name);c.add_argument("--project",required=True,type=Path)
 c=sub.add_parser("verify");c.add_argument("--checkout",type=Path,default=DEFAULT_CHECKOUT)
 c=sub.add_parser("materialize");c.add_argument("--checkout",type=Path,default=DEFAULT_CHECKOUT);c.add_argument("--home",type=Path,default=Path.home())
 for name in ("activate","status","rollback"):c=sub.add_parser(name);c.add_argument("--home",type=Path,default=Path.home())
 c=sub.add_parser("approve");c.add_argument("--project",required=True,type=Path);c.add_argument("--home",type=Path,default=Path.home());c.add_argument("--capability",action="append",required=True,choices=sorted(CAPABILITIES));c.add_argument("--evidence",required=True,help="materializes the current explicit user instruction as sealed receipt evidence");c.add_argument("--ttl",type=int,default=3600)
 c=sub.add_parser("grant");c.add_argument("--project",required=True,type=Path);c.add_argument("--home",type=Path,default=Path.home());c.add_argument("--approval-receipt",required=True)
 c=sub.add_parser("revoke");c.add_argument("--project",required=True,type=Path);c.add_argument("--home",type=Path,default=Path.home())
 c=sub.add_parser("rotate-secret");c.add_argument("--home",type=Path,default=Path.home())
 args=p.parse_args()
 try:
  if args.command in ("sync","check"):return sync(args.project,args.command=="check")
  if args.command=="approve":result=approve(args.project,args.home,args.capability,args.evidence,args.ttl)
  elif args.command=="grant":result=grant(args.project,args.home,args.approval_receipt)
  elif args.command=="revoke":result=revoke(args.project,args.home)
  elif args.command=="rotate-secret":result=rotate_secret(args.home)
  elif args.command=="verify":result=verify(args.checkout)
  elif args.command=="materialize":result=materialize(args.checkout,args.home)
  elif args.command=="activate":result=activate(args.home)
  elif args.command=="status":result=status(args.home)
  else:result=rollback(args.home)
  print(json.dumps(result,sort_keys=True));return 0
 except (brand_package.ContractError,AdapterError,OSError,ValueError,KeyError,json.JSONDecodeError) as exc:print(f"impeccable adapter: {exc}",file=sys.stderr);return 1
if __name__=="__main__":raise SystemExit(main())
