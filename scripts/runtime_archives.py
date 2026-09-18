#!/usr/bin/env python3
"""Deterministic relocatable component runtime archives (H4)."""
from __future__ import annotations
import argparse, concurrent.futures, hashlib, io, json, os, pathlib, stat, subprocess, tarfile
import shared_toolchain as st

TOKEN = pathlib.Path("/__BRANDKIT_RUNTIME_ROOT__")
COMPONENTS = ("humanizer", "pdf-home", "mermaid", "graphviz")

def canonical(value): return json.dumps(value,sort_keys=True,separators=(",",":")).encode()
def bindings(lock, name):
    if name=="humanizer": sources=[lock["components"]["humanizer"],lock["artifacts"]["humanizer_source"]];packages=[lock["artifacts"]["humanizer_wheelhouse"]]
    elif name=="pdf-home": sources=[lock["components"]["pdf_renderer"]];packages=[lock["artifacts"]["pdf_wheelhouse"],lock["artifacts"]["pdf_issuer_key"]]
    elif name=="mermaid": sources=[lock["components"]["mermaid"]];packages=[lock["artifacts"]["mermaid_bundle"]]
    else: sources=[lock["components"]["graphviz"]];packages=[lock["artifacts"]["graphviz_bundle"]]
    transforms=[lock["commands"]["implementation"],lock["commands"]["runtime_builder"],lock["transforms"],
                hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()]
    return {"source_digest":hashlib.sha256(canonical(sources)).hexdigest(),
      "package_digest":hashlib.sha256(canonical(packages)).hexdigest(),
      "transform_digest":hashlib.sha256(canonical(transforms)).hexdigest()}

def _canonical_runtime_tar(entries, payloads, archive_format):
    """Canonical USTAR, or strict PAX where Chrome/npm paths exceed USTAR."""
    output=io.BytesIO();manifest=[]
    tar_format={"ustar":tarfile.USTAR_FORMAT,"pax":tarfile.PAX_FORMAT}[archive_format]
    with tarfile.open(fileobj=output,mode="w",format=tar_format) as archive:
      for entry in entries:
       name=entry["path"]+("/" if entry["type"]=="directory" else "")
       info=tarfile.TarInfo(name);info.uid=info.gid=0;info.uname=info.gname="root";info.mtime=0
       info.mode=int(entry.get("mode","0777"),8)
       if entry["type"]=="file":
        data=payloads[entry["path"]];info.type=tarfile.REGTYPE;info.size=len(data);archive.addfile(info,io.BytesIO(data))
        manifest.append({"path":entry["path"],"type":"file","mode":entry["mode"],"size":len(data),"sha256":hashlib.sha256(data).hexdigest()})
       elif entry["type"]=="directory":
        info.type=tarfile.DIRTYPE;info.size=0;archive.addfile(info);manifest.append({"path":entry["path"],"type":"directory","mode":entry["mode"]})
       else:
        info.type=tarfile.SYMTYPE;info.size=0;info.linkname=entry["target"];archive.addfile(info);manifest.append({"path":entry["path"],"type":"symlink","target":entry["target"]})
    return output.getvalue(),manifest

def _archive_format(entries):
    for entry in entries:
      probe=tarfile.TarInfo(entry["path"]+("/" if entry["type"]=="directory" else ""))
      try: probe.tobuf(tarfile.USTAR_FORMAT)
      except ValueError: return "pax"
    return "ustar"

def _validate_link_target(member_path:str, target:str):
    """Return the logical target and reject non-canonical or escaping links."""
    pure=pathlib.PurePosixPath(target)
    if (not target or pure.is_absolute() or target!=pure.as_posix() or
        "\\" in target or "//" in target):
      raise st.ToolchainError("runtime archive symlink target differs")
    parts=list(pathlib.PurePosixPath(member_path).parent.parts);saw_name=False
    for part in pure.parts:
      if part=="..":
       if saw_name or not parts: raise st.ToolchainError("runtime archive symlink target escapes")
       parts.pop()
      elif part==".":
       if target!=".": raise st.ToolchainError("runtime archive symlink target differs")
      else: parts.append(part);saw_name=True
    return pathlib.PurePosixPath(*parts)

def _validate_archive_structure(entries):
    """Validate the complete member graph before materialization starts."""
    kinds={}
    for entry in entries:
      name=entry["path"]
      if name in kinds: raise st.ToolchainError("runtime archive duplicate member")
      kinds[name]=entry["type"]
      if entry["type"]=="symlink": _validate_link_target(name,entry["target"])
    for name in kinds:
      parts=pathlib.PurePosixPath(name).parts
      for length in range(1,len(parts)):
       ancestor=pathlib.PurePosixPath(*parts[:length]).as_posix()
       kind=kinds.get(ancestor)
       if kind is None: raise st.ToolchainError("runtime archive member has undeclared directory ancestor")
       if kind=="symlink": raise st.ToolchainError("runtime archive member has symlink ancestor")
       if kind!="directory": raise st.ToolchainError("runtime archive member has non-directory ancestor")

def _open_absolute_directory(path:pathlib.Path):
    """Open an absolute directory without following a symlink component."""
    if not path.is_absolute(): raise st.ToolchainError("runtime destination parent is not absolute")
    flags=os.O_RDONLY|os.O_DIRECTORY|getattr(os,"O_NOFOLLOW",0)
    descriptor=os.open("/",flags)
    try:
      for part in path.parts[1:]:
       following=os.open(part,flags,dir_fd=descriptor);os.close(descriptor);descriptor=following
      return descriptor
    except BaseException:
      os.close(descriptor);raise

def _open_relative_directory(root_fd:int, relative:pathlib.PurePosixPath):
    flags=os.O_RDONLY|os.O_DIRECTORY|getattr(os,"O_NOFOLLOW",0)
    descriptor=os.dup(root_fd)
    try:
      for part in (() if relative==pathlib.PurePosixPath(".") else relative.parts):
       following=os.open(part,flags,dir_fd=descriptor);os.close(descriptor);descriptor=following
      return descriptor
    except BaseException:
      os.close(descriptor);raise

def _create_fresh_root(destination:pathlib.Path):
    parent_fd=_open_absolute_directory(destination.parent)
    try:
      os.mkdir(destination.name,0o700,dir_fd=parent_fd)
      return os.open(destination.name,os.O_RDONLY|os.O_DIRECTORY|getattr(os,"O_NOFOLLOW",0),dir_fd=parent_fd)
    finally: os.close(parent_fd)

def _write_file_at(root_fd:int, relative:pathlib.PurePosixPath, data:bytes, mode:int):
    parent_fd=_open_relative_directory(root_fd,relative.parent)
    try:
      descriptor=os.open(relative.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),mode,dir_fd=parent_fd)
      try:
       view=memoryview(data)
       while view: view=view[os.write(descriptor,view):]
       os.fchmod(descriptor,mode)
      finally: os.close(descriptor)
    finally: os.close(parent_fd)

def tree_archive(root:pathlib.Path, *, exclude=()):
    entries=[];payloads={}; excluded=set(exclude)
    paths=sorted(root.rglob("*"),key=lambda p:p.relative_to(root).as_posix())
    regular=[]
    for path in paths:
      rel=path.relative_to(root).as_posix()
      if rel in excluded: continue
      info=path.lstat()
      if path.is_symlink(): entries.append({"path":rel,"type":"symlink","target":os.readlink(path)})
      elif path.is_dir(): entries.append({"path":rel,"type":"directory","mode":format(stat.S_IMODE(info.st_mode),"04o")})
      elif path.is_file():
        if info.st_nlink!=1: raise st.ToolchainError("runtime archive contains hardlink")
        entries.append({"path":rel,"type":"file","mode":format(stat.S_IMODE(info.st_mode),"04o")});regular.append((rel,path))
      else: raise st.ToolchainError("runtime archive contains special file")
    # External-volume latency dominates npm trees. Parallel reads do not alter
    # ordering: the canonical writer still consumes the sorted entry sequence.
    def read(item): return item[0],item[1].read_bytes()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16,max(1,len(regular)))) as pool:
      payloads.update(pool.map(read,regular))
    archive_format=_archive_format(entries)
    raw,manifest=_canonical_runtime_tar(entries,payloads,archive_format)
    return raw,manifest

def inspect_archive(path:pathlib.Path, record:dict):
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record["archive_sha256"] or len(raw)!=record["archive_size"]: raise st.ToolchainError("runtime archive digest/size differs")
    entries=[];payloads={}
    try:
      with tarfile.open(fileobj=io.BytesIO(raw),mode="r:") as archive:
       for member in archive.getmembers():
        original=member.name;name=original[:-1] if member.isdir() and original.endswith("/") else original;pure=pathlib.PurePosixPath(name)
        if not name or name!=pure.as_posix() or pure.is_absolute() or ".." in pure.parts or "\\" in name or "//" in original or "/./" in original or member.uid or member.gid or member.uname!="root" or member.gname!="root" or member.mtime: raise st.ToolchainError("runtime archive member differs")
        if member.isfile():
         stream=archive.extractfile(member);data=stream.read() if stream else None
         if data is None: raise st.ToolchainError("runtime archive payload absent")
         payloads[name]=data;entries.append({"path":name,"type":"file","mode":format(member.mode,"04o"),"size":len(data),"sha256":hashlib.sha256(data).hexdigest()})
        elif member.isdir():entries.append({"path":name,"type":"directory","mode":format(member.mode,"04o")})
        elif member.issym():entries.append({"path":name,"type":"symlink","target":member.linkname})
        else:raise st.ToolchainError("runtime archive special member")
    except tarfile.TarError as exc:raise st.ToolchainError("runtime archive invalid") from exc
    _validate_archive_structure(entries)
    if record.get("format") not in {"ustar","pax"}: raise st.ToolchainError("runtime archive format differs")
    canonical_raw,manifest=_canonical_runtime_tar(entries,payloads,record["format"])
    if canonical_raw!=raw or hashlib.sha256(canonical(manifest)).hexdigest()!=record["manifest_sha256"] or len(manifest)!=record["entries"] or sum(x["type"]=="file" for x in manifest)!=record["files"] or sum(x.get("size",0) for x in manifest)!=record["bytes"]:raise st.ToolchainError("runtime archive inventory differs")
    token = str(TOKEN/record["component"]) if record["component"] in {"humanizer","pdf-home"} else None
    if record.get("relocation_token") != token:
      raise st.ToolchainError("runtime archive relocation binding differs")
    if token is not None and not any(token.encode() in value for value in payloads.values()):
      raise st.ToolchainError("runtime archive relocation token is absent")
    return entries,payloads

def materialize(name,path,record,destination,final_root,key=None):
    if destination.exists() or destination.is_symlink():raise st.ToolchainError("runtime destination is not fresh")
    entries,payloads=inspect_archive(path,record);root_fd=None
    try:
     root_fd=_create_fresh_root(destination)
     directories=sorted((entry for entry in entries if entry["type"]=="directory"),key=lambda entry:(entry["path"].count("/"),entry["path"]))
     files=(entry for entry in entries if entry["type"]=="file")
     links=(entry for entry in entries if entry["type"]=="symlink")
     directory_modes=[]
     for entry in directories:
      relative=pathlib.PurePosixPath(entry["path"]);parent_fd=_open_relative_directory(root_fd,relative.parent)
      try: os.mkdir(relative.name,0o700,dir_fd=parent_fd)
      finally: os.close(parent_fd)
      directory_modes.append((relative,int(entry["mode"],8)))
     for entry in files:
      relative=pathlib.PurePosixPath(entry["path"])
      _write_file_at(root_fd,relative,payloads[entry["path"]],int(entry["mode"],8))
     if name=="pdf-home":
      if key is None:raise st.ToolchainError("PDF runtime key absent")
      key_info=key.lstat()
      if not stat.S_ISREG(key_info.st_mode) or key_info.st_nlink!=1: raise st.ToolchainError("PDF runtime key is unsafe")
      _write_file_at(root_fd,pathlib.PurePosixPath(".agents/keys/brand-voice-pdf.key"),key.read_bytes(),0o600)
      st.relocate_pdf_install(destination,TOKEN/name,final_root)
     elif name=="humanizer":st.relocate_humanizer_install(destination,TOKEN/name,final_root)
     # Links are deliberately last: no subsequent payload write can traverse
     # an archive-created symlink, and every parent is opened O_NOFOLLOW.
     for entry in links:
      relative=pathlib.PurePosixPath(entry["path"]);parent_fd=_open_relative_directory(root_fd,relative.parent)
      try: os.symlink(entry["target"],relative.name,dir_fd=parent_fd)
      finally: os.close(parent_fd)
     for relative,mode in reversed(directory_modes):
      descriptor=_open_relative_directory(root_fd,relative)
      try: os.fchmod(descriptor,mode)
      finally: os.close(descriptor)
     os.fchmod(root_fd,0o755)
    except OSError as exc:
     raise st.ToolchainError(f"runtime materialization filesystem operation failed: {exc}") from exc
    except BaseException:
     raise
    finally:
     if root_fd is not None: os.close(root_fd)
     if __import__("sys").exc_info()[0] is not None and destination.exists(): st.remove_snapshot(destination)

def reject_builder_root(raw:bytes, builder_root:pathlib.Path):
    if str(builder_root.resolve()).encode() in raw:
      raise st.ToolchainError("runtime archive retained builder-root binding")

def build_component(name, output, work):
    lock,_=st.validate_lock_structure()
    required={"humanizer":("humanizer_source","humanizer_wheelhouse"),
      "pdf-home":("pdf_wheelhouse","pdf_issuer_key"),"mermaid":("mermaid_bundle",),
      "graphviz":("graphviz_bundle",)}[name]
    for artifact in required: st.verify_artifact_record(lock["artifacts"][artifact],artifact)
    if work.exists() or work.is_symlink(): raise st.ToolchainError("runtime builder work root is not fresh")
    work.mkdir(parents=True); root=work/name
    try:
      if name=="humanizer":
        root.mkdir();st.build_humanizer(root,lock,pathlib.Path(lock["artifacts"]["humanizer_source"]["path"]),pathlib.Path(lock["artifacts"]["humanizer_wheelhouse"]["path"]));st.relocate_humanizer_install(root,root,TOKEN/name)
      elif name=="pdf-home":
        subprocess.run([str(st.ROOT/"toolchain/pdf-renderer/install-from-lock"),str(root),lock["artifacts"]["pdf_wheelhouse"]["path"],lock["artifacts"]["pdf_issuer_key"]["path"]],check=True,env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1"});st.relocate_pdf_install(root,root,TOKEN/name)
      elif name=="mermaid": subprocess.run([str(st.ROOT/"toolchain/mermaid-svg/install-offline"),str(root),lock["artifacts"]["mermaid_bundle"]["path"]],check=True)
      else: subprocess.run([str(st.ROOT/"toolchain/graphviz-svg/install-offline"),str(root),lock["artifacts"]["graphviz_bundle"]["path"]],check=True)
      raw,manifest=tree_archive(root,exclude=(".agents/keys/brand-voice-pdf.key",) if name=="pdf-home" else ())
      reject_builder_root(raw,work)
      record={"schema_version":"brandkit-runtime-archive.v1","component":name,
        "relocation_token":str(TOKEN/name) if name in {"humanizer","pdf-home"} else None,
        "format":_archive_format(manifest),
        **bindings(lock,name),"archive_sha256":hashlib.sha256(raw).hexdigest(),"archive_size":len(raw),"manifest_sha256":hashlib.sha256(canonical(manifest)).hexdigest(),"entries":len(manifest),"files":sum(x["type"]=="file" for x in manifest),"bytes":sum(x.get("size",0) for x in manifest)}
      output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(raw);(output.with_suffix(".json")).write_bytes(canonical(record)+b"\n");return record
    finally:
      if work.exists(): st.remove_snapshot(work)

def main():
 p=argparse.ArgumentParser();p.add_argument("component",choices=COMPONENTS);p.add_argument("output",type=pathlib.Path);p.add_argument("work",type=pathlib.Path);a=p.parse_args();print(json.dumps(build_component(a.component,a.output,a.work),sort_keys=True))
if __name__=="__main__": main()
