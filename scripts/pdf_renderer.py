#!/usr/bin/env python3
"""Deterministic BrandKit Markdown v1 to PDF renderer.

The renderer consumes the closed Markdown AST; it never interprets raw HTML,
CSS, external URLs, or unknown directives.  Production results are HMAC-bound
to the committed renderer trust profile and to the exact final Markdown bytes.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, Image, KeepTogether, ListFlowable,
    ListItem, PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

import markdown_contract as contract
import brand_package
import pdf_visual_qa

ROOT = Path(__file__).resolve().parents[1]
ENGINE = {"name": "brandkit-pdf-renderer", "version": "1.2.28"}
ISSUER = "brandkit-production-renderer-v1"
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
# Structured loopback sink: npm receives an unreachable local proxy, never an
# external endpoint. Keeping host/port explicit makes the offline boundary
# machine-testable and reviewable without allowlisting a URL-shaped string.
OFFLINE_PROXY = {"scheme": "http", "host": "127.0.0.1", "port": 9}


class RenderError(Exception):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _hex(value: str, label: str) -> colors.Color:
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise RenderError(f"missing or invalid brand color token: {label}")
    return colors.HexColor(value)


def validate_brand(path: Path, dtype: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise RenderError("brand manifest must be a regular file")
    brand = load_json(path)
    contract.validate_schema(brand, "brand-package.v1.schema.json", "brand package")
    for asset_class in ("fonts","logos"):
        for asset in brand["assets"][asset_class]:
            rights_path=path.parent/asset["rights"]["evidence_path"]
            if rights_path.is_symlink() or not rights_path.is_file() or sha(rights_path.read_bytes())!=asset["rights"]["evidence_sha256"]:
                raise RenderError(f"{asset['id']} rights evidence path/hash failed")
            rights=load_json(rights_path);contract.validate_schema(rights,"rights-evidence.v1.schema.json",f"{asset['id']} rights evidence")
            if rights["asset_id"]!=asset["id"] or rights["status"]=="todo-review" or rights["todo"] is not None:
                raise RenderError(f"{asset['id']} production rights are not closed")
    tokens = brand["pdf"]["tokens"]
    required_colors = {"primary", "accent", "text", "muted", "background", "surface"}
    missing = required_colors - set(tokens["color_roles"])
    if missing:
        raise RenderError(f"brand package lacks renderer color roles: {sorted(missing)}")
    for role in required_colors:
        _hex(tokens["color_roles"][role], role)
    required_type = {"body", "heading", "mono"}
    if required_type - set(tokens["typography_roles"]):
        raise RenderError(f"brand package lacks typography roles: {sorted(required_type-set(tokens['typography_roles']))}")
    lock=load_json(ROOT / "toolchain/pdf-renderer.production.v1.lock.json")
    allowed_fonts = set(lock["builtin_fonts"]);font_assets={x["id"]:x for x in brand["assets"]["fonts"]}
    roles=set(tokens["typography_roles"].values())
    if not roles.issubset(allowed_fonts|set(font_assets)):raise RenderError("typography role is neither pinned builtin nor declared font asset")
    runtime_fonts={}
    for font_id in sorted(roles-set(allowed_fonts)):
        asset=font_assets[font_id];woff=path.parent/asset["path"]
        derivative=lock.get("font_derivatives",{}).get(asset["sha256"])
        if not derivative or sha(woff.read_bytes())!=asset["sha256"]:raise RenderError("font asset has no pinned deterministic TrueType derivative")
        ttf=ROOT/derivative["path"]
        if sha(ttf.read_bytes())!=derivative["sha256"] or derivative["source_sha256"]!=asset["sha256"]:raise RenderError("font derivative hash/source binding failed")
        name=f"BrandKit-{font_id}";pdfmetrics.registerFont(TTFont(name,str(ttf)));runtime_fonts[font_id]=name
    brand["_renderer_fonts"]={**{x:x.split(":",1)[1] for x in allowed_fonts},**runtime_fonts}
    if float(tokens["page"]["margin_mm"]) < float(dtype["minimum_margin_mm"]):
        raise RenderError("brand page margin is below document-type minimum")
    declared = [x for x in brand["document_types"] if x["id"] == dtype["id"]]
    if len(declared) != 1 or declared[0]["version"] != dtype["version"]:
        raise RenderError("brand/document-type resolution is missing or ambiguous")
    return brand, sha(path.read_bytes())


def inline_text(tokens: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for token in tokens:
        kind = token["type"]
        if kind == "text": out.append(html.escape(token["text"]))
        elif kind == "protected": out.append(html.escape(token["literal"]))
        elif kind == "link": out.append(f'<link href="{html.escape(token["target"],quote=True)}">{html.escape(token["label"])}</link>')
        elif kind == "figure-ref": out.append(f'<link href="#{html.escape(token["target"],quote=True)}">{html.escape(token["reference_text"])}</link>')
        elif kind == "footnote-ref": out.append(f'<super>[{token["source_id"]}]</super>')
        else: raise RenderError(f"unsupported inline token: {kind}")
    return "".join(out)


class Callout(Flowable):
    def __init__(self, title: str, body: str, width: float, accent: colors.Color, surface: colors.Color, style: ParagraphStyle, appearance: str="default", icon: bool=True):
        super().__init__(); self.title=title; self.body=body; self.width=width; self.accent=accent; self.surface=surface; self.style=style;self.appearance=appearance;self.icon=icon
        self.p = Paragraph(f"<b>{title}</b><br/>{body}", style)
    def wrap(self, availWidth: float, availHeight: float) -> tuple[float,float]:
        _w,h=self.p.wrap(availWidth-14*mm,availHeight); self.height=h+6*mm; return availWidth,self.height
    def draw(self) -> None:
        if self.appearance=="default":
            self.canv.setFillColor(self.surface); self.canv.roundRect(0,0,self.width,self.height,2*mm,stroke=0,fill=1)
            self.canv.setFillColor(self.accent); self.canv.rect(0,0,2*mm,self.height,stroke=0,fill=1)
        elif self.appearance=="simple":
            self.canv.setStrokeColor(self.accent);self.canv.setLineWidth(.7);self.canv.roundRect(0,0,self.width,self.height,2*mm,stroke=1,fill=0)
        else:
            self.canv.setStrokeColor(self.accent);self.canv.setLineWidth(1.2);self.canv.line(0,self.height,self.width,self.height)
        if self.icon:
            self.canv.setFillColor(self.accent);self.canv.circle(4.5*mm,self.height-4*mm,1.25*mm,stroke=0,fill=1)
        self.p.drawOn(self.canv,8*mm,3*mm)


class SafeSvgFlowable(Flowable):
    """Render the closed static SVG subset already accepted by the contract."""
    def __init__(self, path: Path, width: float, font_name: str="Helvetica"):
        super().__init__(); root=ET.fromstring(path.read_bytes()); local=lambda s:s.rsplit("}",1)[-1]
        allowed={"svg","title","desc","rect","circle","line","text"}
        if any(local(e.tag) not in allowed for e in root.iter()):raise RenderError("PDF v1 image renderer supports only safe SVG metadata, geometry, and labels")
        view=[float(x) for x in root.attrib.get("viewBox","").split()]
        if len(view)!=4 or view[2]<=0 or view[3]<=0:raise RenderError("safe SVG requires a positive viewBox")
        self.vw,self.vh=view[2],view[3];self.width=min(width,120*mm);self.height=self.width*self.vh/self.vw;self.shapes=[];self.font_name=font_name
        for e in root.iter():
            if local(e.tag)=="rect":
                attrs=e.attrib
                def n(k:str,default:float)->float:return float(attrs.get(k,default))
                fill=attrs.get("fill")
                if not fill or not HEX.fullmatch(fill):raise RenderError("safe SVG rect requires a six-digit fill")
                self.shapes.append(("rect",n("x",0),n("y",0),n("width",self.vw),n("height",self.vh),colors.HexColor(fill)))
            elif local(e.tag)=="circle":
                attrs=e.attrib;fill=attrs.get("fill")
                if not fill or not HEX.fullmatch(fill):raise RenderError("safe SVG circle requires a six-digit fill")
                self.shapes.append(("circle",float(attrs["cx"]),float(attrs["cy"]),float(attrs["r"]),colors.HexColor(fill)))
            elif local(e.tag)=="line":
                attrs=e.attrib;stroke=attrs.get("stroke")
                if not stroke or not HEX.fullmatch(stroke):raise RenderError("safe SVG line requires a six-digit stroke")
                self.shapes.append(("line",float(attrs["x1"]),float(attrs["y1"]),float(attrs["x2"]),float(attrs["y2"]),float(attrs.get("stroke-width","1")),colors.HexColor(stroke)))
            elif local(e.tag)=="text":
                attrs=e.attrib;fill=attrs.get("fill");literal=(e.text or "").strip()
                if not fill or not HEX.fullmatch(fill) or attrs.get("text-anchor")!="middle" or not literal or len(literal)>80:raise RenderError("safe SVG text requires a closed centered visible label")
                self.shapes.append(("text",float(attrs["x"]),float(attrs["y"]),float(attrs.get("font-size","12")),literal,colors.HexColor(fill)))
        if not self.shapes:raise RenderError("safe SVG contains no visible geometry")
    def draw(self)->None:
        sx=self.width/self.vw;sy=self.height/self.vh
        for shape in self.shapes:
            if shape[0]=="rect":
                _,x,y,w,h,fill=shape;self.canv.setFillColor(fill);self.canv.rect(x*sx,self.height-(y+h)*sy,w*sx,h*sy,fill=1,stroke=0)
            elif shape[0]=="circle":
                _,cx,cy,r,fill=shape;self.canv.setFillColor(fill);self.canv.circle(cx*sx,self.height-cy*sy,r*min(sx,sy),fill=1,stroke=0)
            elif shape[0]=="line":
                _,x1,y1,x2,y2,line_width,stroke=shape;self.canv.setStrokeColor(stroke);self.canv.setLineWidth(line_width*min(sx,sy));self.canv.line(x1*sx,self.height-y1*sy,x2*sx,self.height-y2*sy)
            else:
                _,x,y,size,literal,fill=shape;self.canv.setFillColor(fill);self.canv.setFont(self.font_name,max(6.,size*min(sx,sy)));self.canv.drawCentredString(x*sx,self.height-y*sy,literal)


class DestinationImage(Image):
    """Bind one rendered figure to one stable PDF destination."""
    def __init__(self, filename: str, destination: str, **kwargs: Any):
        super().__init__(filename, **kwargs);self.destination=destination
    def draw(self) -> None:
        self.canv.bookmarkHorizontal(self.destination,0,self.drawHeight)
        super().draw()


class DestinationParagraph(Paragraph):
    """Bind a heading destination to the exact painted flowable top edge."""
    def __init__(self, text: str, style: ParagraphStyle, destination: str):
        super().__init__(text, style);self.destination=destination
    def draw(self) -> None:
        self.canv.bookmarkHorizontal(self.destination,0,self.height)
        super().draw()


class GraphvizSvgFlowable(Flowable):
    """Paint the exact, inert SVG vocabulary emitted by the pinned Viz.js DOT subset."""
    def __init__(self,path:Path,width:float,destination:str,font_name:str):
        super().__init__();self.destination=destination;self.font_name=font_name
        root=ET.fromstring(path.read_bytes());local=lambda value:value.rsplit("}",1)[-1]
        allowed={"svg","g","title","polygon","ellipse","path","text"}
        if any(local(element.tag) not in allowed for element in root.iter()):raise RenderError("Graphviz emitted an unsupported SVG element")
        for element in root.iter():
            for key,value in element.attrib.items():
                name=local(key)
                if name not in {"width","height","viewBox","id","class","transform","fill","stroke","points","cx","cy","rx","ry","d","space","text-anchor","x","y","font-family","font-size","data-brand-primary","data-brand-accent","data-brand-text","data-brand-muted","data-brand-background","data-brand-surface"}:raise RenderError(f"Graphviz emitted an unsupported SVG attribute: {name}")
                if re.search(r"(?i)(?:[a-z][a-z0-9+.-]*:|url\s*\(|href|image|script)",value):raise RenderError("Graphviz emitted an active/external SVG attribute")
        view=[float(x) for x in root.attrib.get("viewBox","").split()]
        if len(view)!=4 or view[2]<=0 or view[3]<=0:raise RenderError("Graphviz SVG requires a positive viewBox")
        self.vx,self.vy,self.vw,self.vh=view;self.width=min(width,150*mm);self.height=self.width*self.vh/self.vw
        graph=next((element for element in root if local(element.tag)=="g"),None)
        if graph is None:raise RenderError("Graphviz SVG has no graph group")
        transform=re.fullmatch(r"scale\(1 1\) rotate\(0\) translate\((-?[0-9.]+) (-?[0-9.]+)\)",graph.attrib.get("transform",""))
        if transform is None:raise RenderError("Graphviz SVG transform differs from the pinned form")
        self.tx,self.ty=map(float,transform.groups());self.shapes=[]
        colour=lambda value: colors.white if value=="white" else colors.black if value=="black" else None if value=="none" else colors.HexColor(value) if HEX.fullmatch(value or "") else (_ for _ in ()).throw(RenderError("Graphviz SVG colour is outside the closed palette"))
        for element in graph.iter():
            kind=local(element.tag);a=element.attrib
            if kind=="polygon":
                points=[tuple(map(float,pair.split(","))) for pair in a["points"].split()]
                if len(points)<3:raise RenderError("Graphviz polygon has fewer than three points")
                self.shapes.append((kind,points,colour(a.get("fill","none")),colour(a.get("stroke","none"))))
            elif kind=="ellipse":self.shapes.append((kind,float(a["cx"]),float(a["cy"]),float(a["rx"]),float(a["ry"]),colour(a.get("fill","none")),colour(a.get("stroke","none"))))
            elif kind=="path":
                path=re.fullmatch(r"M(-?[0-9.]+),(-?[0-9.]+)(.*)",a["d"])
                if path is None:raise RenderError("Graphviz path lacks a closed move command")
                rest=path.group(3);commands=[]
                while rest:
                    curve=re.match(r"C(-?[0-9.]+),(-?[0-9.]+) (-?[0-9.]+),(-?[0-9.]+) (-?[0-9.]+),(-?[0-9.]+)",rest)
                    line=re.match(r"L(-?[0-9.]+),(-?[0-9.]+)",rest)
                    if curve:commands.append(("C",*map(float,curve.groups())));rest=rest[curve.end():]
                    elif line:commands.append(("L",*map(float,line.groups())));rest=rest[line.end():]
                    else:raise RenderError("Graphviz path command is outside M/C/L")
                self.shapes.append((kind,float(path.group(1)),float(path.group(2)),commands,colour(a.get("fill","none")),colour(a.get("stroke","none"))))
            elif kind=="text":
                if a.get("text-anchor")!="middle" or not (element.text or "").strip():raise RenderError("Graphviz text differs from the centered literal form")
                self.shapes.append((kind,float(a["x"]),float(a["y"]),float(a["font-size"]),(element.text or "").strip(),colour(a.get("fill","black"))))
        if not any(shape[0] in {"polygon","ellipse","path"} for shape in self.shapes):raise RenderError("Graphviz SVG contains no visible geometry")
    def _point(self,x:float,y:float)->tuple[float,float]:
        sx=self.width/self.vw;sy=self.height/self.vh;return ((x+self.tx-self.vx)*sx,self.height-(y+self.ty-self.vy)*sy)
    def draw(self)->None:
        self.canv.bookmarkHorizontal(self.destination,0,self.height)
        scale=min(self.width/self.vw,self.height/self.vh)
        for shape in self.shapes:
            kind=shape[0]
            if kind=="polygon":
                _,points,fill,stroke=shape;p=self.canv.beginPath();x,y=self._point(*points[0]);p.moveTo(x,y)
                for raw in points[1:]:x,y=self._point(*raw);p.lineTo(x,y)
                p.close();self.canv.setFillColor(fill or colors.white);self.canv.setStrokeColor(stroke or colors.white);self.canv.drawPath(p,fill=int(fill is not None),stroke=int(stroke is not None))
            elif kind=="ellipse":
                _,cx,cy,rx,ry,fill,stroke=shape;x,y=self._point(cx,cy);self.canv.setFillColor(fill or colors.white);self.canv.setStrokeColor(stroke or colors.white);self.canv.ellipse(x-rx*scale,y-ry*scale,x+rx*scale,y+ry*scale,fill=int(fill is not None),stroke=int(stroke is not None))
            elif kind=="path":
                _,x0,y0,commands,fill,stroke=shape;p=self.canv.beginPath();x,y=self._point(x0,y0);p.moveTo(x,y)
                for command in commands:
                    if command[0]=="L":x,y=self._point(command[1],command[2]);p.lineTo(x,y)
                    else:
                        points=[self._point(command[i],command[i+1]) for i in (1,3,5)];p.curveTo(*(coordinate for point in points for coordinate in point))
                self.canv.setFillColor(fill or colors.white);self.canv.setStrokeColor(stroke or colors.white);self.canv.drawPath(p,fill=int(fill is not None),stroke=int(stroke is not None))
            else:
                _,x,y,size,literal,fill=shape;px,py=self._point(x,y);font_size=max(6.,size*scale);self.canv.setFillColor(fill or colors.black);self.canv.setFont(self.font_name,font_size);self.canv.drawCentredString(px,py,literal)


class MermaidSvgFlowable(Flowable):
    """Paint the complete CSS-free Mermaid publication SVG as PDF vectors."""
    MAX_BYTES=1_000_000;MAX_ELEMENTS=512;MAX_PATH_TOKENS=4096
    NUMBER=r"-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)"

    @classmethod
    def _number(cls,value:str,label:str)->float:
        if re.fullmatch(cls.NUMBER,value or "") is None:raise RenderError(f"Mermaid SVG {label} is not a closed number")
        number=float(value)
        if not math.isfinite(number) or abs(number)>1_000_000:raise RenderError(f"Mermaid SVG {label} exceeds the coordinate limit")
        return number

    @classmethod
    def _path(cls,value:str)->list[tuple[Any,...]]:
        if len(value)>65536 or re.search(r"[^0-9eE+.,\- MmLlCcZz]",value):raise RenderError("Mermaid SVG path contains an unsupported command")
        tokens=re.findall(r"[MLCZmlcz]|-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?",value)
        if not tokens or len(tokens)>cls.MAX_PATH_TOKENS or "".join(tokens).lower()!=re.sub(r"[\s,]","",value).lower():raise RenderError("Mermaid SVG path tokenization is not closed")
        result=[];index=0;command=None;current=(0.,0.);start=(0.,0.)
        widths={"M":2,"L":2,"C":6,"Z":0}
        while index<len(tokens):
            token=tokens[index]
            if token.upper() in widths:command=token;index+=1
            if command is None:raise RenderError("Mermaid SVG path lacks an initial command")
            upper=command.upper();relative=command.islower();width=widths[upper]
            if width==0:
                result.append(("Z",));current=start;command=None;continue
            if index+width>len(tokens) or any(tokens[i].upper() in widths for i in range(index,index+width)):raise RenderError("Mermaid SVG path command is incomplete")
            values=[cls._number(tokens[i],"path coordinate") for i in range(index,index+width)];index+=width
            if upper in {"M","L"}:
                x,y=values
                if relative:x+=current[0];y+=current[1]
                result.append((upper,x,y));current=(x,y)
                if upper=="M":start=current;command="l" if relative else "L"
            else:
                points=[]
                for offset in range(0,6,2):
                    x,y=values[offset:offset+2]
                    if relative:x+=current[0];y+=current[1]
                    points.extend((x,y))
                result.append(("C",*points));current=(points[4],points[5])
        return result

    def __init__(self,path:Path,width:float,destination:str,font_name:str):
        super().__init__();self.destination=destination;self.font_name=font_name
        raw=path.read_bytes()
        if len(raw)>self.MAX_BYTES or any(token in raw.lower() for token in (b"<!doctype",b"<!entity",b"<style",b"@import",b"url(",b"javascript:",b"data:",b"file:",b"http:",b"https:",b"xlink:",b"href=")):
            raise RenderError("Mermaid SVG contains CSS, entities, or an active/external reference")
        try:root=ET.fromstring(raw)
        except ET.ParseError as exc:raise RenderError("Mermaid SVG is not closed XML") from exc
        local=lambda value:value.rsplit("}",1)[-1];elements=list(root.iter())
        if len(elements)>self.MAX_ELEMENTS or any(local(element.tag) not in {"svg","path","rect","text"} for element in elements):raise RenderError("Mermaid SVG contains an unsupported or excessive element domain")
        allowed={"svg":{"width","height","viewBox","data-brand-primary","data-brand-accent","data-brand-text","data-brand-muted","data-brand-background","data-brand-surface"},"path":{"d","fill","stroke","stroke-width"},"rect":{"x","y","width","height","rx","ry","fill","stroke","stroke-width"},"text":{"x","y","fill","font-family","font-size","text-anchor"}}
        for element in elements:
            kind=local(element.tag)
            for key,value in element.attrib.items():
                name=local(key)
                if name not in allowed[kind] or re.search(r"(?is)(?:url\s*\(|@import|[a-z][a-z0-9+.-]*:|\\[0-9a-f]{1,6}|&#|&[a-z])",value):raise RenderError("Mermaid SVG contains an unsupported attribute or encoded reference")
        view=[self._number(value,"viewBox") for value in root.attrib.get("viewBox","").split()]
        if len(view)!=4 or view[2]<=0 or view[3]<=0:raise RenderError("Mermaid SVG requires a positive viewBox")
        self.vx,self.vy,self.vw,self.vh=view;self.width=min(width,150*mm);self.height=self.width*self.vh/self.vw
        if self.height>180*mm:
            scale=(180*mm)/self.height;self.width*=scale;self.height*=scale;self.hAlign="CENTER"
        self.shapes=[]
        colour=lambda value:None if value=="none" else colors.HexColor(value) if HEX.fullmatch(value or "") else (_ for _ in ()).throw(RenderError("Mermaid SVG colour is outside the closed palette"))
        for element in elements[1:]:
            kind=local(element.tag);a=element.attrib
            if kind=="path":self.shapes.append((kind,self._path(a["d"]),colour(a.get("fill","none")),colour(a.get("stroke","none")),self._number(a.get("stroke-width","1"),"stroke width")))
            elif kind=="rect":
                values=[self._number(a.get(key,"0"),key) for key in ("x","y","width","height","rx","ry")]
                if values[2]<=0 or values[3]<=0:raise RenderError("Mermaid SVG rectangle has non-positive geometry")
                self.shapes.append((kind,*values,colour(a.get("fill","none")),colour(a.get("stroke","none")),self._number(a.get("stroke-width","1"),"stroke width")))
            else:
                literal="".join(element.itertext()).strip()
                if not literal or len(literal)>1024 or any(ord(char)<32 and char not in "\t\n\r" for char in literal):raise RenderError("Mermaid SVG text is empty or outside limits")
                self.shapes.append((kind,self._number(a["x"],"text x"),self._number(a["y"],"text y"),self._number(a["font-size"],"font size"),literal,colour(a["fill"])))
        if not any(shape[0] in {"path","rect"} for shape in self.shapes) or not any(shape[0]=="text" for shape in self.shapes):raise RenderError("Mermaid SVG omits visible geometry or labels")

    def _point(self,x:float,y:float)->tuple[float,float]:
        return ((x-self.vx)*self.width/self.vw,self.height-(y-self.vy)*self.height/self.vh)

    def draw(self)->None:
        self.canv.bookmarkHorizontal(self.destination,0,self.height);scale=min(self.width/self.vw,self.height/self.vh)
        for shape in self.shapes:
            kind=shape[0]
            if kind=="path":
                _,commands,fill,stroke,line_width=shape;p=self.canv.beginPath()
                for command in commands:
                    if command[0]=="M":p.moveTo(*self._point(command[1],command[2]))
                    elif command[0]=="L":p.lineTo(*self._point(command[1],command[2]))
                    elif command[0]=="C":p.curveTo(*(coordinate for index in (1,3,5) for coordinate in self._point(command[index],command[index+1])))
                    else:p.close()
                self.canv.setFillColor(fill or colors.white);self.canv.setStrokeColor(stroke or colors.white);self.canv.setLineWidth(line_width*scale);self.canv.drawPath(p,fill=int(fill is not None),stroke=int(stroke is not None))
            elif kind=="rect":
                _,x,y,w,h,rx,ry,fill,stroke,line_width=shape;px,py=self._point(x,y+h);self.canv.setFillColor(fill or colors.white);self.canv.setStrokeColor(stroke or colors.white);self.canv.setLineWidth(line_width*scale)
                self.canv.roundRect(px,py,w*self.width/self.vw,h*self.height/self.vh,min(rx*self.width/self.vw,ry*self.height/self.vh),fill=int(fill is not None),stroke=int(stroke is not None))
            else:
                _,x,y,size,literal,fill=shape;px,py=self._point(x,y);self.canv.setFillColor(fill or colors.black);self.canv.setFont(self.font_name,max(6.,size*scale));self.canv.drawCentredString(px,py,literal)


def _pdf_destination_plan(ast: dict[str, Any]) -> dict[str, str]:
    """Close the renderer-owned destination domain before ReportLab builds."""
    _figure_registry(ast)
    definitions:list[tuple[str,str]]=[];links:list[str]=[]
    def collect_inline(tokens:list[dict[str,Any]])->None:
        links.extend(token["target"][1:] for token in tokens if token.get("type")=="link" and token.get("target","").startswith("#"))
        links.extend(token["target"] for token in tokens if token.get("type")=="figure-ref")
    for node in ast["children"]:
        kind=node["type"]
        if kind in {"heading","mermaid","graphviz"} and node.get("id"):definitions.append((node["id"],kind))
        if kind=="paragraph":collect_inline(node.get("inline",[]))
        elif kind in {"ordered-list","unordered-list"}:
            for item in node.get("items",[]):collect_inline(item.get("inline",[]))
    result:dict[str,str]={}
    for destination,kind in definitions:
        if destination in result:raise RenderError(f"duplicate PDF destination: {destination}")
        result[destination]=kind
    for destination in links:
        if destination not in result:raise RenderError(f"local PDF link targets missing or foreign destination: {destination}")
    return result


def _expected_internal_links(ast:dict[str,Any],toc:bool)->list[dict[str,str]]:
    """Describe every renderer-owned link by its source and target IDs."""
    result=[];position=0
    for node in ast["children"]:
        if node["type"]=="blank":continue
        position+=1;source=node.get("id") or node.get("attrs",{}).get("id") or f"node-{position}"
        groups=[]
        if node["type"]=="paragraph":groups=[node.get("inline",[])]
        elif node["type"] in {"ordered-list","unordered-list"}:groups=[item.get("inline",[]) for item in node.get("items",[])]
        for tokens in groups:
            result.extend({"source_id":source,"target_id":token["target"][1:]} for token in tokens if token.get("type")=="link" and token.get("target","").startswith("#"))
            result.extend({"source_id":source,"target_id":token["target"]} for token in tokens if token.get("type")=="figure-ref")
    if toc:
        result.extend({"source_id":f'toc-{node["id"]}',"target_id":node["id"]} for node in ast["children"] if node["type"]=="heading" and node["level"]==2)
    return result


def _figure_registry(ast:dict[str,Any])->list[dict[str,Any]]:
    try:return contract.validate_figure_registry(ast)
    except contract.ContractError as exc:raise RenderError(str(exc)) from exc


def _figure_references(ast:dict[str,Any])->list[dict[str,Any]]:
    """Expose semantic references separately from ordinary anchor links."""
    result=[];position=0
    for node in ast["children"]:
        if node["type"]=="blank":continue
        position+=1;source=node.get("id") or node.get("attrs",{}).get("id") or f"node-{position}"
        groups=[node.get("inline",[])] if node["type"]=="paragraph" else [item.get("inline",[]) for item in node.get("items",[])] if node["type"] in {"ordered-list","unordered-list"} else []
        for tokens in groups:
            result.extend({"source_id":source,"target_id":token["target"],"number":token["figure_number"],"reference_text":token["reference_text"]} for token in tokens if token.get("type")=="figure-ref")
    return result


def _deterministic_canvas(*args: Any, **kwargs: Any) -> canvas.Canvas:
    """Write pages immediately so ReportLab destinations bind real page refs."""
    kwargs.update(invariant=1, pageCompression=0, pdfVersion=(1,4))
    return canvas.Canvas(*args, **kwargs)


class InstrumentedDocTemplate(BaseDocTemplate):
    """Record renderer object geometry before PDF text extraction can lose it."""
    def __init__(self,*args:Any,source_records:list[dict[str,Any]],**kwargs:Any):
        self.source_records=source_records;super().__init__(*args,**kwargs)
    def afterFlowable(self,flowable:Flowable)->None:
        source_id=getattr(flowable,"_brandkit_source_id",None)
        if source_id and getattr(self,"frame",None) is not None:
            x=float(self.frame._x);height=max(1.0,float(getattr(flowable,"height",getattr(flowable,"_height",1.0)) or 1.0))
            # Frame._y is already below spaceAfter when afterFlowable runs.
            # The painted flowable starts above that gap; binding the gap as
            # ink shifts every claimed box down by exactly spaceAfter.
            space_after=float(flowable.getSpaceAfter()) if hasattr(flowable,"getSpaceAfter") else 0.0
            bottom=float(self.frame._y)+space_after;width=min(float(getattr(flowable,"width",getattr(flowable,"_width",self.frame._aW)) or self.frame._aW),float(self.frame._aW))
            alignment=str(getattr(flowable,"hAlign","LEFT")).upper()
            if alignment=="CENTER":x+=(float(self.frame._aW)-width)/2
            elif alignment=="RIGHT":x+=float(self.frame._aW)-width
            if isinstance(flowable,Callout):
                x+=8*mm;width=max(1.,width-14*mm);bottom+=3*mm;height=float(flowable.p.height)
            record={"source_id":source_id,"type":getattr(flowable,"_brandkit_source_type","content"),"page":self.page,"bbox_pdf_pt":[x,bottom,x+width,bottom+height],"visible":True}
            if getattr(flowable,"_brandkit_evidence_text",None):record.update(evidence_kind="text",evidence_text=flowable._brandkit_evidence_text)
            self.source_records.append(record)


def make_styles(brand: dict[str, Any]) -> dict[str, ParagraphStyle]:
    t=brand["pdf"]["tokens"]; cr=t["color_roles"]; fonts=t["typography_roles"]
    body=brand["_renderer_fonts"][fonts["body"]]; heading=brand["_renderer_fonts"][fonts["heading"]]; text=_hex(cr["text"],"text");primary=_hex(cr["primary"],"primary")
    spacing=[float(x)*mm for x in t["spacing_scale"]];small,medium,large=spacing[0],spacing[min(1,len(spacing)-1)],spacing[-1]
    return {
      "body":ParagraphStyle("Body",fontName=body,fontSize=10.2,leading=15,textColor=text,spaceAfter=large),
      "h1":ParagraphStyle("H1",fontName=heading,fontSize=28,leading=32,textColor=primary,spaceAfter=2*large),
      "h2":ParagraphStyle("H2",fontName=heading,fontSize=18,leading=22,textColor=primary,spaceBefore=large+medium,spaceAfter=large,keepWithNext=True),
      "h3":ParagraphStyle("H3",fontName=heading,fontSize=13,leading=17,textColor=primary,spaceBefore=large+small,spaceAfter=medium,keepWithNext=True),
      "small":ParagraphStyle("Small",fontName=body,fontSize=8,leading=11,textColor=_hex(cr["muted"],"muted")),
      "title":ParagraphStyle("Title",fontName=heading,fontSize=34,leading=39,textColor=colors.white,alignment=TA_LEFT),
      "title-light":ParagraphStyle("TitleLight",fontName=heading,fontSize=34,leading=39,textColor=primary,alignment=TA_LEFT),
    }


def layout_strategy(dtype: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Map each document family to one closed, visibly distinct strategy."""
    strategies={
      "report":{"id":"report-evidence","cover":"dark","section_numbers":True,"toc":True,"hero_mm":0},
      "offer":{"id":"offer-proposal","cover":"light-band","section_numbers":False,"toc":False,"hero_mm":0},
      "freeform":{"id":"freeform-editorial","cover":"editorial","section_numbers":False,"toc":False,"hero_mm":0},
      "one-pager":{"id":"one-pager-hero","cover":"hero","section_numbers":False,"toc":False,"hero_mm":48},
    }
    family=dtype.get("extends") or dtype["id"]
    if family not in strategies:raise RenderError(f"no layout strategy for document type family: {family}")
    policy=dict(strategies[family]);policy["toc"]=bool(policy["toc"] and dtype["layout"]["toc"])
    overrides=overrides or {}
    if "toc" in overrides:policy["toc"]=bool(overrides["toc"])
    if "section_numbering" in overrides:policy["section_numbers"]=bool(overrides["section_numbering"])
    return policy


def callout_text(node: dict[str, Any], ast: dict[str, Any]) -> tuple[str, str]:
    """Return every visible semantic field carried by a v2 callout."""
    attrs=node["attrs"]
    body=" ".join(inline_text(x.get("inline",[])) if x["type"]=="paragraph" else html.escape(x.get("text","").strip()) for x in node["children"])
    details=[]
    if node["kind"] in {"note","tip","warning","caution","important"}:
        # Matrix IDs are visible evidence, not renderer-only metadata.  This
        # lets PDF extraction, HTML and the source map prove the same exact
        # 15-case domain without relying on callout titles being unique.
        details.append(f'ID: {html.escape(attrs["id"])}')
    if node["kind"]=="metric":
        fact=next((x for x in ast["frontmatter"]["facts"] if x["id"]==attrs["value_fact"]),None)
        if fact is None:raise RenderError("metric value_fact is absent after contract validation")
        details.extend([f'<b>{html.escape(attrs["label"])}</b>: {html.escape(str(fact["value"]))}',f'Fakt: {html.escape(attrs["value_fact"])}',f'Quelle: {html.escape(attrs["source_id"])}'])
    elif node["kind"] in {"quote","legal"}:
        details.append(f'Quelle: {html.escape(attrs["source_id"])}')
    return attrs["title"],"<br/>".join([*details,body] if details else [body])


def _list(node: dict[str, Any], styles: dict[str, ParagraphStyle], ordered: bool) -> ListFlowable:
    items=[]
    for item in node["items"]:
        parts=[Paragraph(inline_text(item["inline"]),styles["body"])]
        for child in item.get("children",[]): parts.append(_list(child,styles,child["type"]=="ordered-list"))
        items.append(ListItem(parts,leftIndent=4*mm))
    return ListFlowable(items,bulletType="1" if ordered else "bullet",leftIndent=6*mm,bulletFontName=styles["body"].fontName,bulletFontSize=9,bulletColor=styles["body"].textColor,spaceAfter=3*mm)


def _asset_path(ast: dict[str, Any], asset_root: Path, asset_id: str) -> Path:
    item=next((x for x in ast["frontmatter"]["assets"] if x["id"]==asset_id),None)
    if not item:raise RenderError(f"image asset not declared: {asset_id}")
    path=(asset_root/item["path"]).resolve(strict=True)
    if asset_root.resolve() not in path.parents or sha(path.read_bytes())!=item["sha256"]:raise RenderError("image asset path/hash failed")
    return path


def _contrast(foreground:str,background:str)->float:
    def luminance(value:str)->float:
        channels=[int(value[index:index+2],16)/255 for index in (1,3,5)]
        converted=[channel/12.92 if channel<=.04045 else ((channel+.055)/1.055)**2.4 for channel in channels]
        return .2126*converted[0]+.7152*converted[1]+.0722*converted[2]
    first,second=sorted((luminance(foreground),luminance(background)),reverse=True)
    return round((first+.05)/(second+.05),3)


def _diagram_brand_projection(brand:dict[str,Any])->dict[str,Any]:
    tokens=brand["pdf"]["tokens"];roles=tokens["color_roles"];font_id=tokens["typography_roles"]["body"]
    asset=next((item for item in brand["assets"]["fonts"] if item["id"]==font_id),None)
    lock=load_json(ROOT/"toolchain/pdf-renderer.production.v1.lock.json")
    derivative=lock.get("font_derivatives",{}).get(asset["sha256"] if asset else "")
    if not asset or not derivative:raise RenderError("diagram typography role has no pinned font derivative")
    font_path=ROOT/derivative["path"]
    if font_path.is_symlink() or not font_path.is_file() or sha(font_path.read_bytes())!=derivative["sha256"]:
        raise RenderError("diagram font derivative differs from the production lock")
    family="BrandKit-"+font_id
    contrasts={
      "text_on_background":_contrast(roles["text"],roles["background"]),
      "text_on_surface":_contrast(roles["text"],roles["surface"]),
      "primary_on_background":_contrast(roles["primary"],roles["background"]),
    }
    if min(contrasts.values())<4.5:raise RenderError("diagram brand role contrast is below 4.5:1")
    return {"font":{"asset_id":font_id,"family":family,"asset_sha256":asset["sha256"],"derivative_sha256":derivative["sha256"]},
            "colors":{key:roles[key] for key in ("primary","accent","text","muted","background","surface")},
            "line_role":"accent","contrast":contrasts,"font_embedded":True,"forbidden_fallbacks_absent":True}


def _embedded_font_css(projection:dict[str,Any])->str:
    lock=load_json(ROOT/"toolchain/pdf-renderer.production.v1.lock.json")
    derivative=next((item for item in lock["font_derivatives"].values() if item["sha256"]==projection["font"]["derivative_sha256"]),None)
    if derivative is None:raise RenderError("diagram font derivative is absent from the production lock")
    font_path=ROOT/derivative["path"]
    encoded=base64.b64encode(font_path.read_bytes()).decode("ascii")
    variables=";".join(f'--brand-{key}:{value}' for key,value in projection["colors"].items())
    return (f'@font-face{{font-family:{projection["font"]["family"]};src:url(data:font/ttf;base64,{encoded}) format("truetype");}}'
            f':root{{{variables}}}*{{font-family:{projection["font"]["family"]};}}')


def _mermaid_config(brand:dict[str,Any])->tuple[dict[str,Any],dict[str,Any]]:
    projection=_diagram_brand_projection(brand);roles=projection["colors"];family=projection["font"]["family"]
    config={"deterministicIds":True,"deterministicIDSeed":"brandkit-mermaid-svg-v1","securityLevel":"strict","theme":"base",
            "fontFamily":family,"themeCSS":_embedded_font_css(projection),"flowchart":{"htmlLabels":False,"useMaxWidth":False},
            "themeVariables":{"fontFamily":family,"fontSize":"16px","background":roles["background"],
              "primaryColor":roles["surface"],"primaryTextColor":roles["text"],"primaryBorderColor":roles["accent"],
              "secondaryColor":roles["background"],"secondaryTextColor":roles["text"],"secondaryBorderColor":roles["accent"],
              "tertiaryColor":roles["surface"],"tertiaryTextColor":roles["text"],"tertiaryBorderColor":roles["accent"],
              "lineColor":roles["accent"],"textColor":roles["text"],"mainBkg":roles["surface"],
              "nodeBorder":roles["accent"],"clusterBkg":roles["surface"],"clusterBorder":roles["accent"],
              "edgeLabelBackground":roles["background"],"noteBkgColor":roles["surface"],"noteTextColor":roles["text"]}}
    return config,projection


def _audit_diagram_svg(svg:Path,projection:dict[str,Any])->dict[str,Any]:
    text=svg.read_text(encoding="utf-8");lower=text.lower();family=projection["font"]["family"]
    forbidden_fonts=("arial","times","system-ui","sans-serif","serif")
    forbidden_colors=("#000","#000000","#333","#333333","#666","#666666","#999","#999999","#eee","#eeeeee")
    if family not in text or any(re.search(rf'font-family[^;>]*\\b{re.escape(value)}\\b',lower) for value in forbidden_fonts):
        raise RenderError("diagram SVG typography is not closed over the active brand font")
    if any(value in lower for value in forbidden_colors):raise RenderError("diagram SVG retains a forbidden default color")
    missing=[value for value in projection["colors"].values() if value.lower() not in lower]
    if missing:raise RenderError(f"diagram SVG omits active brand colors: {missing}")
    return {**projection,"svg_sha256":sha(svg.read_bytes())}


def _close_mermaid_svg(raw_svg:Path,svg:Path,projection:dict[str,Any])->None:
    """Lower pinned mmdc output into the complete CSS-free publication subset."""
    raw=raw_svg.read_bytes()
    if len(raw)>MermaidSvgFlowable.MAX_BYTES or b"<!doctype" in raw.lower() or b"<!entity" in raw.lower():raise RenderError("Mermaid worker SVG exceeds XML safety limits")
    try:root=ET.fromstring(raw)
    except ET.ParseError as exc:raise RenderError("Mermaid worker SVG is not parseable") from exc
    local=lambda value:value.rsplit("}",1)[-1];view=root.attrib.get("viewBox","").split()
    if len(view)!=4:raise RenderError("Mermaid worker SVG lacks the pinned viewBox")
    numbers=[MermaidSvgFlowable._number(value,"worker viewBox") for value in view]
    roles=projection["colors"]
    published=ET.Element("svg",{"width":str(numbers[2]),"height":str(numbers[3]),"viewBox":" ".join(view),**{f"data-brand-{key}":value for key,value in roles.items()}})
    ET.SubElement(published,"rect",{"x":view[0],"y":view[1],"width":view[2],"height":view[3],"fill":roles["background"],"stroke":"none","stroke-width":"0"})
    edges=[]
    for element in root.iter():
        if local(element.tag)=="path" and "flowchart-link" in element.attrib.get("class",""):
            commands=MermaidSvgFlowable._path(element.attrib.get("d",""));edges.append(commands)
            ET.SubElement(published,"path",{"d":element.attrib["d"],"fill":"none","stroke":roles["accent"],"stroke-width":"1.5"})
            end=next((command for command in reversed(commands) if command[0] in {"M","L","C"}),None)
            if end:
                x,y=(end[-2],end[-1]);arrow=f"M{x-4},{y-7} L{x},{y} L{x+4},{y-7} Z"
                ET.SubElement(published,"path",{"d":arrow,"fill":roles["accent"],"stroke":roles["accent"],"stroke-width":"1"})
    node_count=0
    for group in root.iter():
        if local(group.tag)!="g" or "node" not in group.attrib.get("class","").split():continue
        match=re.fullmatch(r"translate\(\s*(%s)\s*,\s*(%s)\s*\)"%(MermaidSvgFlowable.NUMBER,MermaidSvgFlowable.NUMBER),group.attrib.get("transform",""))
        box=next((child for child in group if local(child.tag)=="rect" and "label-container" in child.attrib.get("class","").split()),None)
        label=next((" ".join("".join(child.itertext()).split()) for child in group.iter() if local(child.tag) in {"foreignObject","text"} and " ".join("".join(child.itertext()).split())),None)
        if match is None or box is None or not label:raise RenderError("Mermaid worker node differs from the closed flowchart geometry")
        tx,ty=map(float,match.groups());x=tx+MermaidSvgFlowable._number(box.attrib.get("x",""),"node x");y=ty+MermaidSvgFlowable._number(box.attrib.get("y",""),"node y")
        width=MermaidSvgFlowable._number(box.attrib.get("width",""),"node width");height=MermaidSvgFlowable._number(box.attrib.get("height",""),"node height")
        ET.SubElement(published,"rect",{"x":str(x),"y":str(y),"width":str(width),"height":str(height),"rx":"4","ry":"4","fill":roles["surface"],"stroke":roles["accent"],"stroke-width":"1.5"})
        text=ET.SubElement(published,"text",{"x":str(tx),"y":str(ty+5.5),"fill":roles["text"],"font-family":projection["font"]["family"],"font-size":"16","text-anchor":"middle"});text.text=label;node_count+=1
    if not edges or not node_count:raise RenderError("Mermaid worker SVG omits flowchart edges or nodes")
    svg.write_bytes(ET.tostring(published,encoding="utf-8",xml_declaration=False))
    MermaidSvgFlowable(svg,150*mm,"mermaid-validation",projection["font"]["family"])


def _mermaid_svg(node: dict[str, Any], work: Path, runtime_argument: Path, brand:dict[str,Any]) -> tuple[Path, Path, dict[str, Any]]:
    contract.validate_mermaid_v1(node["code"])
    lock_path = ROOT / "toolchain/mermaid-svg.v1.lock.json"; lock = load_json(lock_path)
    if sha(lock_path.read_bytes()) != sha((ROOT / "toolchain/mermaid-svg.v1.lock.json").read_bytes()):
        raise RenderError("Mermaid lock changed while rendering")
    runtime_pin = lock.get("runtime_image")
    if not isinstance(runtime_pin, dict):
        raise RenderError("pinned Mermaid runtime image is absent")
    runtime = Path(runtime_pin["path"])
    if runtime_argument != runtime:
        raise RenderError("Mermaid runtime argument differs from the pinned image")
    # Both the normative SVG and the PDF raster are emitted by mmdc inside the
    # sealed, networkless Linux worker.  No host browser evaluates the source.
    diagram_work = work / f'{node["id"]}-linux-worker'
    source = diagram_work / "input.mmd";config_path=diagram_work/"brand-config.json";raw_svg=diagram_work/"worker-output.svg";svg=diagram_work/"output.svg";png=diagram_work/"output.png"
    config,projection=_mermaid_config(brand);config_bytes=canonical_json(config)+b"\n"
    worker_evidence_path = diagram_work / "worker-evidence.json"
    if diagram_work.exists() or diagram_work.is_symlink():
        if (diagram_work.is_symlink() or not diagram_work.is_dir()
                or any(path.is_symlink() or not path.is_file()
                       for path in (source,config_path,raw_svg,svg,png,worker_evidence_path))
                or source.read_text(encoding="utf-8") != node["code"] or config_path.read_bytes()!=config_bytes):
            raise RenderError("cached Mermaid worker outputs differ")
        worker_evidence = load_json(worker_evidence_path)
    else:
        diagram_work.mkdir();source.write_text(node["code"],encoding="utf-8");config_path.write_bytes(config_bytes)
        worker_tool = ROOT / "toolchain/mermaid-svg/linux_worker.py"
        worker_run = subprocess.run(
            ["/opt/homebrew/bin/python3.12",str(worker_tool),"render-pair",str(source),str(diagram_work),"--config",str(config_path)],
            check=True, text=True, capture_output=True,
        )
        try:
            worker_evidence = json.loads(worker_run.stdout)
        except json.JSONDecodeError as exc:
            raise RenderError("Mermaid Linux worker returned invalid evidence") from exc
        svg.rename(raw_svg);_close_mermaid_svg(raw_svg,svg,projection)
        worker_evidence_path.write_text(json.dumps(worker_evidence,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
    if (worker_evidence.get("network") != "none"
            or worker_evidence.get("sandbox_disable_flags") != []
            or worker_evidence.get("config_sha256") != sha(config_path.read_bytes())
            or worker_evidence.get("svg_sha256") != sha(raw_svg.read_bytes())
            or worker_evidence.get("png_sha256") != sha(png.read_bytes())):
        raise RenderError("Mermaid Linux worker evidence differs from output")
    prefix=work.name
    brand_audit=_audit_diagram_svg(svg,projection)
    evidence = {"id":node["id"],"source_path":f"{prefix}/{diagram_work.name}/{source.name}","source_sha256":sha(source.read_bytes()),"config_path":f"{prefix}/{diagram_work.name}/{config_path.name}","config_sha256":sha(config_path.read_bytes()),"worker_svg_path":f"{prefix}/{diagram_work.name}/{raw_svg.name}","worker_svg_sha256":sha(raw_svg.read_bytes()),"svg_path":f"{prefix}/{diagram_work.name}/{svg.name}","svg_sha256":sha(svg.read_bytes()),"raster_path":f"{prefix}/{diagram_work.name}/{png.name}","raster_sha256":sha(png.read_bytes()),"toolchain_lock_sha256":sha(lock_path.read_bytes()),"brand_projection":brand_audit,"linux_worker":worker_evidence}
    return svg, png, evidence


def _apply_graphviz_brand_palette(svg:Path,brand:dict[str,Any])->dict[str,Any]:
    """Project Graphviz's closed output onto active brand colors and font."""
    projection=_diagram_brand_projection(brand);roles=projection["colors"]
    replacements={"black":roles["accent"],"#000000":roles["accent"],"white":roles["surface"],"#ffffff":roles["surface"]}
    root=ET.fromstring(svg.read_bytes())
    changed=False
    for element in root.iter():
        kind=element.tag.rsplit("}",1)[-1]
        if kind=="text":
            element.set("fill",roles["text"]);element.set("font-family",projection["font"]["family"]);changed=True
        for attribute in ("fill","stroke"):
            value=element.attrib.get(attribute)
            replacement=replacements.get(value.lower()) if isinstance(value,str) else None
            if replacement is not None:
                element.set(attribute,replacement);changed=True
    if not changed:raise RenderError("Graphviz SVG exposes no closed default palette to bind")
    for key,value in roles.items():root.set(f"data-brand-{key}",value)
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    svg.write_bytes(ET.tostring(root,encoding="utf-8",xml_declaration=False))
    return _audit_diagram_svg(svg,projection)


def _graphviz_svg(node:dict[str,Any],work:Path,brand:dict[str,Any])->tuple[Path,dict[str,Any]]:
    contract.validate_graphviz_v1(node["code"])
    lock_path=ROOT/"toolchain/graphviz-svg.v1.lock.json";lock=load_json(lock_path)
    contract.validate_schema(lock,"graphviz-svg-toolchain.v1.schema.json","Graphviz SVG toolchain lock")
    production=load_json(ROOT/"toolchain/pdf-renderer.production.v1.lock.json")
    if production["capabilities"]["graphviz"]["status"]!="installed":raise RenderError("Graphviz capability is not installed")
    cas=Path(lock["artifact_bundle"]["path"]);install=work/"graphviz-install"
    env={**os.environ,"PYTHON_BIN":sys.executable}
    if not install.exists():subprocess.run([str(ROOT/"toolchain/graphviz-svg/install-offline"),str(install),str(cas)],check=True,env=env)
    source=work/f'{node["id"]}.dot';svg=work/f'{node["id"]}.svg';source.write_text(node["code"],encoding="utf-8")
    subprocess.run([str(ROOT/"toolchain/graphviz-svg/render-offline"),str(install),str(source),str(svg)],check=True,env=env)
    brand_audit=_apply_graphviz_brand_palette(svg,brand)
    if svg.is_symlink() or not svg.is_file() or not svg.read_bytes().startswith(b"<svg "):raise RenderError("Graphviz renderer did not produce a regular SVG")
    prefix=work.name;evidence={"id":node["id"],"source_path":f"{prefix}/{source.name}","source_sha256":sha(source.read_bytes()),"svg_path":f"{prefix}/{svg.name}","svg_sha256":sha(svg.read_bytes()),"toolchain_lock_sha256":sha(lock_path.read_bytes()),"brand_projection":brand_audit}
    return svg,evidence


def _figure_caption(node:dict[str,Any])->str:
    number=node.get("figure_number")
    if not isinstance(number,int) or number<1:raise RenderError("figure caption lacks an automatic figure number")
    value=f'Abbildung {number}: {node["caption"]} — {node["description"]}'
    if node.get("source_id"):value+=f' — Quelle: {node["source_id"]}'
    return value


def story(ast: dict[str, Any], brand: dict[str, Any], dtype: dict[str, Any], asset_root: Path, work: Path, mermaid_cas: Path, policy: dict[str, Any] | None = None) -> tuple[list[Flowable], dict[str,list[dict[str,Any]]]]:
    _figure_registry(ast)
    _pdf_destination_plan(ast)
    styles=make_styles(brand);t=brand["pdf"]["tokens"];cr=t["color_roles"];accent=_hex(cr["accent"],"accent");surface=_hex(cr["surface"],"surface");text=_hex(cr["text"],"text")
    width=A4[0]-2*float(t["page"]["margin_mm"])*mm; result=[]; diagrams={"mermaid":[],"graphviz":[]}
    content_position=0;policy=policy or layout_strategy(dtype);section_number=0
    for node in ast["children"]:
        if node["type"]!="blank":content_position+=1
        start=len(result);source_id=node.get("id") or node.get("attrs",{}).get("id") or f"node-{content_position}"
        kind=node["type"]
        if kind=="blank":continue
        if kind=="heading":
            label=node["text"]
            if node["level"]==2 and policy["section_numbers"]:
                section_number+=1;label=f'Kapitel {section_number:02d}  {label}'
            elif node["level"]==2:label=f'Abschnitt: {label}'
            result.append(DestinationParagraph(label,styles[f'h{node["level"]}'],node["id"]))
        elif kind=="paragraph":result.append(Paragraph(inline_text(node["inline"]),styles["body"]))
        elif kind in {"ordered-list","unordered-list"}:result.append(_list(node,styles,kind=="ordered-list"))
        elif kind=="table":
            data=[[Paragraph(str(x),styles["small"]) for x in node["headers"]]]+[[Paragraph(re.sub(r"\{\{(?:fact|number):[^|]+\|([^}]+)\}\}",r"\1",str(x)),styles["small"]) for x in row] for row in node["rows"]]
            tbl=Table(data,colWidths=[width/len(data[0])]*len(data[0]),repeatRows=1,hAlign="LEFT")
            tbl.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),surface),("TEXTCOLOR",(0,0),(-1,0),text),("GRID",(0,0),(-1,-1),.35,colors.HexColor("#B9BEC5")),("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),3*mm),("RIGHTPADDING",(0,0),(-1,-1),3*mm),("TOPPADDING",(0,0),(-1,-1),2*mm),("BOTTOMPADDING",(0,0),(-1,-1),2*mm)]));result.extend([tbl,Spacer(1,5*mm)])
        elif kind=="callout":
            title,body=callout_text(node,ast)
            result.extend([Callout(title,body,width,accent,surface,styles["body"],node["attrs"].get("appearance","default"),node["attrs"].get("icon","true")=="true"),Spacer(1,2*mm)])
        elif kind=="image":
            path=_asset_path(ast,asset_root,node["asset"])
            if path.suffix.lower()==".svg":
                result.append(SafeSvgFlowable(path,width,styles["body"].fontName))
            elif path.suffix.lower() not in {".png",".jpg",".jpeg"}:
                raise RenderError("unsupported image extension")
            else:
                with PILImage.open(path) as im:w,h=im.size
                iw=min(width,120*mm);ih=iw*h/w;result.append(Image(str(path),width=iw,height=ih))
            result.extend([Paragraph(node["description"],styles["small"]),Spacer(1,4*mm)])
        elif kind=="mermaid":
            svg,_png,evidence=_mermaid_svg(node,work,mermaid_cas,brand);diagrams["mermaid"].append(evidence)
            diagram=MermaidSvgFlowable(svg,width,source_id,styles["body"].fontName);caption=Paragraph(_figure_caption(node),styles["small"]);caption._brandkit_source_id=source_id+":caption";caption._brandkit_source_type="figure-caption";caption._brandkit_evidence_text=_figure_caption(node)
            result.extend([diagram,caption,Spacer(1,4*mm)])
        elif kind=="graphviz":
            svg,evidence=_graphviz_svg(node,work,brand);diagrams["graphviz"].append(evidence)
            diagram=GraphvizSvgFlowable(svg,width,source_id,styles["body"].fontName);caption=Paragraph(_figure_caption(node),styles["small"]);caption._brandkit_source_id=source_id+":caption";caption._brandkit_source_type="figure-caption";caption._brandkit_evidence_text=_figure_caption(node)
            result.extend([diagram,caption,Spacer(1,4*mm)])
        elif kind=="page-break":result.append(PageBreak())
        elif kind=="footnote":result.append(Paragraph(f'[{node["source_id"]}] {node["text"]}',styles["small"]))
        else:raise RenderError(f"unsupported AST block: {kind}")
        for flowable in result[start:]:
            if not isinstance(flowable,(Spacer,PageBreak)) and not hasattr(flowable,"_brandkit_source_id"):
                flowable._brandkit_source_id=source_id;flowable._brandkit_source_type=kind
    return result,diagrams


def render_html(ast: dict[str, Any], brand: dict[str, Any], dtype: dict[str, Any], asset_root: Path, output: Path,
                overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Render the same closed AST to standalone, marker-rich HTML."""
    _figure_registry(ast)
    policy=layout_strategy(dtype,overrides);tokens=brand["pdf"]["tokens"];colorset=tokens["color_roles"]
    def inline(tokens_value:list[dict[str,Any]])->str:
        parts=[]
        for token in tokens_value:
            kind=token["type"]
            if kind=="text":parts.append(html.escape(token["text"]))
            elif kind=="protected":parts.append(f'<span data-protected="{html.escape(token["kind"])}">{html.escape(token["literal"])}</span>')
            elif kind=="link":parts.append(f'<a href="{html.escape(token["target"],quote=True)}">{html.escape(token["label"])}</a>')
            elif kind=="figure-ref":parts.append(f'<a href="#{html.escape(token["target"],quote=True)}" data-figure-ref="{html.escape(token["target"],quote=True)}" data-figure-number="{token["figure_number"]}">{html.escape(token["reference_text"])}</a>')
            elif kind=="footnote-ref":parts.append(f'<sup><a href="#fn-{html.escape(token["source_id"])}">[{html.escape(token["source_id"])}]</a></sup>')
            else:raise RenderError(f"unsupported HTML inline token: {kind}")
        return "".join(parts)
    def attrs(node:dict[str,Any])->str:
        source=node.get("id") or node.get("attrs",{}).get("id") or node.get("source_id") or "block"
        return f'data-brandkit-id="{html.escape(source)}"'
    body=[];section_number=0
    headings=[n for n in ast["children"] if n["type"]=="heading" and n["level"]==2]
    if policy["toc"]:
        body.append('<nav data-brandkit-role="toc"><h2>Inhalt</h2><ol>'+"".join(f'<li><a href="#{html.escape(n["id"])}">{i:02d} {html.escape(n["text"])}</a></li>' for i,n in enumerate(headings,1))+'</ol></nav>')
    for node in ast["children"]:
        kind=node["type"]
        if kind=="blank":continue
        if kind=="heading":
            if node["level"]==1:continue
            text_value=node["text"]
            if node["level"]==2 and policy["section_numbers"]:section_number+=1;text_value=f"Kapitel {section_number:02d}  {text_value}"
            elif node["level"]==2:text_value=f"Abschnitt: {text_value}"
            body.append(f'<h{node["level"]} id="{html.escape(node["id"])}" {attrs(node)}>{html.escape(text_value)}</h{node["level"]}>')
        elif kind=="paragraph":body.append(f'<p {attrs(node)}>{inline(node["inline"])}</p>')
        elif kind in {"ordered-list","unordered-list"}:
            tag="ol" if kind=="ordered-list" else "ul";items="".join(f'<li>{inline(item["inline"])}</li>' for item in node["items"]);body.append(f'<{tag} {attrs(node)}>{items}</{tag}>')
        elif kind=="table":
            head="".join(f"<th>{html.escape(str(x))}</th>" for x in node["headers"]);rows="".join("<tr>"+"".join(f"<td>{html.escape(str(x))}</td>" for x in row)+"</tr>" for row in node["rows"]);body.append(f'<table {attrs(node)}><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>')
        elif kind=="callout":
            title,text_value=callout_text(node,ast);a=node["attrs"];icon=a.get("icon","true");appearance=a.get("appearance","default")
            semantic="".join(f' data-{key.replace("_","-")}="{html.escape(a[key])}"' for key in ("label","value_fact","source_id") if key in a)
            body.append(f'<aside {attrs(node)} data-callout-type="{html.escape(node["kind"])}" data-appearance="{html.escape(appearance)}" data-icon="{html.escape(icon)}"{semantic}><h3>{html.escape(title)}</h3><div>{text_value}</div></aside>')
        elif kind=="image":
            path=_asset_path(ast,asset_root,node["asset"]);mime=next(x["media_type"] for x in ast["frontmatter"]["assets"] if x["id"]==node["asset"]);encoded=base64.b64encode(path.read_bytes()).decode("ascii")
            body.append(f'<figure {attrs(node)}><img src="data:{mime};base64,{encoded}" alt="{html.escape(node["alt"])}"><figcaption>{html.escape(node["description"])}</figcaption></figure>')
        elif kind=="mermaid":body.append(f'<figure id="{html.escape(node["id"])}" {attrs(node)} data-diagram="mermaid" data-figure-number="{node["figure_number"]}"><pre>{html.escape(node["code"])}</pre><figcaption>{html.escape(_figure_caption(node))}</figcaption></figure>')
        elif kind=="graphviz":
            svg_path=output.parent/"renderer-work"/f'{node["id"]}.svg'
            if svg_path.is_symlink() or not svg_path.is_file():raise RenderError("Graphviz HTML requires the exact renderer SVG")
            encoded=base64.b64encode(svg_path.read_bytes()).decode("ascii")
            body.append(f'<figure id="{html.escape(node["id"])}" {attrs(node)} data-diagram="graphviz" data-figure-number="{node["figure_number"]}"><img src="data:image/svg+xml;base64,{encoded}" alt="{html.escape(node["description"])}"><figcaption>{html.escape(_figure_caption(node))}</figcaption></figure>')
        elif kind=="page-break":body.append('<hr data-brandkit-role="page-break">')
        elif kind=="footnote":body.append(f'<p id="fn-{html.escape(node["source_id"])}" {attrs(node)} class="footnote">[{html.escape(node["source_id"])}] {html.escape(node["text"])}</p>')
        else:raise RenderError(f"unsupported HTML AST block: {kind}")
    cover=policy["cover"];hero=f'<header class="cover {cover}" data-layout-strategy="{policy["id"]}"><h1>{html.escape(ast["frontmatter"]["title"])}</h1></header>'
    css=f"""@page {{ size: A4; margin: {tokens['page']['margin_mm']}mm; }}
:root {{--primary:{colorset['primary']};--accent:{colorset['accent']};--text:{colorset['text']};--surface:{colorset['surface']};}}
body{{margin:0;color:var(--text);font:10.2pt/1.45 sans-serif}}main{{max-width:170mm;margin:0 auto;padding:16mm 20mm}}.cover{{min-height:72mm;padding:20mm;background:#fff;border-left:8mm solid var(--accent)}}.cover.dark{{background:var(--primary);color:#fff;border:0}}.cover.hero{{min-height:48mm;background:var(--primary);color:#fff;border:0}}.cover.light-band{{border-top:36mm solid var(--primary);border-left:0}}h2{{color:var(--primary);margin-top:9mm}}aside{{margin:2mm 0;padding:3mm 6mm;background:var(--surface);border-left:2mm solid var(--accent)}}aside[data-appearance=simple]{{background:#fff;border:1px solid var(--accent)}}aside[data-appearance=minimal]{{background:#fff;border-left:0;border-top:1.2mm solid var(--accent)}}table{{border-collapse:collapse;width:100%}}th,td{{border:.2mm solid #b9bec5;padding:2mm;text-align:left}}img{{max-width:100%}}[data-brandkit-role=page-break]{{break-after:page;border:0}}"""
    rendered="<!doctype html><html lang=\"de\"><head><meta charset=\"utf-8\"><meta name=\"generator\" content=\"brandkit-pdf-renderer 1.2.28\"><title>"+html.escape(ast["frontmatter"]["title"])+"</title><style>"+css+"</style></head><body>"+hero+"<main>"+"".join(body)+"</main></body></html>\n"
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(rendered,encoding="utf-8")
    markers=re.findall(r'data-brandkit-id="([^"]+)"',rendered)
    return {"html_sha256":sha(output.read_bytes()),"layout_strategy":policy,"marker_count":len(markers),"markers":markers}


def _project_identity_sha256(project_root: Path, owner_approval: Path, fixture_identity_sha256: str | None = None) -> str:
    """Bind production runs to their path and deterministic E2E fixtures to a logical ID."""
    if fixture_identity_sha256 is None:
        return sha(str(project_root.resolve()).encode())
    if not re.fullmatch(r"[0-9a-f]{64}", fixture_identity_sha256):
        raise RenderError("fixture project identity is not a lowercase SHA-256")
    if load_json(owner_approval) != {"evidence_origin": "deterministic-e2e-fixture-not-provider-evidence"}:
        raise RenderError("logical project identity is restricted to deterministic E2E fixtures")
    return fixture_identity_sha256


def render(markdown: Path, dtype_path: Path, brand_path: Path, asset_root: Path, output: Path, result_path: Path, key_path: Path, work: Path,
           project: Path, humanizer_audit: Path, mermaid_cas: Path, owner_approval: Path, *, fixture_project_identity_sha256: str | None = None) -> dict[str, Any]:
    dtype=load_json(dtype_path); bases=contract.load_base_types();contract.validate_document_type(dtype,bases)
    ast=contract.run_validate(markdown,dtype_path,asset_root,False,None);figure_registry=_figure_registry(ast);figure_references=_figure_references(ast);brand,brand_manifest_hash=validate_brand(brand_path,dtype)
    project_root=brand_package.git_root(project); target=brand_package.current_state_target(project_root)
    activation,skill_lock,activation_bytes,lock_bytes=brand_package.read_state(project_root,target)
    pinned_brand,pinned_root,brand_content_hash=brand_package.validate_bound_state(project_root,activation,skill_lock)
    if pinned_root.resolve()!=brand_path.resolve().parent or pinned_brand["brand_id"]!=brand["brand_id"]:
        raise RenderError("renderer brand input differs from exact project activation")
    entry=next((x for x in brand["document_types"] if x["id"]==dtype["id"] and x["version"]==dtype["version"]),None)
    if not entry or sha(dtype_path.read_bytes())!=entry["contract_sha256"] or dtype_path.resolve()!= (brand_path.parent/entry["contract"]).resolve():
        raise RenderError("renderer document type differs from activated package contract")
    audit=load_json(humanizer_audit);contract.validate_schema(audit,"humanizer-audit.v1.schema.json","Humanizer audit")
    audit_payload=contract._verify_envelope(audit,"humanizer-audit.v1",key_path)
    if audit_payload["final_sha256"]!=ast["source_sha256"]:raise RenderError("Humanizer audit differs from final Markdown")
    one=dtype["id"]=="one-pager" or dtype.get("extends")=="one-pager";policy=layout_strategy(dtype,activation.get("overrides",{}));margin=float(brand["pdf"]["tokens"]["page"]["margin_mm"])*mm
    work.mkdir(parents=True,exist_ok=True);output.parent.mkdir(parents=True,exist_ok=True);result_path.parent.mkdir(parents=True,exist_ok=True)
    styles=make_styles(brand);primary=_hex(brand["pdf"]["tokens"]["color_roles"]["primary"],"primary")
    logo_asset=brand["assets"]["logos"][0];logo_path=brand_path.parent/logo_asset["path"]
    if sha(logo_path.read_bytes())!=logo_asset["sha256"]:raise RenderError("logo hash failed")
    source_records:list[dict[str,Any]]=[];known_page_count:int|None=None
    furniture_font=styles["body"].fontName;muted=_hex(brand["pdf"]["tokens"]["color_roles"]["muted"],"muted")
    def draw_logo(c:canvas.Canvas)->None:
        logo=SafeSvgFlowable(logo_path,35*mm);x=A4[0]-margin-logo.width;y=A4[1]-margin-logo.height;logo.drawOn(c,x,y)
        source_records.append({"source_id":"furniture-logo-1","type":"logo","page":1,"bbox_pdf_pt":[x,y,x+logo.width,y+logo.height],"visible":True,"evidence_kind":"vector","evidence_sha256":sha(logo_path.read_bytes())})
    def title_page(c: canvas.Canvas,doc: BaseDocTemplate)->None:
        if doc.page!=1:return
        c.saveState();cover=policy["cover"]
        if cover=="dark":c.setFillColor(primary);c.rect(0,0,A4[0],A4[1],fill=1,stroke=0)
        elif cover=="light-band":
            c.setFillColor(_hex(brand["pdf"]["tokens"]["color_roles"]["background"],"background"));c.rect(0,0,A4[0],A4[1],fill=1,stroke=0);c.setFillColor(primary);c.rect(0,A4[1]-72*mm,A4[0],72*mm,fill=1,stroke=0)
        else:
            c.setFillColor(_hex(brand["pdf"]["tokens"]["color_roles"]["background"],"background"));c.rect(0,0,A4[0],A4[1],fill=1,stroke=0);c.setFillColor(_hex(brand["pdf"]["tokens"]["color_roles"]["accent"],"accent"));c.rect(0,0,8*mm,A4[1],fill=1,stroke=0)
        draw_logo(c);kicker=dtype["id"].replace("-"," ").upper();kicker_size=9.;kicker_y=A4[1]-(65*mm if cover=="dark" else 32*mm);c.setFillColor(colors.white if cover in {"dark","light-band"} else primary);c.setFont(furniture_font,kicker_size);c.drawString(margin,kicker_y,kicker)
        source_records.append({"source_id":"furniture-cover-kicker-1","type":"cover-kicker","page":1,"bbox_pdf_pt":[margin,kicker_y+pdfmetrics.getDescent(furniture_font,kicker_size),margin+c.stringWidth(kicker,furniture_font,kicker_size),kicker_y+pdfmetrics.getAscent(furniture_font,kicker_size)],"visible":True,"evidence_kind":"text","evidence_text":kicker})
        p=Paragraph(ast["frontmatter"]["title"],styles["title-light" if cover=="editorial" else "title"]);pw,ph=p.wrapOn(c,A4[0]-2*margin,A4[1]);x=margin;y=A4[1]-(95*mm if cover=="dark" else 55*mm);p.drawOn(c,x,y)
        title_node=next((n for n in ast["children"] if n["type"]=="heading" and n["level"]==1),None)
        if title_node:source_records.append({"source_id":title_node.get("id","title"),"type":"title","page":1,"bbox_pdf_pt":[x,y,x+pw,y+ph],"visible":True})
        c.setFillColor(_hex(brand["pdf"]["tokens"]["color_roles"]["accent"],"accent"));c.rect(margin,y-8*mm,45*mm,2*mm,fill=1,stroke=0);c.restoreState()
        deck_node=next((n for n in ast["children"] if n["type"]=="paragraph"),None)
        if deck_node:
            deck_text="".join(token.get("text") or token.get("literal") or token.get("label") or token.get("reference_text") or "" for token in deck_node.get("inline",[])).strip()
            if deck_text:
                deck_style=ParagraphStyle("CoverDeck",parent=styles["body"],fontSize=11.5,leading=16,textColor=colors.white if cover=="dark" else styles["body"].textColor,spaceAfter=0)
                deck=Paragraph(html.escape(deck_text),deck_style);dw,dh=deck.wrapOn(c,min(A4[0]-2*margin,140*mm),60*mm);dx=margin;dy=A4[1]-(130*mm if cover=="dark" else 105*mm);deck.drawOn(c,dx,dy)
                source_records.append({"source_id":"furniture-cover-deck-1","type":"cover-deck","page":1,"bbox_pdf_pt":[dx,dy,dx+dw,dy+dh],"visible":True,"evidence_kind":"text","evidence_text":deck_text})
    def later(c: canvas.Canvas,doc: BaseDocTemplate)->None:
        if one:return
        c.saveState();divider_y=A4[1]-17*mm;c.setStrokeColor(primary);c.setLineWidth(.5);c.line(margin,divider_y,A4[0]-margin,divider_y)
        divider_hash=sha(canonical_json({"role":"page-divider","page":doc.page,"x0":margin,"x1":A4[0]-margin,"y":divider_y,"width":.5}))
        source_records.append({"source_id":f"furniture-divider-{doc.page}","type":"divider","page":doc.page,"bbox_pdf_pt":[margin,divider_y-.25,A4[0]-margin,divider_y+.25],"visible":True,"evidence_kind":"vector","evidence_sha256":divider_hash})
        if dtype["layout"]["header"]:
            text=ast["frontmatter"]["title"];size=8.;x=margin;y=A4[1]-14*mm;c.setFillColor(primary);c.setFont(furniture_font,size);c.drawString(x,y,text)
            source_records.append({"source_id":f"furniture-header-{doc.page}","type":"header","page":doc.page,"bbox_pdf_pt":[x,y+pdfmetrics.getDescent(furniture_font,size),x+c.stringWidth(text,furniture_font,size),y+pdfmetrics.getAscent(furniture_font,size)],"visible":True})
        c.restoreState()
    def end_page(c:canvas.Canvas,doc:BaseDocTemplate)->None:
        if known_page_count is None or one or doc.page<=1:return
        c.saveState();text=f"Seite {doc.page-1} von {known_page_count-1}";size=8.;right=A4[0]-margin;y=14*mm;width=c.stringWidth(text,furniture_font,size)
        c.setFillColor(muted);c.setFont(furniture_font,size);c.drawRightString(right,y,text)
        ascent=pdfmetrics.getAscent(furniture_font,size);descent=pdfmetrics.getDescent(furniture_font,size)
        source_records.append({"source_id":f"furniture-footer-{doc.page}","type":"footer","page":doc.page,"bbox_pdf_pt":[right-width,y+descent,right,y+ascent],"visible":True});c.restoreState()
    def one_page(c:canvas.Canvas,doc:BaseDocTemplate)->None:
        later(c,doc);c.saveState();c.setFillColor(primary);c.rect(0,A4[1]-48*mm,A4[0],48*mm,fill=1,stroke=0);draw_logo(c);p=Paragraph(ast["frontmatter"]["title"],styles["title"]);pw,ph=p.wrapOn(c,A4[0]-2*margin-35*mm,40*mm);x=margin;y=A4[1]-34*mm;p.drawOn(c,x,y);c.restoreState()
        title_node=next((n for n in ast["children"] if n["type"]=="heading" and n["level"]==1),None);source_records.append({"source_id":title_node.get("id","title") if title_node else "title","type":"hero-title","page":1,"bbox_pdf_pt":[x,y,x+pw,y+ph],"visible":True,"evidence_kind":"text","evidence_text":ast["frontmatter"]["title"]})
    def make_doc(path:Path)->InstrumentedDocTemplate:
        title_frame=Frame(margin,margin,A4[0]-2*margin,A4[1]-2*margin,id="title",showBoundary=0,leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)
        body_top=(54*mm if one else max(margin,20*mm));body_frame=Frame(margin,margin,A4[0]-2*margin,A4[1]-margin-body_top,id="body",showBoundary=0,leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)
        built=InstrumentedDocTemplate(str(path),source_records=source_records,pagesize=A4,leftMargin=margin,rightMargin=margin,topMargin=18*mm,bottomMargin=18*mm,title=ast["frontmatter"]["title"],author="BrandKit",creator="brandkit-pdf-renderer 1.2.28",subject=f'{dtype["id"]} document')
        if one:built.addPageTemplates([PageTemplate(id="one",frames=[body_frame],onPage=one_page)])
        else:built.addPageTemplates([PageTemplate(id="title",frames=[title_frame],onPage=title_page,autoNextPageTemplate="body"),PageTemplate(id="body",frames=[body_frame],onPage=later,onPageEnd=end_page)])
        return built
    def make_content()->tuple[list[Flowable],dict[str,list[dict[str,Any]]]]:
        built,evidence=story(ast,brand,dtype,asset_root,work,mermaid_cas,policy)
        if one:return [x for x in built if not (isinstance(x,Paragraph) and getattr(getattr(x,"style",None),"name",None)=="H1")],evidence
        body=[x for x in built if not (isinstance(x,Paragraph) and getattr(getattr(x,"style",None),"name",None)=="H1")]
        if not policy["toc"]:return [Spacer(1,125*mm),PageBreak(),*body],evidence
        toc=[];heading=Paragraph("Inhalt",styles["h2"]);heading._brandkit_source_id="toc-heading";heading._brandkit_source_type="toc";heading._brandkit_evidence_text="Inhalt";toc.append(heading)
        for number,node in enumerate((n for n in ast["children"] if n["type"]=="heading" and n["level"]==2),1):
            label=f'Zu Abschnitt {number:02d}: {node["text"]}';entry=Paragraph(f'<link href="#{node["id"]}">{label}</link>',styles["body"]);entry._brandkit_source_id=f'toc-{node["id"]}';entry._brandkit_source_type="toc";entry._brandkit_evidence_text=label;toc.append(entry)
        return [Spacer(1,125*mm),PageBreak(),*toc,Spacer(1,5*mm),*body],evidence
    # First pass establishes the exact page count without delaying page writes.
    # The second pass writes footer totals and creates bookmarks only after each
    # concrete PDF page reference exists.
    dry_output=work/"layout-pass.pdf";dry_content,_=make_content();make_doc(dry_output).build(dry_content,canvasmaker=_deterministic_canvas)
    known_page_count=len(contract._strict_pdf_pages(dry_output.read_bytes().rstrip(b"\r\n\t ")))
    source_records.clear();content,diagram_evidence=make_content();doc=make_doc(output)
    # The npm tree is a verified build dependency, not a publication output.
    # Keeping it below the run directory would force the commit snapshot to
    # hold tens of thousands of irrelevant files open and can exhaust FDs.
    mermaid_install=work/"mermaid-install"
    if mermaid_install.exists() or mermaid_install.is_symlink():
        if mermaid_install.is_symlink() or not mermaid_install.is_dir():
            raise RenderError("Mermaid working installation is not a regular directory")
        shutil.rmtree(mermaid_install)
    graphviz_install=work/"graphviz-install"
    if graphviz_install.exists() or graphviz_install.is_symlink():
        if graphviz_install.is_symlink() or not graphviz_install.is_dir():raise RenderError("Graphviz working installation is not a regular directory")
        shutil.rmtree(graphviz_install)
    doc.build(content,canvasmaker=_deterministic_canvas)
    dry_output.unlink(missing_ok=True)
    data=output.read_bytes().rstrip(b"\r\n\t "); output.write_bytes(data)
    parsed_pages=contract._strict_pdf_pages(data);pages=contract._pdf_pagination(parsed_pages,one);page_count=len(parsed_pages)
    lock=ROOT/"toolchain/pdf-renderer.production.v1.lock.json";trust=contract.validate_production_renderer_trust()
    key=contract._key_bytes(key_path)
    if sha(key)!=trust["issuer_key_sha256"]:raise RenderError("issuer key fingerprint does not match committed renderer trust")
    ast_path=work/"final.ast.json";ast_path.write_text(json.dumps(ast,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    required_furniture=[]
    for page_number in range(1,page_count+1):
        if (one or page_number>1) and dtype["layout"]["header"]:
            source_id=f"furniture-header-{page_number}";required_furniture.append(source_id)
        if not one and page_number>1:
            source_id=f"furniture-footer-{page_number}";required_furniture.append(source_id)
    content_nodes={};content_position=0
    for candidate in ast["children"]:
        if candidate["type"]=="blank":continue
        content_position+=1;content_nodes[candidate.get("id") or candidate.get("attrs",{}).get("id") or f"node-{content_position}"]=candidate
    def evidence_for(source_id: str, object_type: str, page: int) -> dict[str, Any]:
        """Declare what an independent extractor must find for this object."""
        explicit=next((x for x in source_records if x["source_id"]==source_id and "evidence_kind" in x),None)
        if explicit:
            value={"evidence_kind":explicit["evidence_kind"]}
            if "evidence_text" in explicit:value["evidence_text"]=explicit["evidence_text"]
            else:value["evidence_sha256"]=explicit["evidence_sha256"]
            return value
        if object_type == "header": return {"evidence_kind":"text","evidence_text":ast["frontmatter"]["title"]}
        if object_type == "footer": return {"evidence_kind":"text","evidence_text":f"Seite {page-1} von {page_count-1}"}
        if object_type == "figure-caption":
            figure_id=source_id.rsplit(":caption",1)[0];node=content_nodes.get(figure_id)
            if node is None:raise RenderError("figure caption has no registered figure")
            return {"evidence_kind":"text","evidence_text":_figure_caption(node)}
        node=content_nodes.get(source_id)
        if node is None: return {"evidence_kind":"raster","evidence_sha256":"0"*64}
        kind=node["type"]
        if kind=="heading":
            text=node["text"]
            if node["level"]==2 and policy["section_numbers"]:
                number=next(i for i,candidate in enumerate((n for n in ast["children"] if n["type"]=="heading" and n["level"]==2),1) if candidate.get("id")==node.get("id"));text=f"Kapitel {number:02d}  {text}"
            elif node["level"]==2:text=f"Abschnitt: {text}"
        elif kind=="paragraph": text="".join(t.get("text") or t.get("literal") or t.get("label") or t.get("reference_text") or "" for t in node.get("inline",[]))
        elif kind=="callout":
            title,body=callout_text(node,ast)
            if node["kind"]=="metric":
                attrs=node["attrs"];fact=next(x for x in ast["frontmatter"]["facts"] if x["id"]==attrs["value_fact"])
                text=f'{title} {attrs["label"]}: {fact["value"]} Fakt: {attrs["value_fact"]} Quelle: {attrs["source_id"]}'
            else:
                text=re.sub(r"<[^>]+>"," ",title+" "+body);text=re.sub(r"\s+"," ",html.unescape(text)).strip()
        elif kind=="image":
            path=_asset_path(ast,asset_root,node["asset"]);return {"evidence_kind":"vector" if path.suffix.lower()==".svg" else "raster","evidence_sha256":sha(path.read_bytes())}
        elif kind=="table": text=" ".join(str(value) for value in node["headers"])
        elif kind in {"ordered-list","unordered-list"} and node.get("items"):
            text="".join(t.get("text") or t.get("literal") or t.get("label") or t.get("reference_text") or "" for t in node["items"][0].get("inline",[]))
        elif kind=="footnote": text=f'[{node["source_id"]}] {node["text"]}'
        elif kind=="mermaid": return {"evidence_kind":"vector","evidence_sha256":next(x["svg_sha256"] for x in diagram_evidence["mermaid"] if x["id"]==node["id"])}
        elif kind=="graphviz": return {"evidence_kind":"vector","evidence_sha256":next(x["svg_sha256"] for x in diagram_evidence["graphviz"] if x["id"]==node["id"])}
        else: return {"evidence_kind":"vector","evidence_sha256":sha(canonical_json(node))}
        return {"evidence_kind":"text","evidence_text":text}
    grouped=[]
    for source_id in dict.fromkeys(x["source_id"] for x in source_records):
        rows=[x for x in source_records if x["source_id"]==source_id];page=rows[0]["page"]
        if any(x["page"]!=page for x in rows):raise RenderError(f"source object split across pages: {source_id}")
        boxes=[x["bbox_pdf_pt"] for x in rows];left=min(x[0] for x in boxes);bottom=min(x[1] for x in boxes);right=max(x[2] for x in boxes);top=max(x[3] for x in boxes)
        grouped.append({"source_id":source_id,"type":rows[0]["type"],"page":page,"bbox_pt":[round(left,3),round(A4[1]-top,3),round(right,3),round(A4[1]-bottom,3)],"visible":True,**evidence_for(source_id,rows[0]["type"],page)})
    if {x["page"] for x in grouped} != set(range(1,page_count+1)):
        raise RenderError("instrumented source map page domain differs from the exact PDF")
    source_map_value={"schema_version":"renderer-source-map.v1","page_coordinate_system":"pdf-points-top-left","page_count":page_count,"required_furniture":required_furniture,"objects":grouped,"visible_inventory":pdf_visual_qa.closed_visible_inventory(output),"expected_internal_links":_expected_internal_links(ast,policy["toc"]),"figure_registry":figure_registry,"figure_references":figure_references}
    source_map_path=work/"renderer-source-map.json";source_map_path.write_text(json.dumps(source_map_value,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    qa_path=result_path.parent/"visual-qa.json";png_dir=result_path.parent/"pages"
    qa=pdf_visual_qa.inspect(output,png_dir,qa_path,ast_path,brand_path,144,source_map_path)
    assets=[{k:item[k] for k in ("id","path","media_type","sha256")} for item in ast["frontmatter"]["assets"]]
    brand_assets=[]
    for family in ("fonts","logos"):
        for item in brand["assets"][family]:
            rights=item["rights"];brand_assets.append({"id":item["id"],"path":item["path"],"media_type":item["media_type"],"sha256":item["sha256"],"rights_path":rights["evidence_path"],"rights_sha256":rights["evidence_sha256"]})
    binding={"project_identity_sha256":_project_identity_sha256(project_root,owner_approval,fixture_project_identity_sha256),"activation_sha256":brand_package.state_identity(activation_bytes,lock_bytes),"brand":{"id":brand["brand_id"],"version":brand["version"],"manifest_sha256":brand_manifest_hash,"content_sha256":brand_content_hash},"document_type":{"id":dtype["id"],"version":dtype["version"],"sha256":sha(dtype_path.read_bytes())},"markdown_contract_sha256":sha((ROOT/"markdown-contract.v2.json").read_bytes()),"humanizer_audit_sha256":sha(humanizer_audit.read_bytes()),"owner_approval_sha256":sha(owner_approval.read_bytes())}
    figure_result=[{**item,"caption_text":_figure_caption(next(node for node in ast["children"] if node.get("id")==item["id"])),"reference_text":f'Abbildung {item["number"]}',"reference_count":sum(ref["target_id"]==item["id"] for ref in figure_references)} for item in figure_registry]
    payload={"final_input_sha256":ast["source_sha256"],"output_pdf":{"path":output.relative_to(result_path.parent).as_posix(),"sha256":sha(data)},"engine":trust["engine"],"exit":{"code":0,"errors":[],"warnings":[]},"assets":assets,"brand_assets":brand_assets,"pages":pages,"binding":binding,"figures":figure_result,"mermaid":diagram_evidence["mermaid"],"graphviz":diagram_evidence["graphviz"],"visual_qa":{"path":qa_path.relative_to(result_path.parent).as_posix(),"sha256":sha(qa_path.read_bytes()),"status":"pass","dpi":144,"page_pngs":[{"index":x["index"],"path":x["png_path"],"sha256":x["png_sha256"]} for x in qa["pages"]]},"nonce":"brandkit-render-0001","created_at":"2026-08-26T00:00:00Z"}
    signed={"schema_version":"render-result.v1","issuer":ISSUER,"payload":payload}; envelope={**signed,"hmac_sha256":hmac.new(key,canonical_json(signed),hashlib.sha256).hexdigest()}
    contract.validate_schema(envelope,"render-result.v1.schema.json","render result");result_path.write_text(json.dumps(envelope,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    return envelope


def main(argv: list[str]|None=None)->int:
    p=argparse.ArgumentParser();p.add_argument("--markdown",type=Path,required=True);p.add_argument("--document-type",type=Path,required=True);p.add_argument("--brand",type=Path,required=True);p.add_argument("--asset-root",type=Path,required=True);p.add_argument("--output",type=Path,required=True);p.add_argument("--render-result",type=Path,required=True);p.add_argument("--issuer-key",type=Path,required=True);p.add_argument("--work",type=Path,required=True);p.add_argument("--project",type=Path,required=True);p.add_argument("--humanizer-audit",type=Path,required=True);p.add_argument("--mermaid-cas",type=Path,required=True);p.add_argument("--owner-approval",type=Path,required=True);a=p.parse_args(argv)
    try:render(a.markdown,a.document_type,a.brand,a.asset_root,a.output,a.render_result,a.issuer_key,a.work,a.project,a.humanizer_audit,a.mermaid_cas,a.owner_approval);print(f"Rendered {a.output}");return 0
    except (RenderError,contract.ContractError,pdf_visual_qa.QaError,OSError,ValueError) as exc:print(f"PDF render failed: {exc}",file=sys.stderr);return 1

if __name__=="__main__":raise SystemExit(main())
