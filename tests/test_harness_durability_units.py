import importlib.util, pathlib, sys, unittest
from unittest import mock
ROOT=pathlib.Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("harness_phases_units",ROOT/"scripts/harness_phases.py")
phases=importlib.util.module_from_spec(SPEC);assert SPEC.loader;sys.modules[SPEC.name]=phases;SPEC.loader.exec_module(phases)

class HarnessDurabilityUnitTests(unittest.TestCase):
 def test_append_has_no_sync_and_explicit_sync_is_single(self):
  log=object.__new__(phases.EventLog);log.run_id="run";log.descriptor=10;log.synced_size=0;log.closed=False
  info=type("Info",(),{"st_dev":1,"st_ino":2,"st_size":100})()
  sync_name="fdatasync" if hasattr(phases.os,"fdatasync") else "fsync"
  with mock.patch.object(log,"_verify_descriptor",return_value=info),mock.patch.object(phases.os,"write",side_effect=lambda fd,raw:len(raw)) as write,mock.patch.object(phases.os,sync_name) as sync,mock.patch.object(phases.os,"fstat",return_value=info):
   log.append({"event":"unit"});self.assertEqual(0,sync.call_count);log.sync();self.assertEqual(1,sync.call_count);self.assertEqual(1,write.call_count)
 def test_finalize_syncs_once_then_receipt_then_closure(self):
  dag=object.__new__(phases.PhaseDAG);dag.completed={"source":"1"*64};dag.specs={"source":phases.PhaseSpec("source",resumable=True)};dag.run_id="run";dag.root=pathlib.Path("/virtual");dag.events=mock.Mock(path=pathlib.Path("/virtual/events/run.jsonl"));dag.pending_receipts={"source":(pathlib.Path("/virtual/receipts/source.json"),{"receipt":1})}
  result={"sha256":"2"*64,"size":1,"events":2,"phase_receipts":dag.completed};order=[]
  dag.events.sync.side_effect=lambda:order.append("sync");dag.events.close.side_effect=lambda:order.append("close")
  with mock.patch.object(phases,"validate_event_log",return_value=result),mock.patch.object(phases,"atomic_json",side_effect=lambda path,value:order.append(path.parent.name)),mock.patch.object(phases,"require_single_link_file"):
   dag.finalize()
  self.assertEqual(["sync","receipts","closures","close"],order)
 def test_error_syncs_once_and_preserves_primary(self):
  dag=object.__new__(phases.PhaseDAG);dag.specs={"phase":phases.PhaseSpec("phase")};dag.completed={};dag.events=mock.Mock();dag._bindings=mock.Mock(return_value=({},"1"*64,"2"*64,0,0));dag._event=mock.Mock()
  with self.assertRaisesRegex(ValueError,"primary"):dag.run("phase",lambda:(_ for _ in ()).throw(ValueError("primary")),inputs={},code_paths=[])
  dag.events.sync.assert_called_once_with();dag.events.close.assert_called_once_with()
 def test_post_receipt_preclosure_failure_has_no_commit(self):
  dag=object.__new__(phases.PhaseDAG);dag.completed={"source":"1"*64};dag.specs={"source":phases.PhaseSpec("source",resumable=True)};dag.run_id="run";dag.root=pathlib.Path("/virtual");dag.events=mock.Mock(path=pathlib.Path("/virtual/events/run.jsonl"));dag.pending_receipts={"source":(pathlib.Path("/virtual/receipts/source.json"),{"receipt":1})}
  result={"sha256":"2"*64,"size":1,"events":2,"phase_receipts":dag.completed};published=[]
  def publish(path,value):
   published.append(path.parent.name)
   if path.parent.name=="closures":raise OSError("preclosure")
  with mock.patch.object(phases,"validate_event_log",return_value=result),mock.patch.object(phases,"atomic_json",side_effect=publish):
   with self.assertRaisesRegex(OSError,"preclosure"):dag.finalize()
  self.assertEqual(["receipts","closures"],published);dag.events.close.assert_called_once_with()
 def test_postrename_parent_sync_failure_unlinks_visible_target(self):
  handle=mock.MagicMock();handle.__enter__.return_value=handle;handle.fileno.return_value=10
  with mock.patch.object(phases.tempfile,"mkstemp",return_value=(10,"/virtual/.tmp")),mock.patch.object(phases.os,"fdopen",return_value=handle),mock.patch.object(phases.json,"dump"),mock.patch.object(phases.os,"replace"),mock.patch.object(phases.os,"open",return_value=11),mock.patch.object(phases.os,"fsync",side_effect=[None,OSError("parent")]),mock.patch.object(phases.os,"close"),mock.patch.object(phases.os,"unlink"),mock.patch.object(phases.pathlib.Path,"unlink") as unlink,mock.patch.object(phases,"ensure_directory"):
   with self.assertRaisesRegex(OSError,"parent"):phases.atomic_json(pathlib.Path("/virtual/closure.json"),{"closed":True})
  unlink.assert_called_once_with()
if __name__=="__main__":unittest.main()
