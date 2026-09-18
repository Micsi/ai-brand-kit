import copy, importlib.util, pathlib, signal, sys, unittest
from unittest import mock
ROOT=pathlib.Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("harness_selftest_supervisor",ROOT/"scripts/harness_selftest_supervisor.py")
supervisor=importlib.util.module_from_spec(SPEC);assert SPEC.loader;sys.modules[SPEC.name]=supervisor;SPEC.loader.exec_module(supervisor)

class HarnessSelftestSupervisorTests(unittest.TestCase):
 def test_environment_normalization_is_exact_and_secret_safe(self):
  expected={"PATH":"/bound","LC_CTYPE":"C.UTF-8"};cf="0x1F5:0x0:0x3";bound={"environment":expected,"python_executable":"/python3.12","darwin_injected_environment":{"__CF_USER_TEXT_ENCODING":cf}}
  with mock.patch.object(supervisor.sys,"platform","darwin"):
   observed={**expected,"__CF_USER_TEXT_ENCODING":cf,"__PYVENV_LAUNCHER__":"/python3.12"};normalized,diagnostic=supervisor.normalize_worker_environment(observed,bound)
   self.assertEqual(expected,normalized);self.assertEqual(["__PYVENV_LAUNCHER__","__CF_USER_TEXT_ENCODING"],diagnostic["allowed_injected"])
   attacks=({**expected,"__CF_USER_TEXT_ENCODING":"0x1f5:0x0:0x3"},{**expected,"__CF_USER_TEXT_ENCODING":"0x1F5:0x0:0x4"},{**expected,"__CF_USER_TEXT_ENCODING":cf,"SECRET_TOKEN":"secret-value"},{"PATH":"/bound","__CF_USER_TEXT_ENCODING":cf},{**expected,"PATH":"changed","__CF_USER_TEXT_ENCODING":cf},expected)
   for observed in attacks:
    with self.subTest(keys=sorted(observed)):
     normalized,diagnostic=supervisor.normalize_worker_environment(observed,bound);self.assertEqual("mismatch",diagnostic["status"])
     self.assertNotIn("secret-value",str(diagnostic));self.assertTrue(diagnostic["differences"])
  payload='{"event":"environment","diagnostic":{"status":"mismatch","differences":[]}}\n'
  with mock.patch.object(pathlib.Path,"read_text",return_value=payload):
   self.assertEqual("mismatch",supervisor.environment_diagnostic_from_log(pathlib.Path("/virtual/log"))["status"])
 def test_binding_and_collection_tamper_in_memory(self):
  binding={"cwd":str(ROOT),"python":"/python3.12","env":{"PYTHONPATH":str(ROOT)},"command":["python","runner"]}
  for field in binding:
   attacked=copy.deepcopy(binding);attacked[field]="attack"
   with self.subTest(field=field),self.assertRaises(supervisor.SupervisorError):supervisor.require_equal_binding(attacked,binding)
  ids=["tests.synthetic.test_one"];sha=supervisor.digest_bytes(supervisor.canonical(ids))
  collection={"modules":list(supervisor.MODULES),"excluded":[supervisor.EXCLUDED],"test_count":1,"test_ids":ids,"collection_sha256":sha}
  supervisor.require_closed_collection(collection,sha,1)
  for field,value in (("modules",[]),("excluded",[]),("test_count",2),("test_ids",ids+["attack"]),("collection_sha256","0"*64)):
   attacked=copy.deepcopy(collection);attacked[field]=value
   with self.subTest(field=field),self.assertRaises(supervisor.SupervisorError):supervisor.require_closed_collection(attacked,sha,1)
 def test_claim_precedes_state_and_replay_crash_fail_closed(self):
  run=mock.MagicMock();run.__truediv__.side_effect=lambda value:pathlib.Path("/virtual")/value;plan=mock.MagicMock()
  with mock.patch.object(supervisor,"digest_file",return_value="1"*64),mock.patch.object(supervisor,"atomic_exclusive_json") as publish,mock.patch.object(pathlib.Path,"mkdir") as mkdir:
   supervisor.claim_attempt(run,plan);self.assertTrue(publish.called);self.assertTrue(mkdir.called)
  with mock.patch.object(supervisor,"digest_file",return_value="1"*64),mock.patch.object(supervisor,"atomic_exclusive_json",side_effect=FileExistsError),mock.patch.object(pathlib.Path,"mkdir") as mkdir:
   with self.assertRaises(FileExistsError):supervisor.claim_attempt(run,plan)
   mkdir.assert_not_called()
  with mock.patch.object(supervisor,"digest_file",return_value="1"*64),mock.patch.object(supervisor,"atomic_exclusive_json"),mock.patch.object(pathlib.Path,"mkdir",side_effect=OSError("crash after claim")):
   with self.assertRaisesRegex(OSError,"crash after claim"):supervisor.claim_attempt(run,plan)
 def test_atomic_order_and_replay_fully_mocked(self):
  calls=[]
  with mock.patch.object(supervisor.os,"open",return_value=10),mock.patch.object(supervisor.os,"write",side_effect=lambda fd,raw:(calls.append("write") or len(raw))),mock.patch.object(supervisor.os,"fsync",side_effect=lambda fd:calls.append("sync")),mock.patch.object(supervisor.os,"close"),mock.patch.object(supervisor.os,"link",side_effect=lambda a,b:calls.append("publish")),mock.patch.object(pathlib.Path,"mkdir"),mock.patch.object(pathlib.Path,"unlink"):
   supervisor.atomic_exclusive_json(pathlib.Path("/virtual/end.json"),{"end":True})
  self.assertEqual(["write","sync","publish","sync"],calls)
  with mock.patch.object(supervisor.os,"open",return_value=10),mock.patch.object(supervisor.os,"write",side_effect=lambda fd,raw:len(raw)),mock.patch.object(supervisor.os,"fsync"),mock.patch.object(supervisor.os,"close"),mock.patch.object(supervisor.os,"link",side_effect=FileExistsError),mock.patch.object(pathlib.Path,"mkdir"),mock.patch.object(pathlib.Path,"unlink"):
   with self.assertRaises(FileExistsError):supervisor.atomic_exclusive_json(pathlib.Path("/virtual/end.json"),{"end":False})
 def test_exited_leader_live_group_is_killed_and_reaped(self):
  member={"state":"members","members":[{"pid":4,"pgid":123,"uid":501,"stat":"S"}],"sha256":"1"*64};empty={"state":"empty","members":[],"sha256":"2"*64};signals=[]
  with mock.patch.object(supervisor.os,"killpg",side_effect=lambda pid,sig:signals.append(sig)),mock.patch.object(supervisor,"snapshot_group",side_effect=[member,member,member,empty,empty]),mock.patch.object(supervisor.time,"monotonic",side_effect=[0,0]),mock.patch.object(supervisor.time,"sleep"):
   result=supervisor.cleanup_group(123,0,501)
  self.assertTrue(result["term_sent"] and result["kill_sent"] and result["group_reaped"]);self.assertIn(signal.SIGKILL,signals)
 def test_unknown_group_after_kill_fails_closed(self):
  unknown={"state":"unknown","members":[],"sha256":None};foreign={"state":"foreign","members":[{"pid":9,"uid":0}],"sha256":"3"*64}
  for snapshot in (unknown,foreign):
   with self.subTest(state=snapshot["state"]),mock.patch.object(supervisor.os,"killpg",side_effect=PermissionError),mock.patch.object(supervisor,"snapshot_group",side_effect=[snapshot,snapshot]):
    result=supervisor.cleanup_group(123,.5,501);self.assertFalse(result["group_reaped"]);self.assertFalse(result["term_sent"])
  empty={"state":"empty","members":[],"sha256":"2"*64}
  with mock.patch.object(supervisor.os,"killpg",side_effect=PermissionError),mock.patch.object(supervisor,"snapshot_group",side_effect=[empty,empty]):
   result=supervisor.cleanup_group(123,.5,501);self.assertTrue(result["group_reaped"]);self.assertEqual("killpg-eperm-ps",result["cleanup_probe"])
if __name__=="__main__":unittest.main()
