#!/usr/bin/env python3
"""Project validated Brand Package tokens without crossing medium ownership."""
from __future__ import annotations
import argparse,json
from pathlib import Path
import markdown_contract as contract

def project(path:Path)->dict:
    brand=json.loads(path.read_text(encoding="utf-8"));contract.validate_schema(brand,"brand-package.v1.schema.json","brand package")
    projection={"schema_version":"brand-token-projection.v1","pdf":brand["pdf"]}
    # Portable tokens exist only when the package owner made them explicit.
    # Preserve that JSON value and never infer it from a medium namespace.
    if "shared_visual_tokens" in brand: projection["shared_visual_tokens"]=brand["shared_visual_tokens"]
    return projection

def main()->int:
    p=argparse.ArgumentParser();p.add_argument("--brand",type=Path,required=True);p.add_argument("--json",action="store_true");a=p.parse_args()
    value=project(a.brand)
    if a.json:print(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")))
    return 0
if __name__=="__main__":raise SystemExit(main())
