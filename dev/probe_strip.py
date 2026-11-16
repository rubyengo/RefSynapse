import os, collections
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ContentStream
SRC, OUT = "agent/manual_en.pdf", "agent/probe_strip.pdf"
if not os.path.exists(SRC):
    raise SystemExit("Put the manual into agent/manual_en.pdf first")
r, w = PdfReader(SRC), PdfWriter(clone_from=SRC)
show = {b"Tj", b"TJ", b"'", b'"'}
for n in (10, 12):
    p = w.pages[n-1]
    was = len(r.pages[n-1].extract_text() or "")
    cs = ContentStream(p.get_contents(), w)
    a = collections.Counter(op for _, op in cs.operations)
    cs.operations = [(o, op) for o, op in cs.operations if op not in show]
    b = collections.Counter(op for _, op in cs.operations)
    p.replace_contents(cs)
    now = len(p.extract_text() or "")
    ok = all(a[k] == b[k] for k in (b"m", b"l", b"c", b"re", b"Do", b"f", b"S", b"k", b"gs"))
    print("page", n, "text", was, "->", now, "drawings kept:", ok)
w.write(OUT)
print("saved", OUT, round(os.path.getsize(OUT)/1048576, 2), "MB", "bookmarks", len(w.outline))
