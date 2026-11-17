import os, re, sys
sys.path.insert(0, os.getcwd())
from pypdf import PdfReader, PdfWriter
from refsynapse.ai import board_pdf_translate_tools as T

path = os.path.join("agent", "mk-740_742_manual_no_ru.pdf")
reader = PdfReader(path)
print("PAGES", len(reader.pages))
for i, page in enumerate(reader.pages):
    print("=" * 60)
    print("PAGE", i + 1, "size", float(page.mediabox.width), float(page.mediabox.height))
    try:
        spans = T._page_spans(page)
    except Exception as ex:
        print("  spans ERROR:", ex)
        spans = []
    print("  spans:", len(spans), "forms_have_text:", T._forms_have_text(page))
    try:
        blocks = T._group_blocks(spans, float(page.mediabox.width))
        locked, ready = T._classify(blocks)
        print("  blocks:", len(blocks), "locked:", len(locked), "ready:", len(ready))
    except Exception as ex:
        print("  group ERROR:", ex)
        locked, ready = [], []
    w = PdfWriter(clone_from=reader)
    tb = [dict(b, id="t%d" % n) for n, b in enumerate(ready, 1)]
    kb = [dict(b, id="k%d" % n) for n, b in enumerate(locked, 1)]
    ok = T._surgical_strip(w, i, tb, kb)
    after = re.sub(r"\s", "", w.pages[i].extract_text() or "")
    print("  surgical_strip ok:", ok, "after_chars:", len(after))
    print("  after_sample:", after[:160])
    for b in ready[:6]:
        print("   block", repr(b["text"][:50]), "color", T._block_color(b, T._text_colors(page)))