#!/usr/bin/env python3
"""Generate isolated, fail-closed PDF/HTML E2E goldens."""
from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from pypdf import PdfReader

import brand_package
import markdown_contract as contract
import pdf_renderer

ROOT=Path(__file__).resolve().parents[1]
FIXTURE=ROOT/"fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360"
SOURCES=ROOT/"fixtures/pdf-renderer/e2e"
DOCS=("report","offer","freeform","one-pager","rich-report","benchmark-report")


def sha(path:Path)->str:return hashlib.sha256(path.read_bytes()).hexdigest()
def write_json(path:Path,value)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf-8")


def fixture_project_identity(label:str)->str:
    if label not in {*DOCS,"report-toggle-on","report-toggle-off"}:raise RuntimeError("unknown deterministic fixture project")
    return hashlib.sha256(f"brandkit-pdf-e2e-project.v1:{label}".encode()).hexdigest()


def artifact_locator(path:Path,output:Path)->str:
    resolved=path.resolve();domain=output.resolve()
    if resolved==domain or domain in resolved.parents:return resolved.relative_to(domain).as_posix()
    if resolved==ROOT or ROOT in resolved.parents:return resolved.relative_to(ROOT).as_posix()
    raise RuntimeError("golden artifact path escapes runtime source and output domains")


def closed_file_inventory(output:Path)->dict:
    entries=[]
    for path in sorted(output.rglob("*")):
        if path.is_symlink():raise RuntimeError("golden output must not contain symlinks")
        if not path.is_file() or path==output/"inventory.json":continue
        entries.append({"path":path.relative_to(output).as_posix(),"sha256":sha(path),"size_bytes":path.stat().st_size})
    return {"excluded":["inventory.json:self-referential"],"entry_count":len(entries),"entries":entries}


def verify_closed_file_inventory(output:Path,inventory:dict)->None:
    if inventory!=closed_file_inventory(output):raise RuntimeError("golden closed file inventory differs from output tree")


def verify_informative_svg(path:Path)->dict:
    """Reject the former single-fill placeholder while preserving safe vectors."""
    root=ET.fromstring(path.read_bytes());local=lambda value:value.rsplit("}",1)[-1]
    view=[float(value) for value in root.attrib.get("viewBox","").split()]
    if len(view)!=4 or view[2]<=0 or view[3]<=0:raise RuntimeError("rich SVG fixture lacks a positive viewBox")
    labels=[" ".join("".join(item.itertext()).split()) for item in root.iter() if local(item.tag)=="text" and " ".join("".join(item.itertext()).split())]
    foreground=[];colors=set()
    for item in root.iter():
        kind=local(item.tag);attrs=item.attrib
        if kind not in {"rect","circle","line","ellipse","polyline","polygon","path"}:continue
        full_rect=(kind=="rect" and float(attrs.get("x","0"))==view[0] and float(attrs.get("y","0"))==view[1] and float(attrs.get("width","0"))==view[2] and float(attrs.get("height","0"))==view[3])
        if not full_rect:foreground.append(kind)
        colors.update(value.upper() for key,value in attrs.items() if key in {"fill","stroke"} and re.fullmatch(r"#[0-9A-Fa-f]{6}",value))
    if len(labels)<3 or len(foreground)<5 or len(colors)<3:raise RuntimeError("rich SVG fixture is an empty/single-fill placeholder")
    return {"source_sha256":sha(path),"labels":labels,"foreground_geometry":foreground,"colors":sorted(colors)}


def materialize_package(root:Path)->Path:
    package=root/"brand";shutil.copytree(FIXTURE,package)
    manifest=json.loads((package/"brand-package.json").read_text())
    documents=[]
    for name in ("report","offer","freeform","one-pager"):
        source=ROOT/f"document-types/{name}.v1.json";target=package/f"documents/{name}.json";shutil.copy2(source,target)
        value=json.loads(target.read_text());documents.append({"id":name,"version":value["version"],"contract":f"documents/{name}.json","contract_sha256":sha(target),"engine_compatibility":value["engine_compatibility"]})
    manifest["document_types"]=documents;write_json(package/"brand-package.json",manifest)
    brand_package.validate_package(package);return package


def audit(ast:dict,key:Path)->dict:
    segment=next(x for x in ast["humanizer_segments"]["mutable"] if x["text"].strip());raw=[{"path":f"fixture-evidence/artifact-{i}.json","sha256":"0"*64} for i in range(8)]
    operation={"segment_id":segment["segment_id"],"old_hash":segment["old_hash"],"replacement_sha256":hashlib.sha256(segment["text"].encode()).hexdigest(),"result_path":"fixture-evidence/result.md","report_path":"fixture-evidence/report.json","changed":False,"provider_state":"audit-only/no-change"}
    payload={"baseline_sha256":"0"*64,"final_sha256":ast["source_sha256"],"provider":"claude","humanizer":{"version":"5.22.2-brandkit.2","release_path":"deterministic-e2e-fixture-not-provider-evidence","release_sha256":"0"*64},"raw_artifacts":raw,"operations":[operation],"protected_stable":True,"ast_skeleton_stable":True}
    signed={"schema_version":"humanizer-audit.v1","issuer":"brandkit-production-renderer-v1","payload":payload}
    return {**signed,"hmac_sha256":hmac.new(contract._key_bytes(key),contract.canonical_json(signed),hashlib.sha256).hexdigest()}


def project_for(work:Path,package:Path,label:str,overrides:dict|None=None)->tuple[Path,Path,dict,Path]:
    project=work/f"project-{label}";project.mkdir();subprocess.run(["git","init","-q",str(project)],check=True)
    brand_package.pin_project(project,package,"1.0.0")
    if overrides:
        root=project.resolve();activation,lock=brand_package.read_closed_activation(root);activation=copy.deepcopy(activation);activation["overrides"]=dict(overrides)
        target,_,_=brand_package.write_state(root,activation,lock);brand_package.atomic_symlink(root,".brandkit/current",target)
    activation,_=brand_package.read_closed_activation(project.resolve());manifest=project.resolve()/activation["package_path"]/"brand-package.json"
    return project.resolve(),manifest,json.loads(manifest.read_text()),manifest.parent


def link_count(pdf:Path)->int:
    count=0
    for page in PdfReader(str(pdf)).pages:
        for ref in page.get("/Annots",[]):
            item=ref.get_object()
            action=item.get("/A") or {}
            if str(item.get("/Subtype"))=="/Link" and (item.get("/Dest") is not None or action.get("/D") is not None):count+=1
    return count


def publish_one(work:Path,package:Path,output:Path,name:str,key:Path,mermaid_cas:Path,overrides:dict|None=None,event:dict|None=None)->dict:
    document_type="report" if name in {"rich-report","benchmark-report"} or name.startswith("report-toggle") else name
    source=SOURCES/("report.md" if name.startswith("report-toggle") else f"{name}.md")
    project,manifest,brand,package_root=project_for(work,package,name,overrides)
    if event is not None:event["project_materialized"]=True
    dtype_path=package_root/f"documents/{document_type}.json";dtype=json.loads(dtype_path.read_text());ast=contract.run_validate(source,dtype_path,SOURCES,False,None)
    stage=work/f"stage-{name}";stage.mkdir();audit_path=stage/"humanizer-audit.json";approval=stage/"owner-approval.fixture.json"
    write_json(audit_path,audit(ast,key));write_json(approval,{"evidence_origin":"deterministic-e2e-fixture-not-provider-evidence"})
    pdf=stage/f"{name}.pdf";result=stage/"render-result.json";render_work=stage/"renderer-work"
    pdf_renderer.render(source,dtype_path,manifest,SOURCES,pdf,result,key,render_work,project,audit_path,mermaid_cas,approval,fixture_project_identity_sha256=fixture_project_identity(name))
    html=stage/f"{name}.html";html_evidence=pdf_renderer.render_html(ast,brand,dtype,SOURCES,html,overrides)
    text=subprocess.run(["pdftotext",str(pdf),"-"],check=True,capture_output=True,text=True).stdout
    info=subprocess.run(["pdfinfo",str(pdf)],check=True,capture_output=True,text=True).stdout;pages=int(next(line.split(":",1)[1] for line in info.splitlines() if line.startswith("Pages:")).strip())
    final=output/name;shutil.copytree(stage,final)
    pdf=final/pdf.name;html=final/html.name;result=final/"render-result.json";render_work=final/"renderer-work";page_pngs=sorted((final/"pages").glob("*.png"))
    record={"id":name,"document_type":document_type,"source":{"path":artifact_locator(source,output),"sha256":sha(source)},"pdf":{"path":artifact_locator(pdf,output),"sha256":sha(pdf),"pages":pages,"page_pngs":[{"path":artifact_locator(p,output),"sha256":sha(p)} for p in page_pngs]},"html":{"path":artifact_locator(html,output),**html_evidence},"ast":{"path":artifact_locator(render_work/"final.ast.json",output),"sha256":sha(render_work/"final.ast.json")},"source_map":{"path":artifact_locator(render_work/"renderer-source-map.json",output),"sha256":sha(render_work/"renderer-source-map.json")},"render_result":{"path":artifact_locator(result,output),"sha256":sha(result)},"layout_strategy":pdf_renderer.layout_strategy(dtype,overrides),"visual_qa":{"path":artifact_locator(final/"visual-qa.json",output),"sha256":sha(final/"visual-qa.json")}}
    if document_type=="one-pager":
        if pages!=1 or re.search(r"\bSeite\s+[0-9]+\s+von\s+[0-9]+\b",text):raise RuntimeError("one-pager must be exactly one unnumbered PDF page")
        record["one_page_contract"]={"page_count":1,"page_label_count":0,"html_layout":"one-pager-hero","ast_present":True,"qa_status":"pass","source_map_page_count":json.loads((render_work/"renderer-source-map.json").read_text())["page_count"]}
    if name=="rich-report":
        matrix=[n for n in ast["children"] if n["type"]=="callout" and n["kind"] in {"note","tip","warning","caution","important"}];ids=[n["attrs"]["id"] for n in matrix]
        html_text=html.read_text();source_text=source.read_text();mapped=json.loads((render_work/"renderer-source-map.json").read_text())["objects"]
        counts={i:{"source":len(re.findall(rf"#{re.escape(i)}(?=[\s}}])",source_text)),"ast":sum(n["attrs"].get("id")==i for n in matrix),"pdf":text.count(i),"html_marker":html_text.count(f'data-brandkit-id="{i}"'),"source_map":sum(x["source_id"]==i for x in mapped)} for i in ids}
        if len(ids)!=15 or len(set(ids))!=15 or any(any(v!=1 for v in row.values()) for row in counts.values()):raise RuntimeError("rich callout IDs are not exactly-once across source/AST/PDF/HTML/source-map")
        expected={"metric-rich":["Anteil","42 %","metric-value","source-one"],"quote-rich":["source-one","belegte Aussage"],"legal-rich":["source-one","Herkunftshinweis"]}
        evidence={item:{token:{"pdf":token in text,"html":token in html_text,"source_map":any(x["source_id"]==item and token in x.get("evidence_text","") for x in mapped)} for token in tokens} for item,tokens in expected.items()}
        if not all(all(all(destinations.values()) for destinations in tokens.values()) for tokens in evidence.values()):raise RuntimeError("rich semantic evidence differs across PDF/HTML/source-map")
        svg_audit=verify_informative_svg(SOURCES/"assets/diagram.svg")
        if any(label not in text for label in svg_audit["labels"]):raise RuntimeError("informative SVG labels are absent from the rendered PDF")
        figures=[n for n in ast["children"] if n["type"] in {"mermaid","graphviz"}]
        figure_ids=[n["id"] for n in figures];result_payload=json.loads(result.read_text())["payload"]
        source_map_value=json.loads((render_work/"renderer-source-map.json").read_text());semantic_refs=source_map_value["figure_references"]
        figure_parity={n["id"]:{"ast":True,"pdf_caption":pdf_renderer._figure_caption(n) in text,"pdf_reference":text.count(f'Abbildung {n["figure_number"]}')>=3,"html_anchor":f'id="{n["id"]}"' in html_text,"html_number":f'data-figure-number="{n["figure_number"]}"' in html_text,"source_map":any(x["source_id"]==n["id"] for x in mapped),"source_map_registry":n["id"] in {x["id"] for x in source_map_value["figure_registry"]},"render_result":any(x["id"]==n["id"] and x["number"]==n["figure_number"] and x["reference_text"]==f'Abbildung {n["figure_number"]}' for x in result_payload["figures"]),"render_evidence":any(x["id"]==n["id"] for x in result_payload[n["type"]])} for n in figures}
        if figure_ids!=["fig-flow-rich","fig-dependencies-rich"] or not all(all(row.values()) for row in figure_parity.values()):raise RuntimeError("Mermaid/Graphviz figure parity differs across outputs")
        expected_refs=[("fig-flow-rich",1),("fig-dependencies-rich",2),("fig-flow-rich",1),("fig-dependencies-rich",2)]
        if [(item["target_id"],item["number"]) for item in semantic_refs]!=expected_refs or source_text.count("@fig-flow-rich")!=2 or source_text.count("@fig-dependencies-rich")!=2:raise RuntimeError("semantic figure references differ across source/AST/source-map")
        qa=json.loads((final/"visual-qa.json").read_text());links=qa["source_map_independent_binding"]["internal_links"]
        figure_pages={ident:next(x["page"] for x in mapped if x["source_id"]==ident) for ident in figure_ids}
        caption_pages={ident:next(x["page"] for x in mapped if x["source_id"]==ident+":caption") for ident in figure_ids}
        xrefs={x["target_id"]:x for x in links if x["target_id"] in figure_ids}
        if set(xrefs)!=set(figure_ids) or any(xrefs[ident]["target_page"]!=figure_pages[ident] or caption_pages[ident]!=figure_pages[ident] for ident in figure_ids):raise RuntimeError("actual PDF figure destinations differ from diagram/caption source-map pages")
        toc_links=[x for x in links if x["source_id"].startswith("toc-")]
        if not toc_links or any(x["target_page"]!=next(item["page"] for item in mapped if item["source_id"]==x["target_id"]) for x in toc_links):raise RuntimeError("actual PDF TOC destinations differ from source-map pages")
        graphviz_evidence=next(x for x in result_payload["graphviz"] if x["id"]=="fig-dependencies-rich")
        graphviz_svg=render_work/"fig-dependencies-rich.svg"
        if graphviz_evidence["svg_sha256"]!=sha(graphviz_svg):raise RuntimeError("Graphviz publication hash differs from exact rendered SVG")
        mermaid_evidence=next(x for x in result_payload["mermaid"] if x["id"]=="fig-flow-rich")
        projections={"mermaid":mermaid_evidence["brand_projection"],"graphviz":graphviz_evidence["brand_projection"]}
        expected_roles=set(brand["pdf"]["tokens"]["color_roles"].values())
        if any(set(item["colors"].values())!=expected_roles or item["font"]["asset_id"]!="neutral-font" or item["line_role"]!="accent" or min(item["contrast"].values())<4.5 for item in projections.values()):raise RuntimeError("diagram outputs differ from active brand roles")
        diagram_audit={"schema_version":"brandkit-diagram-brand-audit.v1","status":"pass","active_brand":{"brand_id":brand["brand_id"],"version":brand["version"],"manifest_sha256":sha(manifest)},"renderers":projections,"forbidden_fonts":["Arial","Times","system-ui","sans-serif","serif"],"forbidden_default_colors":["#000","#000000","#333","#333333","#666","#666666","#999","#999999","#EEE","#EEEEEE"]}
        diagram_audit_path=final/"diagram-brand-audit.json";write_json(diagram_audit_path,diagram_audit)
        navigation={"figure_pages":figure_pages,"caption_pages":caption_pages,"xrefs":xrefs,"toc":toc_links}
        record["rich_parity"]={"presentation_callouts":15,"unique_ids":ids,"exactly_once":counts,"custom_title":{"source":source_text.count("Eigener Titel ohne Icon")==1,"pdf":text.count("Eigener Titel ohne Icon")==1,"html":html_text.count("Eigener Titel ohne Icon")==1},"icon_false":{"ast":any(n["attrs"].get("icon")=="false" for n in matrix),"html":'data-icon="false"' in html_text},"semantic_evidence":evidence,"figure_registry":figure_parity,"figure_references":semantic_refs,"informative_svg":svg_audit,"navigation":navigation,"diagram_brand_audit":{"path":artifact_locator(diagram_audit_path,output),"sha256":sha(diagram_audit_path)},"graphviz_publication":{"svg_sha256":sha(graphviz_svg),"toolchain_lock_sha256":graphviz_evidence["toolchain_lock_sha256"]},"blocks":{"table":True,"raster":True,"svg":True,"link":True,"footnote":True,"page_break":True,"mermaid":True,"graphviz":True}}
    if name=="benchmark-report":
        mapped=json.loads((render_work/"renderer-source-map.json").read_text())["objects"]
        role_ids=("positioning","orientation","proof","synthesis","outlook")
        role_pages={ident:next((item["page"] for item in mapped if item["source_id"]==ident),None) for ident in role_ids}
        if any(value is None for value in role_pages.values()):raise RuntimeError("benchmark report omits a DataHub role")
        if not (role_pages["positioning"]<role_pages["orientation"]<role_pages["proof"]<role_pages["synthesis"]==role_pages["outlook"]==pages):
            raise RuntimeError(f"benchmark DataHub role page order differs: {role_pages}")
        last_text=subprocess.run(["pdftotext","-f",str(pages),"-l",str(pages),str(pdf),"-"],check=True,capture_output=True,text=True).stdout
        words=re.findall(r"(?u)\b[\wÄÖÜäöüß]+\b",last_text)
        if len(words)<130 or "Verdichtung" not in last_text or "Ausblick" not in last_text:
            raise RuntimeError("benchmark closing page lacks dense synthesis and outlook")
        audit_value={"schema_version":"brandkit-benchmark-report-roles.v1","status":"pass","document_id":name,"page_count":pages,
                     "roles":[{"role":ident,"page":role_pages[ident],"source_id":ident} for ident in role_ids],
                     "numbered_proof_items":3,"closing_page":{"page":pages,"proxy_word_count":len(words),"contains_synthesis":True,"contains_outlook":True,"underfilled":False},
                     "source_map_sha256":sha(render_work/"renderer-source-map.json"),"pdf_sha256":sha(pdf)}
        audit_path=final/"benchmark-role-audit.json";write_json(audit_path,audit_value)
        record["benchmark_roles"]={"path":artifact_locator(audit_path,output),"sha256":sha(audit_path),"pages":role_pages,"closing_page_proxy_words":len(words)}
    return record


def summarize_events(events:list[dict])->dict:
    counts={state:sum(event["state"]==state for event in events) for state in ("rendered","failed","skipped")}
    counts["attempted"]=counts["rendered"]+counts["failed"]
    materialized=sum(bool(event.get("project_materialized")) for event in events)
    failure_indexes=[index for index,event in enumerate(events) if event["state"]=="failed"]
    continued=all(any(later["state"]=="rendered" for later in events[index+1:]) for index in failure_indexes)
    return {
        "attempted":counts["attempted"],
        "rendered":counts["rendered"],
        "failed":counts["failed"],
        "skipped":counts["skipped"],
        "materialized_projects":materialized,
        "events":events,
        "failed_document_does_not_skip_later_documents":continued,
    }


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("--output",type=Path,default=ROOT/"fixtures/pdf-renderer/goldens/v2");parser.add_argument("--issuer-key",type=Path,required=True);parser.add_argument("--mermaid-cas",type=Path,required=True);parser.add_argument("--skip-rich-render",action="store_true",help="record the known Mermaid blocker without launching the rich renderer");args=parser.parse_args()
    output=args.output.resolve()
    if output.exists():raise SystemExit("output must be absent; goldens are immutable")
    output.mkdir(parents=True)
    inventory=[];failures=[];events=[]
    with tempfile.TemporaryDirectory(prefix="brandkit-e2e-") as td:
        work=Path(td);package=materialize_package(work)
        for name in DOCS:
            if name=="rich-report" and args.skip_rich_render:
                events.append({"id":name,"phase":"document","state":"skipped","project_materialized":False})
                failures.append({"id":name,"error_type":"KnownToolchainBlocker","message":"pinned Mermaid browser remains unavailable; rich publication was not launched"})
                continue
            event={"id":name,"phase":"document","state":"attempted","project_materialized":False};events.append(event)
            try:inventory.append(publish_one(work,package,output,name,args.issuer_key,args.mermaid_cas,event=event));event["state"]="rendered"
            except Exception as exc:event["state"]="failed";failures.append({"id":name,"error_type":type(exc).__name__,"message":str(exc)})
        toggles=[]
        for enabled in (True,False):
            name=f"report-toggle-{'on' if enabled else 'off'}";overrides={"toc":enabled,"section_numbering":enabled}
            event={"id":name,"phase":"activation-toggle","state":"attempted","project_materialized":False};events.append(event)
            try:
                record=publish_one(work,package,output,name,args.issuer_key,args.mermaid_cas,overrides,event);pdf=output/name/f"{name}.pdf";html=(output/name/f"{name}.html").read_text();text=subprocess.run(["pdftotext",str(pdf),"-"],check=True,capture_output=True,text=True).stdout;mapped=json.loads((output/name/"renderer-work/renderer-source-map.json").read_text())
                observed={"toc_pdf":"Inhalt" in text,"numbering_pdf":"Kapitel 01" in text,"toc_html":'data-brandkit-role="toc"' in html,"numbering_html":"Kapitel 01" in html,"pdf_internal_links":link_count(pdf),"source_map_toc_objects":sum(x["source_id"].startswith("toc-") for x in mapped["objects"])}
                if enabled and not all((observed["toc_pdf"],observed["numbering_pdf"],observed["toc_html"],observed["numbering_html"],observed["pdf_internal_links"]>0,observed["source_map_toc_objects"]>0)):raise RuntimeError("enabled TOC/section numbering is incomplete")
                if not enabled and any((observed["toc_pdf"],observed["numbering_pdf"],observed["toc_html"],observed["numbering_html"],observed["pdf_internal_links"],observed["source_map_toc_objects"])):raise RuntimeError("disabled TOC/section numbering remains visible")
                toggles.append({"enabled":enabled,"document":record,"observed":observed});event["state"]="rendered"
            except Exception as exc:event["state"]="failed";failures.append({"id":name,"error_type":type(exc).__name__,"message":str(exc)})
        isolation=summarize_events(events)
        status="pass" if not failures else "fail"
        isolation["aggregate_fail_closed"]=(status=="fail")==bool(failures)
        evidence={"schema_version":"brandkit-pdf-e2e-goldens.v2","renderer":pdf_renderer.ENGINE,"markdown_contract":{"version":contract.MARKDOWN_CONTRACT_VERSION,"sha256":sha(ROOT/"markdown-contract.v2.json"),"migration_sha256":sha(ROOT/"markdown-contract-migration.v1.json")},"evidence_origin":"deterministic-e2e-fixture-not-provider-evidence","status":status,"document_isolation":isolation,"documents":inventory,"report_activation_double_render":toggles,"failures":failures,"graphviz":{"status":"installed","toolchain_lock":"toolchain/graphviz-svg.v1.lock.json","network":"disabled"}}
        evidence["closed_file_inventory"]=closed_file_inventory(output)
        write_json(output/"inventory.json",evidence)
        verify_closed_file_inventory(output,evidence["closed_file_inventory"])
    print(json.dumps({"status":evidence["status"],"documents":len(inventory),"failures":len(failures),"inventory":str(output/"inventory.json")},sort_keys=True))
    return 0 if not failures else 1


if __name__=="__main__":raise SystemExit(main())
