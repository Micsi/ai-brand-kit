#!/usr/bin/env python3
"""Closed, measured visual QA for BrandKit PDFs."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import subprocess
import xml.etree.ElementTree as ET
import math
from collections import Counter
from pathlib import Path

from PIL import Image
from pypdf import PdfReader
from pypdf.generic import ContentStream


class QaError(Exception):
    pass


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tool(command: str, version_args: list[str]) -> dict[str, str]:
    resolved = Path(subprocess.run(["command", "-v", command], shell=False, capture_output=True, text=True).stdout.strip()) if False else None
    import shutil
    binary = shutil.which(command)
    if not binary:
        raise QaError(f"required QA tool is missing: {command}")
    out = subprocess.run([binary, *version_args], capture_output=True, text=True, check=False)
    version = (out.stdout or out.stderr).splitlines()[0].strip()
    return {"path": str(Path(binary).resolve()), "sha256": sha(Path(binary).resolve()), "version": version}


def luminance(hex_color: str) -> float:
    values = [int(hex_color[i:i+2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in values]
    return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2]


def contrast(a: str, b: str) -> float:
    x, y = sorted((luminance(a), luminance(b)), reverse=True)
    return (x + .05) / (y + .05)


def rgb_luminance(rgb: tuple[int, int, int]) -> float:
    values=[v/255 for v in rgb]; linear=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in values]
    return .2126*linear[0]+.7152*linear[1]+.0722*linear[2]


def pixel_contrast(crop: Image.Image) -> float:
    pixels=list(crop.convert("RGB").getdata())
    if not pixels: return 1.0
    levels=sorted(rgb_luminance(p) for p in pixels)
    low=levels[0]; high=levels[-1]
    return (high+.05)/(low+.05)


def _rgb_ratio(foreground: list[float], background: tuple[int,int,int], alpha: float) -> float:
    """WCAG ratio against one pixel measured from the final Poppler paint stack."""
    bg=[x/255 for x in background]
    effective=[max(0.,min(1.,foreground[i]*alpha+bg[i]*(1-alpha))) for i in range(3)]
    def lum(values):
        linear=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in values]
        return .2126*linear[0]+.7152*linear[1]+.0722*linear[2]
    high,low=sorted((lum(effective),lum(bg)),reverse=True)
    return (high+.05)/(low+.05)


def measured_backgrounds(image: Image.Image, box: tuple[float,float,float,float], sx: float, sy: float,
                         foreground: list[float], alpha: float) -> list[tuple[int,int,int]]:
    """Return concrete backgrounds below a word from the fully composed raster.

    Poppler is the normative recursive compositor here: page, Form, annotation
    appearance, pattern, shading, image, soft mask, blend and clip have already
    been applied in their PDF paint order.  The most frequent exact pixels in
    the word rectangle are background samples; antialias colours near the
    predicted glyph colour are excluded.  Keeping every material cluster (>=1%
    of pixels) makes patterned/raster/transparency backgrounds fail on their
    weakest local contrast instead of being averaged away.
    """
    x0=max(0,int(box[0]*sx));y0=max(0,int(box[1]*sy));x1=min(image.width,max(x0+1,int(math.ceil(box[2]*sx))));y1=min(image.height,max(y0+1,int(math.ceil(box[3]*sy))))
    pixels=list(image.crop((x0,y0,x1,y1)).convert("RGB").getdata())
    if not pixels:return []
    counts=Counter(pixels);minimum=max(1,math.ceil(len(pixels)*.01))
    # A word box can be mostly glyph ink (notably a large white hero title).
    # Bind the baseline surface to the final raster immediately surrounding
    # the word, where no glyph belonging to that word is painted.  This is
    # still Poppler's fully composed result and therefore covers page fills,
    # Forms, images, transparency and clipping without trusting renderer
    # intent or a declared token.  The in-box clusters below retain the
    # weakest material background for patterned/raster surfaces.
    radius=max(2,int(round(max(sx,sy)*1.5)))
    rx0=max(0,x0-radius);ry0=max(0,y0-radius);rx1=min(image.width,x1+radius);ry1=min(image.height,y1+radius)
    expanded=image.crop((rx0,ry0,rx1,ry1)).convert("RGB")
    ring=[]
    for yy in range(expanded.height):
        for xx in range(expanded.width):
            inside=(x0-rx0)<=xx<(x1-rx0) and (y0-ry0)<=yy<(y1-ry0)
            if not inside:ring.append(expanded.getpixel((xx,yy)))
    base=Counter(ring).most_common(1)[0][0] if ring else counts.most_common(1)[0][0]
    predicted=tuple(round(255*(foreground[i]*alpha+(base[i]/255)*(1-alpha))) for i in range(3))
    def segment_distance(colour):
        direction=[base[i]-predicted[i] for i in range(3)];den=sum(x*x for x in direction)
        t=0. if den==0 else max(0.,min(1.,sum((colour[i]-predicted[i])*direction[i] for i in range(3))/den))
        return sum((colour[i]-(predicted[i]+t*direction[i]))**2 for i in range(3))**.5
    material=[]
    for colour,count in counts.most_common(32):
        if count<minimum:continue
        # Exact/near glyph and antialias pixels are foreground, not a surface.
        if colour!=base and segment_distance(colour)<8:continue
        material.append(colour)
    if not material:material=[counts.most_common(1)[0][0]]
    return material


def intersection_ratio(a: tuple[float,float,float,float], b: tuple[float,float,float,float]) -> float:
    x=max(0,min(a[2],b[2])-max(a[0],b[0]));y=max(0,min(a[3],b[3])-max(a[1],b[1]))
    if not x or not y:return 0.0
    return x*y/min((a[2]-a[0])*(a[3]-a[1]),(b[2]-b[0])*(b[3]-b[1]))


def sequence_box(group: list[dict[str,str]], text: str) -> tuple[float,float,float,float] | None:
    wanted=[x.casefold() for x in re.findall(r"\w+",text,flags=re.UNICODE)]
    hay=[(piece.casefold(),row) for row in group for piece in re.findall(r"\w+",row["text"],flags=re.UNICODE)]
    if not wanted:return None
    for start in range(len(hay)-len(wanted)+1):
        if [x[0] for x in hay[start:start+len(wanted)]]==wanted:
            rows=[x[1] for x in hay[start:start+len(wanted)]];left=min(float(x["left"]) for x in rows);top=min(float(x["top"]) for x in rows)
            right=max(float(x["left"])+float(x["width"]) for x in rows);bottom=max(float(x["top"])+float(x["height"]) for x in rows)
            return left,top,right,bottom
    return None


def sequence_word_indexes(group: list[dict[str,str]], text: str) -> list[int]:
    wanted=[x.casefold() for x in re.findall(r"\w+",text,flags=re.UNICODE)]
    hay=[(piece.casefold(),index) for index,row in enumerate(group) for piece in re.findall(r"\w+",row["text"],flags=re.UNICODE)]
    for start in range(len(hay)-len(wanted)+1):
        if wanted and [x[0] for x in hay[start:start+len(wanted)]]==wanted:return sorted(set(x[1] for x in hay[start:start+len(wanted)]))
    return []


def _matrix_multiply(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
    """Compose PDF affine matrices using ReportLab/PDF operand order."""
    a0,a1,a2,a3,a4,a5=a;b0,b1,b2,b3,b4,b5=b
    return (a0*b0+a2*b1,a1*b0+a3*b1,a0*b2+a2*b3,a1*b2+a3*b3,a0*b4+a2*b5+a4,a1*b4+a3*b5+a5)


def _point(matrix: tuple[float, ...], x: float, y: float) -> tuple[float,float]:
    a,b,c,d,e,f=matrix
    return a*x+c*y+e,b*x+d*y+f


def _top_box(points: list[tuple[float,float]], height: float) -> list[float]:
    xs=[x for x,_ in points];ys=[y for _,y in points]
    return [round(min(xs),3),round(height-max(ys),3),round(max(xs),3),round(height-min(ys),3)]


def _bounded_box(points: list[tuple[float,float]], width: float, height: float, label: str) -> list[float]:
    if not points or any(not math.isfinite(v) for point in points for v in point):raise QaError(f"{label} has non-finite geometry")
    xs=[x for x,_ in points];ys=[y for _,y in points];raw=[min(xs),height-max(ys),max(xs),height-min(ys)];epsilon=.01
    if abs(raw[2]-raw[0])<epsilon:raw[0]-=.25;raw[2]+=.25
    if abs(raw[3]-raw[1])<epsilon:raw[1]-=.25;raw[3]+=.25
    if not (-epsilon<=raw[0]<raw[2]<=width+epsilon and -epsilon<=raw[1]<raw[3]<=height+epsilon):raise QaError(f"{label} lies outside its PDF page: {[round(x,3) for x in raw]}")
    raw=[max(0.,raw[0]),max(0.,raw[1]),min(width,raw[2]),min(height,raw[3])]
    # Rounding a MediaBox edge up (A4 is 595.2756 pt wide) must not make a
    # previously bounded object appear outside the exact PDF page domain.
    rounded=[round(x,3) for x in raw]
    rounded[0]=max(0.,rounded[0]);rounded[1]=max(0.,rounded[1])
    rounded[2]=min(width,rounded[2]);rounded[3]=min(height,rounded[3])
    return rounded


def _clip_box(box: list[float], clip: list[float] | None, label: str) -> list[float] | None:
    if clip is None:return box
    value=[max(box[0],clip[0]),max(box[1],clip[1]),min(box[2],clip[2]),min(box[3],clip[3])]
    if value[0]>=value[2] or value[1]>=value[3]:return None
    return [round(x,3) for x in value]


def _stream_hash(obj) -> str:
    if hasattr(obj,"get_data"):
        data=obj.get_data()
    elif hasattr(obj,"write_to_stream"):
        # `str(DictionaryObject)` can change after lazy indirect objects have
        # been resolved.  PDF serialization preserves the concrete object and
        # its stable indirect references, so repeated inventory is identical.
        buffer=io.BytesIO();obj.write_to_stream(buffer,None);data=buffer.getvalue()
    else:
        data=canonical_inventory_bytes({"object":str(obj)})
    return hashlib.sha256(data).hexdigest()


def _resource_dict(resources, name: str) -> dict:
    value=(resources or {}).get(name) or {}
    return value.get_object() if hasattr(value,"get_object") else value


def _text_value(operands, op: str) -> str:
    values=operands[0] if op=="TJ" and operands else operands[:1]
    if not isinstance(values,(list,tuple)):values=[values]
    return "".join(str(x) for x in values if isinstance(x,(str,bytes)) or x.__class__.__name__.endswith("StringObject"))


def bind_word_paints(page_words: list[dict[str,str]], paints: list[dict]) -> list[dict]:
    """Bind Poppler words to the PDF text operators that paint their glyphs.

    Poppler may join punctuation from the next ``Tj`` operator into the prior
    word (``Anteil`` + ``: 42 %`` becomes ``Anteil:``).  A positional word-count
    expansion therefore shifts every subsequent paint.  When both extractors
    expose the same non-whitespace character stream, bind by character overlap;
    retain the conservative whitespace fallback for subset-font control codes.
    """
    fallback=[]
    for paint in paints:
        fallback.extend([paint]*max(1,len(paint.get("text","").split())))
    fallback=[fallback[min(index,len(fallback)-1)] for index in range(len(page_words))] if fallback else []

    operator_by_character=[];paint_stream=[]
    for paint_index,paint in enumerate(paints):
        for character in paint.get("text",""):
            if not character.isspace():
                paint_stream.append(character);operator_by_character.append(paint_index)
    word_stream=[];word_ranges=[]
    for word in page_words:
        start=len(word_stream)
        word_stream.extend(character for character in word.get("text","") if not character.isspace())
        word_ranges.append((start,len(word_stream)))
    if word_stream!=paint_stream or any(start==end for start,end in word_ranges):
        return fallback
    bound=[]
    for start,end in word_ranges:
        counts=Counter(operator_by_character[start:end])
        paint_index=max(counts,key=lambda index:(counts[index],-index))
        bound.append(paints[paint_index])
    return bound


def closed_visible_inventory(pdf: Path, words: list[list[dict[str,str]]] | None = None) -> list[dict]:
    """Inventory every independently observable text, paint and XObject operation.

    The inventory is intentionally lower-level than the renderer source map.  A
    new word, line, fill, raster or Form XObject therefore changes the closed
    domain even when the renderer forgot to declare a semantic source object.
    """
    if words is None:
        binary=tool("pdftotext",["-v"])["path"]
        tsv=subprocess.run([binary,"-tsv",str(pdf),"-"],check=True,capture_output=True,text=True).stdout
        rows=list(csv.DictReader(io.StringIO(tsv),delimiter="\t"))
        pages=sorted({int(x["page_num"]) for x in rows if x.get("text")=="###PAGE###"})
        words=[[x for x in rows if x.get("level")=="5" and int(x["page_num"])==page] for page in pages]
    reader=PdfReader(str(pdf));inventory=[]
    for page_number,page in enumerate(reader.pages,1):
        page_width=float(page.mediabox.width);page_height=float(page.mediabox.height)
        paints=text_paint_inventory_page(page,reader,page_number)
        bound_paints=bind_word_paints(words[page_number-1],paints)
        for word_index,row in enumerate(words[page_number-1],1):
            left,top,width,height=(float(row[x]) for x in ("left","top","width","height"))
            raw=row["text"].encode("utf-8")
            box=[round(left,3),round(top,3),round(left+width,3),round(top+height,3)]
            if not (0<=box[0]<box[2]<=page_width and 0<=box[1]<box[3]<=page_height):raise QaError("Poppler text bbox lies outside page")
            paint=bound_paints[word_index-1] if word_index<=len(bound_paints) else {"font":"unknown","rgb":[0.,0.,0.],"stroke_rgb":[0.,0.,0.],"fill_alpha":1.,"stroke_alpha":1.,"render_mode":0,"font_size_pt":0.,"bold":False}
            identity={"text":row["text"],"font":paint["font"],"rgb":paint["rgb"],"stroke_rgb":paint["stroke_rgb"],"fill_alpha":paint["fill_alpha"],"stroke_alpha":paint["stroke_alpha"],"render_mode":paint["render_mode"],"font_size_pt":paint["font_size_pt"],"bold":paint["bold"]}
            inventory.append({"object_id":f"p{page_number}-text-{word_index:04d}","kind":"text","pdf_object_sha256":hashlib.sha256(canonical_inventory_bytes(identity)).hexdigest(),"transform":[1,0,0,1,round(left,3),round(top,3)],"bbox_pt":box,"paint":identity})
        resources=page.get("/Resources") or {}
        content=ContentStream(page.get_contents(),reader) if page.get_contents() is not None else None
        counters={"vector":0,"clip":0,"shading":0,"pattern":0,"raster":0,"form":0,"inline-image":0,"annotation":0,"widget":0}
        def add(kind,obj_hash,ctm,points,paint,clip=None):
            box=_clip_box(_bounded_box(points,page_width,page_height,kind),clip,kind)
            if box is None:return
            counters[kind]+=1;inventory.append({"object_id":f"p{page_number}-{kind}-{counters[kind]:04d}","kind":kind,"pdf_object_sha256":obj_hash,"transform":[round(v,6) for v in ctm],"bbox_pt":box,"paint":paint})
        def walk(stream,resource_scope,outer=(1.,0.,0.,1.,0.,0.),seen=(),scope="page",initial_clip=None):
            if stream is None:return
            ctm=outer;stack=[];path_points=[];fill=[0.,0.,0.];stroke=[0.,0.,0.];fill_alpha=stroke_alpha=1.;clip=initial_clip;blend="/Normal";softmask="/None";fill_pattern=stroke_pattern=None
            ext=_resource_dict(resource_scope,"/ExtGState");xobjects=_resource_dict(resource_scope,"/XObject");shadings=_resource_dict(resource_scope,"/Shading");patterns=_resource_dict(resource_scope,"/Pattern")
            for operands,operator in stream.operations:
                op=operator.decode("ascii","replace")
                if op=="q":stack.append((ctm,list(path_points),fill,stroke,fill_alpha,stroke_alpha,clip,blend,softmask,fill_pattern,stroke_pattern))
                elif op=="Q":
                    if stack:ctm,path_points,fill,stroke,fill_alpha,stroke_alpha,clip,blend,softmask,fill_pattern,stroke_pattern=stack.pop()
                elif op=="cm":ctm=_matrix_multiply(ctm,tuple(float(x) for x in operands))
                elif op=="rg":fill=[float(x) for x in operands]
                elif op=="RG":stroke=[float(x) for x in operands]
                elif op=="g":fill=[float(operands[0])]*3
                elif op=="G":stroke=[float(operands[0])]*3
                elif op=="gs":
                    gs=ext.get(operands[0]);gs=gs.get_object() if hasattr(gs,"get_object") else gs or {}
                    fill_alpha=float(gs.get("/ca",fill_alpha));stroke_alpha=float(gs.get("/CA",stroke_alpha));blend=str(gs.get("/BM",blend));sm=gs.get("/SMask",softmask);softmask=_stream_hash(sm.get_object() if hasattr(sm,"get_object") else sm) if sm is not None and str(sm)!="/None" else "/None"
                elif op in {"scn","SCN"}:
                    name=next((x for x in reversed(operands) if str(x).startswith("/")),None)
                    if name is not None:
                        ref=patterns.get(name);obj=ref.get_object() if hasattr(ref,"get_object") else ref
                        if obj is None:raise QaError(f"unresolved paint pattern: {name}")
                        (fill_pattern if op=="scn" else stroke_pattern)
                        if op=="scn":fill_pattern=(str(name),_stream_hash(obj))
                        else:stroke_pattern=(str(name),_stream_hash(obj))
                elif op in {"m","l"}:path_points.append(_point(ctm,float(operands[0]),float(operands[1])))
                elif op=="re":
                    x,y,w,h=(float(v) for v in operands);path_points.extend([_point(ctm,x,y),_point(ctm,x+w,y),_point(ctm,x+w,y+h),_point(ctm,x,y+h)])
                elif op in {"c","v","y"}:
                    values=[float(v) for v in operands]
                    for index in range(0,len(values),2):path_points.append(_point(ctm,values[index],values[index+1]))
                elif op in {"W","W*"} and path_points:
                    new_clip=_bounded_box(path_points,page_width,page_height,"clip");clip=_clip_box(new_clip,clip,"clip")
                    payload={"operator":op,"fill":fill,"stroke":stroke,"fill_alpha":fill_alpha,"stroke_alpha":stroke_alpha,"scope":scope};add("clip",hashlib.sha256(canonical_inventory_bytes({**payload,"bbox":new_clip})).hexdigest(),ctm,path_points,payload,clip)
                elif op in {"S","s","f","F","f*","B","B*","b","b*"}:
                    if path_points:
                        payload={"operator":op,"fill":fill,"stroke":stroke,"fill_alpha":fill_alpha,"stroke_alpha":stroke_alpha,"fill_pattern":fill_pattern,"stroke_pattern":stroke_pattern,"blend":blend,"softmask":softmask,"scope":scope};add("vector",hashlib.sha256(canonical_inventory_bytes({**payload,"points":[[round(v,6) for v in p] for p in path_points]})).hexdigest(),ctm,path_points,payload,clip)
                    path_points=[]
                elif op=="n":path_points=[]
                elif op=="sh":
                    obj=shadings.get(operands[0]);obj=obj.get_object() if hasattr(obj,"get_object") else obj
                    points=[_point(ctm,0,0),_point(ctm,page_width,0),_point(ctm,page_width,page_height),_point(ctm,0,page_height)]
                    add("shading",_stream_hash(obj),ctm,points,{"operator":"sh","resource":str(operands[0]),"blend":blend,"softmask":softmask,"scope":scope},clip)
                elif op=="Do":
                    name=operands[0];ref=xobjects.get(name)
                    if ref is None:raise QaError(f"unresolved visible XObject: {name}")
                    obj=ref.get_object();subtype=str(obj.get("/Subtype"));kind="raster" if subtype=="/Image" else "form"
                    bbox=[0.,0.,1.,1.] if kind=="raster" else [float(v) for v in (obj.get("/BBox") or [0,0,1,1])]
                    points=[_point(ctm,bbox[0],bbox[1]),_point(ctm,bbox[2],bbox[1]),_point(ctm,bbox[2],bbox[3]),_point(ctm,bbox[0],bbox[3])]
                    image_geometry={}
                    if kind=="raster":
                        pixel_width=int(obj.get("/Width",0));pixel_height=int(obj.get("/Height",0))
                        if pixel_width<=0 or pixel_height<=0:raise QaError("raster XObject has no positive intrinsic pixel dimensions")
                        image_geometry={"pixel_width":pixel_width,"pixel_height":pixel_height}
                    add(kind,_stream_hash(obj),ctm,points,{"resource":str(name),"subtype":subtype,"fill_alpha":fill_alpha,"stroke_alpha":stroke_alpha,"blend":blend,"softmask":softmask,"scope":scope,**image_geometry},clip)
                    marker=getattr(ref,"idnum",id(obj))
                    if kind=="form" and marker not in seen:
                        matrix=tuple(float(x) for x in (obj.get("/Matrix") or [1,0,0,1,0,0]));walk(ContentStream(obj,reader),obj.get("/Resources") or resource_scope,_matrix_multiply(ctm,matrix),seen+(marker,),f"{scope}/form:{name}",clip)
                elif op=="INLINE IMAGE":
                    settings=operands.get("settings",{}) if isinstance(operands,dict) else {};data=operands.get("data",b"") if isinstance(operands,dict) else b"";points=[_point(ctm,0,0),_point(ctm,1,0),_point(ctm,1,1),_point(ctm,0,1)]
                    add("inline-image",hashlib.sha256(bytes(data)).hexdigest(),ctm,points,{"settings_sha256":hashlib.sha256(canonical_inventory_bytes({str(k):str(v) for k,v in settings.items()})).hexdigest(),"blend":blend,"softmask":softmask,"scope":scope},clip)
        if content is not None:walk(content,resources)
        annots=page.get("/Annots") or []
        for ref in annots:
            obj=ref.get_object();rect=[float(v) for v in obj.get("/Rect",[])];
            if len(rect)!=4:raise QaError("visible annotation has no finite rectangle")
            points=[(rect[0],rect[1]),(rect[2],rect[1]),(rect[2],rect[3]),(rect[0],rect[3])];kind="widget" if str(obj.get("/Subtype"))=="/Widget" else "annotation";appearance=obj.get("/AP");digest=_stream_hash(obj)
            if appearance:
                normal=appearance.get_object().get("/N") if hasattr(appearance,"get_object") else appearance.get("/N")
                if normal is not None:
                    normal=normal.get_object() if hasattr(normal,"get_object") else normal;digest=_stream_hash(normal)
            add(kind,digest,(1.,0.,0.,1.,0.,0.),points,{"subtype":str(obj.get("/Subtype")),"appearance":bool(appearance)})
            if appearance and normal is not None:
                bbox=[float(v) for v in (normal.get("/BBox") or [0,0,1,1])];bw=max(.001,bbox[2]-bbox[0]);bh=max(.001,bbox[3]-bbox[1]);matrix=(abs(rect[2]-rect[0])/bw,0.,0.,abs(rect[3]-rect[1])/bh,min(rect[0],rect[2])-bbox[0],min(rect[1],rect[3])-bbox[1])
                walk(ContentStream(normal,reader),normal.get("/Resources") or resources,matrix,(),f"appearance:{getattr(ref,'idnum','direct')}",_bounded_box(points,page_width,page_height,"appearance"))
    return inventory


def canonical_inventory_bytes(value: dict) -> bytes:
    return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode("utf-8")


def text_paint_inventory_page(page,reader,page_number: int) -> list[dict]:
    resources=page.get("/Resources") or {};result=[];index=0
    def walk(stream,scope_resources,state=None,seen=(),scope="page"):
        nonlocal index
        if stream is None:return
        fonts=_resource_dict(scope_resources,"/Font");ext=_resource_dict(scope_resources,"/ExtGState");xobjects=_resource_dict(scope_resources,"/XObject")
        rgb,stroke_rgb,font_size,font,fill_alpha,stroke_alpha,render_mode,blend,softmask=state or ((0.,0.,0.),(0.,0.,0.),0.,"unknown",1.,1.,0,"/Normal","/None")
        stack=[]
        for operands,operator in stream.operations:
            op=operator.decode("ascii","replace")
            if op=="q":stack.append((rgb,stroke_rgb,font_size,font,fill_alpha,stroke_alpha,render_mode,blend,softmask))
            elif op=="Q" and stack:rgb,stroke_rgb,font_size,font,fill_alpha,stroke_alpha,render_mode,blend,softmask=stack.pop()
            elif op=="rg":rgb=tuple(float(x) for x in operands)
            elif op=="RG":stroke_rgb=tuple(float(x) for x in operands)
            elif op=="g":rgb=(float(operands[0]),)*3
            elif op=="G":stroke_rgb=(float(operands[0]),)*3
            elif op=="k":
                c,m,y,k=(float(x) for x in operands);rgb=(1-min(1,c+k),1-min(1,m+k),1-min(1,y+k))
            elif op=="Tf":
                font_size=float(operands[1]);font_ref=fonts.get(operands[0]);font_obj=font_ref.get_object() if hasattr(font_ref,"get_object") else font_ref or {};font=str(font_obj.get("/BaseFont",operands[0]))
            elif op=="Tr":render_mode=int(operands[0])
            elif op=="gs":
                gs=ext.get(operands[0]);gs=gs.get_object() if hasattr(gs,"get_object") else gs or {};fill_alpha=float(gs.get("/ca",fill_alpha));stroke_alpha=float(gs.get("/CA",stroke_alpha));blend=str(gs.get("/BM",blend));sm=gs.get("/SMask");softmask="/None" if sm in (None,"/None") else _stream_hash(sm.get_object() if hasattr(sm,"get_object") else sm)
            elif op=="Do":
                ref=xobjects.get(operands[0]);obj=ref.get_object() if hasattr(ref,"get_object") else ref
                if obj is not None and str(obj.get("/Subtype"))=="/Form":
                    marker=getattr(ref,"idnum",id(obj))
                    if marker not in seen:walk(ContentStream(obj,reader),obj.get("/Resources") or scope_resources,(rgb,stroke_rgb,font_size,font,fill_alpha,stroke_alpha,render_mode,blend,softmask),seen+(marker,),f"{scope}/form:{operands[0]}")
            elif op in {"Tj","TJ","'","\""}:
                index+=1;result.append({"page":page_number,"operator_index":index,"rgb":[round(x,6) for x in rgb],"stroke_rgb":[round(x,6) for x in stroke_rgb],"font_size_pt":round(font_size,3),"font":font,"bold":"bold" in font.casefold(),"fill_alpha":round(fill_alpha,6),"stroke_alpha":round(stroke_alpha,6),"render_mode":render_mode,"blend":blend,"softmask":softmask,"scope":scope,"text":_text_value(operands,op)})
    content=ContentStream(page.get_contents(),reader) if page.get_contents() is not None else None
    walk(content,resources)
    for ref in page.get("/Annots") or []:
        obj=ref.get_object();ap=obj.get("/AP")
        if not ap:continue
        normal=(ap.get_object() if hasattr(ap,"get_object") else ap).get("/N");normal=normal.get_object() if hasattr(normal,"get_object") else normal
        if normal is not None:walk(ContentStream(normal,reader),normal.get("/Resources") or resources,scope=f"appearance:{getattr(ref,'idnum','direct')}")
    return result


def text_paint_inventory(pdf: Path) -> list[dict]:
    reader=PdfReader(str(pdf));return [item for number,page in enumerate(reader.pages,1) for item in text_paint_inventory_page(page,reader,number)]


def _rgb(hex_color: str) -> list[float]:
    return [round(int(hex_color[i:i+2],16)/255,6) for i in (1,3,5)]


def _contrast_rgb(foreground: list[float], background: list[float], alpha: float) -> float:
    effective=[max(0.,min(1.,foreground[i]*alpha+background[i]*(1-alpha))) for i in range(3)]
    def lum(values):
        linear=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in values]
        return .2126*linear[0]+.7152*linear[1]+.0722*linear[2]
    high,low=sorted((lum(effective),lum(background)),reverse=True)
    return (high+.05)/(low+.05)


def validate_source_map(mapped: dict, pages: list[dict]) -> list[dict]:
    """Validate the closed renderer geometry domain against the actual PDF."""
    if (not isinstance(mapped,dict) or set(mapped)!={"schema_version","page_coordinate_system","page_count","required_furniture","objects","visible_inventory","expected_internal_links","figure_registry","figure_references"}
            or mapped.get("schema_version")!="renderer-source-map.v1"
            or mapped.get("page_coordinate_system")!="pdf-points-top-left"
            or mapped.get("page_count")!=len(pages)):
        raise QaError("renderer source map contract/page domain failed")
    objects=mapped.get("objects"); required=mapped.get("required_furniture");links=mapped.get("expected_internal_links");figures=mapped.get("figure_registry");references=mapped.get("figure_references")
    if not isinstance(objects,list) or not isinstance(required,list) or any(not isinstance(x,str) for x in required):
        raise QaError("renderer source map objects/furniture must be closed arrays")
    if not isinstance(links,list) or any(not isinstance(x,dict) or set(x)!={"source_id","target_id"} or not all(isinstance(x[k],str) and x[k] for k in x) for x in links):
        raise QaError("renderer source map internal-link plan is invalid")
    figure_keys={"id","number","type","caption","description"};reference_keys={"source_id","target_id","number","reference_text"}
    if (not isinstance(figures,list) or any(not isinstance(x,dict) or set(x)!=figure_keys for x in figures)
            or [x.get("number") for x in figures]!=list(range(1,len(figures)+1))
            or any(x.get("type") not in {"mermaid","graphviz"} or not all(isinstance(x.get(k),str) and x[k].strip() for k in ("id","caption","description")) for x in figures)):
        raise QaError("renderer source map figure registry is invalid")
    by_figure={x["id"]:x for x in figures}
    if len(by_figure)!=len(figures) or not isinstance(references,list) or any(not isinstance(x,dict) or set(x)!=reference_keys for x in references):
        raise QaError("renderer source map figure references are invalid")
    for ref in references:
        target=by_figure.get(ref.get("target_id"))
        if (target is None or ref.get("number")!=target["number"] or ref.get("reference_text")!=f'Abbildung {target["number"]}'
                or {"source_id":ref.get("source_id"),"target_id":ref.get("target_id")} not in links):
            raise QaError("renderer source map semantic figure reference differs from registry/link plan")
    ids=[x.get("source_id") for x in objects if isinstance(x,dict)]
    if len(ids)!=len(objects) or len(ids)!=len(set(ids)) or len(required)!=len(set(required)):
        raise QaError("renderer source map source IDs/furniture must be unique")
    page_domain=set(range(1,len(pages)+1))
    if {x.get("page") for x in objects} != page_domain:
        raise QaError("renderer source map pages must equal the PDF page domain")
    by_id={x["source_id"]:x for x in objects}
    if not set(required).issubset(by_id) or any(by_id[x].get("type") not in {"header","footer"} for x in required):
        raise QaError("renderer source map is missing required furniture")
    if any(x["source_id"] not in by_id or x["target_id"] not in by_id for x in links):
        raise QaError("renderer source map internal-link plan has a missing or foreign object")
    if any(item["id"] not in by_id or by_id[item["id"]].get("type")!=item["type"] for item in figures):
        raise QaError("renderer source map figure registry has a missing or mistyped visible object")
    for item in objects:
        common={"source_id","type","page","bbox_pt","visible","evidence_kind"}
        evidence={"evidence_text"} if item.get("evidence_kind")=="text" else {"evidence_sha256"}
        if set(item)!=common|evidence or item["visible"] is not True or not isinstance(item["source_id"],str) or not item["source_id"]:
            raise QaError("renderer source map object schema failed")
        if item["evidence_kind"]=="text":
            if not isinstance(item["evidence_text"],str) or not item["evidence_text"].strip():raise QaError("text source-map evidence is empty")
        elif item["evidence_kind"] not in {"raster","vector"} or not re.fullmatch(r"[0-9a-f]{64}",str(item.get("evidence_sha256",""))):
            raise QaError("non-text source-map evidence hash is invalid")
        box=item["bbox_pt"]
        if not isinstance(box,list) or len(box)!=4 or any(not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) for v in box):
            raise QaError("renderer source map bbox is invalid")
        page=pages[item["page"]-1]
        if not (0<=box[0]<box[2]<=page["width"] and 0<=box[1]<box[3]<=page["height"]):
            raise QaError("renderer source map bbox lies outside its PDF page")
    inventory=mapped["visible_inventory"]
    if not isinstance(inventory,list) or not inventory or len({x.get("object_id") for x in inventory if isinstance(x,dict)})!=len(inventory):
        raise QaError("renderer source map visible inventory is empty or not unique")
    for item in inventory:
        if not isinstance(item,dict) or set(item)!={"object_id","kind","pdf_object_sha256","transform","bbox_pt","paint"} or item["kind"] not in {"text","raster","vector","form","inline-image","clip","shading","pattern","annotation","widget"}:
            raise QaError("renderer visible object schema failed")
        if not re.fullmatch(r"[0-9a-f]{64}",str(item["pdf_object_sha256"])) or len(item["transform"])!=6 or len(item["bbox_pt"])!=4 or not isinstance(item["paint"],dict):
            raise QaError("renderer visible object identity/hash/geometry failed")
        match=re.fullmatch(r"p([1-9][0-9]*)-(?:text|raster|vector|form|inline-image|clip|shading|pattern|annotation|widget)-[0-9]{4}",str(item["object_id"]))
        if not match or int(match.group(1))>len(pages):raise QaError("renderer visible object ID is outside the page/type domain")
        box=item["bbox_pt"];page_number=int(match.group(1));page=pages[page_number-1]
        if any(not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) for v in box) or not (0<=box[0]<box[2]<=page["width"] and 0<=box[1]<box[3]<=page["height"]):
            raise QaError("renderer visible object bbox is not finite, ordered, and page-bound")
    return objects


def verify_internal_links(pdf:Path,expected:list[dict],objects:list[dict])->list[dict]:
    """Bipartite-match every actual /Link to its source and exact /XYZ anchor."""
    reader=PdfReader(str(pdf));by_id={x["source_id"]:x for x in objects}
    page_refs={(page.indirect_reference.idnum,page.indirect_reference.generation):index for index,page in enumerate(reader.pages,1)}
    def resolve(destination:Any)->tuple[int,float,float]:
        if isinstance(destination,str):
            named=reader.named_destinations.get(destination)
            if named is None:raise QaError("PDF internal link has an unresolved named destination")
            target_page=reader.get_destination_page_number(named)+1
            kind=str(named.typ);left=named.left;top=named.top
        else:
            if not isinstance(destination,(list,tuple)) or len(destination)<5:
                raise QaError("PDF internal link destination is not a complete /XYZ array")
            target=destination[0];key=(target.idnum,target.generation) if hasattr(target,"idnum") else None
            if key not in page_refs:raise QaError("PDF internal link targets a foreign page object")
            target_page=page_refs[key];kind=str(destination[1]);left=destination[2];top=destination[3]
        if kind!="/XYZ" or left is None or top is None:
            raise QaError("PDF internal link destination must use explicit /XYZ coordinates")
        coordinates=(float(left),float(reader.pages[target_page-1].mediabox.height)-float(top))
        if any(not math.isfinite(value) for value in coordinates):raise QaError("PDF internal link /XYZ coordinate is not finite")
        return target_page,*coordinates
    actual=[]
    for source_page,page in enumerate(reader.pages,1):
        height=float(page.mediabox.height)
        for annotation_ref in page.get("/Annots",[]):
            annotation=annotation_ref.get_object()
            if str(annotation.get("/Subtype"))!="/Link":continue
            destination=annotation.get("/Dest")
            if destination is None:
                action=annotation.get("/A") or {};destination=action.get("/D") if str(action.get("/S"))=="/GoTo" else None
            if destination is None:continue
            target_page,target_x,target_y=resolve(destination)
            rect=[float(x) for x in annotation["/Rect"]];top_box=[rect[0],height-rect[3],rect[2],height-rect[1]]
            actual.append({"source_page":source_page,"target_page":target_page,"target_xyz_pt":[target_x,target_y],"bbox_pt":top_box})

    # A paragraph can own several inline links while exposing one coarse source
    # box.  Preserve inline order by ranking its annotation rectangles in visual
    # reading order; target identity then comes solely from the resolved page and
    # /XYZ anchor.  This detects swapped targets without inventing link boxes.
    expected_rank=[];seen:dict[str,int]={}
    for link in expected:
        expected_rank.append(seen.get(link["source_id"],0));seen[link["source_id"]]=expected_rank[-1]+1
    source_rank:dict[str,dict[int,int]]={}
    for source_id in seen:
        source=by_id[source_id]
        indexes=[index for index,item in enumerate(actual) if item["source_page"]==source["page"] and intersection_ratio(tuple(item["bbox_pt"]),tuple(source["bbox_pt"]))>.01]
        indexes.sort(key=lambda index:(actual[index]["bbox_pt"][1],actual[index]["bbox_pt"][0],actual[index]["bbox_pt"][3],actual[index]["bbox_pt"][2],index))
        source_rank[source_id]={index:rank for rank,index in enumerate(indexes)}
    edges=[]
    for expected_index,link in enumerate(expected):
        source=by_id[link["source_id"]];target=by_id[link["target_id"]];target_box=target["bbox_pt"]
        edges.append([index for index,item in enumerate(actual)
            if source_rank[link["source_id"]].get(index)==expected_rank[expected_index]
            and item["target_page"]==target["page"]
            and abs(item["target_xyz_pt"][0]-float(target_box[0]))<=1.0
            and abs(item["target_xyz_pt"][1]-float(target_box[1]))<=1.0])

    # Maximum bipartite matching makes duplicate expected links a true multiset:
    # every expected edge consumes exactly one actual annotation and vice versa.
    matched_actual:dict[int,int]={}
    def augment(expected_index:int,visited:set[int])->bool:
        for actual_index in edges[expected_index]:
            if actual_index in visited:continue
            visited.add(actual_index)
            if actual_index not in matched_actual or augment(matched_actual[actual_index],visited):
                matched_actual[actual_index]=expected_index;return True
        return False
    if any(not augment(index,set()) for index in range(len(expected))):
        raise QaError("PDF internal-link multiset has a missing, swapped, or wrong-destination annotation")
    if len(matched_actual)!=len(actual):raise QaError("PDF contains a foreign or duplicated renderer-owned internal link annotation")
    by_expected={expected_index:actual_index for actual_index,expected_index in matched_actual.items()}
    return [{**link,"source_page":by_id[link["source_id"]]["page"],"target_page":by_id[link["target_id"]]["page"],"target_xyz_pt":actual[by_expected[index]]["target_xyz_pt"]} for index,link in enumerate(expected)]


def bind_source_map_to_pdf(objects: list[dict], words: list[list[dict[str,str]]], pdf: Path, claimed_inventory: list[dict]) -> dict:
    """Bind claimed geometry to Poppler text boxes and the PDF image inventory."""
    measured=[]
    for item in objects:
        if item["evidence_kind"] != "text": continue
        actual=sequence_box(words[item["page"]-1],item["evidence_text"])
        if actual is None:raise QaError(f"source-map text is absent from PDF: {item['source_id']}")
        claimed=tuple(float(x) for x in item["bbox_pt"])
        # ReportLab flowable boxes may be wider/taller than ink, but the first
        # glyph, vertical centre and total area remain tightly bounded.
        actual_area=(actual[2]-actual[0])*(actual[3]-actual[1]);claimed_area=(claimed[2]-claimed[0])*(claimed[3]-claimed[1])
        container=item["type"] in {"ordered-list","unordered-list","table","image"}
        if (abs(claimed[0]-actual[0])>14
                or (not container and abs((claimed[1]+claimed[3])/2-(actual[1]+actual[3])/2)>20)
                or not (claimed[0]-3<=actual[0] and claimed[2]+3>=actual[2] and claimed[1]-14<=actual[1] and claimed[3]+14>=actual[3])
                or claimed_area/actual_area>(200 if container else 18)):
            raise QaError(f"source-map bbox differs from Poppler measurement: {item['source_id']}")
        measured.append({"source_id":item["source_id"],"page":item["page"],"bbox_pt":[round(x,3) for x in actual],"extractor":"pdftotext-tsv"})
    actual_inventory=closed_visible_inventory(pdf,words)
    if claimed_inventory!=actual_inventory:raise QaError("closed visible object inventory differs from PDF operators/XObjects/text")
    semantic_groups=[]
    for item in objects:
        page=item["page"];box=tuple(float(x) for x in item["bbox_pt"]);candidates=[]
        if item["evidence_kind"]=="text":
            indexes=sequence_word_indexes(words[page-1],item["evidence_text"])
            candidates=[x for x in actual_inventory if x["object_id"] in {f"p{page}-text-{index+1:04d}" for index in indexes}]
        else:
            accepted={"raster":{"raster","inline-image"},"vector":{"vector","form","shading","pattern"}}[item["evidence_kind"]]
            source_area=(box[2]-box[0])*(box[3]-box[1])
            pool=[x for x in actual_inventory if x["kind"] in accepted and x["object_id"].startswith(f"p{page}-")]
            exact=[x for x in pool if x["pdf_object_sha256"]==item["evidence_sha256"]]
            candidates=exact
            if not exact:
                candidates=[x for x in pool if intersection_ratio(box,tuple(x["bbox_pt"]))>0
                            and box[0]-2 <= (x["bbox_pt"][0]+x["bbox_pt"][2])/2 <= box[2]+2
                            and box[1]-2 <= (x["bbox_pt"][1]+x["bbox_pt"][3])/2 <= box[3]+2
                            and (x["bbox_pt"][2]-x["bbox_pt"][0])*(x["bbox_pt"][3]-x["bbox_pt"][1]) <= source_area*4]
                leaves=[x for x in candidates if x["kind"]!="form"]
                if leaves:candidates=leaves
        if not candidates:raise QaError(f"semantic source ID has no concrete visible PDF object group: {item['source_id']}")
        candidates=sorted(candidates,key=lambda x:x["object_id"]);group_box=[round(min(x["bbox_pt"][0] for x in candidates),3),round(min(x["bbox_pt"][1] for x in candidates),3),round(max(x["bbox_pt"][2] for x in candidates),3),round(max(x["bbox_pt"][3] for x in candidates),3)]
        if item["evidence_kind"]!="text" and not (box[0]-3<=group_box[0]<=group_box[2]<=box[2]+3 and box[1]-3<=group_box[1]<=group_box[3]<=box[3]+3):raise QaError(f"semantic PDF object group is not tight: {item['source_id']}")
        group_hash=hashlib.sha256(canonical_inventory_bytes(candidates)).hexdigest();source_hash=item.get("evidence_sha256") or hashlib.sha256(item["evidence_text"].encode()).hexdigest()
        semantic_groups.append({"source_id":item["source_id"],"type":item["type"],"source_evidence_sha256":source_hash,"object_ids":[x["object_id"] for x in candidates],"group_sha256":group_hash,"derivation_sha256":hashlib.sha256(bytes.fromhex(source_hash)+bytes.fromhex(group_hash)+canonical_inventory_bytes({"transform":[x["transform"] for x in candidates],"bbox_pt":group_box})).hexdigest(),"bbox_pt":group_box})
    return {"text_objects":measured,"semantic_groups":semantic_groups,"visible_inventory":actual_inventory}


def expected_mermaid_ids(ast: dict) -> list[str]:
    """Derive the closed Mermaid source-ID domain from the independent AST."""
    if not isinstance(ast,dict):
        raise QaError("renderer AST must be an object")
    result=[]
    def walk(value: Any) -> None:
        if isinstance(value,dict):
            if value.get("type")=="mermaid":
                source_id=value.get("id")
                if not isinstance(source_id,str) or not source_id:
                    raise QaError("every Mermaid AST node must have a non-empty source ID")
                result.append(source_id)
            for child in value.values():walk(child)
        elif isinstance(value,list):
            for child in value:walk(child)
    children=ast.get("children")
    if not isinstance(children,list):raise QaError("renderer AST children must be an array")
    walk(children)
    if len(result)!=len(set(result)):
        raise QaError("Mermaid AST source IDs must be unique")
    return result


def measure_mermaid_raster_quality(binding: dict, expected_ids: list[str], minimum_effective_dpi: float = 300.0) -> tuple[list[dict],list[dict]]:
    """Match the AST Mermaid domain exactly, then measure PDF raster DPI."""
    if minimum_effective_dpi != 300.0:
        raise QaError("Mermaid minimum effective DPI is pinned to 300")
    if (not isinstance(expected_ids,list) or any(not isinstance(x,str) or not x for x in expected_ids)
            or len(expected_ids)!=len(set(expected_ids))):
        raise QaError("expected Mermaid source-ID domain must be a unique string array")
    inventory_items=binding.get("visible_inventory",[]);groups=binding.get("semantic_groups",[])
    if not isinstance(inventory_items,list) or not isinstance(groups,list):
        raise QaError("Mermaid binding inventory/groups must be arrays")
    inventory_ids=[item.get("object_id") for item in inventory_items if isinstance(item,dict)]
    if len(inventory_ids)!=len(inventory_items) or len(inventory_ids)!=len(set(inventory_ids)) or any(not isinstance(x,str) or not x for x in inventory_ids):
        raise QaError("Mermaid visible inventory IDs must be unique non-empty strings")
    inventory={item["object_id"]:item for item in inventory_items}
    if any(not isinstance(group,dict) or not isinstance(group.get("source_id"),str) or not group["source_id"] for group in groups):
        raise QaError("Mermaid semantic groups must have non-empty source IDs")
    expected=set(expected_ids)
    mermaid_groups=[group for group in groups if group.get("type")=="mermaid"]
    if {group["source_id"] for group in mermaid_groups} != expected:
        raise QaError("Mermaid semantic-group domain differs from the renderer AST")
    by_expected={source_id:[group for group in groups if group["source_id"]==source_id] for source_id in expected_ids}
    if any(len(matches)!=1 or matches[0].get("type")!="mermaid" for matches in by_expected.values()):
        raise QaError("every AST Mermaid ID must have exactly one type-preserving semantic group")
    owners:dict[str,list[str]]={}
    for group in groups:
        object_ids=group.get("object_ids")
        if not isinstance(object_ids,list) or any(not isinstance(x,str) or not x for x in object_ids):
            raise QaError(f"semantic group has an invalid object binding: {group['source_id']}")
        for object_id in object_ids:owners.setdefault(object_id,[]).append(group["source_id"])
    checks=[];findings=[]
    for source_id in expected_ids:
        group=by_expected[source_id][0];object_ids=group["object_ids"]
        if len(object_ids)!=1:
            raise QaError(f"Mermaid source must bind exactly one raster XObject: {group.get('source_id')}")
        object_id=object_ids[0];raster=inventory.get(object_id)
        if raster is None or raster.get("kind")!="raster" or owners.get(object_id)!=[source_id]:
            raise QaError(f"Mermaid raster XObject binding is missing, foreign, or ambiguous: {source_id}")
        paint=raster.get("paint",{});box=raster.get("bbox_pt",[]);group_box=group.get("bbox_pt",[])
        if (len(box)!=4 or not all(isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) for value in box)
                or box[2]<=box[0] or box[3]<=box[1]
                or len(group_box)!=4 or not all(isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) for value in group_box)
                or group_box[2]<=group_box[0] or group_box[3]<=group_box[1]
                or any(abs(float(group_box[index])-float(box[index]))>0.001 for index in range(4))
                or not isinstance(paint,dict)
                or not isinstance(paint.get("pixel_width"),int) or paint["pixel_width"]<=0
                or not isinstance(paint.get("pixel_height"),int) or paint["pixel_height"]<=0):
            raise QaError(f"Mermaid raster geometry is incomplete: {group.get('source_id')}")
        width_inches=(box[2]-box[0])/72.;height_inches=(box[3]-box[1])/72.
        dpi_x=paint["pixel_width"]/width_inches;dpi_y=paint["pixel_height"]/height_inches
        record={"source_id":group["source_id"],"object_id":raster["object_id"],"pixel_width":paint["pixel_width"],"pixel_height":paint["pixel_height"],"paint_width_pt":round(box[2]-box[0],3),"paint_height_pt":round(box[3]-box[1],3),"effective_dpi_x":round(dpi_x,3),"effective_dpi_y":round(dpi_y,3),"minimum_effective_dpi":minimum_effective_dpi}
        checks.append(record)
        if min(dpi_x,dpi_y)+1e-9<minimum_effective_dpi:findings.append(record)
    return checks,findings


def measure_mermaid_vector_quality(binding: dict, expected_ids: list[str]) -> list[dict]:
    """Bind every AST Mermaid figure to primitive vectors and no raster/Form."""
    if (not isinstance(expected_ids,list) or any(not isinstance(value,str) or not value for value in expected_ids)
            or len(expected_ids)!=len(set(expected_ids))):raise QaError("expected Mermaid source-ID domain must be a unique string array")
    inventory_items=binding.get("visible_inventory",[]);groups=binding.get("semantic_groups",[])
    if not isinstance(inventory_items,list) or not isinstance(groups,list):raise QaError("Mermaid binding inventory/groups must be arrays")
    inventory={item.get("object_id"):item for item in inventory_items if isinstance(item,dict)}
    if len(inventory)!=len(inventory_items) or None in inventory:raise QaError("Mermaid visible inventory IDs must be unique")
    mermaid=[group for group in groups if isinstance(group,dict) and group.get("type")=="mermaid"]
    if {group.get("source_id") for group in mermaid}!=set(expected_ids) or len(mermaid)!=len(expected_ids):raise QaError("Mermaid semantic-group domain differs from the renderer AST")
    checks=[]
    for source_id in expected_ids:
        group=next(group for group in mermaid if group["source_id"]==source_id);ids=group.get("object_ids")
        if not isinstance(ids,list) or not ids or len(ids)!=len(set(ids)):raise QaError(f"Mermaid vector binding is empty or ambiguous: {source_id}")
        objects=[]
        for object_id in ids:
            item=inventory.get(object_id)
            if item is None:raise QaError(f"Mermaid vector object is absent from the inventory: {source_id}")
            objects.append(item)
        kinds={item.get("kind") for item in objects}
        if kinds & {"raster","inline-image","form","shading","pattern"}:raise QaError(f"Mermaid figure binds a non-primitive or raster object: {source_id}")
        if "vector" not in kinds:raise QaError(f"Mermaid figure lacks primitive vector geometry: {source_id}")
        checks.append({"source_id":source_id,"object_count":len(objects),"kinds":sorted(kinds),"image_xobjects":0,"status":"pass"})
    return checks


def inspect(pdf: Path, png_dir: Path, output: Path, ast_path: Path, brand_path: Path, dpi: int = 144, renderer_source_map: Path | None = None) -> dict:
    if dpi != 144:
        raise QaError("visual QA DPI is pinned to 144")
    pdftocairo = tool("pdftocairo", ["-v"])
    pdftotext = tool("pdftotext", ["-v"])
    pdfinfo = tool("pdfinfo", ["-v"])
    pdffonts = tool("pdffonts", ["-v"])
    png_dir.mkdir(parents=True, exist_ok=False)
    subprocess.run([pdftocairo["path"], "-png", "-r", str(dpi), str(pdf), str(png_dir / "page")], check=True, capture_output=True)
    tsv = subprocess.run([pdftotext["path"], "-tsv", str(pdf), "-"], check=True, capture_output=True, text=True).stdout
    rows = list(csv.DictReader(io.StringIO(tsv), delimiter="\t"))
    page_rows = [row for row in rows if row["text"] == "###PAGE###"]
    pages = [{"number":int(row["page_num"]),"width":float(row["width"]),"height":float(row["height"])} for row in page_rows]
    words = [[row for row in rows if row["level"] == "5" and int(row["page_num"]) == page["number"]] for page in pages]
    if not pages or any(not group for group in words):
        raise QaError("every PDF page must contain selectable text")
    ast = json.loads(ast_path.read_text(encoding="utf-8"));mermaid_ids=expected_mermaid_ids(ast)
    brand = json.loads(brand_path.read_text(encoding="utf-8"));colors = brand["pdf"]["tokens"]["color_roles"]
    paint=text_paint_inventory(pdf);body_allowed=[_rgb(colors[x]) for x in ("text","primary","muted")]
    invalid_paint=[]
    for item in paint:
        allowed=list(body_allowed)
        # Page-one white is a supported title role for both multi-page covers
        # and the one-pager hero. Its safety is established below against the
        # actual Poppler-composed background for every word, not by this paint
        # vocabulary check.
        if item["page"]==1:allowed.append([1.,1.,1.])
        if item["rgb"] not in allowed:invalid_paint.append(item)
    geometry = []; safe_findings=[]; overlap_findings=[]; contrast_findings=[]; invisible_findings=[]
    actual_inventory=closed_visible_inventory(pdf,words)
    text_inventory={page:[x for x in actual_inventory if x["kind"]=="text" and x["object_id"].startswith(f"p{page}-")] for page in range(1,len(pages)+1)}
    unsupported_visible=[x["object_id"] for x in actual_inventory if x["kind"]=="widget"]
    instrumented=[];renderer_contract=None
    if renderer_source_map is not None:
        renderer_contract=json.loads(renderer_source_map.read_text(encoding="utf-8"))
        instrumented=validate_source_map(renderer_contract,pages)
        independent_binding=bind_source_map_to_pdf(instrumented,words,pdf,renderer_contract["visible_inventory"])
        independent_binding["internal_links"]=verify_internal_links(pdf,renderer_contract["expected_internal_links"],instrumented)
    else: independent_binding={"text_objects":[],"semantic_groups":[],"visible_inventory":[]}
    mermaid_vector_quality=measure_mermaid_vector_quality(independent_binding,mermaid_ids)
    raster_dpi_findings=[]
    if not instrumented:
        for page_number,page in enumerate(PdfReader(str(pdf)).pages,1):
            contents=page.get_contents();stream=contents.get_data() if contents is not None else b"";seen_ops={}
            for match in re.finditer(rb"BT\s+1\s+0\s+0\s+1\s+([-0-9.]+)\s+([-0-9.]+)\s+Tm\s+\(([^()]*)\)\s+Tj",stream):
                key=(match.group(1),match.group(2),match.group(3));seen_ops[key]=seen_ops.get(key,0)+1
            for (x,y,text),count in seen_ops.items():
                if count>1:overlap_findings.append({"page":page_number,"first":"pdf-object","second":"pdf-object-duplicate","text":text.decode("latin-1","replace"),"draw_count":count,"origin_pt":[float(x),float(y)]})
    all_text = []
    pngs = sorted(png_dir.glob("page-*.png"))
    if len(pngs) != len(pages): raise QaError("Poppler page raster count differs from PDF page count")
    safe_pt=10/25.4*72
    for index, (page, group, png_path) in enumerate(zip(pages, words, pngs), 1):
        width, height = page["width"], page["height"]
        overflow = []
        boxes=[]; image=Image.open(png_path).convert("RGB");sx=image.width/width;sy=image.height/height
        for word_index,word in enumerate(group):
            left,top,w,h=(float(word[k]) for k in ("left","top","width","height"));box=(left,top,left+w,top+h)
            boxes.append((word_index,word["text"],box))
            if box[0] < safe_pt or box[1] < safe_pt or box[2] > width-safe_pt or box[3] > height-safe_pt or box[0] >= box[2] or box[1] >= box[3]:
                overflow.append(word["text"])
                safe_findings.append({"page":index,"text":word["text"],"bbox_pt":[round(v,3) for v in box]})
            crop=image.crop((max(0,int(box[0]*sx)-1),max(0,int(box[1]*sy)-1),min(image.width,int(box[2]*sx)+2),min(image.height,int(box[3]*sy)+2)))
            pixel_ratio=pixel_contrast(crop)
            visible=text_inventory[index][word_index]["paint"] if word_index<len(text_inventory[index]) else None
            if visible is None:contrast_findings.append({"page":index,"text":word["text"],"reason":"word has no bound text-paint operator"})
            else:
                mode=int(visible["render_mode"]);stroke=mode in {1,2,5,6};foreground=visible["stroke_rgb"] if stroke else visible["rgb"];alpha=float(visible["stroke_alpha"] if stroke else visible["fill_alpha"])
                backgrounds=measured_backgrounds(image,box,sx,sy,foreground,alpha)
                effective_ratio=min((_rgb_ratio(foreground,bg,alpha) for bg in backgrounds),default=0.0);large=float(visible["font_size_pt"])>=18 or (float(visible["font_size_pt"])>=14 and bool(visible["bold"]));threshold=3.0 if large else 4.5
                if mode in {3,7} or alpha<=0:invisible_findings.append({"page":index,"text":word["text"],"render_mode":mode,"alpha":alpha,"bbox_pt":[round(v,3) for v in box]})
                if effective_ratio+1e-9<threshold:contrast_findings.append({"page":index,"text":word["text"],"effective_ratio":round(effective_ratio,6),"background_rgb":[list(x) for x in backgrounds],"background_source":"poppler-144dpi-final-recursive-paint-stack","pixel_ratio":round(pixel_ratio,6),"threshold":threshold,"font_size_pt":visible["font_size_pt"],"bold":visible["bold"],"alpha":alpha,"bbox_pt":[round(v,3) for v in box]})
            if pixel_ratio<1.2: invisible_findings.append({"page":index,"text":word["text"],"pixel_ratio":round(pixel_ratio,6),"bbox_pt":[round(v,3) for v in box]})
        overlap_objects=[x for x in instrumented if x.get("page")==index and x.get("visible") is True]
        if not overlap_objects:overlap_objects=[{"source_id":f"word-{i}","bbox_pt":box} for i,_text,box in boxes]
        for ai,a in enumerate(overlap_objects):
            for b in overlap_objects[ai+1:]:
                ratio=intersection_ratio(tuple(a["bbox_pt"]),tuple(b["bbox_pt"]))
                if ratio>=.35: overlap_findings.append({"page":index,"first":a["source_id"],"second":b["source_id"],"ratio":round(ratio,3)})
        text = " ".join(w["text"] for w in group)
        all_text.append(text)
        geometry.append({"page": index, "width_pt": width, "height_pt": height, "overflow_words": overflow})
    source_text = " ".join(all_text)
    glyph_findings = [token for token in ("\ufffd", ".notdef", "cid:0") if token in source_text]
    orphan = []; source_map=list(instrumented)
    headings = [node["text"] for node in ast["children"] if node["type"] == "heading" and node["level"] > 1]
    top_nodes=[n for n in ast["children"] if n["type"]!="blank"]
    for position,node in enumerate(top_nodes):
        label=node.get("text") or node.get("title") or node.get("description")
        node_id=node.get("id") or f"node-{position+1}"
        if not label:continue
        matches=[{"page":x["page"],"bbox_pt":x["bbox_pt"]} for x in instrumented if x["source_id"]==node_id]
        if not instrumented:
            for page,group in zip(pages,words):
                box=sequence_box(group,label)
                if box:matches.append({"page":page["number"],"bbox_pt":[round(v,3) for v in box]})
        if len(matches)!=1: orphan.append(f"unmapped:{node_id}")
        elif not instrumented:source_map.append({"source_id":node_id,"type":node["type"],**matches[0]})
    mapped={x["source_id"]:x for x in source_map}
    for node in top_nodes:
        if node["type"]!="heading" or node["level"]<=1:continue
        heading=node["text"]
        located = False
        entry=mapped.get(node.get("id"))
        if entry:
            idx=top_nodes.index(node);following=next((x for x in top_nodes[idx+1:] if x["type"] not in {"page-break"}),None)
            following_index=top_nodes.index(following) if following else -1
            next_entry=mapped.get((following or {}).get("id") or f"node-{following_index+1}") if following else None
            if entry["bbox_pt"][3]>pages[entry["page"]-1]["height"]-safe_pt or (next_entry and next_entry["page"]!=entry["page"]):orphan.append(heading)
            located=True
        if not located:
            orphan.append(heading)
    ratios = {"declared_text_on_background": contrast(colors["text"], colors["background"]), "declared_primary_on_background": contrast(colors["primary"], colors["background"])}
    for role in ("text","primary","muted"):
        for background in ("background","surface"):
            if contrast(colors[role],colors[background])<4.5:invalid_paint.append({"declared_role":role,"background":background,"ratio":round(contrast(colors[role],colors[background]),3)})
    if contrast("#FFFFFF",colors["primary"])<4.5:invalid_paint.append({"declared_role":"title","background":"primary"})
    font_output=subprocess.run([pdffonts["path"],str(pdf)],check=True,capture_output=True,text=True).stdout
    font_lines=[line.split() for line in font_output.splitlines()[2:] if line.strip()];font_records={fields[0]:fields for fields in font_lines if len(fields)>=7};used_fonts={str(x["font"]).lstrip("/") for x in paint if x.get("text")}
    font_findings=[] if used_fonts and all(name in font_records and font_records[name][3:6]==["yes","yes","yes"] for name in used_fonts) else [{"used_fonts":sorted(used_fonts),"font_inventory":font_output,"requirement":"every visible font embedded/subset/Unicode"}]
    findings = {
        "safe_area": safe_findings,
        "overflow": [x for page in geometry for x in page["overflow_words"]],
        "overlap": overlap_findings,
        "orphan_headings": orphan,
        "glyph_replacement": glyph_findings+font_findings+invisible_findings+unsupported_visible,
        "contrast": contrast_findings+invalid_paint,
        "raster_dpi": raster_dpi_findings,
    }
    result = {
        "schema_version": "visual-qa.v1", "status": "pass" if not any(findings.values()) else "fail",
        "pdf_sha256": sha(pdf), "dpi": dpi, "safe_area_mm": 10, "tools": {"pdftocairo": pdftocairo, "pdftotext": pdftotext, "pdfinfo": pdfinfo, "pdffonts":pdffonts},
        "pages": [{"index": i, "png_path": p.relative_to(output.parent).as_posix(), "png_sha256": sha(p)} for i, p in enumerate(pngs, 1)],
        "geometry": geometry, "source_map":source_map,
        # Preserve the complete closed contract.  The ID lookup below is only
        # an internal convenience and cannot replace its schema/page domain.
        "source_map_contract": renderer_contract,
        "source_map_independent_binding":independent_binding,
        "diagram_vector_quality":{"mermaid":mermaid_vector_quality},
        "text_paint_operators":paint,
        "contrast_ratios": ratios, "findings": findings,
        "derivation": "Poppler text bboxes and final 144-dpi recursive compositor pixels; ordered page/Form/appearance/vector/raster/shading/pattern/alpha/soft-mask/blend/clip background clusters per word; Mermaid source groups require primitive vector geometry and reject raster/Form/image ownership; extracted diagram labels are checked separately from their narrower text boxes; 10-mm safe area; measured source binding and WCAG small/large thresholds",
    }
    output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if result["status"] != "pass":
        raise QaError(f"visual QA failed: {findings}")
    return result


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pdf", type=Path, required=True); p.add_argument("--png-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True); p.add_argument("--ast", type=Path, required=True)
    p.add_argument("--brand", type=Path, required=True); p.add_argument("--dpi", type=int, default=144);p.add_argument("--renderer-source-map",type=Path)
    a = p.parse_args()
    try:
        inspect(a.pdf, a.png_dir, a.output, a.ast, a.brand, a.dpi,a.renderer_source_map); print(f"Visual QA passed: {a.output}"); return 0
    except (QaError, OSError, ValueError, subprocess.SubprocessError, ET.ParseError) as exc:
        print(f"Visual QA failed: {exc}", file=__import__('sys').stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
