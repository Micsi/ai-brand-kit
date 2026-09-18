from __future__ import annotations
import hashlib,importlib,json,pathlib,sys,tarfile,tempfile,unittest
from unittest import mock
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"scripts"))
import runtime_archives as ra

class RuntimeArchiveTests(unittest.TestCase):
 def record(self,raw,manifest,name="mermaid",archive_format=None):
  return {"schema_version":"brandkit-runtime-archive.v1","component":name,
   "relocation_token":str(ra.TOKEN/name) if name in {"humanizer","pdf-home"} else None,
   "format":archive_format or ra._archive_format(manifest),
   "source_digest":"0"*64,"package_digest":"1"*64,"transform_digest":"2"*64,"archive_sha256":hashlib.sha256(raw).hexdigest(),"archive_size":len(raw),"manifest_sha256":hashlib.sha256(ra.canonical(manifest)).hexdigest(),"entries":len(manifest),"files":sum(x["type"]=="file" for x in manifest),"bytes":sum(x.get("size",0) for x in manifest)}
 def test_two_empty_roots_are_byte_identical_and_materialize(self):
  with tempfile.TemporaryDirectory() as raw:
   base=pathlib.Path(raw)
   values=[]
   for label in "ab":
    root=base/label;root.mkdir();(root/"bin").mkdir();(root/"bin/tool").write_text("relative-runtime\n");(root/"bin/tool").chmod(0o755);(root/"link").symlink_to("bin/tool")
    values.append(ra.tree_archive(root))
   self.assertEqual(values[0],values[1]);archive=base/"runtime.tar";archive.write_bytes(values[0][0]);record=self.record(*values[0])
   target=base/"fresh";ra.materialize("mermaid",archive,record,target,target)
   self.assertEqual(b"relative-runtime\n",(target/"bin/tool").read_bytes());self.assertEqual("bin/tool",(target/"link").readlink().as_posix())
 def test_archive_and_binding_attacks_fail_closed(self):
  with tempfile.TemporaryDirectory() as raw:
   base=pathlib.Path(raw);root=base/"root";root.mkdir();(root/"payload").write_text("locked")
   archive,manifest=ra.tree_archive(root);path=base/"runtime.tar";path.write_bytes(archive);record=self.record(archive,manifest)
   attacks=[];changed=bytearray(archive);changed[0]^=1;attacks.append(bytes(changed));attacks.append(archive[:-512])
   for value in attacks:
    path.write_bytes(value)
    with self.assertRaises(ra.st.ToolchainError):ra.inspect_archive(path,record)
   path.write_bytes(archive);wrong=dict(record);wrong["manifest_sha256"]="f"*64
   with self.assertRaises(ra.st.ToolchainError):ra.inspect_archive(path,wrong)
   lock={"components":{"mermaid":{"sha256":"a"}},"artifacts":{"mermaid_bundle":{"sha256":"b"}},"commands":{"implementation":{"sha256":"c"},"runtime_builder":{"sha256":"e"}},"transforms":{"x":{"sha256":"d"}}}
   first=ra.bindings(lock,"mermaid");lock["artifacts"]["mermaid_bundle"]["sha256"]="changed";self.assertNotEqual(first,ra.bindings(lock,"mermaid"))
 def test_old_builder_root_is_rejected_by_productive_builder_gate(self):
  with tempfile.TemporaryDirectory() as raw:
   base=pathlib.Path(raw);root=base/"root";root.mkdir()
   (root/"payload").write_text("/Volumes/Daten/Projekte/BrandKit/.runtime/old-root/bin")
   archive,_=ra.tree_archive(root)
   with self.assertRaisesRegex(ra.st.ToolchainError,"builder-root"):
    ra.reject_builder_root(archive,pathlib.Path("/Volumes/Daten/Projekte/BrandKit/.runtime/old-root"))
 def test_top_lock_closes_real_archives_and_run_consumers(self):
  lock=json.loads((ROOT/"toolchain/toolchain.lock.json").read_text())
  self.assertEqual(set(lock["runtime_archives"]),set(ra.COMPONENTS))
  self.assertEqual(set(ra.st.CONSUMED_RUN_ARTIFACTS),
   {"runtime_humanizer","runtime_pdf_home","runtime_mermaid","runtime_graphviz","pdf_issuer_key"})
  for name,record in lock["runtime_archives"].items():
   self.assertEqual(ra.bindings(lock,name),{key:record[key] for key in ("source_digest","package_digest","transform_digest")})
   artifact=lock["artifacts"]["runtime_"+name.replace("-","_")]
   path=pathlib.Path(artifact["path"])
   self.assertTrue(path.is_file());self.assertEqual(record["archive_size"],path.stat().st_size)
 def archive(self,base,entries,archive_format="ustar"):
  payloads={entry["path"]:entry.pop("data") for entry in entries if entry["type"]=="file"}
  raw,manifest=ra._canonical_runtime_tar(entries,payloads,archive_format)
  path=base/f"attack-{archive_format}.tar";path.write_bytes(raw)
  return path,self.record(raw,manifest,archive_format=archive_format)
 def test_symlink_pivots_and_unsafe_targets_fail_before_any_write(self):
  with tempfile.TemporaryDirectory() as raw:
   base=pathlib.Path(raw);outside=base/"outside";outside.mkdir()
   for archive_format in ("ustar","pax"):
    for order in ("early","late"):
     link={"path":"escape","type":"symlink","target":str(outside)}
     payload={"path":"escape/pwn","type":"file","mode":"0644","data":b"escaped"}
     entries=[link,payload] if order=="early" else [payload,link]
     with self.subTest(format=archive_format,order=order):
      path,record=self.archive(base,entries,archive_format);destination=base/f"dest-{archive_format}-{order}"
      with self.assertRaises(ra.st.ToolchainError):ra.materialize("mermaid",path,record,destination,destination)
      self.assertFalse((outside/"pwn").exists());self.assertFalse(destination.exists())
    for index,target in enumerate(("", "../../outside", "../outside", "safe/../outside", "./safe", "safe//outside", "safe\\outside")):
     with self.subTest(format=archive_format,target=target):
      path,record=self.archive(base,[{"path":"link","type":"symlink","target":target}],archive_format)
      with self.assertRaises(ra.st.ToolchainError):ra.inspect_archive(path,record)
 def test_nested_symlink_ancestor_is_order_independent(self):
  with tempfile.TemporaryDirectory() as raw:
   base=pathlib.Path(raw)
   directory={"path":"nested","type":"directory","mode":"0755"}
   link={"path":"nested/pivot","type":"symlink","target":"../nested"}
   payload={"path":"nested/pivot/pwn","type":"file","mode":"0644","data":b"escaped"}
   for archive_format in ("ustar","pax"):
    for entries in ([directory.copy(),link.copy(),payload.copy()],[directory.copy(),payload.copy(),link.copy()]):
     with self.subTest(format=archive_format,order=[entry["type"] for entry in entries]):
      path,record=self.archive(base,entries,archive_format)
      with self.assertRaisesRegex(ra.st.ToolchainError,"symlink ancestor"):ra.inspect_archive(path,record)
 def test_existing_destination_parent_symlink_is_never_followed(self):
  with tempfile.TemporaryDirectory() as raw:
   base=pathlib.Path(raw);actual=base/"actual";actual.mkdir();alias=base/"alias";alias.symlink_to(actual)
   path,record=self.archive(base,[{"path":"payload","type":"file","mode":"0644","data":b"locked"}])
   destination=alias/"install"
   with self.assertRaises(ra.st.ToolchainError):ra.materialize("mermaid",path,record,destination,destination)
   self.assertFalse((actual/"install").exists())
 def test_undeclared_directory_ancestor_is_rejected_before_materialization(self):
  with tempfile.TemporaryDirectory() as raw:
   base=pathlib.Path(raw);path,record=self.archive(base,[{"path":"missing/payload","type":"file","mode":"0644","data":b"locked"}])
   destination=base/"install"
   with self.assertRaisesRegex(ra.st.ToolchainError,"undeclared directory ancestor"):ra.materialize("mermaid",path,record,destination,destination)
   self.assertFalse(destination.exists())
 def test_internal_npm_links_survive_ustar_and_canonical_pax(self):
  with tempfile.TemporaryDirectory() as raw:
   base=pathlib.Path(raw)
   for archive_format in ("ustar","pax"):
    package="pkg" if archive_format=="ustar" else "p"*120
    entries=[
     {"path":"node_modules","type":"directory","mode":"0755"},
     {"path":"node_modules/.bin","type":"directory","mode":"0755"},
     {"path":f"node_modules/{package}","type":"directory","mode":"0755"},
     {"path":f"node_modules/{package}/tool.js","type":"file","mode":"0755","data":b"#!/usr/bin/env node\n"},
     {"path":"node_modules/.bin/tool","type":"symlink","target":f"../{package}/tool.js"},
    ]
    path,record=self.archive(base,entries,archive_format);destination=base/f"npm-{archive_format}"
    ra.materialize("mermaid",path,record,destination,destination)
    self.assertEqual(f"../{package}/tool.js",(destination/"node_modules/.bin/tool").readlink().as_posix())
    self.assertEqual(b"#!/usr/bin/env node\n",(destination/"node_modules/.bin/tool").read_bytes())
if __name__=="__main__":unittest.main()
