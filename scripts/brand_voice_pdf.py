#!/usr/bin/env python3
"""Execute the closed project activation -> Humanizer -> PDF workflow."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import brand_package
import markdown_contract as contract
import pdf_renderer

ROOT = Path(__file__).resolve().parents[1]
HUMANIZER_VERSION = "5.22.2-brandkit.2"
HUMANIZER_EXPECTED = {
    "runner": "c1f62ed72120bc46e32c3a77555933bdac9dc2b6951ff79803809432c204e0d7",
    "skill": "92e8f4be7ba2d5f634e614a6ad2e6918abbc97d4b1ccb7f5bf330c712f197240",
    "catalog": "9664a1c8c5bbb7700447042babba1c1b78cbf999f349643160cfd517bb6b5f2d",
}


class WorkflowError(Exception):
    pass


class PublicationSnapshot:
    """Hold the complete publication file domain open through the return gate."""
    def __init__(self, roots: list[Path], files: list[Path], links: list[Path]):
        self.roots=[x.resolve(strict=True) for x in roots];self.handles=[];self.records={};self.directories={};self.links={};self.ancestors={}
        def bind_ancestors(path: Path) -> None:
            current=path.parent
            while current != current.parent:
                st=current.lstat()
                target=os.readlink(current) if stat.S_ISLNK(st.st_mode) else None
                if target is not None and current not in {Path("/var"),Path("/tmp")}:
                    raise WorkflowError(f"publication path has a symlink ancestor: {current}")
                self.ancestors[str(current)]=(st.st_dev,st.st_ino,stat.S_IMODE(st.st_mode),target)
                current=current.parent
        for path in [*self.roots,*files,*links]:bind_ancestors(Path(path))
        paths=[]
        for root in self.roots:
            for directory,names,filenames in os.walk(root,followlinks=False):
                here=Path(directory)
                entries=sorted(names+filenames);st=here.lstat();self.directories[str(here)]=(st.st_dev,st.st_ino,stat.S_IMODE(st.st_mode),entries)
                for name in list(names):
                    child=here/name
                    if child.is_symlink():
                        st=child.lstat();self.links[str(child)]=(st.st_dev,st.st_ino,os.readlink(child));names.remove(name)
                for name in filenames:
                    child=here/name
                    if child.is_symlink():
                        st=child.lstat();self.links[str(child)]=(st.st_dev,st.st_ino,os.readlink(child))
                    else:paths.append(child)
        paths.extend(files)
        for link in links:
            st=link.lstat()
            if not stat.S_ISLNK(st.st_mode):raise WorkflowError(f"publication activation is not a symlink: {link}")
            self.links[str(link)]=(st.st_dev,st.st_ino,os.readlink(link))
        for path in sorted(set(paths),key=lambda x:str(x)):
            if path.is_symlink() or not path.is_file():raise WorkflowError(f"publication snapshot rejects non-regular file: {path}")
            fd=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0));handle=os.fdopen(fd,"rb",closefd=True);data=handle.read();handle.seek(0);st=os.fstat(fd)
            self.handles.append(handle);self.records[str(path)]=(st.st_dev,st.st_ino,stat.S_IMODE(st.st_mode),st.st_size,sha(data))
    def verify(self)->None:
        for path,expected in self.ancestors.items():
            try:
                st=Path(path).lstat();target=os.readlink(path) if stat.S_ISLNK(st.st_mode) else None
                actual=(st.st_dev,st.st_ino,stat.S_IMODE(st.st_mode),target)
            except OSError as exc:raise WorkflowError(f"publication ancestor disappeared after gate: {path}") from exc
            if actual!=expected:raise WorkflowError(f"publication ancestor topology changed after gate: {path}")
        for directory,expected in self.directories.items():
            try:
                st=Path(directory).lstat();actual=(st.st_dev,st.st_ino,stat.S_IMODE(st.st_mode),sorted(os.listdir(directory)))
            except OSError as exc:raise WorkflowError(f"publication directory disappeared after gate: {directory}") from exc
            if stat.S_ISLNK(st.st_mode) or actual!=expected:raise WorkflowError(f"publication directory domain changed after gate: {directory}")
        for path,expected in self.links.items():
            try:st=Path(path).lstat();actual=(st.st_dev,st.st_ino,os.readlink(path))
            except OSError as exc:raise WorkflowError(f"publication activation link disappeared after gate: {path}") from exc
            if actual!=expected:raise WorkflowError(f"publication activation link changed after gate: {path}")
        for handle,(path,expected) in zip(self.handles,self.records.items()):
            try:st=os.stat(path,follow_symlinks=False);handle.seek(0);actual=(st.st_dev,st.st_ino,stat.S_IMODE(st.st_mode),st.st_size,sha(handle.read()))
            except OSError as exc:raise WorkflowError(f"publication file disappeared after gate: {path}") from exc
            if actual!=expected:raise WorkflowError(f"publication file identity/bytes changed after gate: {path}")
    def close(self)->None:
        for handle in self.handles:handle.close()
    def commit_inventory(self, marker: Path | None=None) -> dict[str,Any]:
        """Canonical whole-domain identity, including modes, topology and roots."""
        directories={path:{"device":v[0],"inode":v[1],"mode":format(v[2],"04o"),"entries":list(v[3])} for path,v in self.directories.items()}
        if marker is not None and str(marker.parent) in directories and marker.name not in directories[str(marker.parent)]["entries"]:
            directories[str(marker.parent)]["entries"]=sorted([*directories[str(marker.parent)]["entries"],marker.name])
        records={path:{"device":v[0],"inode":v[1],"mode":format(v[2],"04o"),"bytes":v[3],"sha256":v[4]} for path,v in self.records.items() if marker is None or Path(path)!=marker}
        links={path:{"device":v[0],"inode":v[1],"target":v[2]} for path,v in self.links.items()}
        ancestors={path:{"device":v[0],"inode":v[1],"mode":format(v[2],"04o"),"target":v[3]} for path,v in self.ancestors.items()}
        if marker is not None:
            output_root=marker.parent
            for path,row in records.items():
                if Path(path)==output_root or output_root in Path(path).parents:row["mode"]="0400"
            for path,row in directories.items():
                if Path(path)==output_root or output_root in Path(path).parents:row["mode"]="0500"
        return {"schema_version":"brand-voice-pdf-commit-domain.v1","roots":[str(x) for x in self.roots],"files":records,"directories":directories,"links":links,"ancestors":ancestors}


def publication_snapshot(preflight: dict[str,Any], run_dir: Path, args: argparse.Namespace) -> PublicationSnapshot:
    root=preflight["root"]
    state_link=root/".brandkit/current"
    agent_home=Path(os.environ.get("BRANDKIT_HOME",str(Path.home())))
    skill_link=agent_home/".agents/skills/brand-voice-pdf"
    install=verify_global_installation()
    state_root=root/".brandkit"/preflight["state_target"] if "state_target" in preflight else state_link.resolve(strict=True)
    files=[args.workflow_lock,args.owner_approval,args.owner_key,args.issuer_key,preflight["brand_path"],preflight["dtype_path"],ROOT/"renderer-trust.v1.json",ROOT/"toolchain/pdf-renderer.production.v1.lock.json",install["activation"],install["manifest"]]
    roots=[run_dir,state_root,preflight.get("brand_root",Path(preflight["brand_path"]).parent),preflight.get("release",humanizer_release()[0]),install["skill_release"],install["runtime"]]
    links=[state_link,skill_link];links.extend(path for path in (root/".brandkit/project.json",root/".brandkit/skill-lock.json") if os.path.lexists(path))
    return PublicationSnapshot(roots,[Path(x) for x in files],links)


def _security_harness_boundary(name: str) -> None:
    """Deterministic external synchronisation used only by the committed attack harness."""
    directory=os.environ.get("BRANDKIT_SECURITY_HARNESS_BOUNDARY")
    if not directory:return
    root=Path(directory)
    if root.is_symlink() or not root.is_dir():raise WorkflowError("security harness boundary directory is invalid")
    ready=root/f"{name}.ready";resume=root/f"{name}.resume"
    fd=os.open(ready,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,"w") as stream:stream.write(name+"\n");stream.flush();os.fsync(stream.fileno())
    import time
    for _ in range(1000):
        if resume.is_file() and not resume.is_symlink():return
        time.sleep(.01)
    raise WorkflowError("security harness boundary timed out")


def atomic_publication_commit(preflight: dict[str,Any],run_dir:Path,args:argparse.Namespace,gate,run_json:Path,expected_run:dict[str,Any]) -> dict[str,Any]:
    """Seal the final validated domain with one exclusive content-addressed marker."""
    marker=run_dir/"output/publication-commit.json"
    if marker.exists() or marker.is_symlink():
        existing=json.loads(contract.stable_read(marker,run_dir,"publication commit").decode("utf-8"));seal=publication_snapshot(preflight,run_dir,args)
        try:
            gate();verify_signed_run(run_json,expected_run,args.issuer_key,run_dir);_security_harness_boundary("final-atomic-commit");seal.verify();inventory=seal.commit_inventory(marker)
            if existing!={"schema_version":"brand-voice-pdf-publication-commit.v1","commit_sha256":sha(canonical(inventory)),"domain":inventory}:raise WorkflowError("publication commit-domain inventory differs")
            return existing
        finally:seal.close()
    seal=publication_snapshot(preflight,run_dir,args)
    try:
        gate();verify_signed_run(run_json,expected_run,args.issuer_key,run_dir)
        _security_harness_boundary("final-atomic-commit")
        # The snapshot was opened before the boundary: a synchronized mutation
        # is rejected here.  The marker then records that exact open domain.
        seal.verify();inventory=seal.commit_inventory(marker);payload={"schema_version":"brand-voice-pdf-publication-commit.v1","commit_sha256":sha(canonical(inventory)),"domain":inventory}
        data=(json.dumps(payload,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode();fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400)
        with os.fdopen(fd,"wb") as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        return payload
    finally:seal.close()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def verify_global_installation() -> dict[str,Path]:
    """Verify the signed immutable release/runtime before every workflow start."""
    home=Path(os.environ.get("BRANDKIT_HOME",str(Path.home()))).resolve(strict=True)
    version="1.2.28";state=home/".agents/activation-state";activation=state/"brand-voice-pdf.json";manifest=state/f"brand-voice-pdf-{version}.manifest.json"
    verifier=home/f".agents/runtime-store/brandkit-pdf-renderer/{version}/toolchain/pdf-renderer/verify-installation"
    key=home/".agents/keys/brand-voice-pdf.key"
    if not verifier.is_file():raise WorkflowError("brand-voice-pdf 1.2.28 immutable verifier is not installed")
    completed=subprocess.run([str(verifier),str(home),str(key),version],capture_output=True,text=True,env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1"})
    if completed.returncode:raise WorkflowError(f"global renderer installation verification failed: {completed.stderr.strip()}")
    return {"activation":activation,"manifest":manifest,"skill_release":home/f".agents/skill-store/brand-voice-pdf/{version}","runtime":home/f".agents/runtime-store/brandkit-pdf-renderer/{version}"}


def humanizer_release() -> tuple[Path, Path, str]:
    active = Path.home() / ".agents/skills/humanizer-de"
    release = active.resolve(strict=True)
    expected = Path.home() / f".agents/skill-store/humanizer-de/{HUMANIZER_VERSION}"
    if release != expected.resolve(strict=True):
        raise WorkflowError(f"humanizer-de must resolve exactly to {expected}; run toolchain/humanizer-de/install-from-lock")
    files = {"runner": release/"scripts/humanizer_two_pass.py", "skill": release/"SKILL.md", "catalog": release/"references/patterns.md"}
    actual = {name: sha(path.read_bytes()) for name, path in files.items()}
    if actual != HUMANIZER_EXPECTED:
        raise WorkflowError("installed humanizer-de release differs from the reviewed runtime hashes")
    python = release/".venv/bin/python"
    if not python.is_file(): raise WorkflowError("humanizer-de pinned venv is missing")
    attestation = sha(canonical({"version":HUMANIZER_VERSION,"files":actual}))
    return release, python, attestation


def workflow_preflight(project: Path, brand_id: str | None, provider: str, owner_key: Path) -> dict[str, Any]:
    """Resolve every executable input without creating a directory or file."""
    verify_global_installation()
    if provider != "claude":
        raise WorkflowError("humanizer-de provider support for this workflow is exactly: claude")
    root = brand_package.git_root(project)
    target = brand_package.current_state_target(root)
    activation, activation_lock, activation_bytes, activation_lock_bytes = brand_package.read_state(root, target)
    brand, brand_root, brand_content = brand_package.validate_bound_state(root, activation, activation_lock)
    if activation_lock["engine"]["version"] != "1.0.0":
        raise WorkflowError("activation engine differs from workflow engine 1.0.0")
    if brand_id is not None and brand_id != brand["brand_id"]:
        raise WorkflowError("explicit brand differs from activation")
    selected = [x for x in brand["document_types"] if not activation["overrides"].get("document_type") or x["id"] == activation["overrides"]["document_type"]]
    if len(selected) != 1:
        raise WorkflowError("project activation must resolve exactly one document type")
    dtype_path = brand_root / selected[0]["contract"]
    dtype = json.loads(dtype_path.read_text(encoding="utf-8"))
    if sha(dtype_path.read_bytes()) != selected[0]["contract_sha256"]:
        raise WorkflowError("activated document type hash differs from brand manifest")
    brand_path = brand_root / "brand-package.json"
    release, python, release_attestation = humanizer_release()
    trust_path = ROOT / "renderer-trust.v1.json"
    trust = contract.validate_production_renderer_trust()
    renderer_lock = ROOT / trust["engine"]["lock_path"]
    value = {
        "schema_version": "brand-voice-workflow-lock.v2",
        "activation": {
            "project_sha256": sha(activation_bytes), "lock_sha256": sha(activation_lock_bytes),
            "state_sha256": brand_package.state_identity(activation_bytes, activation_lock_bytes),
            "project_utf8_base64": base64.b64encode(activation_bytes).decode("ascii"),
            "lock_utf8_base64": base64.b64encode(activation_lock_bytes).decode("ascii"),
        },
        "brand": {"id": brand["brand_id"], "version": brand["version"], "manifest_sha256": sha(brand_path.read_bytes()), "content_sha256": brand_content},
        "humanizer": {"version": HUMANIZER_VERSION, "release_attestation_sha256": release_attestation, "tool_hashes": HUMANIZER_EXPECTED, "provider_capability": {"provider": "claude", "allowed_states": [
            {"mode":"audit-only","outcome":"no-confirmed-change","model_calls":1,"confirmed_edit_count":0,"rejected_candidates":"evidence-required","rewrite_present":False},
            {"mode":"audit+rewrite","outcome":"changed","model_calls":2,"minimum_edit_count":1,"rewrite_present":True}
        ]}},
        "renderer": {"name": trust["engine"]["name"], "version": trust["engine"]["version"], "lock_sha256": sha(renderer_lock.read_bytes()), "trust_sha256": sha(trust_path.read_bytes())},
        "markdown_contract": {"sha256": sha((ROOT / "markdown-contract.v2.json").read_bytes())},
        "document_type": {"id": dtype["id"], "version": dtype["version"], "sha256": sha(dtype_path.read_bytes())},
        "owner_approval_key_sha256": sha(contract._key_bytes(owner_key)),
    }
    contract.validate_schema(value, "brand-voice-workflow-lock.v2.schema.json", "Brand Voice workflow lock")
    return {"value": value, "root": root, "state_target":target, "activation": activation, "activation_lock": activation_lock,
            "activation_bytes": activation_bytes, "activation_lock_bytes": activation_lock_bytes,
            "brand": brand, "brand_root": brand_root, "brand_path": brand_path, "brand_content": brand_content,
            "dtype": dtype, "dtype_path": dtype_path, "release": release, "python": python}


def verify_workflow_lock(preflight: dict[str, Any], lock_path: Path) -> dict[str, Any]:
    root = preflight["root"].resolve(strict=True)
    if lock_path.is_symlink() or not lock_path.is_file():
        raise WorkflowError("workflow lock must be a regular pre-existing file")
    resolved = lock_path.resolve(strict=True)
    if root != resolved and root not in resolved.parents:
        raise WorkflowError("workflow lock must be inside the exact Git project")
    raw = contract.stable_read(lock_path,root,"workflow lock")
    try:
        supplied = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"workflow lock is not strict UTF-8 JSON: {exc}") from exc
    contract.validate_schema(supplied, "brand-voice-workflow-lock.v2.schema.json", "Brand Voice workflow lock")
    if supplied != preflight["value"] or raw != json.dumps(supplied, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n":
        raise WorkflowError("workflow lock differs from the re-resolved executable inputs")
    return supplied


def expected_binding(preflight: dict[str, Any], audit_path: Path, owner_approval: Path) -> dict[str, Any]:
    """Construct the verifier expectation from re-hashed trusted inputs, never a render result."""
    lock = preflight["value"]
    return {
        "project_identity_sha256": sha(str(preflight["root"].resolve()).encode()),
        "activation_sha256": lock["activation"]["state_sha256"],
        "brand": dict(lock["brand"]),
        "document_type": dict(lock["document_type"]),
        "markdown_contract_sha256": lock["markdown_contract"]["sha256"],
        "humanizer_audit_sha256": sha(contract.stable_read(audit_path,audit_path.parents[1],"Humanizer audit")),
        "owner_approval_sha256": sha(contract.stable_read(owner_approval,owner_approval.parent,"owner approval")),
    }


def verify_owner_approval(preflight: dict[str, Any], approval_path: Path, owner_key: Path, workflow_lock: Path,
                          run_id: str, final_ast: dict[str, Any]) -> None:
    approval_bytes=contract.stable_read(approval_path,approval_path.parent,"owner approval")
    approval=json.loads(approval_bytes.decode("utf-8"));contract.validate_schema(approval,"owner-approval.v1.schema.json","owner approval")
    signed={k:approval[k] for k in ("schema_version","issuer","payload")}
    expected=hmac.new(contract._key_bytes(owner_key),canonical(signed),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,approval["hmac_sha256"]):raise WorkflowError("owner approval signature failed")
    if sha(contract._key_bytes(owner_key))!=preflight["value"]["owner_approval_key_sha256"]:raise WorkflowError("owner approval key differs from workflow lock")
    payload=approval["payload"]
    required={"decision":"release","brand_id":preflight["brand"]["brand_id"],"document_type_id":preflight["dtype"]["id"],"workflow_lock_sha256":sha(contract.stable_read(workflow_lock,preflight["root"],"workflow lock")),"project_identity_sha256":sha(str(preflight["root"].resolve()).encode()),"run_id":run_id,"final_sha256":final_ast["source_sha256"]}
    if any(payload.get(k)!=v for k,v in required.items()):raise WorkflowError("owner approval differs from draft/brand/type/workflow lock")


def signed_run(summary: dict[str, Any], result_bytes: bytes, final_bytes: bytes,
               workflow_lock_bytes: bytes, activation_sha256: str, key: Path) -> dict[str, Any]:
    payload={"summary":summary,"render_result_sha256":sha(result_bytes),"final_sha256":sha(final_bytes),
             "workflow_lock_sha256":sha(workflow_lock_bytes),"activation_sha256":activation_sha256}
    signed={"schema_version":"brand-voice-pdf-run.v2","issuer":"brandkit-production-renderer-v1","payload":payload}
    return {**signed,"hmac_sha256":hmac.new(contract._key_bytes(key),canonical(signed),hashlib.sha256).hexdigest()}


def verify_signed_run(path: Path, expected: dict[str, Any], key: Path, run_root: Path) -> dict[str, Any]:
    raw=contract.stable_read(path,run_root,"signed run")
    try: envelope=json.loads(raw.decode("utf-8"))
    except (UnicodeError,json.JSONDecodeError) as exc:raise WorkflowError(f"signed run is not strict JSON: {exc}") from exc
    if not isinstance(envelope,dict) or set(envelope)!={"schema_version","issuer","payload","hmac_sha256"}:
        raise WorkflowError("signed run envelope is not closed")
    signed={k:envelope[k] for k in ("schema_version","issuer","payload")}
    if (envelope["schema_version"]!="brand-voice-pdf-run.v2" or envelope["issuer"]!="brandkit-production-renderer-v1"
            or envelope["payload"]!=expected["payload"] or not hmac.compare_digest(envelope.get("hmac_sha256",""),hmac.new(contract._key_bytes(key),canonical(signed),hashlib.sha256).hexdigest())):
        raise WorkflowError("signed run binding/signature failed")
    return envelope["payload"]["summary"]


def mutable_segments(ast: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = sorted((x for x in ast["humanizer_segments"]["mutable"] if x["text"].strip()), key=lambda x: x["segment_id"])
    if not candidates: raise WorkflowError("draft has no mutable prose segment suitable for the Humanizer pass")
    if len({x["segment_id"] for x in candidates}) != len(candidates): raise WorkflowError("draft has duplicate mutable segment IDs")
    return candidates


def make_audit(baseline: dict[str, Any], final_ast: dict[str, Any], key: Path, run_root: Path,
               raw_dirs: list[tuple[dict[str, Any], Path, str]], release: Path, release_attestation: str) -> dict[str, Any]:
    base_required = ["original.md","preflight.json","audit-call.json","audit-ledger.json","confirmed-ledger.json","edits.json","result.md","report.json","changes.diff","evidence-ledger.json","evidence-report.json"]
    artifacts=[]; operations=[]
    for segment, raw_dir, replacement in raw_dirs:
        report=json.loads((raw_dir/"report.json").read_text(encoding="utf-8")) if (raw_dir/"report.json").is_file() else {}
        changed=replacement!=segment["text"]
        edit_count=report.get("edit_count")
        candidate_count=report.get("candidate_count");discarded=report.get("discarded_candidate_count")
        confirmed=json.loads((raw_dir/"confirmed-ledger.json").read_text(encoding="utf-8")) if (raw_dir/"confirmed-ledger.json").is_file() else {}
        rejected=confirmed.get("discarded_candidates",[])
        rejection_closed=(isinstance(candidate_count,int) and candidate_count>=0 and discarded==candidate_count and report.get("confirmed_candidate_count")==0 and len(rejected)==candidate_count and all(isinstance(x,dict) and x.get("id") and x.get("reason") for x in rejected))
        evidence=json.loads((raw_dir/"evidence-report.json").read_text(encoding="utf-8")) if (raw_dir/"evidence-report.json").is_file() else {}
        no_change=not changed and edit_count==0 and report.get("model_calls")==1 and report.get("prompt_sha256",{}).get("rewrite") is None and rejection_closed and evidence.get("ok") is True and not evidence.get("findings")
        required=base_required+(["rewrite-call.json"] if changed else [])
        missing = [name for name in required if not (raw_dir/name).is_file()]
        if missing: raise WorkflowError(f"Humanizer raw evidence is incomplete for {segment['segment_id']}: {missing}")
        edited=changed and report.get("model_calls")==2 and isinstance(edit_count,int) and edit_count>0
        if report.get("accepted") is not True or report.get("provider") != "claude" or not (edited or no_change) or report.get("protected_violations") or report.get("evidence_blockers"):
            raise WorkflowError(f"Humanizer report is not a closed accepted Claude two-pass result for {segment['segment_id']}")
        for name in required:
            path=raw_dir/name; artifacts.append({"path":path.relative_to(run_root).as_posix(),"sha256":sha(path.read_bytes())})
        result_path=(raw_dir/"result.md").relative_to(run_root).as_posix()
        state="audit+rewrite/changed" if changed else "audit-only/no-change"
        operations.append({"segment_id":segment["segment_id"],"old_hash":segment["old_hash"],"replacement_sha256":sha(replacement.encode("utf-8")),"result_path":result_path,"report_path":(raw_dir/"report.json").relative_to(run_root).as_posix(),"changed":changed,"provider_state":state})
    payload={"baseline_sha256":sha(canonical(baseline)),"final_sha256":final_ast["source_sha256"],"provider":"claude","humanizer":{"version":HUMANIZER_VERSION,"release_path":str(release),"release_sha256":release_attestation},"raw_artifacts":artifacts,"operations":operations,"protected_stable":True,"ast_skeleton_stable":True}
    signed={"schema_version":"humanizer-audit.v1","issuer":"brandkit-production-renderer-v1","payload":payload}
    envelope={**signed,"hmac_sha256":hmac.new(contract._key_bytes(key),canonical(signed),hashlib.sha256).hexdigest()}
    contract.validate_schema(envelope,"humanizer-audit.v1.schema.json","Humanizer audit")
    return envelope


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    """Create the reviewable Markdown/evidence set.  This phase never creates a PDF."""
    if args.provider != "claude": raise WorkflowError("humanizer-de provider support for this workflow is exactly: claude")
    preflight = workflow_preflight(args.project, args.brand_id, args.provider, args.owner_key)
    verify_workflow_lock(preflight, args.workflow_lock)
    external_ast=contract.run_validate(args.draft,preflight["dtype_path"],args.asset_root,False,None)
    resolution=brand_package.resolve(args.project,None,args.brand_id,"1.0.0",args.run_id)
    root=preflight["root"]; brand_root=preflight["brand_root"]; brand_path=preflight["brand_path"]
    brand=preflight["brand"]; dtype_path=preflight["dtype_path"]; dtype=preflight["dtype"]
    for path in resolution["roots"].values(): brand_package.safe_dir(root,Path(path).relative_to(root).as_posix())
    run_dir=Path(resolution["roots"]["work"]); output_dir=run_dir/"output"
    resume=False
    if any(run_dir.iterdir()):
        marker=run_dir/"prepare.json"
        if marker.is_file() and not marker.is_symlink():
            prior=json.loads(marker.read_text(encoding="utf-8"))
            if prior.get("draft_sha256")==external_ast["source_sha256"] and prior.get("workflow_lock_sha256")==sha(args.workflow_lock.read_bytes()):
                return prior["summary"]
        draft=contract._safe_run_file(run_dir,"draft.md","resumable draft")
        baseline_path=contract._safe_run_file(run_dir,"humanizer-baseline.json","resumable baseline")
        if sha(contract.stable_read(draft,run_dir,"resumable draft"))!=external_ast["source_sha256"]:raise WorkflowError("partial prepare draft differs; refusing resume")
        resume=True
    else:
        draft=run_dir/"draft.md";draft.write_bytes(args.draft.read_bytes())
    ast=contract.run_validate(draft,dtype_path,args.asset_root,False,None)
    if ast["source_sha256"]!=external_ast["source_sha256"]:raise WorkflowError("draft bytes changed after side-effect-free preflight")
    if resume:
        baseline=json.loads(contract.stable_read(baseline_path,run_dir,"resumable baseline").decode("utf-8"))
    else:
        baseline=contract.create_humanizer_baseline(ast,dtype,resolution["brand"]["content_sha256"],"brandkit-production-renderer-v1",args.issuer_key,f"humanizer-{args.run_id}-00000001","2026-08-26T00:00:00Z",brand_manifest=brand_path)
        baseline_path=run_dir/"humanizer-baseline.json";write_json(baseline_path,baseline)
    release,python=preflight["release"],preflight["python"];release_attestation=preflight["value"]["humanizer"]["release_attestation_sha256"]
    rewritten=[]; raw_runs=[]
    for segment in mutable_segments(ast):
        raw=run_dir/"humanizer-raw"/segment["segment_id"]; raw.parent.mkdir(parents=True,exist_ok=True)
        if resume:
            if raw.is_symlink() or not (raw/"report.json").is_file() or not (raw/"result.md").is_file():raise WorkflowError(f"partial prepare lacks complete reusable evidence for {segment['segment_id']}")
        else:
            mutable_input=run_dir/f"humanizer-input-{segment['segment_id']}.md";mutable_input.write_text(segment["text"]+"\n",encoding="utf-8")
            completed=subprocess.run([str(python),str(release/"scripts/humanizer_two_pass.py"),"--file",str(mutable_input),"--out-dir",str(raw),"--provider","claude","--model",args.model,"--timeout",str(args.timeout)],capture_output=True,text=True)
            (run_dir/f"humanizer-{segment['segment_id']}.stdout").write_text(completed.stdout,encoding="utf-8");(run_dir/f"humanizer-{segment['segment_id']}.stderr").write_text(completed.stderr,encoding="utf-8")
            if completed.returncode: raise WorkflowError(f"humanizer-de failed for {segment['segment_id']}: {completed.stderr.strip()}")
        replacement=(raw/"result.md").read_text(encoding="utf-8").rstrip("\n")
        if not replacement: raise WorkflowError(f"Humanizer deleted transport segment {segment['segment_id']}")
        rewritten.append((segment["segment_id"],segment["old_hash"],replacement));raw_runs.append((segment,raw,replacement))
    final_source,final_ast=contract.rewrite_humanizer(baseline,args.issuer_key,rewritten,args.asset_root,dtype,brand_path,resolution["brand"]["content_sha256"])
    final=run_dir/"final.md";final.write_text(final_source,encoding="utf-8")
    audit=make_audit(baseline,final_ast,args.issuer_key,run_dir,raw_runs,release,release_attestation)
    audit_path=run_dir/"humanizer-audit.json";write_json(audit_path,audit)
    request={"schema_version":"approval-request.v1","payload":{"final_sha256":final_ast["source_sha256"],"brand_id":brand["brand_id"],"document_type_id":dtype["id"],"workflow_lock_sha256":sha(args.workflow_lock.read_bytes()),"project_identity_sha256":sha(str(root.resolve()).encode()),"run_id":args.run_id}}
    contract.validate_schema(request,"approval-request.v1.schema.json","approval request")
    request_path=run_dir/"approval-request.json";write_json(request_path,request)
    summary={"schema_version":"brand-voice-pdf-run.v1","run_id":args.run_id,"project_root":str(root),"brand":resolution["brand"],"document_type":{"id":dtype["id"],"version":dtype["version"]},"draft":str(draft),"baseline":str(baseline_path),"humanizer_audit":str(audit_path),"final":str(final),"approval_request":str(request_path),"status":"awaiting-owner-approval"}
    write_json(run_dir/"prepare.json",{"draft_sha256":external_ast["source_sha256"],"workflow_lock_sha256":sha(args.workflow_lock.read_bytes()),"summary":summary})
    return summary


def publish(args: argparse.Namespace) -> dict[str, Any]:
    """Re-resolve every input, verify a later approval, then render and publish."""
    preflight=workflow_preflight(args.project,args.brand_id,args.provider,args.owner_key)
    verify_workflow_lock(preflight,args.workflow_lock)
    resolution=brand_package.resolve(args.project,None,args.brand_id,"1.0.0",args.run_id)
    root=preflight["root"];run_dir=Path(resolution["roots"]["work"])
    if run_dir.is_symlink() or not run_dir.is_dir():raise WorkflowError("prepared run does not exist as a regular directory")
    final=contract._safe_run_file(run_dir,"final.md","prepared final Markdown")
    baseline_path=contract._safe_run_file(run_dir,"humanizer-baseline.json","Humanizer baseline")
    audit_path=contract._safe_run_file(run_dir,"humanizer-audit.json","Humanizer audit")
    request_path=contract._safe_run_file(run_dir,"approval-request.json","approval request")
    dtype=preflight["dtype"];final_ast=contract.run_validate(final,preflight["dtype_path"],args.asset_root,False,None)
    request=json.loads(contract.stable_read(request_path,run_dir,"approval request").decode("utf-8"));contract.validate_schema(request,"approval-request.v1.schema.json","approval request")
    expected_request={"final_sha256":final_ast["source_sha256"],"brand_id":preflight["brand"]["brand_id"],"document_type_id":dtype["id"],"workflow_lock_sha256":sha(args.workflow_lock.read_bytes()),"project_identity_sha256":sha(str(root.resolve()).encode()),"run_id":args.run_id}
    if request["payload"]!=expected_request:raise WorkflowError("approval request differs from prepared final/run/project/workflow lock")
    verify_owner_approval(preflight,args.owner_approval,args.owner_key,args.workflow_lock,args.run_id,final_ast)
    baseline=json.loads(contract.stable_read(baseline_path,run_dir,"Humanizer baseline").decode("utf-8"))
    output_dir=run_dir/"output"
    output=output_dir/f'{final_ast["frontmatter"]["document_id"]}.pdf';result=output_dir/"render-result.json"
    summary={"schema_version":"brand-voice-pdf-run.v1","run_id":args.run_id,"project_root":str(root),"brand":resolution["brand"],"document_type":{"id":dtype["id"],"version":dtype["version"]},"draft":str(run_dir/"draft.md"),"baseline":str(baseline_path),"humanizer_audit":str(audit_path),"final":str(final),"pdf":str(output),"render_result":str(result),"visual_qa":str(output_dir/"visual-qa.json"),"status":"published"}
    def final_commit_gate(fresh: dict[str, Any], expected: dict[str, Any]) -> None:
        """The last read barrier: run.v2 exists before every protected byte is re-gated."""
        contract.publication_gate(final_ast,result,baseline,args.issuer_key,dtype,args.asset_root,audit_path,fresh["brand_path"],fresh["brand_content"],expected,run_dir,args.owner_approval,args.owner_key,fresh["value"]["owner_approval_key_sha256"],project_root=root,workflow_lock=args.workflow_lock,workflow_lock_value=fresh["value"],final_path=final,baseline_path=baseline_path)
    if output_dir.exists():
        run_json=output_dir/"run.json"
        committed=output_dir/"publication-commit.json"
        if (committed.exists() or committed.is_symlink()) and not (run_json.exists() or run_json.is_symlink()):
            raise WorkflowError("committed publication is missing run.v2; recovery cannot replace a committed domain")
        if run_json.exists() or run_json.is_symlink():
            fresh=workflow_preflight(args.project,args.brand_id,args.provider,args.owner_key)
            verify_workflow_lock(fresh,args.workflow_lock)
            expected=expected_binding(fresh,audit_path,args.owner_approval)
            final_commit_gate(fresh,expected)
            expected_run=signed_run(summary,contract.stable_read(result,output_dir,"render result"),contract.stable_read(final,run_dir,"final Markdown"),contract.stable_read(args.workflow_lock,root,"workflow lock"),fresh["value"]["activation"]["state_sha256"],args.issuer_key)
            verified=verify_signed_run(run_json,expected_run,args.issuer_key,run_dir)
            # A synchronized attacker may replace any class after the first gate.
            # Re-gate the complete domain only after run.v2 has also verified.
            final_commit_gate(fresh,expected)
            verify_signed_run(run_json,expected_run,args.issuer_key,run_dir)
            atomic_publication_commit(fresh,run_dir,args,lambda:final_commit_gate(fresh,expected),run_json,expected_run)
            return verified
        number=1
        while (run_dir/f"failed-output-{number}").exists():number+=1
        quarantine=run_dir/f"failed-output-{number}"
        os.replace(output_dir,quarantine)
    output_dir.mkdir(mode=0o700)
    pdf_renderer.render(final,preflight["dtype_path"],preflight["brand_path"],args.asset_root,output,result,args.issuer_key,output_dir/"renderer-work",root,audit_path,args.mermaid_cas,args.owner_approval)
    # Re-resolve from fresh bytes after rendering.  The gate repeats this check itself.
    fresh=workflow_preflight(args.project,args.brand_id,args.provider,args.owner_key)
    verify_workflow_lock(fresh,args.workflow_lock)
    expected=expected_binding(fresh,audit_path,args.owner_approval)
    final_commit_gate(fresh,expected)
    run_envelope=signed_run(summary,contract.stable_read(result,output_dir,"render result"),contract.stable_read(final,run_dir,"final Markdown"),contract.stable_read(args.workflow_lock,root,"workflow lock"),fresh["value"]["activation"]["state_sha256"],args.issuer_key)
    write_json(output_dir/"run.json",run_envelope)
    verified=verify_signed_run(output_dir/"run.json",run_envelope,args.issuer_key,run_dir)
    final_commit_gate(fresh,expected)
    verify_signed_run(output_dir/"run.json",run_envelope,args.issuer_key,run_dir)
    atomic_publication_commit(fresh,run_dir,args,lambda:final_commit_gate(fresh,expected),output_dir/"run.json",run_envelope)
    for path in sorted(output_dir.rglob("*"),reverse=True):
        if path.is_file():path.chmod(0o400)
        elif path.is_dir():path.chmod(0o500)
    output_dir.chmod(0o500)
    final_seal=publication_snapshot(fresh,run_dir,args)
    try:final_seal.verify()
    finally:final_seal.close()
    return verified


def run(args: argparse.Namespace) -> dict[str, Any]:
    """Programmatic phase dispatcher retained for agent integrations."""
    if args.provider!="claude":raise WorkflowError("humanizer-de provider support for this workflow is exactly: claude")
    command=getattr(args,"command",None)
    if command=="prepare":return prepare(args)
    if command=="publish":return publish(args)
    raise WorkflowError("explicit prepare or publish phase is required")


def main(argv: list[str]|None=None)->int:
    p=argparse.ArgumentParser();p.add_argument("command",nargs="?",choices=("prepare","publish"));p.add_argument("--project",type=Path,required=True);p.add_argument("--draft",type=Path);p.add_argument("--asset-root",type=Path);p.add_argument("--run-id");p.add_argument("--brand-id");p.add_argument("--provider",default="claude");p.add_argument("--model",default="haiku");p.add_argument("--timeout",type=int,default=600);p.add_argument("--issuer-key",type=Path);p.add_argument("--mermaid-cas",type=Path);p.add_argument("--workflow-lock",type=Path);p.add_argument("--owner-approval",type=Path);p.add_argument("--owner-key",type=Path,required=True);p.add_argument("--print-workflow-lock",action="store_true");a=p.parse_args(argv)
    if a.print_workflow_lock:
        try: print(json.dumps(workflow_preflight(a.project,a.brand_id,a.provider,a.owner_key)["value"],ensure_ascii=False,sort_keys=True,indent=2));return 0
        except (WorkflowError,brand_package.ContractError,contract.ContractError,OSError,ValueError) as exc:print(f"brand-voice-pdf: ERROR: {exc}",file=sys.stderr);return 1
    required=("draft","asset_root","run_id","issuer_key","workflow_lock") if a.command=="prepare" else ("asset_root","run_id","issuer_key","mermaid_cas","workflow_lock","owner_approval")
    missing=[name for name in required if getattr(a,name) is None]
    if a.command is None or missing:print(f"brand-voice-pdf: ERROR: {a.command or 'command'} requires {', '.join(missing) if missing else 'prepare or publish'}",file=sys.stderr);return 2
    try: print(json.dumps(prepare(a) if a.command=="prepare" else publish(a),ensure_ascii=False,sort_keys=True,indent=2));return 0
    except (WorkflowError,brand_package.ContractError,contract.ContractError,pdf_renderer.RenderError,OSError,ValueError,subprocess.SubprocessError) as exc:print(f"brand-voice-pdf: ERROR: {exc}",file=sys.stderr);return 1


if __name__=="__main__":raise SystemExit(main())
