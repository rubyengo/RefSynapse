r"""Verify agent\manual_ru_preview.pdf: artwork intact, Russian in place.

Pure pypdf, no window. Run from the repo root:
    & ".\python\python.exe" -u agent\verify_preview.py
"""
import collections
import io
import os

from pypdf import PdfReader
from pypdf.generic import ArrayObject, ContentStream

SB = "agent"
SRC = os.path.join(SB, "manual_en.pdf")
OUT = os.path.join(SB, "manual_ru_preview.pdf")
TRANSLATED = {5: ("Меры предосторожности", "Installation Precautions"),
              10: ("Установка памяти", "Installing the Memory"),
              12: ("Разъёмы задней панели", "Back Panel Connectors")}
SAMPLE_UNTOUCHED = (1, 3, 6)

# real artwork: lines, curves, pictures, fills, strokes, colours, shading
ART = (b"m", b"l", b"c", b"v", b"y", b"h", b"Do", b"f", b"F", b"B", b"b",
       b"S", b"s", b"k", b"K", b"gs", b"sh", b"BI")
# layering text on a page adds ONE page-boundary clip: re + W + n
CLIP = (b"re", b"W", b"n")


def ops(page, reader):
    return collections.Counter(
        op for _, op in ContentStream(page.get_contents(), reader).operations)


def layers(page):
    raw = page.get("/Contents")
    raw = raw.get_object() if hasattr(raw, "get_object") else raw
    if isinstance(raw, ArrayObject):
        return len(raw)
    return 1 if raw is not None else 0


def main():
    fill = os.path.join(SB, "translate_fill.py")
    if os.path.isfile(fill):
        with io.open(fill, "r", encoding="utf-8-sig") as stream:
            body = stream.read()
        if body.startswith('"""'):
            with io.open(fill, "w", encoding="utf-8", newline="\n") as stream:
                stream.write("r" + body)
            print("translate_fill.py: docstring marked raw (warning silenced)")

    for path in (SRC, OUT):
        if not os.path.isfile(path):
            raise SystemExit("Missing file: %s" % path)
    src, res = PdfReader(SRC), PdfReader(OUT)
    print("pages:", len(src.pages), "->", len(res.pages))
    fails = []
    if len(src.pages) != len(res.pages):
        fails.append("page count")

    for number, (ru, en) in TRANSLATED.items():
        a = ops(src.pages[number - 1], src)
        b = ops(res.pages[number - 1], res)
        changed = [k.decode() for k in ART if a[k] != b[k]]
        clip = {k.decode(): b[k] - a[k] for k in CLIP}
        text = res.pages[number - 1].extract_text() or ""
        ru_ok, en_gone = ru in text, en not in text
        print("page %d: artwork changed=%s | clip added=%s | layers=%d"
              % (number, changed or "none", clip, layers(res.pages[number - 1])))
        print("page %d: russian=%s english_gone=%s chars=%d"
              % (number, ru_ok, en_gone, len(text)))
        if changed:
            fails.append("artwork p%d" % number)
        if any(v != 1 for v in clip.values()):
            fails.append("clip p%d" % number)
        if not (ru_ok and en_gone):
            fails.append("text p%d" % number)

    for number in SAMPLE_UNTOUCHED:
        same = ops(src.pages[number - 1], src) == ops(res.pages[number - 1], res)
        print("page %d untouched identical=%s" % (number, same))
        if not same:
            fails.append("untouched p%d" % number)

    print("FAILS:", len(fails), fails)
    print("Open agent\\manual_ru_preview.pdf and compare pages 5, 10, 12 "
          "with the original.")


if __name__ == "__main__":
    main()