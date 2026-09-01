#!/usr/bin/env python3
"""Exercise the real renderer rollback transaction in one isolated HOME."""
from __future__ import annotations
import argparse,hashlib,hmac,json,os,shutil,stat,subprocess
from pathlib import Path

VERSION="1.2.26";NEXT="1.2.28"
def sha(data:bytes)->str:return hashlib.sha256(data).hexdigest()
def write(path:Path,value)->None:path.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n")
def inventory(root:Path):
 out=[]
 for path in sorted(root.rglob("*")):
  st=path.lstat();row={"path":path.relative_to(root).as_posix(),"mode":format(stat.S_IMODE(st.st_mode),"04o"),"type":"symlink" if path.is_symlink() else "directory" if path.is_dir() else "file"}
  if path.is_symlink():row["target"]=os.readlink(path)
  elif path.is_file():row["sha256"]=sha(path.read_bytes())
  out.append(row)
 return out
def sign(payload,key):return {"payload":payload,"hmac_sha256":hmac.new(key,json.dumps(payload,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode(),hashlib.sha256).hexdigest()}
def seed(home:Path,source:Path,key_path:Path):
 release=home/f".agents/skill-store/brand-voice-pdf/{VERSION}";runtime=home/f".agents/runtime-store/brandkit-pdf-renderer/{VERSION}";release.parent.mkdir(parents=True);runtime.parent.mkdir(parents=True)
 shutil.copytree(source/f".agents/skill-store/brand-voice-pdf/{VERSION}",release,symlinks=True,copy_function=shutil.copy2);shutil.copytree(source/f".agents/runtime-store/brandkit-pdf-renderer/{VERSION}",runtime,symlinks=True,copy_function=shutil.copy2)
 links={home/".agents/skills/brand-voice-pdf":f"../skill-store/brand-voice-pdf/{VERSION}",home/".claude/skills/brand-voice-pdf":"../../.agents/skills/brand-voice-pdf",home/".hermes/skills/brand-voice-pdf":"../../.agents/skills/brand-voice-pdf"}
 for path,target in links.items():path.parent.mkdir(parents=True,exist_ok=True);path.symlink_to(target)
 key_target=home/".agents/keys/brand-voice-pdf.key";key_target.parent.mkdir(parents=True);shutil.copy2(key_path,key_target);key_target.chmod(0o600);key=key_target.read_bytes();state=home/".agents/activation-state";state.mkdir()
 manifest_payload={"schema_version":"brand-voice-pdf-release-manifest.v1","version":VERSION,"predecessor_links":{str(p):None for p in links},"release":inventory(release),"runtime":inventory(runtime),"link_topology":{str(p):t for p,t in links.items()}};manifest=state/f"brand-voice-pdf-{VERSION}.manifest.json";write(manifest,sign(manifest_payload,key))
 activation={"schema_version":"brand-voice-pdf-activation.v1","version":VERSION,"manifest_sha256":sha(manifest.read_bytes()),"predecessor_links":{str(p):None for p in links},"links":{str(p):t for p,t in links.items()}};write(state/"brand-voice-pdf.json",sign(activation,key))
def active_snapshot(home:Path):
 state=home/".agents/activation-state/brand-voice-pdf.json";links=[home/".agents/skills/brand-voice-pdf",home/".claude/skills/brand-voice-pdf",home/".hermes/skills/brand-voice-pdf"]
 return {"activation":state.read_bytes(),"links":{str(p):os.readlink(p) for p in links},"pyc":sorted(str(p) for p in home.rglob("*.pyc"))}
def assert_active(home:Path,expected):
 actual=active_snapshot(home)
 if actual!=expected:raise RuntimeError("rollback failure did not restore activation, links, or pyc inventory byte-exactly")
 verifier=home/f".agents/runtime-store/brandkit-pdf-renderer/{NEXT}/toolchain/pdf-renderer/verify-installation"
 check=subprocess.run([str(verifier),str(home),str(home/".agents/keys/brand-voice-pdf.key"),NEXT],capture_output=True,text=True,env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1"})
 if check.returncode:raise RuntimeError(f"restored target fails signed verification: {check.stderr}")
def main():
 p=argparse.ArgumentParser();p.add_argument("--home",type=Path,required=True);p.add_argument("--source-home",type=Path,required=True);p.add_argument("--repo",type=Path,required=True);p.add_argument("--wheelhouse",type=Path,required=True);p.add_argument("--key",type=Path,required=True);a=p.parse_args()
 if a.home.exists():raise SystemExit("test HOME must be absent")
 a.home.mkdir();seed(a.home,a.source_home,a.key);subprocess.run([str(a.repo/"toolchain/pdf-renderer/install-from-lock"),str(a.home),str(a.wheelhouse),str(a.key)],check=True,capture_output=True,text=True)
 rollback=a.home/f".agents/skill-store/brand-voice-pdf/{NEXT}/scripts/rollback";baseline=active_snapshot(a.home);matrix=[]
 def blocked(label,mutate=None,restore=None,env=None,target=VERSION):
  if mutate:mutate()
  run=subprocess.run([str(rollback),str(a.home),str(a.home/".agents/keys/brand-voice-pdf.key"),target],capture_output=True,text=True,env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1",**(env or {})})
  if restore:restore()
  if run.returncode==0:raise RuntimeError(f"rollback attack unexpectedly succeeded: {label}")
  assert_active(a.home,baseline);matrix.append({"case":label,"status":"blocked","exit":run.returncode})
 blocked("bad-target-pin",target="9.9.9")
 manifest=a.home/f".agents/activation-state/brand-voice-pdf-{VERSION}.manifest.json";manifest_bytes=manifest.read_bytes();blocked("manifest-tamper",lambda:manifest.write_bytes(manifest_bytes+b"x"),lambda:manifest.write_bytes(manifest_bytes))
 release_file=a.home/f".agents/skill-store/brand-voice-pdf/{VERSION}/SKILL.md";release_bytes=release_file.read_bytes();release_mode=stat.S_IMODE(release_file.stat().st_mode)
 blocked("release-tamper",lambda:(release_file.chmod(0o600),release_file.write_bytes(release_bytes+b"x")),lambda:(release_file.write_bytes(release_bytes),release_file.chmod(release_mode)))
 runtime_file=a.home/f".agents/runtime-store/brandkit-pdf-renderer/{VERSION}/scripts/pdf_renderer.py";runtime_bytes=runtime_file.read_bytes();runtime_mode=stat.S_IMODE(runtime_file.stat().st_mode)
 blocked("runtime-tamper",lambda:(runtime_file.chmod(0o600),runtime_file.write_bytes(runtime_bytes+b"x")),lambda:(runtime_file.write_bytes(runtime_bytes),runtime_file.chmod(runtime_mode)))
 blocked("mode-tamper",lambda:runtime_file.chmod(0o600),lambda:runtime_file.chmod(runtime_mode))
 runtime_root=runtime_file.parents[1];extra=runtime_root/"unexpected-link";blocked("link-topology-tamper",lambda:(runtime_root.chmod(0o700),extra.symlink_to("scripts")),lambda:(extra.unlink(),runtime_root.chmod(0o555)))
 for switch in (1,2,3):blocked(f"failure-after-switch-{switch}",env={"BRANDKIT_ROLLBACK_FAIL_AFTER_SWITCH":str(switch)})
 success_home=a.home.parent/"success-home";success_home.mkdir();seed(success_home,a.source_home,a.key);subprocess.run([str(a.repo/"toolchain/pdf-renderer/install-from-lock"),str(success_home),str(a.wheelhouse),str(a.key)],check=True,capture_output=True,text=True)
 success_rollback=success_home/f".agents/skill-store/brand-voice-pdf/{NEXT}/scripts/rollback";success_pyc=sorted(str(p.relative_to(success_home)) for p in success_home.rglob("*.pyc"));success=subprocess.run([str(success_rollback),str(success_home),str(success_home/".agents/keys/brand-voice-pdf.key"),VERSION],capture_output=True,text=True,env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1"})
 if success.returncode or json.loads((success_home/".agents/activation-state/brand-voice-pdf.json").read_text())["payload"]["version"]!=VERSION:raise RuntimeError(f"successful rollback/smoke failed: {success.stderr}")
 forward=success_home/f".agents/skill-store/brand-voice-pdf/{VERSION}/scripts/rollback";restore=subprocess.run([str(forward),str(success_home),str(success_home/".agents/keys/brand-voice-pdf.key"),NEXT],capture_output=True,text=True,env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1"})
 verifier=success_home/f".agents/runtime-store/brandkit-pdf-renderer/{NEXT}/toolchain/pdf-renderer/verify-installation";verified=subprocess.run([str(verifier),str(success_home),str(success_home/".agents/keys/brand-voice-pdf.key"),NEXT],capture_output=True,text=True,env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1"})
 if restore.returncode or verified.returncode or json.loads((success_home/".agents/activation-state/brand-voice-pdf.json").read_text())["payload"]["version"]!=NEXT:raise RuntimeError(f"atomic forward restoration failed: {restore.stderr} {verified.stderr}")
 if sorted(str(p.relative_to(success_home)) for p in success_home.rglob("*.pyc"))!=success_pyc:raise RuntimeError("rollback created runtime pyc drift")
 result={"schema_version":"brand-voice-pdf-rollback-matrix.v1","from":NEXT,"to":VERSION,"negative_cases":matrix,"success":{"status":"pass","target_smoke":"pass","roundtrip":f"{NEXT}->{VERSION}->{NEXT}","restored_version":NEXT,"restored_verifier":"pass"},"pyc_runtime_drift":0};write(a.home.parent/"rollback-matrix.json",result);print(json.dumps({"status":"pass","negative_cases":len(matrix),"report":str(a.home.parent/"rollback-matrix.json")},sort_keys=True));return 0
if __name__=="__main__":raise SystemExit(main())
