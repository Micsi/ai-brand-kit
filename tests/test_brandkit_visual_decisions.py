from __future__ import annotations

import contextlib
import copy
import fcntl
from fractions import Fraction
import hashlib
import hmac
import inspect
import html
from html.parser import HTMLParser
import io
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest

from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._f_v_a_r import Axis
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.ttLib.tables.O_S_2f_2 import Panose

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import brandkit_visual_decisions as visual

NOW = 2_000_000_000

# Executed by a real, separate interpreter process: it shares no memory with the
# test process, so the only affinity replay state it can observe is the ledger
# that the previous process left on disk.
REPLAY_RUNNER = """
import json, os, sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import brandkit_visual_decisions as visual

try:
    result = visual.validate_bundle(Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]), sidecar_root=Path(sys.argv[5]), affinity_auth=json.loads(Path(sys.argv[6]).read_text()))
    payload = {"ok": True, "pid": os.getpid(), "eligible": sorted(item["id"] for item in result["eligible_candidates"])}
except visual.VisualDecisionError as exc:
    payload = {"ok": False, "pid": os.getpid(), "error": str(exc)}
sys.stdout.write("RESULT:" + json.dumps(payload) + "\\n")
"""


def write(path: Path, data: bytes | str) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True); raw = data.encode() if isinstance(data, str) else data; path.write_bytes(raw)
    return {"path": path.name, "sha256": hashlib.sha256(raw).hexdigest()}


def canonical_write(root: Path, name: str, value: dict) -> dict[str, str]:
    raw = visual.canonical_json(value); (root / name).write_bytes(raw); return {"path": name, "sha256": visual.sha256_bytes(raw)}


def unavailable(project: Path, run_id: str, *, complete: bool = True, matches: list[str] | None = None, providers: list[dict] | None = None) -> dict:
    providers = providers or []; matches = matches or []
    status = "unknown" if not complete else ("unavailable" if not matches else ("ambiguous" if len(matches) > 1 else "available"))
    return {"schema_version":"brandkit-affinity-observation.v1","project_identity_sha256":visual.project_identity(project),"run_id":run_id,"observed_at":NOW-10,"expires_at":NOW+10,"status":status,"probe":{"mode":"read-only","complete":complete,"inventory_sha256":visual.sha256_bytes(visual.canonical_json(providers)),"providers":providers},"matching_servers":matches,"lock":None,"mutation_capable":False,"blocker":"No unique pinned Affinity MCP capability."}


def palette() -> dict[str, str]:
    return {"canvas":"#FFFFFF","surface":"#F5F5F5","surface-strong":"#E5E5E5","text":"#111111","text-muted":"#555555","border":"#767676","primary":"#111111","on-primary":"#FFFFFF","secondary":"#222222","on-secondary":"#FFFFFF","accent":"#005FCC","on-accent":"#FFFFFF","success":"#176B2C","on-success":"#FFFFFF","warning":"#6B4F00","on-warning":"#FFFFFF","danger":"#A00000","on-danger":"#FFFFFF","focus":"#005FCC"}


def make_font(path: Path, family: str, classification: str, ligatures: str | None = None) -> tuple[bytes, bytes]:
    """A real font for ``family``; ``ligatures`` gives it a GSUB of one shape.

    ``"liga"`` and ``"calt"`` substitute f+i by a ligature glyph under that
    feature; ``"unreferenced"`` builds a liga feature and then takes it out of
    every language system; ``"empty"`` keeps the feature referenced but gives it
    no lookup; ``"other"`` builds a stylistic set only.  No shape shows in the
    family name - which is what the sheet's ligature statement is tested against.
    Without ``ligatures`` the bytes are exactly what they were before.
    """
    chars = sorted({char for char in visual.SPECIMEN if not char.isspace()}); order = [".notdef", "space"] + [f"u{ord(char):06X}" for char in chars] + (["f_i"] if ligatures else [])
    fb = FontBuilder(1000, isTTF=True); fb.setupGlyphOrder(order); fb.setupCharacterMap({ord(char):f"u{ord(char):06X}" for char in chars} | {32:"space"})
    glyphs = {}; metrics = {}
    for index, glyph in enumerate(order):
        pen = TTGlyphPen(None)
        if glyph not in {".notdef", "space"}: pen.moveTo((80,0)); pen.lineTo((520,0)); pen.lineTo((520,700)); pen.lineTo((80,700)); pen.closePath()
        glyphs[glyph] = pen.glyph(); metrics[glyph] = ((600 if classification == "mono" else 560 + index % 3 * 20), 40)
    fb.setupGlyf(glyphs); fb.setupHorizontalMetrics(metrics); fb.setupHorizontalHeader(ascent=800,descent=-200)
    panose = Panose(); panose.bFamilyType=2; panose.bSerifStyle = 2 if classification == "serif" else 11; panose.bProportion = 9 if classification == "mono" else 2
    fb.setupOS2(sTypoAscender=800,sTypoDescender=-200,usWinAscent=800,usWinDescent=200,usWeightClass=400,usWidthClass=5,fsType=0,panose=panose)
    fb.setupNameTable({"familyName":family,"styleName":"Regular","fullName":f"{family} Regular","psName":f"{family.replace(' ','')}-Regular","version":"Version 1.000"}); fb.setupPost(isFixedPitch=1 if classification == "mono" else 0); fb.setupMaxp()
    if ligatures:
        feature = {"calt": "calt", "other": "ss01"}.get(ligatures, "liga")
        addOpenTypeFeaturesFromString(fb.font, f"languagesystem DFLT dflt;\nlanguagesystem latn dflt;\nfeature {feature} {{\n  sub u000066 u000069 by f_i;\n}} {feature};\n")
        table = fb.font["GSUB"].table
        if ligatures == "unreferenced":
            for script in table.ScriptList.ScriptRecord:
                for system in [script.Script.DefaultLangSys] + [record.LangSys for record in script.Script.LangSysRecord]:
                    if system is not None: system.FeatureIndex = []; system.FeatureCount = 0
        if ligatures == "empty":
            for record in table.FeatureList.FeatureRecord: record.Feature.LookupListIndex = []; record.Feature.LookupCount = 0
    source = path.with_suffix(".ttf"); fb.save(source); source_bytes=source.read_bytes(); fb.font.flavor="woff2"; fb.save(path); return path.read_bytes(), source_bytes


@contextlib.contextmanager
def deadline(seconds: float, what: str):
    """Turn "does not terminate" into an ordinary test failure.

    A test that hangs is not a measurement.  The r12 mutation run killed one
    mutant only by wedging the suite: the string branch of the CSS
    canonicaliser span for ever, no assertion ever ran, and the property that
    should have been red was simply absent.  Everything below that touches the
    scanner therefore runs under a wall-clock limit, and the limit fires as an
    assertion so the property shows up as a failure with a name.
    """
    def fire(_signum, _frame) -> None:
        raise AssertionError(f"{what} did not terminate within {seconds:g}s")
    previous = signal.signal(signal.SIGALRM, fire)
    # Nested, because every test runs inside the module-wide limit below.  There
    # is one interval timer per process: arming a second one without handing the
    # first its remaining time back would leave the outer limit *disarmed* for
    # the rest of the test, so a hang after an inner block would go back to
    # hanging.  setitimer answers with what was left, so the outer deadline is
    # restored on the way out, minus the time this block really took.
    remaining, _interval = signal.setitimer(signal.ITIMER_REAL, seconds)
    started = time.monotonic()
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if remaining > 0:
            signal.setitimer(signal.ITIMER_REAL, max(remaining - (time.monotonic() - started), 0.001))


class TimeLimitedTestCase(unittest.TestCase):
    """Every test in this module, under a hard wall-clock limit.

    The r14 mutation run killed exactly one mutant by *hanging*: the property
    "the key and the ledger stay outside the project by identity" had no
    assertion that noticed its removal, and the run stood for 902.9 s until an
    external timeout cut it off.  In CI that is a wedged job, not a red test,
    and a suite that kills by hanging reports a coverage it does not have.

    A limit around the individual scanner calls was not enough for that, because
    the hang was in the affinity path rather than in the scanner: the limit
    belongs around *every* test.  ``TestCase.run`` executes setUp, the test
    method and tearDown, and an exception raised into any of them from the
    signal handler is recorded as an ordinary failure carrying the test's name.
    """

    TIME_LIMIT = 180.0

    def run(self, result=None):
        with deadline(self.TIME_LIMIT, f"test {self.id()}"):
            return super().run(result)


def _names_protected_run_probe(path: Path) -> bool:
    """The rule under test, stated once: does this path name the sealed run?"""
    return visual._names_protected_run(path)


def same_file(one: Path, two: Path) -> bool:
    """Do these two spellings name the same file, as the kernel sees it?"""
    try:
        first, second = os.stat(one), os.stat(two)
    except OSError:
        return False
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def case_variant(path: Path) -> Path | None:
    """A differently-cased spelling of ``path`` that names the very same file.

    ``None`` on a case-sensitive file system, where the variant is simply a
    different, non-existent path and there is nothing to defend against.  The
    caller then skips that leg rather than asserting something the host cannot
    exhibit.
    """
    parts = list(path.parts)
    for index in range(len(parts) - 1, 0, -1):
        flipped = parts[index].swapcase()
        if flipped == parts[index]:
            continue
        candidate = Path(*parts[:index], flipped, *parts[index + 1:])
        if same_file(candidate, path):
            return candidate
    return None


def normalization_variant(path: Path) -> Path | None:
    """An NFD spelling of ``path`` that names the very same file, or ``None``.

    This is the second, case-*independent* form of the same defect: it works
    even where the file system compares names case-sensitively, so a fix that
    only folds case does not close it.
    """
    text = str(path)
    for form in ("NFD", "NFC"):
        candidate = Path(unicodedata.normalize(form, text))
        if candidate != path and same_file(candidate, path):
            return candidate
    return None


class VisualDecisionCoreR2Tests(TimeLimitedTestCase):
    def project(self) -> tuple[tempfile.TemporaryDirectory, Path, Path]:
        temp=tempfile.TemporaryDirectory(); project=Path(temp.name).resolve(); workspace=visual.visual_workspace(project,"run-a",create=True); return temp,project,workspace

    def test_descriptor_reads_reject_duplicate_nonfinite_symlink_hardlink_and_escape(self):
        temp,_,root=self.project()
        try:
            (root/"a.json").write_text("{}")
            (root/"hard.json").hardlink_to(root/"a.json")
            with self.assertRaisesRegex(visual.VisualDecisionError,"exclusive"): visual.secure_read(root,"a.json","hardlinked")
            (root/"sym.json").symlink_to(root/"a.json")
            with self.assertRaises(visual.VisualDecisionError): visual.secure_read(root,"sym.json","linked")
            with self.assertRaises(visual.VisualDecisionError): visual.secure_read(root,"../x","escape")
            with self.assertRaisesRegex(visual.VisualDecisionError,"duplicate"): visual.load_json_bytes(b'{"x":1,"x":2}',"dup")
            with self.assertRaisesRegex(visual.VisualDecisionError,"non-finite"): visual.load_json_bytes(b'{"x":NaN}',"nan")
        finally: temp.cleanup()

    def test_workspace_is_canonical_and_first_ci_is_hard_protected_with_reproducible_inventory(self):
        temp,project,_=self.project()
        try:
            protected=project/".brandkit/work/first-ci"; protected.mkdir(); (protected/"a.txt").write_text("sealed")
            before=(protected/"a.txt").read_bytes(); one=visual.protected_run_inventory(project); two=visual.protected_run_inventory(project)
            self.assertEqual(one,two); self.assertEqual(before,(protected/"a.txt").read_bytes()); self.assertEqual("brandkit-protected-run-inventory.v1",one["algorithm"])
            with self.assertRaisesRegex(visual.VisualDecisionError,"protected"): visual.visual_workspace(project,"first-ci",create=True)
            link=project.parent/(project.name+"-link"); link.symlink_to(project)
            try:
                with self.assertRaisesRegex(visual.VisualDecisionError,"canonical"): visual.visual_workspace(link,"run-b",create=True)
            finally: link.unlink()
            with tempfile.TemporaryDirectory() as bad_name, tempfile.TemporaryDirectory() as outside_name:
                bad=Path(bad_name).resolve(); (bad/".brandkit").symlink_to(Path(outside_name).resolve(),target_is_directory=True)
                with self.assertRaises(visual.VisualDecisionError): visual.visual_workspace(bad,"run-a",create=True)
        finally: temp.cleanup()

    def test_full_semantic_palette_matrix_exact_threshold_and_non_color_cues(self):
        report=visual.color_contrast_report("a"*64,"color-a",palette(),["text","icon"]); visual.validate_color_report(report); self.assertEqual(13,len(report["tests"]))
        bad=palette(); bad.pop("on-danger")
        with self.assertRaisesRegex(visual.VisualDecisionError,"roles differ"): visual.color_contrast_report("a"*64,"color-a",bad,["text","icon"])
        with self.assertRaisesRegex(visual.VisualDecisionError,"non-color"): visual.color_contrast_report("a"*64,"color-a",palette(),["text"])
        near=palette(); near["text"]="#777777"; near["surface"]="#FFFFFF"; near["surface-strong"]="#FFFFFF"
        result=visual.color_contrast_report("a"*64,"color-a",near,["text","icon"]); self.assertFalse(result["tests"][0]["passed"]); self.assertEqual("4.48:1",result["tests"][0]["ratio_display"])

    def test_five_owner_decision_domains_are_independent(self):
        report={"path":"report.json","sha256":"a"*64}; values=[
            ("color",{"domain":"color","roles":palette(),"report":report}),
            ("typography",{"domain":"typography","font_candidate_ids":["serif-a","sans-a"],"specimen_sha256":visual.SPECIMEN_SHA256,"report":report}),
            ("code-font",{"domain":"code-font","code_font_id":"mono-a","specimen_sha256":visual.SPECIMEN_SHA256,"report":report}),
            ("logo",{"domain":"logo","logo_candidate_ids":["mark-a","favicon-a"],"creation_origin":"owner-supplied","report":report}),
            ("lockup",{"domain":"lockup","lockup_candidate_ids":["horizontal-a","stacked-a"],"creation_origin":"owner-supplied","report":report}),
        ]
        candidates=[]
        for kind,value in values: candidates.append({"id":kind+"-choice","kind":kind,"label":kind,"value":value,"value_sha256":visual.sha256_bytes(visual.canonical_json(value)),"source_ids":["source-a"],"status":"candidate"})
        document={"schema_version":"brandkit-visual-candidate-set.v1","brief_sha256":"b"*64,"source_review":{"path":"review.json","sha256":"c"*64},"status":"complete","candidates":candidates,"recommendations":[],"normative":False}
        visual.validate_schema(document,"candidates"); self.assertEqual(["color","typography","code-font","logo","lockup"],[item["kind"] for item in candidates])

    def test_real_font_tables_rights_source_license_glyphs_and_distinct_classes(self):
        temp,_,root=self.project()
        try:
            candidates=[]; revision="1"*40; license_binding=write(root/"OFL.txt","fixture license")
            for classification,family in (("serif","Fixture Serif"),("sans","Fixture Sans"),("mono","Fixture Mono")):
                font_id=f"{classification}-a"; asset,source=make_font(root/f"{font_id}.woff2",family,classification); source_binding={"path":f"{font_id}.ttf","sha256":visual.sha256_bytes(source)}
                rights={"schema_version":"rights-evidence.v1","issuer":"Fixture","source":"https://example.invalid/fonts","scope":"single-asset","asset_id":font_id,"license_id":"OFL-1.1","status":"open-license","version":"1.0","date":"2026-09-03","todo":None,"source_revision":revision,"source_path":source_binding["path"],"source_sha256":source_binding["sha256"],"license_evidence_path":"OFL.txt","license_evidence_sha256":license_binding["sha256"],"transformation":"Deterministic test conversion to WOFF2."}; rights_binding=canonical_write(root,f"{font_id}.rights.json",rights)
                extracted=visual.extract_font_metadata(asset); self.assertEqual(classification,extracted["classification"]); self.assertEqual([],extracted["missing_specimen_codepoints"])
                candidates.append({"id":font_id,"classification":classification,"family":family,"specimen":visual.SPECIMEN,"specimen_sha256":visual.SPECIMEN_SHA256,"asset":{"path":f"{font_id}.woff2","sha256":visual.sha256_bytes(asset)},"metadata":{"license_spdx":"OFL-1.1","source_pin":{"origin":"https://example.invalid/fonts","revision":revision,**source_binding},"license_evidence":license_binding,"extracted":extracted},"rights":rights_binding,"objective_checks":{"woff2-valid":True,"asset-hash-valid":True,"rights-closed":True,"pin-valid":True,"specimen-identical":True,"metadata-matches":True,"glyph-coverage":True,"embedding-allowed":True},"human_reviews":{"readability":"required","tracking":"required","digits":"required","german-glyphs":"required","code-punctuation":"required","fallback":"required","reflow-200":"required"}})
            report={"schema_version":"brandkit-font-candidate-report.v1","candidate_set_sha256":"a"*64,"specimen_sha256":visual.SPECIMEN_SHA256,"candidates":candidates,"objective_status":"pass","human_review_status":"required"}; visual.validate_font_report(report,root)
            forged=copy.deepcopy(report); forged["candidates"][0]["metadata"]["extracted"]["family"]="Forgery"
            with self.assertRaisesRegex(visual.VisualDecisionError,"metadata"): visual.validate_font_report(forged,root)
            same=copy.deepcopy(report); same["candidates"][1]["asset"]=same["candidates"][0]["asset"]
            with self.assertRaises(visual.VisualDecisionError): visual.validate_font_report(same,root)
            # The source a font is pinned to has to be fetchable over a channel
            # that authenticates the bytes.  Like the Affinity probe mode, this
            # is decided *twice* - the report schema carries ``^https://`` and
            # the validator repeats it - so the first layer is measured on its
            # own message, and the second with an input that answers the
            # question twice: a pin whose origin reads ``https://`` for the
            # schema and plain HTTP for the line below it.  Without that, the
            # second layer could be deleted and no test would notice.
            for origin in ("http://example.invalid/fonts","ftp://example.invalid/fonts",
                           "HTTPS://example.invalid/fonts","//example.invalid/fonts","file:///fonts"):
                with self.subTest(schema_layer=origin):
                    plain=copy.deepcopy(report); plain["candidates"][0]["metadata"]["source_pin"]["origin"]=origin
                    with self.assertRaisesRegex(visual.VisualDecisionError,"source_pin.origin"): visual.validate_font_report(plain,root)
            class FlippingPin(dict):
                reads=0
                def __getitem__(self,key):
                    if key=="origin":
                        FlippingPin.reads+=1
                        return "https://example.invalid/fonts" if FlippingPin.reads==1 else "http://example.invalid/fonts"
                    return super().__getitem__(key)
            layered=copy.deepcopy(report); metadata=layered["candidates"][0]["metadata"]
            metadata["source_pin"]=FlippingPin(metadata["source_pin"])
            with self.assertRaisesRegex(visual.VisualDecisionError,"is not HTTPS"): visual.validate_font_report(layered,root)
            self.assertEqual(2,FlippingPin.reads,"the origin is read by the schema and by the code, in that order")
        finally: temp.cleanup()

    def test_svg_requires_complete_variants_sizes_single_paint_and_human_passability(self):
        temp,project,root=self.project()
        try:
            obs=unavailable(project,"run-a"); variants=("mark","lockup-horizontal","lockup-stacked","favicon"); candidates=[]
            for variant in variants:
                ident=variant.replace("lockup-","")+"-a"; asset_id="fixture-"+ident; raw=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><title>{asset_id}</title><rect x="2" y="2" width="60" height="60" fill="#111111"/></svg>'.encode(); (root/f"{ident}.svg").write_bytes(raw); checks=visual._svg_static_checks(raw,ident,asset_id)
                candidates.append({"id":ident,"asset_id":asset_id,"variant":variant,"asset":{"path":f"{ident}.svg","sha256":visual.sha256_bytes(raw)},"creation_origin":"owner-supplied","minimum_width_px":16,"test_sizes_px":[16,24,32,48,64,128],"objective_checks":checks,"human_reviews":{name:"required" for name in ("size-16","size-24","size-32","size-48","size-64","size-128","minimum-width","monochrome","clipping","legibility")}})
            report={"schema_version":"brandkit-logo-test-report.v1","candidate_set_sha256":"a"*64,"affinity_observation_sha256":visual.sha256_bytes(visual.canonical_json(obs)),"candidates":candidates,"objective_status":"pass","human_review_status":"required"}; visual.validate_logo_report(report,root,obs,project_root=project,run_id="run-a",now=NOW)
            missing=copy.deepcopy(report); missing["candidates"].pop()
            with self.assertRaises(visual.VisualDecisionError): visual.validate_logo_report(missing,root,obs,project_root=project,run_id="run-a",now=NOW)
            gradient=report["candidates"][0]; raw=(root/gradient["asset"]["path"]).read_text().replace('<rect','<defs><linearGradient id="g"><stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#000"/></linearGradient></defs><rect'); (root/"gradient.svg").write_text(raw)
            self.assertFalse(visual._svg_static_checks(raw.encode(),"gradient",gradient["asset_id"])["monochrome_static"])
            # A candidate that claims Affinity authorship while the observation
            # says the server was never there is a contradiction the report
            # itself carries; it is refused before any receipt is asked for, so
            # the refusal names the conflict rather than the missing receipt.
            for variant in ("mark","favicon","lockup-horizontal"):
                with self.subTest(authored=variant):
                    claimed=copy.deepcopy(report)
                    next(item for item in claimed["candidates"] if item["variant"]==variant)["creation_origin"]="affinity-mcp"
                    with self.assertRaisesRegex(visual.VisualDecisionError,"conflicts with unavailable observation"):
                        visual.validate_logo_report(claimed,root,obs,project_root=project,run_id="run-a",now=NOW)
        finally: temp.cleanup()

    def test_affinity_states_freshness_tool_pins_hmac_output_and_replay(self):
        temp,project,root=self.project()
        try:
            for complete,matches,expected in ((False,[],"unknown"),(True,[],"unavailable")):
                obs=unavailable(project,"run-a",complete=complete,matches=matches); self.assertEqual(expected,obs["status"]); visual.validate_affinity_observation(obs,root,project_root=project,run_id="run-a",now=NOW)
            stale=unavailable(project,"run-a"); stale["expires_at"]=NOW-1
            with self.assertRaisesRegex(visual.VisualDecisionError,"stale"): visual.validate_affinity_observation(stale,root,project_root=project,run_id="run-a",now=NOW)
            providers=[{"server_id":"a","origin":"https://example.invalid/a","version":"1","tools":[{"name":"export_svg","schema_sha256":"1"*64}]},{"server_id":"b","origin":"https://example.invalid/b","version":"1","tools":[{"name":"export_svg","schema_sha256":"2"*64}]}]; amb=unavailable(project,"run-a",matches=["a","b"],providers=providers); visual.validate_affinity_observation(amb,root,project_root=project,run_id="run-a",now=NOW)
            provider=providers[0]; lock={"schema_version":"brandkit-affinity-mcp-lock.v1","server_id":"a","origin":provider["origin"],"version":"1","license_spdx":"MIT","source_revision":"3"*40,"source_sha256":"4"*64,"tools":provider["tools"]}; lock_binding=canonical_write(root,"affinity.lock.json",lock)
            obs=unavailable(project,"run-a",matches=["a"],providers=[provider]); obs.update(status="available",lock={**lock_binding,"server_id":"a"},mutation_capable=True,blocker=None)
            key=b"k"*32; owner_key=domain_key(key,OWNER_DOMAIN); owner_raw={"schema_version":"brandkit-discovery-owner-receipt.v1","key_id":visual.sha256_bytes(owner_key)[:16],"payload":{"decision":"graphics-mutate","project_identity_sha256":visual.project_identity(project),"run_id":"run-a","document_id":"logo-doc"}}; owner_raw["hmac_sha256"]=hmac.new(owner_key,visual.canonical_json(owner_raw),hashlib.sha256).hexdigest(); owner=canonical_write(root,"owner.json",owner_raw)
            write(root/"input.json","input"); out=write(root/"staging/logo-doc/mark.svg","output"); out["path"]="staging/logo-doc/mark.svg"
            operation_key=visual._derive_key(key,b"brandkit-affinity-operation-v1")
            receipt={"schema_version":"brandkit-affinity-mutation-receipt.v1","issuer":"fixture","key_id":visual.sha256_bytes(operation_key)[:16],"capability":"graphics-mutate","project_identity_sha256":visual.project_identity(project),"run_id":"run-a","document_id":"logo-doc","staging_root":".brandkit/work/run-a/visual-decisions/staging/logo-doc","observation_sha256":visual.sha256_bytes(visual.canonical_json(obs)),"server_lock_sha256":lock_binding["sha256"],"owner_approval":owner,"tool":provider["tools"][0],"operation":"export-svg","inputs":[{"path":"input.json","sha256":visual.sha256_bytes(b"input")}],"outputs":[{"candidate_id":"mark-a",**out}],"issued_at":NOW-1,"expires_at":NOW+1,"nonce":"5"*64}; receipt["hmac_sha256"]=hmac.new(operation_key,visual.canonical_json(receipt),hashlib.sha256).hexdigest(); replay=set()
            visual.validate_affinity_mutation_receipt(receipt,obs,root,project_root=project,run_id="run-a",key=key,replay_nonces=replay,now=NOW)
            with self.assertRaisesRegex(visual.VisualDecisionError,"already consumed"): visual.validate_affinity_mutation_receipt(receipt,obs,root,project_root=project,run_id="run-a",key=key,replay_nonces=replay,now=NOW)
            drift=copy.deepcopy(obs); drift["probe"]["providers"][0]["tools"][0]["schema_sha256"]="9"*64; drift["probe"]["inventory_sha256"]=visual.sha256_bytes(visual.canonical_json(drift["probe"]["providers"]))
            with self.assertRaisesRegex(visual.VisualDecisionError,"lock identity"): visual.validate_affinity_observation(drift,root,project_root=project,run_id="run-a",now=NOW)
            # The lock pins the provider's *identity*, and the origin decision
            # of r1j leans on exactly this conjunct: the observation schema lets
            # any ``https://`` host through, so the only thing that stops a
            # foreign origin from being adopted is that the immutable lock has
            # to carry the very same string.  Until r1j that conjunct was
            # unmeasured - the tools drift above kills a mutant on ``tools``,
            # not one on ``origin``.  One case per pinned field.
            for field,other in (("origin","https://elsewhere.invalid/a"),("server_id","b"),("version","2")):
                with self.subTest(pinned=field):
                    forged=copy.deepcopy(lock); forged[field]=other; forged_binding=canonical_write(root,"affinity.forged.json",forged)
                    moved=copy.deepcopy(obs); moved["lock"]={**forged_binding,"server_id":obs["lock"]["server_id"]}
                    with self.assertRaisesRegex(visual.VisualDecisionError,"lock identity"):
                        visual.validate_affinity_observation(moved,root,project_root=project,run_id="run-a",now=NOW)
            # The inventory hash binds the provider list the status is derived
            # from; without it the list may be rewritten under a stale digest.
            for tamper in (lambda value: value["probe"].__setitem__("inventory_sha256","0"*64),
                           lambda value: value["probe"]["providers"][0].__setitem__("version","2"),
                           lambda value: value["probe"]["providers"][0].__setitem__("origin","https://swapped.invalid/a")):
                with self.subTest(inventory="rewritten"):
                    loosened=copy.deepcopy(obs); tamper(loosened)
                    with self.assertRaisesRegex(visual.VisualDecisionError,"Affinity inventory hash differs"):
                        visual.validate_affinity_observation(loosened,root,project_root=project,run_id="run-a",now=NOW)
        finally: temp.cleanup()

    def test_the_affinity_observation_is_decided_by_its_shipped_schema(self):
        """The schema binding itself, and the origin pattern it carries.

        ``validate_affinity_observation`` opens with ``validate_schema`` and the
        core suite never noticed: removing that call left every test green, so
        the pattern the origin decision rests on was a file nothing applied.
        The cases below are chosen so that *only* the schema can refuse them -
        an origin the pattern rejects reaches no other rule, because a probe
        with no matching server returns before the lock is ever read.

        The accepted column is deliberately as loud as the refused one.  It
        records what the pattern really promises: the http branch is closed
        against prefix and userinfo spellings, and the ``https://`` branch is a
        blank cheque - ``https://evil.invalid/`` passes.  That is the shipped
        rule, measured rather than assumed; the containment for it is the lock
        pin above, not this pattern.
        """
        temp,project,root=self.project()
        try:
            def observation(origin):
                providers=[{"server_id":"a","origin":origin,"version":"1","tools":[{"name":"export_svg","schema_sha256":"1"*64}]}]
                return unavailable(project,"run-a",matches=[],providers=providers)
            accepted=("https://example.invalid/a","http://127.0.0.1:6767/sse","http://[::1]:6767/sse",
                      "http://localhost:6767/sse","http://127.0.0.1:06767/sse","http://127.0.0.1:0/",
                      "https://evil.invalid/sse")          # register point: the https branch is unbounded
            refused=("http://127.0.0.1.evil.invalid/sse","http://127.0.0.1.evil.invalid:80/sse",
                     "http://127.0.0.1:6767@evil.invalid/sse","http://127.0.0.1:@evil.invalid/sse",
                     "http://[::1]:1@evil.invalid/sse","http://evil.invalid/sse","http://127.0.0.1/sse",
                     "http://127.0.0.1:666666/","HTTP://127.0.0.1:6767/","http://[0:0:0:0:0:0:0:1]:6767/",
                     "http://[::ffff:127.0.0.1]:6767/","http://2130706433:6767/","ftp://127.0.0.1:6767/",
                     "file:///etc/passwd","javascript:alert(1)","")
            for origin in accepted:
                with self.subTest(accepted=origin):
                    visual.validate_affinity_observation(observation(origin),root,project_root=project,run_id="run-a",now=NOW)
            for origin in refused:
                with self.subTest(refused=origin):
                    with self.assertRaisesRegex(visual.VisualDecisionError,"origin"):
                        visual.validate_affinity_observation(observation(origin),root,project_root=project,run_id="run-a",now=NOW)
            # Two more shapes only the schema decides: a field the probe has no
            # place for, and a provider that offers no tool at all.
            extra=observation("https://example.invalid/a"); extra["probe"]["extra"]=1
            with self.assertRaisesRegex(visual.VisualDecisionError,"probe"):
                visual.validate_affinity_observation(extra,root,project_root=project,run_id="run-a",now=NOW)
            tools=observation("https://example.invalid/a"); tools["probe"]["providers"][0]["tools"]=[]
            tools["probe"]["inventory_sha256"]=visual.sha256_bytes(visual.canonical_json(tools["probe"]["providers"]))
            with self.assertRaisesRegex(visual.VisualDecisionError,"tools"):
                visual.validate_affinity_observation(tools,root,project_root=project,run_id="run-a",now=NOW)

            # The read-only rule is decided *twice* - once by the schema's
            # ``const`` and once by the validator's own line.  A second layer is
            # only observable if the first one passes, and the honest way to
            # make it pass without substituting anything is an input that
            # answers the question twice: a probe whose ``mode`` reads
            # "read-only" for the schema and "read-write" for the code below it.
            # This is the same class the module already reasons about for a
            # mutable ``__fspath__``; here it is what shows that the second
            # layer exists at all.
            class Flipping(dict):
                reads=0
                def __getitem__(self,key):
                    if key=="mode":
                        Flipping.reads+=1
                        return "read-only" if Flipping.reads==1 else "read-write"
                    return super().__getitem__(key)
            flipping=observation("https://example.invalid/a"); flipping["probe"]=Flipping(flipping["probe"])
            with self.assertRaisesRegex(visual.VisualDecisionError,"must remain read-only"):
                visual.validate_affinity_observation(flipping,root,project_root=project,run_id="run-a",now=NOW)
            self.assertEqual(2,Flipping.reads,"the mode is read by the schema and by the code, in that order")
            # And the first layer answers the same question on its own.
            plain=observation("https://example.invalid/a"); plain["probe"]["mode"]="read-write"
            with self.assertRaisesRegex(visual.VisualDecisionError,"probe.mode"):
                visual.validate_affinity_observation(plain,root,project_root=project,run_id="run-a",now=NOW)
        finally: temp.cleanup()

    def test_bundle_cross_bindings_and_recursive_normative_rejection(self):
        temp,project,root=self.project()
        try:
            handoff=write(root/"interview-handoff.json","{}")
            source_path=visual.ROOT/"visual-decision-sources.v1.json"; review_binding={"path":source_path.name,"sha256":visual.sha256_bytes(source_path.read_bytes())}
            brief={"schema_version":"brandkit-visual-brief.v1","brief_id":"brief-a","project_identity_sha256":visual.project_identity(project),"run_id":"run-a","brand_id":"brand-a","interview_handoff":handoff,"open_decisions":["color"],"locale":"de-DE","viewports":[320,768,1440],"offline":True,"normative":False}; canonical_write(root,"visual-brief.json",brief)
            candidate={"schema_version":"brandkit-visual-candidate-set.v1","brief_sha256":visual.sha256_bytes(visual.canonical_json(brief)),"source_review":review_binding,"status":"partial","candidates":[{"id":"color-a","kind":"color","label":"Palette A","value":{"domain":"color","roles":palette(),"report":{"path":"color-report.json","sha256":"0"*64}},"value_sha256":"0"*64,"source_ids":["impeccable"],"status":"candidate"}],"recommendations":[{"kind":"color","candidate_id":"color-a","rationale":"Objective contrast checks pass.","source_ids":["impeccable"]}],"normative":False}; candidate["candidates"][0]["value_sha256"]=visual.sha256_bytes(visual.canonical_json(candidate["candidates"][0]["value"])); context=visual.candidate_context_sha256(candidate); report=visual.color_contrast_report(context,"color-a",palette(),["text","icon"]); report_binding=canonical_write(root,"color-report.json",report); candidate["candidates"][0]["value"]["report"]=report_binding; candidate["candidates"][0]["value_sha256"]=visual.sha256_bytes(visual.canonical_json(candidate["candidates"][0]["value"])); canonical_write(root,"candidate-set.json",candidate); canonical_write(root,"affinity-observation.json",unavailable(project,"run-a"))
            result=visual.validate_bundle(project,"run-a",NOW,sidecar_root=visual.ROOT.parents[1]); self.assertEqual(["color-a"],[item["id"] for item in result["eligible_candidates"]]); self.assertEqual(visual.sha256_bytes((root/"candidate-set.json").read_bytes()),result["candidate_set_sha256"])
            evil=copy.deepcopy(candidate); evil["candidates"][0]["value"]["approved"]={"owner":True}; evil["candidates"][0]["value_sha256"]=visual.sha256_bytes(visual.canonical_json(evil["candidates"][0]["value"])); canonical_write(root,"candidate-set.json",evil)
            with self.assertRaises(visual.VisualDecisionError): visual.validate_bundle(project,"run-a",NOW,sidecar_root=visual.ROOT.parents[1])
            fake=copy.deepcopy(candidate); fake["source_review"]={"path":"source-review.json","sha256":"0"*64}; canonical_write(root,"candidate-set.json",fake)
            with self.assertRaisesRegex(visual.VisualDecisionError,"release-bound"): visual.validate_bundle(project,"run-a",NOW,sidecar_root=visual.ROOT.parents[1])
        finally: temp.cleanup()

    def test_sheet_validator_parses_csp_semantics_urls_assets_and_preselection(self):
        temp,_,root=self.project()
        try:
            sheet=root/"sheet"; sheet.mkdir(); (sheet/"assets").mkdir(); (sheet/"assets/a.svg").write_text("svg")
            css='@font-face{src:url("assets/a.svg")}'; html=f'<!doctype html><html lang="de"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="{visual.SHEET_CSP}"><link rel="stylesheet" href="style.css"><title>x</title></head><body><main>'+''.join(f'<section id="{name}"><h2>x</h2></section>' for name in ("candidates-color","candidates-typography","candidates-code-font","candidates-logo","candidates-lockup","recommendations","owner-review"))+'</main></body></html>'; (sheet/"style.css").write_text(css); (sheet/"index.html").write_text(html)
            def manifest():
                files=[]
                for path in (sheet/"index.html",sheet/"style.css",sheet/"assets/a.svg"): files.append({"path":path.relative_to(sheet).as_posix(),"sha256":visual.sha256_bytes(path.read_bytes())})
                return {"schema_version":"brandkit-visual-sheet-manifest.v1","brief_sha256":"a"*64,"candidate_set_sha256":"b"*64,"affinity_observation_sha256":"c"*64,"files":files,"viewports":[320,768,1440],"offline":True,"interactive_controls":False,"preselection":False,"normative":False,"human_review_status":"required"}
            visual.validate_sheet_manifest(manifest(),sheet)
            for injection in ('<script src="assets/a.svg"></script>','<form><input checked></form>','<img src="https://evil.invalid/x">','<div style="color:red">x</div>'):
                (sheet/"index.html").write_text(html.replace('</main>',injection+'</main>'))
                with self.assertRaises(visual.VisualDecisionError): visual.validate_sheet_manifest(manifest(),sheet)
            (sheet/"index.html").write_text(html); (sheet/"style.css").write_text('@import "https://evil.invalid/x";')
            with self.assertRaises(visual.VisualDecisionError): visual.validate_sheet_manifest(manifest(),sheet)
            for attack,reason in ((r'a{background:url(assets/a.svg)}@font-face{src:u\72l("h\74tps://evil.invalid/x")}',"network or active content"),(r'a{background:url(assets/a.svg)}b{background:u/**/rl(https://evil.invalid/x)}',"network or active content"),('a{background:url(assets/a.svg)}@import "https://evil.invalid/x";',"network or active content"),(r'a{background:url(assets/a.svg)}b{content:"\110000"}',"invalid escape"),(r'a{background:url(assets/a.svg)}b{content:"\D800"}',"invalid escape"),('a{background:url(assets/a.svg)}b{content:"'+"\\"*16+'"}',"undecodable escape")):
                (sheet/"style.css").write_text(attack)
                with self.assertRaisesRegex(visual.VisualDecisionError,reason): visual.validate_sheet_manifest(manifest(),sheet)
        finally: temp.cleanup()

    def test_persisted_replay_ledger_blocks_a_second_independent_process(self):
        temp,project,root=self.project(); secrets=Path(tempfile.mkdtemp()).resolve()
        try:
            key=b"z"*32; key_path=secrets/"owner.key"; key_path.write_bytes(key); key_path.chmod(0o600)
            ledger_key=visual._derive_key(key,b"brandkit-affinity-replay-ledger-v1")
            genesis={"schema_version":"brandkit-affinity-replay-ledger.v1","project_identity_sha256":visual.project_identity(project),"run_id":"run-a","generation":0,"predecessor_sha256":"0"*64,"nonces":[],"key_id":visual.sha256_bytes(ledger_key)[:16]}
            genesis_raw=visual.canonical_json({**genesis,"hmac_sha256":hmac.new(ledger_key,visual.canonical_json(genesis),hashlib.sha256).hexdigest()})
            ledger_path=secrets/"replay-ledger.json"; ledger_path.write_bytes(genesis_raw); ledger_path.chmod(0o600)
            handoff=write(root/"interview-handoff.json","{}"); source_path=visual.ROOT/"visual-decision-sources.v1.json"; review_binding={"path":source_path.name,"sha256":visual.sha256_bytes(source_path.read_bytes())}
            brief={"schema_version":"brandkit-visual-brief.v1","brief_id":"brief-a","project_identity_sha256":visual.project_identity(project),"run_id":"run-a","brand_id":"brand-a","interview_handoff":handoff,"open_decisions":["logo","lockup"],"locale":"de-DE","viewports":[320,768,1440],"offline":True,"normative":False}; canonical_write(root,"visual-brief.json",brief)
            provider={"server_id":"a","origin":"https://example.invalid/a","version":"1","tools":[{"name":"export_svg","schema_sha256":"1"*64}]}
            lock={"schema_version":"brandkit-affinity-mcp-lock.v1","server_id":"a","origin":provider["origin"],"version":"1","license_spdx":"MIT","source_revision":"3"*40,"source_sha256":"4"*64,"tools":provider["tools"]}; lock_binding=canonical_write(root,"affinity.lock.json",lock)
            obs=unavailable(project,"run-a",matches=["a"],providers=[provider]); obs.update(status="available",lock={**lock_binding,"server_id":"a"},mutation_capable=True,blocker=None); canonical_write(root,"affinity-observation.json",obs)
            owner_key=domain_key(key,OWNER_DOMAIN); owner_raw={"schema_version":"brandkit-discovery-owner-receipt.v1","key_id":visual.sha256_bytes(owner_key)[:16],"payload":{"decision":"graphics-mutate","project_identity_sha256":visual.project_identity(project),"run_id":"run-a","document_id":"logo-doc"}}; owner_raw["hmac_sha256"]=hmac.new(owner_key,visual.canonical_json(owner_raw),hashlib.sha256).hexdigest(); owner=canonical_write(root,"owner.json",owner_raw)
            write(root/"input.json","input"); input_binding={"path":"input.json","sha256":visual.sha256_bytes(b"input")}; (root/"staging/logo-doc").mkdir(parents=True); report_candidates=[]
            for variant in ("mark","lockup-horizontal","lockup-stacked","favicon"):
                ident=variant.replace("lockup-","")+"-a"; asset_id="fixture-"+ident; relative=f"staging/logo-doc/{ident}.svg"
                raw=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><title>{asset_id}</title><rect x="2" y="2" width="60" height="60" fill="#111111"/></svg>'.encode(); (root/relative).write_bytes(raw)
                report_candidates.append({"id":ident,"asset_id":asset_id,"variant":variant,"asset":{"path":relative,"sha256":visual.sha256_bytes(raw)},"creation_origin":"affinity-mcp","minimum_width_px":16,"test_sizes_px":[16,24,32,48,64,128],"objective_checks":visual._svg_static_checks(raw,ident,asset_id),"human_reviews":{name:"required" for name in ("size-16","size-24","size-32","size-48","size-64","size-128","minimum-width","monochrome","clipping","legibility")}})
            placeholder={"path":"logo-report.json","sha256":"0"*64}
            logo_value={"domain":"logo","logo_candidate_ids":["mark-a","favicon-a"],"creation_origin":"affinity-mcp","report":dict(placeholder)}
            lockup_value={"domain":"lockup","lockup_candidate_ids":["horizontal-a","stacked-a"],"creation_origin":"affinity-mcp","report":dict(placeholder)}
            candidate_set={"schema_version":"brandkit-visual-candidate-set.v1","brief_sha256":visual.sha256_bytes(visual.canonical_json(brief)),"source_review":review_binding,"status":"partial","candidates":[{"id":"logo-choice","kind":"logo","label":"Logo A","value":logo_value,"value_sha256":visual.sha256_bytes(visual.canonical_json(logo_value)),"source_ids":["impeccable"],"status":"candidate"},{"id":"lockup-choice","kind":"lockup","label":"Lockup A","value":lockup_value,"value_sha256":visual.sha256_bytes(visual.canonical_json(lockup_value)),"source_ids":["impeccable"],"status":"candidate"}],"recommendations":[],"normative":False}
            report={"schema_version":"brandkit-logo-test-report.v1","candidate_set_sha256":visual.candidate_context_sha256(candidate_set),"affinity_observation_sha256":visual.sha256_bytes(visual.canonical_json(obs)),"candidates":report_candidates,"objective_status":"pass","human_review_status":"required"}; report_binding=canonical_write(root,"logo-report.json",report)
            for value in (logo_value,lockup_value): value["report"]=dict(report_binding)
            for entry in candidate_set["candidates"]: entry["value_sha256"]=visual.sha256_bytes(visual.canonical_json(entry["value"]))
            canonical_write(root,"candidate-set.json",candidate_set)
            operation_key=visual._derive_key(key,b"brandkit-affinity-operation-v1"); receipts={}; nonces=[]
            for item in report_candidates:
                nonce=hashlib.sha256(("nonce-"+item["id"]).encode()).hexdigest(); nonces.append(nonce)
                receipt={"schema_version":"brandkit-affinity-mutation-receipt.v1","issuer":"fixture","key_id":visual.sha256_bytes(operation_key)[:16],"capability":"graphics-mutate","project_identity_sha256":visual.project_identity(project),"run_id":"run-a","document_id":"logo-doc","staging_root":".brandkit/work/run-a/visual-decisions/staging/logo-doc","observation_sha256":visual.sha256_bytes(visual.canonical_json(obs)),"server_lock_sha256":lock_binding["sha256"],"owner_approval":owner,"tool":provider["tools"][0],"operation":"export-svg","inputs":[input_binding],"outputs":[{"candidate_id":item["id"],**item["asset"]}],"issued_at":NOW-1,"expires_at":NOW+1,"nonce":nonce}
                receipt["hmac_sha256"]=hmac.new(operation_key,visual.canonical_json(receipt),hashlib.sha256).hexdigest(); receipts[item["id"]]=canonical_write(root,f"receipt-{item['id']}.json",receipt)
            auth_path=secrets/"affinity-auth.json"; auth_path.write_text(json.dumps({"owner_key_path":str(key_path),"replay_ledger_path":str(ledger_path),"receipts":receipts}))
            def in_separate_process() -> dict:
                done=subprocess.run([sys.executable,"-c",REPLAY_RUNNER,str(ROOT/"scripts"),str(project),"run-a",str(NOW),str(visual.ROOT.parents[1]),str(auth_path)],capture_output=True,text=True)
                self.assertEqual(0,done.returncode,done.stderr); line=next(item for item in done.stdout.splitlines() if item.startswith("RESULT:")); return json.loads(line[len("RESULT:"):])
            first=in_separate_process(); self.assertTrue(first["ok"],first); self.assertEqual(["lockup-choice","logo-choice"],first["eligible"])
            committed=ledger_path.read_bytes(); state=json.loads(committed)
            self.assertEqual(1,state["generation"]); self.assertEqual(visual.sha256_bytes(genesis_raw),state["predecessor_sha256"]); self.assertEqual(sorted(nonces),state["nonces"])
            self.assertEqual(0o600,stat.S_IMODE(ledger_path.stat().st_mode))
            second=in_separate_process(); self.assertFalse(second["ok"],second); self.assertIn("already consumed",second["error"]); self.assertNotEqual(first["pid"],second["pid"])
            replayed=ledger_path.read_bytes(); after=json.loads(replayed)
            self.assertEqual(committed,replayed); self.assertEqual(1,after["generation"]); self.assertEqual(visual.sha256_bytes(genesis_raw),after["predecessor_sha256"])
            self.assertEqual(len(after["nonces"]),len(set(after["nonces"]))); self.assertEqual([],[name for name in secrets.iterdir() if name.name.startswith(".replay-ledger.json.")])
            visual.load_replay_ledger(ledger_path,key,project,"run-a")
            ledger_path.write_bytes(genesis_raw); ledger_path.chmod(0o600); control=in_separate_process()
            self.assertTrue(control["ok"],control); self.assertNotIn(control["pid"],{first["pid"],second["pid"]}); self.assertEqual(sorted(nonces),json.loads(ledger_path.read_bytes())["nonces"])
        finally: shutil.rmtree(secrets,ignore_errors=True); temp.cleanup()


# Executed by separate interpreters that all start from the same genesis ledger.  The
# commit window is widened by argv[7] seconds so that an unsynchronised read-modify-write
# would demonstrably double-spend instead of only occasionally losing the race.  argv[8]
# picks how the caller spells the two external paths of the descriptor: the annotation is
# ``dict[str, Any]`` and the rest of the API hands out ``Path`` objects, so both spellings
# are inside the contract and both have to serialise.
CONCURRENT_RUNNER = REPLAY_RUNNER.replace("import brandkit_visual_decisions as visual", """import brandkit_visual_decisions as visual
import time as _time
_replace = os.replace
def _slow_replace(*args, **kwargs):
    _time.sleep(float(sys.argv[7]))
    return _replace(*args, **kwargs)
os.replace = _slow_replace
def _spell(auth):
    if sys.argv[8] != "path":
        return auth
    return {**auth, "owner_key_path": Path(auth["owner_key_path"]), "replay_ledger_path": Path(auth["replay_ledger_path"])}""").replace(
    "affinity_auth=json.loads(Path(sys.argv[6]).read_text())", "affinity_auth=_spell(json.loads(Path(sys.argv[6]).read_text()))")

LEDGER_DOMAIN = b"brandkit-affinity-replay-ledger-v1"
OPERATION_DOMAIN = b"brandkit-affinity-operation-v1"
OWNER_DOMAIN = b"brandkit-affinity-owner-approval-v1"


def domain_key(key: bytes, domain: bytes) -> bytes:
    """Derived without the module under test, so collapsing its HMAC domains fails here."""
    return hmac.new(key, domain, hashlib.sha256).digest()


def signed_ledger(key: bytes, identity: str, run_id: str, generation: int, predecessor: str, nonces: list[str]) -> bytes:
    ledger_key = domain_key(key, LEDGER_DOMAIN)
    body = {"schema_version":"brandkit-affinity-replay-ledger.v1","project_identity_sha256":identity,"run_id":run_id,"generation":generation,"predecessor_sha256":predecessor,"nonces":sorted(nonces),"key_id":visual.sha256_bytes(ledger_key)[:16]}
    return visual.canonical_json({**body,"hmac_sha256":hmac.new(ledger_key,visual.canonical_json(body),hashlib.sha256).hexdigest()})


def write_receipt(root: Path, document: dict, signing_key: bytes) -> dict:
    body = {name:value for name,value in document.items() if name != "hmac_sha256"}
    body["hmac_sha256"] = hmac.new(signing_key,visual.canonical_json(body),hashlib.sha256).hexdigest()
    return canonical_write(root, f"receipt-{document['outputs'][0]['candidate_id']}.json", body)


def owner_approval(root: Path, name: str, key: bytes, identity: str, run_id: str, document_id: str) -> dict:
    owner_key = domain_key(key, OWNER_DOMAIN)
    body = {"schema_version":"brandkit-discovery-owner-receipt.v1","key_id":visual.sha256_bytes(owner_key)[:16],"payload":{"decision":"graphics-mutate","project_identity_sha256":identity,"run_id":run_id,"document_id":document_id}}
    body["hmac_sha256"] = hmac.new(owner_key,visual.canonical_json(body),hashlib.sha256).hexdigest()
    return canonical_write(root,name,body)


def color_bundle(project: Path, root: Path, run_id: str = "run-a") -> dict:
    handoff=write(root/"interview-handoff.json","{}"); source_path=visual.ROOT/"visual-decision-sources.v1.json"
    brief={"schema_version":"brandkit-visual-brief.v1","brief_id":"brief-a","project_identity_sha256":visual.project_identity(project),"run_id":run_id,"brand_id":"brand-a","interview_handoff":handoff,"open_decisions":["color"],"locale":"de-DE","viewports":[320,768,1440],"offline":True,"normative":False}; canonical_write(root,"visual-brief.json",brief)
    value={"domain":"color","roles":palette(),"report":{"path":"color-report.json","sha256":"0"*64}}
    document={"schema_version":"brandkit-visual-candidate-set.v1","brief_sha256":visual.sha256_bytes(visual.canonical_json(brief)),"source_review":{"path":source_path.name,"sha256":visual.sha256_bytes(source_path.read_bytes())},"status":"partial","candidates":[{"id":"color-a","kind":"color","label":"Palette A","value":value,"value_sha256":visual.sha256_bytes(visual.canonical_json(value)),"source_ids":["impeccable"],"status":"candidate"}],"recommendations":[],"normative":False}
    report=visual.color_contrast_report(visual.candidate_context_sha256(document),"color-a",palette(),["text","icon"])
    value["report"]=canonical_write(root,"color-report.json",report); document["candidates"][0]["value_sha256"]=visual.sha256_bytes(visual.canonical_json(value))
    canonical_write(root,"candidate-set.json",document); canonical_write(root,"affinity-observation.json",unavailable(project,run_id))
    return document


def affinity_bundle(project: Path, root: Path, secrets: Path, *, run_id: str = "run-a", key: bytes = b"z"*32) -> dict:
    """A complete Affinity-authored bundle plus every external handle a test needs to tamper with."""
    identity=visual.project_identity(project)
    key_path=secrets/"owner.key"; key_path.write_bytes(key); key_path.chmod(0o600)
    genesis_raw=signed_ledger(key,identity,run_id,0,"0"*64,[]); ledger_path=secrets/"replay-ledger.json"; ledger_path.write_bytes(genesis_raw); ledger_path.chmod(0o600)
    handoff=write(root/"interview-handoff.json","{}"); source_path=visual.ROOT/"visual-decision-sources.v1.json"; review_binding={"path":source_path.name,"sha256":visual.sha256_bytes(source_path.read_bytes())}
    brief={"schema_version":"brandkit-visual-brief.v1","brief_id":"brief-a","project_identity_sha256":identity,"run_id":run_id,"brand_id":"brand-a","interview_handoff":handoff,"open_decisions":["logo","lockup"],"locale":"de-DE","viewports":[320,768,1440],"offline":True,"normative":False}; canonical_write(root,"visual-brief.json",brief)
    provider={"server_id":"a","origin":"https://example.invalid/a","version":"1","tools":[{"name":"export_svg","schema_sha256":"1"*64}]}
    lock={"schema_version":"brandkit-affinity-mcp-lock.v1","server_id":"a","origin":provider["origin"],"version":"1","license_spdx":"MIT","source_revision":"3"*40,"source_sha256":"4"*64,"tools":provider["tools"]}; lock_binding=canonical_write(root,"affinity.lock.json",lock)
    obs=unavailable(project,run_id,matches=["a"],providers=[provider]); obs.update(status="available",lock={**lock_binding,"server_id":"a"},mutation_capable=True,blocker=None); canonical_write(root,"affinity-observation.json",obs)
    owner=owner_approval(root,"owner.json",key,identity,run_id,"logo-doc")
    write(root/"input.json","input"); input_binding={"path":"input.json","sha256":visual.sha256_bytes(b"input")}; (root/"staging/logo-doc").mkdir(parents=True); report_candidates=[]
    for variant in ("mark","lockup-horizontal","lockup-stacked","favicon"):
        ident=variant.replace("lockup-","")+"-a"; asset_id="fixture-"+ident; relative=f"staging/logo-doc/{ident}.svg"
        raw=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><title>{asset_id}</title><rect x="2" y="2" width="60" height="60" fill="#111111"/></svg>'.encode(); (root/relative).write_bytes(raw)
        report_candidates.append({"id":ident,"asset_id":asset_id,"variant":variant,"asset":{"path":relative,"sha256":visual.sha256_bytes(raw)},"creation_origin":"affinity-mcp","minimum_width_px":16,"test_sizes_px":[16,24,32,48,64,128],"objective_checks":visual._svg_static_checks(raw,ident,asset_id),"human_reviews":{name:"required" for name in ("size-16","size-24","size-32","size-48","size-64","size-128","minimum-width","monochrome","clipping","legibility")}})
    placeholder={"path":"logo-report.json","sha256":"0"*64}
    logo_value={"domain":"logo","logo_candidate_ids":["mark-a","favicon-a"],"creation_origin":"affinity-mcp","report":dict(placeholder)}
    lockup_value={"domain":"lockup","lockup_candidate_ids":["horizontal-a","stacked-a"],"creation_origin":"affinity-mcp","report":dict(placeholder)}
    candidate_set={"schema_version":"brandkit-visual-candidate-set.v1","brief_sha256":visual.sha256_bytes(visual.canonical_json(brief)),"source_review":review_binding,"status":"partial","candidates":[{"id":"logo-choice","kind":"logo","label":"Logo A","value":logo_value,"value_sha256":visual.sha256_bytes(visual.canonical_json(logo_value)),"source_ids":["impeccable"],"status":"candidate"},{"id":"lockup-choice","kind":"lockup","label":"Lockup A","value":lockup_value,"value_sha256":visual.sha256_bytes(visual.canonical_json(lockup_value)),"source_ids":["impeccable"],"status":"candidate"}],"recommendations":[],"normative":False}
    report={"schema_version":"brandkit-logo-test-report.v1","candidate_set_sha256":visual.candidate_context_sha256(candidate_set),"affinity_observation_sha256":visual.sha256_bytes(visual.canonical_json(obs)),"candidates":report_candidates,"objective_status":"pass","human_review_status":"required"}; report_binding=canonical_write(root,"logo-report.json",report)
    for value in (logo_value,lockup_value): value["report"]=dict(report_binding)
    for entry in candidate_set["candidates"]: entry["value_sha256"]=visual.sha256_bytes(visual.canonical_json(entry["value"]))
    canonical_write(root,"candidate-set.json",candidate_set)
    operation_key=domain_key(key,OPERATION_DOMAIN); documents={}; receipts={}; nonces=[]
    for item in report_candidates:
        nonce=hashlib.sha256(("nonce-"+item["id"]).encode()).hexdigest(); nonces.append(nonce)
        documents[item["id"]]={"schema_version":"brandkit-affinity-mutation-receipt.v1","issuer":"fixture","key_id":visual.sha256_bytes(operation_key)[:16],"capability":"graphics-mutate","project_identity_sha256":identity,"run_id":run_id,"document_id":"logo-doc","staging_root":f".brandkit/work/{run_id}/visual-decisions/staging/logo-doc","observation_sha256":visual.sha256_bytes(visual.canonical_json(obs)),"server_lock_sha256":lock_binding["sha256"],"owner_approval":owner,"tool":provider["tools"][0],"operation":"export-svg","inputs":[input_binding],"outputs":[{"candidate_id":item["id"],**item["asset"]}],"issued_at":NOW-1,"expires_at":NOW+1,"nonce":nonce}
        receipts[item["id"]]=write_receipt(root,documents[item["id"]],operation_key)
    return {"key":key,"identity":identity,"run_id":run_id,"key_path":key_path,"ledger_path":ledger_path,"genesis_raw":genesis_raw,"auth":{"owner_key_path":str(key_path),"replay_ledger_path":str(ledger_path),"receipts":receipts},"documents":documents,"nonces":nonces,"operation_key":operation_key,"observation":obs,"owner":owner}


class VisualDecisionCoreR5Tests(TimeLimitedTestCase):
    """Each of these measures one literal promise of the acceptance bar directly."""

    def project(self) -> tuple[Path, Path]:
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup); project=Path(temp.name).resolve()
        return project, visual.visual_workspace(project,"run-a",create=True)

    def affinity(self) -> tuple[Path, Path, Path, dict]:
        project,root=self.project(); secrets=Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree,secrets,True)
        return project,root,secrets,affinity_bundle(project,root,secrets)

    def bundle(self, project: Path, auth: dict | None = None, sidecar: Path | None = None) -> dict:
        return visual.validate_bundle(project,"run-a",NOW,sidecar_root=sidecar or visual.ROOT.parents[1],affinity_auth=auth)

    # --- acceptance bar, point 1: source trust ---------------------------------

    def test_bundle_fails_when_the_independent_source_validator_reports_an_error(self):
        project,root=self.project(); color_bundle(project,root)
        self.assertEqual(["color-a"],[item["id"] for item in self.bundle(project)["eligible_candidates"]])
        with tempfile.TemporaryDirectory() as detached:
            with self.assertRaisesRegex(visual.VisualDecisionError,"independent visual source contract failed"):
                self.bundle(project,sidecar=Path(detached).resolve())

    def test_every_candidate_must_cite_exactly_the_selected_dependency(self):
        project,root=self.project(); document=color_bundle(project,root)
        selected=visual.validate_source_contract(document["source_review"],visual.ROOT.parents[1])["selected_dependency"]
        self.assertEqual([selected],document["candidates"][0]["source_ids"])
        for source_ids in ([selected,"invented-source"],["invented-source"],["a-source","b-source"]):
            broken=copy.deepcopy(document); broken["candidates"][0]["source_ids"]=source_ids; canonical_write(root,"candidate-set.json",broken)
            with self.assertRaisesRegex(visual.VisualDecisionError,"candidate source coverage differs"): self.bundle(project)

    # --- acceptance bar, point 2: affinity mutation ----------------------------

    def test_replay_ledger_must_be_authentic_scoped_0600_and_outside_the_project(self):
        project,root,secrets,fixture=self.affinity(); auth=fixture["auth"]; ledger_path=fixture["ledger_path"]
        def restore(raw: bytes | None = None) -> None: ledger_path.write_bytes(raw or fixture["genesis_raw"]); ledger_path.chmod(0o600)
        forged=json.loads(fixture["genesis_raw"]); forged["hmac_sha256"]="0"*64
        raw_key_ledger=json.loads(fixture["genesis_raw"]); raw_key_ledger["key_id"]=visual.sha256_bytes(fixture["key"])[:16]
        raw_key_ledger["hmac_sha256"]=hmac.new(fixture["key"],visual.canonical_json({name:value for name,value in raw_key_ledger.items() if name!="hmac_sha256"}),hashlib.sha256).hexdigest()
        for raw,reason in ((visual.canonical_json(forged),"authentication/scope failed"),(visual.canonical_json(raw_key_ledger),"authentication/scope failed"),(signed_ledger(fixture["key"],visual.sha256_bytes(b"another-project"),"run-a",0,"0"*64,[]),"authentication/scope failed"),(signed_ledger(fixture["key"],fixture["identity"],"run-b",0,"0"*64,[]),"authentication/scope failed"),(signed_ledger(b"y"*32,fixture["identity"],"run-a",0,"0"*64,[]),"authentication/scope failed"),(signed_ledger(fixture["key"],fixture["identity"],"run-a",0,"1"*64,[]),"genesis differs")):
            restore(raw)
            with self.assertRaisesRegex(visual.VisualDecisionError,reason): self.bundle(project,auth)
        restore(); ledger_path.chmod(0o644)
        with self.assertRaisesRegex(visual.VisualDecisionError,"exclusive 0600 regular file"): self.bundle(project,auth)
        inside_key=project/"owner.key"; inside_key.write_bytes(fixture["key"]); inside_key.chmod(0o600)
        inside_ledger=project/"replay-ledger.json"; inside_ledger.write_bytes(fixture["genesis_raw"]); inside_ledger.chmod(0o600)
        for descriptor in ({**auth,"owner_key_path":str(inside_key)},{**auth,"replay_ledger_path":str(inside_ledger)}):
            restore()
            with self.assertRaisesRegex(visual.VisualDecisionError,"must remain outside the project"): self.bundle(project,descriptor)
        restore()
        self.assertEqual(["lockup-choice","logo-choice"],sorted(item["id"] for item in self.bundle(project,auth)["eligible_candidates"]))
        committed=json.loads(ledger_path.read_bytes())
        self.assertEqual(1,committed["generation"]); self.assertEqual(sorted(fixture["nonces"]),committed["nonces"])
        self.assertEqual(visual.sha256_bytes(fixture["genesis_raw"]),committed["predecessor_sha256"])
        self.assertEqual(0o600,stat.S_IMODE(ledger_path.stat().st_mode))
        self.assertEqual([],[name.name for name in project.rglob("*") if name.name.startswith(".affinity-lock.")])

    def test_affinity_descriptor_receipts_and_hmac_domains_are_closed_and_separated(self):
        project,root,secrets,fixture=self.affinity(); auth=fixture["auth"]
        def restore() -> None: fixture["ledger_path"].write_bytes(fixture["genesis_raw"]); fixture["ledger_path"].chmod(0o600)
        def respin(signing_key: bytes | None = None, **overrides) -> dict:
            signing_key=signing_key or fixture["operation_key"]
            return {**auth,"receipts":{candidate_id:write_receipt(root,{**document,**overrides,"key_id":visual.sha256_bytes(signing_key)[:16]},signing_key) for candidate_id,document in fixture["documents"].items()}}
        other_owner=owner_approval(root,"owner-other.json",fixture["key"],fixture["identity"],"run-a","other-doc")
        def ghost() -> dict:
            complete=respin(); return {**complete,"receipts":{**complete["receipts"],"ghost-a":complete["receipts"]["mark-a"]}}
        cases=[(lambda: {**respin(),"note":"extra"},"descriptor is not closed"),(lambda: {name:value for name,value in respin().items() if name!="replay_ledger_path"},"descriptor is not closed"),(lambda: {**respin(),"receipts":[]},"descriptor is not closed"),
               (ghost,"receipt inventory differs"),
               (lambda: respin(expires_at=NOW-2),"expired or future-dated"),(lambda: respin(staging_root=".brandkit/work/run-a/visual-decisions/staging/other-doc"),"staging/output root differs"),
               (lambda: respin(owner_approval=other_owner),"owner approval scope differs"),(lambda: respin(signing_key=domain_key(fixture["key"],LEDGER_DOMAIN)),"receipt authentication failed"),
               (lambda: respin(signing_key=fixture["key"]),"receipt authentication failed")]
        for build,reason in cases:
            restore(); descriptor=build()
            with self.assertRaisesRegex(visual.VisualDecisionError,reason): self.bundle(project,descriptor)
            self.assertEqual(fixture["genesis_raw"],fixture["ledger_path"].read_bytes())
        restore()
        self.assertEqual(["lockup-choice","logo-choice"],sorted(item["id"] for item in self.bundle(project,respin())["eligible_candidates"]))

    def test_a_commit_never_overwrites_a_ledger_another_writer_replaced(self):
        project,root,secrets,fixture=self.affinity()
        ledger,raw=visual.load_replay_ledger(fixture["ledger_path"],fixture["key"],project,"run-a")
        rival=signed_ledger(fixture["key"],fixture["identity"],"run-a",1,visual.sha256_bytes(raw),fixture["nonces"][:1])
        fixture["ledger_path"].write_bytes(rival); fixture["ledger_path"].chmod(0o600)
        with self.assertRaisesRegex(visual.VisualDecisionError,"changed before commit"):
            visual.persist_replay_ledger(fixture["ledger_path"],fixture["key"],ledger,raw,set(fixture["nonces"]))
        self.assertEqual(rival,fixture["ledger_path"].read_bytes())
        self.assertEqual([],[name.name for name in secrets.iterdir() if name.name.startswith(".replay-ledger.json.")])

    def test_concurrent_processes_never_consume_the_same_receipts_twice(self):
        project,root,secrets,fixture=self.affinity()
        auth_path=secrets/"affinity-auth.json"; auth_path.write_text(json.dumps(fixture["auth"]))
        for spelling in ("str","path"):
            fixture["ledger_path"].write_bytes(fixture["genesis_raw"]); fixture["ledger_path"].chmod(0o600)
            command=[sys.executable,"-c",CONCURRENT_RUNNER,str(ROOT/"scripts"),str(project),"run-a",str(NOW),str(visual.ROOT.parents[1]),str(auth_path),"1.0",spelling]
            processes=[subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(3)]
            results=[]
            for process in processes:
                out,err=process.communicate(); self.assertEqual(0,process.returncode,err)
                results.append(json.loads(next(line for line in out.splitlines() if line.startswith("RESULT:"))[len("RESULT:"):]))
            self.assertEqual(3,len({item["pid"] for item in results}),(spelling,results))
            self.assertEqual(1,len([item for item in results if item["ok"]]),(spelling,results))
            self.assertTrue(all("already consumed" in item["error"] for item in results if not item["ok"]),(spelling,results))
            committed=json.loads(fixture["ledger_path"].read_bytes())
            self.assertEqual(1,committed["generation"],spelling); self.assertEqual(sorted(fixture["nonces"]),committed["nonces"])

    def test_affinity_receipts_must_all_be_consumed_by_authored_candidates(self):
        project,root=self.project(); color_bundle(project,root); secrets=Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree,secrets,True)
        key=b"z"*32; key_path=secrets/"owner.key"; key_path.write_bytes(key); key_path.chmod(0o600)
        ledger_path=secrets/"replay-ledger.json"; ledger_path.write_bytes(signed_ledger(key,visual.project_identity(project),"run-a",0,"0"*64,[])); ledger_path.chmod(0o600)
        stray=write(root/"stray-receipt.json","{}")
        with self.assertRaisesRegex(visual.VisualDecisionError,"unused receipts"):
            self.bundle(project,{"owner_key_path":str(key_path),"replay_ledger_path":str(ledger_path),"receipts":{"color-a":stray}})

    def test_external_key_and_ledger_paths_fail_closed_as_contract_errors(self):
        project,root,secrets,fixture=self.affinity()
        linked_ledger=secrets/"linked-ledger.json"; linked_ledger.symlink_to(fixture["ledger_path"])
        linked_key=secrets/"linked-owner.key"; linked_key.symlink_to(fixture["key_path"])
        short_key=secrets/"short.key"; short_key.write_bytes(b"z"*31); short_key.chmod(0o600)
        linked_parent=Path(tempfile.mkdtemp()).resolve()/"secrets-link"; self.addCleanup(shutil.rmtree,linked_parent.parent,True); linked_parent.symlink_to(secrets,target_is_directory=True)
        for descriptor,reason in (({**fixture["auth"],"replay_ledger_path":str(linked_ledger)},"exclusive 0600 regular file"),({**fixture["auth"],"owner_key_path":str(linked_key)},"exclusive 0600 regular file"),({**fixture["auth"],"owner_key_path":str(short_key)},"exactly 32 bytes"),({**fixture["auth"],"replay_ledger_path":str(linked_parent/"replay-ledger.json")},"canonical")):
            with self.assertRaisesRegex(visual.VisualDecisionError,reason): self.bundle(project,descriptor)

    # --- acceptance bar, point 3: offline sheet --------------------------------

    def test_sheet_requires_the_closed_offline_content_security_policy(self):
        project,root=self.project(); sheet=root/"sheet"; (sheet/"assets").mkdir(parents=True); (sheet/"assets/a.svg").write_text("svg")
        page='<!doctype html><html lang="de"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="%s"><link rel="stylesheet" href="style.css"><title>x</title></head><body><main>'+''.join(f'<section id="{name}"><h2>x</h2></section>' for name in ("candidates-color","candidates-typography","candidates-code-font","candidates-logo","candidates-lockup","recommendations","owner-review"))+'</main></body></html>'
        (sheet/"style.css").write_text('@font-face{src:url("assets/a.svg")}')
        def manifest() -> dict:
            files=[{"path":path.relative_to(sheet).as_posix(),"sha256":visual.sha256_bytes(path.read_bytes())} for path in (sheet/"index.html",sheet/"style.css",sheet/"assets/a.svg")]
            return {"schema_version":"brandkit-visual-sheet-manifest.v1","brief_sha256":"a"*64,"candidate_set_sha256":"b"*64,"affinity_observation_sha256":"c"*64,"files":files,"viewports":[320,768,1440],"offline":True,"interactive_controls":False,"preselection":False,"normative":False,"human_review_status":"required"}
        (sheet/"index.html").write_text(page%visual.SHEET_CSP); visual.validate_sheet_manifest(manifest(),sheet)
        for policy in ("default-src 'self'","default-src *; img-src *",visual.SHEET_CSP.replace("'none'","'self'",1),visual.SHEET_CSP+"; script-src 'unsafe-inline'"):
            (sheet/"index.html").write_text(page%policy)
            with self.assertRaisesRegex(visual.VisualDecisionError,"CSP differs from the closed offline policy"): visual.validate_sheet_manifest(manifest(),sheet)


class VisualDecisionCoreR7Tests(TimeLimitedTestCase):
    """Properties the r6 review found unmeasured: descriptor typing, the fail-closed
    ledger lock, string-aware CSS canonicalisation, the receipt MAC on its own and the
    exact release binding of the source contract."""

    def project(self) -> tuple[Path, Path]:
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup); project=Path(temp.name).resolve()
        return project, visual.visual_workspace(project,"run-a",create=True)

    def affinity(self) -> tuple[Path, Path, Path, dict]:
        project,root=self.project(); secrets=Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree,secrets,True)
        return project,root,secrets,affinity_bundle(project,root,secrets)

    def bundle(self, project: Path, auth: object = None) -> dict:
        return visual.validate_bundle(project,"run-a",NOW,sidecar_root=visual.ROOT.parents[1],affinity_auth=auth)

    def locks(self, directory: Path) -> list[str]:
        return sorted(item.name for item in directory.iterdir() if item.name.startswith(".affinity-lock."))

    def sheet(self):
        """Return a callable that validates one style sheet inside a complete fixture."""
        _project,root=self.project(); sheet=root/"sheet"; (sheet/"assets").mkdir(parents=True); (sheet/"assets/a.svg").write_text("svg")
        sections=''.join(f'<section id="{name}"><h2>x</h2></section>' for name in ("candidates-color","candidates-typography","candidates-code-font","candidates-logo","candidates-lockup","recommendations","owner-review"))
        (sheet/"index.html").write_text('<!doctype html><html lang="de"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="'+visual.SHEET_CSP+'"><link rel="stylesheet" href="style.css"><title>x</title></head><body><main>'+sections+'</main></body></html>')
        def check(css: str) -> None:
            (sheet/"style.css").write_text(css)
            files=[{"path":path.relative_to(sheet).as_posix(),"sha256":visual.sha256_bytes(path.read_bytes())} for path in (sheet/"index.html",sheet/"style.css",sheet/"assets/a.svg")]
            visual.validate_sheet_manifest({"schema_version":"brandkit-visual-sheet-manifest.v1","brief_sha256":"a"*64,"candidate_set_sha256":"b"*64,"affinity_observation_sha256":"c"*64,"files":files,"viewports":[320,768,1440],"offline":True,"interactive_controls":False,"preselection":False,"normative":False,"human_review_status":"required"},sheet)
        return check

    # --- acceptance bar, point 2: the ledger lock may never be skipped silently ---

    def test_every_spelling_of_the_external_paths_still_takes_the_ledger_lock(self):
        project,root,secrets,fixture=self.affinity(); auth=fixture["auth"]
        spellings=(auth,{**auth,"owner_key_path":Path(auth["owner_key_path"]),"replay_ledger_path":Path(auth["replay_ledger_path"])})
        for descriptor in spellings:
            fixture["ledger_path"].write_bytes(fixture["genesis_raw"]); fixture["ledger_path"].chmod(0o600)
            for name in self.locks(secrets): (secrets/name).unlink()
            self.assertEqual(["lockup-choice","logo-choice"],sorted(item["id"] for item in self.bundle(project,descriptor)["eligible_candidates"]))
            self.assertEqual(1,len(self.locks(secrets)),descriptor)
            self.assertEqual(0o600,stat.S_IMODE((secrets/self.locks(secrets)[0]).stat().st_mode))
            self.assertEqual([],[item.name for item in project.rglob("*") if item.name.startswith(".affinity-lock.")])
            self.assertEqual(1,json.loads(fixture["ledger_path"].read_bytes())["generation"])

    def test_a_descriptor_path_that_is_not_a_filesystem_path_is_a_contract_error(self):
        project,root,secrets,fixture=self.affinity(); auth=fixture["auth"]
        for value in (b"/tmp/x",7,None,True,["/tmp/x"],{"path":"/tmp/x"},""):
            for field in ("owner_key_path","replay_ledger_path"):
                with self.assertRaisesRegex(visual.VisualDecisionError,"descriptor is not closed"):
                    self.bundle(project,{**auth,field:value})
        for descriptor in (["owner_key_path","replay_ledger_path","receipts"],("owner_key_path","replay_ledger_path","receipts"),"owner_key_path",42):
            with self.assertRaisesRegex(visual.VisualDecisionError,"descriptor is not closed"):
                self.bundle(project,descriptor)
        # The descriptor is typed before any lock is taken and before the ledger is touched.
        self.assertEqual([],self.locks(secrets))
        self.assertEqual(fixture["genesis_raw"],fixture["ledger_path"].read_bytes())

    def test_a_named_ledger_that_cannot_be_locked_fails_instead_of_running_unlocked(self):
        project,root,secrets,fixture=self.affinity(); auth=fixture["auth"]
        inside=project/"replay-ledger.json"; inside.write_bytes(fixture["genesis_raw"]); inside.chmod(0o600)
        missing=secrets/"gone"/"replay-ledger.json"
        linked=Path(tempfile.mkdtemp()).resolve()/"secrets-link"; self.addCleanup(shutil.rmtree,linked.parent,True); linked.symlink_to(secrets,target_is_directory=True)
        for ledger,reason in ((inside,"must remain outside the project"),(missing,"parent is not canonical"),(linked/"replay-ledger.json","parent is not canonical")):
            with self.assertRaisesRegex(visual.VisualDecisionError,reason):
                self.bundle(project,{**auth,"replay_ledger_path":str(ledger)})
        self.assertEqual([],[item.name for item in project.rglob("*") if item.name.startswith(".affinity-lock.")])
        self.assertEqual(fixture["genesis_raw"],fixture["ledger_path"].read_bytes())

    # --- acceptance bar, point 2: the receipt MAC, measured on its own ------------

    def test_a_receipt_mac_is_verified_even_when_its_key_id_is_correct(self):
        project,root,secrets,fixture=self.affinity()
        def descriptor(mangle) -> dict:
            receipts={}
            for candidate_id,document in fixture["documents"].items():
                body={name:value for name,value in document.items() if name!="hmac_sha256"}
                body["hmac_sha256"]=hmac.new(fixture["operation_key"],visual.canonical_json(body),hashlib.sha256).hexdigest()
                receipts[candidate_id]=canonical_write(root,f"receipt-{candidate_id}.json",mangle(body))
            return {**fixture["auth"],"receipts":receipts}
        def flip(digest: str, position: int) -> str:
            index=position % len(digest); replacement="0" if digest[index]!="0" else "1"
            return digest[:index]+replacement+digest[index+1:]
        cases=(lambda body: {**body,"hmac_sha256":flip(body["hmac_sha256"],-1)},
               lambda body: {**body,"hmac_sha256":flip(body["hmac_sha256"],0)},
               lambda body: {**body,"issuer":"forged"})
        self.assertEqual(["lockup-choice","logo-choice"],sorted(item["id"] for item in self.bundle(project,descriptor(lambda body: body))["eligible_candidates"]))
        for mangle in cases:
            fixture["ledger_path"].write_bytes(fixture["genesis_raw"]); fixture["ledger_path"].chmod(0o600)
            forged=descriptor(mangle)
            stored=json.loads((root/"receipt-mark-a.json").read_bytes())
            # The key id stays exactly right, so the MAC comparison is the only guard left.
            self.assertEqual(visual.sha256_bytes(fixture["operation_key"])[:16],stored["key_id"])
            with self.assertRaisesRegex(visual.VisualDecisionError,"receipt authentication failed"): self.bundle(project,forged)
            self.assertEqual(fixture["genesis_raw"],fixture["ledger_path"].read_bytes())

    # --- acceptance bar, point 1: the source contract is bound to the release path ---

    def test_a_source_binding_must_be_the_release_path_itself_not_merely_end_in_it(self):
        release=visual.ROOT/"visual-decision-sources.v1.json"; genuine=release.read_bytes()
        staged=Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree,staged,True)
        (staged/release.name).write_bytes(genuine)
        document=json.loads(genuine); document["selected_dependency"]="invented-source"; raw=visual.canonical_json(document)
        for parent in ("sub","deep/deeper"):
            target=staged/parent/release.name; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(raw)
        sidecar=visual.ROOT.parents[1]; original=visual.ROOT; visual.ROOT=staged; self.addCleanup(setattr,visual,"ROOT",original)
        # Each alias exists under the module root with a matching hash, so nothing but the
        # release-path binding itself can be what rejects it.
        for alias in ("sub/visual-decision-sources.v1.json","deep/deeper/visual-decision-sources.v1.json"):
            with self.assertRaisesRegex(visual.VisualDecisionError,"release-bound"):
                visual.validate_source_contract({"path":alias,"sha256":visual.sha256_bytes(raw)},sidecar)

    # --- acceptance bar, point 3: canonicalisation the tokenizer would agree with ---

    def test_css_comment_canonicalisation_never_reaches_into_a_string_token(self):
        check=self.sheet(); local='a{background:url(assets/a.svg)}'
        smuggled=(local+'b{content:"/*"}c{background:url(https://evil.invalid/x)}d{content:"*/"}',
                  local+'b{content:"/*"}@import "https://evil.invalid/x";d{content:"*/"}',
                  local+'b{content:"/*"}c{background:url(data:image/svg+xml,<svg/>)}d{content:"*/"}',
                  local+'b{content:"/*"}c{background:u\\72l(\\68ttps://evil.invalid/x)}d{content:"*/"}',
                  local+"b{content:'/*'}c{background:url(https://evil.invalid/x)}d{content:'*/'}",
                  local+'b{content:"\\"/*\\""}c{background:url(https://evil.invalid/x)}d{content:"*/"}',
                  local+'b{content:"/*\nx"}c{background:url(https://evil.invalid/x)}d{content:"*/"}',
                  local+'b{content:"/*"}c{background:url(https://evil.invalid/x)}')
        for css in smuggled:
            with self.assertRaises(visual.VisualDecisionError): check(css)
        # A string that merely carries the delimiters is legal CSS and must stay accepted.
        check(local+'b{content:"/*"}c{content:"*/"}')

    def test_css_comments_are_canonicalised_across_lines_before_the_offline_scan(self):
        check=self.sheet(); local='a{background:url(assets/a.svg)}'
        for css in (local+'b{background:u/**/rl(https://evil.invalid/x)}',
                    local+'b{background:u/*\n*/rl(https://evil.invalid/x)}',
                    local+'@import/**/"https://evil.invalid/x";',
                    local+'b{background:u/*x*/r/*y*/l(https://evil.invalid/x)}',
                    # Neither the function name nor the scheme survives as a token here,
                    # so only rejoining the split halves can see this declaration at all.
                    local+'c{background:u/**/rl(ht/**/tps://evil.invalid/x)}',
                    local+'b{content:"x"}c{background:u/**/rl(ht/**/tps://evil.invalid/x)}',
                    local+"b{content:'x'}c{background:u/**/rl(ht/**/tps://evil.invalid/x)}",
                    # An unescaped newline ends the string token, so what follows is code again.
                    local+'b{content:"x\n}c{background:u/**/rl(ht/**/tps://evil.invalid/x)}',
                    # A string that is never closed still hands its contents to the scan.
                    local+'b{content:"https://evil.invalid/x'):
            with self.assertRaises(visual.VisualDecisionError): check(css)
        # Removed comments are really gone: their content must not reach the token scan,
        # and a string token stops at its closing quote, at a newline or at end of input
        # instead of swallowing the rest of the sheet.
        check(local+'/* see https://example.invalid and @import for the rationale */')
        check(local+'/*\nhttps://example.invalid\n@import\n*/')
        check(local+'b{content:"x"}/* https://example.invalid */')
        check(local+"b{content:'x'}/* @import https://example.invalid */")
        check(local+'b{content:"x\n}/* https://example.invalid */')
        check(local+'/* an unterminated comment runs to end of input: https://example.invalid')


class VisualDecisionCoreR9Tests(TimeLimitedTestCase):
    """Properties the r8 review found unmeasured, plus the two holes it found open.

    Each test states one property of the shipped contract and probes it from
    several sides, so a neighbouring weakening of the same guard - a prefix
    comparison instead of a full one, a dropped conjunct, an off-by-one bound -
    fails here as surely as deleting the guard outright.
    """

    def project(self) -> tuple[Path, Path]:
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup); project=Path(temp.name).resolve()
        return project, visual.visual_workspace(project,"run-a",create=True)

    def affinity(self) -> tuple[Path, Path, Path, dict]:
        project,root=self.project(); secrets=Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree,secrets,True)
        return project,root,secrets,{**affinity_bundle(project,root,secrets),"root":root}

    def bundle(self, project: Path, auth: object = None) -> dict:
        return visual.validate_bundle(project,"run-a",NOW,sidecar_root=visual.ROOT.parents[1],affinity_auth=auth)

    def locks(self, directory: Path) -> list[str]:
        return sorted(item.name for item in directory.iterdir() if item.name.startswith(".affinity-lock."))

    def sheet(self):
        """Return a callable that validates one sheet; extra assets and a custom page are optional."""
        _project,root=self.project(); sheet=root/"sheet"; (sheet/"assets").mkdir(parents=True); (sheet/"assets/a.svg").write_text("svg")
        sections=''.join(f'<section id="{name}"><h2>x</h2></section>' for name in ("candidates-color","candidates-typography","candidates-code-font","candidates-logo","candidates-lockup","recommendations","owner-review"))
        default='<!doctype html><html lang="de"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="'+visual.SHEET_CSP+'"><link rel="stylesheet" href="style.css"><title>x</title></head><body><main>%s</main></body></html>'
        def check(css: str, *, body: str = "", assets: tuple[str, ...] = (), drop: tuple[str, ...] = (), duplicate: tuple[str, ...] = ()) -> None:
            (sheet/"style.css").write_text(css); (sheet/"index.html").write_text(default%(sections+body))
            members=[sheet/"index.html",sheet/"style.css",sheet/"assets/a.svg"]
            for name in assets:
                target=sheet/"assets"/name; target.write_text("svg"); members.append(target)
            files=[{"path":item.relative_to(sheet).as_posix(),"sha256":visual.sha256_bytes(item.read_bytes())} for item in members]
            files=[item for item in files if item["path"] not in drop]+[item for item in files if item["path"] in duplicate]
            visual.validate_sheet_manifest({"schema_version":"brandkit-visual-sheet-manifest.v1","brief_sha256":"a"*64,"candidate_set_sha256":"b"*64,"affinity_observation_sha256":"c"*64,"files":files,"viewports":[320,768,1440],"offline":True,"interactive_controls":False,"preselection":False,"normative":False,"human_review_status":"required"},sheet)
        return check

    def receipts(self, fixture: dict, signing_key: bytes | None = None, **overrides) -> dict:
        """Re-sign every receipt of the fixture with the given field overrides."""
        signing_key=signing_key or fixture["operation_key"]
        return {**fixture["auth"],"receipts":{candidate_id:write_receipt(root=fixture["root"],document={**document,**overrides,"key_id":visual.sha256_bytes(signing_key)[:16]},signing_key=signing_key) for candidate_id,document in fixture["documents"].items()}}

    def genesis(self, fixture: dict) -> None:
        fixture["ledger_path"].write_bytes(fixture["genesis_raw"]); fixture["ledger_path"].chmod(0o600)

    # --- acceptance bar, point 3: /* is not a comment inside a url token -------

    def test_a_comment_never_opens_inside_an_unquoted_css_url_token(self):
        check=self.sheet()
        # css-syntax-3 4.3.6: inside url( ... ) without quotes, "/*" is ordinary URL
        # text.  Every payload below is one the browser parses as two rules, the
        # second of which fetches a remote resource, so the offline scan must see it.
        smuggled=('a{background:url(assets/a.svg/*)}b{background:url(https://evil.invalid/x.png)}',
                  'a{background:url(assets/a.svg/*)}b{background:url(https://evil.invalid/x.png)}/**/',
                  'a{background:url(assets/a.svg/*)}@import url(https://evil.invalid/x.css);',
                  'a{background:url(assets/a.svg/*)}b{background:url(data:image/svg+xml,<svg/>)}',
                  'a{background:url( assets/a.svg/*)}b{background:url(https://evil.invalid/x.png)}',
                  'a{background:url(\tassets/a.svg/*)}b{background:url(https://evil.invalid/x.png)}',
                  'a{background:url(\n assets/a.svg/*)}b{background:url(https://evil.invalid/x.png)}',
                  # whitespace inside the token does not hand control back: what
                  # follows is the remnants of a bad url token (4.3.14), read to the
                  # same ")" without looking for a comment either
                  'a{background:url(assets/a.svg /*)}b{background:url(https://evil.invalid/x.png)}',
                  # a backslash escapes the next character inside the token, so an
                  # escaped ")" does not close it
                  'a{background:url(assets/a.svg\\)/*)}b{background:url(https://evil.invalid/x.png)}',
                  # url( is matched ASCII case-insensitively, escapes included
                  'a{background:URL(assets/a.svg/*)}b{background:url(https://evil.invalid/x.png)}',
                  'a{background:Url(assets/a.svg/*)}b{background:url(https://evil.invalid/x.png)}',
                  'a{background:u\\72l(assets/a.svg/*)}b{background:url(https://evil.invalid/x.png)}',
                  'a{background:\\75rl(assets/a.svg/*)}b{background:url(https://evil.invalid/x.png)}',
                  'a{background:\\75 rl(assets/a.svg/*)}b{background:url(https://evil.invalid/x.png)}',
                  'a{background:\\000075rl(assets/a.svg/*)}b{background:url(https://evil.invalid/x.png)}',
                  'a{background:\\55\\52\\4c(assets/a.svg/*)}b{background:url(https://evil.invalid/x.png)}',
                  # a url token starts no string either, so a stray quote inside it
                  # must not swallow the rest of the sheet
                  'a{background:url(assets/a.svg")}b{background:url(https://evil.invalid/x.png)}',
                  "a{background:url(assets/a.svg')}b{background:url(https://evil.invalid/x.png)}",
                  'a{background:url(assets/a.svg/*)}b{content:"x"}c{background:url(https://evil.invalid/x)}')
        for css in smuggled:
            with self.assertRaises(visual.VisualDecisionError): check(css)
        # ... while every legitimate spelling of a local url stays accepted, and a
        # comment that really is outside a url token is still removed.
        for accepted in ('a{background:url(assets/a.svg)}',
                         'a{background:url("assets/a.svg")}',
                         "a{background:url('assets/a.svg')}",
                         'a{background:url( assets/a.svg )}',
                         'a{background:url(  "assets/a.svg"  )}',
                         'a{background:URL(assets/a.svg)}',
                         'a{background:u\\72l(assets/a.svg)}',
                         'a{background:\\75 rl(assets/a.svg)}',
                         '@font-face{src:url("assets/a.svg") format("svg")}a{background:url(assets/a.svg)}',
                         'a{background:url(assets/a.svg)}/* https://example.invalid @import expression( */',
                         'a{background:url(assets/a.svg)}b{content:"/*"}c{content:"*/"}',
                         'a{background:url(assets/a.svg)}b{color:red}/*unterminated https://example.invalid'):
            check(accepted)

    def test_a_carriage_return_or_form_feed_ends_a_css_string_like_a_newline(self):
        check=self.sheet(); local='a{background:url(assets/a.svg)}'
        for ending in ("\n","\r","\f","\r\n"):
            # The string token stops at the line break, so the comment behind it is a
            # real comment and is stripped - the sheet stays acceptable.  For U+000C
            # the statement is made at the scanner primitive and the member is
            # refused by name; the reading of css-syntax-3 is the same either way.
            css_case(self,check,local+'b{content:"x'+ending+'}/* https://example.invalid */',None)
            # ... and for the same reason what follows the break is code again, so a
            # remote reference hidden there cannot escape the scan.
            css_case(self,check,local+'b{content:"x'+ending+'}c{background:u/**/rl(ht/**/tps://evil.invalid/x)}',"")
            css_case(self,check,local+'b{content:"x'+ending+'}c{background:url(https://evil.invalid/x)}',"")

    def test_css_escape_bounds_are_closed_at_both_ends(self):
        check=self.sheet(); local='a{background:url(assets/a.svg)}'
        # A CSS escape may not name U+0000, a surrogate, or anything past the last
        # code point - and both surrogate bounds are themselves invalid.
        for escape in ("\\0","\\00","\\000000","\\D800","\\DBFF","\\DC00","\\DFFF","\\110000","\\FFFFFF"):
            with self.assertRaisesRegex(visual.VisualDecisionError,"invalid escape"):
                check(local+'b{content:"'+escape+'"}')
        for escape in ("\\1","\\D7FF","\\E000","\\10FFFF","\\41"):
            check(local+'b{content:"'+escape+'"}')

    def test_each_forbidden_css_token_is_rejected_on_its_own_and_case_blind(self):
        check=self.sheet(); local='a{background:url(assets/a.svg)}'
        # Every carrier keeps the url() inventory identical to the manifest, so the
        # forbidden-token scan is the only guard that can still reject it - one
        # payload per token, so dropping any single entry from the list shows up.
        carriers=('@import "x.css";',
                  'b{content:"http://evil.invalid/x"}',
                  'b{content:"https://evil.invalid/x"}',
                  'b{content:"data:text/html,x"}',
                  'b{content:"javascript:alert(1)"}',
                  'b{width:expression(alert(1))}')
        for carrier in carriers:
            for spelling in (carrier,carrier.upper(),carrier.title()):
                with self.assertRaisesRegex(visual.VisualDecisionError,"network or active content"):
                    check(local+spelling)
        check(local+'b{content:"a local note"}')

    def test_sheet_references_and_manifest_assets_must_match_in_both_directions(self):
        check=self.sheet(); local='a{background:url(assets/a.svg)}'
        check(local)
        check(local+'c{background:url(assets/b.svg)}',assets=("b.svg",))
        # one reference too many - no forbidden token anywhere, so only the exact
        # equality against the manifest asset set can reject these
        for extra in ('b{background:url(assets/ghost.svg)}',
                      'b{background:url(elsewhere/x.svg)}',
                      'b{background:url("assets/ghost.svg")}',
                      'b{background:URL(assets/ghost.svg)}',
                      'b{list-style-image:url(assets/a.svg.bak)}'):
            with self.assertRaisesRegex(visual.VisualDecisionError,"asset references differ"): check(local+extra)
        with self.assertRaisesRegex(visual.VisualDecisionError,"asset references differ"):
            check(local,body='<img src="assets/ghost.svg" alt="x">')
        # ... and one reference too few: an asset the manifest carries but nothing names
        with self.assertRaisesRegex(visual.VisualDecisionError,"asset references differ"):
            check(local,assets=("b.svg",))
        with self.assertRaisesRegex(visual.VisualDecisionError,"asset references differ"):
            check('b{color:red}')

    def test_the_sheet_manifest_inventory_must_be_complete_and_duplicate_free(self):
        check=self.sheet(); local='a{background:url(assets/a.svg)}c{background:url(assets/b.svg)}d{background:url(assets/c.svg)}'
        spare={"assets":("b.svg","c.svg")}                 # keeps the manifest schema-valid while entries are dropped
        check(local,**spare)
        # The page and its style sheet are the two files the offline policy is read
        # from; a manifest that omits either, or that lists one twice, is not the
        # closed inventory every later check assumes.
        for kwargs in ({"drop":("style.css",)},{"drop":("index.html",)},{"drop":("index.html","style.css")},
                       {"duplicate":("index.html",)},{"duplicate":("style.css",)},{"duplicate":("assets/a.svg",)}):
            with self.assertRaisesRegex(visual.VisualDecisionError,"file inventory is incomplete or duplicated"):
                check(local,**spare,**kwargs)

    def test_an_observation_must_sit_inside_a_non_empty_window_that_contains_now(self):
        project,root=self.project(); base=unavailable(project,"run-a")
        visual.validate_affinity_observation(base,root,project_root=project,run_id="run-a",now=NOW)
        for observed,expires in ((NOW+1,NOW+2),(NOW+3600,NOW+7200),(NOW-10,NOW-1),(NOW,NOW),(NOW+5,NOW+5),(NOW-1,NOW-2)):
            with self.assertRaisesRegex(visual.VisualDecisionError,"stale or future-dated"):
                visual.validate_affinity_observation({**base,"observed_at":observed,"expires_at":expires},root,project_root=project,run_id="run-a",now=NOW)
        for observed,expires in ((NOW,NOW+1),(NOW-1,NOW),(NOW-3600,NOW+3600)):
            visual.validate_affinity_observation({**base,"observed_at":observed,"expires_at":expires},root,project_root=project,run_id="run-a",now=NOW)

    def test_the_sheet_output_directory_must_be_named_sheet_and_start_empty(self):
        _project,root=self.project()
        for name in ("sheets","Sheet","sheet2","assets","sheet ",".sheet"):
            with self.assertRaisesRegex(visual.VisualDecisionError,"sheet output name differs"):
                visual._safe_output_dir(root/name)
        visual.os.close(visual._safe_output_dir(root/"sheet"))
        self.assertTrue((root/"sheet").is_dir())
        visual.os.close(visual._safe_output_dir(root/"sheet"))
        for stray in ("stray.txt",".hidden"):
            (root/"sheet"/stray).write_text("x")
            with self.assertRaisesRegex(visual.VisualDecisionError,"absent or empty"):
                visual._safe_output_dir(root/"sheet")
            (root/"sheet"/stray).unlink()

    # --- confinement of every relative read ------------------------------------

    def test_secure_read_confines_every_relative_spelling_to_its_root(self):
        _project,root=self.project()
        outside=root.parent/"secret.txt"; outside.write_bytes(b"TOP SECRET OUTSIDE ROOT\n")
        (root/"sub").mkdir(); (root/"sub/inner.txt").write_bytes(b"inner\n")
        self.assertEqual(b"inner\n",visual.secure_read(root,"sub/inner.txt","inner")[0])
        # The escape target really is reachable from the root by plain path
        # arithmetic, so nothing but the confinement check can be what stops it.
        self.assertEqual(b"TOP SECRET OUTSIDE ROOT\n",(root/"sub/../../secret.txt").read_bytes())
        for relative in ("sub/../../secret.txt","../secret.txt","../../secret.txt","..","sub/..",
                         "sub/../inner.txt","./sub/inner.txt","sub/./inner.txt","sub//inner.txt",
                         "sub/inner.txt/","/etc/passwd",str(outside),"","."):
            with self.assertRaisesRegex(visual.VisualDecisionError,"not a normalized relative path"):
                visual.secure_read(root,relative,"probe")
            with self.assertRaisesRegex(visual.VisualDecisionError,"not a normalized relative path"):
                visual.confined_regular(root,relative,"probe")
        self.assertEqual(root/"sub/inner.txt",visual.confined_regular(root,"sub/inner.txt","inner"))

    def test_a_bound_digest_must_match_over_its_whole_length_and_exact_spelling(self):
        _project,root=self.project()
        binding=write(root/"payload.json",'{"x":1}'); digest=binding["sha256"]
        self.assertEqual(b'{"x":1}',visual._verify_bound_file(root,binding,"payload"))
        def flip(value: str, index: int) -> str:
            return value[:index]+("0" if value[index]!="0" else "1")+value[index+1:]
        # A digest that agrees on any prefix but differs anywhere at all is not the
        # digest, so a truncated comparison is not a comparison.
        for index in (0,7,8,15,31,32,62,63):
            with self.assertRaises(visual.VisualDecisionError):
                visual._verify_bound_file(root,{"path":binding["path"],"sha256":flip(digest,index)},"payload")
        for value in (digest+"\n","\n"+digest,digest+" ",digest.upper(),digest[:63],digest[:32],digest+"a","g"*64,None,123,True,["a"*64]):
            with self.assertRaisesRegex(visual.VisualDecisionError,"lowercase SHA-256"):
                visual.require_sha256(value,"probe")
        self.assertEqual(digest,visual.require_sha256(digest,"probe"))

    def test_run_ids_are_anchored_and_the_protected_name_is_banned_anywhere_in_the_path(self):
        project,_root=self.project()
        for run_id in ("run-a\n","\nrun-a","run-a\nfirst-ci","run a","run-a/x","Run-A","-run-a","","x"*64,"first-ci","first-ci\n"):
            with self.assertRaisesRegex(visual.VisualDecisionError,"run id is invalid or protected"):
                visual.visual_workspace(project,run_id,create=True)
        # The protected name is refused wherever it sits in the resolved workspace
        # path, not only in its last component.
        outer=Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree,outer,True)
        nested=outer/"first-ci"/"inner"; nested.mkdir(parents=True)
        with self.assertRaisesRegex(visual.VisualDecisionError,"protected first-ci path is forbidden"):
            visual.visual_workspace(nested,"run-b",create=True)

    def test_protected_inventory_refuses_a_linked_or_non_canonical_run_root(self):
        project,_root=self.project()
        real=Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree,real,True)
        (real/"a.txt").write_text("sealed")
        work=project/".brandkit/work"; linked=work/"first-ci"; linked.symlink_to(real,target_is_directory=True)
        with self.assertRaisesRegex(visual.VisualDecisionError,"absent or linked"): visual.protected_run_inventory(project)
        linked.unlink()
        with self.assertRaisesRegex(visual.VisualDecisionError,"absent or linked"): visual.protected_run_inventory(project)
        with self.assertRaisesRegex(visual.VisualDecisionError,"defined only for first-ci"):
            visual.protected_run_inventory(project,"run-a")
        (work/"first-ci").mkdir(); (work/"first-ci/a.txt").write_text("sealed")
        self.assertEqual(visual.protected_run_inventory(project),visual.protected_run_inventory(project))
        (work/"first-ci/link.txt").symlink_to(real/"a.txt")
        with self.assertRaises(visual.VisualDecisionError): visual.protected_run_inventory(project)
        (work/"first-ci/link.txt").unlink()
        # A run root that is itself canonical but is reached through a linked parent
        # is not the run root either.
        other=Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree,other,True)
        (other/".brandkit").mkdir(); (other/"elsewhere/first-ci").mkdir(parents=True)
        (other/".brandkit/work").symlink_to(other/"elsewhere",target_is_directory=True)
        with self.assertRaisesRegex(visual.VisualDecisionError,"absent or linked"): visual.protected_run_inventory(other)

    def test_a_file_that_changes_under_the_read_is_rejected_even_at_the_same_length(self):
        _project,root=self.project()
        target=root/"payload.json"; target.write_bytes(b"a"*4096)
        self.assertEqual(b"a"*4096,visual.secure_read(root,"payload.json","payload")[0])
        real_read=visual.os.read
        def rewrite_once(descriptor, size):
            visual.os.read=real_read                          # only the first chunk swaps the content
            target.write_bytes(b"b"*4096)                     # same inode, same length, other bytes
            visual.os.utime(target,ns=(1_000_000_000,1_000_000_000))
            return real_read(descriptor,size)
        visual.os.read=rewrite_once
        try:
            with self.assertRaisesRegex(visual.VisualDecisionError,"changed while reading"):
                visual.secure_read(root,"payload.json","payload")
        finally: visual.os.read=real_read

    def test_a_short_read_is_not_mistaken_for_the_whole_file(self):
        _project,root=self.project()
        target=root/"payload.json"; target.write_bytes(b"c"*8192)
        real_read=visual.os.read; seen: list[int]=[]
        def truncated(descriptor, size):
            seen.append(descriptor)
            return real_read(descriptor,16) if len(seen)==1 else b""
        visual.os.read=truncated
        try:
            with self.assertRaisesRegex(visual.VisualDecisionError,"changed while reading"):
                visual.secure_read(root,"payload.json","payload")
        finally: visual.os.read=real_read

    # --- acceptance bar, point 2: the external descriptor is resolved once ------

    def test_the_external_descriptor_is_resolved_exactly_once_per_run(self):
        project,root,secrets,fixture=self.affinity()
        class Counting:
            def __init__(self, value: object) -> None: self.value=str(value); self.calls=0
            def __fspath__(self) -> str: self.calls+=1; return self.value
        key=Counting(fixture["key_path"]); ledger=Counting(fixture["ledger_path"])
        result=self.bundle(project,{**fixture["auth"],"owner_key_path":key,"replay_ledger_path":ledger})
        self.assertEqual(["lockup-choice","logo-choice"],sorted(item["id"] for item in result["eligible_candidates"]))
        # One resolution per field: the lock and the section it protects are handed
        # the same already-resolved paths instead of asking the descriptor twice.
        self.assertEqual((1,1),(key.calls,ledger.calls))

    def test_a_mutable_fspath_cannot_split_the_lock_from_the_ledger_it_protects(self):
        project,root,secrets,fixture=self.affinity()
        decoy=secrets/"decoy-ledger.json"; decoy.write_bytes(fixture["genesis_raw"]); decoy.chmod(0o600)
        class Flip:
            def __init__(self) -> None: self.calls=0
            def __fspath__(self) -> str:
                self.calls+=1
                return str(decoy) if self.calls==1 else str(fixture["ledger_path"])
        flip=Flip()
        result=self.bundle(project,{**fixture["auth"],"replay_ledger_path":flip})
        self.assertEqual(["lockup-choice","logo-choice"],sorted(item["id"] for item in result["eligible_candidates"]))
        self.assertEqual(1,flip.calls)
        # Exactly one ledger was named, locked, read and committed - the first answer.
        # The second answer never becomes a ledger, and never gets its own lock.
        self.assertEqual(1,json.loads(decoy.read_bytes())["generation"])
        self.assertEqual(fixture["genesis_raw"],fixture["ledger_path"].read_bytes())
        self.assertEqual([".affinity-lock.decoy-ledger.json"],self.locks(secrets))

    def test_the_ledger_may_not_be_the_project_root_or_anything_inside_it(self):
        project,root,secrets,fixture=self.affinity()
        for value in (str(project),str(project)+"/",str(project)+"/.",str(project/".brandkit"),str(root/"replay-ledger.json")):
            with self.assertRaisesRegex(visual.VisualDecisionError,"must remain outside the project"):
                self.bundle(project,{**fixture["auth"],"replay_ledger_path":value})
        # The refusal comes before any lock file is created, inside the project or beside it.
        self.assertEqual([],[item.name for item in project.rglob("*") if item.name.startswith(".affinity-lock.")])
        self.assertNotIn(f".affinity-lock.{project.name}",self.locks(project.parent))
        self.assertEqual(fixture["genesis_raw"],fixture["ledger_path"].read_bytes())

    def test_a_lock_that_can_never_be_created_fails_closed_instead_of_running_unlocked(self):
        project,root,secrets,fixture=self.affinity()
        real_open=visual.os.open
        def refuse(path, flags, *args, **kwargs):
            if isinstance(path,str) and path.startswith(".affinity-lock."):
                if flags & visual.os.O_CREAT: raise FileExistsError(17,"exists")
                raise FileNotFoundError(2,"missing")
            return real_open(path,flags,*args,**kwargs)
        visual.os.open=refuse
        try:
            with self.assertRaisesRegex(visual.VisualDecisionError,"lock is unavailable"):
                self.bundle(project,fixture["auth"])
        finally: visual.os.open=real_open
        self.assertEqual(fixture["genesis_raw"],fixture["ledger_path"].read_bytes())

    def test_the_external_key_and_ledger_must_be_unshared_files_with_exactly_mode_0600(self):
        project,root,secrets,fixture=self.affinity()
        def restore() -> None:
            fixture["ledger_path"].chmod(0o600); fixture["key_path"].chmod(0o600); self.genesis(fixture)
        for target in (fixture["key_path"],fixture["ledger_path"]):
            for mode in (0o400,0o200,0o700,0o604,0o640,0o660,0o666,0o601,0o610):
                restore(); target.chmod(mode)
                with self.assertRaisesRegex(visual.VisualDecisionError,"exclusive 0600 regular file"):
                    self.bundle(project,fixture["auth"])
                target.chmod(0o600)
                self.assertEqual(fixture["genesis_raw"],fixture["ledger_path"].read_bytes())
            restore()
            # A second name for the same inode makes it a shared file, whatever its mode.
            alias=secrets/(target.name+".alias"); alias.hardlink_to(target)
            self.assertEqual(2,target.stat().st_nlink)
            with self.assertRaisesRegex(visual.VisualDecisionError,"exclusive 0600 regular file"):
                self.bundle(project,fixture["auth"])
            alias.unlink()
        restore()
        self.assertEqual(["lockup-choice","logo-choice"],sorted(item["id"] for item in self.bundle(project,fixture["auth"])["eligible_candidates"]))

    def test_a_commit_rejects_an_equally_long_but_different_ledger(self):
        project,root,secrets,fixture=self.affinity()
        ledger,raw=visual.load_replay_ledger(fixture["ledger_path"],fixture["key"],project,"run-a")
        rival=signed_ledger(fixture["key"],fixture["identity"],"run-a",0,"1"*64,[])
        # Same byte count, different bytes: only a full comparison can tell them apart.
        self.assertEqual(len(raw),len(rival)); self.assertNotEqual(raw,rival)
        fixture["ledger_path"].write_bytes(rival); fixture["ledger_path"].chmod(0o600)
        with self.assertRaisesRegex(visual.VisualDecisionError,"changed before commit"):
            visual.persist_replay_ledger(fixture["ledger_path"],fixture["key"],ledger,raw,set(fixture["nonces"]))
        self.assertEqual(rival,fixture["ledger_path"].read_bytes())
        self.assertEqual([],[item.name for item in secrets.iterdir() if item.name.startswith(".replay-ledger.json.")])

    def test_the_ledger_mac_is_compared_over_its_whole_length(self):
        project,root,secrets,fixture=self.affinity()
        body=json.loads(fixture["genesis_raw"]); genuine=body["hmac_sha256"]
        def flip(value: str, index: int) -> str:
            return value[:index]+("0" if value[index]!="0" else "1")+value[index+1:]
        forgeries=[flip(genuine,index) for index in (0,7,8,15,31,32,62,63)]
        # A malformed MAC must fail the contract closed, not escape as a crash.
        forgeries+=[genuine.upper(),genuine[:32],genuine[:63],genuine+"0","g"*64,"ü"*64,123,None,["x"]]
        for forged in forgeries:
            fixture["ledger_path"].write_bytes(visual.canonical_json({**body,"hmac_sha256":forged})); fixture["ledger_path"].chmod(0o600)
            with self.assertRaisesRegex(visual.VisualDecisionError,"authentication/scope failed"):
                self.bundle(project,fixture["auth"])
        self.genesis(fixture)
        self.assertEqual(["lockup-choice","logo-choice"],sorted(item["id"] for item in self.bundle(project,fixture["auth"])["eligible_candidates"]))

    def test_the_ledger_state_must_be_a_well_formed_generation_chain(self):
        project,root,secrets,fixture=self.affinity()
        nonce=fixture["nonces"][0]; chained=visual.sha256_bytes(fixture["genesis_raw"])
        cases=((signed_ledger(fixture["key"],fixture["identity"],"run-a",0,"0"*64,[nonce]),"genesis differs"),
               (signed_ledger(fixture["key"],fixture["identity"],"run-a",0,"0"*64,fixture["nonces"]),"genesis differs"),
               (signed_ledger(fixture["key"],fixture["identity"],"run-a",1,chained,[nonce,nonce]),"state differs"),
               (signed_ledger(fixture["key"],fixture["identity"],"run-a",2,chained,fixture["nonces"]+[nonce]),"state differs"),
               (signed_ledger(fixture["key"],fixture["identity"],"run-a",-1,chained,[]),"state differs"),
               (signed_ledger(fixture["key"],fixture["identity"],"run-a",-1,"0"*64,[]),"state differs"),
               (signed_ledger(fixture["key"],fixture["identity"],"run-a",1.0,chained,[]),"state differs"),
               (signed_ledger(fixture["key"],fixture["identity"],"run-a",1,chained,["z"*64]),"state differs"),
               (signed_ledger(fixture["key"],fixture["identity"],"run-a",1,chained,[nonce.upper()]),"state differs"))
        for raw,reason in cases:
            fixture["ledger_path"].write_bytes(raw); fixture["ledger_path"].chmod(0o600)
            with self.assertRaisesRegex(visual.VisualDecisionError,reason): self.bundle(project,fixture["auth"])
        # A genesis without nonces and a chained generation with distinct ones are fine.
        self.genesis(fixture)
        self.assertEqual(["lockup-choice","logo-choice"],sorted(item["id"] for item in self.bundle(project,fixture["auth"])["eligible_candidates"]))

    # --- acceptance bar, point 2: what a receipt is allowed to say -------------

    def test_a_receipt_is_only_valid_inside_its_own_validity_window(self):
        project,root,secrets,fixture=self.affinity(); fixture["root"]=root
        for issued,expires in ((NOW+1,NOW+2),(NOW+3600,NOW+7200),(NOW-10,NOW-1),(NOW-10,NOW-9),(NOW,NOW),(NOW+5,NOW+5),(NOW-1,NOW-2)):
            self.genesis(fixture)
            with self.assertRaisesRegex(visual.VisualDecisionError,"expired or future-dated"):
                self.bundle(project,self.receipts(fixture,issued_at=issued,expires_at=expires))
            self.assertEqual(fixture["genesis_raw"],fixture["ledger_path"].read_bytes())
        for issued,expires in ((NOW,NOW+1),(NOW-1,NOW),(NOW-3600,NOW+3600)):
            self.genesis(fixture)
            self.assertEqual(["lockup-choice","logo-choice"],sorted(item["id"] for item in self.bundle(project,self.receipts(fixture,issued_at=issued,expires_at=expires))["eligible_candidates"]))

    def test_a_receipt_is_scoped_to_its_project_and_to_its_run(self):
        project,root,secrets,fixture=self.affinity(); fixture["root"]=root
        for overrides in ({"run_id":"run-b"},{"run_id":"run-a-b"},{"project_identity_sha256":visual.sha256_bytes(b"another-project")},
                          {"run_id":"run-b","project_identity_sha256":visual.sha256_bytes(b"another-project")}):
            self.genesis(fixture)
            with self.assertRaisesRegex(visual.VisualDecisionError,"receipt project or run differs"):
                self.bundle(project,self.receipts(fixture,**overrides))
            self.assertEqual(fixture["genesis_raw"],fixture["ledger_path"].read_bytes())

    def test_every_receipt_output_must_live_under_the_receipts_own_staging_root(self):
        project,root,secrets,fixture=self.affinity(); fixture["root"]=root
        write(root/"outside.svg","<svg/>")
        def outputs(path: str) -> dict:
            receipts={}
            for candidate_id,document in fixture["documents"].items():
                body={**document,"outputs":[{"candidate_id":candidate_id,"path":path,"sha256":document["outputs"][0]["sha256"]}]}
                body["hmac_sha256"]=hmac.new(fixture["operation_key"],visual.canonical_json({name:value for name,value in body.items() if name!="hmac_sha256"}),hashlib.sha256).hexdigest()
                receipts[candidate_id]=canonical_write(root,f"receipt-{candidate_id}.json",body)
            return {**fixture["auth"],"receipts":receipts}
        for path in ("outside.svg","logo-report.json","staging/other-doc/mark-a.svg","staging/logo-doc-evil/mark-a.svg",
                     "staging/logo-doc.bak/mark-a.svg","x/staging/logo-doc/mark-a.svg","staging/logo-doc"):
            self.genesis(fixture)
            with self.assertRaisesRegex(visual.VisualDecisionError,"staging/output root differs"):
                self.bundle(project,outputs(path))
            self.assertEqual(fixture["genesis_raw"],fixture["ledger_path"].read_bytes())
        # The staging_root field itself has to name the same place the run id and
        # the document id do - every component of it, not merely its tail.
        for staging in (".brandkit/work/run-b/visual-decisions/staging/logo-doc",
                        ".brandkit/work/run-a/visual-decisions/staging/logo-doc-evil",
                        ".brandkit/work/run-a/visual-decisions/staging/other-doc"):
            self.genesis(fixture)
            with self.assertRaisesRegex(visual.VisualDecisionError,"staging/output root differs"):
                self.bundle(project,self.receipts(fixture,staging_root=staging))


class VisualDecisionCoreR11Tests(TimeLimitedTestCase):
    """The CSS escape rule of css-syntax-3 4.3.7, measured as a rule in every branch.

    r6, r8 and r10 each closed one instance of the same defect: the scanner cut
    an escape shorter than the tokenizer does, dropped out of the token it was
    in, opened a comment on the remainder and deleted from the offline scan
    what the browser went on to execute.  Three fixes, three fresh holes,
    because the length of an escape was approximated separately in the string,
    the url and the ident branch.  The tests below therefore pin the *rule* -
    how long an escape is, and where a url token may start - in all three
    branches at once, over every whitespace form and every digit count, so a
    change that re-splits any one of them fails here.

    A few of them assert on the canonicaliser directly rather than through the
    manifest: the asset-inventory check downstream masks some of these
    differences by rejecting for an unrelated reason, which would let a real
    weakening of the scanner pass a contract-level test unnoticed.
    """

    LOCAL = 'a{background:url(assets/a.svg)}'
    REMOTE = 'c{background:url(https://evil.invalid/x)}'
    WHITESPACE = (" ", "\t", "\n", "\r", "\f", "\r\n")
    NEWLINES = ("\n", "\r", "\f", "\r\n")
    QUOTES = ('"', "'")

    def sheet(self):
        """Return a callable that validates one sheet built from the given CSS."""
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup); project=Path(temp.name).resolve()
        root=visual.visual_workspace(project,"run-a",create=True); sheet=root/"sheet"
        (sheet/"assets").mkdir(parents=True); (sheet/"assets/a.svg").write_text("svg")
        sections=''.join(f'<section id="{name}"><h2>x</h2></section>' for name in ("candidates-color","candidates-typography","candidates-code-font","candidates-logo","candidates-lockup","recommendations","owner-review"))
        page='<!doctype html><html lang="de"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="'+visual.SHEET_CSP+'"><link rel="stylesheet" href="style.css"><title>x</title></head><body><main>'+sections+'</main></body></html>'
        def check(css: str) -> None:
            (sheet/"style.css").write_bytes(css.encode("utf-8")); (sheet/"index.html").write_text(page)
            members=[sheet/"index.html",sheet/"style.css",sheet/"assets/a.svg"]
            files=[{"path":item.relative_to(sheet).as_posix(),"sha256":visual.sha256_bytes(item.read_bytes())} for item in members]
            visual.validate_sheet_manifest({"schema_version":"brandkit-visual-sheet-manifest.v1","brief_sha256":"a"*64,"candidate_set_sha256":"b"*64,"affinity_observation_sha256":"c"*64,"files":files,"viewports":[320,768,1440],"offline":True,"interactive_controls":False,"preselection":False,"normative":False,"human_review_status":"required"},sheet)
        return check

    def test_a_hex_escape_swallows_the_one_whitespace_character_that_follows_it(self):
        check=self.sheet()
        # 4.3.7: a hex escape is 1-6 digits plus *one* optional following
        # whitespace character, and after the CRLF collapse of 3.3 a CRLF pair is
        # that one character.  When it is a line break it therefore does not end
        # the string, so the "/*" behind it is string content and the rule that
        # follows stays visible to the offline scan.  Chrome parses every payload
        # here into two rules and fetches the second one.
        for digits in ("\\9","\\41","\\041","\\0041","\\00041","\\000041"):
            for space in self.WHITESPACE:
                for quote in self.QUOTES:
                    css_case(self,check,self.LOCAL+'b{content:'+quote+'x'+digits+space+'/*'+quote+'}'+self.REMOTE)

    def test_a_backslash_before_a_newline_continues_a_css_string(self):
        check=self.sheet()
        # 4.3.5: inside a string "\" + newline is the line continuation, so the
        # string runs on - with CRLF counted as the single newline of 3.3.
        for newline in self.NEWLINES:
            for quote in self.QUOTES:
                css_case(self,check,self.LOCAL+'b{content:'+quote+'x\\'+newline+'/*'+quote+'}'+self.REMOTE)
        # ... while the very same newline *without* the backslash still ends the
        # string, which is what makes the comment behind it a real comment.  The
        # two halves together pin the continuation instead of the line break.
        for newline in self.NEWLINES:
            for quote in self.QUOTES:
                css_case(self,check,self.LOCAL+'b{content:'+quote+'x'+newline+'}/* https://example.invalid */',None)

    def test_a_css_escape_is_at_most_six_hex_digits_long(self):
        check=self.sheet()
        # Six digits and the line break they swallow: the string runs on.
        with self.assertRaisesRegex(visual.VisualDecisionError,"network or active content"):
            check(self.LOCAL+'b{content:"x\\000041\n/*"}'+self.REMOTE)
        # A seventh hex digit is no longer part of the escape, so the line break
        # behind it is an unescaped one that ends the string; the "/*" is then a
        # real comment running to end of input and the sheet is clean - exactly
        # what Chrome makes of it (verified: no rule, no request).
        check(self.LOCAL+'b{content:"x\\0000411\n/*"}'+self.REMOTE)

    def test_the_string_the_url_and_the_ident_branch_read_an_escape_the_same_way(self):
        check=self.sheet()
        for css in (# string branch: the escape carries the line break
                    self.LOCAL+'b{content:"x\\41\n/*"}'+self.REMOTE,
                    self.LOCAL+'b{content:"x\\41\r\n/*"}'+self.REMOTE,
                    # ident branch: the same, so the run still spells "url" and
                    # the "/*" behind the "(" is url text rather than a comment
                    'a{background:\\75\nrl(assets/a.svg/*)}'+self.REMOTE,
                    'a{background:\\75\r\nrl(assets/a.svg/*)}'+self.REMOTE,
                    # url branch: an escaped ")" does not close the token, in the
                    # short form and in the six-digit-plus-whitespace form alike
                    'a{background:url(assets/a.svg\\29/*)}'+self.REMOTE,
                    'a{background:url(assets/a.svg\\000029 /*)}'+self.REMOTE,
                    # decode stage: a CRLF is that one whitespace character there
                    # too, or "htt\70" + CRLF + ":" would hide the "http:" the
                    # browser assembles out of it
                    self.LOCAL+'b{content:"htt\\70\r\n://evil.invalid/x"}'):
            with self.assertRaisesRegex(visual.VisualDecisionError,"network or active content"):
                check(css)

    def test_a_trailing_backslash_is_undecodable_in_every_branch(self):
        check=self.sheet()
        # "\" at end of input consumes only itself in each of the three states,
        # so a residual backslash always reaches the escape stage and fails there.
        for css in (self.LOCAL+'b{content:"x\\',self.LOCAL+'b{color:re\\','a{background:url(assets/a.svg\\'):
            with self.assertRaisesRegex(visual.VisualDecisionError,"undecodable escape"):
                check(css)

    def test_a_url_token_starts_only_where_the_tokenizer_starts_an_ident_like_token(self):
        check=self.sheet()
        # An ident run never starts inside an identifier, so "xurl(" is not
        # "url(".  Entering the url state one time too often is not merely
        # strict: the state ends at a ")" the scanner would otherwise have read
        # as string content, and a comment can open behind that ")" which the
        # tokenizer never opens - turning a rejection into an acceptance.
        smuggled='xurl(a"b)/*z{background:url(https://evil.invalid/x)}'
        self.assertIn("https:",visual._security_canonical_css(smuggled))
        with self.assertRaises(visual.VisualDecisionError): check(self.LOCAL+smuggled)
        # 4.3.6/4.3.14: only ")" or end of input ends the token and its bad-url
        # remnants, so "}" is ordinary URL text and no comment opens behind it.
        braced='a{background:url(q}/*)}z{background:url(https://evil.invalid/x)}'
        self.assertIn("https:",visual._security_canonical_css(braced))
        with self.assertRaises(visual.VisualDecisionError): check(braced)

    def test_the_ident_behind_a_hash_or_an_at_belongs_to_that_token(self):
        check=self.sheet()
        # 4.3.1: "#url(" is a hash token plus "(" and "@url(" an at-keyword plus
        # "(" - neither is a url token.  Chrome parses both payloads below into
        # no rule at all and issues no request, so declining the url state here
        # is the exact reading, not a gap.
        for prefix in ("@","#","@\\","#5"):
            check(self.LOCAL+prefix+'url(assets/a.svg/*)b{background:url(https://evil.invalid/x)}')
        # ... but neither token may swallow a url token that really follows it,
        # and an ident that merely starts with "#"/"@" text is unaffected.
        for css in ('@import url(https://evil.invalid/x.css);',
                    '@media screen{a{background:url(assets/a.svg/*)}}'+self.REMOTE,
                    '@font-face{src:url(assets/a.svg/*)}'+self.REMOTE,
                    '@x url(assets/a.svg/*)'+self.REMOTE,
                    self.LOCAL+'#id{background:url(https://evil.invalid/x)}'):
            with self.assertRaises(visual.VisualDecisionError): check(css)
        for css in ('@media screen{'+self.LOCAL+'}',self.LOCAL+'#id{color:red}',
                    '@font-face{src:url("assets/a.svg")}'+self.LOCAL):
            check(css)

    def test_a_backslash_before_a_newline_is_a_continuation_only_inside_a_string(self):
        check=self.sheet()
        # 4.3.8: outside a string "\" + newline is no escape at all, so the ident
        # run ends at the backslash and the "url(" behind the break starts a new
        # ident-like token - a url token whose "/*" is URL text.  Reading the
        # continuation everywhere would glue the run into "xurl", lose the url
        # token and let the "/*" open a comment that hides the rule behind it.
        for newline in self.NEWLINES:
            css_case(self,check,self.LOCAL+'b{content:x\\'+newline+'url(assets/a.svg/*)}c{background:url(https://evil.invalid/x)}')

    def test_a_hex_escape_swallows_exactly_one_whitespace_character_not_a_run(self):
        check=self.sheet()
        # 4.3.7 consumes *one* following whitespace character.  Consuming the
        # whole run would let "\75" plus two spaces still spell "url" and open a
        # url token where the tokenizer has an ident "u" and a function "rl(" -
        # one url state too many, and the ")" it ends at lets a comment open.
        for spacing in (" \t","\n\n","\r\n\r\n","  "):
            with self.assertRaisesRegex(visual.VisualDecisionError,"network or active content"):
                check(self.LOCAL+'\\75'+spacing+'rl(a"b)/*z{background:url(https://evil.invalid/x)}')

    def test_the_ident_run_in_front_of_a_url_token_must_spell_url_exactly(self):
        check=self.sheet()
        # Neither a longer ident that merely ends or starts with "url", nor a
        # "(" that is only reached across whitespace, opens a url token: 4.3.6
        # requires the ident-like token's own immediately following "(".
        for css in ('urlx(a"b)/*z{background:url(https://evil.invalid/x)}',
                    'xurl(a"b)/*z{background:url(https://evil.invalid/x)}',
                    'url (a"b)/*z{background:url(https://evil.invalid/x)}',
                    'url\n(a"b)/*z{background:url(https://evil.invalid/x)}',
                    # a non-ASCII code point is an ident code point (4.3), so it
                    # belongs to the run in front of the "(" rather than ending it
                    '\u00aburl(assets/a.svg"x)/*z{background:url(https://evil.invalid/x)}',
                    'b{background:y\u2022url(assets/a.svg"x)/*z{c:url(https://evil.invalid/x)}'):
            with self.assertRaisesRegex(visual.VisualDecisionError,"network or active content"):
                check(self.LOCAL+css)

    def test_comments_are_removed_once_the_way_the_tokenizer_removes_them(self):
        check=self.sheet()
        # A "/*" that only appears once a comment has been cut out is not a
        # comment to any browser - the tokenizer passes over the text once.
        # Stripping twice would delete the rule below from the scan while Blink
        # still parses it.
        with self.assertRaisesRegex(visual.VisualDecisionError,"network or active content"):
            check(self.LOCAL+'b{background:/'+'/*x*/'+'*url(https://evil.invalid/x)}')


class VisualDecisionCoreR13Tests(TimeLimitedTestCase):
    """The four holes r12 opened, measured as rules rather than as payloads.

    Two of them are one defect: a security decision taken over the *text* of a
    path on a file system that does not compare paths as text.  Two more are the
    same shape in the sheet scanner: a promise about "every reference" and
    "every pragma" kept by looking at exactly one spelling of each.  The tests
    below therefore state the rule - which resource forms a browser resolves,
    which pragmas a sheet may declare, which file a path names - and probe each
    from several sides, so a neighbouring weakening fails here too.
    """

    SECTIONS = ("candidates-color", "candidates-typography", "candidates-code-font",
                "candidates-logo", "candidates-lockup", "recommendations", "owner-review")
    LOCAL = 'a{background:url(assets/a.svg)}'

    def project(self) -> tuple[Path, Path]:
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve()
        return project, visual.visual_workspace(project, "run-a", create=True)

    def page(self, extra_meta: str = "", *, csp: str | None = None) -> str:
        sections = "".join(f'<section id="{name}"><h2>x</h2></section>' for name in self.SECTIONS)
        policy = visual.SHEET_CSP if csp is None else csp
        return ('<!doctype html><html lang="de"><head><meta charset="utf-8">'
                f'<meta http-equiv="Content-Security-Policy" content="{policy}">' + extra_meta +
                '<link rel="stylesheet" href="style.css"><title>x</title></head><body><main>'
                + sections + '</main></body></html>')

    def sheet(self):
        """Return a callable that validates one sheet built from the given CSS and page."""
        _project, root = self.project(); sheet = root / "sheet"
        (sheet / "assets").mkdir(parents=True); (sheet / "assets/a.svg").write_text("svg")
        def check(css: str, *, page: str | None = None, assets: tuple[str, ...] = ()) -> None:
            (sheet / "style.css").write_bytes(css.encode("utf-8"))
            (sheet / "index.html").write_bytes((self.page() if page is None else page).encode("utf-8"))
            members = [sheet / "index.html", sheet / "style.css", sheet / "assets/a.svg"]
            for name in assets:
                target = sheet / "assets" / name; target.write_text("svg"); members.append(target)
            files = [{"path": item.relative_to(sheet).as_posix(), "sha256": visual.sha256_bytes(item.read_bytes())} for item in members]
            manifest = {"schema_version": "brandkit-visual-sheet-manifest.v1", "brief_sha256": "a" * 64,
                        "candidate_set_sha256": "b" * 64, "affinity_observation_sha256": "c" * 64, "files": files,
                        "viewports": [320, 768, 1440], "offline": True, "interactive_controls": False,
                        "preselection": False, "normative": False, "human_review_status": "required"}
            with deadline(120, "validate_sheet_manifest"):
                visual.validate_sheet_manifest(manifest, sheet)
        return check

    # --- acceptance bar: the reference set really is the closed manifest ------

    def test_a_css_resource_reference_is_captured_in_every_form_blink_resolves(self):
        check = self.sheet()
        # CSS Images 4 lets image-set() name its image as a bare <string>, and
        # Blink resolves that string in every one of the properties below - and
        # nested inside -webkit-cross-fade().  The scan used to know one
        # spelling, "url(", so each of these fetched a file the manifest does
        # not carry while the manifest check reported that the references
        # matched.  The rule is "every form the browser resolves", not "url()".
        for carrier in ('b{background-image:image-set("assets/ghost.svg" 1x)}',
                        'b{background-image:-webkit-image-set("assets/ghost.svg" 1x)}',
                        'b{mask-image:image-set("assets/ghost.svg" 1x)}',
                        'b{list-style-image:image-set("assets/ghost.svg" 1x)}',
                        'b{border-image-source:image-set("assets/ghost.svg" 1x)}',
                        'b{cursor:image-set("assets/ghost.svg" 1x),auto}',
                        'b{content:image-set("assets/ghost.svg" 1x)}',
                        "b{background-image:image-set('assets/ghost.svg' 1x)}",
                        'b{background-image:IMAGE-SET("assets/ghost.svg" 1x)}',
                        'b{background-image:image-set(url("assets/ghost.svg") 1x)}',
                        'b{background-image:image-set(url(assets/ghost.svg) 1x)}',
                        'b{background-image:-webkit-cross-fade(image-set("assets/ghost.svg" 1x),url(assets/a.svg),50%)}',
                        'b{background-image:cross-fade(image-set("assets/ghost.svg" 1x),url(assets/a.svg),50%)}',
                        '@font-face{src:"assets/ghost.svg"}',
                        '@font-face{src:url("assets/ghost.svg") format("woff2")}'):
            with self.assertRaisesRegex(visual.VisualDecisionError, "asset references differ"):
                check(self.LOCAL + carrier)
        # A foreign origin named the same way is caught by the same rule: it is
        # not a manifest path either, and the scheme tokens back it up.
        for remote in ('b{background-image:-webkit-image-set("//127.0.0.1:1/x.png" 1x)}',
                       'b{background-image:image-set("//evil.invalid/x.png" 1x)}'):
            with self.assertRaises(visual.VisualDecisionError):
                check(self.LOCAL + remote)
        # ... and the other direction: a manifest asset that is named *only*
        # through image-set() counts as named, so the rule is a capture of
        # references and not a blanket ban on the function.
        check(self.LOCAL + 'b{background-image:image-set("assets/b.svg" 1x)}', assets=("b.svg",))
        check(self.LOCAL + 'b{background-image:-webkit-image-set(url(assets/b.svg) 1x)}', assets=("b.svg",))
        check('@font-face{src:url("assets/a.svg") format("woff2")}')

    def test_a_reference_form_the_scanner_cannot_read_is_refused_rather_than_ignored(self):
        check = self.sheet()
        # The closed manifest can only be closed against the reference forms
        # this scanner understands.  A function it has never seen may be one
        # more way to name a resource - Blink has grown several - so an unknown
        # function fails closed instead of being skipped in silence.
        for unknown in ('b{background-image:-moz-image-rect(url(assets/ghost.svg),0,1,1,0)}',
                        'b{background-image:image-tbd("assets/ghost.svg")}',
                        'b{background:paint(worklet)}',
                        'b{background:-webkit-canvas(surface)}',
                        'b{background:element(#source)}'):
            with self.assertRaisesRegex(visual.VisualDecisionError, "unsupported function"):
                check(self.LOCAL + unknown)
        # Everything the rendered sheet itself uses stays acceptable, so the
        # list is a gate and not a wall.
        check(self.LOCAL + ':root{--ink:#171717}b{color:var(--ink);width:calc(100% - 1rem)}'
              '.grid{grid-template-columns:repeat(2,minmax(0,1fr))}c{color:rgb(1,2,3)}')

    def test_the_reference_scanner_reads_the_declaration_it_is_standing_in(self):
        # Measured on the scanner itself: a string is a URL where the browser
        # reads one and ordinary text everywhere else, so the capture can be
        # exact instead of merely broad.
        references, functions = visual._css_resource_references('@font-face{src:url("a.woff2") format("woff2");font-family:"Family"}')
        self.assertEqual({"a.woff2"}, references)
        self.assertEqual({"url", "format"}, functions)
        self.assertEqual((set(), set()), visual._css_resource_references('b{content:"not-a-url.png"}'))
        self.assertEqual(({"x.css"}, set()), visual._css_resource_references('@import "x.css";'))
        self.assertEqual(({"a.png", "b.png"}, {"-webkit-cross-fade", "image-set"}),
                         visual._css_resource_references('b{background:-webkit-cross-fade(image-set("a.png"),image-set("b.png"),50%)}'))
        # A "url(" that no tokenizer ever opens contributes no function and no
        # new reference form: behind a hash or an at-keyword, or at the tail of
        # a longer ident, it is not a url token.
        self.assertEqual(set(), visual._css_resource_references('@url(assets/ghost.svg)')[1])
        self.assertEqual(set(), visual._css_resource_references('#url(assets/ghost.svg)')[1])
        # The *innermost* function decides, not the outermost: a URL function
        # nested inside a harmless one still names a resource.  Reading the
        # outer name instead loses the reference entirely.
        self.assertEqual({"a.png"}, visual._css_resource_references('b{background:format(image-set("a.png"))}')[0])
        self.assertEqual(set(), visual._css_resource_references('b{background:image-set(format("woff2"))}')[0])
        # 4.3.6: an unquoted url token ends at ")" or at end of input, never at
        # the whitespace inside it - stopping at the space would report
        # "assets/a.svg" for a token whose text is "assets/a.svg x".
        self.assertEqual({"assets/a.svg x"}, visual._css_resource_references('b{background:url(assets/a.svg x)}')[0])
        self.assertEqual({"a b c"}, visual._css_resource_references('b{background:url( a b c )}')[0])
        # A block boundary ends a function left open by malformed input, so an
        # unclosed "(" cannot hide a later src: descriptor behind it.
        self.assertEqual({"ghost.svg"}, visual._css_resource_references('a{x:calc( } @font-face{src:"ghost.svg"}')[0])
        self.assertEqual({"ghost.svg"}, visual._css_resource_references('a{x:calc( ; @font-face{src:"ghost.svg"}')[0])
        # The text search for "url(" is kept beside the token scanner on
        # purpose: it also fires where no url token opens, so the reference set
        # can only ever grow and this module cannot accept a sheet that r12
        # rejected.  Measured through the contract, where the union is applied.
        check = self.sheet()
        for hidden in ('b{content:"url(assets/ghost.svg)"}',
                       '@url(assets/ghost.svg)',
                       'b{content:"see url(assets/ghost.svg) for the rationale"}'):
            with self.assertRaisesRegex(visual.VisualDecisionError, "asset references differ"):
                check(self.LOCAL + hidden)

    # --- acceptance bar: a sheet declares one pragma, and it is the policy ----

    def test_the_only_meta_pragma_a_sheet_may_declare_is_the_offline_policy(self):
        check = self.sheet()
        # http-equiv was compared against the policy name but never limited to
        # it, so <meta http-equiv="refresh"> navigated a validated, "offline"
        # sheet to a foreign origin.  SHEET_CSP cannot catch that: it has no
        # navigate-to directive and default-src does not govern top-level
        # navigation, so this check is the only layer there is.
        check(self.LOCAL)
        for pragma, content in (("refresh", "1;url=http://127.0.0.1:1/exfil.png"),
                                ("Refresh", "0;url=//127.0.0.1:1/exfil.png"),
                                ("REFRESH", "5"),
                                ("refresh", "0"),
                                ("content-type", "text/html;charset=utf-8"),
                                ("x-ua-compatible", "IE=edge"),
                                ("set-cookie", "a=b"),
                                ("default-style", "x"),
                                ("origin-trial", "token"),
                                ("content-security-policy", visual.SHEET_CSP)):
            with self.assertRaisesRegex(visual.VisualDecisionError, "forbidden meta pragma"):
                check(self.LOCAL, page=self.page(f'<meta http-equiv="{pragma}" content="{content}">'))
        # The pragma is refused whether it stands before or after the policy
        # meta, and a correct policy beside it does not excuse it.
        with self.assertRaisesRegex(visual.VisualDecisionError, "forbidden meta pragma"):
            check(self.LOCAL, page=self.page().replace('<meta charset="utf-8">',
                                                       '<meta charset="utf-8"><meta http-equiv="refresh" content="1;url=http://127.0.0.1:1/x">'))
        # Metas that carry no pragma at all are unaffected.
        check(self.LOCAL, page=self.page('<meta name="viewport" content="width=device-width,initial-scale=1">'))

    # --- acceptance bar point 4: the sealed run, in every spelling ------------

    def test_the_sealed_run_is_unreachable_under_every_spelling_of_the_project_root(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve()
        sealed = project / ".brandkit/work/first-ci"; sealed.mkdir(parents=True)
        (sealed / "sealed.txt").write_text("sealed")
        before = visual.protected_run_inventory(project)["tree_sha256"]
        # The honest spelling has always been refused.  The point of this test
        # is that "first-ci" is a *file*, not a string: every spelling the file
        # system accepts for it has to be refused too, or the sealed run is a
        # work root and an output root after all.
        for spelling in (sealed, Path(str(sealed) + "/"), Path(str(sealed) + "/."),
                         Path(str(sealed / "x") + "/..")):
            with self.assertRaises(visual.VisualDecisionError):
                visual.visual_workspace(spelling, "run-b", create=True)
        variant = case_variant(sealed)
        if variant is not None:
            self.assertTrue(same_file(variant, sealed))
            with self.assertRaises(visual.VisualDecisionError):
                visual.visual_workspace(variant, "run-b", create=True)
            with self.assertRaisesRegex(visual.VisualDecisionError, "exact canonical directory"):
                visual.exact_project_root(variant)
            # ... and the same variant one level up, where the sealed run is
            # not the root itself but the run id underneath it.
            with self.assertRaises(visual.VisualDecisionError):
                visual.visual_workspace(case_variant(project) or project.parent, "run-b", create=True)
        for run_id in ("first-ci", "First-CI", "FIRST-CI", "first-CI"):
            with self.assertRaisesRegex(visual.VisualDecisionError, "run id is invalid or protected"):
                visual.visual_workspace(project, run_id, create=True)
        # The sealed *name* is banned anywhere in the resolved workspace path,
        # and "anywhere" has to mean every spelling the file system folds
        # together - otherwise a project root one directory below a "FIRST-CI"
        # walks straight past a guard that "first-ci" trips.
        outer = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, outer, True)
        for spelling in ("first-ci", "First-CI", "FIRST-CI", "fIrSt-Ci"):
            holder = outer / spelling
            if holder.exists(): shutil.rmtree(holder, ignore_errors=True)
            nested = holder / "inner"; nested.mkdir(parents=True)
            self.assertTrue(_names_protected_run_probe(nested), spelling)
            with self.assertRaisesRegex(visual.VisualDecisionError, "protected first-ci path is forbidden"):
                visual.visual_workspace(nested.resolve(strict=True), "run-b", create=True)
            self.assertEqual([], sorted(item.name for item in nested.iterdir()), spelling)
            shutil.rmtree(holder, ignore_errors=True)
        self.assertEqual(before, visual.protected_run_inventory(project)["tree_sha256"])
        self.assertEqual(["sealed.txt"], sorted(item.name for item in sealed.rglob("*")))

    def test_a_project_root_is_the_spelling_the_file_system_stores(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        # A non-ASCII component gives the *normalization* form of the same
        # defect, which survives on a case-sensitive volume: NFC and NFD are
        # different strings for one directory wherever the file system folds
        # them, and "abspath == resolve" is true for both.
        project = base / unicodedata.normalize("NFC", "grüße-projekt"); project.mkdir()
        project = project.resolve(strict=True)
        self.assertEqual(project, visual.exact_project_root(project))
        for variant in (case_variant(project), normalization_variant(project)):
            if variant is None:
                continue
            self.assertTrue(same_file(variant, project))
            self.assertEqual(Path(os.path.abspath(variant)), Path(os.path.abspath(variant)).resolve(strict=True))
            with self.assertRaisesRegex(visual.VisualDecisionError, "exact canonical directory"):
                visual.exact_project_root(variant)
        # Two spellings of one project must never become two project identities.
        self.assertEqual(visual.project_identity(project), visual.project_identity(Path(str(project))))
        # An ASCII name, where swapping the case really is the fold the file
        # system applies: the answer has to be the *stored* spelling, compared
        # verbatim.  Comparing case-blind here would accept every variant again
        # and hand the run a project root the file system spells differently.
        ascii_project = (base / "asciiproject"); ascii_project.mkdir()
        ascii_project = ascii_project.resolve(strict=True)
        upper = ascii_project.parent / ascii_project.name.upper()
        if same_file(upper, ascii_project):
            stored = visual._stored_spelling(upper)
            if stored is not None:
                self.assertEqual(ascii_project, stored)
            self.assertFalse(visual._listed_verbatim(upper))
            with self.assertRaisesRegex(visual.VisualDecisionError, "exact canonical directory"):
                visual.exact_project_root(upper)
            with self.assertRaises(visual.VisualDecisionError):
                visual.visual_workspace(upper, "run-b", create=True)
        self.assertTrue(visual._listed_verbatim(ascii_project))

    # --- acceptance bar point 2: key and ledger, outside, in every spelling ---

    def affinity_inside(self) -> tuple[Path, Path, Path, dict]:
        """A complete bundle whose key and ledger deliberately sit in the project."""
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve()
        root = visual.visual_workspace(project, "run-a", create=True)
        secrets = project / ".brandkit" / "secrets"; secrets.mkdir(parents=True)
        return project, root, secrets, affinity_bundle(project, root, secrets)

    def test_the_key_and_the_ledger_are_kept_out_of_the_project_by_file_identity(self):
        project, _root, secrets, fixture = self.affinity_inside()
        spellings = [str(fixture["ledger_path"])]
        for maker in (case_variant, normalization_variant):
            variant = maker(secrets)
            if variant is not None:
                self.assertTrue(same_file(variant, secrets))
                spellings.append(str(variant / "replay-ledger.json"))
        # A path laundered through "..", and one laundered through a symlink
        # that lives outside the project: both are strings that no prefix test
        # recognises, and both name a file inside the project.
        spellings.append(str(project.parent / project.name / ".brandkit" / "x" / ".." / "secrets" / "replay-ledger.json"))
        outside = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, outside, True)
        (outside / "alias").symlink_to(secrets, target_is_directory=True)
        spellings.append(str(outside / "alias" / "replay-ledger.json"))
        for spelling in spellings:
            with self.assertRaisesRegex(visual.VisualDecisionError, "must remain outside the project"):
                visual.validate_bundle(project, "run-a", NOW, sidecar_root=visual.ROOT.parents[1],
                                       affinity_auth={**fixture["auth"], "replay_ledger_path": spelling})
            with self.assertRaisesRegex(visual.VisualDecisionError, "must remain outside the project"):
                visual.validate_bundle(project, "run-a", NOW, sidecar_root=visual.ROOT.parents[1],
                                       affinity_auth={**fixture["auth"], "owner_key_path": spelling})
        # The containment primitive itself, so a caller that stops resolving is
        # not covered only by whatever the next check happens to reject: a
        # symlink outside the project that points into it *is* inside it.
        self.assertTrue(visual._identity_contains(project, outside / "alias" / "replay-ledger.json"))
        self.assertTrue(visual._identity_contains(project, secrets))
        self.assertTrue(visual._identity_contains(project, project))
        self.assertFalse(visual._identity_contains(project, outside))
        self.assertFalse(visual._identity_contains(project, project.parent))
        # Nothing was locked, read or committed on the way to those refusals.
        self.assertEqual(fixture["genesis_raw"], fixture["ledger_path"].read_bytes())
        self.assertEqual([], [item.name for item in project.rglob("*") if item.name.startswith(".affinity-lock.")])
        # The same bundle with the very same files outside the project is fine,
        # so the rule is the *place* and not the bundle.
        elsewhere = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, elsewhere, True)
        good_project, good_root = self.project()
        good = affinity_bundle(good_project, good_root, elsewhere)
        result = visual.validate_bundle(good_project, "run-a", NOW, sidecar_root=visual.ROOT.parents[1], affinity_auth=good["auth"])
        self.assertEqual(["lockup-choice", "logo-choice"], sorted(item["id"] for item in result["eligible_candidates"]))

    def test_replay_protection_is_exactly_write_control_over_the_external_ledger(self):
        elsewhere = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, elsewhere, True)
        project, root = self.project()
        fixture = affinity_bundle(project, root, elsewhere)
        def run() -> str:
            try:
                visual.validate_bundle(project, "run-a", NOW, sidecar_root=visual.ROOT.parents[1], affinity_auth=fixture["auth"])
                return "ok"
            except visual.VisualDecisionError as exc:
                return str(exc)
        self.assertEqual("ok", run())
        self.assertEqual(1, json.loads(fixture["ledger_path"].read_bytes())["generation"])
        self.assertIn("already consumed", run())
        # Restoring the earlier, still validly signed bytes frees the nonces
        # again.  That is the accepted model - the ledger is an owner-owned
        # file and whoever may rewrite it may replay it - and it is exactly why
        # the ledger may never live where the untrusted run can write.  The
        # assertion above and this one together are the whole property.
        fixture["ledger_path"].write_bytes(fixture["genesis_raw"]); fixture["ledger_path"].chmod(0o600)
        self.assertEqual("ok", run())
        # Truncating the nonce list without the key is not a rollback: the
        # signature is over the whole state, so an edited ledger is refused.
        edited = json.loads(fixture["ledger_path"].read_bytes()); edited["nonces"] = []
        fixture["ledger_path"].write_bytes(visual.canonical_json(edited)); fixture["ledger_path"].chmod(0o600)
        self.assertIn("authentication/scope failed", run())

    def test_a_run_local_sidecar_is_not_an_independent_source_check(self):
        project, root = self.project()
        elsewhere = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, elsewhere, True)
        fixture = affinity_bundle(project, root, elsewhere)
        # The sidecar exists to be a second, run-independent copy of the
        # narrative.  A sidecar root the run itself writes is not independent,
        # however faithful its bytes are, so the place is enforced rather than
        # merely defaulted.
        release = visual.ROOT / "visual-decision-sources.v1.json"
        for inside in (root, root / "sheet", project, project / ".brandkit"):
            with self.assertRaisesRegex(visual.VisualDecisionError, "sidecar must remain outside the project"):
                visual.validate_bundle(project, "run-a", NOW, sidecar_root=inside, affinity_auth=fixture["auth"])
        variant = case_variant(project)
        if variant is not None:
            with self.assertRaisesRegex(visual.VisualDecisionError, "sidecar must remain outside the project"):
                visual.validate_bundle(project, "run-a", NOW, sidecar_root=variant / ".brandkit", affinity_auth=fixture["auth"])
        self.assertTrue(release.is_file())
        self.assertEqual(fixture["genesis_raw"], fixture["ledger_path"].read_bytes())
        result = visual.validate_bundle(project, "run-a", NOW, sidecar_root=visual.ROOT.parents[1], affinity_auth=fixture["auth"])
        self.assertEqual(["lockup-choice", "logo-choice"], sorted(item["id"] for item in result["eligible_candidates"]))

    # --- fail-closed instead of fail-slow ------------------------------------

    def test_a_bundle_member_that_is_not_a_regular_file_fails_closed_at_once(self):
        project, root = self.project()
        fifo = root / "visual-brief.json"; os.mkfifo(fifo, 0o600)
        # O_NOFOLLOW stops a symlink but not a FIFO, and open(O_RDONLY) on a
        # FIFO with no writer blocks for ever - the run replaces its own brief
        # with one and validation never returns.  A guard that only fires after
        # the open is no guard at all.
        with deadline(20, "secure_read on a FIFO"):
            with self.assertRaisesRegex(visual.VisualDecisionError, "regular file"):
                visual.secure_read(root, "visual-brief.json", "visual brief")
        with deadline(20, "load_json on a FIFO"):
            with self.assertRaises(visual.VisualDecisionError):
                visual.load_json(Path(), root, "visual-brief.json", "visual brief")
        secrets = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, secrets, True)
        key_fifo = secrets / "owner.key"; os.mkfifo(key_fifo, 0o600)
        with deadline(20, "_external_0600 on a FIFO"):
            with self.assertRaisesRegex(visual.VisualDecisionError, "exclusive 0600 regular file"):
                visual._external_0600(key_fifo, "Affinity owner key")
        sealed = project / ".brandkit/work/first-ci"; sealed.mkdir(parents=True)
        os.mkfifo(sealed / "pipe", 0o600)
        with deadline(20, "protected_run_inventory over a FIFO member"):
            with self.assertRaises(visual.VisualDecisionError):
                visual.protected_run_inventory(project)

    # --- the escape rule, pinned in the primitive and not only on one path ----

    def test_a_crlf_pair_is_one_newline_in_the_primitive_and_in_every_stage(self):
        # The historical defect of this scanner in its smallest form.  r12
        # showed it pinned on the hex path and unpinned in the primitive: a
        # mutant that made _css_newline_run count CRLF as one character turned
        # a rejection into an acceptance without a single test going red.
        self.assertEqual(2, visual._css_newline_run("\r\n", 0))
        for single in ("\n", "\r", "\f", "\n\r", "\r\r", "\f\n"):
            self.assertEqual(1, visual._css_newline_run(single, 0))
        for none in ("", "a", " ", "\t", "x\r\n"):
            self.assertEqual(0, visual._css_newline_run(none, 0))
        # The line continuation of 4.3.5 swallows the pair, never half of it ...
        self.assertEqual((3, "", visual._ESCAPE_CONTINUATION), visual._consume_escape("\\\r\nx", 0, in_string=True))
        self.assertEqual((0, None, visual._ESCAPE_NONE), visual._consume_escape("\\\r\nx", 0, in_string=False))
        # ... and so does the one whitespace character a hex escape may carry.
        self.assertEqual((5, "A", visual._ESCAPE_HEX), visual._consume_escape("\\41\r\nx", 0, in_string=True))
        self.assertEqual((5, "A", visual._ESCAPE_HEX), visual._consume_escape("\\41\r\nx", 0, in_string=False))
        # The decode stage is no longer a second implementation of that rule,
        # so it answers identically instead of merely similarly.
        self.assertEqual("ab", visual._decode_css_escapes("a\\\r\nb"))
        self.assertEqual("Ax", visual._decode_css_escapes("\\41\r\nx"))
        self.assertEqual("A\nx", visual._decode_css_escapes("\\41\r\n\nx"))
        # End to end: splitting the pair anywhere lets these two reassemble
        # into something the browser reads and the scan no longer sees.
        self.assertIn("@import", visual._security_canonical_css("}@\\\r\n\\\nimport"))
        self.assertIn("http:", visual._security_canonical_css('b{content:"htt\\70\r\n://evil.invalid/x"}'))
        self.assertIn("https:", visual._security_canonical_css('\'\\22\\\r\n@\\29/*\nhttps:\\\r\n\r\n\\75\t'))

    def test_a_single_character_escape_decodes_to_that_character_in_every_stage(self):
        # The last branch of the shared routine, and the one r12 left unpinned:
        # a mutant that let it decode to nothing turned a rejection into an
        # acceptance.  It is one line, and it is load-bearing in all three
        # token states plus the decode stage.
        for text, decoded in (("\\(", "("), ("\\)", ")"), ("\\{", "{"), ("\\\"", "\""),
                              ("\\'", "'"), ("\\/", "/"), ("\\*", "*"), ("\\g", "g"), ("\\ ", " ")):
            self.assertEqual((2, decoded, visual._ESCAPE_LITERAL), visual._consume_escape(text, 0, in_string=False))
            self.assertEqual(decoded, visual._decode_css_escapes(text))
        # The ident branch reads the same escape, which is why "u\72l(" is a
        # url token and not three tokens.
        self.assertEqual((5, "url"), visual._css_ident_run("u\\72l(", 0))
        self.assertEqual((5, "url"), visual._css_ident_run("\\75rl(", 0))
        # The decoded character is what the scan then sees, so dropping it -
        # the r12 mutant that made this branch decode to nothing - turns a
        # rejection into an acceptance.  "htt" + "\\p" + ":" is "http:" to the
        # browser and to the scan; losing the "p" would leave "htt:".
        self.assertEqual("\\", visual._decode_css_escapes("\\5c"))
        self.assertIn("http:", visual._security_canonical_css('b{content:"htt\\p:"}'))
        self.assertIn("@import", visual._security_canonical_css('\\@import "x";'))
        with self.assertRaisesRegex(visual.VisualDecisionError, "undecodable escape"):
            visual._security_canonical_css('\\(url(\\t\\)}@\n\\75\\5c/*a{*/')

    def test_the_shared_escape_routine_always_makes_progress_inside_a_string(self):
        # r12 killed a mutant here only by hanging the suite: the string branch
        # of the canonicaliser trusted that this routine can never stand still
        # with in_string=True, and when that broke it span for ever.  A hang is
        # not a measurement, so the invariant is stated and the scanner is run
        # under a wall clock.
        alphabet = ["", "a", "0", "\\", "\n", "\r", "\r\n", "\f", " ", "\t", "\"", "'", ")", "(", "41", "0041", "10FFFF", "D800", "0"]
        for tail in alphabet:
            text = "\\" + tail
            for in_string in (True, False):
                end, value, kind = visual._consume_escape(text, 0, in_string=in_string)
                if kind == visual._ESCAPE_NONE:
                    self.assertFalse(in_string, f"a string escape must never be {kind}: {text!r}")
                    self.assertIsNone(value); self.assertEqual(0, end)
                else:
                    self.assertIsNotNone(value, f"{text!r} in_string={in_string}")
                    self.assertGreater(end, 0, f"{text!r} in_string={in_string}")
        payloads = ['a{content:"' + item + '"}' for item in ("\\", "\\\\", "x\\", "\\\r\n", "\\\n", "\\41", "/*", "*/")]
        payloads += ['a{background:url(' + item for item in ("", " ", "\\", "assets/a.svg", "assets/a.svg/*")]
        payloads += ["a{background:url(", "url(", '"', "'", "\\", "/*", "@", "#"]
        with deadline(30, "the CSS canonicaliser over degenerate input"):
            for payload in payloads:
                try:
                    visual._security_canonical_css(payload)
                except visual.VisualDecisionError:
                    pass

    def test_a_url_token_that_runs_to_the_end_of_input_is_read_not_crashed(self):
        check = self.sheet()
        # 4.3.6 ends an unquoted url token at ")" or at end of input.  Nothing
        # fed the scanner the second case, so the guard that keeps the quote
        # test from stepping past the end was unmeasured - and a crash inside
        # the canonicaliser is not a fail-closed rejection, it is an escape
        # from the VisualDecisionError boundary the CLI relies on.
        with deadline(20, "the CSS canonicaliser at end of input"):
            for tail in ("a{background:url(", "a{background:url( ", "a{background:url(\t",
                         "a{background:url(\n", "a{background:url(\r\n", "a{background:url",
                         "a{background:url(\"", "a{background:url('", "a{background:url()"):
                self.assertIsInstance(visual._security_canonical_css(tail), str)
        for tail in ("b{background:url(", "b{background:url( ", "b{background:url()"):
            with self.assertRaisesRegex(visual.VisualDecisionError, "asset references differ"):
                check(self.LOCAL + tail)

    def test_the_style_sheet_this_module_renders_passes_its_own_offline_scan(self):
        check = self.sheet()
        # The scanner refuses a CSS function it does not know, which is only a
        # gate and not a wall as long as the CSS this module itself writes goes
        # through it.  Nothing measured the two against each other before, so a
        # renderer that grew one more function would have shipped a sheet its
        # own validator rejects - or, worse, a validator relaxed until it did
        # not.  The renderer's text is fed here verbatim.
        faces = ('@font-face{font-family:"VisualCandidate0";src:url("assets/b.svg") format("woff2");font-display:block}'
                 ' .font-0 .specimen{font-family:"VisualCandidate0",sans-serif}')
        swatches = ".swatch-0-0{border-left-color:#FFFFFF} .swatch-0-1{border-left-color:#111111}"
        css = visual._sheet_css(faces, swatches)
        references, functions = visual._css_resource_references(visual._security_canonical_css(css))
        self.assertEqual({"assets/b.svg"}, references)
        self.assertEqual(set(), functions - visual.CSS_ALLOWED_FUNCTIONS)
        check(css + self.LOCAL, assets=("b.svg",))
        # An empty sheet - no colour reports, no fonts - is the other end of
        # the same template and must pass too.
        bare = visual._sheet_css("", "")
        self.assertEqual((set(), {"var", "repeat", "minmax"}), visual._css_resource_references(visual._security_canonical_css(bare)))
        check(bare + self.LOCAL)




class VisualDecisionCoreR15Tests(TimeLimitedTestCase):
    """The four holes r14 opened, stated as rules rather than as payloads.

    Two of them are one defect with two faces: the reference scanner tokenized
    a text in which escapes had *already* been decoded, so a ``\\29`` was a
    closing paren and a ``\\22`` was a string delimiter for this scanner while
    both are ordinary content for Blink.  The rule is therefore stated once -
    a decoded code point is the content of the token it stands in and never a
    delimiter of it - and probed from the url side, the string side and the
    contract.  The third is about *scope*: an apparatus that runs over one file
    name says nothing about a bundle.  The fourth is about *when* an identity is
    taken: a guard that re-derives its anchor from a path disarms itself the
    moment the path stops resolving.
    """

    SECTIONS = ("candidates-color", "candidates-typography", "candidates-code-font",
                "candidates-logo", "candidates-lockup", "recommendations", "owner-review")
    LOCAL = 'a{background:url(assets/a.svg)}'

    def project(self) -> tuple[Path, Path]:
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve()
        return project, visual.visual_workspace(project, "run-a", create=True)

    def page(self, links: tuple[str, ...] = ("style.css",), *, rel: str = "stylesheet") -> str:
        sections = "".join(f'<section id="{name}"><h2>x</h2></section>' for name in self.SECTIONS)
        head = "".join(f'<link rel="{rel}" href="{href}">' for href in links)
        return ('<!doctype html><html lang="de"><head><meta charset="utf-8">'
                f'<meta http-equiv="Content-Security-Policy" content="{visual.SHEET_CSP}">'
                + head + '<title>x</title></head><body><main>' + sections + '</main></body></html>')

    def sheet(self):
        """Validate one bundle: style.css, the page, and any extra members it carries."""
        _project, root = self.project(); sheet = root / "sheet"
        (sheet / "assets").mkdir(parents=True); (sheet / "assets/a.svg").write_text("svg")
        def check(css: str, *, page: str | None = None, extra: dict[str, str] | None = None,
                  off_manifest: dict[str, str] | None = None) -> None:
            for stale in sorted(sheet.rglob("*"), reverse=True):
                if stale.relative_to(sheet).as_posix() in {"assets", "assets/a.svg"}:
                    continue
                stale.rmdir() if stale.is_dir() and not stale.is_symlink() else stale.unlink()
            (sheet / "style.css").write_bytes(css.encode("utf-8"))
            (sheet / "index.html").write_bytes((self.page() if page is None else page).encode("utf-8"))
            members = [sheet / "index.html", sheet / "style.css", sheet / "assets/a.svg"]
            for relative, content in (extra or {}).items():
                target = sheet / relative; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content); members.append(target)
            for relative, content in (off_manifest or {}).items():
                target = sheet / relative; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            files = [{"path": item.relative_to(sheet).as_posix(), "sha256": visual.sha256_bytes(item.read_bytes())} for item in members]
            manifest = {"schema_version": "brandkit-visual-sheet-manifest.v1", "brief_sha256": "a" * 64,
                        "candidate_set_sha256": "b" * 64, "affinity_observation_sha256": "c" * 64, "files": files,
                        "viewports": [320, 768, 1440], "offline": True, "interactive_controls": False,
                        "preselection": False, "normative": False, "human_review_status": "required"}
            with deadline(60, "validate_sheet_manifest"):
                visual.validate_sheet_manifest(manifest, sheet)
        return check

    # --- acceptance bar point 3: the scan covers the bundle, not one file name ---

    def test_every_stylesheet_the_bundle_carries_goes_through_the_same_offline_scan(self):
        check = self.sheet()
        two = self.page(("assets/second.css", "style.css"))
        ghost = {"ghost.svg": "svg", "assets/ghost.svg": "svg"}
        # The whole offline apparatus used to run over contents["style.css"] and
        # nothing else, so a second <link rel="stylesheet"> was a regular
        # manifest member whose CSS no rule below ever touched: comments,
        # escapes, the scheme tokens, the function allowlist and the manifest
        # equality all simply did not apply to it.  Each of those rules is
        # probed here through the *second* sheet.
        for payload, reason in (
                ('main{background-image:url(../ghost.svg)!important}', "asset references differ"),
                ('main{background-image:url(ghost.svg)}', "asset references differ"),
                ('main{background:url(https://evil.invalid/x)}', "network or active content"),
                ('@import "../ghost.svg";', "network or active content"),
                ('main{background:paint(worklet)}', "unsupported function"),
                ('main{background-image:image-set("ghost.svg" 1x)}', "asset references differ"),
                ('main{content:"A\\22 x";background-image:image-set("ghost.svg" 1x)}', "asset references differ"),
                ('main{background-image:url(a.svg\\29 /../ghost.svg)}', "asset references differ"),
                ('main{background:url(/*)}b{background:url(https://evil.invalid/x)}', "network or active content")):
            with self.assertRaisesRegex(visual.VisualDecisionError, reason):
                check(self.LOCAL, page=two, extra={"assets/second.css": payload}, off_manifest=ghost)
        # ... and the other direction, so the rule is a scan and not a ban: a
        # second stylesheet that names a real member is accepted, and its
        # reference is resolved against *its own* directory rather than the
        # sheet root.
        check(self.LOCAL, page=two, extra={"assets/second.css": 'main{background:url(b.svg)}', "assets/b.svg": "svg"})
        check(self.LOCAL, page=two, extra={"assets/second.css": 'main{background-image:image-set("b.svg" 1x)}', "assets/b.svg": "svg"})
        check(self.LOCAL, page=two, extra={"assets/second.css": "main{color:red}"})

    def test_a_sheet_loads_exactly_the_stylesheets_the_manifest_carries_as_css(self):
        check = self.sheet()
        # "rel" was collected and never limited, which is what made a second
        # stylesheet a legal member in the first place.  It is now an allowlist
        # of one value, spelled exactly - a browser folds the case of a link
        # relation, so accepting "Stylesheet" here while refusing to scan it
        # would be the same hole in a different spelling.
        for rel in ("preload", "icon", "Stylesheet", "STYLESHEET", "stylesheet alternate", "alternate stylesheet", ""):
            with self.assertRaisesRegex(visual.VisualDecisionError, "forbidden link relation"):
                check(self.LOCAL, page=self.page(("style.css",), rel=rel))
        with self.assertRaisesRegex(visual.VisualDecisionError, "link without a target"):
            check(self.LOCAL, page=self.page(("",)))
        # Both directions of "every CSS the browser loads is a CSS this
        # apparatus read": a CSS member the page never links is as much a hole
        # as a link the manifest does not carry as CSS.
        with self.assertRaisesRegex(visual.VisualDecisionError, "stylesheet links differ"):
            check(self.LOCAL, extra={"assets/second.css": "main{color:red}"})
        with self.assertRaisesRegex(visual.VisualDecisionError, "stylesheet links differ"):
            check(self.LOCAL, page=self.page(("assets/a.svg", "style.css")))
        with self.assertRaisesRegex(visual.VisualDecisionError, "stylesheet links differ"):
            check(self.LOCAL, page=self.page(()))
        # The member grammar is closed, so "which member is CSS" is a question
        # about the manifest rather than a guess about a file name.
        for path in ("second.css", "assets/x.js", "notes.txt", "assets/.hidden.svg",
                     "assets/sub/x.svg", "assets/x", "manifest.json", "assets/a.SVG"):
            with self.assertRaisesRegex(visual.VisualDecisionError, "not an allowed sheet file"):
                check(self.LOCAL, extra={path: "x"})
        self.assertEqual("html", visual._sheet_member_kind("index.html"))
        self.assertEqual("css", visual._sheet_member_kind("style.css"))
        self.assertEqual("css", visual._sheet_member_kind("assets/second.css"))
        self.assertEqual("asset", visual._sheet_member_kind("assets/a-1.svg"))
        self.assertEqual("asset", visual._sheet_member_kind("assets/a.woff2"))

    # --- acceptance bar point 3: an escape is content, never a delimiter ------

    def test_a_decoded_escape_is_the_content_of_its_token_and_never_a_delimiter(self):
        # The scanner used to read a text whose escapes were already decoded, so
        # a "\\29" had become a ")" that ends a url token Blink reads straight
        # past.  The reference it reported was a *prefix* of the real one - and
        # a prefix that happens to be a manifest member passes the equality
        # check while the browser fetches something else.
        self.assertEqual({"assets/a.svg)/../../evil.svg"},
                         visual._css_resource_references(r'main{background-image:url(assets/a.svg\29 /../../evil.svg)}')[0])
        self.assertEqual({"a)b"}, visual._css_resource_references(r'a{background:url(a\29 b)}')[0])
        self.assertEqual({"a)b"}, visual._css_resource_references(r'a{background:url(a\000029 b)}')[0])
        # ... and the same defect through the string branch: a "\\22" became a
        # quote that flipped the parity of everything behind it and hid every
        # following reference - from this scanner, from the function allowlist
        # and from the url( regex alike.
        references, functions = visual._css_resource_references(r'main{content:"A\22 x";background-image:image-set("evil.svg" 1x)}')
        self.assertEqual({"evil.svg"}, references)
        self.assertEqual({"image-set"}, functions)
        self.assertEqual((set(), set()), visual._css_resource_references(r'main{content:"A\22 x"}'))
        self.assertEqual({"//evil.invalid/x.svg"},
                         visual._css_resource_references(r'a{content:"\22";background:image-set("//evil.invalid/x.svg" 1x)}')[0])
        # The escape that carries a *literal* delimiter is the sharp case in
        # each of the two branches: a backslash before a quote does not end the
        # string and a backslash before a ")" does not end the url token, so
        # neither may be found by looking for the bare character.
        self.assertEqual({"evil.svg"}, visual._css_resource_references(
            r'a{content:"A\"x";background:image-set("evil.svg" 1x)}')[0])
        self.assertEqual({"evil.svg"}, visual._css_resource_references(
            'a{content:"A\\\n x";background:image-set("evil.svg" 1x)}')[0])
        self.assertEqual({"a)b"}, visual._css_resource_references(r'a{background:url(a\)b)}')[0])
        self.assertEqual({"a(b"}, visual._css_resource_references(r'a{background:url(a\(b)}')[0])
        # Whitespace is stripped from the *raw* token, before decoding: an
        # escaped space belongs to the URL and a raw one does not, which is
        # exactly the difference between "a " and "a".
        self.assertEqual({"a "}, visual._css_resource_references(r'a{background:url(a\20)}')[0])
        self.assertEqual({"a"}, visual._css_resource_references('a{background:url( a )}')[0])
        # A string that reaches end of input still yields its decoded value.
        self.assertEqual({"aAb"}, visual._css_resource_references(r'@font-face{src:"a\41 b')[0])
        # An escape glued to a function name belongs to the ident, so the run
        # spells a function this scanner has never seen - and the raw pass is
        # the only one that can tell, because decoding first turns the escape
        # into whitespace that ends the ident.
        self.assertEqual({"\timage-set"}, visual._css_resource_references(r'b{background:\9image-set("evil.svg")}')[1])
        self.assertEqual({"image-set"}, visual._css_resource_references(
            visual._security_canonical_css(r'b{background:\9image-set("evil.svg")}'))[1])
        # ... and the allowlist is applied to the union of both passes, or the
        # decoded reading - where the escape has become whitespace that ends the
        # ident - would let that unknown function through under the name of an
        # allowed one.
        check = self.sheet()
        for hidden in (r'b{width:\9calc(1px)}', r'b{width:\20 calc(1px)}', r'b{color:\9var(--x)}'):
            with self.assertRaisesRegex(visual.VisualDecisionError, "unsupported function"):
                check(self.LOCAL + hidden)
        check(self.LOCAL + 'b{width:calc(1px);color:var(--x)}')
        # The decoded text is exactly where both readings used to be taken, and
        # on it the reference is still invisible - which is why the scan that
        # decides a token boundary may not be the one that runs on it.
        self.assertEqual(set(), visual._css_resource_references(
            visual._security_canonical_css(r'main{content:"A\22 x";background-image:image-set("evil.svg" 1x)}'))[0])
        # Escapes that name a manifest member are decoded to that member, so the
        # rule reads values as the browser reads them in both directions.
        self.assertEqual({"assets/a.svg"}, visual._css_resource_references(r'a{background:url(assets/a\2e svg)}')[0])
        self.assertEqual({"assets/a.svg"}, visual._css_resource_references(r'a{background:image-set("assets/a\2e svg")}')[0])
        # ... and through the contract, where each of them is a bundle that
        # r14 accepted while Blink fetched a file the manifest does not carry.
        check = self.sheet()
        for payload in (r'main{background-image:url(assets/a.svg\29 /../../evil.svg)}',
                        r'main{content:"A\22 x"; background-image:image-set("evil.svg" 1x)}',
                        r'main{content:"A\22 x"; background-image:image-set("//127.0.0.1:1/x.svg" 1x)}',
                        r'main{content:"A\22 x"; background-image:url(evil.svg)}',
                        r'main{background-image:url(assets/a.svg\29 /../../assets/ghost.svg)}'):
            with self.assertRaisesRegex(visual.VisualDecisionError, "asset references differ"):
                check(self.LOCAL + payload, off_manifest={"evil.svg": "svg"})
        check(self.LOCAL + r'b{background:url(assets/a\2e svg)}')
        check(self.LOCAL + r'b{content:"A\22 x"}')

    def test_a_reference_is_resolved_against_the_stylesheet_that_names_it(self):
        assets = visual.PurePosixPath("assets")
        root = visual.PurePosixPath(".")
        self.assertEqual("assets/b.svg", visual._sheet_css_reference(assets, "b.svg"))
        self.assertEqual("assets/deep/b.svg", visual._sheet_css_reference(assets, "deep/b.svg"))
        self.assertEqual("b.svg", visual._sheet_css_reference(root, "b.svg"))
        self.assertEqual("assets/b.svg", visual._sheet_css_reference(root, "assets/b.svg"))
        # Nothing is normalized into agreement: a reference that is not already
        # a manifest path is handed on as its own text, and no manifest member
        # can equal it, so the closed-manifest equality refuses it.
        for unresolvable in ("../ghost.svg", "./assets/a.svg", "/etc/passwd", "//host/x", "",
                             "assets//a.svg", "assets/x/../a.svg", "assets/.", "a.svg)/../../evil.svg"):
            self.assertEqual(unresolvable, visual._sheet_css_reference(assets, unresolvable))
            self.assertEqual(unresolvable, visual._sheet_css_reference(root, unresolvable))

    def test_the_property_in_front_of_a_colon_is_read_as_an_ident_run_and_not_backwards(self):
        # Walking backwards over ident code points is a third reading of the
        # same text and it stops at a backslash, so an escaped descriptor name
        # spelled the property something else and the src: string rule never
        # applied.  The ident run in front of the colon is already consumed with
        # its escapes, so that run *is* the property.
        for css in (r'@font-face{s\72 c:"assets/ghost.svg"}',
                    r'@font-face{\73 rc:"assets/ghost.svg"}',
                    r'@font-face{SRC:"assets/ghost.svg"}',
                    '@font-face{src :"assets/ghost.svg"}'):
            self.assertEqual({"assets/ghost.svg"}, visual._css_resource_references(css)[0], css)
        # ... and only when the run really stands in front of the colon.
        self.assertEqual(set(), visual._css_resource_references('@font-face{src x:"assets/ghost.svg"}')[0])
        self.assertEqual(set(), visual._css_resource_references('@font-face{:"assets/ghost.svg"}')[0])
        self.assertEqual(set(), visual._css_resource_references('@font-face{src ):"assets/ghost.svg"}')[0])
        self.assertEqual(set(), visual._css_resource_references('@font-face{src}:"assets/ghost.svg"')[0])
        # The second, decoded pass is an over-approximation and stays one: an
        # escaped colon is an ident code point to a tokenizer, so the raw pass
        # sees no descriptor at all while the decoded pass does - and the union
        # is what keeps the answer on the refusing side.
        self.assertEqual(set(), visual._css_resource_references(r'@font-face{src\3a "assets/ghost.svg"}')[0])
        check = self.sheet()
        for css in (r'@font-face{s\72 c:"assets/ghost.svg"}',
                    r'@font-face{\73 rc:"assets/ghost.svg"}',
                    r'@font-face{src\3a "assets/ghost.svg"}'):
            with self.assertRaisesRegex(visual.VisualDecisionError, "asset references differ"):
                check(self.LOCAL + css)

    # --- acceptance bar point 4: the sealed run, by identity and not by name --

    def test_the_sealed_run_is_refused_as_an_output_root_on_its_own(self):
        # Reachable only through visual_workspace today; the check costs one
        # string test and does not depend on that staying true.
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        for spelling in ("first-ci", "FIRST-CI", "First-Ci"):
            sealed = base / spelling / "visual-decisions"; sealed.mkdir(parents=True)
            with self.assertRaisesRegex(visual.VisualDecisionError, "protected first-ci path is forbidden"):
                visual._safe_output_dir(sealed / "sheet")
            self.assertEqual([], sorted(item.name for item in sealed.iterdir()))
            shutil.rmtree(base / spelling)
        ordinary = base / "run-a" / "visual-decisions"; ordinary.mkdir(parents=True)
        visual.os.close(visual._safe_output_dir(ordinary / "sheet"))

    def test_the_kernel_answer_for_a_stored_spelling_is_really_asked_for(self):
        # r14-03 ("_stored_spelling always answers None") survived because the
        # portable fallback carried every case it was measured on.  The primary
        # path has a contract of its own: where the platform defines F_GETPATH
        # it must answer, and it must answer the stored spelling.
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = (Path(temp.name).resolve() / "Stored-Spelling"); project.mkdir()
        project = project.resolve(strict=True)
        if visual._F_GETPATH is None:
            self.assertIsNone(visual._stored_spelling(project))
            return
        self.assertEqual(project, visual._stored_spelling(project))
        self.assertTrue(visual._stored_spelling(project).is_absolute())
        variant = case_variant(project)
        if variant is not None:
            self.assertEqual(project, visual._stored_spelling(variant))
        self.assertIsNone(visual._stored_spelling(project / "absent"))

    def sealed_project(self) -> Path:
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = (Path(temp.name).resolve() / "proj")
        (project / ".brandkit/work/first-ci").mkdir(parents=True)
        (project / ".brandkit/work/first-ci/sealed.txt").write_text("sealed")
        return project.resolve(strict=True)

    def test_the_sealed_run_survives_a_swap_of_the_work_directory_in_the_creation_window(self):
        # The window between the guard and the creation is admitted by the build
        # itself, and r14 measured what happens inside it: the sealed tree
        # became the run's work root, because the guard re-derived its anchor
        # from a path that the swap had just made unresolvable.  The anchor is
        # now taken once, from a descriptor held for the whole call, and the
        # walk is descriptor-relative - so the swap can neither redirect a
        # component that is already open nor disarm the anchor.
        original = visual.os.mkdir
        self.addCleanup(setattr, visual.os, "mkdir", original)
        for trigger, upfront, reason in ((".brandkit", False, "linked or non-directory component"),
                                         ("work", False, "linked or non-directory component"),
                                         ("myrun", False, "protected first-ci path is forbidden"),
                                         ("visual-decisions", False, "protected first-ci path is forbidden"),
                                         # A work directory that is *already* a
                                         # symlink when the call starts is now
                                         # refused one guard earlier: the run
                                         # identity cannot be established
                                         # through a linked component, and
                                         # "cannot establish" is a refusal.  The
                                         # four legs above still enter the walk
                                         # and are still stopped by it.
                                         (None, True, "run discovery handoff cannot be identified")):
            project = self.sealed_project()
            before = visual.protected_run_inventory(project)
            def swap() -> None:
                work = Path(os.path.realpath(project / ".brandkit/work"))
                os.rename(work, work.parent / "work_real")
                os.symlink(work.parent / "work_real/first-ci", work, target_is_directory=True)
            fired = [False]
            def hooked(path, mode=0o777, *, dir_fd=None):
                if path == trigger and not fired[0]:
                    fired[0] = True; swap()
                return original(path, mode, dir_fd=dir_fd)
            if upfront: swap()
            visual.os.mkdir = hooked
            try:
                with self.assertRaisesRegex(visual.VisualDecisionError, reason):
                    visual.visual_workspace(project, "myrun", create=True)
            finally:
                visual.os.mkdir = original
                link = project / ".brandkit/work"
                if link.is_symlink():
                    os.unlink(link); os.rename(link.parent / "work_real", link)
            self.assertTrue(fired[0] or upfront, trigger)
            after = visual.protected_run_inventory(project)
            self.assertEqual(before["tree_sha256"], after["tree_sha256"], trigger)
            self.assertEqual(["sealed.txt"], sorted(item.name for item in (project / ".brandkit/work/first-ci").iterdir()))

    def test_the_path_that_is_returned_is_the_directory_that_was_walked(self):
        # The walk validates descriptors; the caller gets a path, and a path is
        # resolved again by whoever uses it.  The window between the last guard
        # and the return is simulated deterministically here - the swap runs
        # from inside the closing guard, after it has computed its answer - so
        # the closing check is the only thing left that can notice.
        project = self.sealed_project()
        before = visual.protected_run_inventory(project)
        workspace = project / ".brandkit/work/myrun/visual-decisions"
        original = visual._identity_holds
        self.addCleanup(setattr, visual, "_identity_holds", original)
        def hooked(anchor, candidate):
            answer = original(anchor, candidate)
            if candidate == workspace:
                work = Path(os.path.realpath(project / ".brandkit/work"))
                os.rename(work, work.parent / "work_real")
                os.symlink(work.parent / "work_real/first-ci", work, target_is_directory=True)
            return answer
        visual._identity_holds = hooked
        try:
            with self.assertRaisesRegex(visual.VisualDecisionError, "changed while it was being opened|workspace is absent"):
                visual.visual_workspace(project, "myrun", create=True)
        finally:
            visual._identity_holds = original
            link = project / ".brandkit/work"
            if link.is_symlink():
                os.unlink(link); os.rename(link.parent / "work_real", link)
        self.assertEqual(before["tree_sha256"], visual.protected_run_inventory(project)["tree_sha256"])

    def test_a_sealed_run_whose_identity_cannot_be_established_fails_the_call_closed(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve()
        work = project / ".brandkit/work"; work.mkdir(parents=True)
        elsewhere = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, elsewhere, True)
        # "I cannot tell what this is" is not "there is nothing here".  A
        # symlinked first-ci resolves to a directory that is not the sealed
        # tree, and protected_run_inventory refuses exactly that shape; running
        # on unanchored is the one answer this call may not give.
        (work / "first-ci").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(visual.VisualDecisionError, "cannot be identified"):
            visual.visual_workspace(project, "run-b", create=True)
        with self.assertRaisesRegex(visual.VisualDecisionError, "absent or linked"):
            visual.protected_run_inventory(project)
        (work / "first-ci").unlink()
        # An unreadable work directory is the same answer for the same reason.
        os.chmod(work, 0o000); self.addCleanup(os.chmod, work, 0o700)
        with self.assertRaisesRegex(visual.VisualDecisionError, "cannot be identified"):
            visual.visual_workspace(project, "run-b", create=True)
        os.chmod(work, 0o700)
        # An absent first-ci really is absent: there is nothing to anchor to and
        # the ordinary run proceeds, so the fail-closed answer above is about
        # uncertainty and not about strictness.
        self.assertEqual(project / ".brandkit/work/run-b/visual-decisions",
                         visual.visual_workspace(project, "run-b", create=True))
        self.assertEqual([], sorted(item.name for item in work.iterdir() if item.name == "first-ci"))

    def test_containment_is_never_hidden_by_an_unreadable_ancestor(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve()
        middle = project / "middle"; middle.mkdir()
        leaf = middle / "leaf"; leaf.mkdir()
        target = leaf / "replay-ledger.json"; target.write_text("{}")
        os.chmod(middle, 0o000); self.addCleanup(os.chmod, middle, 0o700)
        # The ancestor walk may not stop at the first component it cannot stat:
        # an unreadable intermediate directory would otherwise hide the
        # containment and hand the run a ledger inside its own project.
        with self.assertRaises(OSError):
            os.stat(target)
        self.assertTrue(visual._identity_contains(project, target))
        self.assertTrue(visual._identity_contains(project, leaf))
        with self.assertRaisesRegex(visual.VisualDecisionError, "must remain outside the project"):
            visual._outside_project(project, ((target, "Affinity keys and replay ledger must remain outside the project"),))

    def test_both_answers_to_the_stored_spelling_are_load_bearing_end_to_end(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        stored, listed = visual._stored_spelling, visual._listed_verbatim
        self.addCleanup(setattr, visual, "_stored_spelling", stored)
        self.addCleanup(setattr, visual, "_listed_verbatim", listed)
        project = (base / "asciiproject"); project.mkdir(); project = project.resolve(strict=True)
        upper = project.parent / project.name.upper()
        if same_file(upper, project):
            # Neither branch was ever pinned end to end, because whichever one
            # was switched off the other carried the answer.  Each is therefore
            # measured with the other one disabled.
            visual._stored_spelling = lambda path: None
            self.assertEqual(project, visual.exact_project_root(project))
            with self.assertRaisesRegex(visual.VisualDecisionError, "exact canonical directory"):
                visual.exact_project_root(upper)
            with self.assertRaises(visual.VisualDecisionError):
                visual.visual_workspace(upper, "run-b", create=True)
            visual._stored_spelling = stored
            visual._listed_verbatim = lambda path: True
            self.assertEqual(project, visual.exact_project_root(project))
            if stored(project) is not None:
                with self.assertRaisesRegex(visual.VisualDecisionError, "exact canonical directory"):
                    visual.exact_project_root(upper)
            visual._listed_verbatim = listed
        # The fallback's own error branch: a component that cannot be listed is
        # not "spelled verbatim", it is unknown - and unknown has to be False,
        # or an unreadable ancestor turns the portable answer into a yes.
        unreadable = base / "noread"; unreadable.mkdir()
        inner = unreadable / "proj"; inner.mkdir()
        os.chmod(unreadable, 0o111); self.addCleanup(os.chmod, unreadable, 0o700)
        inner = Path(os.path.abspath(inner))
        self.assertFalse(visual._listed_verbatim(inner))
        visual._stored_spelling = lambda path: None
        with self.assertRaisesRegex(visual.VisualDecisionError, "exact canonical directory"):
            visual.exact_project_root(inner)
        visual._stored_spelling = stored
        # With the kernel answer available the very same root is accepted, so
        # the refusal above is the fallback's conservatism and not a rule.
        if stored(inner) is not None:
            self.assertEqual(inner, visual.exact_project_root(inner))
        # A project root that is not a directory at all is refused, whichever
        # branch answers.
        regular = base / "regular.txt"; regular.write_text("x")
        with self.assertRaisesRegex(visual.VisualDecisionError, "exact canonical directory"):
            visual.exact_project_root(regular)

    def test_the_key_and_the_ledger_stay_outside_the_project_by_identity_under_a_hard_limit(self):
        # r14 killed the mutant that drops the identity test from
        # _outside_project only by *hanging* for 902.9 s.  A hang is not a
        # measurement: the property is asserted here, and the assertion runs
        # under a wall-clock limit so that losing it is red rather than stuck.
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve()
        inside = project / ".brandkit" / "secrets"; inside.mkdir(parents=True)
        outside = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, outside, True)
        (outside / "alias").symlink_to(inside, target_is_directory=True)
        alias = outside / "alias" / "replay-ledger.json"
        message = "Affinity keys and replay ledger must remain outside the project"
        with deadline(30, "the outside-the-project identity test"):
            for candidate in (alias, outside / "alias", inside / "replay-ledger.json",
                              project / ".brandkit" / "x" / ".." / "secrets" / "replay-ledger.json"):
                with self.assertRaisesRegex(visual.VisualDecisionError, "must remain outside the project"):
                    visual._outside_project(project, ((candidate, message),))
            visual._outside_project(project, ((outside / "replay-ledger.json", message),))
        root = visual.visual_workspace(project, "run-a", create=True)
        fixture = affinity_bundle(project, root, inside)
        with deadline(90, "validate_bundle with the ledger inside the project"):
            for field in ("replay_ledger_path", "owner_key_path"):
                with self.assertRaisesRegex(visual.VisualDecisionError, "must remain outside the project"):
                    visual.validate_bundle(project, "run-a", NOW, sidecar_root=visual.ROOT.parents[1],
                                           affinity_auth={**fixture["auth"], field: str(alias)})
        self.assertEqual([], [item.name for item in project.rglob("*") if item.name.startswith(".affinity-lock.")])
        self.assertEqual(fixture["genesis_raw"], fixture["ledger_path"].read_bytes())


class VisualDecisionCoreR17Tests(TimeLimitedTestCase):
    """The HTML half of the offline apparatus, measured as a rule for the first time.

    Until r16 no test ever handed ``_SheetParser`` anything but the one canonical
    ``<head>``: ``grep -c CDATA`` over this file was 0, and all four fail-open
    mutants r16 found sat in exactly that unobserved surface.  The rule the tests
    below state is not "these payloads are refused" but the one the module now
    keeps: *a sheet is a generated artifact, so it has exactly one document form,
    and everything outside that form fails closed* - because every divergence
    between ``html.parser`` and Blink is a hole, and that list is open-ended.

    Each rule is probed from several sides and, where two rules overlap, each is
    made observable on its own, so that weakening one of them is red here even
    while the other still catches the payload.
    """

    SECTION_IDS = ("candidates-color", "candidates-typography", "candidates-code-font",
                   "candidates-logo", "candidates-lockup", "recommendations", "owner-review")
    SECTIONS = "".join(f'<section id="{name}"><h2>x</h2></section>' for name in SECTION_IDS)
    LINK = '<link rel="stylesheet" href="style.css">'
    LOCAL_CSS = '@font-face{src:url("assets/a.svg")}'
    REMOTE = "http://127.0.0.1:1/x"

    def meta(self, policy: str | None = None) -> str:
        return f'<meta http-equiv="Content-Security-Policy" content="{visual.SHEET_CSP if policy is None else policy}">'

    def page(self, *, head: str | None = None, body: str | None = None, doctype: str = "<!doctype html>") -> str:
        head = self.meta() + self.LINK if head is None else head
        body = "<main>" + self.SECTIONS + "</main>" if body is None else body
        return (doctype + '<html lang="de"><head><meta charset="utf-8">' + head
                + '<title>x</title></head><body>' + body + '</body></html>')

    def main(self, payload: str = "") -> str:
        return "<main>" + self.SECTIONS + payload + "</main>"

    def sheet(self):
        """A callable that validates one sheet built from the given page and CSS."""
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        sheet = Path(temp.name).resolve() / "sheet"; (sheet / "assets").mkdir(parents=True)
        (sheet / "assets/a.svg").write_text("svg")

        def check(page: str, *, css: str | None = None, extra: dict[str, str] | None = None) -> None:
            (sheet / "index.html").write_bytes(page.encode("utf-8"))
            (sheet / "style.css").write_bytes((self.LOCAL_CSS if css is None else css).encode("utf-8"))
            members = [sheet / "index.html", sheet / "style.css", sheet / "assets/a.svg"]
            for name, data in (extra or {}).items():
                target = sheet / name; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(data); members.append(target)
            files = [{"path": item.relative_to(sheet).as_posix(), "sha256": visual.sha256_bytes(item.read_bytes())} for item in members]
            manifest = {"schema_version": "brandkit-visual-sheet-manifest.v1", "brief_sha256": "a" * 64,
                        "candidate_set_sha256": "b" * 64, "affinity_observation_sha256": "c" * 64, "files": files,
                        "viewports": [320, 768, 1440], "offline": True, "interactive_controls": False,
                        "preselection": False, "normative": False, "human_review_status": "required"}
            with deadline(60, "validate_sheet_manifest"):
                visual.validate_sheet_manifest(manifest, sheet)
        return check

    # --- the document form, against the whole parser-divergence class ---------

    def test_no_markup_declaration_comment_or_instruction_can_smuggle_a_tag(self):
        check = self.sheet()
        check(self.page())
        # html.parser implements XML CDATA sections and hands the whole span to
        # unknown_decl; HTML5 has no such thing in HTML content - "<![CDATA[" is
        # a bogus comment that ends at the FIRST ">", so Blink parses everything
        # after it as real markup.  The same shape of divergence exists for a
        # comment, a doctype-like declaration and a processing instruction, so
        # the *class* is refused rather than the four members of it.
        payloads = (f'<meta http-equiv="refresh" content="0;url={self.REMOTE}">',
                    f'<link rel="stylesheet" href="{self.REMOTE}.css">',
                    f'<script src="{self.REMOTE}.js"></script>',
                    f'<img src="{self.REMOTE}.png" alt="">',
                    '<img src="assets/ghost.svg" alt="">')
        wrappers = ('<![CDATA[ > %s ]]>', '<![CDATA[%s]]>', '<!-- > %s -->', '<!ENTITY a "%s">',
                    '<?xml %s ?>', '<! > %s >', '<![if IE]> %s <![endif]>')
        for wrapper in wrappers:
            for payload in payloads:
                with self.assertRaisesRegex(visual.VisualDecisionError, "markup that is not a plain tag"):
                    check(self.page(body=self.main(wrapper % payload)))
                with self.assertRaisesRegex(visual.VisualDecisionError, "markup that is not a plain tag"):
                    check(self.page(head=self.meta() + self.LINK + (wrapper % payload)))
        # Unhidden, every one of those payloads is refused on its own merits, so
        # the wrapper is the only thing the rule above adds.
        for payload, reason in ((payloads[0], "forbidden meta pragma"),
                                (payloads[1], "remote or unsafe URL"),
                                (payloads[2], "forbidden HTML tag"),
                                (payloads[3], "remote or unsafe URL"),
                                (payloads[4], "asset references differ")):
            with self.assertRaisesRegex(visual.VisualDecisionError, reason):
                check(self.page(body=self.main(payload)))

    def test_a_solidus_and_a_duplicate_attribute_are_not_read_the_browsers_way_but_refused(self):
        check = self.sheet()
        # A self-closing spelling used to be handed to a handler that no longer
        # delegated, and the whole element with it: <img src="http://..."/> was
        # ACCEPTED.  The reference of a void element is collected whichever way
        # it is spelled.
        for spelling in (f'<img src="{self.REMOTE}.png" alt=""/>', f'<img src="{self.REMOTE}.png" alt="" />',
                         f'<img alt="" src="{self.REMOTE}.png"/>'):
            with self.assertRaisesRegex(visual.VisualDecisionError, "remote or unsafe URL"):
                check(self.page(body=self.main(spelling)))
        with self.assertRaisesRegex(visual.VisualDecisionError, "asset references differ"):
            check(self.page(body=self.main('<img src="assets/ghost.svg" alt=""/>')))
        # On anything that is not a void element HTML5 discards the solidus, so
        # <div/> opens a div and the "closed" subtree is a fiction.
        for tag in ("div", "section", "main", "p", "span"):
            with self.assertRaisesRegex(visual.VisualDecisionError, "solidus on a non-void tag"):
                check(self.page(body=self.main(f"<{tag}/>")))
        # HTML5 keeps the FIRST duplicate attribute, dict(attrs) the last, so
        # the validator read one URL and the browser fetched the other.  Neither
        # reading is adopted; both orders are refused.
        for duplicate in (f'<img src="{self.REMOTE}.png" src="assets/a.svg" alt="">',
                          f'<img src="assets/a.svg" src="{self.REMOTE}.png" alt="">',
                          '<img src="assets/a.svg" alt="a" alt="b">',
                          '<div class="a" class="b">x</div>'):
            with self.assertRaisesRegex(visual.VisualDecisionError, "duplicate HTML attribute"):
                check(self.page(body=self.main(duplicate)))
        with self.assertRaisesRegex(visual.VisualDecisionError, "duplicate HTML attribute"):
            check(self.page(head=f'<meta http-equiv="Content-Security-Policy" http-equiv="refresh" content="{visual.SHEET_CSP}">' + self.LINK))

    def test_the_document_outside_the_one_generated_form_is_refused_whatever_it_says(self):
        check = self.sheet()
        cases = (
            (self.page(doctype=""), "does not open with the HTML5 doctype"),
            (self.page(doctype="<!doctype html5>"), "does not open with the HTML5 doctype"),
            (self.page(doctype=" <!doctype html>"), "does not open with the HTML5 doctype"),
            (self.page(body=self.main().replace("<main>", "<MAIN>", 1)), "markup that is not a plain tag"),
            (self.page(body=self.main('<img src=assets/a.svg alt="">')), "markup that is not a plain tag"),
            (self.page(body=self.main("<img src='assets/a.svg' alt=''>")), "markup that is not a plain tag"),
            (self.page(body=self.main('<img src="assets/a&#x2e;svg" alt="">')), "markup that is not a plain tag"),
            (self.page(body=self.main('<img alt="a>b" src="assets/a.svg">')), "markup that is not a plain tag"),
            (self.page(body=self.main("<img")), "markup that is not a plain tag"),
            (self.page(body=self.main("</img>")), "void element carries an end tag"),
            (self.page(body=self.main('</main class="x">')), "an end tag carries markup"),
            (self.page(body=self.main().replace("</main>", "</div>", 1)), "unbalanced end tag"),
            (self.page(body=self.main().replace("</main>", "", 1)), "unbalanced end tag"),
            (self.page(body=self.main() + '<div class="a">'), "unbalanced end tag"),
            (self.page().replace("</html>", "", 1), "unclosed element"),
            (self.page(head=self.meta() + self.LINK + "<p>x</p>"), "element not allowed in <head>"),
            (self.page(head=self.meta() + self.LINK + "<title><img src=\"assets/a.svg\" alt=\"\"></title>"), "carries markup"),
            (self.page(body=self.main() + "</body></html><html lang=\"de\"><head></head><body></body>"), "single <html>"),
            (self.page(body='<meta name="viewport" content="w">' + self.main()), "element not allowed in <body>"),
            # A second encoding declaration is refused wherever it stands, and
            # it is refused as an encoding statement rather than as a placement,
            # so the two rules stay observable one beside the other.
            (self.page(body='<meta charset="utf-8">' + self.main()), "more than one character encoding"),
            (self.page(body=self.LINK + self.main()), "element not allowed in <body>"),
            (self.page().replace("</head><body>", "</head>text<body>", 1), "text outside <body>"),
            (self.page() + "trailing", "text outside <body>"),
            (self.page(body=self.main("\x00")), "forbidden control character"),
            (self.page(body=self.main("<p>x\x07y</p>")), "forbidden control character"),
        )
        for page, reason in cases:
            with self.assertRaisesRegex(visual.VisualDecisionError, reason):
                check(page)
        # Whitespace between the elements of that form is still a document.
        check(self.page().replace("</head><body>", "</head>\n  <body>", 1) + "\n")

    def test_html_parser_is_a_second_reader_that_swallows_nothing(self):
        # Every callback through which html.parser used to absorb markup without
        # a word now fails closed, and the leading doctype is the one markup
        # declaration a sheet may carry - and only while it is still leading.
        for text, reason in (('<![CDATA[ > <img src="x"> ]]>', "markup declaration"),
                             ('<!ENTITY a "b">', "markup declaration|HTML comment"),
                             ('<!doctype html>', "markup declaration"),
                             ('<!-- c -->', "HTML comment"),
                             ('<?php echo 1; ?>', "processing instruction")):
            view = visual._SheetHTMLView()
            with self.assertRaisesRegex(visual.VisualDecisionError, reason):
                view.feed('<!doctype html><html lang="de">' + text); view.close()
        view = visual._SheetHTMLView(); view.feed("<!doctype html><html></html>"); view.close()
        self.assertEqual([("start", "html", ()), ("end", "html", ())], view.events)
        # And on a real sheet the two independent readers produce the identical
        # element stream - which is what makes their disagreement usable as a
        # rejection at all.
        page = self.page()
        view = visual._SheetHTMLView(); view.feed(page); view.close()
        self.assertEqual(view.events, visual._SheetParser()._scan(page))
        self.assertIn(("start", "meta", (("http-equiv", "Content-Security-Policy"), ("content", visual.SHEET_CSP))), view.events)

    # --- where the policy counts, and where it does not ----------------------

    def test_a_content_policy_counts_only_from_inside_the_head(self):
        check = self.sheet()
        # f2 and f3 of the r16 browser run are byte-identical but for the
        # position of this one element: in <head> Blink blocked both remote
        # loads, in <body> it ignored the policy entirely and the page executed
        # foreign script with not one violation report.  The validator said
        # ACCEPTED to both.
        check(self.page(head=self.meta() + self.LINK))
        check(self.page(head=self.LINK + self.meta()))
        for body in (self.meta() + self.main(), self.main() + self.meta(), self.main(self.meta())):
            with self.assertRaisesRegex(visual.VisualDecisionError, "content policy outside <head>"):
                check(self.page(head=self.LINK, body=body))
        # A second policy composes as an intersection in Blink, so it cannot
        # loosen the first - but the artifact is then not the one closed policy
        # this module promises, and last-writer-wins was how it was read.
        for head in (self.meta("default-src *") + self.meta() + self.LINK,
                     self.meta() + self.LINK + self.meta("default-src *"),
                     self.meta() + self.meta() + self.LINK):
            with self.assertRaisesRegex(visual.VisualDecisionError, "more than one content policy"):
                check(self.page(head=head))
        with self.assertRaisesRegex(visual.VisualDecisionError, "CSP differs from the closed offline policy"):
            check(self.page(head=self.LINK))
        # A meta without a pragma is refused in the body by the content model,
        # which is the second, independent statement of the same placement rule.
        with self.assertRaisesRegex(visual.VisualDecisionError, "element not allowed in <body>"):
            check(self.page(head=self.meta() + self.LINK, body='<meta name="viewport" content="x">' + self.main()))
        # frame-ancestors is a dead directive in a meta-delivered policy - Blink
        # logs "is ignored when delivered via a <meta> element" for every sheet -
        # so the policy does not claim it.
        self.assertNotIn("frame-ancestors", visual.SHEET_CSP)
        for directive in ("default-src 'none'", "script-src 'none'", "style-src 'self'",
                          "img-src 'self'", "font-src 'self'", "base-uri 'none'", "form-action 'none'"):
            self.assertIn(directive, visual.SHEET_CSP)

    # --- the closed member grammar -------------------------------------------

    def test_the_sheet_member_grammar_admits_only_css_svg_and_woff2(self):
        for path in ("assets/x.png", "assets/x.jpg", "assets/x.gif", "assets/x.webp",
                     "assets/x.ico", "assets/x.woff", "assets/x.ttf", "assets/x.html",
                     "assets/x.svgz", "assets/x.css.png", "assets/x.js"):
            with self.assertRaisesRegex(visual.VisualDecisionError, "not an allowed sheet file"):
                visual._sheet_member_kind(path)
        self.assertEqual("css", visual._sheet_member_kind("assets/x.png.css"))
        # The per-tag attribute allowlist is what actually keeps an event
        # handler or an inline style out; the explicit on*/style guard beside it
        # is a second statement of the same thing.  That the two cannot drift
        # apart is the property, so it is asserted rather than assumed.
        for tag, names in visual._SheetParser.allowed_attrs.items():
            self.assertEqual(set(), {name for name in names if name.startswith("on") or name == "style"}, tag)
        self.assertEqual("asset", visual._sheet_member_kind("assets/x.woff2"))
        check = self.sheet()
        for path in ("assets/x.png", "assets/x.woff", "assets/x.ttf"):
            with self.assertRaisesRegex(visual.VisualDecisionError, "not an allowed sheet file"):
                check(self.page(), extra={path: "x"})
        # The restriction is deliberate and it is a restriction: the renderer
        # writes only SVG and WOFF2 today, so a raster candidate would need this
        # grammar widened - and scanned - rather than the manifest loosened.
        self.assertEqual({"css", "svg", "woff2"}, set(visual.SHEET_ASSET_MEMBER.pattern.rsplit("(", 1)[1].rstrip(")").split("|")))

    # --- what the report binding actually binds ------------------------------

    def test_the_candidate_context_hash_covers_the_whole_candidate_set(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve(); root = visual.visual_workspace(project, "run-a", create=True)
        document = color_bundle(project, root)
        base = visual.candidate_context_sha256(document)
        # A hash taken over the candidate list alone would not tell any of these
        # apart, and the report that carries it claims to bind the context.
        for field, value in (("brief_sha256", "d" * 64),
                             ("source_review", {"path": "other.json", "sha256": "e" * 64}),
                             ("recommendations", [{"kind": "color", "candidate_id": "color-a",
                                                   "rationale": "x", "source_ids": ["impeccable"]}]),
                             ("status", "complete"),
                             ("schema_version", "brandkit-visual-candidate-set.v2")):
            variant = copy.deepcopy(document); variant[field] = value
            self.assertNotEqual(base, visual.candidate_context_sha256(variant), field)
        # The one thing it must not see is the digest of the report that binds
        # back to it, because that would be a circle.
        blinded = copy.deepcopy(document)
        blinded["candidates"][0]["value"]["report"]["sha256"] = "f" * 64
        self.assertEqual(base, visual.candidate_context_sha256(blinded))
        # ... but the *path* of that report is content like any other.
        moved = copy.deepcopy(document)
        moved["candidates"][0]["value"]["report"]["path"] = "elsewhere.json"
        self.assertNotEqual(base, visual.candidate_context_sha256(moved))

    # --- which file answers the delegation -----------------------------------

    def test_the_source_validator_is_bound_to_a_file_and_a_digest_not_to_a_module_name(self):
        path = visual.ROOT / visual.SOURCE_VALIDATOR_PATH
        self.assertEqual("scripts/validate_visual_decision_sources.py", visual.SOURCE_VALIDATOR_PATH)
        # The pin and the file may not drift apart silently.
        self.assertEqual(visual.SOURCE_VALIDATOR_SHA256, visual.sha256_bytes(path.read_bytes()))
        # A stub of the same module name, first on sys.path, used to answer the
        # delegation: the independent second opinion became a no-op and nothing
        # in the suite noticed.
        shadow = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, shadow, True)
        (shadow / "validate_visual_decision_sources.py").write_text(
            "def validate_visual_decision_sources(*args, **kwargs):\n    return []\n")
        sys.path.insert(0, str(shadow)); self.addCleanup(sys.path.remove, str(shadow))
        sys.modules.pop("validate_visual_decision_sources", None)
        self.addCleanup(sys.modules.pop, "validate_visual_decision_sources", None)
        self.assertEqual(str(path), visual._load_source_validator().__file__)
        project = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, project, True)
        color_bundle(project, visual.visual_workspace(project, "run-a", create=True))
        with tempfile.TemporaryDirectory() as detached:
            with self.assertRaisesRegex(visual.VisualDecisionError, "independent visual source contract failed"):
                visual.validate_bundle(project, "run-a", NOW, sidecar_root=Path(detached).resolve())
        self.assertEqual(["color-a"], [item["id"] for item in visual.validate_bundle(
            project, "run-a", NOW, sidecar_root=visual.ROOT.parents[1])["eligible_candidates"]])
        # An unannounced change to the delegate fails closed here rather than
        # running whatever the file now contains.
        pinned = visual.SOURCE_VALIDATOR_SHA256
        self.addCleanup(setattr, visual, "SOURCE_VALIDATOR_SHA256", pinned)
        visual.SOURCE_VALIDATOR_SHA256 = "0" * 64
        with self.assertRaisesRegex(visual.VisualDecisionError, "source validator hash differs"):
            visual._load_source_validator()
        with self.assertRaisesRegex(visual.VisualDecisionError, "source validator hash differs"):
            visual.validate_bundle(project, "run-a", NOW, sidecar_root=visual.ROOT.parents[1])

    # --- three purposes, three derived domains -------------------------------

    def test_the_owner_key_signs_three_purposes_in_three_derived_domains(self):
        domains = (visual.LEDGER_DOMAIN, visual.OPERATION_DOMAIN, visual.OWNER_APPROVAL_DOMAIN)
        self.assertEqual(3, len(set(domains)))
        key = b"z" * 32
        derived = [domain_key(key, domain) for domain in domains]
        self.assertEqual(3, len(set(derived)))
        self.assertNotIn(key, derived)
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve(); root = visual.visual_workspace(project, "run-a", create=True)
        secrets = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, secrets, True)
        fixture = affinity_bundle(project, root, secrets)

        counter = itertools.count()

        def approval(signing_key: bytes, **overrides) -> dict:
            body = {"schema_version": visual.OWNER_APPROVAL_TYPE,
                    "key_id": visual.sha256_bytes(signing_key)[:16],
                    "payload": {"decision": "graphics-mutate", "project_identity_sha256": fixture["identity"],
                                "run_id": "run-a", "document_id": "logo-doc"}}
            body.update(overrides)
            body["hmac_sha256"] = hmac.new(signing_key, visual.canonical_json(body), hashlib.sha256).hexdigest()
            return canonical_write(root, f"owner-variant-{next(counter)}.json", body)

        def descriptor(binding: dict) -> dict:
            return {**fixture["auth"], "receipts": {name: write_receipt(root, {**document, "owner_approval": binding}, fixture["operation_key"])
                                                    for name, document in fixture["documents"].items()}}

        def restore() -> None:
            fixture["ledger_path"].write_bytes(fixture["genesis_raw"]); fixture["ledger_path"].chmod(0o600)

        owner_key = domain_key(fixture["key"], visual.OWNER_APPROVAL_DOMAIN)
        cases = ((approval(fixture["key"]), "owner approval authentication failed"),
                 (approval(domain_key(fixture["key"], visual.LEDGER_DOMAIN)), "owner approval authentication failed"),
                 (approval(domain_key(fixture["key"], visual.OPERATION_DOMAIN)), "owner approval authentication failed"),
                 (approval(owner_key, schema_version="brandkit-affinity-mutation-receipt.v1"), "type or shape differs"),
                 (approval(owner_key, issuer="fixture"), "type or shape differs"))
        for binding, reason in cases:
            restore()
            with self.assertRaisesRegex(visual.VisualDecisionError, reason):
                visual.validate_bundle(project, "run-a", NOW, sidecar_root=visual.ROOT.parents[1], affinity_auth=descriptor(binding))
            self.assertEqual(fixture["genesis_raw"], fixture["ledger_path"].read_bytes())
        # The scope is a closed set as well, so a field nobody checks cannot ride along.
        wide = {"decision": "graphics-mutate", "project_identity_sha256": fixture["identity"],
                "run_id": "run-a", "document_id": "logo-doc", "capability": "anything"}
        restore()
        with self.assertRaisesRegex(visual.VisualDecisionError, "owner approval scope differs"):
            visual.validate_bundle(project, "run-a", NOW, sidecar_root=visual.ROOT.parents[1],
                                   affinity_auth=descriptor(approval(owner_key, payload=wide)))
        restore()
        self.assertEqual(["lockup-choice", "logo-choice"], sorted(item["id"] for item in visual.validate_bundle(
            project, "run-a", NOW, sidecar_root=visual.ROOT.parents[1],
            affinity_auth=descriptor(approval(owner_key)))["eligible_candidates"]))

    # --- the sealed run, and the rename that was called unclosable ------------

    def test_a_run_directory_that_names_another_run_is_never_used_as_a_workspace(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve(); work = project / ".brandkit" / "work"
        (work / visual.PROTECTED_RUN_ID / "discovery").mkdir(parents=True)
        (work / visual.PROTECTED_RUN_ID / "discovery" / "handoff.json").write_bytes(visual.canonical_json(
            {"schema_version": "brandkit-discovery-handoff.v1", "run_id": visual.PROTECTED_RUN_ID,
             "workspace": f".brandkit/work/{visual.PROTECTED_RUN_ID}/discovery"}))
        (work / visual.PROTECTED_RUN_ID / "brand-raw.md").write_text("raw\n")
        before = visual.protected_run_inventory(project)
        # The seal is a name-based contract, and the name is what a rename takes
        # away.  The tree says which run it is; that statement is now read, and
        # it is read BEFORE the create walk, which creates as it descends.
        os.rename(work / visual.PROTECTED_RUN_ID, work / "myrun")
        with self.assertRaisesRegex(visual.VisualDecisionError, "discovery handoff for a different run"):
            visual.visual_workspace(project, "myrun", create=True)
        self.assertFalse((work / "myrun" / "visual-decisions").exists())
        os.rename(work / "myrun", work / visual.PROTECTED_RUN_ID)
        self.assertEqual(before["tree_sha256"], visual.protected_run_inventory(project)["tree_sha256"])
        # No false rejection: a run that names itself, and a run with no handoff
        # at all, both get their workspace.
        (work / "run-b" / "discovery").mkdir(parents=True)
        (work / "run-b" / "discovery" / "handoff.json").write_bytes(visual.canonical_json(
            {"schema_version": "brandkit-discovery-handoff.v1", "run_id": "run-b"}))
        self.assertTrue(visual.visual_workspace(project, "run-b", create=True).is_dir())
        self.assertTrue(visual.visual_workspace(project, "run-c", create=True).is_dir())
        # A handoff that cannot be read as a statement is not a permission.
        (work / "run-d" / "discovery").mkdir(parents=True)
        (work / "run-d" / "discovery" / "handoff.json").write_text("not json at all")
        with self.assertRaises(visual.VisualDecisionError):
            visual.visual_workspace(project, "run-d", create=True)
        self.assertFalse((work / "run-d" / "visual-decisions").exists())
        (work / "run-e" / "discovery").mkdir(parents=True)
        (work / "run-e" / "discovery" / "handoff.json").write_bytes(visual.canonical_json(["run-e"]))
        with self.assertRaisesRegex(visual.VisualDecisionError, "discovery handoff for a different run"):
            visual.visual_workspace(project, "run-e", create=True)

    # --- the renderer's own output, through its own validator -----------------

    def test_the_sheet_this_module_renders_passes_its_own_document_form(self):
        # Nothing ever fed the renderer's HTML to the validator, so a document
        # form this narrow could have refused the one document it exists for.
        # Since the render-sheet fix _render_sheet reads every report through the
        # candidate that binds it, so the bundle is built on disk rather than
        # handed over as loose report objects.
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve(); root = visual.visual_workspace(project, "run-a", create=True)
        # _render_sheet validates the logo report without a clock of its own, so
        # the observation it carries has to be live rather than fixed at NOW.
        live = int(time.time())
        candidate_set = five_domain_bundle(project, root, live)
        brief = json.loads((root / "visual-brief.json").read_bytes())
        observation = json.loads((root / "affinity-observation.json").read_bytes())
        logo_candidates = json.loads((root / "logo-report.json").read_bytes())["candidates"]
        font_candidates = json.loads((root / "font-report.json").read_bytes())["candidates"]
        with deadline(120, "_render_sheet"):
            manifest = visual._render_sheet(root / "sheet", root, brief, candidate_set, observation)
        # _render_sheet ends in validate_sheet_manifest, so reaching this line is
        # the round trip; the parser is asked directly as well, so that a change
        # to the form shows up here as a document-form failure by name.
        page = (root / "sheet" / "index.html").read_text(encoding="utf-8")
        parser = visual._SheetParser(); parser.feed(page); parser.close()
        self.assertEqual(visual.SHEET_CSP, parser.csp)
        self.assertEqual(set(self.SECTION_IDS), parser.sections)
        self.assertEqual({"style.css"}, parser.stylesheets)
        members = {item["path"] for item in manifest["files"]}
        # Assets are named by content.  The typography card shows serif and
        # sans, the code-font card the mono: all three fonts of the report.
        self.assertEqual({"index.html", "style.css"} | {f"assets/{item['asset']['sha256']}.svg" for item in logo_candidates}
                         | {f"assets/{item['asset']['sha256']}.woff2" for item in font_candidates}, members)
        # The sheet directory holds exactly its members plus the manifest that
        # describes them - which is itself not a member, and cannot be, since it
        # would have to carry its own digest.  Stating the whole directory is
        # what keeps "closed manifest" a statement about the directory.
        on_disk = {item.relative_to(root / "sheet").as_posix() for item in (root / "sheet").rglob("*") if item.is_file()}
        self.assertEqual(members | {"manifest.json"}, on_disk)

    # --- the error boundary the r16 review tripped over ----------------------

    def test_an_unavailable_read_root_is_a_contract_error_and_not_a_raw_oserror(self):
        # root.resolve(strict=True) stood OUTSIDE the try that turns an OSError
        # into a VisualDecisionError, so a root that does not exist left
        # secure_read as a bare FileNotFoundError while every other failure came
        # out as a contract error.  No caller reached it before; the run-identity
        # guard in visual_workspace is the first that may, and a guard that has
        # to catch two unrelated exception families to stay closed is one
        # refactor away from catching neither.
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        (root / "file").write_text("x")
        for absent in (root / "absent", root / "file" / "deeper", root / "a" / "b" / "c"):
            with self.assertRaisesRegex(visual.VisualDecisionError, "root is unavailable"):
                visual.secure_read(absent, "x.json", "probe")
        # An existing root still answers with the ordinary confinement errors.
        with self.assertRaisesRegex(visual.VisualDecisionError, "not a normalized relative path"):
            visual.secure_read(root, "../x.json", "probe")

    # --- the one over-approximation that is left, named as one ----------------

    def test_a_url_that_only_appears_once_a_string_is_decoded_is_refused_on_purpose(self):
        check = self.sheet()
        # The reference scan runs twice: once on the raw, comment-free text -
        # the browser-faithful reading that decides token boundaries - and once
        # on the fully decoded text, purely additively.  The second run can only
        # ever add references, so no payload that was refused can become
        # acceptable; the price is this false rejection, and it is a real one.
        # r16 verified in Blink that the sheet below fetches assets/a.svg and
        # NOT assets/ghost.svg: getComputedStyle(a).content is the single string
        # 'x"url(assets/ghost.svg)"y' and the origin's access log for the whole
        # session carries no ghost.svg line for it.
        with self.assertRaisesRegex(visual.VisualDecisionError, "asset references differ"):
            check(self.page(), css=r'a{content:"x\22 url(assets/ghost.svg)\22 y"}b{background:url(assets/a.svg)}')
        # The control that shows the stylesheet is alive and the rejection above
        # is about the escape rather than about the file.
        check(self.page(), css='a{content:"x"}b{background:url(assets/a.svg)}')
        # And a reference whose name is written with escapes is a reference in
        # every reading, so the price above buys nothing that was already free.
        with self.assertRaisesRegex(visual.VisualDecisionError, "asset references differ"):
            check(self.page(), css=r'b{background:url(assets/a.svg)}c{background:url("assets/gh\6fst.svg")}')



# --- r19 -----------------------------------------------------------------------

# Building a real WOFF2 costs more than everything else in a render put
# together, and the r19 matrix renders the sheet five times.  The fixture fonts
# are therefore built once per process and copied per case; nothing about the
# property under test depends on rebuilding them.
_FONT_CACHE: dict[tuple[str, str], tuple[bytes, bytes]] = {}
_METADATA_CACHE: dict[str, dict] = {}


def cached_font(path: Path, family: str, classification: str, ligatures: str | None = None) -> tuple[bytes, bytes]:
    key = (family, classification, ligatures)
    if key not in _FONT_CACHE:
        scratch = Path(tempfile.mkdtemp())
        try:
            _FONT_CACHE[key] = make_font(scratch / "f.woff2", family, classification, ligatures)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    asset, source = _FONT_CACHE[key]
    path.write_bytes(asset); path.with_suffix(".ttf").write_bytes(source)
    return asset, source


_WEIGHTED_FONT_CACHE: dict[tuple[str, str, tuple[int, int]], tuple[bytes, bytes]] = {}
_XHEIGHT_FONT_CACHE: dict[tuple, tuple[bytes, bytes]] = {}


def css_rules(css: str) -> list[tuple[str, str]]:
    """Top-level rules as (selector list, declaration block), at-rules skipped by depth.

    A selector has to be matched EXACTLY somewhere: ``.pg-code`` is a substring of
    ``.pg-face-0 .pg-code``, so a test that greps for the base rule silently read a
    per-face rule instead the moment the faces started carrying sizes.
    """
    out: list[tuple[str, str]] = []; index = 0
    while index < len(css):
        brace = css.find("{", index)
        if brace < 0: break
        selector = css[index:brace].strip()
        if selector.startswith("@"):
            depth, cursor = 1, brace + 1
            while cursor < len(css) and depth:
                depth += (css[cursor] == "{") - (css[cursor] == "}"); cursor += 1
            index = cursor; continue
        end = css.find("}", brace)
        out.append((selector, css[brace + 1:end])); index = end + 1
    return out


def _set_vertical(font, vertical: tuple[int, int] | None) -> None:
    """Give one fixture a vertical metric of its OWN.

    ``make_font`` writes 800/-200 into every fixture, so a rendered sheet had
    ONE ascent across all its faces - and "every candidate starts on the same
    first baseline" cannot be measured on that: compensating each face against
    the first face's ascent gives a byte-identical stylesheet.  Same blind spot
    ``weighted_font`` removed for the range and ``specimen_font`` for the
    outline, removed here for the line box.  Both tables are written because the
    engine reads one of them and the file has to be consistent either way.
    """
    if vertical is None:
        return
    ascent, descent = vertical
    font["hhea"].ascent, font["hhea"].descent = ascent, descent
    font["OS/2"].sTypoAscender, font["OS/2"].sTypoDescender = ascent, descent
    font["OS/2"].usWinAscent, font["OS/2"].usWinDescent = max(ascent, 0), max(-descent, 0)


def divergent_vertical_font(path: Path, family: str, classification: str, *, hhea: tuple[int, int],
                            typo: tuple[int, int] | None = None, win: tuple[int, int] | None = None,
                            line_gap: int = 0, use_typo_metrics: bool = False,
                            x_top: int = 500) -> bytes:
    """A fixture whose ``hhea``, ``OS/2`` typo and ``usWin`` values DISAGREE on purpose.

    ``_set_vertical`` writes hhea and typo to the SAME numbers, which is what most
    fixtures want and exactly what makes the one question of this round
    untestable: with both tables equal, a rule reading typo is byte-identical to
    one reading hhea, so "the metric comes from hhea" cannot fail.  Here they
    differ, the line gap can be non-zero, and ``USE_TYPO_METRICS`` can be set at
    OS/2 version 4 - the version where that bit is defined at all.  The written
    BYTES are read back before the fixture is handed out, because an edit to the
    object in memory proves nothing about the file.
    """
    scratch = Path(tempfile.mkdtemp())
    try:
        asset, _source = specimen_font(scratch / "d.woff2", family, classification, x_top=x_top)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    font = TTFont(io.BytesIO(asset))
    ascent, descent = hhea
    font["hhea"].ascent, font["hhea"].descent, font["hhea"].lineGap = ascent, descent, line_gap
    os2 = font["OS/2"]; os2.version = 4
    os2.sTypoAscender, os2.sTypoDescender = typo if typo is not None else (ascent, descent)
    os2.sTypoLineGap = 0
    os2.usWinAscent, os2.usWinDescent = win if win is not None else (max(ascent, 0), max(-descent, 0))
    os2.fsSelection = (os2.fsSelection | 0x80) if use_typo_metrics else (os2.fsSelection & ~0x80)
    buffer = io.BytesIO(); font.flavor = "woff2"; font.save(buffer)
    written = buffer.getvalue(); path.write_bytes(written)
    back = TTFont(io.BytesIO(written)); b2, o2 = back["hhea"], back["OS/2"]
    assert (int(b2.ascent), int(b2.descent), int(b2.lineGap)) == (ascent, descent, line_gap)
    assert (int(o2.sTypoAscender), int(o2.sTypoDescender)) == (typo if typo is not None else (ascent, descent))
    assert int(o2.version) == 4 and bool(o2.fsSelection & 0x80) is use_typo_metrics
    return written


def x_height_font(path: Path, family: str, classification: str, x_height: int, *,
                  numerals: str = "", drop_x: bool = False, weights: tuple[int, int] | None = None,
                  vertical: tuple[int, int] | None = None) -> tuple[bytes, bytes]:
    """The fixture font with an x-height of its OWN, and optionally numeral features.

    ``make_font`` draws every fixture glyph the same height and leaves
    ``OS/2.sxHeight`` at 0, so a bundle of them carries ONE x-height ratio across
    all faces - and "the same effective x-height for every candidate" cannot be
    measured on that: normalising every face against the first face's ratio gives
    a byte-identical stylesheet.  This is the same blind spot ``weighted_font``
    was built to remove for the weight range, removed here for the size.

    ``x_height`` is written to ``OS/2.sxHeight``, the source the module prefers.
    ``drop_x`` instead zeroes the metric AND takes U+0078 out of the cmap, so the
    substitute path has nothing to measure either - the one shape the fail-closed
    guard exists for.  Such a font is deliberately unusable in a whole bundle (the
    specimen needs its ``x``), so it is only ever handed to ``_font_facts``
    directly.  ``numerals`` is a space-separated feature list, built the way the
    ligature fixtures are built, so a numeral row can only ever be shown for a
    font whose GSUB really applies that feature.
    """
    key = (family, classification, x_height, numerals, drop_x, weights, vertical)
    if key not in _XHEIGHT_FONT_CACHE:
        scratch = Path(tempfile.mkdtemp())
        try:
            if weights is None:
                asset, source = make_font(scratch / "f.woff2", family, classification)
            else:
                asset, source = weighted_font(scratch / "f.woff2", family, classification, weights)
            font = TTFont(io.BytesIO(asset))
            font["OS/2"].sxHeight = 0 if drop_x else x_height
            _set_vertical(font, vertical)
            if drop_x:
                for table in font["cmap"].tables:
                    table.cmap.pop(0x78, None)
            if numerals:
                addOpenTypeFeaturesFromString(font, "languagesystem DFLT dflt;\nlanguagesystem latn dflt;\n"
                                              + "".join(f"feature {tag} {{\n  sub u000030 by u000031;\n}} {tag};\n"
                                                        for tag in numerals.split()))
            buffer = io.BytesIO(); font.flavor = "woff2"; font.save(buffer)
            _XHEIGHT_FONT_CACHE[key] = (buffer.getvalue(), source)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    asset, source = _XHEIGHT_FONT_CACHE[key]
    path.write_bytes(asset); path.with_suffix(".ttf").write_bytes(source)
    return asset, source


_SPECIMEN_FONT_CACHE: dict[tuple, tuple[bytes, bytes]] = {}


def specimen_font(path: Path, family: str, classification: str, *, x_top: int, stated: int | None = None,
                  weights: tuple[int, int] | None = None, rise: int = 0,
                  vertical: tuple[int, int] | None = None) -> tuple[bytes, bytes]:
    """A fixture whose glyph ``x`` is really DRAWN ``x_top`` units tall.

    ``make_font`` draws every glyph to the same 700 units and ``x_height_font``
    then writes a DIFFERENT number into ``OS/2.sxHeight``.  That is the right
    fixture for "the metric is the source", and the wrong one for anything about
    the drawn letter: in it the metric and the outline disagree by construction,
    so "every candidate draws the same x-height" cannot be measured on it at all -
    the sizes are normalised on the metric while the ink stays 700 for everyone.

    Here the outline carries the height and ``stated`` defaults to the same
    number, so a bundle of these faces has ratios of its own AND a metric that
    tells the truth about its own drawing.  ``stated`` can be set apart from
    ``x_top`` on purpose, which is the only way to reach the cross-check that
    holds a statement against its own outline.  ``rise`` gives the ``x`` glyph a
    ``gvar`` delta, so its top really MOVES along the ``wght`` axis - the effect
    eight of twelve real variable faces have and no fixture had.
    """
    key = (family, classification, x_top, stated, weights, rise, vertical)
    if key not in _SPECIMEN_FONT_CACHE:
        scratch = Path(tempfile.mkdtemp())
        try:
            if weights is None:
                asset, _source = make_font(scratch / "f.woff2", family, classification)
            else:
                asset, _source = weighted_font(scratch / "f.woff2", family, classification, weights)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        font = TTFont(io.BytesIO(asset))
        name = font.getBestCmap()[ord("x")]
        pen = TTGlyphPen(None)
        pen.moveTo((80, 0)); pen.lineTo((520, 0)); pen.lineTo((520, x_top)); pen.lineTo((80, x_top)); pen.closePath()
        font["glyf"][name] = pen.glyph()
        font["glyf"][name].recalcBounds(font["glyf"])
        font["OS/2"].sxHeight = x_top if stated is None else stated
        _set_vertical(font, vertical)
        if rise and weights is not None:
            # One delta on the axis maximum: the two upper points move up, the
            # two lower ones and the four phantom points stay.  At the axis
            # maximum the scalar is 1, so the top really is ``x_top + rise``
            # there - measured, not assumed, by the tests that use this.
            variation = TupleVariation({"wght": (0.0, 1.0, 1.0)},
                                       [(0, 0), (0, 0), (0, rise), (0, rise), (0, 0), (0, 0), (0, 0), (0, 0)])
            table = newTable("gvar")
            table.version, table.reservedFlags = 1, 0
            table.variations = {glyph: [] for glyph in font.getGlyphOrder()}
            table.variations[name] = [variation]
            font["gvar"] = table
        source_buffer = io.BytesIO(); font.flavor = None; font.save(source_buffer)
        buffer = io.BytesIO(); font.flavor = "woff2"; font.save(buffer)
        _SPECIMEN_FONT_CACHE[key] = (buffer.getvalue(), source_buffer.getvalue())
    asset, source = _SPECIMEN_FONT_CACHE[key]
    path.write_bytes(asset); path.with_suffix(".ttf").write_bytes(source)
    return asset, source


def ink_x_height(asset: bytes, weight: int | None = None) -> int:
    """Top of the outline of ``x``, read WITHOUT the function under test.

    The whole point of this round is that the DRAWN letter is measured instead of
    the declared size being multiplied back by the declared metric.  This reads
    the outline off the asset bytes with fontTools, at the instance the argument
    names, so an expectation formed from it is independent of the rule measured.
    """
    font = TTFont(io.BytesIO(asset), lazy=False)
    name = (font.getBestCmap() or {}).get(ord("x"))
    glyphs = font.getGlyphSet() if weight is None else font.getGlyphSet(location={"wght": weight}, normalized=False)
    pen = BoundsPen(glyphs)
    glyphs[name].draw(pen)
    return int(round(pen.bounds[3]))


def hhea_metrics(asset: bytes) -> tuple[int, int, int]:
    """``hhea`` ascent, descent and em, read WITHOUT the function under test.

    The common first baseline is derived from these three numbers, so an
    expectation formed from them has to come off the asset bytes rather than
    through the module that is being measured.
    """
    font = TTFont(io.BytesIO(asset))
    return int(font["hhea"].ascent), int(font["hhea"].descent), int(font["head"].unitsPerEm)


def sx_height(asset: bytes) -> tuple[int, int]:
    """``OS/2.sxHeight`` and the em of a font, read WITHOUT the function under test."""
    font = TTFont(io.BytesIO(asset))
    return int(font["OS/2"].sxHeight), int(font["head"].unitsPerEm)


def weighted_font(path: Path, family: str, classification: str, weights: tuple[int, int]) -> tuple[bytes, bytes]:
    """The fixture font of ``family``, carrying a weight range of its very own.

    ``make_font`` builds every fixture at ``usWeightClass`` 400.  A bundle of
    them therefore has ONE range across all its faces, and the rule "each face
    declares the range of its own font and clamps only inside it" cannot be
    measured on it: clamping every face against the first face's range gives
    the same stylesheet.  That is the blind spot mutant M24 of the presentation
    round survived, and it is what this builder removes.

    The two sources ``_font_facts`` reads are both reachable: a range with two
    different bounds is written as a real ``wght`` axis, a single weight as the
    ``usWeightClass`` of a static font.  The axis default is deliberately put at
    neither bound where the range allows it, because a real variable font rests
    at 400 inside a wider range and a reader that took the default for the
    minimum would understate the file.
    """
    low, high = weights
    key = (family, classification, weights)
    if key not in _WEIGHTED_FONT_CACHE:
        scratch = Path(tempfile.mkdtemp())
        try:
            asset, _source = make_font(scratch / "f.woff2", family, classification)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        font = TTFont(io.BytesIO(asset))
        if low == high:
            font["OS/2"].usWeightClass = low
        else:
            axis = Axis(); axis.axisTag = "wght"; axis.minValue = low
            axis.defaultValue = min(max(400, low), high); axis.maxValue = high
            axis.axisNameID = 256
            font["name"].setName("Weight", 256, 3, 1, 0x409)
            table = newTable("fvar"); table.axes = [axis]; table.instances = []
            font["fvar"] = table
        source_buffer = io.BytesIO(); font.flavor = None; font.save(source_buffer)
        buffer = io.BytesIO(); font.flavor = "woff2"; font.save(buffer)
        _WEIGHTED_FONT_CACHE[key] = (buffer.getvalue(), source_buffer.getvalue())
    asset, source = _WEIGHTED_FONT_CACHE[key]
    path.write_bytes(asset); path.with_suffix(".ttf").write_bytes(source)
    return asset, source


def cached_metadata(asset: bytes) -> dict:
    key = visual.sha256_bytes(asset)
    if key not in _METADATA_CACHE:
        _METADATA_CACHE[key] = visual.extract_font_metadata(asset)
    return copy.deepcopy(_METADATA_CACHE[key])


# Every code point Python calls whitespace that HTML5 does not.  U+000B and
# U+001C-U+001F belong to the same difference but are refused one rule earlier
# by _SHEET_CONTROL; the nineteen below are "empty text" to str.strip() and
# ordinary *character tokens* to Blink.
NON_HTML5_WHITESPACE = tuple(chr(code) for code in (
    0x0085, 0x00A0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005,
    0x2006, 0x2007, 0x2008, 0x2009, 0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000))
# Three of the nineteen no longer reach the text rule or the tag rule at all:
# closing the separators put U+0085, U+2028 and U+2029 into ``_SHEET_CONTROL``,
# which is asked first, exactly as U+000B and U+001C-U+001F always were.  Every
# one of the nineteen is still refused - the class below only records which rule
# now owns which character, so the class stays measured as one class and a
# later reader does not mistake the changed message for a lost rejection.
SEPARATOR_CONTROLS = frozenset("\u0085\u2028\u2029")
#: U+000C is the fourth character of that same group, closed one round later.  It
#: is ASCII whitespace to HTML5 and a whitespace token to css-syntax-3 - both tag
#: grammars and the CSS tokenizer go on carrying it, and ``ASCII_WHITESPACE`` is
#: unchanged - but it is a ``str.splitlines()`` boundary, so a string carrying one
#: makes one record read as two.  ``_SHEET_CONTROL`` owns it now, exactly as it
#: owns the three above, and these tests say which rule owns which character so a
#: changed message can never pass for a lost rejection.
WHITESPACE_CONTROLS = frozenset("\x0c")
#: The HTML5 whitespace the document form still carries in every position.
OPEN_WHITESPACE = "".join(char for char in visual.ASCII_WHITESPACE if char not in WHITESPACE_CONTROLS)


def css_case(test, check, css: str, message: str | None = "network or active content") -> None:
    """Hold one stylesheet to the rule that owns it, losing neither statement.

    ``message`` is the refusal css-syntax-3 earns for this payload, or ``None``
    when the payload is clean.  A stylesheet whose whitespace is U+000C cannot
    reach ``validate_sheet_manifest`` any more - the control class refuses the
    member one rule earlier - so what css-syntax-3 says about it is measured at
    ``_scan_sheet_css``, which is the reader that statement is about, and the
    named refusal is measured beside it.  Every other payload runs through the
    manifest exactly as before.  Nothing is dropped: the tokenizer rule keeps its
    measurement and the control rule gains one.
    """
    control = bool(visual._SHEET_CONTROL.search(css))
    run = (lambda: visual._scan_sheet_css("style.css", css)) if control else (lambda: check(css))
    if message is None:
        run()
    else:
        with test.assertRaisesRegex(visual.VisualDecisionError, message):
            run()
    if control:
        with test.assertRaisesRegex(visual.VisualDecisionError, "forbidden control character"):
            check(css)


class VisualDecisionCoreR19Tests(TimeLimitedTestCase):
    """Unicode semantics where a specification says ASCII, and what that cost.

    r18 measured in Blink what one U+00A0 does to a validated sheet.  The
    scanner reads "empty text, ignore"; Blink reads a character token, and a
    character token in the "in head" insertion mode closes ``<head>``.  The CSP
    ``<meta>`` then stands in ``<body>``, where a meta-delivered policy is
    ignored outright: the sheet fetched across origins exactly like the
    policy-free control, and the validator said ACCEPTED to both.

    The same difference sat in the two tag grammars (where ``\\s`` matched 29
    code points and HTML5 matches five), in the doctype comparison, in the CSS
    ident folds, in the SVG ``viewBox`` split and in the specimen coverage set.
    One defect, sixteen sites, so the rules below are stated over the *class*:
    the named helpers, then every position and every code point at which the
    class was reachable, then the positive controls that keep the fix from
    turning into a wall.
    """

    SECTION_IDS = ("candidates-color", "candidates-typography", "candidates-code-font",
                   "candidates-logo", "candidates-lockup", "recommendations", "owner-review")
    SECTIONS = "".join(f'<section id="{name}"><h2>x</h2></section>' for name in SECTION_IDS)
    LINK = '<link rel="stylesheet" href="style.css">'
    LOCAL_CSS = '@font-face{src:url("assets/a.svg")}'

    def meta(self, policy: str | None = None) -> str:
        return f'<meta http-equiv="Content-Security-Policy" content="{visual.SHEET_CSP if policy is None else policy}">'

    def page(self, *, head: str | None = None, body: str | None = None, doctype: str = "<!doctype html>",
             charset: str = '<meta charset="utf-8">', lang: str = ' lang="de"',
             after_doctype: str = "", in_html: str = "", between: str = "", trailing: str = "") -> str:
        head = self.meta() + self.LINK if head is None else head
        body = "<main>" + self.SECTIONS + "</main>" if body is None else body
        return (doctype + after_doctype + f"<html{lang}>" + in_html + "<head>" + charset + head
                + "<title>x</title></head>" + between + "<body>" + body + "</body></html>" + trailing)

    def sheet(self):
        """A callable that validates one sheet built from the given page and CSS."""
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        sheet = Path(temp.name).resolve() / "sheet"; (sheet / "assets").mkdir(parents=True)
        (sheet / "assets/a.svg").write_text("svg")

        def check(page: str, *, css: str | None = None, raw: bytes | None = None) -> None:
            (sheet / "index.html").write_bytes(page.encode("utf-8") if raw is None else raw)
            (sheet / "style.css").write_bytes((self.LOCAL_CSS if css is None else css).encode("utf-8"))
            members = [sheet / "index.html", sheet / "style.css", sheet / "assets/a.svg"]
            files = [{"path": item.relative_to(sheet).as_posix(), "sha256": visual.sha256_bytes(item.read_bytes())} for item in members]
            manifest = {"schema_version": "brandkit-visual-sheet-manifest.v1", "brief_sha256": "a" * 64,
                        "candidate_set_sha256": "b" * 64, "affinity_observation_sha256": "c" * 64, "files": files,
                        "viewports": [320, 768, 1440], "offline": True, "interactive_controls": False,
                        "preselection": False, "normative": False, "human_review_status": "required"}
            with deadline(60, "validate_sheet_manifest"):
                visual.validate_sheet_manifest(manifest, sheet)
        return check

    # --- the class itself, at the named helpers -------------------------------

    def test_ascii_semantics_are_a_named_facility_and_not_a_python_default(self):
        self.assertEqual(" \t\n\r\f", visual.ASCII_WHITESPACE)
        # The grammar set is UNCHANGED by the form-feed round: U+000C is still
        # HTML5 whitespace and still a css-syntax-3 whitespace token.  What moved
        # is which rule sees it first, because it is a str.splitlines() boundary
        # and made one record read as two.  Both halves are stated here so
        # neither can be inferred away from the other.
        self.assertEqual(frozenset("\x0c"), WHITESPACE_CONTROLS)
        self.assertTrue(visual.ascii_blank("\x0c"))
        self.assertIsNotNone(visual._SHEET_CONTROL.search("\x0c"))
        self.assertEqual(" \t\n\r", OPEN_WHITESPACE)
        self.assertEqual(2, len("a\x0cb".splitlines()))
        for char in visual.ASCII_WHITESPACE:
            self.assertTrue(visual.ascii_blank(char), repr(char))
        for char in NON_HTML5_WHITESPACE:
            self.assertTrue(char.isspace(), repr(char))              # Python calls it whitespace
            self.assertFalse(visual.ascii_blank(char), repr(char))   # HTML5 does not
            self.assertEqual(char, visual.ascii_strip(char), repr(char))
            self.assertEqual([f"a{char}b"], visual.ascii_split(f"a{char}b"), repr(char))
            self.assertEqual(["a", "b"], f"a{char}b".split(), repr(char))
        # U+212A KELVIN SIGN is the only code point in all of Unicode whose
        # lowercase is pure ASCII; the Turkish dotted capital I grows a
        # combining mark.  ASCII case-insensitive matching does neither.
        self.assertEqual("k", "\u212a".lower())
        self.assertEqual("\u212a", visual.ascii_lower("\u212a"))
        self.assertEqual(2, len("\u0130".lower()))
        self.assertEqual("\u0130", visual.ascii_lower("\u0130"))
        self.assertEqual("abc-xyz_0", visual.ascii_lower("ABC-XYZ_0"))
        # XML has no form feed, so the SVG list separator is the four-character set.
        self.assertEqual(" \t\n\r", visual.XML_WHITESPACE)
        self.assertEqual(["a\fb"], visual.ascii_split("a\fb", visual.XML_WHITESPACE))
        # The two tag grammars carry the class explicitly rather than by a flag,
        # so a later edit cannot put Unicode whitespace back by accident.
        for pattern in (visual._SHEET_MARKUP.pattern, visual._SHEET_ATTRIBUTE.pattern):
            self.assertNotIn("\\s", pattern)
        # And the two denylists keep BOTH folds: the wider one can only ever
        # match more spellings of a forbidden token, so dropping it would be the
        # one direction that loses a rejection.
        self.assertEqual({"owner"}, visual._field_name_tokens("OWNER"))
        self.assertEqual({"note", "owner"}, visual._field_name_tokens("note_owner"))
        self.assertLessEqual(set(re.split(r"[-_]", "OWNER".lower())), visual._field_name_tokens("OWNER"))

    # --- C1-r18: the text channel that closed the head ------------------------

    def test_a_character_the_scanner_called_blank_can_never_close_the_head(self):
        check = self.sheet()
        # The three positions r18 verified in Blink, plus the two neighbouring
        # ones.  In each of them a single non-HTML5 whitespace character is a
        # character token, and in "in head" a character token closes <head> - a
        # policy delivered from <body> is then ignored: O2's access log carried
        # the cross-origin GET for the U+00A0 sheet and for the CSP-free
        # control, and nothing at all for the sheet with the ASCII space.
        for char in NON_HTML5_WHITESPACE:
            # Refused either way; the three separators are taken by the control
            # class one rule earlier (see SEPARATOR_CONTROLS).
            expected = "forbidden control character" if char in SEPARATOR_CONTROLS else "text outside <body>"
            for page in (self.page(after_doctype=char),
                         self.page(in_html=char),
                         self.page(head=char + self.meta() + self.LINK),
                         self.page(between=char),
                         self.page(trailing=char)):
                with self.subTest(char=f"U+{ord(char):04X}"), \
                     self.assertRaisesRegex(visual.VisualDecisionError, expected):
                    check(page)
        # The four HTML5 whitespace characters this class leaves open are still a
        # document in all five places at once - this is a rule, not a wall.
        for char in OPEN_WHITESPACE:
            check(self.page(after_doctype=char, in_html=char, head=char + self.meta() + self.LINK,
                            between=char, trailing=char))
        # U+000C is HTML5 whitespace too, and the control class owns it: a
        # document carrying one is refused in every one of the five positions
        # rather than being a document.
        for char in sorted(WHITESPACE_CONTROLS):
            for page in (self.page(after_doctype=char), self.page(in_html=char),
                         self.page(head=char + self.meta() + self.LINK),
                         self.page(between=char), self.page(trailing=char)):
                with self.subTest(owned=f"U+{ord(char):04X}"), \
                     self.assertRaisesRegex(visual.VisualDecisionError, "forbidden control character"):
                    check(page)
        # Inside <body> such a character is ordinary text for both readers, so
        # nothing legitimate is refused there either.
        for char in NON_HTML5_WHITESPACE:
            pages = (self.page(body="<main>" + self.SECTIONS + f"<p>a{char}b</p></main>"),
                     self.page(body="<main>" + self.SECTIONS + f'<p class="a{char}b">x</p></main>'))
            for page in pages:
                with self.subTest(in_body=f"U+{ord(char):04X}"):
                    if char in SEPARATOR_CONTROLS:
                        # A line terminator is not ordinary text anywhere: it
                        # makes one record read as two, so the control class
                        # refuses it inside <body> as well.
                        with self.assertRaisesRegex(visual.VisualDecisionError, "forbidden control character"):
                            check(page)
                    else:
                        check(page)

    def test_the_tag_grammar_is_html5_whitespace_and_not_pythons(self):
        check = self.sheet()
        # ``\s`` in the two tag patterns was Unicode whitespace: </p+U+00A0>
        # closed a <p> for both Python readers, while Blink reads the tag name
        # "p<U+00A0>", finds no such open element and drops the end tag on the
        # floor - the element stays open and the tree is a different tree.
        for char in NON_HTML5_WHITESPACE:
            expected = ("forbidden control character" if char in SEPARATOR_CONTROLS
                        else "markup that is not a plain tag")
            for payload in (f'<p class="q">x</p{char}>',
                            f'<p{char}class="q">x</p>',
                            f'<p class="q"{char}>x</p>',
                            f'<img{char}src="assets/a.svg" alt="">',
                            f'<img src="assets/a.svg" alt=""{char}/>'):
                with self.subTest(char=f"U+{ord(char):04X}"), \
                     self.assertRaisesRegex(visual.VisualDecisionError, expected):
                    check(self.page(body="<main>" + self.SECTIONS + payload + "</main>"))
        # The same five spellings with real HTML5 whitespace are documents - for
        # the four characters this class leaves open.
        for char in OPEN_WHITESPACE:
            for payload in (f'<p class="q">x</p{char}>',
                            f'<p{char}class="q">x</p>',
                            f'<p class="q"{char}>x</p>',
                            f'<img{char}src="assets/a.svg" alt="">',
                            f'<img src="assets/a.svg" alt=""{char}/>'):
                check(self.page(body="<main>" + self.SECTIONS + payload + "</main>"))
        # ... and the fifth is refused in every one of them, by the control class.
        for char in sorted(WHITESPACE_CONTROLS):
            for payload in (f'<p class="q">x</p{char}>',
                            f'<p{char}class="q">x</p>',
                            f'<p class="q"{char}>x</p>',
                            f'<img{char}src="assets/a.svg" alt="">',
                            f'<img src="assets/a.svg" alt=""{char}/>'):
                with self.subTest(owned=f"U+{ord(char):04X}"), \
                     self.assertRaisesRegex(visual.VisualDecisionError, "forbidden control character"):
                    check(self.page(body="<main>" + self.SECTIONS + payload + "</main>"))

    def test_the_doctype_is_compared_by_ascii_folding_and_ascii_trimming(self):
        # The second reader's own doctype gate used str.strip().lower(), so a
        # declaration padded with U+00A0 read as the plain HTML5 doctype here
        # and as a quirks-mode-triggering bogus doctype in a browser.
        for decl in ("doctype html", "DOCTYPE HTML", " doctype html ", "\tDoctype Html\n"):
            visual._SheetHTMLView().handle_decl(decl)
        for char in NON_HTML5_WHITESPACE:
            for decl in (f"doctype{char}html", f"{char}doctype html", f"doctype html{char}"):
                with self.assertRaisesRegex(visual.VisualDecisionError, "markup declaration"):
                    visual._SheetHTMLView().handle_decl(decl)

    def test_a_specification_that_says_ascii_is_read_as_ascii_outside_the_html_too(self):
        # The SVG viewBox is a list of numbers separated by XML whitespace.  With
        # str.split() a U+00A0 between two of them made four values out of three,
        # so an invalid viewBox validated - and the bound that the geometry check
        # is measured against came out of it.
        shape = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}"><rect x="1" y="1" width="2" height="2" fill="#111111"/></svg>'
        self.assertEqual("0 0 64 64", visual._svg_static_checks(shape.format(box="0 0 64 64").encode(), "logo", "logo-a")["view_box"])
        self.assertEqual("0 0 64 64", visual._svg_static_checks(shape.format(box="0\t0\n64\r64").encode(), "logo", "logo-a")["view_box"])
        for char in NON_HTML5_WHITESPACE:
            with self.assertRaisesRegex(visual.VisualDecisionError, "invalid viewBox"):
                visual._svg_static_checks(shape.format(box=f"0 0 64{char}64").encode(), "logo", "logo-a")
        # SVG paint keywords are ASCII case-insensitive.  A Unicode fold decides
        # "this paint is none" for a spelling the renderer paints, and the colour
        # never reaches the set the monochrome test counts.
        painted = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect x="1" y="1" width="2" height="2" fill="#111111"/><rect x="3" y="3" width="2" height="2" fill="{paint}"/></svg>'
        for spelling in ("none", "NONE", "None", "transparent", "TRANSPARENT"):
            self.assertEqual(1, visual._svg_static_checks(painted.format(paint=spelling).encode(), "logo", "logo-a")["color_count"], spelling)
        two = visual._svg_static_checks(painted.format(paint="#222222").encode(), "logo", "logo-a")
        self.assertEqual(2, two["color_count"])
        self.assertFalse(two["monochrome_static"])
        # The specimen coverage set is "every character a font has to draw", and
        # str.isspace() would drop a drawn character straight out of it.
        self.assertEqual(sorted({ord(c) for c in visual.SPECIMEN if c not in visual.ASCII_WHITESPACE}),
                         sorted({ord(c) for c in visual.SPECIMEN if not c.isspace()}))
        self.assertEqual(set(), {ord(c) for c in visual.SPECIMEN} & {ord(c) for c in NON_HTML5_WHITESPACE})

    def test_an_svg_asset_may_not_carry_the_same_id_twice(self):
        """The duplicate-``id`` rule of ``_svg_static_checks``, measured.

        Register point 4 says asset members reach the sheet as bytes that are
        only hashed, so what an asset is *allowed to contain* is decided here
        and nowhere else.  Two elements with the same ``id`` is a document no
        conforming producer emits and every renderer resolves by guessing:
        ``url(#mark)``, ``<use href="#mark">`` and every ARIA reference in the
        page pick one of the two, and which one is not specified.  The rule
        exists for that reason and this test is the first to ask about it - an
        r1h-critic mutant that dropped it was accepted by both suites.

        The owner supplies logo and lockup assets, so the rule is owner
        reachable; it is asked here in the three shapes an id can repeat in,
        plus the two neighbours it must not swallow.
        """
        shape = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                 '{body}</svg>')
        for label, body in {
                "two siblings": '<rect id="mark" x="1" y="1" width="2" height="2" fill="#111111"/>'
                                '<rect id="mark" x="4" y="4" width="2" height="2" fill="#111111"/>',
                "parent and child": '<g id="mark"><rect id="mark" x="1" y="1" width="2" height="2" fill="#111111"/></g>',
                "root and descendant": '<g id="root"><g><rect id="root" x="1" y="1" width="2" height="2" fill="#111111"/></g></g>',
        }.items():
            with self.subTest(shape=label):
                with self.assertRaisesRegex(visual.VisualDecisionError, "has duplicate SVG ids"):
                    visual._svg_static_checks(shape.format(body=body).encode(), "logo a", "logo-a")
        # Distinct ids pass, and an id is compared as the XML identifier it is:
        # ``mark`` and ``MARK`` are two ids in XML, so the rule must not fold
        # them together and refuse a document every renderer resolves.
        for body in ('<rect id="mark" x="1" y="1" width="2" height="2" fill="#111111"/>'
                     '<rect id="other" x="4" y="4" width="2" height="2" fill="#111111"/>',
                     '<rect id="mark" x="1" y="1" width="2" height="2" fill="#111111"/>'
                     '<rect id="MARK" x="4" y="4" width="2" height="2" fill="#111111"/>',
                     '<rect x="1" y="1" width="2" height="2" fill="#111111"/>'
                     '<rect x="4" y="4" width="2" height="2" fill="#111111"/>'):
            self.assertEqual("0 0 64 64",
                             visual._svg_static_checks(shape.format(body=body).encode(), "logo a", "logo-a")["view_box"])

    def test_the_css_scanner_folds_idents_the_way_the_tokenizer_folds_them(self):
        check = self.sheet()
        # url( is an ASCII case-insensitive ident, and both spellings open the
        # same token here as in Blink.
        check(self.page(), css='b{background:URL(assets/a.svg)}')
        check(self.page(), css='b{background:Url(assets/a.svg)}')
        # The one reachable difference in this module's own keyword sets is the
        # "k" of -webkit-image-set: str.lower() folds U+212A onto it and turns an
        # ident Blink has never heard of into a function on the allowlist, whose
        # string argument then counts as a reference.  ASCII folding leaves it
        # the unknown function it is, and an unknown function fails closed.
        with self.assertRaisesRegex(visual.VisualDecisionError, "unsupported function"):
            check(self.page(), css='b{background:-web\u212ait-image-set("assets/a.svg")}')
        check(self.page(), css='b{background:-webkit-image-set("assets/a.svg")}')
        # The forbidden-token scan is a denylist and keeps both folds, so no
        # spelling of @import gets past it.
        for token in ("@import", "@IMPORT", "@Import"):
            with self.assertRaisesRegex(visual.VisualDecisionError, "network or active content"):
                check(self.page(), css=token + ' "x.css";b{background:url(assets/a.svg)}')

    # --- C2-r18: the encoding declaration, bound to the bytes -----------------

    def test_a_sheet_declares_exactly_the_encoding_it_is_validated_as(self):
        check = self.sheet()
        check(self.page())
        # Blink obeys the declaration: document.characterSet was Shift_JIS on a
        # sheet this module had certified while decoding it as UTF-8.  Validator
        # and renderer then read one byte stream as two character streams, and
        # the artifact whose only purpose is human inspection showed a text
        # nobody had validated.
        for label in ("shift_jis", "big5", "euc-jp", "gb18030", "euc-kr", "windows-1252",
                      "iso-8859-1", "utf-16", "x-user-defined", "nonsense", "UTF-8", "utf8", " utf-8"):
            with self.assertRaisesRegex(visual.VisualDecisionError, "character encoding other than utf-8"):
                check(self.page(charset=f'<meta charset="{label}">'))
        # No declaration at all is not "UTF-8 by default": HTML5 sniffs, and the
        # sniffing fallback is not the assumption this validator decodes with.
        with self.assertRaisesRegex(visual.VisualDecisionError, "does not open with its encoding declaration"):
            check(self.page(charset=""))
        # It has to be the first thing in <head>, which is the only position in
        # which the 1024-byte prescan window can be reasoned about at all.
        with self.assertRaisesRegex(visual.VisualDecisionError, "does not open with its encoding declaration"):
            check(self.page(charset="", head=self.meta() + '<meta charset="utf-8">' + self.LINK))
        # HTML5 honours the FIRST declaration, so a second one is a statement
        # this apparatus would read and the browser would not.
        with self.assertRaisesRegex(visual.VisualDecisionError, "more than one character encoding"):
            check(self.page(head='<meta charset="utf-8">' + self.meta() + self.LINK))
        with self.assertRaisesRegex(visual.VisualDecisionError, "more than one character encoding"):
            check(self.page(body='<meta charset="utf-8">' + "<main>" + self.SECTIONS + "</main>"))
        # And the window is measured in bytes, because <html lang="..."> stands
        # in front of it and is an attribute value of unbounded length.
        with self.assertRaisesRegex(visual.VisualDecisionError, "outside the first 1024 bytes"):
            check(self.page(lang=' lang="' + "d" * 1100 + '"'))
        check(self.page(lang=' lang="' + "d" * 900 + '"'))
        # A byte order mark overrides every declaration in HTML5 and is a stray
        # code point in CSS, so no member of the bundle may carry one.
        with self.assertRaisesRegex(visual.VisualDecisionError, "byte order mark"):
            check(self.page(), raw="\ufeff".encode("utf-8") + self.page().encode("utf-8"))
        with self.assertRaisesRegex(visual.VisualDecisionError, "byte order mark"):
            check(self.page(), css="\ufeff" + self.LOCAL_CSS)

    # --- the second reader, pinned by an input that really separates the two ---

    def test_the_two_readers_are_pinned_by_an_input_that_really_separates_them(self):
        # r17 called the cross-check redundant because no input in the suite had
        # ever made the two readers disagree.  r18 produced three, and they are
        # exactly the family this round closed in the *grammar* - so the check
        # has to be pinned with the grammar it is a net under, not with a payload
        # the grammar now catches one rule earlier.  The pre-r19 spelling of the
        # two patterns is restored for the length of each case; everything else,
        # the comparison in _SheetParser.close included, is the shipping code.
        old_markup = re.compile(r'<(/?)([a-z][a-z0-9]*)((?:\s+[a-z][a-z0-9-]*(?:="[^"<>&]*")?)*)\s*(/?)>')
        old_attribute = re.compile(r'\s+([a-z][a-z0-9-]*)(?:="([^"<>&]*)")?')
        new_markup, new_attribute = visual._SHEET_MARKUP, visual._SHEET_ATTRIBUTE
        self.addCleanup(setattr, visual, "_SHEET_MARKUP", new_markup)
        self.addCleanup(setattr, visual, "_SHEET_ATTRIBUTE", new_attribute)
        check = self.sheet()
        nb = "\u00a0"
        for payload in (f'<p class="q">x</p{nb}>', f'<p{nb}class="q">x</p>',
                        f'<p class="q"{nb}>x</p>', f'<img{nb}src="assets/a.svg" alt="">'):
            page = self.page(body="<main>" + self.SECTIONS + payload + "</main>")
            # With the ASCII grammar the form refuses it and the cross-check is
            # never reached.
            with self.assertRaisesRegex(visual.VisualDecisionError, "markup that is not a plain tag"):
                check(page)
            visual._SHEET_MARKUP, visual._SHEET_ATTRIBUTE = old_markup, old_attribute
            try:
                scanned = visual._SheetParser()._scan(page)
                view = visual._SheetHTMLView(); view.feed(page); view.close()
                # The two readers produce the same NUMBER of events and a
                # different sequence, which is why "they agree on how many tags
                # there are" is not the property this check states.
                self.assertEqual(len(scanned), len(view.events), payload)
                self.assertNotEqual(scanned, view.events, payload)
                divergence = next(index for index, (one, two) in enumerate(zip(scanned, view.events)) if one != two)
                self.assertGreater(divergence, 0, payload)
                # The scan alone accepts the document; only the second reader
                # refuses it, and that refusal is the whole apparatus here.
                with self.assertRaisesRegex(visual.VisualDecisionError, "read differently by the two independent parsers"):
                    check(page)
            finally:
                visual._SHEET_MARKUP, visual._SHEET_ATTRIBUTE = new_markup, new_attribute
        # On the document form as it ships the two readers agree event for
        # event, so the check above is a net and not a second grammar.
        page = self.page()
        view = visual._SheetHTMLView(); view.feed(page); view.close()
        self.assertEqual(view.events, visual._SheetParser()._scan(page))
        # close() is kept, and what it can and cannot do is measured rather than
        # claimed: HTMLParser.close() runs goahead(1), whose only extra output is
        # handle_data, and this view records no data events - so for any document
        # the form accepts (which must end in </html> plus ASCII whitespace) the
        # unclosed reader produces the identical stream.  That is why "the second
        # reader is never closed" survives the suite, and it survives as an
        # equivalence, not as a gap.
        for probe in (page, self.page(trailing="\n  "),
                      self.page(body="<main>" + self.SECTIONS + "<p>a&amp;b</p></main>"),
                      self.page(body="<main>" + self.SECTIONS + '<img src="assets/a.svg" alt=""/></main>')):
            unclosed = visual._SheetHTMLView(); unclosed.feed(probe)
            closed = visual._SheetHTMLView(); closed.feed(probe); closed.close()
            self.assertEqual(closed.events, unclosed.events)
            self.assertEqual(closed.events, visual._SheetParser()._scan(probe))

    # --- the state machine of the document form -------------------------------

    def test_the_document_form_is_a_state_machine_and_every_state_is_stated(self):
        check = self.sheet()
        head = self.meta() + self.LINK
        cases = (
            # HTML5 discards a solidus on an end tag, so </div/> closes the div;
            # an end tag that carries markup is where two readers part.
            (self.page(body="<main>" + self.SECTIONS + '<div class="a">x</div/></main>'), "an end tag carries markup"),
            (self.page(body="<main>" + self.SECTIONS + "</main/>"), "an end tag carries markup"),
            (self.page(body="<main>" + self.SECTIONS + '<div class="a">x</div class="a"></main>'), "an end tag carries markup"),
            # <head> is opened exactly once and may not be reopened.
            (self.page().replace("</head><body>", "</head><head></head><body>", 1), "not the first child of <html>"),
            # <body> follows a CLOSED <head>, and there is one of it.
            ('<!doctype html><html lang="de"><body><main>' + self.SECTIONS + "</main></body></html>", "does not follow a closed <head>"),
            (self.page().replace("</body></html>", "</body><body></body></html>", 1), "does not follow a closed <head>"),
            # A document that never opens a <body>, one that never closes it,
            # and one that never closes <html>.
            ('<!doctype html><html lang="de"><head><meta charset="utf-8">' + head + "<title>x</title></head></html>",
             "no closed <head> and <body>"),
            (self.page().replace("</body>", "", 1), "unbalanced end tag"),
            (self.page().replace("</html>", "", 1), "unclosed element"),
        )
        for page, reason in cases:
            with self.assertRaisesRegex(visual.VisualDecisionError, reason):
                check(page)
        check(self.page())

    def test_a_tag_with_no_allowlist_entry_carries_no_attributes_at_all(self):
        check = self.sheet()
        # allowed_attrs.get(tag, set()) is the whole rule for a tag that has no
        # entry of its own, and nothing measured it: <main foo="x"> and
        # <li data-y="z"> went through with only the on*/style guard left.
        for payload, tag in (('<main foo="x">', "main"), ('<ul><li data-y="z">x</li></ul>', "li"),
                             ('<h1 id="x">t</h1>', "h1"), ('<h2 class="a">t</h2>', "h2"),
                             ('<strong lang="de">x</strong>', "strong"), ('<code class="c">x</code>', "code"),
                             ('<footer class="f">x</footer>', "footer"), ('<ul class="g"><li>x</li></ul>', "ul")):
            body = (payload + self.SECTIONS + "</main>") if tag == "main" else ("<main>" + self.SECTIONS + payload + "</main>")
            with self.assertRaisesRegex(visual.VisualDecisionError, "forbidden HTML attributes: " + tag):
                check(self.page(body=body))
        # The tags that do have an entry keep exactly that entry.
        check(self.page(body="<main>" + self.SECTIONS + '<div class="a"><p class="b">x</p><pre class="specimen">y</pre></div></main>'))
        # ``span`` gained an entry with the numeral rows, and it is exactly one
        # attribute: the class that names the feature beside the digits.  The entry
        # is therefore stated in both directions - the class passes, everything
        # else on the same tag is still refused - so it is a statement and not a door.
        check(self.page(body="<main>" + self.SECTIONS + '<p class="numerals"><span class="numerals-name">x</span></p></main>'))
        self.assertEqual({"class"}, visual._SheetParser.allowed_attrs["span"])
        for payload in ('<span id="x">y</span>', '<span lang="de">y</span>', '<span data-x="1">y</span>'):
            with self.subTest(span=payload), self.assertRaisesRegex(visual.VisualDecisionError, "forbidden HTML attributes: span"):
                check(self.page(body="<main>" + self.SECTIONS + payload + "</main>"))

    # --- the sealed run: the anchor, the walk, and the identity ---------------

    def sealed_project(self) -> Path:
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve() / "proj"
        seal = project / ".brandkit/work" / visual.PROTECTED_RUN_ID
        (seal / "discovery").mkdir(parents=True)
        (seal / "discovery/handoff.json").write_bytes(visual.canonical_json(
            {"schema_version": "brandkit-discovery-handoff.v1", "run_id": visual.PROTECTED_RUN_ID}))
        (seal / "sealed.txt").write_text("sealed")
        return project.resolve(strict=True)

    def test_a_run_identity_that_cannot_be_read_is_a_refusal_and_not_an_absence(self):
        project = self.sealed_project()
        work = project / ".brandkit/work"
        before = visual.protected_run_inventory(project)
        # Absent really is absent: a fresh run, a run without discovery/ and a
        # discovery/ without a handoff all get their workspace.  The guard is
        # about uncertainty, not about strictness.
        self.assertTrue(visual.visual_workspace(project, "fresh", create=True).is_dir())
        (work / "no-discovery").mkdir()
        self.assertTrue(visual.visual_workspace(project, "no-discovery", create=True).is_dir())
        (work / "empty-discovery/discovery").mkdir(parents=True)
        self.assertTrue(visual.visual_workspace(project, "empty-discovery", create=True).is_dir())
        # Present but unreadable is neither absent nor a permission.  Each of
        # these used to be swallowed by "except (VisualDecisionError, OSError):
        # handoff = None", which made the whole run-identity anchor fail OPEN -
        # r18 found the symlink and the directory shape and named them E and F.
        (work / "linked/discovery").mkdir(parents=True)
        (work / "linked/discovery/handoff.json").symlink_to(work / visual.PROTECTED_RUN_ID / "discovery/handoff.json")
        (work / "dir-handoff/discovery/handoff.json").mkdir(parents=True)
        (work / "unreadable/discovery").mkdir(parents=True)
        (work / "unreadable/discovery/handoff.json").write_text("{}")
        os.chmod(work / "unreadable/discovery/handoff.json", 0o000)
        self.addCleanup(os.chmod, work / "unreadable/discovery/handoff.json", 0o600)
        (work / "linked-discovery").mkdir()
        (work / "linked-discovery/discovery").symlink_to(work / visual.PROTECTED_RUN_ID / "discovery")
        (work / "file-run").write_text("not a directory")
        (work / "notjson/discovery").mkdir(parents=True)
        (work / "notjson/discovery/handoff.json").write_text("this is not json")
        (work / "noaccess/discovery").mkdir(parents=True)
        (work / "noaccess/discovery/handoff.json").write_text("{}")
        os.chmod(work / "noaccess/discovery", 0o000)
        self.addCleanup(os.chmod, work / "noaccess/discovery", 0o700)
        for run_id in ("linked", "dir-handoff", "unreadable", "linked-discovery", "file-run",
                       "notjson", "noaccess"):
            with self.assertRaises(visual.VisualDecisionError):
                visual.visual_workspace(project, run_id, create=True)
            self.assertFalse((work / run_id / "visual-decisions").exists(), run_id)
        # The walk that establishes the claim has three ways of not being able
        # to answer - the open of a component, the lstat of a component and the
        # read of the handoff - and all three are refusals.  The lstat one is
        # the only one no ordinary file system shape reaches, so it is injected:
        # an unanswerable lstat used to be indistinguishable from "there is
        # nothing here", and that is the shape of the whole fail-open defect.
        (work / "probe/discovery").mkdir(parents=True)
        (work / "probe/discovery/handoff.json").write_bytes(visual.canonical_json(
            {"schema_version": "brandkit-discovery-handoff.v1", "run_id": "probe"}))
        self.assertTrue(visual.visual_workspace(project, "probe", create=True).is_dir())
        shutil.rmtree(work / "probe/visual-decisions")
        original_lstat = visual.os.lstat

        def refusing(path, *args, dir_fd=None, **kwargs):
            if path == "discovery" and dir_fd is not None:
                raise PermissionError(13, "injected")
            return original_lstat(path, *args, dir_fd=dir_fd, **kwargs)

        visual.os.lstat = refusing
        try:
            with self.assertRaisesRegex(visual.VisualDecisionError, "cannot be identified"):
                visual.visual_workspace(project, "probe", create=True)
        finally:
            visual.os.lstat = original_lstat
        self.assertFalse((work / "probe/visual-decisions").exists())
        # The one residual, measured rather than claimed: moving discovery/ aside
        # defeats the anchor.  It needs write permission INSIDE the sealed tree -
        # the actor who needs no deputy in the first place - and the CI
        # environment has to hold that assumption (first-ci 0700, foreign owner).
        os.rename(work / visual.PROTECTED_RUN_ID, work / "renamed")
        os.rename(work / "renamed/discovery", work / "discovery-aside")
        self.assertTrue(visual.visual_workspace(project, "renamed", create=True).is_dir())
        shutil.rmtree(work / "renamed/visual-decisions")
        os.rename(work / "discovery-aside", work / "renamed/discovery")
        # With the handoff back in place the very same rename is refused again,
        # and the sealed tree is byte for byte what it was.
        with self.assertRaisesRegex(visual.VisualDecisionError, "discovery handoff for a different run"):
            visual.visual_workspace(project, "renamed", create=True)
        os.rename(work / "renamed", work / visual.PROTECTED_RUN_ID)
        self.assertEqual(before["tree_sha256"], visual.protected_run_inventory(project)["tree_sha256"])

    def test_the_create_walk_compares_the_anchor_at_every_component_it_opens(self):
        # The walk creates as it descends, so a component that becomes the
        # sealed tree between two steps has to be caught at that step: a check
        # after the walk is too late, because the next mkdir has already run
        # inside the seal.
        project = self.sealed_project()
        work = project / ".brandkit/work"
        before = visual.protected_run_inventory(project)
        original = visual.os.mkdir
        self.addCleanup(setattr, visual.os, "mkdir", original)
        fired = [False]

        def hooked(path, mode=0o777, *, dir_fd=None):
            if path == "myrun" and not fired[0]:
                # The sealed tree takes the run's name after every guard in
                # front of the walk has already given its answer.
                fired[0] = True
                os.rename(work / visual.PROTECTED_RUN_ID, work / "myrun")
            return original(path, mode, dir_fd=dir_fd)

        visual.os.mkdir = hooked
        try:
            with self.assertRaisesRegex(visual.VisualDecisionError, "protected first-ci path is forbidden"):
                visual.visual_workspace(project, "myrun", create=True)
        finally:
            visual.os.mkdir = original
            if (work / "myrun").exists():
                os.rename(work / "myrun", work / visual.PROTECTED_RUN_ID)
        self.assertTrue(fired[0])
        # Nothing was created inside the seal, and the seal is unchanged.
        self.assertEqual(["discovery", "sealed.txt"],
                         sorted(item.name for item in (work / visual.PROTECTED_RUN_ID).iterdir()))
        self.assertEqual(before["tree_sha256"], visual.protected_run_inventory(project)["tree_sha256"])

    def test_the_workspace_is_reidentified_after_the_walk_and_not_merely_restatted(self):
        # The walk validates descriptors; the caller gets a path, and a path is
        # resolved again by whoever uses it.  The swap below leaves a directory
        # standing at the very same path, so os.stat succeeds - only comparing
        # its identity with the descriptor that was walked can notice.
        project = self.sealed_project()
        workspace = project / ".brandkit/work/myrun/visual-decisions"
        original = visual._identity_holds
        self.addCleanup(setattr, visual, "_identity_holds", original)

        def hooked(anchor, candidate):
            answer = original(anchor, candidate)
            if candidate == workspace and workspace.is_dir():
                os.rename(workspace, workspace.parent / "walked")
                os.mkdir(workspace, 0o700)
            return answer

        visual._identity_holds = hooked
        try:
            with self.assertRaisesRegex(visual.VisualDecisionError, "changed while it was being opened"):
                visual.visual_workspace(project, "myrun", create=True)
        finally:
            visual._identity_holds = original
        # The directory that answers the returned path is a different object
        # from the one the walk validated, which is the whole statement.
        self.assertTrue((project / ".brandkit/work/myrun/walked").is_dir())
        self.assertTrue(workspace.is_dir())

    def test_the_protected_inventory_is_pinned_over_a_tree_that_has_directories(self):
        # tree_sha256 is the number the acceptance bar carries, and no test had
        # ever fixed what it is *over*.  Dropping directory members would take
        # the measured tree from 31 members to 27 and leave nothing red.
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve() / "proj"
        root = project / ".brandkit/work" / visual.PROTECTED_RUN_ID
        (root / "discovery").mkdir(parents=True)
        (root / "nested/deeper").mkdir(parents=True)
        (root / "discovery/handoff.json").write_bytes(b'{"run_id": "first-ci"}\n')
        (root / "brand-raw.md").write_text("raw\n")
        (root / "nested/deeper/leaf.txt").write_text("leaf\n")
        (root / "empty").mkdir()
        project = project.resolve(strict=True)
        inventory = visual.protected_run_inventory(project)
        members = inventory["members"]
        self.assertEqual(["discovery", "empty", "nested", "nested/deeper"],
                         [item["path"] for item in members if item["type"] == "directory"])
        self.assertEqual(["brand-raw.md", "discovery/handoff.json", "nested/deeper/leaf.txt"],
                         [item["path"] for item in members if item["type"] == "file"])
        self.assertEqual(7, len(members))
        for item in members:
            if item["type"] == "directory":
                self.assertEqual({"path": item["path"], "type": "directory", "size": 0, "sha256": None}, item)
        # The digest over that exact tree, pinned here rather than only in a
        # review, so that a change to the member set, to the record shape or to
        # the canonical form is red at the moment it is made.
        self.assertEqual("93e848d576b7caa724f677874d1d34847dc05c7fc16d45616492d7876faffc26",
                         inventory["tree_sha256"])
        self.assertEqual("brandkit-protected-run-inventory.v1", inventory["algorithm"])
        # Sorting is over the UTF-8 bytes of the path.  UTF-8 preserves code
        # point order, so the two readings agree by construction - which is
        # exactly why it is worth writing down that they do.
        self.assertEqual([item["path"] for item in members],
                         [item["path"] for item in sorted(members, key=lambda entry: (entry["path"], entry["type"]))])
        # A sealed name that is not a plain directory is refused rather than
        # anchored, in both of the shapes a file system can offer for it.
        other = project / ".brandkit/work/other"; other.mkdir()
        shutil.rmtree(root)
        root.write_text("not a directory")
        with self.assertRaisesRegex(visual.VisualDecisionError, "cannot be identified"):
            visual.visual_workspace(project, "run-b", create=True)
        with self.assertRaisesRegex(visual.VisualDecisionError, "absent or linked"):
            visual.protected_run_inventory(project)
        root.unlink()
        root.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(visual.VisualDecisionError, "cannot be identified"):
            visual.visual_workspace(project, "run-b", create=True)
        root.unlink()
        # And with the name really free, the ordinary run proceeds.
        self.assertTrue(visual.visual_workspace(project, "run-b", create=True).is_dir())

    def test_the_replay_ledger_lock_terminates_instead_of_waiting_for_ever(self):
        # flock(LOCK_EX) without LOCK_NB was the one call in this module that
        # does not terminate: a foreign holder of the lock file blocked
        # validate_bundle indefinitely, and a hang is not a fail-closed answer -
        # it is a suite that reports a coverage it does not have.  The bound is
        # measured here, with a real second holder of a real flock.
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve()
        secrets = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, secrets, True)
        key_path = secrets / "owner.key"; key_path.write_bytes(b"z" * 32); key_path.chmod(0o600)
        ledger_path = secrets / "replay-ledger.json"; ledger_path.write_bytes(b"{}"); ledger_path.chmod(0o600)
        # Without a holder the lock is taken and handed back.
        held = visual._affinity_ledger_lock(project, (key_path, ledger_path))
        self.assertIsNotNone(held); os.close(held)
        foreign = os.open(secrets / f".affinity-lock.{ledger_path.name}", os.O_RDWR)
        self.addCleanup(os.close, foreign)
        fcntl.flock(foreign, fcntl.LOCK_EX)
        pinned = visual.AFFINITY_LOCK_TIMEOUT
        self.addCleanup(setattr, visual, "AFFINITY_LOCK_TIMEOUT", pinned)
        self.assertGreater(pinned, 0)
        visual.AFFINITY_LOCK_TIMEOUT = 0.25
        started = time.monotonic()
        with deadline(30, "_affinity_ledger_lock under a foreign holder"):
            with self.assertRaisesRegex(visual.VisualDecisionError, "lock is unavailable"):
                visual._affinity_ledger_lock(project, (key_path, ledger_path))
        self.assertLess(time.monotonic() - started, 15)
        # And it really is the foreign holder that is refused: released, the
        # very same call succeeds again.
        fcntl.flock(foreign, fcntl.LOCK_UN)
        again = visual._affinity_ledger_lock(project, (key_path, ledger_path))
        self.assertIsNotNone(again); os.close(again)

    # --- what the delegation pin is actually worth ----------------------------

    def test_the_delegate_digest_is_compared_over_its_whole_length(self):
        # The pin test set SOURCE_VALIDATOR_SHA256 to "0"*64, which differs from
        # the real digest in its first character too - so it proved only that
        # SOME comparison happens.  A comparison over an 8-hex prefix is a 2**32
        # search; over 64 hex digits it is not.
        pinned = visual.SOURCE_VALIDATOR_SHA256
        self.addCleanup(setattr, visual, "SOURCE_VALIDATOR_SHA256", pinned)
        self.assertTrue(visual.HEX64.fullmatch(pinned))
        self.assertEqual(pinned, visual.sha256_bytes((visual.ROOT / visual.SOURCE_VALIDATOR_PATH).read_bytes()))
        for index in (0, 7, 8, 9, 31, 62, 63):
            variant = pinned[:index] + ("0" if pinned[index] != "0" else "1") + pinned[index + 1:]
            self.assertNotEqual(pinned, variant)
            self.assertEqual(index >= 8, pinned[:8] == variant[:8])
            visual.SOURCE_VALIDATOR_SHA256 = variant
            with self.assertRaisesRegex(visual.VisualDecisionError, "source validator hash differs"):
                visual._load_source_validator()
        visual.SOURCE_VALIDATOR_SHA256 = pinned
        self.assertEqual(str(visual.ROOT / visual.SOURCE_VALIDATOR_PATH), visual._load_source_validator().__file__)

    # --- the renderer, over the matrix its coupling really spans ---------------

    def render(self, *, color_reports: int = 1, mono: int = 1, recommendations: int = 0,
               family: str = "Fixture Serif", rationale: str | None = None) -> tuple[dict, str]:
        """Render one sheet through the production path and hand back its page.

        ``family`` and ``rationale`` are the two dimensions of CONTENT.  They are
        the only two values ``_render_sheet`` prints that no schema bounds, and
        ``family`` really is written into the font: it goes through
        ``make_font`` into the OpenType ``name`` table and comes back out of
        ``extract_font_metadata``, so this varies the production channel and not
        a literal in a fixture.

        Since the render-sheet fix ``color_reports`` and ``mono`` count
        CANDIDATES - colour candidates and code-font candidates, each binding a
        report of its own - because that is what the renderer now scales over.
        """
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve(); root = visual.visual_workspace(project, "run-a", create=True)
        # _render_sheet validates the logo report without a clock of its own, so
        # the observation it carries has to be live rather than fixed at NOW.
        live = int(time.time())
        advice = [{"kind": "color", "candidate_id": "color-a", "rationale": f"Begruendung {index}" if rationale is None else rationale,
                   "source_ids": ["impeccable"]} for index in range(recommendations)]
        candidate_set = multi_domain_bundle(project, root, live, {"color": color_reports, "typography": 1, "code-font": mono, "logo": 1, "lockup": 1},
                                            recommendations=advice, serif_family=family)
        brief = json.loads((root / "visual-brief.json").read_bytes())
        observation = json.loads((root / "affinity-observation.json").read_bytes())
        with deadline(150, "_render_sheet"):
            manifest = visual._render_sheet(root / "sheet", root, brief, candidate_set, observation)
        return manifest, (root / "sheet" / "index.html").read_text(encoding="utf-8")

    #: The two dimensions of CONTENT the matrix below varies, all of which the
    #: sheet has to carry: the markup characters ``html.escape`` exists for, the
    #: ASCII whitespace the document form allows, code points on both sides of
    #: the BMP, and U+00A0, which is whitespace to ``str`` and not to HTML5.
    #: U+000D is deliberately absent - not because the renderer minds it, but
    #: because ``read_text`` translates it, so the page this helper hands back
    #: would no longer be the bytes on disk.
    OWNER_TEXTS = ('Fixture & <b>Serif</b> "quoted" \'apostrophe\'',
                   "Fixture Serif — ½ ¡¿ Grüße",
                   "Fixture Serif ᚠᚢᚦ 日本語 𝄞 🎨",
                   "Fixture\tSerif\nZweite Zeile")

    def test_the_renderer_passes_its_own_document_form_over_the_whole_matrix(self):
        # One configuration is not a coupling test.  _render_sheet ends in
        # validate_sheet_manifest, so every leg below is a round trip through the
        # production path; what the matrix adds is that the SHAPES of the
        # generated document - an empty recommendation list, several colour
        # reports, several mono candidates - go through it too.
        #
        # r22 found what the shape dimension alone does not reach: the matrix
        # held every owner-supplied STRING at a fixed literal, so the two values
        # the renderer prints that no schema bounds - a font family out of the
        # OpenType name table, and a recommendation's prose - were never varied
        # at all.  Both are varied here, and both are varied through the
        # production channel: `family` is written into the font and read back by
        # extract_font_metadata.
        matrix = ((1, 1, 0, "Fixture Serif", None), (1, 1, 1, "Fixture Serif", None),
                  (1, 1, 2, "Fixture Serif", None), (2, 1, 1, "Fixture Serif", None),
                  (3, 1, 3, "Fixture Serif", None),
                  # Distinct on the two channels, so a leg cannot pass by
                  # counting the family where it means to count the prose.
                  *((1, 1, 2, text, f"Empfehlung wegen {text}") for text in self.OWNER_TEXTS))
        for color_reports, mono, recommendations, family, rationale in matrix:
            with self.subTest(colors=color_reports, mono=mono, recommendations=recommendations,
                              family=family, rationale=rationale):
                manifest, page = self.render(color_reports=color_reports, mono=mono,
                                             recommendations=recommendations,
                                             family=family, rationale=rationale)
                parser = visual._SheetParser(); parser.feed(page); parser.close()
                self.assertEqual(visual.SHEET_CSP, parser.csp)
                self.assertEqual("utf-8", parser.charset)
                self.assertEqual(set(self.SECTION_IDS), parser.sections)
                self.assertEqual({"style.css"}, parser.stylesheets)
                self.assertEqual(mono, page.count('<pre class="specimen">'))
                self.assertEqual(2, page.count('<p class="specimen">'))
                self.assertEqual(color_reports * len(palette()), page.count('class="swatch swatch-'))
                # The content really reached the page, escaped and otherwise
                # verbatim - so the leg measures a round trip of that text and
                # not merely that some sheet validated.
                self.assertIn(html.escape(family), page)
                if rationale is not None:
                    self.assertEqual(recommendations, page.count(html.escape(rationale)))
                if recommendations:
                    self.assertEqual(recommendations, page.count("Unverbindliche Empfehlung:"))
                    self.assertNotIn("Keine Empfehlung abgegeben", page)
                else:
                    # The one branch of the renderer that nothing had ever rendered.
                    self.assertIn("<li>Keine Empfehlung abgegeben.</li>", page)
                self.assertEqual(2 + 4 + 2 + mono, len({item["path"] for item in manifest["files"]}))
        # And the other direction of the same coupling: text the document form
        # will NOT carry is refused where it enters, by the name of the thing
        # that carries it - not four hundred lines later by the renderer, in a
        # message about the HTML.  Before r23 validate_bundle said "ok" and
        # render-sheet then died with "sheet document form: forbidden control
        # character", which named the page while the cause sat in a font file
        # or in a sentence: a valid bundle made unrenderable, and the refusal
        # attributed to the wrong artifact.
        # Both boundaries of every run in _SHEET_CONTROL, on the channel that
        # costs nothing to vary; the four corners on the channel that has to
        # build a font for each one, because there the point is the CHANNEL -
        # the name table of an owner-supplied file, read back by
        # extract_font_metadata - and the predicate is the same object.
        for mark in ("\x00", "\x08", "\x0b", "\x0e", "\x1f", "\x7f"):
            with self.subTest(rationale_control=mark), \
                 self.assertRaisesRegex(visual.VisualDecisionError,
                                        r"recommendation rationale color-a contains a forbidden control character"):
                self.render(recommendations=1, rationale=f"Beste{mark}Lesbarkeit")
        for mark in ("\x00", "\x0b", "\x1b", "\x7f"):
            with self.subTest(family_control=mark), \
                 self.assertRaisesRegex(visual.VisualDecisionError,
                                        r"font family serif-a contains a forbidden control character"):
                self.render(family=f"Fixture{mark}Serif")
        # The mono dimension used to be a coupling: one font report shared by
        # typography and code font, which holds exactly three fonts, so the
        # code-font grid carried exactly one card in every renderable bundle.
        # Since the render-sheet fix every code-font candidate binds a report of
        # its own, and the grid carries one card per candidate - none, one or
        # several.  A report still holds exactly three fonts; that is measured
        # rather than assumed, and so is the grid over the counts it now spans.
        schema = json.loads((visual.SCHEMA_DIR / visual.SCHEMAS["font"]).read_bytes())
        self.assertEqual((3, 3), (schema["properties"]["candidates"]["minItems"],
                                  schema["properties"]["candidates"]["maxItems"]))
        for mono in (0, 2, 3):
            with self.subTest(code_font_candidates=mono):
                manifest, page = self.render(mono=mono)
                self.assertEqual(mono, page.count('<pre class="specimen">'))
                self.assertEqual(2 + 4 + 2 + mono, len({item["path"] for item in manifest["files"]}))
        # A logo report still covers all four variants; a bundle without logo
        # candidates renders an open logo section instead (see
        # VisualDecisionRenderSheetFixTests).
        logo_schema = json.loads((visual.SCHEMA_DIR / visual.SCHEMAS["logo"]).read_bytes())
        self.assertEqual(4, logo_schema["properties"]["candidates"]["minItems"])


class VisualDecisionCoreR21Tests(TimeLimitedTestCase):
    """Every decoded member held to the bytes it is decoded from.

    r19 bound ``index.html`` to the encoding this module decodes it with, and
    left the CSS members that are decoded on the very same line unbound.  r20
    measured what that costs, in Blink, on both delivery paths:

        style.css:  @charset "iso-2022-jp";<ESC>$B/*<ESC>(B{}
                    body{background-image:url(offmanifest.svg)}

    To this scanner one comment running to end of file - ``_scan_sheet_css``
    returned the empty set, so the sheet was ACCEPTED.  To Blink a dead rule and
    a live ``body`` rule, whose ``url()`` it fetched: the access log of the
    sheet's own origin carried ``GET /offmanifest.svg``, a file that is not in
    the manifest, and ``SHEET_CSP`` never applied because ``img-src 'self'``
    allows exactly that same-origin resource.  The same bytes without the
    declaration are the comment the scanner reads them as - the declaration
    alone was the pivot.

    The defect is not the line, it is the class: an encoding *assumption* is
    only safe where the format cannot say otherwise, or where the bytes are held
    to saying exactly this.  This module decodes at five sites, in three
    formats, and this class states the answer at every one of them - the two
    that carry no declaration at all as explicitly as the three that do.
    """

    SECTION_IDS = ("candidates-color", "candidates-typography", "candidates-code-font",
                   "candidates-logo", "candidates-lockup", "recommendations", "owner-review")
    SECTIONS = "".join(f'<section id="{name}"><h2>x</h2></section>' for name in SECTION_IDS)
    LINK = '<link rel="stylesheet" href="style.css">'
    LOCAL_CSS = b'@font-face{src:url("assets/a.svg")}'
    #: r20's counter-example, byte for byte.
    JIS_PIVOT = (b'@charset "iso-2022-jp";\x1b$B/*\x1b(B{}'
                 b'body{background-image:url(offmanifest.svg)}')
    #: The same bytes with the declaration removed - the isolating control.
    JIS_BODY = JIS_PIVOT.split(b";", 1)[1]
    SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><title>logo-a</title>'
           b'<rect x="2" y="2" width="60" height="60" fill="#111111"/></svg>')
    #: The byte order marks of every Unicode encoding form, UTF-32 before the
    #: UTF-16 marks its little-endian form opens with.
    MARKS = (b"\xef\xbb\xbf", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00", b"\xfe\xff", b"\xff\xfe")

    def page(self, *, head: str | None = None, body: str | None = None,
             charset: str = '<meta charset="utf-8">') -> str:
        meta = f'<meta http-equiv="Content-Security-Policy" content="{visual.SHEET_CSP}">'
        head = meta + self.LINK if head is None else head
        body = "<main>" + self.SECTIONS + "</main>" if body is None else body
        return ('<!doctype html><html lang="de"><head>' + charset + head
                + "<title>x</title></head><body>" + body + "</body></html>")

    def sheet(self):
        """A callable that validates one bundle, member by member, from raw bytes."""
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve() / "sheet"; (root / "assets").mkdir(parents=True)

        def raw(value: bytes | str) -> bytes:
            return value if isinstance(value, bytes) else value.encode("utf-8")

        def check(*, page: str | None = None, css: bytes | str | None = None,
                  members: dict[str, bytes | str] | None = None,
                  unlisted: dict[str, bytes | str] | None = None) -> None:
            files = {"index.html": raw(self.page() if page is None else page),
                     "style.css": raw(self.LOCAL_CSS if css is None else css),
                     "assets/a.svg": b"svg"}
            files.update({name: raw(value) for name, value in (members or {}).items()})
            # An "unlisted" file is on disk and NOT in the manifest: it is the
            # resource the r20 payload made Blink fetch, and the closed manifest
            # is the promise it breaks.
            for name, data in list(files.items()) + [(n, raw(v)) for n, v in (unlisted or {}).items()]:
                target = root / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
            manifest = {"schema_version": "brandkit-visual-sheet-manifest.v1", "brief_sha256": "a" * 64,
                        "candidate_set_sha256": "b" * 64, "affinity_observation_sha256": "c" * 64,
                        "files": [{"path": name, "sha256": visual.sha256_bytes(data)} for name, data in files.items()],
                        "viewports": [320, 768, 1440], "offline": True, "interactive_controls": False,
                        "preselection": False, "normative": False, "human_review_status": "required"}
            with deadline(60, "validate_sheet_manifest"):
                visual.validate_sheet_manifest(manifest, root)
        return check

    # --- C1-r20: the stylesheet's own encoding rule ---------------------------

    def test_a_stylesheet_declares_exactly_the_encoding_it_is_validated_as(self):
        check = self.sheet()
        other = "declares an encoding other than utf-8"
        # The positive controls first, so the rule cannot become a wall: this
        # module's own stylesheets carry no @charset at all, and every rule that
        # agrees with the decode stays legal.
        check()
        check(css=b'@charset "utf-8";' + self.LOCAL_CSS)
        # r22 measured the label half in Blink: `UTF-8` and `utf8` are read
        # exactly as `utf-8` is - the rules apply, the outline is 2px on both
        # delivery paths - and r21 refused them.  That was a false rejection of
        # legitimate CSS, not a hardening.  css-syntax-3 3.2 does not compare the
        # label byte for byte; it hands it to the Encoding Standard's "get an
        # encoding", which strips ASCII whitespace, ASCII-lowercases, and looks
        # the result up.  Every label that comes out utf-8 is therefore the same
        # statement, and each one below is accepted here and measured in Blink.
        for label in (b"utf-8", b"UTF-8", b"Utf-8", b"utf8", b"UTF8",
                      b"unicode-1-1-utf-8", b"UNICODE-1-1-UTF-8", b"unicode11utf8",
                      b"unicode20utf8", b"x-unicode20utf8", b" utf-8", b"utf-8 ",
                      b"\tutf-8\t", b" \tutf-8\t "):
            with self.subTest(accepted=label):
                check(css=b'@charset "' + label + b'";' + self.LOCAL_CSS)
        # Spelled out rather than read back off the module, so both sets are a
        # statement of this test and not a copy of the thing they check.
        self.assertEqual({b"unicode-1-1-utf-8", b"unicode11utf8", b"unicode20utf8",
                          b"utf-8", b"utf8", b"x-unicode20utf8"}, set(visual._UTF8_LABELS))
        self.assertEqual(b" \t", visual._CSS_LABEL_PADDING)
        # The padding stops where the CSS string does.  "Get an encoding" strips
        # five ASCII whitespace bytes, but css-syntax-3 preprocesses CR and FF
        # into a newline and a newline inside a string ends it as a BAD-STRING -
        # so a label padded with one of those three is not a utf-8 declaration,
        # it is a broken stylesheet.  Measured in Chrome 152 on both delivery
        # paths: with space or tab the rules apply; with LF, CR or FF cssRules
        # comes back EMPTY, the whole sheet dead.  Refused, like every other
        # form this module can say nothing about.
        for label in (b"\nutf-8", b"utf-8\n", b"\rutf-8", b"utf-8\r", b"\futf-8", b"utf-8\f"):
            with self.subTest(padding=label), self.assertRaisesRegex(visual.VisualDecisionError, other):
                check(css=b'@charset "' + label + b'";' + self.LOCAL_CSS)
        # r20's counter-example, verbatim and end to end.
        with self.assertRaisesRegex(visual.VisualDecisionError, other):
            check(css=self.JIS_PIVOT, unlisted={"offmanifest.svg": self.SVG})
        # The control that isolates the pivot: strip the declaration and the
        # bytes really are the end-of-file comment this scanner reads - Blink
        # agreed, cssRules was empty.  The second, independent statement takes
        # it anyway, because the ESC is the transport a stateful encoding needs.
        self.assertEqual(set(), visual._scan_sheet_css("style.css", self.JIS_PIVOT.decode("utf-8")))
        with self.assertRaisesRegex(visual.VisualDecisionError, "forbidden control character"):
            check(css=self.JIS_BODY, unlisted={"offmanifest.svg": self.SVG})
        # Every label that names an encoding other than the one decoded with -
        # stateful, wide, and single-byte alike.
        for label in (b"iso-2022-jp", b"iso-2022-kr", b"utf-16", b"utf-16le", b"utf-16be",
                      b"gb18030", b"gbk", b"big5", b"shift_jis", b"euc-jp", b"euc-kr",
                      b"windows-1252", b"iso-8859-1", b"x-user-defined", b"nonsense", b""):
            with self.subTest(label=label), self.assertRaisesRegex(visual.VisualDecisionError, other):
                check(css=b'@charset "' + label + b'";' + self.LOCAL_CSS)
        # A label that is *close* to utf-8 but is not one of its labels is not
        # the same statement, and stays refused: "get an encoding" is a table
        # lookup, not a prefix match.
        # `\x0b` is the one byte str.strip()/bytes.strip() removes that ASCII
        # whitespace does not, and the Encoding Standard's strip is the ASCII
        # one - so the padding this accepts has to be exactly those five bytes.
        for label in (b"utf-8x", b"xutf-8", b"utf 8", b"utf_8", b"utf-08", b"u\xc2\xa0tf-8",
                      b"unicode-1-1-utf8", b"unicode10utf8", b"utf-8\x00",
                      b"\x0butf-8", b"utf-8\x0b"):
            with self.subTest(near=label), self.assertRaisesRegex(visual.VisualDecisionError, other):
                check(css=b'@charset "' + label + b'";' + self.LOCAL_CSS)
        # And every SPELLING of the rule that is not the byte sequence
        # css-syntax-3 3.2 tells a browser to look for.  This half is a byte
        # comparison in the specification, and r22 measured that Blink treats
        # each of these as inert - no encoding is taken from them at all - so
        # each is refused for being undecidable rather than for being wrong,
        # which is the fail-closed direction: a form this module can say nothing
        # about may not stand in the artifact.  The label above and the spelling
        # here are deliberately not the same rule.
        for rule in (b'@CHARSET "utf-8";', b'@Charset "utf-8";', b'@charsett "utf-8";',
                     b'@charset  "utf-8";', b'@charset"utf-8";',
                     b'@charset\t"utf-8";', b'@charset\n"utf-8";',
                     b"@charset 'utf-8';", b'@charset "utf-8"', b'@charset "utf-8" ;',
                     b'@charset "utf-8",', b'@charset utf-8;'):
            with self.subTest(rule=rule), self.assertRaisesRegex(visual.VisualDecisionError, other):
                check(css=rule + self.LOCAL_CSS)
        # Not at offset zero is inert in every engine measured, and is refused
        # all the same: pinning the text to the ONE place it may occur is what
        # keeps a later placement quirk of any fallback algorithm from putting a
        # second reading back.  A generated artifact pays nothing for it.
        # Stated with the *right* label as well as with a wrong one: a reading
        # that finds the rule anywhere instead of at offset zero would still
        # refuse the iso-2022-jp lines below - for the label, not the place -
        # so the place is only measured where the label is beyond reproach.
        for css in (b' @charset "iso-2022-jp";' + self.LOCAL_CSS,
                    b'\n@charset "iso-2022-jp";' + self.LOCAL_CSS,
                    self.LOCAL_CSS + b'@charset "iso-2022-jp";',
                    self.LOCAL_CSS + b'/* @charset "iso-2022-jp"; */',
                    b'@charset "utf-8";' + self.LOCAL_CSS + b'@charset "iso-2022-jp";',
                    b' @charset "utf-8";' + self.LOCAL_CSS,
                    b'\n@charset "utf-8";' + self.LOCAL_CSS,
                    b'/* c */@charset "utf-8";' + self.LOCAL_CSS,
                    self.LOCAL_CSS + b'@charset "utf-8";'):
            with self.subTest(css=css), self.assertRaisesRegex(visual.VisualDecisionError, other):
                check(css=css)
        # A byte order mark outranks every declaration in every one of the three
        # formats, so it is refused on the BYTES, before the decode that would
        # otherwise be its only witness - for the document as for a stylesheet.
        # Spelled out here rather than read back from the module: a test that
        # iterates the very tuple it is checking measures nothing at all.
        self.assertEqual(self.MARKS, visual._BYTE_ORDER_MARKS)
        for mark in self.MARKS:
            with self.subTest(mark=mark), self.assertRaisesRegex(visual.VisualDecisionError, "byte order mark"):
                check(css=mark + b'@charset "utf-8";' + self.LOCAL_CSS)
            with self.subTest(mark=mark), self.assertRaisesRegex(visual.VisualDecisionError, "byte order mark"):
                check(page=self.page(), css=self.LOCAL_CSS, members={"index.html": mark + self.page().encode("utf-8")})
        # The rule is about a CSS *member*, not about the file called
        # style.css - the very generalisation r15 made for the scan itself.
        second = self.page(head=f'<meta http-equiv="Content-Security-Policy" content="{visual.SHEET_CSP}">'
                                + self.LINK + '<link rel="stylesheet" href="assets/b.css">')
        check(page=second, members={"assets/b.css": b"a{color:#000}"})
        with self.assertRaisesRegex(visual.VisualDecisionError, other + ": assets/b.css"):
            check(page=second, members={"assets/b.css": self.JIS_PIVOT},
                  unlisted={"offmanifest.svg": self.SVG})

    def test_a_control_character_is_refused_in_a_stylesheet_as_it_is_in_the_document(self):
        check = self.sheet()
        forbidden = "forbidden control character"
        # The three ASCII whitespace characters a stylesheet may still carry, and
        # which the tokenizer of this module and of the browser both skip.
        check(css=b'@font-face{\t\n\r src:url("assets/a.svg")}')
        # U+000C is the fourth, and refused since the form-feed round.  What
        # css-syntax-3 says about it does not change and is measured at the
        # scanner primitive - the reader it is about - while the member itself is
        # refused by name.  Both, so neither can be read out of the other.
        self.assertIn("assets/a.svg", visual._scan_sheet_css("style.css", '@font-face{\f src:url("assets/a.svg")}'))
        with self.assertRaisesRegex(visual.VisualDecisionError, forbidden):
            check(css=b'@font-face{\t\n\r\f src:url("assets/a.svg")}')
        for code in (0x00, 0x01, 0x07, 0x08, 0x0b, 0x0c, 0x0e, 0x1b, 0x1f, 0x7f):
            with self.subTest(code=code), self.assertRaisesRegex(visual.VisualDecisionError, forbidden):
                check(css=self.LOCAL_CSS + bytes([code]))
        # U+000B is the single code point that separates the HTML5 ASCII
        # whitespace set from what ``re.ASCII``'s ``\s`` matches, and the
        # comment above _SHEET_MARKUP rests on exactly that difference.  It was
        # the one member of the control class no test had ever named.
        self.assertNotIn("\x0b", visual.ASCII_WHITESPACE)
        self.assertTrue("\x0b".isspace())
        self.assertIsNotNone(re.compile(r"\s", re.ASCII).fullmatch("\x0b"))
        self.assertFalse(visual.ascii_blank("\x0b"))
        self.assertIsNotNone(visual._SHEET_CONTROL.search("\x0b"))
        with self.assertRaisesRegex(visual.VisualDecisionError, forbidden):
            check(page=self.page(body="<main>\x0b" + self.SECTIONS + "</main>"))
        # And the statement is about a CSS *member*, not about style.css.
        second = self.page(head=f'<meta http-equiv="Content-Security-Policy" content="{visual.SHEET_CSP}">'
                                + self.LINK + '<link rel="stylesheet" href="assets/b.css">')
        check(page=second, members={"assets/b.css": b"a{color:#000}"})
        with self.assertRaisesRegex(visual.VisualDecisionError, forbidden + ": assets/b.css"):
            check(page=second, members={"assets/b.css": b"a{color:#000}\x1b"})

    # --- the third format: an SVG asset declares its encoding too --------------

    def test_the_encoding_of_an_svg_asset_is_bound_to_its_own_declaration(self):
        body = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><title>logo-a</title>'
                b'<rect x="2" y="2" width="60" height="60" fill="#111111"/></svg>')
        # ET.fromstring on a *str* decodes nothing, so it silently ignores the
        # declaration that every processor reading the BYTES obeys - Blink
        # included.  Measured on this interpreter: the declaration below parses
        # without a murmur, and in Shift_JIS a lead byte swallows the ASCII byte
        # behind it, so a ">" this module sees can be gone for the renderer.
        import xml.etree.ElementTree as element_tree
        self.assertIsNotNone(element_tree.fromstring('<?xml version="1.0" encoding="shift_jis"?><svg/>'))
        for declaration in (b"", b'<?xml version="1.0"?>', b'<?xml version="1.0" encoding="utf-8"?>',
                            b'<?xml version="1.0" encoding="UTF-8"?>',
                            b"<?xml version='1.0' encoding='Utf-8'?>",
                            b'<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
                            # XML 1.0 2.9/2.8: Eq is `S? '=' S?`, so the spacing
                            # around the equals sign is the author's choice and
                            # says nothing.  A drawing program that writes it
                            # this way declares utf-8 as plainly as one that
                            # does not, and is accepted as plainly.
                            b'<?xml version="1.0" encoding = "utf-8"?>',
                            b'<?xml version="1.0"\tencoding\t=\t"utf-8"?>',
                            b'<?xml version="1.0" encoding\n=\n"utf-8"?>'):
            with self.subTest(declaration=declaration):
                self.assertEqual("0 0 64 64", visual._svg_static_checks(declaration + body, "logo a", "logo-a")["view_box"])
        for declaration in (b'<?xml version="1.0" encoding="shift_jis"?>',
                            b"<?xml version='1.0' encoding='iso-2022-jp'?>",
                            b'<?xml version="1.0" encoding="utf-16"?>',
                            b'<?xml version="1.0" encoding="gb18030"?>',
                            b'<?xml version="1.0" encoding="windows-1252"?>',
                            b'<?xml version="1.0" encoding=""?>',
                            # And the same spacing on the refusing side, which is
                            # the direction that matters: a reader that only
                            # knows `encoding=` sees no declaration here at all
                            # and lets a Shift_JIS SVG through, where a lead byte
                            # swallows the ASCII byte behind it.  Measured: with
                            # the Eq whitespace dropped from _XML_ENCODING these
                            # three are ACCEPTED and the first list still passes.
                            b'<?xml version="1.0" encoding = "shift_jis"?>',
                            b'<?xml version="1.0"\tencoding\t=\t"shift_jis"?>',
                            b'<?xml version="1.0" encoding\n=\n"shift_jis"?>',
                            b"<?xml version='1.0' encoding\r=\r'iso-2022-jp'?>"):
            with self.subTest(declaration=declaration), \
                 self.assertRaisesRegex(visual.VisualDecisionError, "declares a character encoding other than utf-8"):
                visual._svg_static_checks(declaration + body, "logo a", "logo-a")
        # A declaration this module cannot read is refused rather than skipped.
        with self.assertRaisesRegex(visual.VisualDecisionError, "readable XML declaration"):
            visual._svg_static_checks(b'<?xml version="1.0" ' + body, "logo a", "logo-a")
        # An XML processor matches the PI target case-insensitively too, so the
        # probe that decides whether there IS a declaration folds the same way -
        # otherwise this one is refused by expat's reserved-target rule instead,
        # which is a refusal by the wrong gate.
        with self.assertRaisesRegex(visual.VisualDecisionError, "declares a character encoding other than utf-8"):
            visual._svg_static_checks(b'<?XML version="1.0" encoding="shift_jis"?>' + body, "logo a", "logo-a")
        self.assertEqual(self.MARKS, visual._BYTE_ORDER_MARKS)
        for mark in self.MARKS:
            with self.subTest(mark=mark), self.assertRaisesRegex(visual.VisualDecisionError, "byte order mark"):
                visual._svg_static_checks(mark + body, "logo a", "logo-a")

    # --- the two decodes that carry no encoding statement at all ---------------

    def test_the_two_decodes_that_make_no_encoding_assumption_say_why(self):
        # JSON has no in-band declaration (RFC 8259 8.1 fixes UTF-8 and forbids
        # a BOM) and nothing but this module ever reads those bytes, so there is
        # no second reader to disagree with.  Both byte-level alternatives are
        # already a refusal rather than a second reading.
        self.assertEqual({"a": 1}, visual.load_json_bytes(b'{"a":1}', "probe"))
        for data in (b'\xef\xbb\xbf{"a":1}', '{"a":1}'.encode("utf-16"), '{"a":1}'.encode("utf-16be"),
                     '{"a":1}'.encode("utf-32"), b'{"a":\xff}'):
            with self.subTest(data=data), self.assertRaisesRegex(visual.VisualDecisionError, "is not strict JSON"):
                visual.load_json_bytes(data, "probe")
        # The kernel's answer for a stored spelling is decoded with
        # surrogateescape, which is total and byte-reversible: it does not
        # assume an encoding, it declines to.
        for raw in (b"/tmp/x", b"/tmp/\xff\xfe", b"/tmp/\xc3\xa4", b"/tmp/\x80"):
            with self.subTest(raw=raw):
                self.assertEqual(raw, raw.decode("utf-8", "surrogateescape").encode("utf-8", "surrogateescape"))
        # ``bytes.lower()`` folds the 26 ASCII letters and nothing else - the
        # one place in this module where the Python default already IS the
        # specification, which is why the byte-level encoding checks use it
        # while every str-level one uses ascii_lower().
        self.assertEqual(b"\xc3\x84k", b"\xc3\x84K".lower())
        self.assertNotEqual("\xc4k", "\xc4K".lower())
        self.assertEqual("\xc4k", visual.ascii_lower("\xc4K"))
        # The sixth site, and the answer it gives: a google/fonts METADATA.pb.
        # Protocol buffers text format is UTF-8 by definition and carries no
        # declaration of its own, and nothing but this module reads the bytes a
        # report binds - the JSON case again.  Both byte-level alternatives are a
        # refusal rather than a second reading: a BOM is no token of the format,
        # and bytes that are not UTF-8 are no text-format message at all.
        self.assertEqual({"name": ['"A"']}, visual._protobuf_text_fields(b'name: "A"', "probe"))
        for data, message in ((b'\xef\xbb\xbfname: "A"', "probe is not protobuf text format at offset 0"),
                              ('name: "A"'.encode("utf-16"), "probe is not UTF-8"), (b'name: "\xff"', "probe is not UTF-8")):
            with self.subTest(data=data), self.assertRaisesRegex(visual.VisualDecisionError, message):
                visual._protobuf_text_fields(data, "probe")
        # And the list above is the WHOLE list: a seventh decode site added later
        # has to come back to it and say which of the two answers it gives.
        source = Path(visual.__file__).read_text(encoding="utf-8").splitlines()
        decodes = [line for line in source if ".decode(" in line and not line.lstrip().startswith("#")]
        self.assertEqual(6, len(decodes), decodes)

    # --- the hardening r20 named: two gates on a URL, not one ------------------

    def test_a_url_scheme_is_matched_the_way_a_url_parser_matches_it(self):
        check = self.sheet()
        remote = "sheet contains a remote or unsafe URL"
        # A scheme is ASCII case-insensitive (RFC 3986 3.1) and a browser strips
        # ASCII whitespace out of a URL before it looks at one.  With neither
        # fold, "HTTPS:" and " javascript:" walked past this gate and were
        # caught only by the closed-manifest equality - one gate instead of two,
        # and no test told the two messages apart.
        for target in ("http://evil.invalid/x", "HTTP://evil.invalid/x", "HTTPS://evil.invalid/x",
                       "Http://evil.invalid/x", "javascript:alert(1)", "JAVASCRIPT:alert(1)",
                       "JavaScript:alert(1)", "data:text/html,x", "DATA:text/html,x",
                       "//evil.invalid/x", "/etc/passwd", "../a.svg",
                       " https://evil.invalid/x", "https://evil.invalid/x ",
                       "htt\tps://evil.invalid/x", "ja\nvascript:alert(1)",
                       "HTT\rPS://evil.invalid/x", " /etc/passwd", " ../a.svg"):
            with self.subTest(target=target), self.assertRaisesRegex(visual.VisualDecisionError, remote):
                check(page=self.page(body=f'<main><img src="{target}" alt="">' + self.SECTIONS + "</main>"))
        # The second gate, named separately, so that "two gates" is a measured
        # statement and not a hope: a local reference that is not in the
        # manifest is refused by the equality, with the other message.
        with self.assertRaisesRegex(visual.VisualDecisionError, "asset references differ"):
            check(page=self.page(body='<main><img src="assets/b.svg" alt="">' + self.SECTIONS + "</main>"))
        check(page=self.page(body='<main><img src="assets/a.svg" alt="">' + self.SECTIONS + "</main>"))

    # --- the surviving mutant r20 called a real test gap ------------------------

    def test_the_function_allowlist_reads_the_decoded_pass_as_well_as_the_raw_one(self):
        check = self.sheet()
        # The decoded pass sees function names the raw pass cannot: an escaped
        # "(" is content to the tokenizer and a delimiter once decoded.  The
        # reference half of that union was pinned in r20; the function half was
        # not, and dropping it left the scan accepting this sheet.
        payload = r'b{background:foo\28 assets/a.svg\29 }'
        self.assertEqual(set(), visual._css_resource_references(payload)[1])
        self.assertEqual({"foo"}, visual._css_resource_references(visual._decoded_canonical_css(payload))[1])
        with self.assertRaisesRegex(visual.VisualDecisionError, r"unsupported function: \['foo'\]"):
            check(css=(payload + self.LOCAL_CSS.decode("utf-8")).encode("utf-8"))
        # And the raw half, so neither pass can be dropped: an unescaped call is
        # seen by both, and the functions this module really emits stay legal.
        with self.assertRaisesRegex(visual.VisualDecisionError, r"unsupported function: \['foo'\]"):
            check(css=b"b{background:foo(#000)}" + self.LOCAL_CSS)
        check(css=b'b{width:calc(min(1rem,2rem));background:linear-gradient(#000,#fff)}' + self.LOCAL_CSS)



def font_candidates(root: Path, plan: list[tuple[str, str, str]], *, weights: dict[str, tuple[int, int]] | None = None,
                    x_heights: dict[str, int] | None = None, numerals: dict[str, str] | None = None,
                    ink_heights: dict[str, tuple[int, int]] | None = None,
                    verticals: dict[str, tuple[int, int]] | None = None) -> list[dict]:
    """The three font candidates a report needs, written into ``root`` as real files.

    ``weights`` gives named fonts a weight range of their own (see
    ``weighted_font``); a font it does not name is built exactly as before.  The
    two builders are separate rather than one with a default, because the shared
    cache of the plain one is what keeps a twelve-candidate bundle cheap.
    """
    license_binding = write(root / "OFL.txt", "fixture license"); revision = "1" * 40
    candidates = []
    for classification, family, font_id, *shape in plan:
        wanted = (weights or {}).get(font_id)
        height = (x_heights or {}).get(font_id)
        digits = (numerals or {}).get(font_id)
        drawn = (ink_heights or {}).get(font_id)
        upright = (verticals or {}).get(font_id)
        if drawn is not None:
            # A face whose OUTLINE carries the height and whose metric states it,
            # so "every candidate draws the same x-height" is measurable at all.
            asset, source = specimen_font(root / f"{font_id}.woff2", family, classification,
                                          x_top=drawn[0], weights=wanted, rise=drawn[1], vertical=upright)
        elif height is not None or digits or upright is not None:
            asset, source = x_height_font(root / f"{font_id}.woff2", family, classification, height or 700,
                                          numerals=digits or "", weights=wanted, vertical=upright)
        elif wanted is None:
            asset, source = cached_font(root / f"{font_id}.woff2", family, classification, *shape)
        else:
            asset, source = weighted_font(root / f"{font_id}.woff2", family, classification, wanted)
        source_binding = {"path": f"{font_id}.ttf", "sha256": visual.sha256_bytes(source)}
        rights = {"schema_version": "rights-evidence.v1", "issuer": "Fixture", "source": "https://example.invalid/fonts",
                  "scope": "single-asset", "asset_id": font_id, "license_id": "OFL-1.1", "status": "open-license",
                  "version": "1.0", "date": "2026-09-03", "todo": None, "source_revision": revision,
                  "source_path": source_binding["path"], "source_sha256": source_binding["sha256"],
                  "license_evidence_path": "OFL.txt", "license_evidence_sha256": license_binding["sha256"],
                  "transformation": "Deterministic test conversion to WOFF2."}
        candidates.append({"id": font_id, "classification": classification, "family": family,
                           "specimen": visual.SPECIMEN, "specimen_sha256": visual.SPECIMEN_SHA256,
                           "asset": {"path": f"{font_id}.woff2", "sha256": visual.sha256_bytes(asset)},
                           "metadata": {"license_spdx": "OFL-1.1",
                                        "source_pin": {"origin": "https://example.invalid/fonts", "revision": revision, **source_binding},
                                        "license_evidence": license_binding, "extracted": cached_metadata(asset)},
                           "rights": canonical_write(root, f"{font_id}.rights.json", rights),
                           "objective_checks": {"woff2-valid": True, "asset-hash-valid": True, "rights-closed": True,
                                                "pin-valid": True, "specimen-identical": True, "metadata-matches": True,
                                                "glyph-coverage": True, "embedding-allowed": True},
                           "human_reviews": {name: "required" for name in ("readability", "tracking", "digits", "german-glyphs", "code-punctuation", "fallback", "reflow-200")}})
    return candidates


def logo_candidates(root: Path, variants: tuple[str, ...]) -> list[dict]:
    """One logo report's candidates, in the order ``variants`` names them."""
    candidates = []
    for variant in variants:
        ident = variant.replace("lockup-", "") + "-a"; asset_id = "fixture-" + ident
        raw = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><title>{asset_id}</title><rect x="2" y="2" width="60" height="60" fill="#111111"/></svg>'.encode()
        (root / f"{ident}.svg").write_bytes(raw)
        candidates.append({"id": ident, "asset_id": asset_id, "variant": variant,
                           "asset": {"path": f"{ident}.svg", "sha256": visual.sha256_bytes(raw)},
                           "creation_origin": "owner-supplied", "minimum_width_px": 16,
                           "test_sizes_px": [16, 24, 32, 48, 64, 128],
                           "objective_checks": visual._svg_static_checks(raw, ident, asset_id),
                           "human_reviews": {name: "required" for name in ("size-16", "size-24", "size-32", "size-48", "size-64", "size-128", "minimum-width", "monochrome", "clipping", "legibility")}})
    return candidates


def five_domain_bundle(project: Path, root: Path, now: int, *, run_id: str = "run-a", drop: str | None = None,
                       split: str | None = None, recommendations: list[dict] | None = None,
                       blocked: bool = False, declare: dict[str, str] | None = None) -> dict:
    """A complete bundle that routes all five decision domains, on disk.

    ``color_bundle`` and ``affinity_bundle`` each build the slice their own
    acceptance point needs; neither reaches the public ``render_sheet``, which
    refuses anything that is not exactly one candidate per domain with the font
    and the logo report each shared by its two domains.  This builds that shape
    once so the public entry point can be exercised at all.

    The clock is the caller's: ``_render_sheet`` re-validates the logo report
    without a clock of its own, so the observation window has to contain the
    live instant as well as the ``now`` handed to ``validate_bundle``.

    ``drop`` and ``split`` build the two shapes the sheet's routing guards
    refuse.  They are parameters of the builder rather than edits to a finished
    bundle because every report binds ``candidate_context_sha256``: touching the
    candidate set afterwards would be refused one rule earlier, by the binding
    instead of by the guard under test.

    ``blocked`` builds the one shape the core *accepts* while returning
    blockers: the shared font report carries a human ``fail``, so typography and
    code-font are ``status: "blocked"`` candidates and land in
    ``validate_bundle``'s ``blockers`` list.  It is the only way to reach the
    adapter's own blocker gate with a real core, and for the same reason as
    above it is decided before the reports are written.

    ``declare`` overrides the *declared* ``status`` of named domains, and is the
    only way to build a candidate set whose declaration disagrees with what its
    own reports say.  It has to be a builder parameter for the same reason
    ``blocked`` does: ``candidate_context_sha256`` projects ``status``, so
    editing it on a finished bundle is refused one rule earlier, by the report's
    candidate-set binding, and never reaches the eligibility rule under test.
    """
    handoff = write(root / "interview-handoff.json", "{}"); source_path = visual.ROOT / "visual-decision-sources.v1.json"
    brief = {"schema_version": "brandkit-visual-brief.v1", "brief_id": "brief-a", "project_identity_sha256": visual.project_identity(project),
             "run_id": run_id, "brand_id": "brand-a", "interview_handoff": handoff,
             "open_decisions": ["color", "typography", "code-font", "logo", "lockup"], "locale": "de-DE",
             "viewports": [320, 768, 1440], "offline": True, "normative": False}
    canonical_write(root, "visual-brief.json", brief)
    observation = {**unavailable(project, run_id), "observed_at": now - 10, "expires_at": now + 600}
    canonical_write(root, "affinity-observation.json", observation)
    placeholder = {"sha256": "0" * 64}
    values = {
        "color": {"domain": "color", "roles": palette(), "report": {"path": "color-report.json", **placeholder}},
        "typography": {"domain": "typography", "font_candidate_ids": ["serif-a", "sans-a"], "specimen_sha256": visual.SPECIMEN_SHA256, "report": {"path": "font-report.json", **placeholder}},
        "code-font": {"domain": "code-font", "code_font_id": "mono-a", "specimen_sha256": visual.SPECIMEN_SHA256, "report": {"path": "font-report.json", **placeholder}},
        "logo": {"domain": "logo", "logo_candidate_ids": ["mark-a", "favicon-a"], "creation_origin": "owner-supplied", "report": {"path": "logo-report.json", **placeholder}},
        "lockup": {"domain": "lockup", "lockup_candidate_ids": ["horizontal-a", "stacked-a"], "creation_origin": "owner-supplied", "report": {"path": "logo-report.json", **placeholder}},
    }
    if split == "code-font":
        values["code-font"]["report"] = {"path": "font-report-b.json", **placeholder}
    if split == "lockup":
        values["lockup"]["report"] = {"path": "logo-report-b.json", **placeholder}
    candidate_set = {"schema_version": "brandkit-visual-candidate-set.v1", "brief_sha256": visual.sha256_bytes(visual.canonical_json(brief)),
                     "source_review": {"path": source_path.name, "sha256": visual.sha256_bytes(source_path.read_bytes())}, "status": "complete",
                     "candidates": [{"id": f"{kind}-choice", "kind": kind, "label": f"{kind} A", "value": values[kind],
                                     "value_sha256": visual.sha256_bytes(visual.canonical_json(values[kind])), "source_ids": ["impeccable"],
                                     "status": (declare or {}).get(kind, "blocked" if blocked and kind in {"typography", "code-font"} else "candidate")}
                                    for kind in ("color", "typography", "code-font", "logo", "lockup") if kind != drop],
                     "recommendations": recommendations or [], "normative": False}
    context = visual.candidate_context_sha256(candidate_set)
    fonts = [("serif", "Fixture Serif", "serif-a"), ("sans", "Fixture Sans", "sans-a"), ("mono", "Fixture Mono", "mono-a")]
    variants = ("mark", "lockup-horizontal", "lockup-stacked", "favicon")

    def font_report(candidates: list[dict]) -> dict:
        if blocked:
            candidates = [{**item, "human_reviews": {**item["human_reviews"], "readability": "fail"}} if index == 0 else item
                          for index, item in enumerate(candidates)]
        return {"schema_version": "brandkit-font-candidate-report.v1", "candidate_set_sha256": context, "specimen_sha256": visual.SPECIMEN_SHA256,
                "candidates": candidates, "objective_status": "pass", "human_review_status": "required"}

    def logo_report(candidates: list[dict]) -> dict:
        return {"schema_version": "brandkit-logo-test-report.v1", "candidate_set_sha256": context,
                "affinity_observation_sha256": visual.sha256_bytes(visual.canonical_json(observation)),
                "candidates": candidates, "objective_status": "pass", "human_review_status": "required"}

    bindings = {
        "color-report.json": canonical_write(root, "color-report.json", visual.color_contrast_report(context, "color-choice", palette(), ["text", "icon"])),
        "font-report.json": canonical_write(root, "font-report.json", font_report(font_candidates(root, fonts))),
        "logo-report.json": canonical_write(root, "logo-report.json", logo_report(logo_candidates(root, variants))),
    }
    # The second copy answers every rule its own domain states, and differs only
    # in the order the shared report lists its candidates - so nothing but the
    # identity guard in render_sheet can tell the two bundles apart.
    if split == "code-font":
        bindings["font-report-b.json"] = canonical_write(root, "font-report-b.json", font_report(font_candidates(root, [fonts[2], fonts[0], fonts[1]])))
    if split == "lockup":
        bindings["logo-report-b.json"] = canonical_write(root, "logo-report-b.json", logo_report(logo_candidates(root, variants[1:3] + variants[:1] + variants[3:])))
    for candidate in candidate_set["candidates"]:
        candidate["value"]["report"] = dict(bindings[candidate["value"]["report"]["path"]])
        candidate["value_sha256"] = visual.sha256_bytes(visual.canonical_json(candidate["value"]))
    canonical_write(root, "candidate-set.json", candidate_set)
    return candidate_set


def five_domain_affinity_bundle(project: Path, root: Path, secrets: Path, now: int, *, run_id: str = "run-a",
                                key: bytes = b"z" * 32, recommendations: list[dict] | None = None) -> dict:
    """All five decision domains at once, with logo and lockup authored by Affinity.

    ``five_domain_bundle`` routes five owner-supplied domains and
    ``affinity_bundle`` routes the two Affinity-authored ones; the owner flow
    needs both in a single bundle, because the interview resolves *every* open
    domain in one selection.  Colour and the two font domains are exactly what
    ``five_domain_bundle`` builds; the logo half - observation, server lock,
    owner approval, mutation receipts and the signed replay ledger - is exactly
    what ``affinity_bundle`` builds.  Nothing is post-edited: every report binds
    ``candidate_context_sha256``, so the shape is decided before the reports are
    written, never afterwards.
    """
    identity = visual.project_identity(project)
    key_path = secrets / "owner.key"; key_path.write_bytes(key); key_path.chmod(0o600)
    genesis_raw = signed_ledger(key, identity, run_id, 0, "0" * 64, [])
    ledger_path = secrets / "replay-ledger.json"; ledger_path.write_bytes(genesis_raw); ledger_path.chmod(0o600)
    handoff = write(root / "interview-handoff.json", "{}"); source_path = visual.ROOT / "visual-decision-sources.v1.json"
    brief = {"schema_version": "brandkit-visual-brief.v1", "brief_id": "brief-a", "project_identity_sha256": identity,
             "run_id": run_id, "brand_id": "brand-a", "interview_handoff": handoff,
             "open_decisions": ["color", "typography", "code-font", "logo", "lockup"], "locale": "de-DE",
             "viewports": [320, 768, 1440], "offline": True, "normative": False}
    canonical_write(root, "visual-brief.json", brief)
    provider = {"server_id": "a", "origin": "https://example.invalid/a", "version": "1",
                "tools": [{"name": "export_svg", "schema_sha256": "1" * 64}]}
    lock = {"schema_version": "brandkit-affinity-mcp-lock.v1", "server_id": "a", "origin": provider["origin"],
            "version": "1", "license_spdx": "MIT", "source_revision": "3" * 40, "source_sha256": "4" * 64, "tools": provider["tools"]}
    lock_binding = canonical_write(root, "affinity.lock.json", lock)
    observation = unavailable(project, run_id, matches=["a"], providers=[provider])
    observation.update(status="available", lock={**lock_binding, "server_id": "a"}, mutation_capable=True, blocker=None,
                       observed_at=now - 10, expires_at=now + 600)
    canonical_write(root, "affinity-observation.json", observation)
    owner = owner_approval(root, "owner.json", key, identity, run_id, "logo-doc")
    write(root / "input.json", "input"); input_binding = {"path": "input.json", "sha256": visual.sha256_bytes(b"input")}
    (root / "staging/logo-doc").mkdir(parents=True)
    report_candidates = []
    for variant in ("mark", "lockup-horizontal", "lockup-stacked", "favicon"):
        ident = variant.replace("lockup-", "") + "-a"; asset_id = "fixture-" + ident
        relative = f"staging/logo-doc/{ident}.svg"
        raw = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><title>{asset_id}</title><rect x="2" y="2" width="60" height="60" fill="#111111"/></svg>'.encode()
        (root / relative).write_bytes(raw)
        report_candidates.append({"id": ident, "asset_id": asset_id, "variant": variant,
                                  "asset": {"path": relative, "sha256": visual.sha256_bytes(raw)},
                                  "creation_origin": "affinity-mcp", "minimum_width_px": 16,
                                  "test_sizes_px": [16, 24, 32, 48, 64, 128],
                                  "objective_checks": visual._svg_static_checks(raw, ident, asset_id),
                                  "human_reviews": {name: "required" for name in ("size-16", "size-24", "size-32", "size-48", "size-64", "size-128", "minimum-width", "monochrome", "clipping", "legibility")}})
    placeholder = {"sha256": "0" * 64}
    values = {
        "color": {"domain": "color", "roles": palette(), "report": {"path": "color-report.json", **placeholder}},
        "typography": {"domain": "typography", "font_candidate_ids": ["serif-a", "sans-a"], "specimen_sha256": visual.SPECIMEN_SHA256, "report": {"path": "font-report.json", **placeholder}},
        "code-font": {"domain": "code-font", "code_font_id": "mono-a", "specimen_sha256": visual.SPECIMEN_SHA256, "report": {"path": "font-report.json", **placeholder}},
        "logo": {"domain": "logo", "logo_candidate_ids": ["mark-a", "favicon-a"], "creation_origin": "affinity-mcp", "report": {"path": "logo-report.json", **placeholder}},
        "lockup": {"domain": "lockup", "lockup_candidate_ids": ["horizontal-a", "stacked-a"], "creation_origin": "affinity-mcp", "report": {"path": "logo-report.json", **placeholder}},
    }
    candidate_set = {"schema_version": "brandkit-visual-candidate-set.v1", "brief_sha256": visual.sha256_bytes(visual.canonical_json(brief)),
                     "source_review": {"path": source_path.name, "sha256": visual.sha256_bytes(source_path.read_bytes())}, "status": "complete",
                     "candidates": [{"id": f"{kind}-choice", "kind": kind, "label": f"{kind} A", "value": values[kind],
                                     "value_sha256": visual.sha256_bytes(visual.canonical_json(values[kind])),
                                     "source_ids": ["impeccable"], "status": "candidate"}
                                    for kind in ("color", "typography", "code-font", "logo", "lockup")],
                     "recommendations": recommendations or [], "normative": False}
    context = visual.candidate_context_sha256(candidate_set)
    bindings = {
        "color-report.json": canonical_write(root, "color-report.json", visual.color_contrast_report(context, "color-choice", palette(), ["text", "icon"])),
        "font-report.json": canonical_write(root, "font-report.json", {
            "schema_version": "brandkit-font-candidate-report.v1", "candidate_set_sha256": context,
            "specimen_sha256": visual.SPECIMEN_SHA256,
            "candidates": font_candidates(root, [("serif", "Fixture Serif", "serif-a"), ("sans", "Fixture Sans", "sans-a"), ("mono", "Fixture Mono", "mono-a")]),
            "objective_status": "pass", "human_review_status": "required"}),
        "logo-report.json": canonical_write(root, "logo-report.json", {
            "schema_version": "brandkit-logo-test-report.v1", "candidate_set_sha256": context,
            "affinity_observation_sha256": visual.sha256_bytes(visual.canonical_json(observation)),
            "candidates": report_candidates, "objective_status": "pass", "human_review_status": "required"}),
    }
    for candidate in candidate_set["candidates"]:
        candidate["value"]["report"] = dict(bindings[candidate["value"]["report"]["path"]])
        candidate["value_sha256"] = visual.sha256_bytes(visual.canonical_json(candidate["value"]))
    canonical_write(root, "candidate-set.json", candidate_set)
    operation_key = domain_key(key, OPERATION_DOMAIN); receipts = {}; nonces = []
    for item in report_candidates:
        nonce = hashlib.sha256(("nonce-" + item["id"]).encode()).hexdigest(); nonces.append(nonce)
        document = {"schema_version": "brandkit-affinity-mutation-receipt.v1", "issuer": "fixture",
                    "key_id": visual.sha256_bytes(operation_key)[:16], "capability": "graphics-mutate",
                    "project_identity_sha256": identity, "run_id": run_id, "document_id": "logo-doc",
                    "staging_root": f".brandkit/work/{run_id}/visual-decisions/staging/logo-doc",
                    "observation_sha256": visual.sha256_bytes(visual.canonical_json(observation)),
                    "server_lock_sha256": lock_binding["sha256"], "owner_approval": owner, "tool": provider["tools"][0],
                    "operation": "export-svg", "inputs": [input_binding],
                    "outputs": [{"candidate_id": item["id"], **item["asset"]}],
                    "issued_at": now - 1, "expires_at": now + 600, "nonce": nonce}
        binding = write_receipt(root, document, operation_key)
        # The adapter's descriptor is closed ``{path, size, sha256}`` - one field
        # stricter than the core, which reads path and digest.  The size is the
        # file's own, so the two descriptions cannot disagree.
        receipts[item["id"]] = {**binding, "size": (root / binding["path"]).stat().st_size}
    return {"candidate_set": candidate_set, "key": key, "key_path": key_path, "ledger_path": ledger_path,
            "observation": observation, "nonces": nonces,
            "auth": {"owner_key_path": str(key_path), "replay_ledger_path": str(ledger_path), "receipts": receipts}}


LETTERS = "abcdefghijklmnopqrstuvwxyz"
ACCENTS = ("#005FCC", "#1B5E20", "#6A1B9A", "#8B2500", "#004D40", "#3E2723", "#1A237E", "#4A148C", "#263238", "#01579B", "#33691E", "#880E4F")


def escape_id(value: str) -> str:
    return html.escape(value)


def lettered_palette(index: int, *, failing: bool = False) -> dict[str, str]:
    roles = {**palette(), "accent": ACCENTS[index % len(ACCENTS)], "focus": ACCENTS[index % len(ACCENTS)]}
    return {**roles, "text": "#EEEEEE"} if failing else roles


def lettered_logo_candidates(root: Path, letter: str, directory: str = "") -> list[dict]:
    """One logo report's four candidates for ``letter``.

    With ``directory`` every report reuses the local ids ``mark-a`` ... in a
    directory of its own, and only the drawing differs - the shape in which
    naming an asset by its candidate id would collide.
    """
    candidates = []
    for variant in ("mark", "lockup-horizontal", "lockup-stacked", "favicon"):
        ident = variant.replace("lockup-", "") + ("-a" if directory else f"-{letter}"); asset_id = f"fixture-{ident}-{letter}"
        relative = f"{directory}{ident}.svg"
        raw = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><title>{asset_id}</title>'
               f'<rect x="2" y="2" width="{60 - LETTERS.index(letter) % 20}" height="60" fill="#111111"/></svg>').encode()
        (root / relative).parent.mkdir(parents=True, exist_ok=True); (root / relative).write_bytes(raw)
        candidates.append({"id": ident, "asset_id": asset_id, "variant": variant,
                           "asset": {"path": relative, "sha256": visual.sha256_bytes(raw)},
                           "creation_origin": "owner-supplied", "minimum_width_px": 16,
                           "test_sizes_px": [16, 24, 32, 48, 64, 128],
                           "objective_checks": visual._svg_static_checks(raw, ident, asset_id),
                           "human_reviews": {name: "required" for name in ("size-16", "size-24", "size-32", "size-48", "size-64", "size-128", "minimum-width", "monochrome", "clipping", "legibility")}})
    return candidates


def multi_domain_bundle(project: Path, root: Path, now: int, counts: dict[str, int], *, run_id: str = "run-a",
                        open_decisions: list[str] | None = None, recommendations: list[dict] | None = None,
                        serif_family: str = "Fixture Serif", mono_families: tuple[str, ...] = (),
                        ligatures: tuple[str | None, ...] = (), distinct_fonts: bool = True,
                        blocked: tuple[str, ...] = (), labels: dict[str, str] | None = None,
                        reuse_logo_ids: bool = False, font_weights: dict[str, tuple[int, int]] | None = None,
                        font_x_heights: dict[str, int] | None = None, font_numerals: dict[str, str] | None = None,
                        font_ink_heights: dict[str, tuple[int, int]] | None = None,
                        font_verticals: dict[str, tuple[int, int]] | None = None) -> dict:
    """Any number of candidates per decision domain, each binding a report of its own, on disk.

    ``five_domain_bundle`` builds the one shape the renderer accepted before the
    render-sheet fix: one candidate per domain, one font report shared by
    typography and code font, one logo report shared by logo and lockup.  This
    builds the shapes the owner asked for - several colour and type candidates,
    none or several code fonts, a bundle without logos - and it builds them as
    ``validate_bundle`` accepts them.

    Candidate ids are ``<kind>-<letter>``.  Typography candidate ``x`` binds
    ``font-report-x.json`` (``serif-x``, ``sans-x``, ``mono-x``); code-font
    candidate ``x`` binds ``code-report-x.json`` (``code-serif-x``,
    ``code-sans-x``, ``code-mono-x``); logo and lockup candidate ``x`` share
    ``logo-report-x.json``.  ``distinct_fonts`` gives the shown fonts of every
    report a family - and bytes - of their own; without it all reports reuse the
    same files, which keeps twelve candidates cheap.  ``ligatures[i]`` is the GSUB
    shape (see make_font) of code-font candidate i's mono, ``mono_families[i]``
    its family.  ``blocked`` names domains whose reports fail: a human ``fail``
    for fonts, an unreadable text colour for colour.
    """
    open_decisions = list(visual.SHEET_DOMAINS) if open_decisions is None else open_decisions
    handoff = write(root / "interview-handoff.json", "{}"); source_path = visual.ROOT / "visual-decision-sources.v1.json"
    brief = {"schema_version": "brandkit-visual-brief.v1", "brief_id": "brief-a", "project_identity_sha256": visual.project_identity(project),
             "run_id": run_id, "brand_id": "brand-a", "interview_handoff": handoff, "open_decisions": open_decisions,
             "locale": "de-DE", "viewports": [320, 768, 1440], "offline": True, "normative": False}
    canonical_write(root, "visual-brief.json", brief)
    observation = {**unavailable(project, run_id), "observed_at": now - 10, "expires_at": now + 600}
    canonical_write(root, "affinity-observation.json", observation)
    placeholder = {"sha256": "0" * 64}
    entries = []
    for kind in visual.SHEET_DOMAINS:
        for index in range(counts.get(kind, 0)):
            letter = LETTERS[index]
            if kind == "color":
                value = {"domain": "color", "roles": lettered_palette(index, failing=kind in blocked), "report": {"path": f"color-report-{letter}.json", **placeholder}}
            elif kind == "typography":
                value = {"domain": "typography", "font_candidate_ids": [f"serif-{letter}", f"sans-{letter}"], "specimen_sha256": visual.SPECIMEN_SHA256,
                         "report": {"path": f"font-report-{letter}.json", **placeholder}}
            elif kind == "code-font":
                value = {"domain": "code-font", "code_font_id": f"code-mono-{letter}", "specimen_sha256": visual.SPECIMEN_SHA256,
                         "report": {"path": f"code-report-{letter}.json", **placeholder}}
            else:
                local = "a" if reuse_logo_ids else letter
                field, identifiers = (("logo_candidate_ids", [f"mark-{local}", f"favicon-{local}"]) if kind == "logo"
                                      else ("lockup_candidate_ids", [f"horizontal-{local}", f"stacked-{local}"]))
                value = {"domain": kind, field: identifiers, "creation_origin": "owner-supplied", "report": {"path": f"logo-report-{letter}.json", **placeholder}}
            entries.append({"id": f"{kind}-{letter}", "kind": kind, "label": (labels or {}).get(f"{kind}-{letter}", f"{kind} {letter.upper()}"),
                            "value": value, "value_sha256": visual.sha256_bytes(visual.canonical_json(value)), "source_ids": ["impeccable"],
                            "status": "blocked" if kind in blocked else "candidate"})
    candidate_set = {"schema_version": "brandkit-visual-candidate-set.v1", "brief_sha256": visual.sha256_bytes(visual.canonical_json(brief)),
                     "source_review": {"path": source_path.name, "sha256": visual.sha256_bytes(source_path.read_bytes())}, "status": "complete",
                     "candidates": entries, "recommendations": recommendations or [], "normative": False}
    context = visual.candidate_context_sha256(candidate_set)

    def font_report(kind: str, plan: list[tuple]) -> dict:
        fonts = font_candidates(root, plan, weights=font_weights, x_heights=font_x_heights, numerals=font_numerals,
                                ink_heights=font_ink_heights, verticals=font_verticals)
        if kind in blocked:
            fonts[0] = {**fonts[0], "human_reviews": {**fonts[0]["human_reviews"], "readability": "fail"}}
        return {"schema_version": "brandkit-font-candidate-report.v1", "candidate_set_sha256": context, "specimen_sha256": visual.SPECIMEN_SHA256,
                "candidates": fonts, "objective_status": "pass", "human_review_status": "required"}

    bindings: dict[str, dict] = {}
    for entry in entries:
        kind, letter = entry["kind"], entry["id"].rsplit("-", 1)[1]
        index, path = LETTERS.index(letter), entry["value"]["report"]["path"]
        if path in bindings:
            continue
        suffix = f" {letter}" if distinct_fonts and index else ""
        if kind == "color":
            report = visual.color_contrast_report(context, entry["id"], entry["value"]["roles"], ["text", "icon"])
        elif kind == "typography":
            report = font_report(kind, [("serif", (serif_family if index == 0 else "Fixture Serif") + suffix, f"serif-{letter}"),
                                        ("sans", "Fixture Sans" + suffix, f"sans-{letter}"), ("mono", "Fixture Mono", f"mono-{letter}")])
        elif kind == "code-font":
            family = mono_families[index] if index < len(mono_families) else "Fixture Mono" + suffix
            shape = ligatures[index] if index < len(ligatures) else None
            report = font_report(kind, [("serif", "Fixture Serif", f"code-serif-{letter}"), ("sans", "Fixture Sans", f"code-sans-{letter}"),
                                        ("mono", family, f"code-mono-{letter}", shape)])
        else:
            report = {"schema_version": "brandkit-logo-test-report.v1", "candidate_set_sha256": context,
                      "affinity_observation_sha256": visual.sha256_bytes(visual.canonical_json(observation)),
                      "candidates": lettered_logo_candidates(root, letter, f"logos-{letter}/" if reuse_logo_ids else ""),
                      "objective_status": "pass", "human_review_status": "required"}
        bindings[path] = canonical_write(root, path, report)
    for entry in entries:
        entry["value"]["report"] = dict(bindings[entry["value"]["report"]["path"]])
        entry["value_sha256"] = visual.sha256_bytes(visual.canonical_json(entry["value"]))
    canonical_write(root, "candidate-set.json", candidate_set)
    return candidate_set


def rebind(root: Path, candidate_set: dict) -> dict:
    """Write ``candidate_set`` and make every report it binds bind it back.

    Any edit to a candidate changes ``candidate_context_sha256``, which every
    report binds; without this an edited candidate is refused one rule too early,
    by the binding, and never reaches the rule a test is about.
    """
    context = visual.candidate_context_sha256(candidate_set); written: dict[str, dict] = {}
    for candidate in candidate_set["candidates"]:
        path = candidate["value"]["report"]["path"]
        if path not in written:
            report = json.loads((root / path).read_bytes()); report["candidate_set_sha256"] = context
            written[path] = canonical_write(root, path, report)
        candidate["value"]["report"] = dict(written[path])
        candidate["value_sha256"] = visual.sha256_bytes(visual.canonical_json(candidate["value"]))
    canonical_write(root, "candidate-set.json", candidate_set)
    return candidate_set


class PageTree(HTMLParser):
    """A rendered page as a tree, for tests that state what the owner sees."""

    VOID = frozenset({"meta", "link", "img"})

    def __init__(self, page: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root: dict = {"tag": "#document", "attrs": {}, "children": []}
        self._stack = [self.root]
        self.feed(page); self.close()

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        self._stack[-1]["children"].append(node)
        if tag not in self.VOID:
            self._stack.append(node)

    def handle_endtag(self, tag):
        self._stack.pop()

    def handle_data(self, data):
        self._stack[-1]["children"].append(data)


def nodes(node: dict, tag: str | None = None, css: str | None = None):
    for child in node["children"]:
        if isinstance(child, dict):
            if (tag is None or child["tag"] == tag) and (css is None or css in child["attrs"].get("class", "").split()):
                yield child
            yield from nodes(child, tag, css)


def text_of(node: dict) -> str:
    return "".join(child if isinstance(child, str) else text_of(child) for child in node["children"])


def elements(node: dict) -> list[dict]:
    return [child for child in node["children"] if isinstance(child, dict)]


def sheet_faces(css: str) -> dict[str, tuple[str, str]]:
    """``face-N`` -> (asset digest, generic fallback) as the sheet's stylesheet declares it."""
    return {f"face-{match.group(1)}": (match.group(2), match.group(3)) for match in re.finditer(
        # The descriptor is required, not tolerated: a face without its weight
        # range lets the browser synthesise every weight the page asks for, so a
        # sheet that lost it again would fail here rather than read as no face.
        # The same holds for the optical-size switch: without it the browser picks
        # an instance out of the font SIZE, which is the very thing this sheet
        # normalises, so a face that lost it reads as no face here either.
        r'@font-face\{font-family:"VisualCandidate(\d+)";src:url\("assets/([0-9a-f]{64})\.woff2"\) format\("woff2"\);font-display:block;font-weight:[0-9]+(?: [0-9]+)?\} '
        r'\.face-\1,\.face-\1 pre\{font-family:"VisualCandidate\1",([a-z-]+);font-optical-sizing:none\}', css)}


class VisualDecisionCoreR25Tests(TimeLimitedTestCase):
    """The four nacharbeiten the final core review named, measured rather than argued.

    r24 passed the core and listed five test gaps without a defect behind any of
    them.  Three of the five sit on the same two lines of ``render_sheet`` and on
    the closing line of ``_render_sheet``: the public entry point had no test at
    all, so its two runtime guards and the manifest round trip the r23 test
    explicitly leans on were carried by argument only.  The fourth is the reach
    of the control-character scan, and the fifth is the strictness the XML label
    binder deliberately keeps.  All four are pinned here.
    """

    def bundle(self, **shape) -> tuple[Path, Path, int]:
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve(); root = visual.visual_workspace(project, "run-a", create=True)
        now = int(time.time()); five_domain_bundle(project, root, now, **shape)
        return project, root, now

    def test_the_public_renderer_renders_the_bundle_its_own_validator_accepts(self):
        project, root, now = self.bundle()
        self.assertEqual(["code-font-choice", "color-choice", "lockup-choice", "logo-choice", "typography-choice"],
                         sorted(item["id"] for item in visual.validate_bundle(project, "run-a", now)["eligible_candidates"]))
        with deadline(150, "render_sheet"):
            manifest = visual.render_sheet(project, "run-a", now)
        # index.html + style.css + four logo assets + three font assets.
        self.assertEqual(9, len({item["path"] for item in manifest["files"]}))
        self.assertEqual(visual.sha256_bytes((root / "candidate-set.json").read_bytes()), manifest["candidate_set_sha256"])
        page = (root / "sheet" / "index.html").read_text(encoding="utf-8")
        for section in ("candidates-color", "candidates-typography", "candidates-code-font", "candidates-logo", "candidates-lockup"):
            self.assertIn(f'id="{section}"', page)
        # The manifest on disk is the manifest that was returned, and it still
        # passes the check the renderer ends in when it is re-read from there.
        self.assertEqual(manifest, json.loads((root / "sheet" / "manifest.json").read_bytes()))
        visual.validate_sheet_manifest(manifest, root / "sheet")

    def test_a_domain_the_bundle_does_not_route_is_rendered_open_rather_than_refused(self):
        # Until the render-sheet fix this was a refusal - "sheet requires all
        # five independently routed decision domains" - which was the renderer's
        # limit and not a rule of the bundle: validate_bundle accepts each shape
        # below, and a bundle without logos is the case the playbook describes.
        for kind in ("typography", "code-font", "logo", "lockup"):
            with self.subTest(missing=kind):
                project, root, now = self.bundle(drop=kind)
                self.assertEqual(4, len(visual.validate_bundle(project, "run-a", now)["eligible_candidates"]))
                with deadline(150, "render_sheet"):
                    manifest = visual.render_sheet(project, "run-a", now)
                page = (root / "sheet" / "index.html").read_text(encoding="utf-8")
                section = page.split(f'<section id="candidates-{kind}">', 1)[1].split("</section>", 1)[0]
                self.assertNotIn("<article", section)
                self.assertNotIn("<img", section)
                self.assertIn("Affinity MCP ist unavailable" if kind in {"logo", "lockup"} else "keine Kandidaten", section)
                self.assertFalse(manifest["preselection"])

    def test_two_domains_may_route_one_shared_report_or_two_separate_ones(self):
        # The other former guard ("shared comparison reports differ across
        # independent domains").  A code font or a lockup that binds its own copy
        # of the report renders exactly like the shared case: each card reads the
        # report its own candidate binds, and both reports are validated.
        for kind in ("code-font", "lockup"):
            with self.subTest(rebound=kind):
                project, root, now = self.bundle(split=kind)
                self.assertEqual(5, len(visual.validate_bundle(project, "run-a", now)["eligible_candidates"]))
                with deadline(150, "render_sheet"):
                    manifest = visual.render_sheet(project, "run-a", now)
                page = (root / "sheet" / "index.html").read_text(encoding="utf-8")
                self.assertEqual(1, page.count("<p>mono · Fixture Mono</p>"))
                self.assertEqual(9, len({item["path"] for item in manifest["files"]}))

    def test_the_renderer_ends_in_the_manifest_check_the_round_trip_assumes(self):
        # r23's document-form matrix is only a round trip because _render_sheet
        # closes with validate_sheet_manifest over what it just wrote.  Removing
        # that line left every one of those tests green, so the assumption is
        # stated here directly: exactly one call, after the manifest is on disk,
        # over the object that is returned - and a refusal there is the caller's
        # refusal, not something the renderer swallows.
        project, root, now = self.bundle()
        calls: list[tuple[dict, Path, bool]] = []
        real = visual.validate_sheet_manifest

        def spy(manifest, output):
            calls.append((copy.deepcopy(manifest), Path(output), (Path(output) / "manifest.json").is_file()))
            return real(manifest, output)

        visual.validate_sheet_manifest = spy
        try:
            with deadline(150, "render_sheet"):
                manifest = visual.render_sheet(project, "run-a", now)
        finally:
            visual.validate_sheet_manifest = real
        self.assertEqual(1, len(calls))
        self.assertEqual((manifest, root / "sheet", True), calls[0])

        sentinel = visual.VisualDecisionError("manifest check reached")

        def refuse(manifest, output):
            raise sentinel

        shutil.rmtree(root / "sheet")
        visual.validate_sheet_manifest = refuse
        try:
            with self.assertRaises(visual.VisualDecisionError) as raised:
                visual.render_sheet(project, "run-a", now)
        finally:
            visual.validate_sheet_manifest = real
        self.assertIs(sentinel, raised.exception)

    def test_the_control_scan_reads_the_whole_string_and_not_only_its_opening(self):
        # The suite put every control character at position 5 or 7, so a scan
        # bounded to the first 64 characters was indistinguishable from the one
        # that is written.  The predicate is the same object both call sites use.
        for offset in (0, 1, 5, 63, 64, 65, 200, 900):
            with self.subTest(offset=offset):
                visual._reject_sheet_control("x" * offset + "y", "probe")
                with self.assertRaisesRegex(visual.VisualDecisionError, "probe contains a forbidden control character"):
                    visual._reject_sheet_control("x" * offset + "\x00", "probe")
        # And once through the production channel, far past any such bound.
        project, _root, now = self.bundle(recommendations=[
            {"kind": "color", "candidate_id": "color-choice", "rationale": "Lesbarkeit " + "a" * 900 + "\x1b", "source_ids": ["impeccable"]}])
        with self.assertRaisesRegex(visual.VisualDecisionError, "recommendation rationale color-choice contains a forbidden control character"):
            visual.validate_bundle(project, "run-a", now)

    def test_the_xml_label_binder_keeps_exactly_one_spelling_on_purpose(self):
        """The asymmetry r24 named, decided and pinned rather than left implicit.

        The CSS binder takes six labels because the WHATWG Encoding Standard
        *defines* those six strings as labels of utf-8: accepting them is
        conformance with the specification that decides how Blink decodes a
        stylesheet.  XML 1.0 4.3.3 asks a different question - it requires the
        ``EncName`` to be a name or alias registered with IANA - and ``utf8``,
        ``UTF_8``, ``unicode20utf8`` and ``unicode-1-1-utf-8`` are none of those.
        ``us-ascii`` *is* registered, and names a different encoding: accepting
        it would mean accepting a declaration that is false about the bytes as
        soon as one of them is non-ASCII.  Widening here would therefore be a
        promise about one browser's error recovery with no standard behind it,
        while widening the CSS side was a promise about a standard.  The
        decision is to keep the single spelling, and the cost is carried as a
        documented false-rejection risk; what was missing was any test that
        pinned it, which let a prefix match survive r24's mutation run.
        """
        body = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect width="8" height="8"/></svg>'
        for label in (b"utf-8", b"UTF-8", b"Utf-8"):
            visual._bind_xml_encoding(b'<?xml version="1.0" encoding="' + label + b'"?>' + body, "probe")
        visual._bind_xml_encoding(body, "probe")
        visual._bind_xml_encoding(b'<?xml version="1.0"?>' + body, "probe")
        for label in (b"utf-8x", b"utf-8xyz", b"utf8", b"UTF_8", b"unicode-1-1-utf-8", b"unicode20utf8", b"us-ascii", b"utf-16", b""):
            with self.subTest(label=label):
                with self.assertRaisesRegex(visual.VisualDecisionError, "declares a character encoding other than utf-8"):
                    visual._bind_xml_encoding(b'<?xml version="1.0" encoding="' + label + b'"?>' + body, "probe")
        # A processing instruction whose target merely starts with "xml" is not
        # an XML declaration, and is refused rather than read as one.
        for opener in (b"<?xmlfoo version=\"1.0\"?>", b"<?XMLX?>"):
            with self.subTest(opener=opener):
                with self.assertRaisesRegex(visual.VisualDecisionError, "does not open with a readable XML declaration"):
                    visual._bind_xml_encoding(opener + body, "probe")


class VisualDecisionCoreR26Tests(TimeLimitedTestCase):
    """``render_sheet`` and the two trust inputs it used to pin to ``None``.

    The sheet is the *first* step of the accepted owner flow, so a bundle the
    renderer cannot reach has no reachable flow at all.  ``render_sheet`` pinned
    both trust inputs of ``validate_bundle`` to ``None``; the r1c review read
    that as "not renderable" and it was stronger than that.  The two parameters
    are added keyword-only with default ``None``, which is exactly the value the
    call site carried before, so every existing caller keeps its behaviour and
    the accepted ``validate_bundle`` signature is untouched.

    What that closes and what it does not is measured here rather than argued.
    The Affinity half of it, which r1d could only measure as a barrier, is in
    :class:`VisualDecisionCoreR27Tests`.
    """

    def project(self) -> tuple[Path, Path, int]:
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve() / "project"; project.mkdir()
        root = visual.visual_workspace(project, "run-a", create=True)
        return project, root, int(time.time())

    def test_the_public_renderer_keeps_the_accepted_signatures_and_adds_only_keywords(self):
        self.assertEqual(["project_root", "run_id", "now", "sidecar_root", "affinity_auth"],
                         list(inspect.signature(visual.validate_bundle).parameters))
        parameters = inspect.signature(visual.render_sheet).parameters
        self.assertEqual(["project_root", "run_id", "now", "sidecar_root", "affinity_auth"], list(parameters))
        for name in ("sidecar_root", "affinity_auth"):
            self.assertIs(inspect.Parameter.KEYWORD_ONLY, parameters[name].kind)
            self.assertIsNone(parameters[name].default)

    def test_the_public_renderer_forwards_the_sidecar_it_used_to_default(self):
        project, root, now = self.project()
        five_domain_bundle(project, root, now)
        detached = project.parent / "detached"; detached.mkdir()
        # Forwarded, not defaulted past: a sidecar without the narrative is the
        # sidecar the renderer uses, and it is refused.
        with self.assertRaisesRegex(visual.VisualDecisionError, "source contract failed"):
            visual.render_sheet(project, "run-a", now, sidecar_root=detached)
        with self.assertRaisesRegex(visual.VisualDecisionError, "sidecar must remain outside the project"):
            visual.render_sheet(project, "run-a", now, sidecar_root=project)
        with deadline(150, "render_sheet"):
            manifest = visual.render_sheet(project, "run-a", now, sidecar_root=visual.ROOT.parents[1])
        self.assertEqual(9, len({item["path"] for item in manifest["files"]}))
        self.assertFalse(manifest["preselection"])


class VisualDecisionCoreR27Tests(TimeLimitedTestCase):
    """The Affinity-authored candidate sheet, which used to be unreachable.

    r1d measured the barrier and registered it; r1d's critic measured it again
    and found it *unconditional*: on a frozen bundle with unspent receipts,
    ``render_sheet(..., affinity_auth=...)`` failed on the very first attempt,
    inside ``_render_sheet``, at a ``validate_logo_report`` call that carried no
    receipts.  The single-use property of the receipts was not the binding
    cause at all - nothing had spent them yet.  There was therefore *no* order,
    no repetition and no fresh issuance that produced a sheet, and
    ``render_sheet`` is the only producer of that artifact in the repository.
    The consequence is what makes it a blocker rather than a restriction: the
    owner confirmed logo and lockup decisions over an artifact that never
    showed the candidates.

    Two things had to change and both are measured below: ``_render_sheet``
    validates the logo report *with* the receipts, and rendering the sheet
    verifies them under the ledger lock instead of spending them, so the one
    consumption stays with the call that binds the owner's selection.
    """

    def bundle(self) -> tuple[Path, Path, Path, int, dict]:
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        project = base / "project"; project.mkdir()
        secrets = base / "secrets"; secrets.mkdir()
        root = visual.visual_workspace(project, "run-a", create=True); now = int(time.time())
        return project, root, secrets, now, five_domain_affinity_bundle(project, root, secrets, now)

    def test_an_affinity_authored_bundle_renders_a_complete_candidate_sheet(self):
        """The sheet exists, and the four authored candidates are actually in it.

        "A sheet was produced" would be too weak a claim: the register used to
        offer a two-line markdown file as the containment for this very gap, and
        an owner cannot review a logo that is not on the page.  So the assertions
        are about content - one card and one bound ``<img>`` per authored
        candidate, in the section of its own domain, with the asset copied into
        the sheet's own subresource tree - and about the manifest staying
        non-normative and free of preselection.
        """
        project, root, _secrets, now, fixture = self.bundle()
        genesis = fixture["ledger_path"].read_bytes()
        # Without a descriptor nothing changes: authored candidates still need
        # external authentication, and the refusal costs no ledger generation.
        with self.assertRaisesRegex(visual.VisualDecisionError, "require external authentication and replay state"):
            visual.render_sheet(project, "run-a", now)
        self.assertEqual(genesis, fixture["ledger_path"].read_bytes())

        with deadline(150, "render_sheet"):
            manifest = visual.render_sheet(project, "run-a", now, affinity_auth=fixture["auth"])
        self.assertEqual(9, len({item["path"] for item in manifest["files"]}))
        self.assertFalse(manifest["preselection"]); self.assertFalse(manifest["normative"])
        self.assertFalse(manifest["interactive_controls"])
        self.assertEqual("required", manifest["human_review_status"])
        members = {item["path"]: item["sha256"] for item in manifest["files"]}
        document = (root / "sheet/index.html").read_text(encoding="utf-8")
        for section, kind in (("candidates-logo", "logo"), ("candidates-lockup", "lockup")):
            body = document.split(f'id="{section}"', 1)[1].split("</section>", 1)[0]
            source = next(item for item in fixture["candidate_set"]["candidates"] if item["kind"] == kind)
            expected = source["value"]["logo_candidate_ids" if kind == "logo" else "lockup_candidate_ids"]
            with self.subTest(section=section):
                # One card per bundle candidate, carrying every drawing its value names.
                self.assertEqual(1, body.count("<article"))
                self.assertIn(f"Kandidat {source['id']}", body)
                self.assertEqual("affinity-mcp", source["value"]["creation_origin"])
                for candidate_id in expected:
                    staged = (root / f"staging/logo-doc/{candidate_id}.svg").read_bytes()
                    digest = visual.sha256_bytes(staged)
                    self.assertIn(f" · {escape_id(candidate_id)}</p>", body)
                    self.assertIn(f'<img src="assets/{digest}.svg"', body)
                    # The subresource is a real copy of the authored asset, and
                    # the manifest binds the bytes that were written.
                    written = (root / f"sheet/assets/{digest}.svg").read_bytes()
                    self.assertEqual(digest, members[f"assets/{digest}.svg"])
                    self.assertEqual(staged, written)
        # The manifest round trip the renderer ends in accepts its own output.
        visual.validate_sheet_manifest(manifest, root / "sheet")

    def test_rendering_the_sheet_verifies_the_receipts_and_confirming_spends_them(self):
        """Single-use, unweakened - and the accepted flow now fits inside it.

        The flow reaches the same receipts twice: once to render the sheet the
        owner looks at, once to validate the bundle the owner's selection is
        bound to.  Rendering is not an operation - the sheet mutates nothing and
        decides nothing - so it verifies the receipts under the ledger lock and
        leaves the ledger where it found it.  ``validate_bundle`` is unchanged
        and still spends them, exactly once, and from that moment every further
        presentation is refused, the render included.  That last leg is the one
        that shows the promise was not softened into an exemption.
        """
        project, _root, _secrets, now, fixture = self.bundle()
        genesis = fixture["ledger_path"].read_bytes()
        with deadline(150, "render_sheet"):
            visual.render_sheet(project, "run-a", now, affinity_auth=fixture["auth"])
        self.assertEqual(genesis, fixture["ledger_path"].read_bytes())

        result = visual.validate_bundle(project, "run-a", now, affinity_auth=fixture["auth"])
        self.assertEqual([], result["blockers"])
        spent = fixture["ledger_path"].read_bytes()
        self.assertNotEqual(genesis, spent)
        ledger = json.loads(spent.decode("utf-8"))
        self.assertEqual(sorted(fixture["nonces"]), sorted(ledger["nonces"]))
        self.assertEqual(1, ledger["generation"])

        for label in ("render_sheet", "validate_bundle"):
            with self.subTest(after_consumption=label):
                with self.assertRaisesRegex(visual.VisualDecisionError, "already consumed"):
                    if label == "render_sheet":
                        visual.render_sheet(project, "run-a", now, affinity_auth=fixture["auth"])
                    else:
                        visual.validate_bundle(project, "run-a", now, affinity_auth=fixture["auth"])
        self.assertEqual(spent, fixture["ledger_path"].read_bytes())

    def test_the_renderer_refuses_exactly_what_the_validator_refuses(self):
        """The renderer stopped calling ``validate_bundle`` by name; it must still be it.

        To verify without spending, ``render_sheet`` takes the ledger lock
        itself and runs the very same locked body with ``commit=False``.  The
        only permitted difference between the two entry points is therefore
        whether the ledger generation advances - and that is measured rather
        than argued: four bundles the validator refuses are pushed through both,
        and the refusal has to come back word for word.  A renderer that
        validated *less* than the validator would show up here as a missing or
        differently worded refusal.
        """
        project, root, _secrets, now, fixture = self.bundle()
        genesis = fixture["ledger_path"].read_bytes()
        inside = project / "inside.key"; inside.write_bytes(b"k" * 32); inside.chmod(0o600)
        detached = project.parent / "detached"; detached.mkdir()
        cases = {
            "no descriptor at all": {},
            "a key inside the project": {"affinity_auth": {**fixture["auth"], "owner_key_path": str(inside)}},
            "a sidecar inside the project": {"affinity_auth": fixture["auth"], "sidecar_root": project},
            "a sidecar without the narrative": {"affinity_auth": fixture["auth"], "sidecar_root": detached},
        }
        for label, keywords in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(visual.VisualDecisionError) as validator:
                    visual.validate_bundle(project, "run-a", now, **keywords)
                with self.assertRaises(visual.VisualDecisionError) as renderer:
                    visual.render_sheet(project, "run-a", now, **keywords)
                self.assertEqual(str(validator.exception), str(renderer.exception))
        # None of the eight calls advanced the ledger, and no sheet was begun.
        self.assertEqual(genesis, fixture["ledger_path"].read_bytes())
        self.assertFalse((root / "sheet").exists())

    def test_the_renderer_still_refuses_an_authored_report_it_cannot_authenticate(self):
        """The guard r25 pinned is handed receipts, not removed.

        ``_render_sheet`` re-validates the logo report on purpose, and the value
        of that re-validation is that it refuses an authored report nothing has
        authenticated.  Called with no authentication - the shape every
        owner-supplied bundle has - it refuses the same authored report it
        renders above, with the same sentence as before.  A wrong owner key is
        the second half of the same rule: the receipts are checked here, not
        merely carried.
        """
        project, root, _secrets, now, fixture = self.bundle()
        parameters = inspect.signature(visual._render_sheet).parameters
        self.assertIs(inspect.Parameter.KEYWORD_ONLY, parameters["logo_authentication"].kind)
        self.assertIsNone(parameters["logo_authentication"].default)

        brief = json.loads((root / "visual-brief.json").read_text(encoding="utf-8"))
        candidate_set = json.loads((root / "candidate-set.json").read_text(encoding="utf-8"))
        observation = fixture["observation"]
        receipts = {candidate_id: json.loads((root / binding["path"]).read_text(encoding="utf-8"))
                    for candidate_id, binding in fixture["auth"]["receipts"].items()}

        with self.assertRaisesRegex(visual.VisualDecisionError, "requires its own complete mutation receipt"):
            visual._render_sheet(root / "sheet", root, brief, candidate_set, observation)
        self.assertFalse((root / "sheet").exists())
        with self.assertRaisesRegex(visual.VisualDecisionError, "receipt authentication failed"):
            visual._render_sheet(root / "sheet", root, brief, candidate_set, observation,
                                 logo_authentication={"receipts": receipts, "project_root": project, "run_id": "run-a",
                                                      "key": b"w" * 32, "now": now})
        self.assertFalse((root / "sheet").exists())
        # And the ledger was never in play for any of it.
        self.assertEqual(0, json.loads(fixture["ledger_path"].read_text(encoding="utf-8"))["generation"])

    def test_every_entry_point_reads_the_replay_ledger_under_the_ledger_lock(self):
        """Verifying without spending still has to be serialized against the spender.

        ``render_sheet`` and ``verify_bundle`` do not write the ledger, but they
        *read* it and decide on what they read: a nonce already in the ledger is
        what refuses an Affinity-authored sheet.  Reading it outside the lock
        would mean reading it while ``persist_replay_ledger`` is replacing it -
        a decision taken on a half-written authority - and it would leave the
        two verifying entry points as the only callers of the locked body that
        are not actually serialized.  The property is therefore stated the way
        this module states every other lock property: while a *foreign* holder
        has the ledger, all three entry points must refuse rather than proceed,
        and they must work again the moment the holder lets go.  Neither half is
        expressible without the lock really being taken.
        """
        project, root, _secrets, now, fixture = self.bundle()
        genesis = fixture["ledger_path"].read_bytes()
        lock_file = fixture["ledger_path"].parent / f".affinity-lock.{fixture['ledger_path'].name}"
        # Taking and releasing it once is how the lock file comes into being.
        held = visual._affinity_ledger_lock(project, (fixture["key_path"], fixture["ledger_path"]))
        self.assertIsNotNone(held); os.close(held)
        foreign = os.open(lock_file, os.O_RDWR); self.addCleanup(os.close, foreign)
        fcntl.flock(foreign, fcntl.LOCK_EX)
        pinned = visual.AFFINITY_LOCK_TIMEOUT
        self.addCleanup(setattr, visual, "AFFINITY_LOCK_TIMEOUT", pinned)
        visual.AFFINITY_LOCK_TIMEOUT = 0.25
        for label, call in (("render_sheet", visual.render_sheet),
                            ("verify_bundle", visual.verify_bundle),
                            ("validate_bundle", visual.validate_bundle)):
            with self.subTest(entry_point=label):
                with deadline(60, f"{label} under a foreign lock holder"):
                    with self.assertRaisesRegex(visual.VisualDecisionError, "lock is unavailable"):
                        call(project, "run-a", now, affinity_auth=fixture["auth"])
        # Nothing was begun and nothing was read past the closed door.
        self.assertFalse((root / "sheet").exists())
        self.assertEqual(genesis, fixture["ledger_path"].read_bytes())
        # Released, the very same calls succeed - so it was the holder that was
        # refused, not the bundle.
        fcntl.flock(foreign, fcntl.LOCK_UN)
        visual.AFFINITY_LOCK_TIMEOUT = pinned
        with deadline(150, "the entry points after the holder let go"):
            manifest = visual.render_sheet(project, "run-a", now, affinity_auth=fixture["auth"])
            self.assertEqual([], visual.verify_bundle(project, "run-a", now, affinity_auth=fixture["auth"])["blockers"])
        self.assertFalse(manifest["preselection"])
        self.assertEqual(genesis, fixture["ledger_path"].read_bytes())

    def test_verify_bundle_is_validate_bundle_without_the_spending(self):
        """The public verification: same answer, same refusals, no consumption.

        ``verify_bundle`` exists so that a caller with a one-shot resource can
        establish that its operation will succeed *before* it pays for it - the
        adapter's ``prepare-visual-confirmation`` used to spend the receipts on
        its way to refusing an owner typo.  The promise it has to keep is
        therefore two-sided and both sides are measured here: it must answer
        exactly what ``validate_bundle`` answers, and it must be unable to
        admit anything ``validate_bundle`` would refuse.  Single-use is
        untouched: after the one consumption, verification is refused as well.
        """
        project, _root, _secrets, now, fixture = self.bundle()
        genesis = fixture["ledger_path"].read_bytes()

        # Same answer, and repeatable - a verification is not a resource.
        first = visual.verify_bundle(project, "run-a", now, affinity_auth=fixture["auth"])
        second = visual.verify_bundle(project, "run-a", now, affinity_auth=fixture["auth"])
        self.assertEqual(first, second)
        self.assertEqual([], first["blockers"])
        self.assertEqual(genesis, fixture["ledger_path"].read_bytes())

        # Same refusals, word for word, on the descriptor and sidecar cases.
        inside = project / "inside.key"; inside.write_bytes(b"k" * 32); inside.chmod(0o600)
        detached = project.parent / "detached-for-verify"; detached.mkdir()
        for label, keywords in {
            "no descriptor at all": {},
            "a key inside the project": {"affinity_auth": {**fixture["auth"], "owner_key_path": str(inside)}},
            "a sidecar inside the project": {"affinity_auth": fixture["auth"], "sidecar_root": project},
            "a sidecar without the narrative": {"affinity_auth": fixture["auth"], "sidecar_root": detached},
        }.items():
            with self.subTest(case=label):
                with self.assertRaises(visual.VisualDecisionError) as validator:
                    visual.validate_bundle(project, "run-a", now, **keywords)
                with self.assertRaises(visual.VisualDecisionError) as verifier:
                    visual.verify_bundle(project, "run-a", now, **keywords)
                self.assertEqual(str(validator.exception), str(verifier.exception))
        self.assertEqual(genesis, fixture["ledger_path"].read_bytes())

        # The consuming call still answers the same thing, and still consumes.
        committed = visual.validate_bundle(project, "run-a", now, affinity_auth=fixture["auth"])
        self.assertEqual(first, committed)
        spent = fixture["ledger_path"].read_bytes()
        self.assertNotEqual(genesis, spent)
        self.assertEqual(1, json.loads(spent.decode("utf-8"))["generation"])

        # And from here the weaker capability is refused too: verifying is not
        # a way back to receipts that have been spent.
        with self.assertRaisesRegex(visual.VisualDecisionError, "already consumed"):
            visual.verify_bundle(project, "run-a", now, affinity_auth=fixture["auth"])
        self.assertEqual(spent, fixture["ledger_path"].read_bytes())


class VisualDecisionCoreR28Tests(TimeLimitedTestCase):
    """The five eligibility rules the r1f critic reached and no test measured.

    Each of these rules stands in ``_validate_bundle_locked`` and has stood
    there since the accepted r24 core; none of them is a hole.  What the r1f
    critic measured is that the suite never asked about them: he removed each
    one in turn and the whole battery stayed green, so the core would have gone
    on **accepting** what it refuses today.  That is the direction that matters
    for this module - a bundle lives in the run workspace, which is inside the
    project and therefore writable by whoever the run is defending itself
    against (register point 19).

    They are written as five separate tests because they are five separate
    defences and a single fixture that trips all of them would prove only that
    *some* rule fired.  Each case below reaches its own rule with every other
    rule satisfied, and names the message it expects.
    """

    def bundle(self, **keywords) -> tuple[Path, Path, int, dict]:
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        project = base / "project"; project.mkdir()
        root = visual.visual_workspace(project, "run-a", create=True); now = int(time.time())
        return project, root, now, five_domain_bundle(project, root, now, **keywords)

    def test_a_candidate_its_own_report_fails_can_never_become_eligible(self) -> None:
        """G04.  A human ``fail`` decides eligibility, whatever the set declares.

        The shared font report carries a human ``fail`` on one face.  Reviewed
        honestly - ``blocked=True`` - the core accepts the bundle and returns
        typography and code-font as *blockers*, never as eligible candidates.
        Declared as ``candidate`` instead, the core refuses the whole bundle.

        Both halves are the test.  Without the second, a core that stopped
        reading ``human_reviews`` would let a candidate a human has rejected
        become confirmable, and the owner would be asked to confirm it; without
        the first, the rule could be satisfied by refusing everything.
        """
        project, _root, now, _set = self.bundle(blocked=True)
        honest = visual.validate_bundle(project, "run-a", now)
        self.assertEqual(["code-font-choice", "typography-choice"],
                         sorted(item["id"] for item in honest["blockers"]))
        self.assertEqual(["color-choice", "lockup-choice", "logo-choice"],
                         sorted(item["id"] for item in honest["eligible_candidates"]))
        # No blocked candidate leaks into the eligible list under any spelling.
        self.assertFalse({item["id"] for item in honest["blockers"]}
                         & {item["id"] for item in honest["eligible_candidates"]})

        project, _root, now, _set = self.bundle(blocked=True, declare={"typography": "candidate", "code-font": "candidate"})
        with self.assertRaisesRegex(visual.VisualDecisionError, r"candidate eligibility status differs: (typography|code-font)-choice"):
            visual.validate_bundle(project, "run-a", now)
        with self.assertRaisesRegex(visual.VisualDecisionError, r"candidate eligibility status differs"):
            visual.verify_bundle(project, "run-a", now)

    def test_a_declared_status_is_compared_in_both_directions(self) -> None:
        """G05.  The set's own ``status`` is checked against what the report says.

        The rule above measures the ``candidate``-declared failure; this one
        measures the other direction, which no mutation of the human-review
        rule can reach: a candidate whose report passes cleanly may not be
        declared ``blocked`` either.  A core that simply trusted the declaration
        would answer a *different eligible set* than the reports support, in
        whichever direction the declaration was bent.
        """
        project, _root, now, _set = self.bundle(declare={"color": "blocked"})
        with self.assertRaisesRegex(visual.VisualDecisionError, "candidate eligibility status differs: color-choice"):
            visual.validate_bundle(project, "run-a", now)

    def test_each_report_must_bind_the_candidate_set_it_is_read_with(self) -> None:
        """G03.  A report that describes some other candidate set is refused.

        The edit here is deliberately the one edit a finished bundle tolerates:
        ``candidate_context_sha256`` zeroes ``value.report.sha256`` before it
        hashes, so replacing a report's bytes and re-pinning its digest leaves
        the context - and therefore every *other* report's binding - untouched.
        The only rule left to answer is the one under test, which is what makes
        the refusal below attributable to it rather than to a neighbour.
        """
        project, root, now, candidate_set = self.bundle()
        report = json.loads((root / "color-report.json").read_text(encoding="utf-8"))
        self.assertNotEqual("7" * 64, report["candidate_set_sha256"])
        report["candidate_set_sha256"] = "7" * 64
        binding = canonical_write(root, "color-report.json", report)
        for candidate in candidate_set["candidates"]:
            if candidate["kind"] == "color":
                candidate["value"]["report"] = dict(binding)
                candidate["value_sha256"] = visual.sha256_bytes(visual.canonical_json(candidate["value"]))
        canonical_write(root, "candidate-set.json", candidate_set)
        with self.assertRaisesRegex(visual.VisualDecisionError, "report candidate-set binding differs: color-choice"):
            visual.validate_bundle(project, "run-a", now)

    def test_a_recommendation_may_never_cite_a_candidate_the_core_blocked(self) -> None:
        """G09.  Recommendations are non-normative, but not unbounded.

        A recommendation is the one field of the bundle that names a candidate
        without conferring anything, and the sheet prints it to the owner.  If
        it could name a blocked candidate, the artifact the owner reads would
        recommend exactly what the reports rejected.
        """
        project, _root, now, _set = self.bundle(blocked=True, recommendations=[
            {"kind": "typography", "candidate_id": "typography-choice", "rationale": "unbestätigt", "source_ids": ["impeccable"]}])
        with self.assertRaisesRegex(visual.VisualDecisionError, "recommendation references an ineligible candidate"):
            visual.validate_bundle(project, "run-a", now)

    def test_a_candidate_set_must_bind_the_brief_it_answers(self) -> None:
        """G10.  The brief is re-hashed, never taken on the set's word.

        The brief is edited rather than the set, because the set's own
        ``brief_sha256`` is inside the projection every report binds: bending it
        there is refused by the report binding instead.  The edited brief still
        validates on its own - only its digest moved - so the single rule left
        to answer is whether the set is still bound to the brief in front of it.
        """
        project, root, now, _set = self.bundle()
        brief = json.loads((root / "visual-brief.json").read_text(encoding="utf-8"))
        brief["brand_id"] = "brand-b"
        canonical_write(root, "visual-brief.json", brief)
        with self.assertRaisesRegex(visual.VisualDecisionError, "candidate set does not bind the visual brief"):
            visual.validate_bundle(project, "run-a", now)


class VisualDecisionCoreR29Tests(TimeLimitedTestCase):
    """The value digest of a candidate, which the r1g critic found unmeasured.

    ``candidate_context_sha256`` **recomputes** ``value_sha256`` in its
    projection before it hashes, so a candidate whose declared digest does not
    describe its own value is the one edit a finished bundle survives without
    breaking a single report binding - the same property the neighbouring
    ``report candidate-set binding`` test relies on for its own edit.  The rule
    that answers it is one line in ``_validate_bundle_locked``; removing it left
    the whole battery green, and the core went on **accepting** a set whose
    digest is a claim rather than a measurement.

    It matters beyond the core.  ``prepare_visual_confirmation`` copies exactly
    this field into the handoff as ``candidate_value_sha256``, and the owner
    receipt binds the decisions the handoff carries: an unchecked digest here is
    a value the owner confirms under a hash nobody computed from it.
    """

    def bundle(self, **keywords) -> tuple[Path, Path, int, dict]:
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        project = base / "project"; project.mkdir()
        root = visual.visual_workspace(project, "run-a", create=True); now = int(time.time())
        return project, root, now, five_domain_bundle(project, root, now, **keywords)

    def test_a_candidate_value_digest_must_describe_the_value_it_stands_on(self) -> None:
        """K02.  ``value_sha256`` is recomputed, never taken on the set's word.

        Two edits, because a digest can be wrong in two ways and only the second
        one is interesting: a digest that describes nothing, and a digest that
        describes some *other* candidate's value.  The second is the smuggling
        shape - a value the reports covered, under the digest of a value they
        did not - and it is the reason the rule cannot be satisfied by a
        well-formedness check on the field.

        The honest bundle is validated first and again at the end, so the
        refusals between are attributable to the one edit that produced them
        rather than to the fixture or to the round trip.
        """
        project, root, now, candidate_set = self.bundle()
        accepted = visual.validate_bundle(project, "run-a", now)
        self.assertEqual(5, len(accepted["eligible_candidates"]))
        pinned = visual.candidate_context_sha256(candidate_set)

        values = {candidate["kind"]: candidate["value"] for candidate in candidate_set["candidates"]}
        cases = {
            "a digest of nothing": "0" * 64,
            "the digest of another candidate's value": visual.sha256_bytes(visual.canonical_json(values["logo"])),
        }
        for label, digest in cases.items():
            with self.subTest(edit=label):
                spoiled = json.loads(json.dumps(candidate_set))
                for candidate in spoiled["candidates"]:
                    if candidate["kind"] == "color":
                        self.assertNotEqual(digest, candidate["value_sha256"])
                        candidate["value_sha256"] = digest
                canonical_write(root, "candidate-set.json", spoiled)
                # The edit really is invisible to every other binding: the
                # context the reports are pinned to has not moved.
                self.assertEqual(pinned, visual.candidate_context_sha256(spoiled))
                with self.assertRaisesRegex(visual.VisualDecisionError, "candidate value hash differs: color-choice"):
                    visual.validate_bundle(project, "run-a", now)
                with self.assertRaisesRegex(visual.VisualDecisionError, "candidate value hash differs: color-choice"):
                    visual.verify_bundle(project, "run-a", now)

        canonical_write(root, "candidate-set.json", candidate_set)
        self.assertEqual(5, len(visual.validate_bundle(project, "run-a", now)["eligible_candidates"]))


# --- an explained font classification ------------------------------------------

#: A pinned sparse checkout of github.com/google/fonts beside the repository, like
#: the other upstream checkouts under ``external-sources``.  Its revision is read
#: from the checkout and never written here: the module checks that a revision is
#: bound, not which one.
GOOGLE_FONTS_CHECKOUT = visual.ROOT.parents[1] / "external-sources" / "google-fonts"
GOOGLE_FONTS_ORIGIN = "https://github.com/google/fonts"
#: upstream family directory -> (font file, family in METADATA.pb and name table).
#: Real upstream bytes, chosen across the catalogue's main categories.  The
#: comment names what the fixture stands for; nothing in the module knows a name.
GOOGLE_FONT_FIXTURES = {
    "ofl/spectral": ("Spectral-Regular.ttf", "Spectral"),                      # SERIF    /Serif/Transitional
    "ofl/crimsonpro": ("CrimsonPro[wght].ttf", "Crimson Pro"),                 # SERIF    /Serif/Old Style Garalde
    "ofl/alegreya": ("Alegreya[wght].ttf", "Alegreya"),                        # SERIF    /Serif/Humanist Venetian
    "ofl/playfairdisplay": ("PlayfairDisplay[wght].ttf", "Playfair Display"),  # SERIF    /Serif/Didone
    "ofl/faustina": ("Faustina[wght].ttf", "Faustina"),                        # SERIF    /Serif/Scotch, nameID 1 "Faustina Light"
    "apache/robotoslab": ("RobotoSlab[wght].ttf", "Roboto Slab"),              # SERIF    families.csv: /Slab/Geometric only
    "ofl/figtree": ("Figtree[wght].ttf", "Figtree"),                           # SANS_SERIF
    "ofl/courierprime": ("CourierPrime-Regular.ttf", "Courier Prime"),         # MONOSPACE, fixed pitch, no U+1E9E
    "ofl/azeretmono": ("AzeretMono[wght].ttf", "Azeret Mono"),                 # MONOSPACE, metrics say proportional
    "ofl/anton": ("Anton-Regular.ttf", "Anton"),                               # DISPLAY
    "ofl/indieflower": ("IndieFlower-Regular.ttf", "Indie Flower"),            # HANDWRITING
    "ofl/gloock": ("Gloock-Regular.ttf", "Gloock"),                            # DISPLAY and SERIF
    "ofl/castoro": ("Castoro-Regular.ttf", "Castoro"),                         # SERIF, PANOSE bFamilyType 2 / bSerifStyle 0
    "ofl/karla": ("Karla[wght].ttf", "Karla"),                                 # SANS_SERIF, PANOSE 2 / 11
    "ofl/librebaskerville": ("LibreBaskerville[wght].ttf", "Libre Baskerville"),  # SERIF, PANOSE bFamilyType 2 / bSerifStyle 0
    "ofl/newsreader": ("Newsreader[opsz,wght].ttf", "Newsreader"),             # SERIF, PANOSE 2 / 0, nameID 1 "Newsreader 16pt"
    "ofl/ledger": ("Ledger-Regular.ttf", "Ledger"),                            # SERIF, PANOSE 2 / 0, no U+1E9E
    "apache/mountainsofchristmas": ("MountainsofChristmas-Regular.ttf", "Mountains of Christmas"),  # DISPLAY, PANOSE 3 / 0
    "ofl/cutivemono": ("CutiveMono-Regular.ttf", "Cutive Mono"),               # MONOSPACE, post 0, U+00B7 narrower than the cell
}
_GOOGLE_FONT_CACHE: dict[tuple[str, tuple[int, int] | None, tuple], tuple[bytes, bytes]] = {}


def google_fonts_revision() -> str:
    head = (GOOGLE_FONTS_CHECKOUT / ".git" / "HEAD").read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise AssertionError(f"the google/fonts checkout is not pinned to one revision: {GOOGLE_FONTS_CHECKOUT}")
    return head


def google_metadata(directory: str) -> bytes:
    return (GOOGLE_FONTS_CHECKOUT / directory / "METADATA.pb").read_bytes()


def google_font(directory: str, panose: tuple[int, int] | None = None, retouch: tuple = ()) -> tuple[bytes, bytes]:
    """(WOFF2 asset, sfnt source) of a real upstream font.

    ``panose`` rewrites exactly ``bFamilyType`` and ``bSerifStyle`` and nothing
    else, so a digit can be varied on otherwise real bytes.  ``retouch`` applies
    ``("unmap", code)`` - the code point leaves every cmap subtable - and
    ``("advance", code, value)`` - the glyph that code point maps to gets that
    advance - in order, and nothing else.
    """
    key = (directory, panose, retouch)
    if key not in _GOOGLE_FONT_CACHE:
        path = GOOGLE_FONTS_CHECKOUT / directory / GOOGLE_FONT_FIXTURES[directory][0]
        if not path.is_file():
            raise AssertionError(f"the pinned google/fonts checkout lacks {path}")
        source = path.read_bytes()
        if panose is not None or retouch:
            font = TTFont(io.BytesIO(source))
            if panose is not None:
                font["OS/2"].panose.bFamilyType, font["OS/2"].panose.bSerifStyle = panose
            for edit in retouch:
                if edit[0] == "unmap":
                    for table in font["cmap"].tables:
                        table.cmap.pop(edit[1], None)
                elif edit[0] == "advance":
                    glyph = font.getBestCmap()[edit[1]]; font["hmtx"][glyph] = (edit[2], font["hmtx"][glyph][1])
                else:
                    raise AssertionError(f"unknown retouch: {edit!r}")
            buffer = io.BytesIO(); font.save(buffer); source = buffer.getvalue()
        font = TTFont(io.BytesIO(source)); font.flavor = "woff2"; buffer = io.BytesIO(); font.save(buffer)
        _GOOGLE_FONT_CACHE[key] = (buffer.getvalue(), source)
    return _GOOGLE_FONT_CACHE[key]


class VisualDecisionExplainedFontClassificationTests(TimeLimitedTestCase):
    """PANOSE 0 is "no information", and a bound METADATA.pb may say what it does not.

    ``extract_font_metadata`` reads a zeroed PANOSE as ``sans``.  The rule under
    test lets a report bind a google/fonts ``METADATA.pb`` for such a font and
    declare the class its *main category* maps onto - and nothing else: not for
    a font whose PANOSE says anything, not from a category that maps onto no
    class or onto two, not from a source of another family, another kind, other
    bytes or no revision, and not against the pitch the font's metrics decide.
    Every leg runs on real upstream font bytes through ``validate_font_report``.
    """

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.revision = google_fonts_revision()

    def real(self, font_id: str, directory: str, classification: str, *, root: Path | None = None,
             panose: tuple[int, int] | None = None, source: bool = True, metadata: bytes | None = None) -> dict:
        root = root or self.root
        asset, sfnt = google_font(directory, panose)
        (root / f"{font_id}.woff2").write_bytes(asset)
        source_binding = write(root / f"{font_id}.ttf", sfnt); license_binding = write(root / "OFL.txt", "fixture license")
        rights = {"schema_version": "rights-evidence.v1", "issuer": "Fixture", "source": GOOGLE_FONTS_ORIGIN,
                  "scope": "single-asset", "asset_id": font_id, "license_id": "OFL-1.1", "status": "open-license",
                  "version": "1.0", "date": "2026-09-11", "todo": None, "source_revision": self.revision,
                  "source_path": source_binding["path"], "source_sha256": source_binding["sha256"],
                  "license_evidence_path": "OFL.txt", "license_evidence_sha256": license_binding["sha256"],
                  "transformation": "Deterministic test conversion to WOFF2."}
        extracted = cached_metadata(asset)
        candidate = {"id": font_id, "classification": classification, "family": extracted["family"],
                     "specimen": visual.SPECIMEN, "specimen_sha256": visual.SPECIMEN_SHA256,
                     "asset": {"path": f"{font_id}.woff2", "sha256": visual.sha256_bytes(asset)},
                     "metadata": {"license_spdx": "OFL-1.1",
                                  "source_pin": {"origin": GOOGLE_FONTS_ORIGIN, "revision": self.revision, **source_binding},
                                  "license_evidence": license_binding, "extracted": extracted},
                     "rights": canonical_write(root, f"{font_id}.rights.json", rights),
                     "objective_checks": {"woff2-valid": True, "asset-hash-valid": True, "rights-closed": True,
                                          "pin-valid": True, "specimen-identical": True, "metadata-matches": True,
                                          "glyph-coverage": True, "embedding-allowed": True},
                     "human_reviews": {name: "required" for name in ("readability", "tracking", "digits", "german-glyphs", "code-punctuation", "fallback", "reflow-200")}}
        if source:
            raw = google_metadata(directory) if metadata is None else metadata
            candidate["metadata"]["classification_source"] = {
                "kind": "google-fonts-metadata", "origin": GOOGLE_FONTS_ORIGIN, "revision": self.revision,
                "upstream_path": f"{directory}/METADATA.pb", "family": GOOGLE_FONT_FIXTURES[directory][1],
                "file": write(root / f"{font_id}.METADATA.pb", raw)}
        return candidate

    def slots(self, **replace: dict) -> dict:
        """A report of one serif, one sans and one mono; named slots are replaced."""
        plan = [("serif", "Fixture Serif", "serif-a"), ("sans", "Fixture Sans", "sans-a"), ("mono", "Fixture Mono", "mono-a")]
        chosen = dict(zip(("serif", "sans", "mono"), font_candidates(self.root, plan)))
        chosen.update(replace)
        return {"schema_version": "brandkit-font-candidate-report.v1", "candidate_set_sha256": "a" * 64,
                "specimen_sha256": visual.SPECIMEN_SHA256, "candidates": [chosen["serif"], chosen["sans"], chosen["mono"]],
                "objective_status": "pass", "human_review_status": "required"}

    def accepts(self, report: dict) -> None:
        visual.validate_font_report(report, self.root)

    def refuses(self, report: dict, message: str) -> None:
        with self.assertRaisesRegex(visual.VisualDecisionError, message):
            visual.validate_font_report(report, self.root)

    @staticmethod
    def edited(raw: bytes, old: bytes, new: bytes) -> bytes:
        if raw.count(old) != 1:
            raise AssertionError(f"fixture edit is not unique: {old!r}")
        return raw.replace(old, new)

    def test_a_zeroed_panose_is_explained_by_the_main_category_across_the_catalogue(self) -> None:
        # Serifs from four classification groups, a Scotch face whose name table
        # splits the family over nameIDs 1 and 16, and a slab that families.csv
        # files under /Slab/ alone: the one main category SERIF covers them all,
        # and the subdivision is never read.
        for directory in ("ofl/spectral", "ofl/crimsonpro", "ofl/alegreya", "ofl/playfairdisplay", "ofl/faustina", "apache/robotoslab"):
            with self.subTest(directory=directory):
                asset, _ = google_font(directory)
                panose = TTFont(io.BytesIO(asset))["OS/2"].panose
                self.assertEqual((0, 0), (panose.bFamilyType, panose.bSerifStyle), "a fixture PANOSE says nothing about")
                self.assertEqual("sans", visual.extract_font_metadata(asset)["classification"], "the reading this rule answers")
                explained = self.real("serif-g", directory, "serif")
                self.accepts(self.slots(serif=explained))
                self.assertEqual("sans", explained["metadata"]["extracted"]["classification"],
                                 "metadata.extracted stays the PANOSE reading; only the declared class is explained")
                # Without a source nothing has moved: declared serif is refused,
                # declared sans is accepted exactly as before.
                self.refuses(self.slots(serif=self.real("serif-g", directory, "serif", source=False)),
                             "font metadata or classification differs: serif-g")
                self.accepts(self.slots(sans=self.real("sans-g", directory, "sans", source=False)))
                # With the source, the class is the source's and not the report's.
                self.refuses(self.slots(sans=self.real("sans-g", directory, "sans")), "font metadata or classification differs: sans-g")
        # SANS_SERIF explains sans - the same class the PANOSE fallback gives -
        # and a report that declares serif against it is refused.
        self.accepts(self.slots(sans=self.real("sans-g", "ofl/figtree", "sans")))
        self.refuses(self.slots(serif=self.real("serif-g", "ofl/figtree", "serif")), "font metadata or classification differs: serif-g")
        # MONOSPACE explains mono for a font whose metrics are fixed-pitch.  This
        # face lacks U+1E9E, so the report is refused one rule LATER, on its
        # glyphs: the classification itself went through.
        self.refuses(self.slots(mono=self.real("mono-g", "ofl/courierprime", "mono")), "font lacks required specimen glyphs: mono-g")
        self.refuses(self.slots(sans=self.real("sans-g", "ofl/courierprime", "sans")), "font metadata or classification differs: sans-g")

    def test_a_category_that_maps_onto_no_class_or_onto_two_explains_nothing(self) -> None:
        for directory, category in (("ofl/anton", "DISPLAY"), ("ofl/indieflower", "HANDWRITING")):
            for declared in ("serif", "sans"):
                with self.subTest(directory=directory, declared=declared):
                    self.refuses(self.slots(**{declared: self.real(f"{declared}-g", directory, declared)}),
                                 f"font classification source {declared}-g category {category} maps onto no font class")
            # and without a source the same font stays where PANOSE put it
            self.accepts(self.slots(sans=self.real("sans-g", directory, "sans", source=False)))
        # A family filed under two main categories is ambiguous, whatever the
        # second one is.
        self.refuses(self.slots(serif=self.real("serif-g", "ofl/gloock", "serif")),
                     "serif-g must state exactly one plain top-level category")
        unknown = self.edited(google_metadata("ofl/spectral"), b'category: "SERIF"', b'category: "SYMBOLS"')
        self.refuses(self.slots(serif=self.real("serif-g", "ofl/spectral", "serif", metadata=unknown)),
                     "serif-g category SYMBOLS maps onto no font class")
        # The source does not overrule the pitch the metrics decide - in either
        # direction: a proportional font filed under MONOSPACE is refused for
        # mono and for sans, a fixed-pitch one filed under SERIF for serif.
        mono_spectral = self.edited(google_metadata("ofl/spectral"), b'category: "SERIF"', b'category: "MONOSPACE"')
        self.refuses(self.slots(mono=self.real("mono-g", "ofl/spectral", "mono", metadata=mono_spectral)), "mono-g contradicts the pitch of the font")
        self.refuses(self.slots(sans=self.real("sans-g", "ofl/spectral", "sans", metadata=mono_spectral)), "sans-g contradicts the pitch of the font")
        serif_courier = self.edited(google_metadata("ofl/courierprime"), b'category: "MONOSPACE"', b'category: "SERIF"')
        self.refuses(self.slots(serif=self.real("serif-g", "ofl/courierprime", "serif", metadata=serif_courier)),
                     "serif-g contradicts the pitch of the font")

    def test_a_panose_that_says_anything_is_the_answer_and_no_source_is_consulted(self) -> None:
        forged = self.edited(google_metadata("ofl/karla"), b'category: "SANS_SERIF"', b'category: "SERIF"')
        self.refuses(self.slots(serif=self.real("serif-g", "ofl/karla", "serif", metadata=forged)),
                     "serif-g is not admissible: PANOSE carries a classification")
        # Not even a source that agrees is read.
        self.refuses(self.slots(sans=self.real("sans-g", "ofl/karla", "sans")), "sans-g is not admissible: PANOSE carries a classification")
        self.accepts(self.slots(sans=self.real("sans-g", "ofl/karla", "sans", source=False)))
        # Each digit on its own, on otherwise real bytes - and the rewrite itself
        # is not what refuses.  A serif style is the answer under either family
        # type whose second digit is one (0 "Any", 2 "Latin Text") - 1 "No Fit"
        # included; under every other family type that digit is no serif style,
        # so its 0 opens nothing.
        for pair in ((0, 11), (0, 2), (0, 1), (2, 11), (2, 2), (2, 1), (1, 0), (3, 0), (4, 0), (5, 0)):
            with self.subTest(panose=pair):
                self.refuses(self.slots(serif=self.real("serif-g", "ofl/spectral", "serif", panose=pair)),
                             "serif-g is not admissible: PANOSE carries a classification")
        for pair in ((0, 0), (2, 0)):
            with self.subTest(panose=pair):
                self.accepts(self.slots(serif=self.real("serif-g", "ofl/spectral", "serif", panose=pair)))

    def test_the_source_is_of_a_known_kind_pinned_hashed_and_of_the_font_s_own_family(self) -> None:
        good = self.real("serif-g", "ofl/spectral", "serif"); binding = good["metadata"]["classification_source"]
        self.accepts(self.slots(serif=good))

        def rebound(**changes) -> dict:
            candidate = copy.deepcopy(good); source = candidate["metadata"]["classification_source"]
            for key, value in changes.items():
                if value is None: source.pop(key)
                else: source[key] = value
            return candidate

        crimson = write(self.root / "crimson.METADATA.pb", google_metadata("ofl/crimsonpro"))
        cases = [
            ("a hash that is not the bytes'", rebound(file={**binding["file"], "sha256": "0" * 64}), "font classification source serif-g hash differs"),
            ("a file that is not there", rebound(file={**binding["file"], "path": "absent.METADATA.pb"}), "serif-g"),
            ("no revision", rebound(revision=None), "classification_source: 'revision' is a required property"),
            ("a revision that pins nothing", rebound(revision="8e44913"), "classification_source.revision"),
            ("a branch for a revision", rebound(revision="main"), "classification_source.revision"),
            ("no kind", rebound(kind=None), "classification_source: 'kind' is a required property"),
            ("another kind", rebound(kind="fontsource-package"), "font classification source serif-g kind is not supported: fontsource-package"),
            ("a plain-HTTP origin", rebound(origin="http://github.com/google/fonts"), "classification_source.origin"),
            ("another repository", rebound(origin="https://github.com/example/fonts"), "serif-g is not a family METADATA.pb of https://github.com/google/fonts"),
            ("a repository whose name only begins like it", rebound(origin="https://github.com/google/fonts-mirror"),
             "serif-g is not a family METADATA.pb of https://github.com/google/fonts"),
            ("the licence for a METADATA.pb", rebound(upstream_path="ofl/spectral/OFL.txt"), "is not a family METADATA.pb"),
            ("the taxonomy for a METADATA.pb", rebound(upstream_path="tags/all/families.csv"), "is not a family METADATA.pb"),
            ("a traversal", rebound(upstream_path="ofl/spectral/../crimsonpro/METADATA.pb"), "is not a family METADATA.pb"),
            ("a family METADATA.pb path inside another path", rebound(upstream_path="mirror/ofl/spectral/METADATA.pb"), "is not a family METADATA.pb"),
            ("a family METADATA.pb path with a suffix", rebound(upstream_path="ofl/spectral/METADATA.pb.orig"), "is not a family METADATA.pb"),
            ("no family", rebound(family=None), "classification_source: 'family' is a required property"),
            ("a field the binding has no place for", rebound(category="SERIF"), "Additional properties are not allowed"),
            ("a binding naming another family", rebound(family="Spectral SC"), "serif-g describes another family than the font"),
            ("another family's source, named as such", rebound(file=crimson, upstream_path="ofl/crimsonpro/METADATA.pb", family="Crimson Pro"),
             "serif-g describes another family than the font"),
            ("another family's source, named as this one", rebound(file=crimson, upstream_path="ofl/crimsonpro/METADATA.pb"),
             "serif-g describes another family than the font"),
        ]
        spectral = google_metadata("ofl/spectral")
        top_category = b'category: "SERIF"\n'
        # The family's own line is the first of the file; its fonts repeat the
        # same text indented, so the top-level line is anchored on its successor.
        top_name = b'name: "Spectral"\ndesigner:'
        texts = [
            ("the family absent from the source", self.edited(spectral, top_name, b"designer:"), "exactly one plain top-level name"),
            ("the family stated twice", self.edited(spectral, top_name, b'name: "Spectral"\n' + top_name), "exactly one plain top-level name"),
            ("the category absent", self.edited(spectral, top_category, b""), "exactly one plain top-level category"),
            ("the category stated twice", self.edited(spectral, top_category, top_category * 2), "exactly one plain top-level category"),
            ("the category only inside a nested message", self.edited(spectral, top_category, b'source_extra {\n  category: "SERIF"\n}\n'),
             "exactly one plain top-level category"),
            ("an unquoted category", self.edited(spectral, top_category, b"category: SERIF\n"), "exactly one plain top-level category"),
            ("a category in lower case", self.edited(spectral, top_category, b'category: "serif"\n'), "serif-g category serif maps onto no font class"),
            ("a scalar field without its colon", self.edited(spectral, top_category, b'category "SERIF"\n'), "has a field without a value: category"),
            ("an escaped family", self.edited(spectral, top_name, b'name: "Spec\\164ral"\ndesigner:'), "exactly one plain top-level name"),
            ("a list where a value belongs", self.edited(spectral, top_category, b'category: ["SERIF"]\n'), "is not protobuf text format at offset"),
            ("a message never closed", spectral + b"fonts {\n", "leaves a message open"),
            ("a message never opened", spectral + b"}\n", "closes a message it never opened"),
            ("a field without a value", spectral + b"category }\n", "has a field without a value: category"),
            ("a value where a name belongs", spectral + b'"SERIF"\n', "has a value where a field name belongs"),
            ("a truncated field", spectral + b"category:", "is truncated"),
            ("bytes that are not UTF-8", self.edited(spectral, top_name, b'name: "Spectral\xff"\ndesigner:'), "is not UTF-8"),
        ]
        for label, candidate, message in cases:
            with self.subTest(case=label):
                self.refuses(self.slots(serif=candidate), message)
        # Each source text is written and judged before the next one replaces it
        # under the same name - a candidate built earlier would otherwise bind a
        # hash its file no longer has, and every leg would be refused on the hash.
        for label, raw, message in texts:
            with self.subTest(case=label):
                self.refuses(self.slots(serif=self.real("serif-g", "ofl/spectral", "serif", metadata=raw)), message)
        # A message nested in another message is no top-level field, even when it
        # carries the name of one - so it neither adds a second category nor
        # takes the family's place.
        nested = spectral + b"source_extra {\n  category {\n  }\n  name {\n  }\n}\n"
        self.accepts(self.slots(serif=self.real("serif-g", "ofl/spectral", "serif", metadata=nested)))
        # nameID 1 of this variable font's default instance is the style-linking
        # name "Faustina Light"; the family is nameID 16.  A source for the
        # style-linking name describes another family, even though the string
        # occurs in the name table - and it really does occur there, which is what
        # keeps this refusal from being the trivial one about an absent string.
        # Since the typographic-family round both readings agree: the report names
        # the family, and this rule has always held a source against
        # ``(names[16] or names[1])``.
        light = self.edited(google_metadata("ofl/faustina"), b'name: "Faustina"\ndesigner:', b'name: "Faustina Light"\ndesigner:')
        faustina = self.real("serif-g", "ofl/faustina", "serif", metadata=light)
        faustina["metadata"]["classification_source"]["family"] = "Faustina Light"
        self.assertEqual("Faustina", faustina["metadata"]["extracted"]["family"])
        self.assertEqual("Faustina Light", visual._default_instance_family(google_font("ofl/faustina")[0]))
        self.refuses(self.slots(serif=faustina), "serif-g describes another family than the font")
        # the honest binding, once more after every refusal
        self.accepts(self.slots(serif=self.real("serif-g", "ofl/spectral", "serif")))

    def test_latin_text_without_a_serif_style_is_explained_like_a_zeroed_panose(self) -> None:
        # bFamilyType 2 is "Latin Text", the family type whose second digit is
        # the serif style; bSerifStyle 0 there says nothing about serifs, exactly
        # as under bFamilyType 0.  Real upstream bytes of three serifs.
        for directory in ("ofl/librebaskerville", "ofl/castoro", "ofl/newsreader"):
            with self.subTest(directory=directory):
                asset, _ = google_font(directory)
                panose = TTFont(io.BytesIO(asset))["OS/2"].panose
                self.assertEqual((2, 0), (panose.bFamilyType, panose.bSerifStyle), "real upstream bytes carry half a PANOSE")
                self.assertEqual("sans", visual.extract_font_metadata(asset)["classification"], "the reading this rule answers")
                explained = self.real("serif-g", directory, "serif")
                self.accepts(self.slots(serif=explained))
                self.assertEqual("sans", explained["metadata"]["extracted"]["classification"])
                # without a source nothing has moved
                self.refuses(self.slots(serif=self.real("serif-g", directory, "serif", source=False)), "font metadata or classification differs: serif-g")
                self.accepts(self.slots(sans=self.real("sans-g", directory, "sans", source=False)))
                # with the source the class is the source's
                self.refuses(self.slots(sans=self.real("sans-g", directory, "sans")), "font metadata or classification differs: sans-g")
        # Ledger carries the same half PANOSE and lacks U+1E9E: explained, then
        # refused one rule LATER, on its glyphs - the classification went through.
        asset, _ = google_font("ofl/ledger")
        panose = TTFont(io.BytesIO(asset))["OS/2"].panose
        self.assertEqual((2, 0), (panose.bFamilyType, panose.bSerifStyle))
        self.refuses(self.slots(serif=self.real("serif-g", "ofl/ledger", "serif")), "font lacks required specimen glyphs: serif-g")
        self.refuses(self.slots(serif=self.real("serif-g", "ofl/ledger", "serif", source=False)), "font metadata or classification differs: serif-g")
        # Real bytes under bFamilyType 3 ("Latin Hand Written") with a second
        # digit of 0: that digit is no serif style, so not even a source saying
        # SERIF is read.
        asset, _ = google_font("apache/mountainsofchristmas")
        panose = TTFont(io.BytesIO(asset))["OS/2"].panose
        self.assertEqual((3, 0), (panose.bFamilyType, panose.bSerifStyle))
        forged = self.edited(google_metadata("apache/mountainsofchristmas"), b'category: "DISPLAY"', b'category: "SERIF"')
        self.refuses(self.slots(serif=self.real("serif-g", "apache/mountainsofchristmas", "serif", metadata=forged)),
                     "serif-g is not admissible: PANOSE carries a classification")
        # The other family types on real serif bytes - PANOSE assigns 0 to 5,
        # and a digit outside them is no "Latin Text" either - and Latin Text
        # with a serif style that the explanation contradicts, or agrees with.
        for pair in ((1, 0), (3, 0), (4, 0), (5, 0), (6, 0), (7, 0), (255, 0)):
            with self.subTest(panose=pair):
                self.refuses(self.slots(serif=self.real("serif-g", "ofl/castoro", "serif", panose=pair)),
                             "serif-g is not admissible: PANOSE carries a classification")
        sans_castoro = self.edited(google_metadata("ofl/castoro"), b'category: "SERIF"', b'category: "SANS_SERIF"')
        self.assertEqual("serif", visual.extract_font_metadata(google_font("ofl/castoro", (2, 2))[0])["classification"])
        self.refuses(self.slots(sans=self.real("sans-g", "ofl/castoro", "sans", panose=(2, 2), metadata=sans_castoro)),
                     "sans-g is not admissible: PANOSE carries a classification")
        self.refuses(self.slots(serif=self.real("serif-g", "ofl/castoro", "serif", panose=(2, 11))),
                     "serif-g is not admissible: PANOSE carries a classification")
        self.accepts(self.slots(serif=self.real("serif-g", "ofl/castoro", "serif", panose=(0, 0))))

    def test_space_and_printable_ascii_in_one_advance_is_a_fixed_pitch(self) -> None:
        printable = range(0x20, 0x7F)
        for directory in ("ofl/azeretmono", "ofl/cutivemono"):
            with self.subTest(directory=directory):
                asset, _ = google_font(directory)
                font = TTFont(io.BytesIO(asset)); cmap = font.getBestCmap()
                # Every other path says proportional: post, PANOSE, and "every
                # glyph one advance" - glyphs outside the cell have other ones.
                self.assertEqual(0, font["post"].isFixedPitch)
                self.assertNotEqual(9, font["OS/2"].panose.bProportion)
                self.assertGreater(len({advance for advance, _ in font["hmtx"].metrics.values() if advance}), 1)
                self.assertEqual(1, len({font["hmtx"][cmap[code]][0] for code in printable}))
                metadata = visual.extract_font_metadata(asset)
                self.assertEqual((True, "mono"), (metadata["fixed_pitch"], metadata["classification"]))
                self.accepts(self.slots(mono=self.real("mono-g", directory, "mono", source=False)))
                bound = self.real("mono-g", directory, "mono")
                self.accepts(self.slots(mono=bound))
                # The pitch decides the class here, and still a bound source is
                # checked, never ignored: other bytes than it pins, or a kind this
                # module does not read, refuse the report.
                forged = copy.deepcopy(bound); forged["metadata"]["classification_source"]["file"]["sha256"] = "0" * 64
                self.refuses(self.slots(mono=forged), "font classification source mono-g hash differs")
                unknown = copy.deepcopy(bound); unknown["metadata"]["classification_source"]["kind"] = "fontsource-package"
                self.refuses(self.slots(mono=unknown), "font classification source mono-g kind is not supported: fontsource-package")
                self.refuses(self.slots(sans=self.real("sans-g", directory, "sans", source=False)), "font metadata or classification differs: sans-g")
                self.refuses(self.slots(sans=self.real("sans-g", directory, "sans")), "font metadata or classification differs: sans-g")
                # The path needs every character of the range, at both ends,
                # and one advance over all of them; a font outside it is left
                # to the other paths, which say proportional here.
                # A letter with advance 0 is an advance of its own, not an absence.
                for retouch in ((("unmap", 0x20),), (("unmap", 0x7E),), (("unmap", 0x41),), (("advance", 0x41, 651),), (("advance", 0x7E, 1),),
                                (("advance", 0x41, 0),)):
                    with self.subTest(directory=directory, retouch=retouch):
                        edited = visual.extract_font_metadata(google_font(directory, retouch=retouch)[0])
                        self.assertEqual((False, "sans"), (edited["fixed_pitch"], edited["classification"]))
                # Characters outside the range play no part in it.
                for retouch in ((("unmap", 0x7F),), (("advance", 0xB7, 1),), (("unmap", 0xB7),)):
                    with self.subTest(directory=directory, retouch=retouch):
                        edited = visual.extract_font_metadata(google_font(directory, retouch=retouch)[0])
                        self.assertEqual((True, "mono"), (edited["fixed_pitch"], edited["classification"]))
        # A font that "every glyph one advance" alone calls fixed stays mono
        # although it lacks most of printable ASCII: the new path adds, it does
        # not replace.
        _asset, source = cached_font(self.root / "widths-only.woff2", "Fixture Mono", "mono")
        font = TTFont(io.BytesIO(source)); font["post"].isFixedPitch = 0; font["OS/2"].panose.bProportion = 2; font.flavor = "woff2"
        buffer = io.BytesIO(); font.save(buffer); widths_only = buffer.getvalue()
        self.assertTrue(any(code not in TTFont(io.BytesIO(widths_only)).getBestCmap() for code in printable))
        self.assertEqual((True, "mono"), (visual.extract_font_metadata(widths_only)["fixed_pitch"], visual.extract_font_metadata(widths_only)["classification"]))
        # Real proportional fonts across the main categories stay proportional,
        # and a monospace font the other paths already recognise stays mono.
        for directory in ("ofl/spectral", "ofl/crimsonpro", "ofl/playfairdisplay", "apache/robotoslab", "ofl/librebaskerville", "ofl/figtree",
                          "ofl/karla", "ofl/anton", "ofl/indieflower", "ofl/gloock", "apache/mountainsofchristmas"):
            with self.subTest(directory=directory):
                metadata = visual.extract_font_metadata(google_font(directory)[0])
                self.assertFalse(metadata["fixed_pitch"]); self.assertNotEqual("mono", metadata["classification"])
        self.assertEqual((True, "mono"), (visual.extract_font_metadata(google_font("ofl/courierprime")[0])["fixed_pitch"],
                                          visual.extract_font_metadata(google_font("ofl/courierprime")[0])["classification"]))

    def test_the_sheet_and_the_playground_read_both_residual_decisions(self) -> None:
        # A serif explained under Latin Text without a serif style and a
        # monospace font fixed by its ASCII advances, in one bundle: the domain
        # split, the sheet's serif/sans/mono split and the playground data all
        # read the same classes validate_font_report accepted.
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve() / "project"; project.mkdir()
        root = visual.visual_workspace(project, "run-a", create=True); now = int(time.time())
        candidate_set = five_domain_bundle(project, root, now)
        report = json.loads((root / "font-report.json").read_bytes())
        self.assertEqual(["serif-a", "sans-a", "mono-a"], [item["id"] for item in report["candidates"]])
        report["candidates"][0] = self.real("serif-a", "ofl/librebaskerville", "serif", root=root)
        report["candidates"][2] = self.real("mono-a", "ofl/cutivemono", "mono", root=root, source=False)
        binding = canonical_write(root, "font-report.json", report)
        for candidate in candidate_set["candidates"]:
            if candidate["value"]["report"]["path"] == "font-report.json":
                candidate["value"]["report"] = dict(binding)
                candidate["value_sha256"] = visual.sha256_bytes(visual.canonical_json(candidate["value"]))
        canonical_write(root, "candidate-set.json", candidate_set)
        self.assertEqual(5, len(visual.validate_bundle(project, "run-a", now)["eligible_candidates"]))
        with deadline(170, "render_sheet"):
            visual.render_sheet(project, "run-a", now)
        page = (root / "sheet" / "index.html").read_text(encoding="utf-8")
        self.assertIn("<p>serif · Libre Baskerville</p>", page)
        self.assertIn("<p>mono · Cutive Mono</p>", page)
        self.assertNotIn("sans · Libre Baskerville", page)
        self.assertNotIn("sans · Cutive Mono", page)
        with deadline(170, "render_playground"):
            visual.render_playground(project, "run-a", now)
        raw = (root / "playground" / "data.js").read_bytes()
        data = visual.load_json_bytes(raw[len(visual._PLAYGROUND_DATA_PREFIX):len(raw) - len(visual._PLAYGROUND_DATA_SUFFIX)], "probe")
        faces = {face["asset_sha256"]: face["classification"] for face in data["faces"]}
        self.assertEqual("serif", faces[report["candidates"][0]["asset"]["sha256"]])
        self.assertEqual("mono", faces[report["candidates"][2]["asset"]["sha256"]])
        axes = {axis["domain"]: axis["candidates"] for axis in data["axes"]}
        self.assertEqual("Libre Baskerville", axes["typography"][0]["serif"]["family"])
        self.assertEqual("Cutive Mono", axes["code-font"][0]["mono"]["family"])
        self.assertEqual([], data["blocked"])

    def test_every_reader_of_the_classification_reads_the_explained_class(self) -> None:
        # The CLI entry point.
        report = self.slots(serif=self.real("serif-g", "ofl/spectral", "serif"))
        canonical_write(self.root, "font-report.json", report)
        argv = ["validate", "--kind", "font", "--path", "font-report.json", "--root", str(self.root)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, visual.main(argv))
        bare = copy.deepcopy(report); del bare["candidates"][0]["metadata"]["classification_source"]
        canonical_write(self.root, "font-report.json", bare)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(1, visual.main(argv))
        self.assertIn("font metadata or classification differs: serif-g", errors.getvalue())
        # The bundle and the sheet: the coverage rule, validate_bundle's domain
        # split and the renderer all read the declared class.
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve() / "project"; project.mkdir()
        root = visual.visual_workspace(project, "run-a", create=True); now = int(time.time())
        candidate_set = five_domain_bundle(project, root, now)
        report = json.loads((root / "font-report.json").read_bytes())
        self.assertEqual("serif-a", report["candidates"][0]["id"])
        report["candidates"][0] = self.real("serif-a", "ofl/spectral", "serif", root=root)
        binding = canonical_write(root, "font-report.json", report)
        for candidate in candidate_set["candidates"]:
            if candidate["value"]["report"]["path"] == "font-report.json":
                candidate["value"]["report"] = dict(binding)
                candidate["value_sha256"] = visual.sha256_bytes(visual.canonical_json(candidate["value"]))
        canonical_write(root, "candidate-set.json", candidate_set)
        self.assertEqual(5, len(visual.validate_bundle(project, "run-a", now)["eligible_candidates"]))
        visual.render_sheet(project, "run-a", now)
        page = (root / "sheet" / "index.html").read_text(encoding="utf-8")
        self.assertIn("<p>serif · Spectral</p>", page)
        self.assertNotIn("sans · Spectral", page)
        self.assertEqual(2, page.count('<p class="specimen">'))


class VisualDecisionRenderSheetFixTests(TimeLimitedTestCase):
    """The decision sheet for any brand: any number of candidates, any subset of domains.

    Owner decision 2026-09-11 after the first application run, whose sheet had
    to be built by hand: the product renders it, generically.  Every test below
    states a property of what the owner sees - a card per candidate, a composed
    CI view per colour, set text per typography, a code block per mono, an open
    domain named as open - and reads it off the rendered page and its stylesheet,
    never off a byte of the renderer.
    """

    TIME_LIMIT = 900.0
    SECTIONS = "".join(f'<section id="{name}"><h2>x</h2></section>' for name in (
        "candidates-color", "candidates-typography", "candidates-code-font", "candidates-logo", "candidates-lockup", "recommendations", "owner-review"))

    def workspace(self) -> tuple[Path, Path, int]:
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve() / "project"; project.mkdir()
        return project, visual.visual_workspace(project, "run-a", create=True), int(time.time())

    def rendered(self, counts: dict[str, int], **shape) -> tuple[Path, Path, int, dict, str]:
        project, root, now = self.workspace()
        multi_domain_bundle(project, root, now, counts, **shape)
        with deadline(600, "render_sheet"):
            manifest = visual.render_sheet(project, "run-a", now)
        return project, root, now, manifest, (root / "sheet" / "index.html").read_text(encoding="utf-8")

    @staticmethod
    def section(tree: PageTree, kind: str) -> dict:
        return next(node for node in nodes(tree.root, "section") if node["attrs"].get("id") == f"candidates-{kind}")

    def checker(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        sheet = Path(temp.name).resolve() / "sheet"; (sheet / "assets").mkdir(parents=True)
        (sheet / "assets/a.svg").write_text("svg"); (sheet / "style.css").write_text('@font-face{src:url("assets/a.svg")}')

        def check(fragment: str) -> None:
            page = ('<!doctype html><html lang="de"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="'
                    + visual.SHEET_CSP + '"><link rel="stylesheet" href="style.css"><title>x</title></head><body><main>'
                    + self.SECTIONS + fragment + "</main></body></html>")
            (sheet / "index.html").write_bytes(page.encode("utf-8"))
            files = [{"path": name, "sha256": visual.sha256_bytes((sheet / name).read_bytes())} for name in ("assets/a.svg", "index.html", "style.css")]
            manifest = {"schema_version": "brandkit-visual-sheet-manifest.v1", "brief_sha256": "a" * 64, "candidate_set_sha256": "b" * 64,
                        "affinity_observation_sha256": "c" * 64, "files": files, "viewports": [320, 768, 1440], "offline": True,
                        "interactive_controls": False, "preselection": False, "normative": False, "human_review_status": "required"}
            with deadline(60, "validate_sheet_manifest"):
                visual.validate_sheet_manifest(manifest, sheet)
        return check

    # --- any number of candidates, any subset of domains --------------------------

    def test_any_number_of_candidates_per_domain_renders_through_its_own_validator(self):
        shapes = ({"color": 1, "typography": 1, "code-font": 1, "logo": 1, "lockup": 1},
                  {"color": 4, "typography": 4, "code-font": 2},
                  {"color": 3, "typography": 2},
                  {"typography": 3, "code-font": 3, "logo": 2},
                  {"color": 2, "lockup": 3})
        for counts in shapes:
            with self.subTest(counts=counts):
                project, root, now, manifest, page = self.rendered(counts)
                eligible = visual.validate_bundle(project, "run-a", now)["eligible_candidates"]
                tree = PageTree(page)
                for kind in visual.SHEET_DOMAINS:
                    titles = [text_of(next(nodes(card, "h3"))) for card in nodes(self.section(tree, kind), "article")]
                    self.assertEqual([f"Kandidat {item['id']}" for item in eligible if item["kind"] == kind], titles)
                parser = visual._SheetParser(); parser.feed(page); parser.close()
                self.assertEqual(visual.SHEET_CSP, parser.csp)
                visual.validate_sheet_manifest(manifest, root / "sheet")

    def test_the_page_grows_linearly_with_the_candidates_and_never_combinatorially(self):
        sizes: dict[int, int] = {}
        for count in (1, 3, 5, 12):
            with self.subTest(candidates_per_domain=count):
                *_, page = self.rendered({kind: count for kind in visual.SHEET_DOMAINS}, distinct_fonts=False)
                sizes[count] = len(page.encode("utf-8"))
                tree = PageTree(page)
                for kind in visual.SHEET_DOMAINS:
                    self.assertEqual(count, len(list(nodes(self.section(tree, kind), "article"))))
                # One composed CI view per colour, one set text per typography:
                # the colour x typography cross is not on the page.
                self.assertEqual(count, len(list(nodes(tree.root, "div", "ci"))))
                self.assertEqual(count, len(list(nodes(tree.root, "div", "set"))))
        per_candidate = [(sizes[high] - sizes[low]) / (high - low) for low, high in ((1, 3), (3, 5), (5, 12))]
        # Constant per candidate, up to the extra digit of two-digit indices.
        self.assertLess(max(per_candidate) - min(per_candidate), 0.02 * min(per_candidate), sizes)

    # --- what a colour candidate looks like applied --------------------------------

    def test_every_colour_candidate_is_applied_once_on_the_same_named_reference(self):
        advice = [{"kind": "color", "candidate_id": "color-b", "rationale": "Ruhiger Kontrast", "source_ids": ["impeccable"]}]
        project, root, now, manifest, page = self.rendered({"color": 3, "typography": 1}, recommendations=advice)
        content = visual.SAMPLE_CONTENT["de-DE"]; css = (root / "sheet" / "style.css").read_text(encoding="utf-8")
        tree = PageTree(page); section = self.section(tree, "color")
        cards = list(nodes(section, "article")); shapes = set()
        self.assertEqual(3, len(cards))
        for letter, card in zip("abc", cards):
            with self.subTest(candidate=f"color-{letter}"):
                views = list(nodes(card, "div", "ci")); self.assertEqual(1, len(views)); view = views[0]
                palette_class = next(name for name in view["attrs"]["class"].split() if name.startswith("pal-"))
                report = json.loads((root / f"color-report-{letter}.json").read_bytes())
                declared = re.search(r"\." + palette_class + r"\{([^}]*)\}", css).group(1)
                self.assertEqual({f"--{role}": value for role, value in report["roles"].items()}, dict(item.split(":", 1) for item in declared.split(";")))
                for css_class, expected in (("ci-heading", content["heading"]), ("ci-body", content["paragraphs"][0]),
                                            ("ci-primary", content["action_primary"]), ("ci-secondary", content["action_secondary"]),
                                            ("ci-focus", content["action_focus"]), ("ci-kpi-value", content["kpi_value"]),
                                            ("ci-kpi-label", content["kpi_label"]), ("ci-warning", f'{content["notice_label"]} {content["notice"]}'),
                                            ("ci-danger", f'{content["error_label"]} {content["error"]}')):
                    self.assertEqual([expected], [text_of(node) for node in nodes(view, None, css_class)])
                rows = [[text_of(cell) for cell in elements(row)] for row in nodes(view, "tr")]
                self.assertEqual([content["table_head"], *content["table_rows"]], rows)
                # The reference typography is generic and belongs to no candidate.
                self.assertIn("face-ref-sans", next(nodes(view, None, "ci-heading"))["attrs"]["class"])
                self.assertIn("face-ref-serif", next(nodes(view, None, "ci-body"))["attrs"]["class"])
                self.assertNotIn("Empfehlung", text_of(card))
                # WCAG results come from the report, not from the page.
                shown = [[text_of(cell) for cell in elements(row)] for row in list(nodes(card, "tr"))[len(rows):]]
                self.assertEqual([[item["foreground_role"], item["background_role"], item["usage"], f"{item['minimum_ratio']:g}:1", item["ratio_display"],
                                   "bestanden" if item["passed"] else "nicht bestanden"] for item in report["tests"]], shown[1:])
                shapes.add(re.sub(r"pal-\d+", "pal-N", repr(view)))
        self.assertEqual(1, len(shapes))
        self.assertEqual(1, text_of(section).count("Referenzbelegung, keine Empfehlung:"))
        self.assertIn("Ruhiger Kontrast", text_of(next(node for node in nodes(tree.root, "section") if node["attrs"].get("id") == "recommendations")))
        # The roles work where the owner asked to see them work.
        for rule in (".ci-primary{background:var(--primary);color:var(--on-primary)}", ".ci-secondary{background:var(--secondary);color:var(--on-secondary)}",
                     ".ci-focus{background:var(--surface);color:var(--text);outline:3px solid var(--focus);outline-offset:3px}",
                     ".ci-warning{background:var(--warning);color:var(--on-warning)}", ".ci-danger{background:var(--danger);color:var(--on-danger)}",
                     ".ci-kpi{background:var(--accent);color:var(--on-accent);"):
            self.assertIn(rule, css)

    def test_the_neutral_reference_palette_is_complete_grey_and_passes_every_pairing(self):
        report = visual.color_contrast_report("0" * 64, "reference", visual.REFERENCE_PALETTE, ["text", "icon"])
        self.assertEqual("pass", report["objective_status"])
        self.assertEqual(visual.SEMANTIC_ROLES, set(visual.REFERENCE_PALETTE))
        for value in visual.REFERENCE_PALETTE.values():
            self.assertEqual(1, len({value[1:3], value[3:5], value[5:7]}), value)

    # --- set typography and code ----------------------------------------------------

    def test_set_typography_puts_sans_on_headings_and_serif_on_the_body_with_one_text_for_all(self):
        project, root, now, manifest, page = self.rendered({"typography": 3})
        content = visual.SAMPLE_CONTENT["de-DE"]; css = (root / "sheet" / "style.css").read_text(encoding="utf-8"); faces = sheet_faces(css)
        declared = re.search(r"\.pal-ref\{([^}]*)\}", css).group(1)
        self.assertEqual({f"--{role}": value for role, value in visual.REFERENCE_PALETTE.items()}, dict(item.split(":", 1) for item in declared.split(";")))
        tree = PageTree(page); texts = set()
        for letter, card in zip("abc", nodes(self.section(tree, "typography"), "article")):
            with self.subTest(candidate=f"typography-{letter}"):
                fonts = {item["classification"]: item for item in json.loads((root / f"font-report-{letter}.json").read_bytes())["candidates"]}
                # The fair, byte-identical comparison stays: one specimen per font, in that font.
                specimens = [(faces[node["attrs"]["class"]], text_of(next(nodes(node, "p", "specimen")))) for node in elements(card) if node["attrs"].get("class", "").startswith("face-")]
                self.assertEqual([((fonts[kind]["asset"]["sha256"], visual.GENERIC_FAMILIES[kind]), visual.SPECIMEN) for kind in ("serif", "sans")], specimens)
                typeset = next(nodes(card, "div", "set")); head, body = elements(typeset)
                self.assertEqual("set pal-ref", typeset["attrs"]["class"])
                self.assertEqual((fonts["sans"]["asset"]["sha256"], "sans-serif"), faces[head["attrs"]["class"]])
                self.assertEqual((fonts["serif"]["asset"]["sha256"], "serif"), faces[body["attrs"]["class"]])
                self.assertEqual([content["heading"], content["subheading"]], [text_of(node) for node in elements(head)])
                self.assertEqual(content["paragraphs"], [text_of(node) for node in nodes(body, "p", "t-body")])
                self.assertEqual(content["list"], [text_of(node) for node in nodes(body, "li")])
                self.assertEqual([content["table_head"], *content["table_rows"]], [[text_of(cell) for cell in elements(row)] for row in nodes(body, "tr")])
                self.assertEqual([content["quote"]], [text_of(node) for node in nodes(body, "p", "t-quote")])
                texts.add(text_of(typeset))
        self.assertEqual(1, len(texts))

    def test_every_code_font_shows_one_real_code_block_and_ligatures_come_from_gsub_alone(self):
        shapes = (None, "liga", "calt", "unreferenced", "empty", "other")
        # Names that suggest the opposite of what the font carries.
        families = ("Fixture Ligature Code", "Fixture Plain Mono", "Fixture Mono C", "Fixture Liga Mono", "Fixture Calt Mono", "Fixture Mono F")
        expected = ((), ("liga",), ("calt",), (), (), ())
        project, root, now, manifest, page = self.rendered({"code-font": 6}, ligatures=shapes, mono_families=families)
        code = visual.SAMPLE_CONTENT["de-DE"]["code"]; css = (root / "sheet" / "style.css").read_text(encoding="utf-8")
        tree = PageTree(page)
        for letter, card, features in zip(LETTERS, nodes(self.section(tree, "code-font"), "article"), expected):
            with self.subTest(candidate=f"code-font-{letter}", features=features):
                mono = next(item for item in json.loads((root / f"code-report-{letter}.json").read_bytes())["candidates"] if item["classification"] == "mono")
                self.assertEqual(features, visual._font_facts((root / mono["asset"]["path"]).read_bytes(), "probe")["ligatures"])
                blocks = {node["attrs"]["class"]: text_of(node) for node in nodes(card, "pre") if node["attrs"]["class"] != "specimen"}
                if features:
                    self.assertEqual({"code-liga": code, "code-noliga": code}, blocks)
                    self.assertIn("GSUB-Features " + ", ".join(features), text_of(card))
                else:
                    self.assertEqual({"code": code}, blocks)
                    self.assertIn("keine liga- oder calt-Features", text_of(card))
        self.assertIn('.code-liga{font-variant-ligatures:normal;font-feature-settings:"liga" 1,"calt" 1}', css)
        self.assertIn('.code-noliga{font-variant-ligatures:none;font-feature-settings:"liga" 0,"calt" 0}', css)
        # What the one code block is made of.
        lines = code.split("\n")
        self.assertTrue(any(line.startswith("    ") and line.strip() for line in lines))
        for token in ("→", "->", "=>", "<=", ">=", "!=", "===", "&&", "||", "0xFF", "3.14", "1e9", "O0", "1lI", "# ", "// ", "/* ", '"bestanden"', visual.SPECIMEN_SHA256, "/srv/", " INFO "):
            self.assertIn(token, code)
        self.assertIsInstance(json.loads(next(line for line in lines if line.startswith("json")).split(None, 1)[1]), dict)
        self.assertFalse(code.startswith(("\n", "\r")))

    def test_only_default_ligature_features_count_and_a_required_feature_is_one(self):
        # ``dlig`` is discretionary: a browser applies it only on request, so a
        # font whose GSUB offers nothing else draws no ligature by default.  A
        # language system's *required* feature is applied whether or not it is
        # also listed among its features - the same liga, moved there, draws.
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        base, _source = make_font(Path(temp.name) / "gsub.woff2", "Fixture Mono", "mono")

        def gsub(features: str, *, required: bool = False) -> bytes:
            font = TTFont(io.BytesIO(base))
            addOpenTypeFeaturesFromString(font, "languagesystem DFLT dflt;\nlanguagesystem latn dflt;\n"
                                          + "".join(f"feature {tag} {{\n  sub u000066 u000069 by u000069;\n}} {tag};\n" for tag in features.split()))
            if required:
                for script in font["GSUB"].table.ScriptList.ScriptRecord:
                    for system in [script.Script.DefaultLangSys] + [record.LangSys for record in script.Script.LangSysRecord]:
                        system.ReqFeatureIndex = system.FeatureIndex[0]; system.FeatureIndex = []; system.FeatureCount = 0
            buffer = io.BytesIO(); font.save(buffer); return buffer.getvalue()

        for features, required, expected in (("dlig", False, ()), ("liga", False, ("liga",)), ("dlig liga", False, ("liga",)),
                                             ("liga", True, ("liga",)), ("calt", True, ("calt",)), ("dlig", True, ())):
            with self.subTest(features=features, required=required):
                self.assertEqual(expected, visual._font_facts(gsub(features, required=required), "probe")["ligatures"])

    def test_characters_a_font_lacks_are_named_and_not_hidden(self):
        project, root, now, manifest, page = self.rendered({"typography": 1, "code-font": 1})
        content = visual.SAMPLE_CONTENT["de-DE"]; tree = PageTree(page)
        mono = next(item for item in json.loads((root / "code-report-a.json").read_bytes())["candidates"] if item["classification"] == "mono")
        facts = visual._font_facts((root / mono["asset"]["path"]).read_bytes(), "probe")
        missing = visual._missing_codepoints(facts, content["code"])
        self.assertIn("U+2192", missing)
        self.assertEqual(sorted({f"U+{ord(char):04X}" for char in content["code"] if char not in visual.ASCII_WHITESPACE and ord(char) not in facts["codepoints"]}), missing)
        self.assertEqual([], visual._missing_codepoints(facts, visual.SPECIMEN))
        card = next(nodes(self.section(tree, "code-font"), "article"))
        self.assertIn(f"nicht im Font, Darstellung aus einer Ersatzschrift: {', '.join(missing)}.", text_of(card))
        typography = text_of(next(nodes(self.section(tree, "typography"), "article")))
        for item in json.loads((root / "font-report-a.json").read_bytes())["candidates"]:
            if item["classification"] != "mono":
                lacking = visual._missing_codepoints(visual._font_facts((root / item["asset"]["path"]).read_bytes(), "probe"), visual._typeset_text(content))
                self.assertIn(f"{item['classification']} · {item['family']}: " + (f"nicht im Font, Darstellung aus einer Ersatzschrift: {', '.join(lacking)}." if lacking else "jedes Zeichen"), typography)

    def test_sample_content_is_the_generic_standard_of_the_brief_locale(self):
        schema = json.loads((visual.SCHEMA_DIR / visual.SCHEMAS["brief"]).read_bytes())
        # The brief is closed and has no place for example text of its own.
        self.assertIs(False, schema["additionalProperties"])
        self.assertFalse([name for name in schema["properties"] if any(word in name for word in ("sample", "example", "content", "specimen", "text"))])
        self.assertEqual({"const": "de-DE"}, schema["properties"]["locale"])
        self.assertEqual(["de-DE"], sorted(visual.SAMPLE_CONTENT))
        self.assertIs(visual.SAMPLE_CONTENT["de-DE"], visual._sample_content({"locale": "de-DE"}))
        for locale in ("fr-FR", "de", "", None):
            with self.subTest(locale=locale), self.assertRaisesRegex(visual.VisualDecisionError, "no generic sample content for locale"):
                visual._sample_content({"locale": locale})

    # --- domains without candidates -------------------------------------------------

    def test_a_domain_without_candidates_is_named_open_not_open_or_blocked_and_never_substituted(self):
        def states(page: str) -> dict[str, str]:
            tree = PageTree(page)
            return {kind: text_of(next(nodes(self.section(tree, kind), "p", "state"))) for kind in visual.SHEET_DOMAINS}

        def overview(page: str) -> list[list[str]]:
            table = next(nodes(next(nodes(PageTree(page).root, "div", "overview")), "tbody"))
            return [[text_of(cell) for cell in elements(row)] for row in elements(table)]

        with self.subTest(shape="open, no logo and no lockup, Affinity unavailable"):
            *_, manifest, page = self.rendered({"color": 1, "typography": 1, "code-font": 1})
            found = states(page)
            for kind in ("logo", "lockup"):
                self.assertIn("Offen, Blocker: Affinity MCP ist unavailable.", found[kind])
                section = self.section(PageTree(page), kind)
                self.assertEqual([], list(nodes(section, "img")) + list(nodes(section, "article")))
            self.assertFalse([item for item in manifest["files"] if item["path"].endswith(".svg")])
            self.assertEqual([["Farbe", "offen", "1", "0"], ["Typografie", "offen", "1", "0"], ["Code-Font", "offen", "1", "0"],
                              ["Logo", "offen", "0", "0"], ["Lockup", "offen", "0", "0"]], overview(page))
        with self.subTest(shape="open code font without candidates"):
            project, root, now, manifest, page = self.rendered({"color": 1})
            self.assertEqual("Offen: das Bundle enthält keine Kandidaten für diese Domäne.", states(page)["code-font"])
            # A sheet that shows no font and no drawing has exactly two members,
            # and the two are still required.
            self.assertEqual(["index.html", "style.css"], [item["path"] for item in manifest["files"]])
            with self.assertRaisesRegex(visual.VisualDecisionError, "sheet files: .* is too short|inventory is incomplete"):
                visual.validate_sheet_manifest({**manifest, "files": manifest["files"][:1]}, root / "sheet")
        with self.subTest(shape="not open in the brief"):
            *_, page = self.rendered({"color": 1, "typography": 1}, open_decisions=["color"])
            found = states(page)
            self.assertEqual("Im Visual Brief nicht offen.", found["code-font"])
            self.assertIn("Das Bundle enthält trotzdem Kandidaten", found["typography"])
            self.assertEqual(1, len(list(nodes(self.section(PageTree(page), "typography"), "article"))))
            self.assertEqual(["Typografie", "nicht offen", "1", "0"], overview(page)[1])
        for kind, count in (("typography", 2), ("color", 2)):
            with self.subTest(shape=f"every {kind} candidate blocked"):
                project, root, now, manifest, page = self.rendered({"color": 1, "typography": 1, kind: count}, blocked=(kind,))
                self.assertEqual(sorted(f"{kind}-{letter}" for letter in "ab"), sorted(item["id"] for item in visual.validate_bundle(project, "run-a", now)["blockers"]))
                section = self.section(PageTree(page), kind)
                self.assertEqual(f"Offen, Blocker: kein prüfbarer Kandidat. Blockiert: {count}.", states(page)[kind])
                self.assertEqual([], list(nodes(section, "article")))
                self.assertEqual([f"Kandidat {kind}-{letter}: blockiert, der Prüfbericht ist nicht review-fähig." for letter in "ab"], [text_of(node) for node in nodes(section, "li")])
                # A blocked candidate contributes nothing to the page, not even a file.
                fonts_shown = 0 if kind == "typography" else 2
                self.assertEqual(fonts_shown, len([item for item in manifest["files"] if item["path"].endswith(".woff2")]))
                self.assertEqual(fonts_shown, (root / "sheet" / "style.css").read_text(encoding="utf-8").count("@font-face"))

    def test_assets_are_named_by_content_so_reports_may_reuse_local_ids(self):
        project, root, now, manifest, page = self.rendered({"color": 1, "logo": 2, "lockup": 2}, reuse_logo_ids=True)
        reports = [json.loads((root / f"logo-report-{letter}.json").read_bytes())["candidates"] for letter in "ab"]
        self.assertEqual([item["id"] for item in reports[0]], [item["id"] for item in reports[1]])
        self.assertTrue(all(first["asset"]["sha256"] != second["asset"]["sha256"] for first, second in zip(*reports)))
        members = {item["path"]: item["sha256"] for item in manifest["files"]}
        self.assertEqual({f"assets/{item['asset']['sha256']}.svg" for report in reports for item in report}, {path for path in members if path.endswith(".svg")})
        for path, digest in members.items():
            if path.startswith("assets/"):
                self.assertEqual(path.split("/", 1)[1].split(".", 1)[0], digest)
                self.assertEqual(digest, visual.sha256_bytes((root / "sheet" / path).read_bytes()))
        # Bytes shared by several reports are one member.
        *_, manifest, page = self.rendered({"typography": 3}, distinct_fonts=False)
        self.assertEqual(2, len([item for item in manifest["files"] if item["path"].endswith(".woff2")]))
        self.assertEqual(6, page.count('<p class="specimen">'))

    def test_a_candidate_is_held_to_its_own_report_wherever_the_page_reads_it(self):
        edits = (
            ("typography", lambda value: value.update(font_candidate_ids=["serif-a", "mono-a"]), "typography report candidate/value differs"),
            ("code-font", lambda value: value.update(code_font_id="code-sans-a"), "typography report candidate/value differs"),
            ("logo", lambda value: value.update(logo_candidate_ids=["favicon-a", "mark-a"]), "logo report candidate/value differs"),
            ("lockup", lambda value: value.update(creation_origin="affinity-mcp"), "logo report candidate/value differs"),
            ("color", lambda value: value["roles"].update(accent="#003399"), "color report candidate/value differs"),
        )
        for kind, edit, reason in edits:
            with self.subTest(kind=kind):
                project, root, now = self.workspace()
                candidate_set = multi_domain_bundle(project, root, now, {domain: 1 for domain in visual.SHEET_DOMAINS})
                edit(next(item for item in candidate_set["candidates"] if item["kind"] == kind)["value"])
                rebind(root, candidate_set)
                # The public validator refuses the value first - ``prepare`` asks
                # only it, so a value the report does not back must never be
                # eligible there, whatever the sheet would say afterwards.
                with self.assertRaisesRegex(visual.VisualDecisionError, reason):
                    visual.validate_bundle(project, "run-a", now)
                brief = json.loads((root / "visual-brief.json").read_bytes()); observation = json.loads((root / "affinity-observation.json").read_bytes())
                with self.assertRaisesRegex(visual.VisualDecisionError, reason):
                    visual._render_sheet(root / "sheet", root, brief, candidate_set, observation)
                self.assertFalse((root / "sheet").exists())
        project, root, now = self.workspace()
        candidate_set = multi_domain_bundle(project, root, now, {"color": 1, "typography": 1, "code-font": 1})
        brief = json.loads((root / "visual-brief.json").read_bytes()); observation = json.loads((root / "affinity-observation.json").read_bytes())
        declared = copy.deepcopy(candidate_set); declared["candidates"][0]["status"] = "blocked"
        rebind(root, declared)
        with self.assertRaisesRegex(visual.VisualDecisionError, "candidate eligibility status differs: color-a"):
            visual._render_sheet(root / "sheet", root, brief, declared, observation)
        crossed = copy.deepcopy(candidate_set); crossed["candidates"][1]["value"] = copy.deepcopy(crossed["candidates"][2]["value"])
        rebind(root, crossed)
        with self.assertRaisesRegex(visual.VisualDecisionError, "candidate domain differs: typography-a"):
            visual._render_sheet(root / "sheet", root, brief, crossed, observation)

    # --- the document form: tables and the content model ----------------------------

    def test_a_table_is_admitted_only_in_its_structurally_closed_form(self):
        check = self.checker()
        row = "<tr><th>Nord</th><td>1.204</td></tr>"
        check(f'<div class="t"><table><thead><tr><th>Region</th><th>Q2</th></tr></thead><tbody>{row}</tbody></table></div>')
        check(f'<div class="t"><table><tbody>{row}<tr><th>Süd <strong>!</strong></th><td><code>987</code> <span>x</span></td></tr></tbody></table></div>')
        refused = (
            (f"<table>Text<tbody>{row}</tbody></table>", "text directly in <table>"),
            (f"<table> <tbody>{row}</tbody></table>", "text directly in <table>"),
            (f"<table><tbody> {row}</tbody></table>", "text directly in <tbody>"),
            (f"<table><thead>x<tr><th>a</th></tr></thead><tbody>{row}</tbody></table>", "text directly in <thead>"),
            ("<table><tbody><tr>x<td>1</td></tr></tbody></table>", "text directly in <tr>"),
            (f'<table><tbody>{row}<div class="a">x</div>{row}</tbody></table>', "<div> may not stand in <tbody>"),
            ('<table><tbody><tr><div class="a">x</div></tr></tbody></table>', "<div> may not stand in <tr>"),
            (f"<table>{row}</table>", "<tr> may not stand in <table>"),
            ("<table><tbody><td>1</td></tbody></table>", "<td> may not stand in <tbody>"),
            ('<div class="a"><td>1</td></div>', "<td> must stand directly in <tr>"),
            ('<div class="a"><tr><td>1</td></tr></div>', "<tr> must stand directly in <tbody> or <thead>"),
            ('<div class="a"><tbody><tr><td>1</td></tr></tbody></div>', "<tbody> must stand directly in <table>"),
            (f"<table><tbody><tr><td><table><tbody>{row}</tbody></table></td></tr></tbody></table>", "<table> may not stand in <td>"),
            ("<table><thead><tr><th>a</th></tr></thead></table>", "<table> has no explicit <tbody>"),
            ("<table></table>", "<table> has no explicit <tbody>"),
            (f"<table><tbody>{row}</tbody><thead><tr><th>a</th></tr></thead></table>", "<thead> is out of order in <table>"),
            (f"<table><tbody>{row}</tbody><tbody>{row}</tbody></table>", "<tbody> is out of order in <table>"),
            (f"<table><thead><tr><th>a</th></tr></thead><thead><tr><th>a</th></tr></thead><tbody>{row}</tbody></table>", "<thead> is out of order in <table>"),
            ('<table><tbody><tr><td colspan="2">1</td></tr></tbody></table>', "forbidden HTML attributes: td"),
            (f'<table class="x"><tbody>{row}</tbody></table>', "forbidden HTML attributes: table"),
            (f"<table><caption>c</caption><tbody>{row}</tbody></table>", "forbidden HTML tag: caption"),
            (f"<table><tbody>{row}</tbody><tfoot>{row}</tfoot></table>", "forbidden HTML tag: tfoot"),
            (f'<p class="a"><table><tbody>{row}</tbody></table></p>', "<table> may not stand in <p>"),
        )
        for fragment, reason in refused:
            with self.subTest(fragment=fragment), self.assertRaisesRegex(visual.VisualDecisionError, re.escape(reason)):
                check(fragment)

    def test_the_body_content_model_closes_the_tree_shapes_html5_rearranges(self):
        check = self.checker()
        check('<ul><li>a <strong>b</strong><ul><li>c</li></ul></li></ul><p class="a">x <strong>y</strong> <code>z</code> <span>w</span></p>'
              '<h2>t <span>u</span></h2><pre class="b">x\ny</pre>')
        refused = (
            ('<p class="a"><div class="b">x</div></p>', "<div> may not stand in <p>"),
            ('<p class="a"><p class="b">x</p></p>', "<p> may not stand in <p>"),
            ('<p class="a"><ul><li>x</li></ul></p>', "<ul> may not stand in <p>"),
            ('<p class="a"><pre class="b">x</pre></p>', "<pre> may not stand in <p>"),
            ('<p class="a"><section id="x"><h2>t</h2></section></p>', "<section> may not stand in <p>"),
            ("<h2><h3>x</h3></h2>", "<h3> may not stand in <h2>"),
            ('<p class="a"><strong><div class="b">x</div></strong></p>', "<div> may not stand in <strong>"),
            ('<div class="a"><li>x</li></div>', "<li> must stand directly in <ul>"),
            ('<ul><li><div class="a"><li>x</li></div></li></ul>', "<div> may not stand in <li>"),
            ("<ul><li><li>x</li></li></ul>", "<li> may not stand in <li>"),
            ('<pre class="a"><code>x</code></pre>', "<code> may not stand in <pre>"),
            ('<pre class="a">\nx</pre>', "<pre> opens with a line break the browser drops"),
            ('<pre class="a">\r\nx</pre>', "<pre> opens with a line break the browser drops"),
        )
        for fragment, reason in refused:
            with self.subTest(fragment=fragment), self.assertRaisesRegex(visual.VisualDecisionError, re.escape(reason)):
                check(fragment)


class VisualDecisionPlaygroundTests(TimeLimitedTestCase):
    """The playground: free combination, rendered from the same validated bundle, never a review artifact.

    What is measured here is what Python can state: the data is the validated
    bundle and nothing else, owner text is data and never markup, the validator
    refuses every sink, evaluation, storage and network token by name and every
    other deviation by equality, and the playground and the sheet are never
    taken for each other.  What only a browser can state - that nothing is
    stored, that owner markup stays text in the DOM, that every combination
    renders - is measured in Blink and recorded in the round's evidence.
    """

    TIME_LIMIT = 900.0

    def workspace(self) -> tuple[Path, Path, int]:
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve() / "project"; project.mkdir()
        return project, visual.visual_workspace(project, "run-a", create=True), int(time.time())

    def rendered(self, counts: dict[str, int], **shape) -> tuple[Path, Path, int, dict]:
        project, root, now = self.workspace()
        multi_domain_bundle(project, root, now, counts, **shape)
        with deadline(600, "render_playground"):
            manifest = visual.render_playground(project, "run-a", now)
        return project, root, now, manifest

    @staticmethod
    def data(root: Path) -> dict:
        raw = (root / "playground" / "data.js").read_bytes()
        return visual.load_json_bytes(raw[len(visual._PLAYGROUND_DATA_PREFIX):len(raw) - len(visual._PLAYGROUND_DATA_SUFFIX)], "probe")

    @staticmethod
    def rebound(root: Path, manifest: dict, changes: dict[str, bytes | None]) -> dict:
        """Write ``changes`` into the playground and bind them, so only the rule under test can refuse."""
        folder = root / "playground"; files = {item["path"]: item["sha256"] for item in manifest["files"]}
        for path, data in changes.items():
            if data is None:
                files.pop(path, None); continue
            (folder / path).parent.mkdir(parents=True, exist_ok=True); (folder / path).write_bytes(data); files[path] = visual.sha256_bytes(data)
        return {**manifest, "files": [{"path": path, "sha256": digest} for path, digest in sorted(files.items())]}

    def test_the_playground_offers_every_eligible_candidate_for_every_domain_count(self):
        for count in (1, 3, 5, 12):
            with self.subTest(candidates_per_domain=count):
                project, root, now, manifest = self.rendered({kind: count for kind in visual.SHEET_DOMAINS}, distinct_fonts=False)
                data = self.data(root); eligible = visual.validate_bundle(project, "run-a", now)["eligible_candidates"]
                self.assertEqual(list(visual.SHEET_DOMAINS), [axis["domain"] for axis in data["axes"]])
                for axis in data["axes"]:
                    self.assertEqual([item["id"] for item in eligible if item["kind"] == axis["domain"]], [candidate["id"] for candidate in axis["candidates"]])
                # Every class a control can switch to exists: the palette of every
                # colour, the face of every font slot, and the reference for each.
                # The classes compose independently, so this covers every one of
                # the (count + 1)**3 colour x typography x code combinations.
                css = (root / "playground" / "style.css").read_text(encoding="utf-8")
                axes = {axis["domain"]: axis["candidates"] for axis in data["axes"]}
                classes = {"pg-pal-ref", "pg-face-ref-sans", "pg-face-ref-serif", "pg-face-ref-mono"}
                classes |= {f"pg-pal-{index}" for index in range(len(axes["color"]))}
                classes |= {f"pg-face-{candidate[slot]['face']}" for candidate in axes["typography"] for slot in ("sans", "serif")}
                classes |= {f"pg-face-{candidate['mono']['face']}" for candidate in axes["code-font"]}
                for name in sorted(classes):
                    self.assertRegex(css, r"\." + re.escape(name) + r"[,{]")
                visual.validate_playground_manifest(manifest, root / "playground")
        project, root, now, manifest = self.rendered({"color": 2, "typography": 2})
        data = self.data(root)
        self.assertEqual(["color", "typography"], [axis["domain"] for axis in data["axes"]])
        self.assertEqual(["code-font", "logo", "lockup"], data["open_without_candidates"])

    def test_the_playground_data_is_the_validated_bundle_and_nothing_else(self):
        advice = [{"kind": "typography", "candidate_id": "typography-b", "rationale": "Ruhiger Grauwert", "source_ids": ["impeccable"]}]
        project, root, now, manifest = self.rendered({"color": 2, "typography": 2, "code-font": 2}, recommendations=advice, ligatures=("liga", None))
        data = self.data(root); candidate_set = json.loads((root / "candidate-set.json").read_bytes())
        self.assertEqual(visual.sha256_bytes((root / "candidate-set.json").read_bytes()), data["candidate_set_sha256"])
        self.assertEqual(manifest["candidate_set_sha256"], data["candidate_set_sha256"])
        self.assertEqual(visual.SAMPLE_CONTENT["de-DE"], data["content"]); self.assertEqual(visual.REFERENCE_PALETTE, data["reference_palette"])
        self.assertEqual([{"kind": "typography", "candidate_id": "typography-b", "rationale": "Ruhiger Grauwert"}], data["recommendations"])
        labels = {item["id"]: item["label"] for item in candidate_set["candidates"]}
        axes = {axis["domain"]: axis["candidates"] for axis in data["axes"]}
        faces = [(face["asset_sha256"], face["classification"]) for face in data["faces"]]
        for letter, offered in zip("ab", axes["color"]):
            report = json.loads((root / f"color-report-{letter}.json").read_bytes())
            self.assertEqual((f"color-{letter}", labels[f"color-{letter}"], report["roles"]), (offered["id"], offered["label"], offered["roles"]))
            self.assertEqual([{name: test[name] for name in ("foreground_role", "background_role", "usage", "minimum_ratio", "ratio_display", "passed")} for test in report["tests"]], offered["tests"])
        for letter, offered in zip("ab", axes["typography"]):
            fonts = {item["classification"]: item for item in json.loads((root / f"font-report-{letter}.json").read_bytes())["candidates"]}
            for slot in ("sans", "serif"):
                self.assertEqual((fonts[slot]["id"], fonts[slot]["family"]), (offered[slot]["font_id"], offered[slot]["family"]))
                self.assertEqual((fonts[slot]["asset"]["sha256"], slot), faces[offered[slot]["face"]])
        for letter, offered in zip("ab", axes["code-font"]):
            mono = next(item for item in json.loads((root / f"code-report-{letter}.json").read_bytes())["candidates"] if item["classification"] == "mono")
            facts = visual._font_facts((root / mono["asset"]["path"]).read_bytes(), "probe")
            self.assertEqual(list(facts["ligatures"]), offered["mono"]["ligatures"])
            self.assertEqual(visual._missing_codepoints(facts, visual.SAMPLE_CONTENT["de-DE"]["code"]), offered["mono"]["missing"])
            self.assertEqual((mono["asset"]["sha256"], "mono"), faces[offered["mono"]["face"]])
        project, root, now, manifest = self.rendered({"color": 2, "typography": 1}, blocked=("color",))
        data = self.data(root)
        self.assertEqual([{"domain": "color", "candidate_id": "color-a"}, {"domain": "color", "candidate_id": "color-b"}], data["blocked"])
        self.assertEqual(["typography"], [axis["domain"] for axis in data["axes"]])
        self.assertIn("color", data["open_without_candidates"])

    def test_the_code_block_of_the_playground_is_readable_without_scrolling_inside_it(self):
        """The sheet's code-block correction, in the second artifact of the pair.

        The presentation round fixed the sheet and left the playground on
        ``white-space:pre;overflow-x:auto``, so the same code was complete in
        one artifact and clipped in the other - measured in Blink at 320, 768
        and at 1440 with 200 % zoom.  The owner is asked to judge a code font in
        both, so both wrap.
        """
        project, root, now, manifest = self.rendered({"color": 1, "typography": 1, "code-font": 2})
        css = (root / "playground" / "style.css").read_text(encoding="utf-8")
        # Selector-exact: ".pg-code" is a substring of ".pg-face-0 .pg-code", and
        # since the faces carry sizes a substring search reads the wrong rule.
        base = [body for selector, body in css_rules(css) if selector == ".pg-code"]
        self.assertEqual(1, len(base), "the playground states its code block once")
        block = base[0]
        self.assertIn("white-space:pre-wrap", block)
        self.assertIn("overflow-wrap:break-word", block)
        self.assertNotIn("white-space:pre;", block + ";")
        # Indentation is still code, and every candidate gets the same box.
        self.assertIn("tab-size:4", block)
        for prop in ("font-size:.95rem", "padding:.75rem", "line-height:1.5", "max-width:100%"):
            self.assertIn(prop, block)
        self.assertEqual(1, len([selector for selector, _ in css_rules(css) if selector == ".pg-code"]))
        # And every face normalises that block rather than leaving it nominal: one
        # rule per face, each with a size of its own and the SAME absolute line box.
        per_face = [(selector, body) for selector, body in css_rules(css) if selector.endswith(" .pg-code")]
        self.assertEqual(len(self.data(root)["faces"]), len(per_face))
        self.assertEqual({f"line-height:{visual._rem(visual.ROLE_METRICS['code'][1])}rem"},
                         {body.split(";")[1] for _selector, body in per_face})
        # The same statement the sheet makes, so one owner is not shown two
        # different code blocks for one decision.
        sheet_block = re.search(r"\.code,\.code-liga,\.code-noliga\{([^}]*)\}", visual._sheet_css("", "")).group(1)
        for prop in ("white-space:pre-wrap", "overflow-wrap:break-word", "tab-size:4"):
            self.assertIn(prop, sheet_block)
        # And the rendered stylesheet is still exactly the one its data
        # generates - the validator holds it to that byte for byte.
        self.assertEqual((root / "playground" / "style.css").read_bytes(),
                         visual._playground_css(self.data(root)).encode("utf-8"))
        visual.validate_playground_manifest(manifest, root / "playground")

    def test_owner_markup_reaches_the_playground_only_as_json_text(self):
        family = '<img src=x onerror="alert(1)">'
        label = "</script><script>alert(2)</script> & <b>"
        # A U+2028 stood in this rationale until the separators were closed.
        # It is now refused where the candidate set enters, so it cannot reach
        # the playground to be escaped here - the character moved to the test
        # that states the refusal (see the presentation tests).  What THIS test
        # says is untouched: owner markup and non-ASCII reach data.js only as
        # escaped JSON text, and the markup, the German quotes and the astral
        # character carry that on their own.
        rationale = "<svg onload=alert(3)> „Grund“ 𝄞"
        project, root, now, manifest = self.rendered({"color": 1, "typography": 1}, serif_family=family, labels={"color-a": label},
                                                     recommendations=[{"kind": "color", "candidate_id": "color-a", "rationale": rationale, "source_ids": ["impeccable"]}])
        raw = (root / "playground" / "data.js").read_bytes()
        middle = raw[len(visual._PLAYGROUND_DATA_PREFIX):len(raw) - len(visual._PLAYGROUND_DATA_SUFFIX)]
        self.assertTrue(raw.isascii())
        for character in (b"<", b">", b"&"):
            self.assertNotIn(character, middle)
        data = self.data(root)
        self.assertEqual(family, data["axes"][1]["candidates"][0]["serif"]["family"])
        self.assertEqual(label, data["axes"][0]["candidates"][0]["label"])
        self.assertEqual(rationale, data["recommendations"][0]["rationale"])
        # The script has one way to put data into the page, and it is text.
        app = visual.PLAYGROUND_APP_JS.encode("utf-8")
        for pattern, _what in visual._PLAYGROUND_CODE_REFUSALS + visual._PLAYGROUND_SCRIPT_REFUSALS:
            self.assertIsNone(pattern.search(app), pattern.pattern)
        self.assertEqual(1, visual.PLAYGROUND_APP_JS.count(".textContent = String(text);"))
        self.assertEqual(["node.setAttribute(name, String(attributes[name]));", 'root.setAttribute("class", "pg-preview " + (color === null ? "pg-pal-ref" : "pg-pal-" + state.get("color")));'],
                         [line.strip() for line in visual.PLAYGROUND_APP_JS.splitlines() if ".setAttribute(" in line])
        self.assertEqual(["data.js", "app.js"], re.findall(r'<script src="([a-z]+\.js)" defer></script>', visual.PLAYGROUND_INDEX_HTML))
        self.assertEqual(2, visual.PLAYGROUND_INDEX_HTML.count("<script"))

    def test_the_validator_refuses_sinks_evaluation_storage_network_urls_and_inline_markup_by_name(self):
        project, root, now, manifest = self.rendered({"color": 1, "typography": 1, "code-font": 1})
        folder = root / "playground"; app = visual.PLAYGROUND_APP_JS.encode("utf-8"); page = visual.PLAYGROUND_INDEX_HTML.encode("utf-8")
        anchor = b'"use strict";\n'
        scripts = (
            ("document.body.innerHTML = source.content.heading;", "an HTML parsing sink"),
            ('root.insertAdjacentHTML("beforeend", "x");', "an HTML parsing sink"),
            ('document.write("x");', "an HTML parsing sink"),
            ("const parsed = new DOMParser();", "an HTML parsing sink"),
            ('eval("1");', "dynamic code evaluation"),
            ('new Function("return 1")();', "dynamic code evaluation"),
            ('setTimeout("render()", 1);', "dynamic code evaluation"),
            ('import("./x.js");', "dynamic code evaluation"),
            ('localStorage.setItem("k", "v");', "a storage API"),
            ('sessionStorage.setItem("k", "v");', "a storage API"),
            ('indexedDB.open("k");', "a storage API"),
            ('document.cookie = "k=v";', "a storage API"),
            ('caches.open("k");', "a storage API"),
            ('fetch("x.json");', "a network, navigation or export API"),
            ('navigator.sendBeacon("x", "y");', "a network, navigation or export API"),
            ('const worker = new Worker("w.js");', "a network, navigation or export API"),
            ('location.assign("index.html");', "a network, navigation or export API"),
            ('const link = "https://example.invalid/x";', "a remote or unsafe URL"),
            ('const scheme = "javascript:void 0";', "a remote or unsafe URL"),
            ('const hidden = "e\\u0076al";', "an escape sequence"),
        )
        for snippet, reason in scripts:
            with self.subTest(app_js=snippet):
                changed = self.rebound(root, manifest, {"app.js": app.replace(anchor, anchor + snippet.encode("utf-8") + b"\n", 1)})
                with self.assertRaisesRegex(visual.VisualDecisionError, "playground app.js contains " + re.escape(reason)):
                    visual.validate_playground_manifest(changed, folder)
        self.rebound(root, manifest, {"app.js": app})
        documents = (
            ("<script>render()</script>", "an inline script"),
            ('<script src="app.js">render()</script>', "an inline script"),
            ('<script src="https://example.invalid/x.js"></script>', "a remote or unsafe URL"),
            ('<img src="x.svg" onerror="render()">', "an inline event handler"),
            ('<p style="color:red">x</p>', "an inline style"),
            ("<style>p{color:red}</style>", "an inline style"),
            ('<iframe src="index.html"></iframe>', "a frame, embed, form, base or navigation element"),
            ('<form><input value="x"></form>', "a frame, embed, form, base or navigation element"),
            ('<a href="index.html">x</a>', "a frame, embed, form, base or navigation element"),
            ('<img src="data:image/png;base64,AA">', "a remote or unsafe URL"),
        )
        for snippet, reason in documents:
            with self.subTest(index_html=snippet):
                changed = self.rebound(root, manifest, {"index.html": page.replace(b"</main>", snippet.encode("utf-8") + b"</main>", 1)})
                with self.assertRaisesRegex(visual.VisualDecisionError, "playground index.html contains " + re.escape(reason)):
                    visual.validate_playground_manifest(changed, folder)
        # Any other deviation is refused by the equality, named or not.
        for member, changed_bytes, reason in (
                ("index.html", page.replace(b"Visueller Playground", b"Visueller Spielplatz"), "index.html differs from the generated document"),
                ("index.html", page.replace(b"script-src 'self';", b"script-src 'self' 'unsafe-inline';"), "index.html differs from the generated document"),
                ("app.js", app.replace(b"node.textContent = String(text);", b"node.textContent = String(text).trim();"), "app.js differs from the generated script")):
            with self.subTest(deviation=reason):
                changed = self.rebound(root, manifest, {member: changed_bytes, "index.html" if member == "app.js" else "app.js": page if member == "app.js" else app})
                with self.assertRaisesRegex(visual.VisualDecisionError, reason):
                    visual.validate_playground_manifest(changed, folder)
        visual.validate_playground_manifest(self.rebound(root, manifest, {"app.js": app, "index.html": page}), folder)

    def test_the_data_file_can_carry_nothing_but_its_canonical_data(self):
        project, root, now, manifest = self.rendered({"color": 1, "typography": 1},
                                                     recommendations=[{"kind": "color", "candidate_id": "color-a", "rationale": "Kontrast <stark> & ruhig", "source_ids": ["impeccable"]}])
        folder = root / "playground"; raw = (folder / "data.js").read_bytes()
        prefix, suffix = visual._PLAYGROUND_DATA_PREFIX, visual._PLAYGROUND_DATA_SUFFIX
        middle = raw[len(prefix):len(raw) - len(suffix)]; data = self.data(root)

        def encoded(value: dict) -> bytes:
            return prefix + visual._playground_json(value) + suffix

        def unused_face(value: dict) -> dict:
            """A schema-valid face no candidate points at, so the USE rule is what refuses."""
            first = value["faces"][0]
            ratio = Fraction(int(first["x_height"]), int(first["units_per_em"]))
            return {**first, "classification": "mono",
                    "x_heights": {str(weight): int(first["x_height"]) for weight in visual.ASKED_WEIGHTS},
                    "size_rem": visual._rem(visual._normalised_size(visual.ROLE_METRICS[visual.FACE_ROLE["mono"]][0], ratio))}

        typography = copy.deepcopy(data); slots = typography["axes"][1]["candidates"][0]
        slots["sans"]["face"], slots["serif"]["face"] = slots["serif"]["face"], slots["sans"]["face"]
        cases = (
            (raw + b"void 0\n", "not the generated data-only file"),
            (raw + b"alert(1);\n", "playground data is not strict JSON"),
            (raw.replace(b"Object.freeze(value);", b"void value;"), "not the generated data-only file"),
            (prefix + middle + b");alert(1" + suffix, "playground data is not strict JSON"),
            (prefix + middle + b",1" + suffix, "playground data is not strict JSON"),
            (prefix + b" " + middle + suffix, "not in its canonical data-only encoding"),
            (prefix + middle.replace(b"\\u003c", b"<") + suffix, "not in its canonical data-only encoding"),
            (prefix + json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + suffix, "not in its canonical data-only encoding"),
            (b"\xef\xbb\xbf" + raw, "carries a byte order mark"),
            (encoded({**data, "extra": 1}), "playground data <root>: Additional properties"),
            (encoded({**data, "candidate_set_sha256": "0" * 64}), "does not bind the manifest's candidate set"),
            (encoded({**data, "axes": list(reversed(data["axes"]))}), "out of domain order"),
            (encoded({**data, "blocked": [{"domain": "color", "candidate_id": "color-a"}]}), "offers a blocked candidate"),
            (encoded({**data, "recommendations": [{"kind": "typography", "candidate_id": "color-a", "rationale": "x"}]}), "cites a candidate it does not offer"),
            (encoded({**data, "open_without_candidates": ["color"]}), "both open without candidates and combinable"),
            (encoded({**data, "faces": data["faces"] + [data["faces"][0]]}), "font faces are duplicated"),
            (encoded({**data, "faces": data["faces"] + [unused_face(data)]}), "a font face no candidate uses"),
            # The one derived field of data.js is recomputed rather than believed:
            # a disclosed size that the face's own x-height does not give is refused.
            (encoded({**data, "faces": [{**data["faces"][0], "size_rem": "9.9"}] + data["faces"][1:]}),
             "discloses a size its x-height does not give"),
            (encoded({**data, "faces": [{**data["faces"][0], "x_height": data["faces"][0]["units_per_em"] + 1}] + data["faces"][1:]}),
             "states no usable x-height"),
            (encoded({**data, "faces": [{**data["faces"][0], "weights": [800, 300]}] + data["faces"][1:]}),
             "states no usable weight range"),
            (encoded(typography), "does not fit its slot"),
        )
        for bad, reason in cases:
            with self.subTest(reason=reason, size=len(bad)):
                changed = self.rebound(root, manifest, {"data.js": bad})
                with self.assertRaisesRegex(visual.VisualDecisionError, reason):
                    visual.validate_playground_manifest(changed, folder)
        visual.validate_playground_manifest(self.rebound(root, manifest, {"data.js": raw}), folder)

    def test_the_stylesheet_and_the_assets_are_bound_to_the_data(self):
        project, root, now, manifest = self.rendered({"color": 1, "typography": 1, "logo": 1})
        folder = root / "playground"; css = (folder / "style.css").read_bytes()
        font = next(item["path"] for item in manifest["files"] if item["path"].endswith(".woff2"))
        stray = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><title>x</title></svg>'
        cases = (
            ({"style.css": css + b"body{background:url(assets/x.svg)}"}, "style.css differs from the stylesheet its data generates"),
            ({"style.css": css.replace(b"--canvas:", b"--canvas:#000000;--shadow:", 1)}, "style.css differs from the stylesheet its data generates"),
            ({"assets/" + "0" * 64 + ".svg": stray}, "not named by its SHA-256"),
            ({f"assets/{visual.sha256_bytes(stray)}.svg": stray}, "asset references differ from the closed manifest"),
            ({font: None}, "asset references differ from the closed manifest"),
            ({"assets/extra.css": b"a{}"}, "not an allowed playground file"),
            ({"notes.txt": b"x"}, "not an allowed playground file"),
            ({"app.js": None}, "inventory is incomplete or duplicated"),
        )
        for changes, reason in cases:
            with self.subTest(reason=reason, changes=sorted(changes)):
                with self.assertRaisesRegex(visual.VisualDecisionError, reason):
                    visual.validate_playground_manifest(self.rebound(root, manifest, changes), folder)
                self.rebound(root, manifest, {"style.css": css})
        for field, value in (("persistence", "session"), ("confirmation_artifact", True), ("normative", True), ("preselection", True), ("interactive_controls", False)):
            with self.subTest(field=field), self.assertRaisesRegex(visual.VisualDecisionError, f"playground {field}"):
                visual.validate_playground_manifest({**manifest, field: value}, folder)
        visual.validate_playground_manifest(manifest, folder)

    def test_the_playground_and_the_sheet_are_never_taken_for_each_other(self):
        project, root, now = self.workspace()
        multi_domain_bundle(project, root, now, {kind: 1 for kind in visual.SHEET_DOMAINS})
        sheet = visual.render_sheet(project, "run-a", now); playground = visual.render_playground(project, "run-a", now)
        with self.assertRaises(visual.VisualDecisionError):
            visual.validate_sheet_manifest(playground, root / "playground")
        with self.assertRaises(visual.VisualDecisionError):
            visual.validate_playground_manifest(sheet, root / "sheet")
        # Under a sheet manifest the playground page is refused by the sheet's own form.
        forged = {**sheet, "files": [{"path": item["path"], "sha256": item["sha256"]} for item in playground["files"] if item["path"] in {"index.html", "style.css"} or item["path"].endswith(".woff2")]}
        with self.assertRaisesRegex(visual.VisualDecisionError, "forbidden HTML tag: script|markup that is not a plain tag"):
            visual.validate_sheet_manifest(forged, root / "playground")
        for folder, expected in ((root / "playground", 0), (root / "sheet", 1)):
            with self.subTest(cli_root=folder.name), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(expected, visual.main(["validate", "--kind", "playground", "--path", "manifest.json", "--root", str(folder)]))
        marker = visual.PLAYGROUND_MARKER.encode("utf-8")
        for name in (*visual.PLAYGROUND_ROOT_MEMBERS, "manifest.json"):
            self.assertIn(marker, (root / "playground" / name).read_bytes())
        for path in (root / "sheet").rglob("*"):
            if path.is_file() and path.suffix in {".html", ".css", ".json"}:
                self.assertNotIn(marker, path.read_bytes()); self.assertNotIn(b"<script", path.read_bytes().lower())
        self.assertEqual((True, True, "none", False, False, False), (playground["offline"], playground["interactive_controls"], playground["persistence"],
                                                                     playground["preselection"], playground["normative"], playground["confirmation_artifact"]))
        directives = dict(item.strip().split(" ", 1) for item in visual.PLAYGROUND_CSP.split(";"))
        self.assertEqual({"default-src": "'none'", "script-src": "'self'", "style-src": "'self'", "font-src": "'self'", "img-src": "'self'",
                          "connect-src": "'none'", "form-action": "'none'", "base-uri": "'none'", "require-trusted-types-for": "'script'"}, directives)
        self.assertIn(f'<meta http-equiv="Content-Security-Policy" content="{visual.PLAYGROUND_CSP}">', visual.PLAYGROUND_INDEX_HTML)
        with self.assertRaisesRegex(visual.VisualDecisionError, "playground output name differs"):
            visual._safe_output_dir(root / "sheet", "playground")
        brief = json.loads((root / "visual-brief.json").read_bytes()); observation = json.loads((root / "affinity-observation.json").read_bytes())
        candidate_set = json.loads((root / "candidate-set.json").read_bytes())
        with self.assertRaisesRegex(visual.VisualDecisionError, "playground output name differs"):
            visual._render_playground(root / "elsewhere", root, brief, candidate_set, observation)
        self.assertFalse((root / "elsewhere").exists())
        with self.assertRaisesRegex(visual.VisualDecisionError, "absent or empty"):
            visual.render_playground(project, "run-a", now)

    def test_the_playground_is_rendered_from_the_verified_bundle_and_spends_nothing(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve(); project = base / "project"; project.mkdir(); secrets = base / "secrets"; secrets.mkdir()
        root = visual.visual_workspace(project, "run-a", create=True); now = int(time.time())
        fixture = five_domain_affinity_bundle(project, root, secrets, now)
        genesis = fixture["ledger_path"].read_bytes()
        with self.assertRaisesRegex(visual.VisualDecisionError, "require external authentication and replay state"):
            visual.render_playground(project, "run-a", now)
        with self.assertRaisesRegex(visual.VisualDecisionError, "source contract failed"):
            visual.render_playground(project, "run-a", now, sidecar_root=base, affinity_auth=fixture["auth"])
        with deadline(300, "render_playground"):
            visual.render_playground(project, "run-a", now, affinity_auth=fixture["auth"])
        self.assertEqual(genesis, fixture["ledger_path"].read_bytes())
        logos = {axis["domain"]: axis["candidates"] for axis in self.data(root)["axes"]}
        for kind, identifiers in (("logo", ("mark-a", "favicon-a")), ("lockup", ("horizontal-a", "stacked-a"))):
            self.assertEqual([visual.sha256_bytes((root / f"staging/logo-doc/{identifier}.svg").read_bytes()) for identifier in identifiers],
                             [item["asset_sha256"] for item in logos[kind][0]["items"]])


if __name__ == "__main__": unittest.main()


class VisualDecisionSheetPresentationTests(TimeLimitedTestCase):
    """What the owner's eye actually gets, measured as effect rather than as text.

    The first real application run put three defects in front of an owner that
    every sheet test had passed over, each for the same reason: the tests asked
    whether a declaration was *present*, and none asked what a browser would do
    with the stylesheet as a whole.

    * Every swatch was black.  The generated ``border-left-color`` stood in
      front of the ``.swatch`` rule, whose ``border-left`` shorthand then reset
      the colour to ``currentColor``.  Both rules have one class, so document
      order decides, and the generated one lost.  A test that greps for
      ``border-left-color:#8C1C1C`` passes on that sheet.  The test below
      therefore resolves the cascade for the element that carries both classes
      and asks what colour survives.
    * No ``@font-face`` carried a ``font-weight`` descriptor, so a heading at
      700 was synthesised: the browser smeared an outline the file does not
      contain, and the owner judged a weight nobody drew.
    * The code block was wider than its card and only readable by scrolling
      inside it.

    The properties are stated per candidate, because the range is a property of
    the file; the *sizes* stay equal across candidates, which is what makes the
    candidates comparable at all.
    """

    TIME_LIMIT = 900.0

    # --- reading a stylesheet the way a browser resolves it -----------------------

    @staticmethod
    def declarations(css: str) -> list[tuple[str, str]]:
        """Top-level rules as (selector list, declaration block), at-rules skipped.

        Only the rules that decide the swatch are needed, and none of them sits
        inside an at-rule; an at-rule is skipped over by brace depth rather than
        parsed, so a nested block cannot be mistaken for a top-level rule.
        """
        out: list[tuple[str, str]] = []; index = 0
        while index < len(css):
            brace = css.find("{", index)
            if brace < 0: break
            selector = css[index:brace].strip()
            if selector.startswith("@"):
                depth, cursor = 1, brace + 1
                while cursor < len(css) and depth:
                    depth += (css[cursor] == "{") - (css[cursor] == "}"); cursor += 1
                index = cursor; continue
            end = css.find("}", brace)
            out.append((selector, css[brace + 1:end])); index = end + 1
        return out

    @classmethod
    def resolved(cls, css: str, classes: set[str], prop: str) -> str | None:
        """The value of ``prop`` on an element carrying ``classes``, after the cascade.

        Every selector that decides this element is a list of class selectors of
        equal specificity, so document order alone decides - which is exactly
        the rule the defect turned on.  ``border-left`` is expanded the way CSS
        expands a shorthand: an omitted colour component is reset to
        ``currentColor`` rather than left alone.
        """
        value = None
        for selector, block in cls.declarations(css):
            if not any(part.startswith(".") and " " not in part and set(part[1:].split(".")) <= classes
                       for part in (piece.strip() for piece in selector.split(","))):
                continue
            for declaration in block.split(";"):
                if ":" not in declaration: continue
                name, raw = declaration.split(":", 1); name = name.strip()
                if name == "border-left":
                    parts = raw.split(); value = parts[-1] if len(parts) > 2 else "currentColor"
                elif name == prop:
                    value = raw.strip()
        return value

    def rendered(self, counts: dict | None = None, **shape) -> tuple[Path, Path, str, str]:
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve() / "project"; project.mkdir()
        root = visual.visual_workspace(project, "run-a", create=True)
        now = int(time.time())
        multi_domain_bundle(project, root, now, counts or {"color": 2, "typography": 2, "code-font": 2}, **shape)
        with deadline(600, "render_sheet"):
            visual.render_sheet(project, "run-a", now)
        return project, root, (root / "sheet/style.css").read_text(encoding="utf-8"), (root / "sheet/index.html").read_text(encoding="utf-8")

    # --- point 1: the colour that survives the cascade ----------------------------

    def test_every_swatch_shows_its_own_colour_after_the_cascade_and_not_only_in_its_rule(self):
        project, root, css, page = self.rendered()
        candidate_set = json.loads((root / "candidate-set.json").read_bytes())
        palettes = [json.loads((root / item["value"]["report"]["path"]).read_bytes())["roles"]
                    for item in candidate_set["candidates"] if item["kind"] == "color"]
        self.assertTrue(palettes, "the fixture has to carry colour candidates")
        seen = 0
        for index, roles in enumerate(palettes):
            for role_index, (role, colour) in enumerate(roles.items()):
                classes = {"swatch", f"swatch-{index}-{role_index}"}
                with self.subTest(swatch=f"{index}-{role_index}", role=role):
                    self.assertEqual(colour, self.resolved(css, classes, "border-left-color"),
                                     "the swatch resolves to a colour that is not its own")
                    self.assertNotEqual("currentColor", self.resolved(css, classes, "border-left-color"))
                seen += 1
        self.assertGreater(seen, 10)
        # The cause, not the symptom: the width and the style are stated as
        # longhands, so no later shorthand can reset the colour again - the
        # generated rules keep working whichever side of the base rule they
        # land on.
        base = next(block for selector, block in self.declarations(css) if selector == ".swatch")
        self.assertNotIn("border-left:", base)
        self.assertIn("border-left-width:3rem", base)
        self.assertIn("border-left-style:solid", base)
        swapped = visual._sheet_css("", ".swatch-9-9{border-left-color:#123456}")
        self.assertEqual("#123456", self.resolved(swapped, {"swatch", "swatch-9-9"}, "border-left-color"),
                         "the rule order must not decide the colour any more")

    # --- point 2: the weights the file really has ---------------------------------

    @staticmethod
    def variable_font(family: str, classification: str, low: int, high: int, default: int | None = None) -> bytes:
        """The fixture font with a real ``wght`` axis, so the range comes out of ``fvar``.

        ``default`` is the instance the file rests at.  It matters: a real
        variable font usually sits at 400 inside a wider range, so a reader that
        mistakes the default for the minimum understates what the font can draw.
        """
        scratch = Path(tempfile.mkdtemp())
        try:
            asset, _source = make_font(scratch / "f.woff2", family, classification)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        font = TTFont(io.BytesIO(asset))
        axis = Axis(); axis.axisTag = "wght"; axis.minValue = low
        axis.defaultValue = low if default is None else default; axis.maxValue = high
        axis.axisNameID = 256
        font["name"].setName("Weight", 256, 3, 1, 0x409)
        table = newTable("fvar"); table.axes = [axis]; table.instances = []
        font["fvar"] = table
        buffer = io.BytesIO(); font.flavor = "woff2"; font.save(buffer)
        return buffer.getvalue()

    @staticmethod
    def reweighted(asset: bytes, weight: int) -> bytes:
        """The same static font with another ``usWeightClass``."""
        font = TTFont(io.BytesIO(asset)); font["OS/2"].usWeightClass = weight
        buffer = io.BytesIO(); font.flavor = "woff2"; font.save(buffer)
        return buffer.getvalue()

    def test_the_weight_range_of_a_face_is_read_out_of_the_font_and_nowhere_else(self):
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        static, _source = make_font(scratch / "static.woff2", "Fixture Static", "sans")
        # A static font has exactly the one weight its OS/2 table declares.
        self.assertEqual((400, 400), visual._font_facts(static, "static")["weights"])
        self.assertEqual((700, 700), visual._font_facts(self.reweighted(static, 700), "static")["weights"])
        # A variable font states minimum and maximum on its wght axis, and the
        # axis wins over the single value OS/2 still carries.
        for low, high in ((300, 800), (100, 900), (200, 400)):
            with self.subTest(axis=(low, high)):
                facts = visual._font_facts(self.variable_font("Fixture Variable", "sans", low, high), "variable")
                self.assertEqual((low, high), facts["weights"])
        # The bounds come from the axis, never from the instance the file rests
        # at: a font that sits at 400 in a 100-900 axis can still draw 100 and
        # 900, and a sheet that declared 400-900 would understate it.
        for low, default, high in ((100, 400, 900), (200, 300, 800), (300, 300, 700)):
            with self.subTest(default=default):
                facts = visual._font_facts(self.variable_font("Fixture Variable", "sans", low, high, default=default), "variable")
                self.assertEqual((low, high), facts["weights"])
        # Fail-closed: a font that states no usable weight is refused here
        # rather than rendered with a guess.
        with self.assertRaisesRegex(visual.VisualDecisionError, "states no usable weight range"):
            visual._font_facts(self.reweighted(static, 0), "zero")

    def test_no_face_of_the_sheet_is_asked_for_a_weight_its_font_cannot_draw(self):
        # The clamp, as a table: what the sheet asks for against what the file
        # has.  400 is the body text, 600 a subheading, 700 a heading, a table
        # head and a code label.
        cases = {
            (100, 900): (400, 600, 700), (300, 800): (400, 600, 700), (400, 400): (400, 400, 400),
            (500, 500): (500, 500, 500), (200, 400): (400, 400, 400), (600, 900): (600, 600, 700),
        }
        for weights, (body, medium, strong) in cases.items():
            with self.subTest(range=weights):
                # One clamp for both twins since the playground caught up, so the
                # sheet's rule and the playground's are measured through the very
                # same function rather than trusted to agree.
                for prefix, selectors, medium_selector in (("face", visual.SHEET_WEIGHT_SELECTORS, ".t-subheading"),
                                                           ("pg-face", visual.PLAYGROUND_WEIGHT_SELECTORS, ".pg-subheading")):
                    with self.subTest(artifact=prefix):
                        rules = visual._face_weight_rules(prefix, 3, weights, selectors)
                        self.assertIn(f".{prefix}-3{{font-weight:{body}}}", rules)
                        self.assertIn(f".{prefix}-3 {medium_selector}{{font-weight:{medium}}}", rules)
                        self.assertIn(f"font-weight:{strong}}}", rules)
                        for asked in re.findall(r"font-weight:(\d+)", rules):
                            self.assertTrue(weights[0] <= int(asked) <= weights[1], f"{asked} lies outside {weights}")
                        # Every selector of the map is prefixed with the face, so a
                        # clamp can never land on an element outside it.
                        for selector in selectors.values():
                            for part in selector.split(","):
                                self.assertIn(f".{prefix}-3 {part}", rules)
        self.assertEqual("400", visual._weight_descriptor((400, 400)))
        self.assertEqual("300 800", visual._weight_descriptor((300, 800)))
        # And on a rendered sheet: every face declares its range, and every
        # weight the page asks of that face lies inside it.
        project, root, css, page = self.rendered()
        faces = re.findall(r'@font-face\{font-family:"VisualCandidate(\d+)";[^}]*font-weight:([0-9]+)(?: ([0-9]+))?\}', css)
        self.assertTrue(faces, "the sheet renders no font face")
        for index, low, high in faces:
            declared = (int(low), int(high or low))
            asked = [int(value) for value in re.findall(rf"\.face-{index}(?:[^{{,]*)\{{font-weight:(\d+)\}}", css)]
            with self.subTest(face=index):
                self.assertTrue(asked, "a face without a stated weight leaves the browser to synthesise")
                for value in asked:
                    self.assertTrue(declared[0] <= value <= declared[1], f"face {index} is asked for {value}, has {declared}")

    # --- point 4: the code block is all there -------------------------------------

    def test_the_code_block_of_every_candidate_is_readable_without_scrolling_inside_it(self):
        project, root, css, page = self.rendered(counts={"color": 1, "typography": 1, "code-font": 3})
        block = next(body for selector, body in self.declarations(css) if selector == ".code,.code-liga,.code-noliga")
        # Wrapped rather than clipped: the long line of the sample breaks at a
        # space, and a token longer than the card breaks inside itself, so no
        # part of the code needs an inner scrollbar to be read.
        self.assertIn("white-space:pre-wrap", block)
        self.assertIn("overflow-wrap:break-word", block)
        self.assertNotIn("white-space:pre;", block + ";")
        # Indentation is still code: pre-wrap keeps leading whitespace and the
        # tab stop, which is what a reader judges a code font by.
        self.assertIn("tab-size:4", block)
        # Same measurements for every candidate: one rule serves all three code
        # classes, so no candidate gets a wider box than another.
        self.assertEqual(1, len([1 for selector, _ in self.declarations(css) if selector == ".code,.code-liga,.code-noliga"]))
        for prop in ("font-size:.95rem", "padding:.75rem", "line-height:1.5"):
            self.assertIn(prop, block)
        # Nothing on the page may need a horizontal page scrollbar; the page
        # box and every wide child stay inside their container.  The browser
        # measurement of this is the owner's visual check - here the rules the
        # page relies on are stated.
        page_rules = dict(self.declarations(css))
        self.assertIn("max-width:100%", page_rules["pre"])
        self.assertIn("overflow-wrap:anywhere", page_rules["main"])

    # --- point 84: what the sheet used to carry raw while the review escaped it ---

    SEPARATORS = ("\u0085", "\u2028", "\u2029")
    BIDI = ("\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c",
            "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069")

    def refused(self, message: str, **shape) -> None:
        """One bundle whose owner text carries a mark, rendered and refused by name."""
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve() / "project"; project.mkdir()
        root = visual.visual_workspace(project, "run-a", create=True)
        now = int(time.time())
        multi_domain_bundle(project, root, now, {"color": 1, "typography": 1}, **shape)
        with self.assertRaisesRegex(visual.VisualDecisionError, re.escape(message)), deadline(600, "render_sheet"):
            visual.render_sheet(project, "run-a", now)

    def test_no_owner_string_puts_a_separator_or_a_direction_switch_into_the_sheet(self):
        """The half of the ``ensure_ascii`` point that stood open in the sheet.

        The filled review writes the same owner strings with
        ``ensure_ascii=True`` and has escaped these since the presentation
        round.  The sheet carried all fifteen raw, because they are not C0
        controls: a family name out of a manipulated ``name`` table was one
        record on the page and two lines in a terminal, and a bidi override
        showed the owner a name that reads differently from the bytes he was
        about to confirm.
        """
        marks = self.SEPARATORS + self.BIDI
        # The class as a SET, not as a handful of examples: the C0 controls the
        # document form always refused, plus the three line terminators and the
        # twelve bidi formatting characters, and nothing else in the BMP or the
        # first supplementary plane.
        expected = (set(range(0x00, 0x09)) | {0x0b, 0x0c} | set(range(0x0e, 0x20)) | {0x7f}
                    | {ord(mark) for mark in marks})
        reachable = [code for code in range(0x11000) if not 0xD800 <= code <= 0xDFFF]
        self.assertEqual(expected, {code for code in reachable if visual._SHEET_CONTROL.search(chr(code))})
        # The playground states the same thing on bytes, before any decode, and
        # it has to be the SAME set: two patterns that may drift apart are two
        # rules, and the point of this round is that there is one.
        self.assertEqual(expected, {code for code in reachable
                                    if visual._PLAYGROUND_CONTROL.search(chr(code).encode("utf-8"))})
        # Not a wall: the three whitespace characters the document form needs stay,
        # and ordinary German prose is untouched.  U+000C is the one HTML5
        # whitespace character this class owns - it is a str.splitlines() boundary,
        # so a family name carrying one made the sheet read as more lines for a
        # terminal, a diff or a log than for a reader counting U+000A - and it is
        # stated here rather than left to be inferred from the set above.
        for character in "\t\n\r":
            self.assertIsNone(visual._SHEET_CONTROL.search(character))
        self.assertIsNotNone(visual._SHEET_CONTROL.search("\x0c"))
        self.assertEqual(["\x0c"], sorted(WHITESPACE_CONTROLS))
        # The class is complete against BOTH effects it exists for, measured as a
        # set: every str.splitlines() boundary except the two the document form
        # needs, and the whole of Unicode's Bidi_Control.
        boundaries = {code for code in reachable if len(f"a{chr(code)}b".splitlines()) > 1}
        self.assertEqual({0x0a, 0x0d}, boundaries - expected)
        self.assertLessEqual({0x061c, 0x200e, 0x200f, *range(0x202a, 0x202f), *range(0x2066, 0x206a)}, expected)
        self.assertIsNone(visual._SHEET_CONTROL.search("Fließtext · Überschrift — „Größen“"))

        # Every mark on every owner-supplied channel, through the predicate the
        # three call sites share.  Rendering all forty-five would buy nothing
        # but a font build per family; the channels themselves are rendered
        # below for one separator and one override each.
        for mark in marks + tuple(sorted(WHITESPACE_CONTROLS)):
            for name in ("font family serif-a", "recommendation rationale color-a", "candidate label color-a"):
                with self.subTest(mark=f"U+{ord(mark):04X}", channel=name), \
                     self.assertRaisesRegex(visual.VisualDecisionError,
                                            re.escape(name) + " contains a forbidden control character"):
                    visual._reject_sheet_control(f"Fixture{mark}Wert", name)
        for mark in ("\u2028", "\u202e", "\x0c"):
            with self.subTest(family=f"U+{ord(mark):04X}"):
                self.refused("font family serif-a contains a forbidden control character",
                             serif_family=f"Fixture{mark}Serif")
            with self.subTest(rationale=f"U+{ord(mark):04X}"):
                self.refused("recommendation rationale color-a contains a forbidden control character",
                             recommendations=[{"kind": "color", "candidate_id": "color-a",
                                               "rationale": f"Beste{mark}Lesbarkeit", "source_ids": ["impeccable"]}])
            # The label reaches the playground rather than the sheet, and it is
            # held by the same rule so a later reader cannot find two channels
            # closed and the third open.
            with self.subTest(label=f"U+{ord(mark):04X}"):
                self.refused("candidate label color-a contains a forbidden control character",
                             labels={"color-a": f"Palette{mark}A"})

        # The positive leg, on all three channels at once: legitimate German
        # text renders, and what comes out is one document for every reader -
        # the property the raw separators took away.
        project, root, css, page = self.rendered(
            counts={"color": 1, "typography": 1, "code-font": 1},
            serif_family="Fixture Serif Gross",
            labels={"color-a": "Palette „A“ — groß"},
            recommendations=[{"kind": "color", "candidate_id": "color-a",
                              "rationale": "Beste Lesbarkeit — auch bei 200 %.", "source_ids": ["impeccable"]}])
        self.assertIn(html.escape("Beste Lesbarkeit — auch bei 200 %."), page)
        for mark in marks + tuple(sorted(WHITESPACE_CONTROLS)):
            with self.subTest(absent=f"U+{ord(mark):04X}"):
                self.assertNotIn(mark, page)
                self.assertNotIn(mark, css)
        self.assertEqual(page.count("\n"), len(page.splitlines()), "the sheet reads as two different line counts")
        self.assertEqual(css.count("\n"), len(css.splitlines()), "the stylesheet reads as two different line counts")

    # --- point 88: the range belongs to the face, not to the first face ----------

    def test_every_face_declares_the_range_of_its_own_font_and_clamps_only_inside_it(self):
        """Six faces, six different ranges - the shape that makes the rule visible.

        Every fixture font was ``usWeightClass`` 400 until now, so all faces of
        a rendered sheet had one and the same range and clamping each face
        against the FIRST face's range produced a byte-identical stylesheet.
        The rule was carried by evidence from one real bundle, not by a test,
        and mutant M24 walked through the gap.  With ranges that differ, the
        two readings disagree on every face but the first.
        """
        ranges = {"serif-a": (100, 900), "sans-a": (400, 400), "serif-b": (300, 700),
                  "sans-b": (600, 900), "code-mono-a": (500, 500), "code-mono-b": (200, 800)}
        project, root, css, page = self.rendered(counts={"color": 1, "typography": 2, "code-font": 2},
                                                 font_weights=ranges)
        # Which range belongs to which face is read from the REPORTS, by asset
        # digest - not from the function under test.
        expected: dict[str, tuple[int, int]] = {}
        for path in sorted(root.glob("*report-*.json")):
            for item in json.loads(path.read_bytes()).get("candidates", []):
                if isinstance(item, dict) and item.get("id") in ranges:
                    expected[item["asset"]["sha256"]] = ranges[item["id"]]
        self.assertEqual(len(ranges), len(expected), "the fixture did not give every font its own bytes")

        faces = re.findall(r'@font-face\{font-family:"VisualCandidate(\d+)";src:url\("assets/([0-9a-f]{64})\.woff2"\)'
                           r'[^}]*font-weight:([0-9]+)(?: ([0-9]+))?\}', css)
        self.assertEqual(len(ranges), len(faces), "the sheet does not show one face per candidate font")
        declared: dict[str, tuple[int, int]] = {}
        for index, digest, low, high in faces:
            declared[index] = (int(low), int(high or low))
            with self.subTest(face=index):
                self.assertIn(digest, expected, "a face shows a font no report binds")
                self.assertEqual(expected[digest], declared[index],
                                 "a face declares a range that is not the range of its own font")
        # Without this the test would pass on the old fixtures and measure
        # nothing at all.
        self.assertEqual(len(ranges), len(set(declared.values())), "the faces have to differ to make the rule visible")

        for index, (low, high) in declared.items():
            asked = [int(value) for value in re.findall(rf"\.face-{index}(?:[^{{,]*)\{{font-weight:(\d+)\}}", css)]
            with self.subTest(clamped=index):
                # The sheet asks a face for 400, 600 and 700, each clamped into
                # THIS face's range - not into any other's.
                self.assertEqual({min(max(weight, low), high) for weight in (400, 600, 700)}, set(asked))
                for value in asked:
                    self.assertTrue(low <= value <= high, f"face {index} has {low}-{high} and is asked for {value}")

    # --- points 86 and 87: the two bounds of the fail-closed guard ---------------

    def test_a_weight_range_outside_what_a_weight_can_mean_is_refused_not_guessed(self):
        """Both bounds of the guard, each with a font that really states it.

        The guard's EXISTENCE was measured (a ``usWeightClass`` of 0 is
        refused); its two bounds were not, and mutants M14 and M15 removed them
        without a red test.  A range whose minimum exceeds its maximum and a
        range above 1000 are both things a font file can say and CSS cannot
        mean.
        """
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        static, _source = make_font(scratch / "static.woff2", "Fixture Static", "sans")
        # Point 86 - the axis states its bounds the wrong way round.  Refusing
        # is the only honest answer: which of the two is the minimum is not
        # something the renderer may decide for the file.
        for low, high in ((800, 300), (900, 100), (401, 400)):
            with self.subTest(swapped=(low, high)), \
                 self.assertRaisesRegex(visual.VisualDecisionError, f"states no usable weight range: {low}-{high}"):
                visual._font_facts(self.variable_font("Fixture Swapped", "sans", low, high, default=low), "swapped")
        # Point 87 - above 1000, on both of the two sources.
        for weight in (1001, 1500, 65535):
            with self.subTest(over=weight), \
                 self.assertRaisesRegex(visual.VisualDecisionError, f"states no usable weight range: {weight}-{weight}"):
                visual._font_facts(self.reweighted(static, weight), "over")
        with self.assertRaisesRegex(visual.VisualDecisionError, "states no usable weight range: 400-1200"):
            visual._font_facts(self.variable_font("Fixture Over", "sans", 400, 1200), "over-axis")
        # And the bounds themselves stay usable, so this is a rule and not a
        # wall: 1 and 1000 are weights, and a font that states them renders.
        for low, high in ((1, 1), (1000, 1000), (1, 1000), (400, 400)):
            with self.subTest(edge=(low, high)):
                asset = (self.reweighted(static, low) if low == high
                         else self.variable_font("Fixture Edge", "sans", low, high, default=low))
                self.assertEqual((low, high), visual._font_facts(asset, "edge")["weights"])


class VisualDecisionNormalisedSpecimenTests(TimeLimitedTestCase):
    """One x-height for every candidate, and the digit sets the files really carry.

    At one declared size a sheet compares x-heights instead of fonts.  Measured
    over a real catalogue the ratio runs from 0.413 to 0.550 - a third more
    letter for the same declared size - so the owner could not tell a narrow face
    from a small-running one, and in practice the size would have been adjusted.
    The sheet adjusts it per candidate, keeps the line box absolutely equal, and
    discloses the size it arrived at so nobody reads it as a recommendation.

    Every fixture here carries an x-height of its OWN (see ``x_height_font``):
    with the plain fixtures all faces share one ratio, and normalising each face
    against the first face's ratio would produce a byte-identical stylesheet -
    the same blind spot the weight range had before ``weighted_font``.
    """

    TIME_LIMIT = 900.0

    #: Eight faces, eight different x-heights, so no two candidates can agree by
    #: accident and a rule that reads one face's ratio for all of them is red.
    HEIGHTS = {"serif-a": 420, "sans-a": 520, "serif-b": 460, "sans-b": 540,
               "serif-c": 500, "sans-c": 480, "code-mono-a": 550, "code-mono-b": 430}

    def rendered(self, counts=None, **shape):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve() / "project"; project.mkdir()
        root = visual.visual_workspace(project, "run-a", create=True); now = int(time.time())
        multi_domain_bundle(project, root, now, counts or {"typography": 3, "code-font": 2}, **shape)
        with deadline(600, "render_sheet"):
            visual.render_sheet(project, "run-a", now)
        return (project, root, now, (root / "sheet/style.css").read_text(encoding="utf-8"),
                (root / "sheet/index.html").read_text(encoding="utf-8"))

    @staticmethod
    def faces(css: str, folder: Path, prefix: str, fam: str, selectors: dict) -> tuple[dict, dict, dict]:
        """(ratio, classification, metrics) per face, read from the artifact itself.

        The ratio comes from ``OS/2.sxHeight`` read straight off the asset, never
        from the function under test, so the expected value is independent of the
        rule being measured.
        """
        assets = dict(re.findall(rf'@font-face\{{font-family:"{fam}(\d+)";src:url\("assets/([0-9a-f]{{64}})\.woff2"\)', css))
        ratio, klass = {}, {}
        generic = {"serif": "serif", "sans-serif": "sans", "monospace": "mono"}
        for index, digest in assets.items():
            stated, upem = sx_height((folder / f"assets/{digest}.woff2").read_bytes())
            ratio[index] = Fraction(stated, upem)
            found = re.search(rf'\.{prefix}-{index},\.{prefix}-{index} pre'
                              rf'\{{font-family:"{fam}{index}",([a-z-]+);font-optical-sizing:none\}}', css)
            klass[index] = generic[found.group(1)]
        first_of = {sel.split(",")[0]: role for role, sel in selectors.items()}
        metrics = {}
        for selector, block in css_rules(css):
            match = re.fullmatch(rf"\.{prefix}-(\d+) ([^,]+)(?:,.*)?", selector)
            if match is None:
                continue
            role = first_of.get(match.group(2))
            size = re.search(r"font-size:([0-9.]+)rem", block)
            line = re.search(r"line-height:([0-9.]+)rem", block)
            if role and size and line:
                metrics[(match.group(1), role)] = (Fraction(size.group(1)), line.group(1))
        return ratio, klass, metrics

    def test_every_candidate_of_a_role_is_set_to_the_same_effective_x_height(self):
        project, root, now, css, page = self.rendered(font_x_heights=self.HEIGHTS)
        ratio, klass, metrics = self.faces(css, root / "sheet", "face", "VisualCandidate", visual.SHEET_ROLE_SELECTORS)
        self.assertEqual(8, len(ratio), "the fixture has to show eight faces")
        self.assertGreaterEqual(len(set(ratio.values())), 3,
                                "without differing x-heights this test measures nothing at all")
        for role in visual.SHEET_ROLE_SELECTORS:
            with self.subTest(role=role):
                nominal, line = visual.ROLE_METRICS[role]
                target = nominal * visual.X_HEIGHT_TARGET
                effective = {index: metrics[(index, role)][0] * ratio[index] for index in ratio}
                self.assertEqual(len(ratio), len(effective), "every face sets every role it can reach")
                # Both bounds come out of the ONE rounding this module does, and
                # neither is guessed: a single candidate is at most half a step
                # from the target, and two candidates at most a whole step apart,
                # because they may round in opposite directions.
                for index, value in effective.items():
                    self.assertLessEqual(abs(value - target), (visual.SIZE_QUANTUM / 2) * ratio[index],
                                         f"face {index} misses the target x-height by more than the rounding")
                self.assertLessEqual(max(effective.values()) - min(effective.values()),
                                     visual.SIZE_QUANTUM * max(ratio.values()),
                                     "two candidates differ in x-height by more than one rounding step")
                # The sizes really do differ - otherwise the equality above would
                # be the trivial one and the normalisation would not be measured.
                self.assertGreater(len({metrics[(index, role)][0] for index in ratio}), 1)
                # The line box is an absolute length and the SAME for every
                # candidate, so rows stay pitch for pitch comparable.
                self.assertEqual({visual._rem(line)}, {metrics[(index, role)][1] for index in ratio})

    def test_the_disclosed_size_is_the_size_that_is_set_and_names_its_source(self):
        project, root, now, css, page = self.rendered(font_x_heights=self.HEIGHTS)
        ratio, klass, metrics = self.faces(css, root / "sheet", "face", "VisualCandidate", visual.SHEET_ROLE_SELECTORS)
        disclosed = re.findall(r"x-Höhe (\d+) von (\d+) Einheiten \(([0-9.]+) em, ([^)]+)\), "
                               r"gelesen an der gezeichneten Instanz bei Gewicht (\d+)\. "
                               r"Gewichte: [^.]+\. Gewichtsbereich (\d+) bis (\d+)\. "
                               r"Auf gleiche x-Höhe normalisiert, Grad ([0-9.]+) rem", page)
        self.assertEqual(len(ratio), len(disclosed), "every shown font discloses what it was normalised on")
        for stated, upem, shown_ratio, source, weight, low, high, grade in disclosed:
            with self.subTest(x_height=f"{stated}/{upem}"):
                value = Fraction(int(stated), int(upem))
                index = next(key for key, item in ratio.items() if item == value)
                role = visual.FACE_ROLE[klass[index]]
                # The weight the x-height is stated for is the weight that role
                # is really drawn in, and the range is the file's own - the sheet
                # used to name neither, which is what made the twins asymmetric.
                self.assertEqual(visual.SHEET_ROLE_WEIGHTS[role], int(weight))
                self.assertLessEqual(int(low), int(high))
                # The number the sheet PRINTS is the number the stylesheet SETS.
                self.assertEqual(metrics[(index, role)][0], Fraction(grade))
                self.assertEqual(f"{float(value):.3f}", shown_ratio)
                self.assertEqual("OS/2.sxHeight", source)
        self.assertIn("Der Grad folgt aus der Normalisierung und ist keine Empfehlung.", page)

    def test_the_x_height_is_the_stated_metric_and_the_glyph_is_only_its_substitute(self):
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        # The metric wins over the glyph even where the two disagree - on the real
        # catalogue they disagree for six faces of fourteen, and the file's own
        # statement is the one its designer made.
        stated, _source = x_height_font(scratch / "stated.woff2", "Fixture Stated", "serif", 480)
        facts = visual._font_facts(stated, "stated")
        self.assertEqual((480, 1000, "os2-sxheight"), (facts["x_height"], facts["units_per_em"], facts["x_height_source"]))
        self.assertNotEqual(480, 700, "the fixture glyph is 700 units tall, so the two sources differ here")
        # With no metric the glyph the metric is named after is measured.
        measured, _source = x_height_font(scratch / "measured.woff2", "Fixture Measured", "serif", 0)
        facts = visual._font_facts(measured, "measured")
        self.assertEqual((700, 1000, "glyph-x"), (facts["x_height"], facts["units_per_em"], facts["x_height_source"]))
        # Neither source: refused by name rather than set at a size nothing
        # justifies - the same shape the weight guard has.
        blind, _source = x_height_font(scratch / "blind.woff2", "Fixture Blind", "serif", 0, drop_x=True)
        with self.assertRaisesRegex(visual.VisualDecisionError, "states no usable x-height: 0/1000"):
            visual._font_facts(blind, "blind")
        # And a metric outside what an x-height can mean is refused, not scaled.
        for value in (1001, 4000):
            with self.subTest(over=value), \
                 self.assertRaisesRegex(visual.VisualDecisionError, f"states no usable x-height: {value}/1000"):
                visual._font_facts(x_height_font(scratch / f"over{value}.woff2", f"Fixture Over {value}", "serif", value)[0], "over")

    def test_the_reference_is_half_the_em_and_a_font_at_it_keeps_its_nominal_size(self):
        """The target itself, and the meaning that makes it a reference point.

        Every other test here forms its expectation FROM ``X_HEIGHT_TARGET``, so a
        mutant that moves the target moves both sides of the comparison and passes
        unnoticed - which is exactly what mutant M21 did.  The value is therefore
        stated, and so is the consequence that gives it meaning: a font whose
        x-height IS half its em is set at the nominal size of its role, so the
        normalisation is a reference point and not a rescaling of everything.
        """
        self.assertEqual(Fraction(1, 2), visual.X_HEIGHT_TARGET)
        self.assertEqual(Fraction(1, 10000), visual.SIZE_QUANTUM)
        # The nominal sizes are pinned as text, so a moved role size is red too.
        for role, spelled in (("specimen", "1.15"), ("heading", "1.6"), ("subheading", "1.2"),
                              ("body", "1.05"), ("table-head", "1"), ("table-cell", "1"),
                              ("code", "0.95"), ("numerals", "1.15")):
            with self.subTest(nominal=role):
                self.assertEqual(spelled, visual._rem(visual.ROLE_METRICS[role][0]))
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        half, _source = x_height_font(scratch / "half.woff2", "Fixture Half Em", "serif", 500)
        facts = visual._font_facts(half, "half")
        self.assertEqual(Fraction(1, 2), visual._x_height_ratio(facts))
        for role, (nominal, _line) in visual.ROLE_METRICS.items():
            with self.subTest(role=role):
                self.assertEqual(nominal, visual._normalised_size(nominal, visual._x_height_ratio(facts)))
        # A font at twice that x-height gets half the size, and one at half it
        # twice - the direction of the scaling, stated on real bytes.
        wide, _source = x_height_font(scratch / "wide.woff2", "Fixture Wide Em", "serif", 1000)
        narrow, _source = x_height_font(scratch / "narrow.woff2", "Fixture Narrow Em", "serif", 250)
        body = visual.ROLE_METRICS["body"][0]
        self.assertEqual(body / 2, visual._normalised_size(body, visual._x_height_ratio(visual._font_facts(wide, "wide"))))
        self.assertEqual(body * 2, visual._normalised_size(body, visual._x_height_ratio(visual._font_facts(narrow, "narrow"))))

    #: Eight faces whose outline really carries the height, four of them with a
    #: gvar delta so their ``x`` MOVES along the weight axis - the effect eight of
    #: twelve real variable faces have and no fixture had before.
    INK = {"serif-a": (420, 0), "sans-a": (520, 24), "serif-b": (460, 0), "sans-b": (540, 18),
           "serif-c": (500, 12), "sans-c": (480, 0), "code-mono-a": (550, 30), "code-mono-b": (430, 0)}
    INK_WEIGHTS = {"sans-a": (400, 700), "sans-b": (400, 700), "serif-c": (400, 700), "code-mono-a": (400, 700)}

    def test_the_drawn_x_height_of_every_candidate_is_equal_on_the_outline(self):
        """The measurement the declared promise cannot make about itself.

        Every other x-height test here reads the size out of the stylesheet and
        multiplies it by the metric the size was DERIVED from - which holds
        whatever the browser then draws, and held while two faces of fourteen
        drew a letter 5 % off.  This one reads the size AND the weight out of the
        stylesheet and then measures the OUTLINE of ``x`` at that weight, off the
        asset bytes, with fontTools.  Nothing in the expectation comes from the
        rule under test.
        """
        project, root, now, css, page = self.rendered({"typography": 3, "code-font": 2},
                                                      font_ink_heights=self.INK, font_weights=self.INK_WEIGHTS)
        assets = dict(re.findall(r'@font-face\{font-family:"VisualCandidate(\d+)";src:url\("assets/([0-9a-f]{64})\.woff2"\)', css))
        self.assertEqual(8, len(assets), "the fixture has to show eight faces")
        # Every rule that starts with one selector component, in source order:
        # the weight and the size of a role are two rules with the SAME selector,
        # and a role may carry a whole selector list (".code,.code-liga,...").
        blocks: dict[str, list[str]] = {}
        for selector, body in css_rules(css):
            for part in selector.split(","):
                blocks.setdefault(part.strip(), []).append(body)

        def declared(key: str, prop: str) -> str | None:
            for body in blocks.get(key, []):
                found = re.search(rf"{prop}:([0-9.]+)", body)
                if found:
                    return found.group(1)
            return None

        base_weight = {index: int(declared(f".face-{index}", "font-weight")) for index in assets}
        self.assertGreater(len({ink_x_height((root / f"sheet/assets/{digest}.woff2").read_bytes()) for digest in assets.values()}), 3,
                           "without differing outlines this test measures nothing at all")
        for role, selector in visual.SHEET_ROLE_SELECTORS.items():
            nominal, _line = visual.ROLE_METRICS[role]
            target = nominal * visual.X_HEIGHT_TARGET
            drawn, bounds = {}, {}
            for index, digest in assets.items():
                asset = (root / f"sheet/assets/{digest}.woff2").read_bytes()
                part = selector.split(",")[0]
                key = f".face-{index} {part}"
                # The weight a browser resolves for this block: the rule for the
                # block if there is one, otherwise the weight of the face.
                own = declared(key, "font-weight")
                weight = int(own) if own else base_weight[index]
                size = Fraction(declared(key, "font-size"))
                units, upem = ink_x_height(asset, weight), sx_height(asset)[1]
                drawn[index] = size * Fraction(units, upem)
                bounds[index] = Fraction(units, upem)
            with self.subTest(role=role):
                self.assertEqual(len(assets), len(drawn), "every face draws every role it can reach")
                # The SAME two bounds the declared promise has, derived from the
                # one rounding - but applied to the letter that is drawn.
                for index, value in drawn.items():
                    self.assertLessEqual(abs(value - target), (visual.SIZE_QUANTUM / 2) * bounds[index],
                                         f"face {index} draws an x-height further from the target than the rounding")
                self.assertLessEqual(max(drawn.values()) - min(drawn.values()),
                                     visual.SIZE_QUANTUM * max(bounds.values()),
                                     "two candidates DRAW x-heights more than one rounding step apart")

    def test_the_disclosed_x_height_is_the_one_at_the_drawn_instance(self):
        """The number the owner READS, held against the outline that is drawn.

        The size in the stylesheet and the size in the disclosure come from two
        different expressions - ``_face_metric_rules`` forms its own ratio, the
        disclosure goes through ``_x_height_ratio``.  A mutant that makes the
        second one read the base weight therefore leaves the stylesheet correct
        and prints a number for a different instance, and every existing
        disclosure test agrees with it because its fixtures cannot move: with a
        static outline the x-height at 400 and at 700 is the same number.  This
        one renders faces whose ``x`` really moves along the weight axis.
        """
        project, root, now, css, page = self.rendered({"typography": 3, "code-font": 2},
                                                      font_ink_heights=self.INK, font_weights=self.INK_WEIGHTS)
        ratio, klass, metrics = self.faces(css, root / "sheet", "face", "VisualCandidate",
                                           visual.SHEET_ROLE_SELECTORS)
        assets = dict(re.findall(r'@font-face\{font-family:"VisualCandidate(\d+)";src:url\("assets/([0-9a-f]{64})\.woff2"\)', css))
        expected, moving = set(), 0
        for index, digest in assets.items():
            asset = (root / f"sheet/assets/{digest}.woff2").read_bytes()
            role = visual.FACE_ROLE[klass[index]]
            weight = visual.SHEET_ROLE_WEIGHTS[role]
            # Independently measured, off the asset bytes, at the instance the
            # role is drawn in - never through the function under test.
            drawn = ink_x_height(asset, weight)
            if drawn != ink_x_height(asset, visual.BASE_WEIGHT):
                moving += 1
            expected.add((drawn, weight))
        self.assertGreater(moving, 0, "without a moving outline this test measures nothing at all")
        printed = {(int(stated), int(weight)) for stated, _upem, _shown, _source, weight, _low, _high, _grade
                   in re.findall(r"x-Höhe (\d+) von (\d+) Einheiten \(([0-9.]+) em, ([^)]+)\), "
                                 r"gelesen an der gezeichneten Instanz bei Gewicht (\d+)\. "
                                 r"Gewichte: [^.]+\. Gewichtsbereich (\d+) bis (\d+)\. "
                                 r"Auf gleiche x-Höhe normalisiert, Grad ([0-9.]+) rem", page)}
        self.assertEqual(expected, printed,
                         "the disclosed x-height is not the one the drawn instance carries")
        # And the disclosed WEIGHT RANGE is the range of the file, read here off
        # the asset's own tables.  "low <= high" was not enough: a range printed
        # as "low bis low" satisfies it while naming a range no font has.
        ranges, spans = set(), 0
        for index, digest in assets.items():
            font = TTFont(io.BytesIO((root / f"sheet/assets/{digest}.woff2").read_bytes()))
            axis = next((item for item in font["fvar"].axes if item.axisTag == "wght"), None) if "fvar" in font else None
            low, high = ((int(axis.minValue), int(axis.maxValue)) if axis is not None
                         else (int(font["OS/2"].usWeightClass),) * 2)
            if low != high:
                spans += 1
            ranges.add((low, high))
        self.assertGreater(spans, 0, "without a face whose range really spans, this measures nothing")
        printed_ranges = {(int(low), int(high)) for _s, _u, _sh, _so, _w, low, high, _g
                          in re.findall(r"x-Höhe (\d+) von (\d+) Einheiten \(([0-9.]+) em, ([^)]+)\), "
                                        r"gelesen an der gezeichneten Instanz bei Gewicht (\d+)\. "
                                        r"Gewichte: [^.]+\. Gewichtsbereich (\d+) bis (\d+)\. "
                                        r"Auf gleiche x-Höhe normalisiert, Grad ([0-9.]+) rem", page)}
        self.assertEqual(ranges, printed_ranges, "the disclosed weight range is not the range of the file")

    def test_the_x_height_is_read_at_the_instance_the_role_is_drawn_in(self):
        """A moving outline, and the weight each role is really drawn in.

        ``font-optical-sizing:none`` pins the optical axis to its default, so the
        weight is the only axis left - and it moves the drawn letter without
        always moving the metric.  The fixture moves its ``x`` by a known amount,
        measured here off the outline rather than assumed.
        """
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        asset, _source = specimen_font(scratch / "m.woff2", "Fixture Moving", "serif",
                                       x_top=500, weights=(400, 700), rise=30)
        # Independently measured: the outline really is where the fixture says.
        self.assertEqual(500, ink_x_height(asset))
        self.assertEqual(530, ink_x_height(asset, 700))
        facts = visual._font_facts(asset, "moving")
        self.assertEqual({400: 500, 600: 520, 700: 530}, facts["x_heights"])
        self.assertEqual(500, facts["x_height"], "the default instance is still what the metric states")
        # A static face does not move, and says so for every asked weight.
        still, _source = specimen_font(scratch / "s.woff2", "Fixture Still", "serif", x_top=480)
        self.assertEqual({400: 480, 600: 480, 700: 480}, visual._font_facts(still, "still")["x_heights"])
        self.assertEqual(tuple(visual.ASKED_WEIGHTS), tuple(sorted(facts["x_heights"])))

    def test_a_role_is_drawn_in_one_weight_and_the_tables_are_pinned(self):
        """Point 124: the values a mutant could move, pinned as literals.

        Every expectation about a line box used to be computed FROM
        ``ROLE_METRICS``, so a mutant that doubled every line box moved both sides
        of the comparison and survived the whole suite.  The same held for the
        role maps: a role that fell out of the selector map took the test loop
        with it.  The numbers and the key sets are therefore spelled here.
        """
        self.assertEqual(400, visual.BASE_WEIGHT)
        self.assertEqual((400, 600, 700), tuple(visual.ASKED_WEIGHTS))
        self.assertEqual(Fraction(1, 4), visual.X_HEIGHT_FLOOR)
        self.assertEqual(4, visual.X_HEIGHT_INK_FACTOR)
        self.assertEqual({"specimen", "heading", "subheading", "body", "table-head", "table-cell", "code", "numerals"},
                         set(visual.SHEET_ROLE_SELECTORS))
        self.assertEqual({"heading", "subheading", "body", "table-head", "table-cell", "code", "numerals"},
                         set(visual.PLAYGROUND_ROLE_SELECTORS))
        self.assertEqual({"specimen": 400, "heading": 700, "subheading": 600, "body": 400,
                          "table-head": 700, "table-cell": 400, "code": 400, "numerals": 400},
                         visual.SHEET_ROLE_WEIGHTS)
        self.assertEqual({"heading": 700, "subheading": 600, "body": 400,
                          "table-head": 700, "table-cell": 400, "code": 400, "numerals": 400},
                         visual.PLAYGROUND_ROLE_WEIGHTS)
        for role, spelled in (("specimen", "1.725"), ("heading", "1.92"), ("subheading", "1.8"), ("body", "1.68"),
                              ("table-head", "1.5"), ("table-cell", "1.5"), ("code", "1.425"), ("numerals", "1.725")):
            with self.subTest(line_box=role):
                self.assertEqual(spelled, visual._rem(visual.ROLE_METRICS[role][1]))
        # And the guard that keeps a role from meaning two drawn weights at once -
        # which is what the role ``table`` was before it became two roles.
        with self.assertRaisesRegex(visual.VisualDecisionError, "role is drawn in more than one weight: table"):
            visual._role_weights({"table": "th,td"}, {700: "th"})
        self.assertEqual({"table": 700}, visual._role_weights({"table": "th"}, {700: "th"}))
        # The twin's script carries the same pairing, and it is pinned as a
        # LITERAL: the file is held byte-equal against its own generator, so a
        # mutant that moves the generator moves both sides and the byte equality
        # sees nothing at all.
        self.assertIn('const FACE_WEIGHT = Object.freeze({"mono": 400, "sans": 700, "serif": 400})',
                      visual.PLAYGROUND_APP_JS)
        self.assertEqual({klass: visual.PLAYGROUND_ROLE_WEIGHTS[role] for klass, role in visual.FACE_ROLE.items()},
                         {"mono": 400, "sans": 700, "serif": 400})

    def test_both_artifacts_switch_the_optical_axis_off_once_per_face(self):
        """Step one of the round, and the reason it is enough for that axis.

        Chrome picks an ``opsz`` instance from the font SIZE by default, and the
        size is the very thing this normalisation changes: one real face drew its
        ``x`` 4.8 % short that way.  Switched off, the axis rests at its default -
        the instance whose metric the module read.
        """
        project, root, now, manifest = self.rendered_twin()
        for folder, prefix in (("sheet", "face"), ("playground", "pg-face")):
            css = (root / folder / "style.css").read_text(encoding="utf-8")
            with self.subTest(artifact=folder):
                faces = len(re.findall(r"@font-face\{", css))
                self.assertEqual(faces, css.count("font-optical-sizing:none"),
                                 "one declaration per face, in the rule that names the family")
                self.assertNotIn("font-optical-sizing:auto", css)
                for index in range(faces):
                    self.assertIn(f"font-optical-sizing:none", dict(css_rules(css))[f".{prefix}-{index},.{prefix}-{index} pre"])

    def test_the_twin_must_state_its_x_height_for_exactly_the_drawn_weights(self):
        """What the twin's validator refuses, measured rather than assumed.

        The data file carries the x-height for every instance the artifacts draw.
        A face that states one weight too few leaves a role without a number, and
        a face that states one too many carries data nobody reads - both are
        refused by name, and so is a value under the floor the sheet holds a font
        to.  Without a leg here the checks were only present, not held: a mutant
        that drops the weight-set check passes the whole suite.
        """
        project, root, now, manifest = self.rendered_twin()
        raw = (root / "playground/data.js").read_bytes()
        data = visual.load_json_bytes(raw[len(visual._PLAYGROUND_DATA_PREFIX):len(raw) - len(visual._PLAYGROUND_DATA_SUFFIX)], "probe")
        # The control first: untouched data is accepted, so every refusal below
        # is about the one field it changes.
        visual._validate_playground_data(copy.deepcopy(data), manifest)
        upem = int(data["faces"][0]["units_per_em"])

        def bent(change):
            value = copy.deepcopy(data)
            change(value["faces"][0]["x_heights"])
            return value

        def drop(heights):
            heights.pop(str(visual.ASKED_WEIGHTS[1]))

        def extra(heights):
            heights["800"] = heights[str(visual.BASE_WEIGHT)]

        def under_floor(heights):
            heights[str(visual.BASE_WEIGHT)] = max(1, upem // 5)

        for change, message in ((drop, "does not state its x-height for the drawn weights"),
                                (extra, "does not state its x-height for the drawn weights"),
                                (under_floor, f"states an x-height under {visual.X_HEIGHT_FLOOR} of its em")):
            with self.subTest(change=change.__name__), \
                 self.assertRaisesRegex(visual.VisualDecisionError, re.escape(message)):
                visual._validate_playground_data(bent(change), manifest)

    #: Eight faces with a vertical metric of their OWN.  Without this every
    #: fixture stood at 800/-200 and "the same first baseline" was unmeasurable.
    VERTICALS = {"serif-a": (760, -240), "sans-a": (1000, -300), "serif-b": (820, -180),
                 "sans-b": (700, -350), "serif-c": (900, -250), "sans-c": (780, -220),
                 "code-mono-a": (850, -300), "code-mono-b": (720, -280)}

    def test_every_candidate_of_a_role_starts_on_the_same_first_baseline(self):
        """The owner's finding: blocks side by side must start on one line.

        The size, the line box AND the shift are read out of the produced
        stylesheet; the expectation comes from ``hhea`` off the asset bytes, at
        the instance the role is drawn in.  Nothing in the expectation passes
        through the rule under test.  Which table the engine splits the line box
        with was MEASURED (see BASELINE_METRIC), and so was the UNIT it rounds in
        (see BASELINE_ROUNDING): whole CSS pixels, at device pixel ratio 1 and 2
        alike.  That rounding is spelled out again below instead of borrowed from
        the module, so this leg still stands if the module gets it wrong.
        """
        project, root, now, css, page = self.rendered(font_x_heights=self.HEIGHTS,
                                                      font_verticals=self.VERTICALS)
        assets = dict(re.findall(r'@font-face\{font-family:"VisualCandidate(\d+)";src:url\("assets/([0-9a-f]{64})\.woff2"\)', css))
        self.assertEqual(8, len(assets), "the fixture has to show eight faces")
        # WHICH face draws which role is a property of the markup; the generic
        # family in the produced stylesheet names it, so no second map is kept here.
        generic = {"serif": "serif", "sans-serif": "sans", "monospace": "mono"}
        classes = {index: generic[family] for index, family in re.findall(
            r'\.face-(\d+),\.face-\d+ pre\{font-family:"VisualCandidate\d+",([a-z-]+)', css)}
        self.assertEqual(set(assets), set(classes), "every face states its generic family")
        metrics = {i: hhea_metrics((root / f"sheet/assets/{d}.woff2").read_bytes()) for i, d in assets.items()}
        self.assertGreater(len({(a, d) for a, d, _u in metrics.values()}), 3,
                           "without differing vertical metrics this test measures nothing at all")
        blocks = {}
        for selector, body in css_rules(css):
            for part in selector.split(","):
                blocks.setdefault(part.strip(), []).append(body)

        def declared(key, prop):
            for body in blocks.get(key, []):
                found = re.search(rf"{prop}:(-?[0-9.]+)rem", body)
                if found:
                    return Fraction(found.group(1))
            return None

        def half_up(value):
            """Halves away from zero - the engine's rounding, written out here."""
            return Fraction((value.numerator * 2 + value.denominator) // (value.denominator * 2))

        for role, selector in visual.SHEET_ROLE_SELECTORS.items():
            with self.subTest(role=role):
                natural, placed, shifts = [], [], []
                for index in assets:
                    key = f".face-{index} {selector.split(',')[0]}"
                    size, line = declared(key, "font-size"), declared(key, "line-height")
                    shift = declared(key, "top")
                    self.assertIsNotNone(shift, "every role rule states its compensation")
                    # A WHOLE CSS pixel: that is what lets the shift carry the
                    # engine's rounding instead of leaving it behind as a residual.
                    self.assertEqual(1, (shift * visual.BASELINE_ROOT_PX).denominator,
                                     "the compensation is not a whole number of CSS pixels")
                    ascent, descent, upem = metrics[index]
                    # The DRAWN baseline: ascent and descent each to a whole CSS
                    # pixel, the leading split with those, the result floored.
                    a = Fraction(ascent, upem) * size * visual.BASELINE_ROOT_PX
                    d = Fraction(-descent, upem) * size * visual.BASELINE_ROOT_PX
                    seated = (line * visual.BASELINE_ROOT_PX - (half_up(a) + half_up(d))) / 2 + half_up(a)
                    seated = Fraction(seated.numerator // seated.denominator)
                    if classes[index] not in visual.SHEET_ROLE_CLASSES[role]:
                        continue                      # this face never sets the role
                    natural.append((line - Fraction(ascent - descent, upem) * size) / 2
                                   + Fraction(ascent, upem) * size)
                    placed.append(seated + shift * visual.BASELINE_ROOT_PX); shifts.append(shift)
                # Every candidate that really SETS this role draws its first line on
                # one and the same pixel - not inside a tolerance, on the same pixel.
                self.assertEqual(1, len(set(placed)),
                                 "two candidates of this role start their first line on different pixels")
                # Without the shift they would not - otherwise this measures nothing.
                self.assertGreater(max(natural) - min(natural), visual.SIZE_QUANTUM,
                                   "the fixture does not make the defect visible")
                # No drawing face is lifted, and exactly the lowest one is left alone.
                self.assertEqual(0, min(shifts)); self.assertTrue(all(s >= 0 for s in shifts))
                # The step stays one absolute value per role.
                self.assertEqual({visual._rem(visual.ROLE_METRICS[role][1])},
                                 {visual._rem(declared(f".face-{index} {selector.split(',')[0]}", "line-height"))
                                  for index in assets})

    def test_the_rounding_the_compensation_rests_on_is_pinned(self):
        """The two measured facts a mutant could move, as literals.

        Both were MEASURED and neither can be recomputed from anything in the
        module, so both are spelled here rather than derived.
        """
        self.assertEqual("hhea", visual.BASELINE_METRIC)
        self.assertEqual("css-px", visual.BASELINE_ROUNDING)
        self.assertEqual(16, visual.BASELINE_ROOT_PX)
        # WHICH classification draws which role is markup, not arithmetic.
        self.assertEqual({"specimen": ("mono", "sans", "serif"), "heading": ("sans",),
                          "subheading": ("sans",), "body": ("serif",), "table-head": ("serif",),
                          "table-cell": ("serif",), "code": ("mono",), "numerals": ("mono", "serif")},
                         visual.SHEET_ROLE_CLASSES)
        self.assertEqual({"heading": ("sans",), "subheading": ("sans",), "body": ("serif",),
                          "table-head": ("serif",), "table-cell": ("serif",), "code": ("mono",),
                          "numerals": ("mono", "serif")}, visual.PLAYGROUND_ROLE_CLASSES)
        self.assertEqual(set(visual.SHEET_ROLE_SELECTORS), set(visual.SHEET_ROLE_CLASSES))
        self.assertEqual(set(visual.PLAYGROUND_ROLE_SELECTORS), set(visual.PLAYGROUND_ROLE_CLASSES))

    def test_the_drawn_baseline_is_a_whole_pixel_and_a_half_pixel_tie_is_refused(self):
        """The engine's own arithmetic, and the one case it does not answer twice alike."""
        def facts(ascent, descent, upem=1000):
            return {"ascent": ascent, "descent": descent, "units_per_em": upem}
        # Whole CSS pixels out, always.
        for size, line in ((Fraction(105, 100), Fraction(168, 100)), (Fraction(16, 10), Fraction(192, 100))):
            seated = visual._drawn_first_baseline(facts(728, -272), size, line, "probe")
            self.assertEqual(1, seated.denominator, "a drawn baseline is a whole CSS pixel")
        # An ascent exactly on a half pixel: 1000/1000 em at 1/32 rem is 0.5 px.
        with self.assertRaisesRegex(visual.VisualDecisionError,
                                    "draws its ascent exactly on a half pixel"):
            visual._drawn_first_baseline(facts(1000, -200), Fraction(1, 32), Fraction(2), "probe")
        # And a descent on one, with an ascent that is NOT on one - otherwise the
        # ascent refuses first and the descent guard is never reached.
        with self.assertRaisesRegex(visual.VisualDecisionError,
                                    "draws its descent exactly on a half pixel"):
            visual._drawn_first_baseline(facts(2000, -1000), Fraction(1, 32), Fraction(2), "probe")

    def test_the_role_target_is_taken_over_the_faces_that_draw_the_role(self):
        """Point 160: a face that never sets a role may not decide where it sits."""
        def face(ascent, classification):
            return ({"ascent": ascent, "descent": -200, "units_per_em": 1000,
                     "x_heights": {400: 500, 600: 500, 700: 500}}, classification)
        sans, tall_mono = face(700, "sans"), face(1400, "mono")
        selectors = {"heading": ".t-heading"}; weights = {"heading": 700}
        classes = {"heading": ("sans",)}
        only_sans = visual._baseline_targets([sans], selectors, weights, classes, "t")
        with_mono = visual._baseline_targets([sans, tall_mono], selectors, weights, classes, "t")
        self.assertEqual(only_sans, with_mono,
                         "a mono face that never sets .t-heading moved the heading target")
        # Over ALL faces it would have moved - otherwise this measures nothing.
        self.assertNotEqual(only_sans, visual._baseline_targets(
            [sans, tall_mono], selectors, weights, {"heading": ("sans", "mono")}, "t"))
        # Nothing draws the role at all: the widest set is the honest fallback.
        self.assertEqual(visual._baseline_targets([tall_mono], selectors, weights,
                                                  {"heading": ("sans", "mono")}, "t"),
                         visual._baseline_targets([tall_mono], selectors, weights, classes, "t"))

    def test_a_compensation_may_point_upwards_but_a_size_may_not(self):
        """``_rem_signed`` carries the one length that can be negative; ``_rem`` still refuses it."""
        self.assertEqual("0", visual._rem_signed(Fraction(0)))
        self.assertEqual("0.0625", visual._rem_signed(Fraction(1, 16)))
        self.assertEqual("-0.0625", visual._rem_signed(Fraction(-1, 16)))
        with self.assertRaisesRegex(visual.VisualDecisionError, "not on the rem grid"):
            visual._rem(Fraction(-1, 16))

    def test_the_vertical_metric_comes_from_hhea_and_not_from_the_typo_table(self):
        """Registerpunkt 156 (N15): with both tables equal the source is untestable."""
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        asset = divergent_vertical_font(scratch / "v.woff2", "Fixture Divergent", "serif",
                                        hhea=(900, -300), typo=(700, -200), win=(950, 350))
        facts = visual._font_facts(asset, "divergent")
        self.assertEqual((900, -300), (facts["ascent"], facts["descent"]))
        # The bit that makes some engines prefer the typo table changes nothing here,
        # because the engine this module compensates for was measured to ignore it.
        flagged = divergent_vertical_font(scratch / "b.woff2", "Fixture Bit", "serif",
                                          hhea=(900, -300), typo=(700, -200), use_typo_metrics=True)
        self.assertEqual((900, -300), tuple(visual._font_facts(flagged, "bit")[k] for k in ("ascent", "descent")))

    def test_the_line_gap_does_not_enter_the_ascent(self):
        """Registerpunkt 163 (B04): the gap moves ``line-height:normal`` and nothing here."""
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        asset = divergent_vertical_font(scratch / "g.woff2", "Fixture Gap", "serif",
                                        hhea=(728, -272), line_gap=218)
        facts = visual._font_facts(asset, "gap")
        self.assertEqual(728, facts["ascent"], "the line gap was folded into the ascent")
        self.assertEqual(-272, facts["descent"])
        # A face with the gap and one without place their baseline identically.
        plain = divergent_vertical_font(scratch / "p.woff2", "Fixture Plain", "serif", hhea=(728, -272))
        self.assertEqual(visual._drawn_first_baseline(visual._font_facts(plain, "plain"),
                                                      Fraction(105, 100), Fraction(168, 100), "p"),
                         visual._drawn_first_baseline(facts, Fraction(105, 100), Fraction(168, 100), "g"))

    def test_the_overview_table_states_the_weights_that_are_really_set(self):
        """Registerpunkt 156 (N28): no test called ``_font_overview`` at all."""
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        asset, _source = specimen_font(scratch / "o.woff2", "Fixture Overview", "serif",
                                       x_top=500, weights=(300, 800))
        digest = visual.sha256_bytes(asset); facts = {digest: visual._font_facts(asset, "overview")}
        entry = {"candidate": {"id": "typo-a"},
                 "fonts": [{"classification": "serif", "family": "Fixture Overview",
                            "asset": {"sha256": digest}}]}
        html_table = visual._font_overview([entry], facts)
        # The column says what the stylesheet SETS, not what the file offers.
        self.assertEqual((400, 600, 700), visual._weights_set((300, 800), visual.SHEET_WEIGHT_SELECTORS))
        self.assertIn("<td>400, 600, 700</td>", html_table)
        self.assertNotIn("<td>300, 800</td>", html_table)
        self.assertIn("300–800", html_table, "the range the file offers is named beside it")

    def test_the_twin_states_the_vertical_metric_of_its_own_font_bytes(self):
        """Registerpunkt 163 (F03): a pinned metric in the data file must not survive."""
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve() / "project"; project.mkdir()
        root = visual.visual_workspace(project, "run-a", create=True); now = int(time.time())
        # The twin of ``rendered_twin`` leaves every fixture at 800/-200, and with
        # ONE metric across the bundle a rule that pins it is byte-identical to a
        # rule that reads it - the bend would measure nothing.
        verticals = {"serif-a": (760, -240), "sans-a": (1000, -300), "mono-a": (850, -300),
                     "serif-b": (820, -180), "sans-b": (700, -350), "mono-b": (720, -280),
                     "code-mono-a": (900, -250), "code-mono-b": (780, -220)}
        multi_domain_bundle(project, root, now, {"typography": 2, "code-font": 2},
                            font_x_heights=self.HEIGHTS, font_verticals=verticals)
        with deadline(600, "render_playground"):
            visual.render_playground(project, "run-a", now)
        raw = (root / "playground/data.js").read_bytes()
        data = visual.load_json_bytes(raw[len(visual._PLAYGROUND_DATA_PREFIX):len(raw) - len(visual._PLAYGROUND_DATA_SUFFIX)], "probe")
        seen = set()
        for face in data["faces"]:
            asset = (root / f"playground/assets/{face['asset_sha256']}.woff2").read_bytes()
            ascent, descent, upem = hhea_metrics(asset)
            self.assertEqual((ascent, descent, upem),
                             (face["ascent"], face["descent"], face["units_per_em"]),
                             "the twin states a vertical metric its own bytes do not carry")
            seen.add((ascent, descent))
        self.assertGreater(len(seen), 1, "all faces share one metric - this leg measures nothing")

    def test_the_twin_starts_its_lines_where_the_sheet_does(self):
        """Same file, same role, same first baseline in both artifacts."""
        project, root, now, manifest = self.rendered_twin()
        sheet = (root / "sheet/style.css").read_text(encoding="utf-8")
        play = (root / "playground/style.css").read_text(encoding="utf-8")

        def rules(css, prefix, selectors):
            out = {}
            for role, selector in selectors.items():
                for match in re.finditer(rf"\.{prefix}-(\d+) {re.escape(selector.split(',')[0])}[^{{]*\{{([^}}]*)\}}", css):
                    out[(int(match.group(1)), role)] = match.group(2)
            return out

        a = rules(sheet, "face", visual.SHEET_ROLE_SELECTORS)
        b = rules(play, "pg-face", visual.PLAYGROUND_ROLE_SELECTORS)
        shared = sorted(set(visual.SHEET_ROLE_SELECTORS) & set(visual.PLAYGROUND_ROLE_SELECTORS))
        self.assertEqual(7, len(shared))
        seen = 0
        for (index, role), body in a.items():
            if role not in shared:
                continue
            self.assertIn((index, role), b, "the twin shows the same face in the same role")
            self.assertEqual(body, b[(index, role)],
                             f"sheet and playground set {role} of face {index} differently")
            self.assertIn("position:relative", body); seen += 1
        self.assertGreater(seen, 0)

    def test_both_table_wrappers_declare_the_overflow_they_really_get(self):
        """Punkt 170: ``overflow-y:clip`` neben ``overflow-x:auto`` rechnet zu ``hidden``.

        Am ECHTEN Bogen und am Zwilling gemessen (headless Chrome, 1440 px): die
        geschriebene Kombination ergibt berechnet ``overflow-x: auto`` und
        ``overflow-y: hidden``.  ``overflow-clip-margin`` gilt nur fuer
        ``overflow: clip``; neben ``hidden`` traegt es nichts.  Gegenprobe im
        selben Lauf, mit einer kuenstlichen Verschiebung von 12 px und einem
        isolierten Band unter dem Kasten: die Fassung MIT Polster laesst genau so
        viel Tinte entkommen wie dieselbe Regel OHNE Polster - 0,00 px in beiden,
        gegen 2,25 px bei ``overflow:clip`` auf beiden Achsen.  Das Polster ist
        also nicht schwach, sondern wirkungslos.

        Gewaehlt ist deshalb die ehrliche Deklaration und NICHT ``overflow:clip``
        auf beiden Achsen: ``overflow-x:clip`` naehme einer breiten Tabelle die
        waagerechte Rollbarkeit, und unerreichbarer Inhalt ist schlechter als
        eine Milderung, die heute nichts zu mildern hat.  Dieses Bein haelt beide
        Artefakte, weil die Regel in beiden steht (Luecken F01 und F03).
        """
        project, root, now, manifest = self.rendered_twin()
        sheet = (root / "sheet/style.css").read_text(encoding="utf-8")
        play = (root / "playground/style.css").read_text(encoding="utf-8")
        for label, css, selector in (("Bogen", sheet, ".table-wrap"), ("Zwilling", play, ".pg-table")):
            with self.subTest(artifact=label):
                blocks = [body for sel, body in css_rules(css) if sel.strip() == selector]
                self.assertEqual(1, len(blocks), f"{selector} steht genau einmal im Stylesheet")
                body = blocks[0]
                # Was der Browser wirklich rechnet, steht auch so da.
                self.assertIn("overflow-y:hidden", body,
                              "der Wrapper nennt nicht die Ueberlaufregel, die er bekommt")
                self.assertNotIn("overflow-y:clip", body,
                                 "overflow-y:clip rechnet neben overflow-x:auto zu hidden")
                # Ein Polster, das nur fuer `clip` gilt, darf nicht neben `hidden` stehen.
                self.assertNotIn("overflow-clip-margin", body,
                                 "das Clip-Polster ist neben hidden wirkungslos und wird nicht behauptet")
                # Die waagerechte Erreichbarkeit bleibt ausdruecklich erhalten.
                self.assertIn("overflow-x:auto", body,
                              "eine breite Tabelle muss waagerecht rollbar bleiben")

    def test_the_role_target_is_read_at_the_weight_the_role_is_drawn_in(self):
        """Luecke B05: das Rollenziel am GEZEICHNETEN Gewicht, nicht am Grundgewicht.

        Mit unbeweglichen Umrissen ist beides ununterscheidbar - die x-Hoehe bei
        400 und bei 700 ist dieselbe Zahl, also auch der Grad und die Grundlinie.
        Genau daran ist der Mutant B05 vorbeigekommen.  Dieses Bein rendert
        Flaechen, deren ``x`` sich mit dem Gewicht wirklich BEWEGT, und haelt die
        Eigenschaft, die das falsche Gewicht bricht: das Ziel einer Rolle ist die
        TIEFSTE gezeichnete Grundlinie ihrer zeichnenden Flaechen, also bleibt
        genau diese Flaeche unverschoben.  Wird das Ziel am Grundgewicht gelesen,
        liegt es neben dieser Grundlinie, und die kleinste Verschiebung ist von
        null verschieden - nach oben oder nach unten.

        Die Erwartung kommt aus den ASSETBYTES (``hhea`` ueber einen eigenen
        Leser) und den Graden, die im erzeugten Stylesheet STEHEN; die Rundung
        ist hier ausgeschrieben statt aus dem Modul geholt.  Nichts davon laeuft
        durch die Regel, die geprueft wird.
        """
        # Eine Fixture eigens fuer diese Luecke.  ``self.INK`` taugt hier NICHT:
        # die gezeichnete Grundlinie wird auf ganze Pixel abgerundet, also
        # verschluckt sie kleine Bewegungen der x-Hoehe, und beide Lesarten
        # kommen auf dasselbe Ziel - der Mutant B05 ueberlebt dann sichtbar
        # gruen.  Unterscheidbar wird es erst, wenn die BEWEGLICHE Flaeche das
        # Maximum der Rolle am Grundgewicht STELLT und es am gezeichneten
        # Gewicht verliert.  Durchgerechnet fuer ``heading`` (Zeilenkasten
        # 1,92 rem, Grad 1,6 rem): sans-a geht von 24 auf 22 px, sans-b und
        # sans-c bleiben bei 23 px, das Ziel also 24 gegen 23.
        ink = {"sans-a": (500, 100), "sans-b": (500, 0), "sans-c": (500, 0),
               "serif-a": (500, 0), "serif-b": (500, 0), "serif-c": (500, 0),
               "code-mono-a": (500, 0), "code-mono-b": (500, 0)}
        verticals = {"sans-a": (950, -250), "sans-b": (850, -200), "sans-c": (850, -200),
                     "serif-a": (800, -200), "serif-b": (820, -180), "serif-c": (780, -220),
                     "code-mono-a": (850, -300), "code-mono-b": (720, -280)}
        weights = {"sans-a": (400, 700), "sans-b": (400, 700), "sans-c": (400, 700)}
        project, root, now, css, page = self.rendered({"typography": 3, "code-font": 2},
                                                      font_ink_heights=ink, font_verticals=verticals,
                                                      font_weights=weights)
        assets = dict(re.findall(r'@font-face\{font-family:"VisualCandidate(\d+)";src:url\("assets/([0-9a-f]{64})\.woff2"\)', css))
        self.assertEqual(8, len(assets), "die Fixture muss acht Flaechen zeigen")
        generic = {"serif": "serif", "sans-serif": "sans", "monospace": "mono"}
        classes = {index: generic[family] for index, family in re.findall(
            r'\.face-(\d+),\.face-\d+ pre\{font-family:"VisualCandidate\d+",([a-z-]+)', css)}
        blocks: dict[str, list[str]] = {}
        for selector, body in css_rules(css):
            for part in selector.split(","):
                blocks.setdefault(part.strip(), []).append(body)

        def declared(key, prop):
            for body in blocks.get(key, []):
                found = re.search(rf"{prop}:(-?[0-9.]+)rem", body)
                if found:
                    return Fraction(found.group(1))
            return None

        def half_up(value):
            """Halbe von null weg - die Rundung der Engine, hier ausgeschrieben."""
            return Fraction((value.numerator * 2 + value.denominator) // (value.denominator * 2))

        # Ohne bewegliche Umrisse misst dieses Bein nichts: dann sind Grundgewicht
        # und gezeichnetes Gewicht dieselbe Zahl.  Am Umriss der Bytes geprueft.
        moving = 0
        for index, digest in assets.items():
            asset = (root / f"sheet/assets/{digest}.woff2").read_bytes()
            if "fvar" not in TTFont(io.BytesIO(asset)):
                continue
            if ink_x_height(asset, 700) != ink_x_height(asset, visual.BASE_WEIGHT):
                moving += 1
        self.assertGreater(moving, 0, "keine Flaeche bewegt ihre x-Hoehe mit dem Gewicht")

        for role, selector in visual.SHEET_ROLE_SELECTORS.items():
            placed, tops = [], []
            for index, digest in assets.items():
                if classes[index] not in visual.SHEET_ROLE_CLASSES[role]:
                    continue
                key = f".face-{index} {selector.split(',')[0]}"
                size, line, top = declared(key, "font-size"), declared(key, "line-height"), declared(key, "top")
                self.assertIsNotNone(top, "jede Rollenregel nennt ihre Kompensation")
                ascent, descent, upem = hhea_metrics((root / f"sheet/assets/{digest}.woff2").read_bytes())
                a = Fraction(ascent, upem) * size * visual.BASELINE_ROOT_PX
                d = Fraction(-descent, upem) * size * visual.BASELINE_ROOT_PX
                seated = (line * visual.BASELINE_ROOT_PX - (half_up(a) + half_up(d))) / 2 + half_up(a)
                seated = Fraction(seated.numerator // seated.denominator)
                placed.append(seated + top * visual.BASELINE_ROOT_PX)
                tops.append(top)
            with self.subTest(role=role, weight=visual.SHEET_ROLE_WEIGHTS[role]):
                self.assertTrue(placed, "jede Rolle hat zeichnende Flaechen")
                self.assertEqual(1, len(set(placed)),
                                 "zwei zeichnende Flaechen beginnen auf verschiedenen Pixeln")
                # Das Ziel ist die tiefste gezeichnete Grundlinie DIESER Rolle;
                # die Flaeche, die sie stellt, wird nicht bewegt.  Am falschen
                # Gewicht gelesen, trifft das Ziel diese Flaeche nicht mehr.
                self.assertEqual(0, min(tops),
                                 "die tiefste zeichnende Flaeche der Rolle wurde verschoben")
                self.assertTrue(all(value >= 0 for value in tops),
                                "eine zeichnende Flaeche wird angehoben statt gesenkt")

    def test_a_face_without_usable_vertical_metrics_is_refused(self):
        """Fail-closed: the baseline cannot be derived from a metric that is not there."""
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        for ascent, descent in ((0, -200), (-10, -200), (200, 200), (100, 100)):
            with self.subTest(vertical=(ascent, descent)):
                asset, _source = specimen_font(scratch / f"v{ascent}_{descent}.woff2",
                                               f"Fixture Vertical {ascent} {descent}", "serif",
                                               x_top=500, vertical=(ascent, descent))
                with self.assertRaisesRegex(visual.VisualDecisionError,
                                            f"states no usable vertical metrics: {ascent}/{descent}"):
                    visual._font_facts(asset, "vertical")
        # And a usable one is read as it stands.
        good, _source = specimen_font(scratch / "vok.woff2", "Fixture Vertical Ok", "serif",
                                      x_top=500, vertical=(910, -290))
        facts = visual._font_facts(good, "vertical")
        self.assertEqual((910, -290), (facts["ascent"], facts["descent"]))

    def test_the_metric_the_line_box_is_split_with_is_pinned(self):
        """Point 127 discipline: the two values the compensation rests on, as literals."""
        self.assertEqual("hhea", visual.BASELINE_METRIC)
        self.assertEqual(16, visual.BASELINE_ROOT_PX)
        self.assertEqual({700: ".t-heading,th,.code-label", 600: ".t-subheading"}, visual.SHEET_WEIGHT_SELECTORS)
        self.assertEqual({700: ".pg-heading,.pg-action,.pg-kpi-value,.pg-note,th", 600: ".pg-subheading"},
                         visual.PLAYGROUND_WEIGHT_SELECTORS)

    def test_the_weights_a_file_offers_and_the_ones_it_is_set_in_are_disclosed(self):
        """Owner's second wish, and it comes out of the file rather than the name."""
        self.assertEqual((400, 600, 700), visual._weights_set((100, 900), visual.SHEET_WEIGHT_SELECTORS))
        self.assertEqual((400,), visual._weights_set((400, 400), visual.SHEET_WEIGHT_SELECTORS))
        self.assertEqual((500,), visual._weights_set((500, 500), visual.SHEET_WEIGHT_SELECTORS))
        self.assertEqual((600, 700), visual._weights_set((600, 900), visual.SHEET_WEIGHT_SELECTORS))
        self.assertEqual((400, 600, 700), visual._weights_set((300, 800), visual.SHEET_WEIGHT_SELECTORS))
        # A static file says so, and says it cannot add weight in a heading.
        rigid = visual._weight_note({"weights": (400, 400), "weight_axis": False}, visual.SHEET_WEIGHT_SELECTORS)
        self.assertIn("starr 400", rigid); self.assertIn("kein Zulegen", rigid)
        moving = visual._weight_note({"weights": (300, 800), "weight_axis": True}, visual.SHEET_WEIGHT_SELECTORS)
        self.assertIn("variable Achse wght 300 bis 800", moving); self.assertNotIn("kein Zulegen", moving)
        # And the sheet really prints it, for a bundle whose faces differ.
        project, root, now, css, page = self.rendered(counts={"typography": 2, "code-font": 2},
                                                      font_weights={"serif-a": (400, 400), "sans-a": (100, 900),
                                                                    "code-mono-a": (600, 900)})
        self.assertIn("Gewichte: starr 400; gesetzt werden 400 - kein Zulegen in Überschriften möglich.", page)
        self.assertIn("Gewichte: variable Achse wght 100 bis 900; gesetzt werden 400, 600, 700.", page)
        self.assertIn("Gewichte: variable Achse wght 600 bis 900; gesetzt werden 600, 700.", page)

    def test_the_twin_recomputes_the_weights_and_the_metric_it_states(self):
        """Derived fields of the twin are recomputed, not believed."""
        project, root, now, manifest = self.rendered_twin()
        raw = (root / "playground/data.js").read_bytes()
        data = visual.load_json_bytes(raw[len(visual._PLAYGROUND_DATA_PREFIX):len(raw) - len(visual._PLAYGROUND_DATA_SUFFIX)], "probe")
        visual._validate_playground_data(copy.deepcopy(data), manifest)

        # A face that is really set in MORE than one weight: dropping one from a
        # face that carries a single weight leaves the value unchanged, and a
        # bend that changes nothing measures nothing.
        wide = next((i for i, f in enumerate(data["faces"]) if len(f["weights_set"]) > 1), None)
        self.assertIsNotNone(wide, "no face is set in more than one weight - this bend measures nothing")

        def bent(index, change):
            value = copy.deepcopy(data); change(value["faces"][index]); return value

        for index, change, message in (
                (wide, lambda f: f.__setitem__("weights_set", f["weights_set"][:1]),
                 "states weights its range does not give"),
                (wide, lambda f: f.__setitem__("weights_set", sorted(f["weights_set"] + [800])),
                 "states weights its range does not give"),
                (0, lambda f: f.__setitem__("descent", f["ascent"]),
                 "states no usable vertical metrics")):
            with self.subTest(message=message), \
                 self.assertRaisesRegex(visual.VisualDecisionError, re.escape(message)):
                visual._validate_playground_data(bent(index, change), manifest)
        # An ascent of zero cannot even be spelled: the schema bounds it at 1,
        # so THAT is the refusal a reader gets - named, and one step earlier
        # than the rule.  The rule still carries the case the schema cannot
        # express, a descent that swallows the ascent, and that is held above.
        with self.assertRaisesRegex(visual.VisualDecisionError, "faces.0.ascent"):
            visual._validate_playground_data(bent(0, lambda f: f.__setitem__("ascent", 0)), manifest)

    def test_a_glyph_that_falls_with_the_weight_is_refused_at_that_weight(self):
        """Point 136: both nets, per DRAWN instance, not only at the default."""
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        # 300/1000 at the default, 40/1000 at 700: accepted before, and the
        # sheet then set a heading at 20rem.
        falling, _source = specimen_font(scratch / "fall.woff2", "Fixture Falling", "serif",
                                         x_top=300, stated=300, weights=(400, 700), rise=-260)
        self.assertEqual(300, ink_x_height(falling)); self.assertEqual(40, ink_x_height(falling, 700))
        with self.assertRaisesRegex(visual.VisualDecisionError,
                                    "states an x-height under 1/4 of its em at weight"):
            visual._font_facts(falling, "falling")
        # A face that keeps its letter is untouched.
        steady, _source = specimen_font(scratch / "steady.woff2", "Fixture Steady", "serif",
                                        x_top=500, stated=500, weights=(400, 700), rise=30)
        self.assertEqual({400: 500, 600: 520, 700: 530}, visual._font_facts(steady, "steady")["x_heights"])

    def rendered_twin(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve() / "project"; project.mkdir()
        root = visual.visual_workspace(project, "run-a", create=True); now = int(time.time())
        # Bewegliche Umrisse: ohne sie ist "am gezeichneten Gewicht nachgerechnet"
        # von "am Grundgewicht nachgerechnet" nicht zu unterscheiden.
        multi_domain_bundle(project, root, now, {"typography": 2, "code-font": 2},
                            font_ink_heights=self.INK, font_weights=self.INK_WEIGHTS)
        with deadline(600, "render"):
            visual.render_sheet(project, "run-a", now)
            manifest = visual.render_playground(project, "run-a", now)
        return project, root, now, manifest

    def numeral_font(self, scratch: Path, features: str, *, referenced: bool = True, lookups: bool = True) -> bytes:
        """A font whose GSUB carries ``features``, optionally unreferenced or empty.

        The same three shapes the ligature rule is measured on, so the numeral
        rule is held to the same strictness rather than merely said to be.
        """
        asset, _source = x_height_font(scratch / f"n{abs(hash((features, referenced, lookups)))}.woff2",
                                       f"Fixture Numerals {features} {referenced} {lookups}", "serif", 500,
                                       numerals=features)
        font = TTFont(io.BytesIO(asset))
        table = font["GSUB"].table
        if not referenced:
            for script in table.ScriptList.ScriptRecord:
                for system in [script.Script.DefaultLangSys] + [record.LangSys for record in script.Script.LangSysRecord]:
                    if system is not None:
                        system.FeatureIndex = []; system.FeatureCount = 0
        if not lookups:
            for record in table.FeatureList.FeatureRecord:
                record.Feature.LookupListIndex = []; record.Feature.LookupCount = 0
        buffer = io.BytesIO(); font.flavor = "woff2"; font.save(buffer); return buffer.getvalue()

    def test_a_stated_x_height_under_a_quarter_of_the_em_is_refused(self):
        """Point 120: the metric used to be believed downwards without any bound.

        ``sxHeight`` 1 of 1000 was schema-valid and bound-valid and produced sizes
        of 575 rem - a page 9200 px wide that rendered and validated end to end.
        The floor is measured rather than guessed: over a real catalogue of text,
        display and handwriting faces the ratio runs from 0.378 to 0.732, so a
        quarter of the em drops none of them and sits a factor 1.5 under the
        smallest.  A face exactly ON the quarter is kept - the bound is a
        plausibility floor, and the boundary is pinned in both directions.
        """
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)

        def facts(stated: int):
            asset, _source = specimen_font(scratch / f"f{stated}.woff2", f"Fixture Floor {stated}", "serif",
                                           x_top=700, stated=stated)
            return visual._font_facts(asset, "floor")

        # Exactly a quarter passes on the metric alone, and a metric the font's
        # own drawing does NOT rescue stays refused.
        self.assertEqual(250, facts(250)["x_height"])
        for stated in (100, 25, 1):
            with self.subTest(stated=stated), \
                 self.assertRaisesRegex(visual.VisualDecisionError,
                                        f"floor states an x-height under 1/4 of its em: {stated}/1000"):
                facts(stated)
        # 249 against an outline of 700 is the case the floor used to catch and
        # must not: the statement is implausible, the LETTER is fine, so the
        # outline becomes the source and the face is kept.  Measured over 848
        # real faces from 439 files this changes exactly one file - a shipped
        # system font whose stated x-height is 0.2441 em while its own ``x``
        # draws 0.4971 em.
        kept = facts(249)
        self.assertEqual((700, "glyph-x"), (kept["x_height"], kept["x_height_source"]))
        # The fallback is not a way around the floor, and both halves are held.
        # It needs an outline that holds the floor ITSELF ...
        low, _source = specimen_font(scratch / "low.woff2", "Fixture Low Ink", "serif", x_top=200, stated=200)
        with self.assertRaisesRegex(visual.VisualDecisionError,
                                    "floor states an x-height under 1/4 of its em: 200/1000"):
            visual._font_facts(low, "floor")
        # ... and it needs the two to agree: the reproduced 1/1000 against an
        # outline of 700 is a factor of 700 apart, so no fallback is taken and
        # point 120 stays closed - asserted above in the loop for stated=1.
        # And the size such a face would have been set at, so the number the
        # bound prevents is on the record rather than merely asserted about.
        self.assertEqual(Fraction(575), visual._normalised_size(visual.ROLE_METRICS["specimen"][0], Fraction(1, 1000)))

    def test_a_stated_x_height_its_own_outline_contradicts_is_refused(self):
        """Point 120, the second and independent net.

        The floor bounds the RATIO; this holds the STATEMENT against the one thing
        that can be measured instead of claimed - the outline of the very glyph
        the metric is named after.  It is the only one of the two that sees a
        metric far ABOVE its own drawing, which the floor cannot: such a face has
        a perfectly plausible ratio and draws nothing like it.  The factor is wide
        because an overshoot or a flourish is a design feature - measured over the
        same catalogue the two disagree by at most 15 %, and the reproduced 1/1000
        sits at a factor of 700.
        """
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)

        def facts(x_top: int, stated: int):
            asset, _source = specimen_font(scratch / f"c{x_top}-{stated}.woff2",
                                           f"Fixture Cross {x_top} {stated}", "serif", x_top=x_top, stated=stated)
            return visual._font_facts(asset, "cross")

        # A tiny outline under a metric that claims half the em: plausible ratio,
        # and the drawing says otherwise.
        with self.assertRaisesRegex(visual.VisualDecisionError,
                                    "cross states an x-height its own glyph x contradicts: 500/1000 against 100"):
            facts(100, 500)
        # The control, and a real-sized disagreement that must NOT be refused:
        # 15 % apart is a design feature, and a real catalogue carries it.
        self.assertEqual(700, facts(700, 700)["x_height"])
        self.assertEqual(500, facts(565, 500)["x_height"])
        self.assertEqual(454, facts(395, 454)["x_height"])

    def test_the_source_of_a_negative_metric_is_the_outline_and_says_so(self):
        """Point 126: the behaviour was right and nothing held it.

        A negative ``sxHeight`` falls to the measurement, and the sheet prints
        "gemessen an der Glyphe x".  No fixture carried a negative metric, so a
        mutant that named the metric as the source for it survived the whole
        suite - the SIZE was right in both, only the stated provenance was wrong.
        """
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        for stated in (-1, -500, -32768):
            with self.subTest(stated=stated):
                asset, _source = specimen_font(scratch / f"n{abs(stated)}.woff2", f"Fixture Neg {stated}", "serif",
                                               x_top=700, stated=stated)
                facts = visual._font_facts(asset, "neg")
                self.assertEqual("glyph-x", facts["x_height_source"])
                self.assertEqual(700, facts["x_height"])
                self.assertEqual({400: 700, 600: 700, 700: 700}, facts["x_heights"])
        # Zero is the documented case and takes the same road.
        asset, _source = specimen_font(scratch / "zero.woff2", "Fixture Zero", "serif", x_top=700, stated=0)
        self.assertEqual("glyph-x", visual._font_facts(asset, "zero")["x_height_source"])
        # And a stated metric names itself, so the two answers are distinguishable.
        asset, _source = specimen_font(scratch / "pos.woff2", "Fixture Pos", "serif", x_top=700, stated=480)
        self.assertEqual("os2-sxheight", visual._font_facts(asset, "pos")["x_height_source"])

    def numeral_variant(self, scratch: Path, kind: str) -> bytes:
        """One font per shape in which a numeral feature is present but draws nothing.

        Built on real bytes with real glyph names, because the point is what the
        GSUB tables say rather than what a fixture asserts.
        """
        base, _source = make_font(scratch / f"v-{kind}.woff2", f"Fixture Variant {kind}", "serif")
        probe = TTFont(io.BytesIO(base))
        cmap = probe.getBestCmap()
        zero, one = cmap[ord("0")], cmap[ord("1")]
        letters = [cmap[ord(char)] for char in "xyz" if ord(char) in cmap]
        systems = "languagesystem DFLT dflt;\nlanguagesystem latn dflt;\n"
        feature = f"feature tnum {{\n  sub {zero} by {one};\n}} tnum;\n"
        if kind == "letters":
            feature = f"feature tnum {{\n  sub {letters[0]} by {letters[1]};\n}} tnum;\n"
        elif kind == "arab":
            systems = "languagesystem DFLT dflt;\nlanguagesystem arab dflt;\n"
            feature = f"feature tnum {{\n  script arab;\n  sub {zero} by {one};\n}} tnum;\n"
        elif kind == "identity":
            feature = f"feature zero {{\n  sub {zero} by {zero};\n}} zero;\n"
        font = TTFont(io.BytesIO(base))
        addOpenTypeFeaturesFromString(font, systems + feature)
        if kind == "empty":
            for lookup in font["GSUB"].table.LookupList.Lookup:
                for table in lookup.SubTable:
                    if hasattr(table, "mapping"):
                        table.mapping = {}
        if kind == "nodigits":
            for table in font["cmap"].tables:
                for code in range(0x30, 0x3A):
                    table.cmap.pop(code, None)
        buffer = io.BytesIO(); font.flavor = "woff2"; font.save(buffer); return buffer.getvalue()

    def test_a_numeral_feature_is_read_from_the_tables_and_its_effect_is_not_checked(self):
        """Point 122: the boundary of the promise, measured instead of assumed.

        The sheet promises the digit sets are read "with the same strictness as
        the ligatures" - referenced by a language system and carrying at least one
        lookup.  That is what it does, and it is all it does: a feature whose
        lookup substitutes letters, or maps nothing, or lives under another
        script, still names a row.  Not a new class of defect - the ligatures have
        exactly the same property and the promise says so - but unmeasured until
        here, and a row shown for a feature that draws nothing is what the reader
        would have to notice unaided.
        """
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        for kind, expected in (("letters", ("tnum",)), ("arab", ("tnum",)), ("empty", ("tnum",)),
                               ("nodigits", ("tnum",)), ("identity", ("zero",))):
            with self.subTest(kind=kind):
                facts = visual._font_facts(self.numeral_variant(scratch, kind), "variant")
                self.assertEqual(expected, facts["numerals"])
                rows = visual._numeral_rows("Ziffern", facts, "face-0")
                self.assertIn("numerals-set", rows, "the row is shown - that is the measured boundary")
                self.assertIn(visual.NUMERAL_SAMPLE, rows)
        # The one case the reader IS warned about: no digits in the font at all.
        facts = visual._font_facts(self.numeral_variant(scratch, "nodigits"), "variant")
        self.assertEqual([f"U+{code:04X}" for code in range(0x30, 0x3A)],
                         visual._missing_codepoints(facts, visual.NUMERAL_SAMPLE))

    def test_a_digit_row_says_when_the_digits_are_not_the_fonts_own(self):
        """Point 123: the coverage statement the digit block did not carry.

        ``_coverage_note`` was applied to the set text and to the code block, so a
        font with ``tnum`` and no digits showed ten digits out of a FALLBACK font
        under the heading "Ziffernvarianten aus den Fonttabellen" - the feature
        real, the letters not the candidate's, and nothing said so.
        """
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        facts = visual._font_facts(self.numeral_variant(scratch, "nodigits"), "variant")
        rows = visual._numeral_rows("Ziffern im Fließtext", facts, "face-0")
        self.assertIn("nicht im Font, Darstellung aus einer Ersatzschrift", rows)
        for code in range(0x30, 0x3A):
            self.assertIn(f"U+{code:04X}", rows)
        # And a font that HAS its digits says that, rather than saying nothing.
        complete = visual._font_facts(self.numeral_variant(scratch, "letters"), "variant")
        self.assertIn("jedes Zeichen des Beispieltexts steht im Font",
                      visual._numeral_rows("Ziffern im Fließtext", complete, "face-0"))

    def test_a_numeral_feature_counts_only_when_the_font_really_applies_it(self):
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        for features, referenced, lookups, expected in (
                ("tnum", True, True, ("tnum",)),
                ("lnum onum tnum", True, True, ("lnum", "onum", "tnum")),
                ("zero", True, True, ("zero",)),
                ("dlig tnum", True, True, ("tnum",)),          # dlig is no numeral feature
                ("tnum", False, True, ()),                      # referenced by no language system
                ("tnum", True, False, ())):                     # referenced, but draws nothing
            with self.subTest(features=features, referenced=referenced, lookups=lookups):
                facts = visual._font_facts(self.numeral_font(scratch, features, referenced=referenced, lookups=lookups), "probe")
                self.assertEqual(expected, facts["numerals"])
                # The two feature groups are read in one pass and must not bleed.
                self.assertEqual((), facts["ligatures"])
        self.assertEqual(frozenset({"lnum", "onum", "pnum", "tnum", "zero"}), visual.NUMERAL_FEATURES)
        self.assertEqual(set(visual.NUMERAL_FEATURES), set(visual.NUMERAL_LABELS))

    def test_every_numeral_set_a_font_offers_gets_a_named_row_and_none_is_stated_as_such(self):
        numerals = {"serif-a": "lnum onum tnum", "code-mono-a": "zero tnum"}
        project, root, now, css, page = self.rendered({"typography": 2, "code-font": 2},
                                                     font_x_heights=self.HEIGHTS, font_numerals=numerals)
        ratio, klass, metrics = self.faces(css, root / "sheet", "face", "VisualCandidate", visual.SHEET_ROLE_SELECTORS)
        tree = PageTree(page)
        # One row per feature the font applies, named, and all rows of one face at
        # the SAME size - the rows differ in the feature and in nothing else.
        rows = re.findall(r'<p class="numerals num-([a-z]+)">0123456789'
                          r'<span class="numerals-name">([^<]+)</span></p>', page)
        # ``numerals`` is sorted, so one font's rows run in feature order: the
        # serif carries lnum/onum/tnum, the code mono tnum/zero.
        self.assertEqual([("lnum", "Versalziffern (lnum)"), ("onum", "Mediävalziffern (onum)"),
                          ("tnum", "tabellarisch (tnum)"), ("tnum", "tabellarisch (tnum)"),
                          ("zero", "geschlitzte Null (zero)")], rows)
        for tag in ("lnum", "onum", "tnum", "zero"):
            self.assertIn(f'.num-{tag}{{font-feature-settings:"{tag}" 1}}', css)
        # A font that carries none says so, and it says which five were looked
        # for: "no information" may not be read as "no variants".
        self.assertIn("keine der Ziffernfeatures lnum, onum, pnum, tnum, zero im Font", page)
        self.assertEqual(2, page.count("keine der Ziffernfeatures"),
                         "the second typography serif and the second code mono carry none")
        # tnum is in the domain overview because it decides a column of figures.
        for kind in ("typography", "code-font"):
            with self.subTest(domain=kind):
                head = [text_of(cell) for cell in nodes(self.section(tree, kind), "th")][:10]
                self.assertEqual(["Kandidat", "Klasse", "Familie", "x-Höhe", "x/em", "Quelle", "Grad",
                                  "Gewichte im Font", "gesetzt", "tabellarische Ziffern"], head)
        self.assertEqual(2, len(re.findall(r"<td>ja</td>", page)), "exactly the two fonts carrying tnum")

    @staticmethod
    def section(tree, kind: str) -> dict:
        return next(node for node in nodes(tree.root, "section") if node["attrs"].get("id") == f"candidates-{kind}")

    def test_the_playground_normalises_and_declares_exactly_as_the_sheet_does(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve() / "project"; project.mkdir()
        root = visual.visual_workspace(project, "run-a", create=True); now = int(time.time())
        weights = {"serif-a": (300, 800), "sans-a": (100, 900), "code-mono-a": (500, 500)}
        multi_domain_bundle(project, root, now, {"typography": 2, "code-font": 2}, font_x_heights=self.HEIGHTS,
                            font_numerals={"serif-a": "lnum tnum", "code-mono-a": "zero"}, font_weights=weights)
        with deadline(600, "render"):
            visual.render_sheet(project, "run-a", now)
            manifest = visual.render_playground(project, "run-a", now)
        visual.validate_playground_manifest(manifest, root / "playground")
        css = (root / "playground/style.css").read_text(encoding="utf-8")
        raw = (root / "playground/data.js").read_bytes()
        data = visual.load_json_bytes(raw[len(visual._PLAYGROUND_DATA_PREFIX):len(raw) - len(visual._PLAYGROUND_DATA_SUFFIX)], "probe")
        # Owner decision B: every face declares the range of its own file, so
        # nothing is synthesised - the playground carried no descriptor at all.
        rules = re.findall(r"@font-face\{[^}]*\}", css)
        self.assertEqual(len(data["faces"]), len(rules))
        self.assertEqual(0, sum("font-weight" not in rule for rule in rules),
                         "a playground face without a weight range lets the browser fake a bold")
        ratio, klass, metrics = self.faces(css, root / "playground", "pg-face", "PlaygroundFace",
                                           visual.PLAYGROUND_ROLE_SELECTORS)
        sheet_css = (root / "sheet/style.css").read_text(encoding="utf-8")
        sheet_ratio, sheet_klass, sheet_metrics = self.faces(sheet_css, root / "sheet", "face", "VisualCandidate",
                                                            visual.SHEET_ROLE_SELECTORS)
        for index, face in enumerate(data["faces"]):
            with self.subTest(face=index):
                value = Fraction(int(face["x_height"]), int(face["units_per_em"]))
                self.assertEqual(ratio[str(index)], value)
                # The disclosed size of the twin is the sheet's, derived once.
                role = visual.FACE_ROLE[face["classification"]]
                self.assertEqual(visual._rem(visual._normalised_size(visual.ROLE_METRICS[role][0], value)), face["size_rem"])
                twin = next(key for key, item in sheet_ratio.items()
                            if item == value and sheet_klass[key] == face["classification"])
                self.assertEqual(sheet_metrics[(twin, role)][0], Fraction(face["size_rem"]))
                # And every shared role is set to the same size in both artifacts.
                for shared in set(visual.PLAYGROUND_ROLE_SELECTORS) & set(visual.SHEET_ROLE_SELECTORS):
                    self.assertEqual(sheet_metrics[(twin, shared)], metrics[(str(index), shared)], shared)
        # The digit sets reach the twin as data, so its rows name the same features.
        for axis in data["axes"]:
            for candidate in axis["candidates"]:
                for slot in ("serif", "sans", "mono"):
                    if slot in candidate:
                        digest = data["faces"][candidate[slot]["face"]]["asset_sha256"]
                        facts = visual._font_facts((root / f"playground/assets/{digest}.woff2").read_bytes(), "probe")
                        self.assertEqual(list(facts["numerals"]), candidate[slot]["numerals"])
        # The clamp belongs to the artifact it is written for.  Both twins share ONE
        # clamp function, so only the selector map keeps them apart - and nothing
        # held that, which is how mutant M38 survived clamping the playground with
        # the sheet's block names.
        sheet_parts = {part for selector in visual.SHEET_WEIGHT_SELECTORS.values() for part in selector.split(",")}
        playground_parts = {part for selector in visual.PLAYGROUND_WEIGHT_SELECTORS.values() for part in selector.split(",")}
        self.assertTrue(sheet_parts - playground_parts, "the two maps have to differ for this to measure anything")
        for part in sorted(sheet_parts - playground_parts):
            with self.subTest(sheet_only=part):
                self.assertNotIn(part, css, "the playground carries a block selector of the sheet")
        for index in range(len(data["faces"])):
            for part in sorted(playground_parts):
                with self.subTest(face=index, part=part):
                    self.assertIn(f".pg-face-{index} {part}", css)
        self.assertEqual(5, len(re.findall(r"\.pg-num-[a-z]+\{", css)))


class VisualDecisionTypographicFamilyTests(TimeLimitedTestCase):
    """The family name the sheet prints: the family, and not one cut of it.

    ``extract_font_metadata`` read ``name`` id 1, which is the family name of the
    font's *default instance*.  A variable font that rests below 400 spells a cut
    there, so the sheet named a weight it does not set - the specimens run at 400.
    Id 16 is the typographic family and a font carries it exactly when id 1 carries
    such a suffix, so "id 16 where the font states one, id 1 otherwise" is the
    whole rule and needs no list of names.

    This is not a second reading of the name table.  The classification source
    already held a bound METADATA family against ``(names[16] or names[1])``; the
    two places now answer one rule instead of two that can drift apart.
    """

    TIME_LIMIT = 600.0

    @staticmethod
    def typographic(asset: bytes, family: str) -> bytes:
        """The same font, carrying ``family`` as ``name`` id 16."""
        font = TTFont(io.BytesIO(asset)); font["name"].setName(family, 16, 3, 1, 0x409)
        buffer = io.BytesIO(); font.flavor = "woff2"; font.save(buffer); return buffer.getvalue()

    def test_the_family_is_the_typographic_family_wherever_the_font_states_one(self):
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        plain, _source = make_font(scratch / "f.woff2", "Fixture Serif Light", "serif")
        # A font that states no typographic family keeps the name it has: this is
        # a fallback, not a rename.
        self.assertEqual("Fixture Serif Light", visual.extract_font_metadata(plain)["family"])
        # With id 16 the cut disappears from the one string the sheet prints ...
        named = self.typographic(plain, "Fixture Serif")
        metadata = visual.extract_font_metadata(named)
        self.assertEqual("Fixture Serif", metadata["family"])
        # ... while every field that names the INSTANCE goes on naming it, so the
        # report loses nothing - a reader can still tell which cut the file is.
        self.assertEqual("Regular", metadata["subfamily"])
        self.assertEqual("Fixture Serif Light Regular", metadata["full_name"])
        self.assertEqual("FixtureSerifLight-Regular", metadata["postscript_name"])
        self.assertEqual("Fixture Serif Light", visual._default_instance_family(named))
        # An empty id 16 is not a statement about the family, so it falls back
        # rather than naming a family with no name.
        self.assertEqual("Fixture Serif Light",
                         visual.extract_font_metadata(self.typographic(plain, ""))["family"])

    def test_a_name_record_of_nothing_but_blanks_is_no_statement_about_the_family(self):
        """Registerpunkte 108 and 110, closed by the one expression.

        ``"   "`` satisfied ``minLength: 1``, is no control character and so won
        outright: the sheet named a family made of spaces and the owner confirmed
        a name that is none.  The same expression settles the platform case -
        ``sorted({"", "MacOnly"})[0]`` was the empty string, so a record left
        empty on one platform hid a filled one on another and the typographic
        family the font does state went unread.
        """
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        plain, _source = make_font(scratch / "f.woff2", "Fixture Serif Light", "serif")

        def with_id16(*records: tuple[str, int, int, int]) -> bytes:
            font = TTFont(io.BytesIO(plain))
            for value, platform, encoding, language in records:
                font["name"].setName(value, 16, platform, encoding, language)
            buffer = io.BytesIO(); font.flavor = "woff2"; font.save(buffer); return buffer.getvalue()

        # Point 108: blanks of every kind are no statement, so the family falls
        # back exactly as an absent record does.
        for blank in (" ", "   ", "\t", "\u00a0", " \u2003 "):
            with self.subTest(blank=repr(blank)):
                self.assertEqual("Fixture Serif Light",
                                 visual.extract_font_metadata(with_id16((blank, 3, 1, 0x409)))["family"])
        # Point 110: a blank record on one platform must not out-sort a filled one
        # on another.  Before this the empty string won and the answer was id 1.
        both = with_id16(("", 3, 1, 0x409), ("MacOnly", 1, 0, 0))
        self.assertEqual("MacOnly", visual.extract_font_metadata(both)["family"])
        blanks = with_id16(("   ", 3, 1, 0x409), ("Fixture Serif", 1, 0, 0))
        self.assertEqual("Fixture Serif", visual.extract_font_metadata(blanks)["family"])
        # The rule is the same for every name field, not only for the family: a
        # required field that states nothing is refused by name rather than
        # carrying blanks into a report the owner confirms.
        for identifier, field in ((1, "family"), (2, "subfamily"), (4, "full_name"), (6, "postscript_name")):
            with self.subTest(name_id=identifier):
                font = TTFont(io.BytesIO(plain))
                font["name"].setName("   ", identifier, 3, 1, 0x409)
                for record in list(font["name"].names):
                    if record.nameID == identifier and (record.platformID, record.platEncID) != (3, 1):
                        font["name"].removeNames(record.nameID, record.platformID, record.platEncID, record.langID)
                buffer = io.BytesIO(); font.flavor = "woff2"; font.save(buffer)
                with self.assertRaisesRegex(visual.VisualDecisionError,
                                            f"font states no name in name table id {identifier}"):
                    visual.extract_font_metadata(buffer.getvalue())
        # And the explaining reader of the old name follows the same rule, so a
        # blank id 1 can never become the "legacy" name of a refusal either.
        blank_one = TTFont(io.BytesIO(plain))
        blank_one["name"].setName("   ", 1, 3, 1, 0x409)
        # Every id-1 record has to be blank for this to be the blank case at all:
        # a filled sibling record SHOULD win, which is point 110 working.
        for record in list(blank_one["name"].names):
            if record.nameID == 1 and (record.platformID, record.platEncID) != (3, 1):
                blank_one["name"].removeNames(1, record.platformID, record.platEncID, record.langID)
        buffer = io.BytesIO(); blank_one.flavor = "woff2"; blank_one.save(buffer)
        self.assertEqual("", visual._default_instance_family(buffer.getvalue()))
        # And with one filled sibling record the filled one is the answer - the
        # other half of point 110, stated in the positive direction.
        sibling = TTFont(io.BytesIO(plain))
        sibling["name"].setName("   ", 1, 3, 1, 0x409)
        buffer = io.BytesIO(); sibling.flavor = "woff2"; sibling.save(buffer)
        self.assertEqual("Fixture Serif Light", visual._default_instance_family(buffer.getvalue()))

    def test_the_choice_between_two_name_records_is_lexicographic_and_deterministic(self):
        """Registerpunkt 109: the tie-break was right, written down and unmeasured.

        A mutant taking the LAST record instead of the first survived all 188
        tests of the previous round.  Two records that differ pin the direction.
        """
        scratch = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        plain, _source = make_font(scratch / "f.woff2", "Fixture Serif Light", "serif")

        def pair(first: str, second: str) -> str:
            font = TTFont(io.BytesIO(plain))
            font["name"].setName(second, 16, 3, 1, 0x409)
            font["name"].setName(first, 16, 1, 0, 0)
            buffer = io.BytesIO(); font.flavor = "woff2"; font.save(buffer)
            return visual.extract_font_metadata(buffer.getvalue())["family"]

        # Alphabetically first, whichever platform carries it and whichever way
        # round the two are written - never the last, and never the table order.
        self.assertEqual("Alfa", pair("Alfa", "Bravo"))
        self.assertEqual("Alfa", pair("Bravo", "Alfa"))
        self.assertEqual("Delta", pair("Yankee", "Delta"))
        # Deterministic: the same file answers the same way every time.
        self.assertEqual({"Alfa"}, {pair("Alfa", "Bravo") for _ in range(3)})
        # And the same discipline where only id 1 is ambiguous.
        font = TTFont(io.BytesIO(plain))
        font["name"].setName("Zulu", 1, 3, 1, 0x409)
        font["name"].setName("Alfa", 1, 1, 0, 0)
        buffer = io.BytesIO(); font.flavor = "woff2"; font.save(buffer)
        self.assertEqual("Alfa", visual.extract_font_metadata(buffer.getvalue())["family"])
        self.assertEqual("Alfa", visual._default_instance_family(buffer.getvalue()))

    def test_a_report_that_names_the_default_instance_is_refused_by_name(self):
        """The cost the owner accepted, and the message he gets in return.

        ``metadata.extracted`` is compared byte for byte and the report schema is
        ``additionalProperties: false``, so a report of the older version cannot be
        migrated in place: its digest sits in the candidate value, that value's
        digest in the candidate set, and the set's digest in the brief and in both
        rendered manifests.  Rewriting the one string moves all four - which is
        building the bundle again under another name.  So the refusal has to carry
        its cause: ``_sheet_view`` stops at the first bad report, and the whole
        sheet stays unrenderable until the bundle is rebuilt.
        """
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        project = Path(temp.name).resolve() / "project"; project.mkdir()
        root = visual.visual_workspace(project, "run-a", create=True)
        legacy, current = "Fixture Serif Light", "Fixture Serif"
        candidates = font_candidates(root, [("serif", legacy, "serif-a"), ("sans", "Fixture Sans", "sans-a"),
                                            ("mono", "Fixture Mono", "mono-a")])
        asset = self.typographic((root / "serif-a.woff2").read_bytes(), current)
        (root / "serif-a.woff2").write_bytes(asset)
        serif = candidates[0]
        serif["asset"]["sha256"] = visual.sha256_bytes(asset)
        serif["family"] = current
        serif["metadata"]["extracted"] = visual.extract_font_metadata(asset)
        report = {"schema_version": "brandkit-font-candidate-report.v1", "candidate_set_sha256": "a" * 64,
                  "specimen_sha256": visual.SPECIMEN_SHA256, "candidates": candidates,
                  "objective_status": "pass", "human_review_status": "required"}
        # The control: a report written by this version is accepted, and the two
        # spellings really do differ - without that the legs below measure nothing.
        visual.validate_font_report(report, root)
        self.assertEqual((legacy, current),
                         (visual._default_instance_family(asset), serif["metadata"]["extracted"]["family"]))

        # A report of the older version carries the old spelling in BOTH places,
        # and that is the one shape which earns the named refusal.
        stale = copy.deepcopy(report)
        stale["candidates"][0]["family"] = legacy
        stale["candidates"][0]["metadata"]["extracted"]["family"] = legacy
        with self.assertRaisesRegex(visual.VisualDecisionError,
                                    "font report names the default instance and not the typographic family: "
                                    "serif-a; it was written by an earlier version of this module and the "
                                    "bundle has to be built again"):
            visual.validate_font_report(stale, root)

        # One spelling old and one new is not an older bundle, it is an edited
        # report - so it stays the generic refusal, and the named message cannot
        # be used to explain away a mismatch it does not explain.
        for field in ("family", "extracted"):
            mixed = copy.deepcopy(report)
            if field == "family":
                mixed["candidates"][0]["family"] = legacy
            else:
                mixed["candidates"][0]["metadata"]["extracted"]["family"] = legacy
            with self.subTest(edited=field), \
                 self.assertRaisesRegex(visual.VisualDecisionError, "differs?: serif-a"):
                visual.validate_font_report(mixed, root)

        # The discriminating shape, and the one my first attempt missed: the call
        # site is REACHED - ``extracted`` is tampered in a field that is not the
        # family - while the two spellings of the name disagree.  An older bundle
        # spells the old name in both places, so this is an edited report and has
        # to stay generic.  Without this leg a rule that asks only about
        # ``candidate["family"]`` passes every other leg unnoticed.
        # Registerpunkt 107: even with BOTH spellings old, a report that is also
        # forged elsewhere is not a bundle of the older version, and sending its
        # reader off to rebuild would explain away the forgery.  The named message
        # is therefore given only where the family is the ONLY difference.
        for field, value in (("weight", 401), ("classification", "sans"), ("cmap_sha256", "0" * 64)):
            with self.subTest(forged=field):
                forged = copy.deepcopy(report)
                forged["candidates"][0]["family"] = legacy
                forged["candidates"][0]["metadata"]["extracted"]["family"] = legacy
                forged["candidates"][0]["metadata"]["extracted"][field] = value
                with self.assertRaisesRegex(visual.VisualDecisionError, "font metadata or classification differs: serif-a"):
                    visual.validate_font_report(forged, root)
                # ... and the named message is not what it gets.
                try:
                    visual.validate_font_report(forged, root)
                except visual.VisualDecisionError as exc:
                    self.assertNotIn("written by an earlier version", str(exc))

        # The other half of the same rule, and the shape mutant M12 walked through:
        # the call site IS reached, everything but the family agrees, the candidate
        # spells the OLD name - and ``extracted`` spells a THIRD one.  An older
        # bundle spells the old name in BOTH places, so this is an edited report and
        # stays generic.  The legs above forge a NON-family field, which makes the
        # "family is the only difference" condition false and lets a rule that asks
        # only about ``candidate["family"]`` pass unnoticed.
        third = copy.deepcopy(report)
        third["candidates"][0]["family"] = legacy
        third["candidates"][0]["metadata"]["extracted"]["family"] = "Fixture Serif Third"
        with self.assertRaisesRegex(visual.VisualDecisionError, "font metadata or classification differs: serif-a"):
            visual.validate_font_report(third, root)
        try:
            visual.validate_font_report(third, root)
        except visual.VisualDecisionError as exc:
            self.assertNotIn("written by an earlier version", str(exc),
                             "an edited report must not be explained away as an older version")

        reached = copy.deepcopy(report)
        reached["candidates"][0]["family"] = legacy
        reached["candidates"][0]["metadata"]["extracted"]["weight"] = 401
        self.assertEqual(current, reached["candidates"][0]["metadata"]["extracted"]["family"],
                         "the two spellings have to disagree for this leg to measure anything")
        with self.assertRaisesRegex(visual.VisualDecisionError,
                                    "font metadata or classification differs: serif-a"):
            visual.validate_font_report(reached, root)

        # And a font that states no typographic family can never produce the named
        # refusal, whatever its report claims: there is no older spelling it could
        # have pinned, so the generic message is the honest one.
        other = copy.deepcopy(report)
        other["candidates"][1]["family"] = "Fixture Sans Other"
        other["candidates"][1]["metadata"]["extracted"]["family"] = "Fixture Sans Other"
        with self.assertRaisesRegex(visual.VisualDecisionError,
                                    "font metadata or classification differs: sans-a"):
            visual.validate_font_report(other, root)
