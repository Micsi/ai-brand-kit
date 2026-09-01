#!/usr/bin/env python3
"""AI-Kennzeichnung in ein Bild einbrennen, zuschneiden und fuers Web verkleinern.

Brennt unten rechts eine kleine Markierung ein, die Download und Weitergabe
ueberlebt. Farben und Text sind Parameter, damit die Markierung zum jeweiligen
Brand Kit passt.

Beispiel:
    python ai_label.py roh.png -o hero.jpg --width 1120 --aspect 16:9

Mit eigenen Markenfarben:
    python ai_label.py roh.png -o hero.jpg \\
        --bg "#111111" --fg "#FFFFFF" --line "#737373" --accent "#FFFFFF"

Wichtig: Das sichtbare Zeichen erfuellt die Offenlegung gegenueber Menschen.
Die maschinenlesbare Markierung ist etwas anderes und geht beim Neucodieren
verloren. Mit --iptc wird sie per exiftool wieder gesetzt, sofern exiftool
installiert ist.

Braucht Pillow:  pip install Pillow
"""

import argparse
import os
import shutil
import subprocess
import sys

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow fehlt. Installieren mit:  pip install Pillow")

# IPTC-Standardwert fuer vollstaendig KI-erzeugte Bilder
DIGITAL_SOURCE_TYPE = (
    "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"
)

# Schriftkandidaten fuer die Markierung, erste vorhandene gewinnt.
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\consolab.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
]


def hex_to_rgb(value):
    v = value.lstrip("#")
    if len(v) != 6:
        raise argparse.ArgumentTypeError(f"Farbe muss #RRGGBB sein, war: {value}")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def parse_aspect(value):
    if value is None:
        return None
    try:
        w, h = value.replace(":", "/").split("/")
        return float(w) / float(h)
    except (ValueError, ZeroDivisionError):
        raise argparse.ArgumentTypeError(f"Seitenverhaeltnis wie 16:9, war: {value}")


def find_font(explicit):
    if explicit:
        return explicit
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def crop_cover(im, ratio, anchor):
    """Mittig oder an einer Kante auf das Zielverhaeltnis zuschneiden."""
    w, h = im.size
    if abs(w / h - ratio) < 0.001:
        return im
    if w / h > ratio:
        nw = int(round(h * ratio))
        left = (w - nw) // 2
        return im.crop((left, 0, left + nw, h))
    nh = int(round(w / ratio))
    # Die Markierung sitzt unten rechts. Wer oben anschneidet, behaelt sie.
    top = (h - nh) if anchor == "bottom" else (0 if anchor == "top" else (h - nh) // 2)
    return im.crop((0, top, w, top + nh))


def stamp(im, text, colors, scale):
    """Markierung unten rechts einbrennen."""
    draw = ImageDraw.Draw(im, "RGBA")
    w, h = im.size

    pad = int(round(28 * scale))
    box_h = int(round(62 * scale))
    bar_w = max(2, int(round(4 * scale)))
    gap = int(round(14 * scale))
    inner = int(round(20 * scale))
    text_size = max(9, int(round(34 * scale)))

    font_path = find_font(colors["font"])
    font = (ImageFont.truetype(font_path, text_size) if font_path
            else ImageFont.load_default())

    text_w = draw.textlength(text, font=font)
    box_w = int(inner + bar_w + gap + text_w + inner)

    x1, y1 = w - pad, h - pad
    x0, y0 = x1 - box_w, y1 - box_h

    draw.rounded_rectangle(
        [x0, y0, x1, y1],
        radius=max(2, int(round(8 * scale))),
        fill=colors["bg"] + (235,),
        outline=colors["line"] + (255,),
        width=1,
    )
    bx = x0 + inner
    draw.rectangle(
        [bx, y0 + int(round(16 * scale)), bx + bar_w - 1, y1 - int(round(16 * scale))],
        fill=colors["accent"],
    )
    draw.text(
        (bx + bar_w + gap, (y0 + y1) / 2),
        text, font=font, fill=colors["fg"], anchor="lm",
    )
    return im


def save_under_limit(im, path, max_kb, quality):
    """Speichern und die Qualitaet senken, bis die Groesse passt."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".png" or max_kb is None:
        im.save(path, optimize=True)
        return os.path.getsize(path) / 1024, None
    for q in range(quality, 29, -6):
        im.save(path, "JPEG", quality=q, optimize=True, progressive=True)
        kb = os.path.getsize(path) / 1024
        if kb <= max_kb:
            return kb, q
    return os.path.getsize(path) / 1024, 30


def write_iptc(path):
    """Maschinenlesbare Markierung nachtragen, wenn exiftool da ist."""
    if not shutil.which("exiftool"):
        return False, "exiftool nicht gefunden"
    try:
        subprocess.run(
            ["exiftool", "-overwrite_original",
             f"-XMP-iptcExt:DigitalSourceType={DIGITAL_SOURCE_TYPE}", path],
            check=True, capture_output=True,
        )
        return True, None
    except subprocess.CalledProcessError as exc:
        return False, exc.stderr.decode(errors="replace").strip()


def main():
    p = argparse.ArgumentParser(
        description="AI-Kennzeichnung einbrennen, zuschneiden, verkleinern.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("input", help="Quellbild")
    p.add_argument("-o", "--output", required=True, help="Zieldatei (.jpg oder .png)")
    p.add_argument("--width", type=int, help="Zielbreite in Pixeln")
    p.add_argument("--aspect", type=parse_aspect, help="Seitenverhaeltnis, etwa 16:9")
    p.add_argument("--anchor", choices=["center", "top", "bottom"], default="bottom",
                   help="Wo beim vertikalen Anschnitt verankert wird. "
                        "Standard bottom, damit die Markierung erhalten bleibt.")
    p.add_argument("--text", default="AI", help="Text der Markierung (Standard: AI)")
    p.add_argument("--bg", type=hex_to_rgb, default="#111111", help="Kastenfarbe")
    p.add_argument("--fg", type=hex_to_rgb, default="#FFFFFF", help="Textfarbe")
    p.add_argument("--line", type=hex_to_rgb, default="#737373", help="Rahmenfarbe")
    p.add_argument("--accent", type=hex_to_rgb, default="#FFFFFF", help="Akzentbalken")
    p.add_argument("--font", help="Pfad zu einer TTF-Datei fuer die Markierung")
    p.add_argument("--scale", type=float, default=1.0,
                   help="Groesse der Markierung, bezogen auf 1120 px Bildbreite")
    p.add_argument("--max-kb", type=float, default=70,
                   help="Obergrenze in KB fuer JPEG (Standard 70, 0 schaltet ab)")
    p.add_argument("--quality", type=int, default=78, help="JPEG-Startqualitaet")
    p.add_argument("--iptc", action="store_true",
                   help="Maschinenlesbare Markierung per exiftool nachtragen")
    p.add_argument("--no-stamp", action="store_true",
                   help="Nur zuschneiden und verkleinern, ohne Markierung")
    args = p.parse_args()

    if not os.path.exists(args.input):
        sys.exit(f"Datei nicht gefunden: {args.input}")

    im = Image.open(args.input).convert("RGB")
    src = im.size

    if args.aspect:
        im = crop_cover(im, args.aspect, args.anchor)
    if args.width and args.width != im.size[0]:
        ratio = im.size[1] / im.size[0]
        im = im.resize((args.width, int(round(args.width * ratio))), Image.LANCZOS)

    if not args.no_stamp:
        # Markierung mit der Bildbreite skalieren, damit sie ueberall gleich wirkt.
        scale = args.scale * (im.size[0] / 1120)
        colors = {"bg": args.bg, "fg": args.fg, "line": args.line,
                  "accent": args.accent, "font": args.font}
        im = stamp(im, args.text, colors, scale)

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    max_kb = None if not args.max_kb else args.max_kb
    kb, q = save_under_limit(im, args.output, max_kb, args.quality)

    note = f"  q={q}" if q else ""
    print(f"{args.output}  {src[0]}x{src[1]} -> {im.size[0]}x{im.size[1]}"
          f"  {kb:.1f} KB{note}")

    if max_kb and kb > max_kb:
        print(f"  Hinweis: ueber der Grenze von {max_kb:.0f} KB. "
              f"Kleinere --width waehlen.")

    if args.iptc:
        ok, err = write_iptc(args.output)
        print("  IPTC DigitalSourceType gesetzt" if ok
              else f"  IPTC nicht gesetzt: {err}")
    elif not args.no_stamp:
        print("  Nur sichtbar gekennzeichnet. Fuer die maschinenlesbare "
              "Markierung --iptc ergaenzen.")


if __name__ == "__main__":
    main()
