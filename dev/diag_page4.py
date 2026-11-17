# dev\diag_page4.py - trace page 4 step by step + page 2 overflow detail.
import io, json, os, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.getcwd())
import pypdf
from pypdf import PdfWriter
from pypdf.generic import ContentStream
from refsynapse.ai import board_pdf_translate_tools as T

JOB = os.path.join("agent", "translate", "mk-740_742_manual_no_ru", "job.json")
job = json.load(io.open(JOB, encoding="utf-8"))
lines = []
def say(s):
    lines.append(s); print(s)
say("pypdf %s" % pypdf.__version__)

def needles_of(blocks):
    out = []
    for b in blocks:
        for n in T._block_needles(b, []):
            out.append((b["id"], n))
    return out

def remaining(page, nd):
    txt = T._norm(page.extract_text() or "")
    return txt, sorted({bid for bid, n in nd if n in txt})

# ---------------- page 4 pipeline ----------------
key, idx = "4", 3
blocks = job["blocks"][key]; locked = job["locked"].get(key, [])
trans = job["translations"].get(key, {})
changed = [b for b in blocks
           if T._norm(trans.get(b["id"], b["text"])) != T._norm(b["text"])]
nd = needles_of(changed)
say("PAGE4 changed=%d locked=%d needles=%d" % (len(changed), len(locked), len(nd)))

surg = PdfWriter(clone_from=T._reader(job["source"]))
txt0, rem0 = remaining(surg.pages[idx], nd)
say("  original: chars=%d surviving=%s" % (len(txt0), rem0))

ok = T._surgical_strip(surg, idx, changed, locked)
txt1, rem1 = remaining(surg.pages[idx], nd)
say("  surgical ok=%s chars=%d surviving=%s" % (ok, len(txt1), rem1))

if not ok:
    ok2 = T._strip_page(surg, idx)
    txt2, rem2 = remaining(surg.pages[idx], nd)
    say("  fullstrip ok=%s chars=%d surviving=%s" % (ok2, len(txt2), rem2))

# now replicate the exact transfer: add_page + merge overlay
w = PdfWriter()
w.add_page(surg.pages[idx])
p = w.pages[-1]
def empty_drawer(doc):
    pass
p.merge_page(T._overlay_page(float(p.mediabox.width),
                             float(p.mediabox.height), empty_drawer))
txt3, rem3 = remaining(w.pages[-1], nd)
say("  after add+merge: chars=%d surviving=%s" % (len(txt3), rem3))
if rem3 and not rem1:
    say("  >>> RESURRECTION: text came back during add_page/merge <<<")

# where does text live: page stream or forms?
def walk(res, depth, path):
    xo = T._resolve(res.get("/XObject")) or {}
    for name, ref in xo.items():
        obj = T._resolve(ref)
        try:
            if obj.get("/Subtype") != "/Form":
                continue
            data = obj.get_data()
        except Exception:
            continue
        shows = len(re.findall(rb"(?<![\w])(Tj|TJ)(?![\w])", data))
        say("    form %s/%s shows=%d bytes=%d" % (path, name, shows, len(data)))
        if depth < 6:
            walk(T._resolve(obj.get("/Resources")) or {}, depth + 1,
                 path + "/" + str(name))

say("  ORIGINAL containers:")
walk(T._resolve(surg.pages[idx].get("/Resources")) or {}, 0, "")
ann = surg.pages[idx].get("/Annots")
say("  annotations=%s" % (len(ann) if ann else 0))

# t6 rect vs actual show origins
t6 = next((b for b in changed if b["id"] == "t6"), changed[0])
rect = T._rect_of(t6)
samples = T._text_colors(T._reader(job["source"]).pages[idx])
inside = sum(1 for x, y, c in samples if T._in_rect(rect, x, y))
say("  %s rect=%s show_origins_inside=%d / total=%d"
    % (t6["id"], tuple(round(v, 1) for v in rect), inside, len(samples)))

# ---------------- page 2 overflow detail ----------------
say("PAGE2 overflow detail:")
b2 = job["blocks"]["2"]; l2 = job["locked"].get("2", []); all2 = b2 + l2
tr2 = job["translations"].get("2", {})
for bid in ("t12", "t15"):
    b = next((x for x in b2 if x["id"] == bid), None)
    if not b:
        continue
    tgt = T._expanded_block(b, all2)
    t = tr2.get(bid, b["text"])
    ls, size, over, lead, hs = T._fit_block(
        tgt, t, T._block_font(b), T.MIRROR_SCALE,
        floor_abs=T.MIRROR_FLOOR, tight=True)
    say("  %s lines=%d size=%.2f lead=%.2f hscale=%d over=%s "
        "y_bot=%.1f room=%.1f width=%.1f chars=%d"
        % (bid, len(ls), size, lead, hs, over, b["y_bot"],
           tgt["y_bot"], tgt.get("x_room", tgt["x1"]) - tgt["x0"], len(t)))

io.open(os.path.join("dev", "diag_page4_log.txt"), "w",
        encoding="utf-8").write("\n".join(lines))