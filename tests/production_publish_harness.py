#!/usr/bin/env python3
"""Real production publish attacks; synthetic Humanizer evidence is not provider evidence."""
from __future__ import annotations
import argparse,contextlib,hashlib,hmac,json,os,shutil,stat,subprocess,sys,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"scripts"))
import brand_package,brand_voice_pdf as workflow,markdown_contract as contract
VERSION="1.2.28"
FIXTURE=ROOT/"fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360";ASSETS=ROOT/"fixtures/markdown"
REQUIRED=["original.md","preflight.json","audit-call.json","audit-ledger.json","confirmed-ledger.json","edits.json","result.md","report.json","changes.diff","evidence-ledger.json","evidence-report.json"]
CLASSES=("pdf","page-png","visual-qa","source-map","render-result","run-v2","final","baseline","audit","approval","approval-request","workflow-lock","activation","skill-lock","trust","renderer-lock","humanizer-report","owner-key","issuer-key")
ATTACKS=("replace","remove","leaf-symlink","ancestor-symlink","last-race")
def sha(data:bytes)->str:return hashlib.sha256(data).hexdigest()
def write_json(path:Path,value)->None:path.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf-8")
def inventory(root:Path)->list[dict]:
 out=[]
 for path in sorted(root.rglob("*")):
  st=path.lstat();row={"path":path.relative_to(root).as_posix(),"mode":format(stat.S_IMODE(st.st_mode),"04o"),"type":"symlink" if path.is_symlink() else "directory" if path.is_dir() else "file"}
  if path.is_symlink():row["target"]=os.readlink(path)
  elif path.is_file():row["sha256"]=sha(path.read_bytes())
  out.append(row)
 return out
def clone_installation(home:Path,key:Path)->None:
 source=Path(os.environ.get("BRANDKIT_HARNESS_SOURCE_HOME",str(Path.home())));release=home/f".agents/skill-store/brand-voice-pdf/{VERSION}";runtime=home/f".agents/runtime-store/brandkit-pdf-renderer/{VERSION}";release.parent.mkdir(parents=True);runtime.parent.mkdir(parents=True)
 shutil.copytree(source/f".agents/skill-store/brand-voice-pdf/{VERSION}",release,symlinks=True,copy_function=shutil.copy2);shutil.copytree(source/f".agents/runtime-store/brandkit-pdf-renderer/{VERSION}",runtime,symlinks=True,copy_function=shutil.copy2)
 links={home/".agents/skills/brand-voice-pdf":f"../skill-store/brand-voice-pdf/{VERSION}",home/".claude/skills/brand-voice-pdf":"../../.agents/skills/brand-voice-pdf",home/".hermes/skills/brand-voice-pdf":"../../.agents/skills/brand-voice-pdf"}
 for path,target in links.items():path.parent.mkdir(parents=True,exist_ok=True);path.symlink_to(target)
 verify_key=home/".agents/keys/brand-voice-pdf.key";verify_key.parent.mkdir(parents=True);shutil.copy2(key,verify_key);verify_key.chmod(0o600)
 payload={"schema_version":"brand-voice-pdf-release-manifest.v1","version":VERSION,"predecessor_links":{str(p):None for p in links},"release":inventory(release),"runtime":inventory(runtime),"link_topology":{str(p):t for p,t in links.items()}};raw=json.dumps(payload,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode();envelope={"payload":payload,"hmac_sha256":hmac.new(verify_key.read_bytes(),raw,hashlib.sha256).hexdigest()}
 state=home/".agents/activation-state";state.mkdir(parents=True);manifest=state/f"brand-voice-pdf-{VERSION}.manifest.json";write_json(manifest,envelope);active={"schema_version":"brand-voice-pdf-activation.v1","version":VERSION,"manifest_sha256":sha(manifest.read_bytes()),"predecessor_links":{str(p):None for p in links},"links":{str(p):t for p,t in links.items()}};write_json(state/"brand-voice-pdf.json",{"payload":active,"hmac_sha256":hmac.new(verify_key.read_bytes(),json.dumps(active,sort_keys=True,separators=(",",":")).encode(),hashlib.sha256).hexdigest()})
def seed(workspace:Path,issuer_key:Path,owner_key:Path,run_id="r9-security"):
 project=workspace/"project";project.mkdir();subprocess.run(["git","init","-q",str(project)],check=True);brand_package.pin_project(project,FIXTURE,"1.0.0");preflight=workflow.workflow_preflight(project,"neutral-fixture","claude",owner_key);lock_path=project/"workflow-lock.v2.json";write_json(lock_path,preflight["value"])
 source=(ROOT/"fixtures/markdown/valid-report.md").read_text().replace("status: review","status: release",1);resolution=brand_package.resolve(project,None,"neutral-fixture","1.0.0",run_id);run_dir=Path(resolution["roots"]["work"]);run_dir.mkdir(parents=True);draft=run_dir/"draft.md";final=run_dir/"final.md";draft.write_text(source);final.write_text(source);ast=contract.run_validate(final,preflight["dtype_path"],ASSETS,False,None)
 baseline=contract.create_humanizer_baseline(ast,preflight["dtype"],preflight["brand_content"],"brandkit-production-renderer-v1",issuer_key,f"attack-harness-{run_id}-0001","2026-08-27T00:00:00Z",brand_manifest=preflight["brand_path"]);baseline_path=run_dir/"humanizer-baseline.json";write_json(baseline_path,baseline);raw_runs=[]
 for segment in workflow.mutable_segments(ast):
  raw=run_dir/"humanizer-raw"/segment["segment_id"];raw.mkdir(parents=True)
  for name in REQUIRED:
   if name in {"original.md","result.md"}:(raw/name).write_text(segment["text"]+"\n")
   elif name=="report.json":write_json(raw/name,{"accepted":True,"provider":"claude","model_calls":1,"edit_count":0,"candidate_count":0,"confirmed_candidate_count":0,"discarded_candidate_count":0,"prompt_sha256":{"rewrite":None},"protected_violations":[],"evidence_blockers":[],"evidence_origin":"deterministic-security-fixture-not-provider-evidence"})
   elif name=="confirmed-ledger.json":write_json(raw/name,{"discarded_candidates":[]})
   elif name=="evidence-report.json":write_json(raw/name,{"ok":True,"findings":[],"evidence_origin":"deterministic-security-fixture-not-provider-evidence"})
   else:write_json(raw/name,{"evidence_origin":"deterministic-security-fixture-not-provider-evidence"})
  raw_runs.append((segment,raw,segment["text"]))
 audit=workflow.make_audit(baseline,ast,issuer_key,run_dir,raw_runs,preflight["release"],preflight["value"]["humanizer"]["release_attestation_sha256"]);audit_path=run_dir/"humanizer-audit.json";write_json(audit_path,audit);request={"schema_version":"approval-request.v1","payload":{"final_sha256":ast["source_sha256"],"brand_id":"neutral-fixture","document_type_id":"report","workflow_lock_sha256":sha(lock_path.read_bytes()),"project_identity_sha256":sha(str(project.resolve()).encode()),"run_id":run_id}};request_path=run_dir/"approval-request.json";write_json(request_path,request)
 signed={"schema_version":"owner-approval.v1","issuer":"fixture-owner","payload":{**request["payload"],"decision":"release","approved_at":"2026-08-27T00:00:00Z"}};approval={**signed,"hmac_sha256":hmac.new(contract._key_bytes(owner_key),workflow.canonical(signed),hashlib.sha256).hexdigest()};approval_path=workspace/"owner-approval.json";write_json(approval_path,approval);write_json(run_dir/"prepare.json",{"schema_version":"brand-voice-pdf-security-prepare.v1","evidence_origin":"deterministic-security-fixture-not-provider-evidence","draft_sha256":ast["source_sha256"],"workflow_lock_sha256":sha(lock_path.read_bytes())})
 args=argparse.Namespace(command="publish",project=project,draft=None,asset_root=ASSETS,run_id=run_id,brand_id="neutral-fixture",provider="claude",model="haiku",timeout=30,issuer_key=issuer_key,mermaid_cas=Path(os.environ["BRANDKIT_MERMAID_CAS"]),workflow_lock=lock_path,owner_approval=approval_path,owner_key=owner_key);return args,run_dir,raw_runs
def targets(args,run_dir,raw_runs,home):
 out=run_dir/"output";return {"pdf":next(out.glob("*.pdf")),"page-png":next((out/"pages").glob("*.png")),"visual-qa":out/"visual-qa.json","source-map":out/"renderer-work/renderer-source-map.json","render-result":out/"render-result.json","run-v2":out/"run.json","final":run_dir/"final.md","baseline":run_dir/"humanizer-baseline.json","audit":run_dir/"humanizer-audit.json","approval":args.owner_approval,"approval-request":run_dir/"approval-request.json","workflow-lock":args.workflow_lock,"activation":home/".agents/activation-state/brand-voice-pdf.json","skill-lock":args.project/".brandkit/skill-lock.json","trust":ROOT/"renderer-trust.v1.json","renderer-lock":ROOT/"toolchain/pdf-renderer.production.v1.lock.json","humanizer-report":raw_runs[0][1]/"report.json","owner-key":args.owner_key,"issuer-key":args.issuer_key}
@contextlib.contextmanager
def mutation(path:Path,attack:str):
 parent=path.parent;parent_mode=stat.S_IMODE(parent.stat().st_mode);mode=stat.S_IMODE(path.lstat().st_mode);data=path.read_bytes();original_link=os.readlink(path) if path.is_symlink() else None;moved=None;parent.chmod(parent_mode|0o200)
 try:
  if attack=="replace":path.chmod(mode|0o200);path.write_bytes(data+b"\nattack\n")
  elif attack=="remove":path.unlink()
  elif attack=="leaf-symlink":moved=path.with_name(path.name+".security-original");path.rename(moved);path.symlink_to(moved.name)
  elif attack=="ancestor-symlink":moved=parent.with_name(parent.name+".security-original");grand=parent.parent;grand_mode=stat.S_IMODE(grand.stat().st_mode);grand.chmod(grand_mode|0o200);parent.rename(moved);parent.symlink_to(moved.name,target_is_directory=True)
  yield
 finally:
  if attack=="ancestor-symlink":parent.unlink(missing_ok=True);moved.rename(parent);parent.parent.chmod(grand_mode)
  elif attack=="leaf-symlink":path.unlink(missing_ok=True);moved.rename(path)
  else:
   if original_link is not None:
    if not path.exists() and not path.is_symlink():path.symlink_to(original_link)
    elif attack=="replace":path.chmod(mode|0o200);path.write_bytes(data)
   else:
    if not path.exists():path.write_bytes(data)
    else:path.chmod(mode|0o200);path.write_bytes(data)
    path.chmod(mode)
  parent.chmod(parent_mode)
def run_attack(args,path,attack,boundary_root):
 if attack!="last-race":
  with mutation(path,attack):workflow.publish(args)
  raise RuntimeError("attack unexpectedly published")
 boundary=boundary_root/f"race-{sha(str(path).encode())[:12]}";boundary.mkdir(parents=True);os.environ["BRANDKIT_SECURITY_HARNESS_BOUNDARY"]=str(boundary);result={}
 def invoke():
  try:result["value"]=workflow.publish(args)
  except Exception as exc:result["error"]=exc
 thread=threading.Thread(target=invoke);thread.start();ready=boundary/"final-atomic-commit.ready"
 for _ in range(2000):
  if ready.exists():break
  time.sleep(.01)
 else:raise RuntimeError("publish never reached final boundary")
 with mutation(path,"replace"):(boundary/"final-atomic-commit.resume").write_text("resume\n");thread.join(30)
 os.environ.pop("BRANDKIT_SECURITY_HARNESS_BOUNDARY",None)
 if thread.is_alive() or "error" not in result:raise RuntimeError("last-race unexpectedly published or hung")
def main()->int:
 p=argparse.ArgumentParser();p.add_argument("--workspace",type=Path,required=True);p.add_argument("--issuer-key",type=Path,required=True);p.add_argument("--owner-key",type=Path,required=True);p.add_argument("--mermaid-cas",type=Path,required=True);a=p.parse_args()
 if a.workspace.exists():raise SystemExit("workspace must be absent")
 a.workspace.mkdir(parents=True);home=a.workspace/"home";clone_installation(home,a.issuer_key);os.environ.update(BRANDKIT_HOME=str(home),BRANDKIT_MERMAID_CAS=str(a.mermaid_cas));owner=a.workspace/"owner.key";shutil.copy2(a.owner_key,owner);owner.chmod(0o600);args,run_dir,raw_runs=seed(a.workspace,home/".agents/keys/brand-voice-pdf.key",owner);first=workflow.publish(args);repeat=workflow.publish(args)
 if first!=repeat or first.get("status")!="published":raise SystemExit("positive publish/repeat differs")
 paths=targets(args,run_dir,raw_runs,home);matrix=[]
 for name in CLASSES:
  for attack in ATTACKS:
   print(f"ATTACK {name} {attack}",flush=True)
   try:run_attack(args,paths[name],attack,a.workspace/"boundaries")
   except (workflow.WorkflowError,contract.ContractError,brand_package.ContractError,OSError,ValueError,subprocess.SubprocessError) as exc:matrix.append({"class":name,"attack":attack,"path":str(paths[name]),"expected_exit":"blocked","actual_exit":"blocked","error_type":type(exc).__name__})
   else:matrix.append({"class":name,"attack":attack,"path":str(paths[name]),"expected_exit":"blocked","actual_exit":"blocked","error_type":"thread-captured"})
 domains={"run":run_dir,"project-state":args.project/".brandkit","global-state":home/".agents","brand-release":FIXTURE};whole=[]
 for label,root in domains.items():whole.extend({"domain":label,**row} for row in inventory(root))
 evidence={"schema_version":"brand-voice-pdf-production-attack-harness.v1","renderer_version":VERSION,"evidence_origin":"deterministic-security-fixture-not-provider-evidence","positive":{"first":first,"repeat":repeat},"matrix":matrix,"whole_commit_domain":{"file_count":len(whole),"inventory":whole}}
 if len(matrix)!=len(CLASSES)*len(ATTACKS):raise SystemExit("matrix incomplete")
 write_json(a.workspace/"production-attack-evidence.json",evidence);print(json.dumps({"status":"pass","cases":len(matrix),"evidence":str(a.workspace/"production-attack-evidence.json")},sort_keys=True));return 0
if __name__=="__main__":raise SystemExit(main())
