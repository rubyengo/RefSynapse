# dev\diag_merge.py - isolate WHERE page 4 text comes back + page 2 geometry.
import hashlib
import io
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.getcwd())

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject
from refsynapse.ai import board_pdf_translate_tools as T

JOB = os.path.join("agent", "translate", "mk-740_742_manual_no_ru", "job.json")
job = json.load(io.open(JOB, encoding="utf-8"))
lines = []


def say(s):
    lines.append(s)
    print(s)


idx, key = 3, "4"
blocks = job["blocks"][key]
locked = job["locked"].get(key, [])
trans = job["translations"].get(key, {})
changed = [b for b in blocks
           if T._norm(trans.get(b["id"], b["text"])) != T._norm(b["text"])]
nd = [(b["id"], n) for b in changed for n in T._block_needles(b, [])]


def rem(page):
    txt = T._norm(page.extract_text() or "")
    return len(txt), sorted({i for i, n in nd if n in txt})


def fingerprint(page, label):
    c = T._resolve(page.get("/Contents"))
    streams = list(c) if isinstance(c, ArrayObject) else [c]
    say("%s /Contents=%s streams=%d" % (label, type(c).__name__, len(streams)))
    for k, s in enumerate(streams):
        s = T._resolve(s)
        try:
            data = s.get_data()
        except Exception as ex:
            say("   stream%d ERROR %s" % (k, ex))
            continue
        say("   stream%d len=%d md5=%s has_english=%s"
            % (k, len(data), hashlib.md5(data).hexdigest()[:8],
               b"Connect the microphone" in data))


surg = PdfWriter(clone_from=T._reader(job["source"]))
ok = T._surgical_strip(surg, idx, changed, locked)
say("surgical ok=%s rem=%s" % (ok, rem(surg.pages[idx])))
fingerprint(surg.pages[idx], "surg")

w1 = PdfWriter()
w1.add_page(surg.pages[idx])
say("add_only rem=%s" % (rem(w1.pages[-1]),))
fingerprint(w1.pages[-1], "w1")

buf = io.BytesIO()
surg.write(buf)
buf.seek(0)
r2 = PdfReader(buf)
say("reread rem=%s" % (rem(r2.pages[idx]),))
fingerprint(r2.pages[idx], "r2")

w2 = PdfWriter()
w2.add_page(r2.pages[idx])
say("reread_add rem=%s" % (rem(w2.pages[-1]),))
p = w2.pages[-1]
p.merge_page(T._overlay_page(float(p.mediabox.width),
                             float(p.mediabox.height), lambda doc: None))
say("reread_add_merge rem=%s" % (rem(w2.pages[-1]),))

say("PAGE2 geometry:")
all2 = job["blocks"]["2"] + job["locked"].get("2", [])
tr2 = job["translations"].get("2", {})
for bid in ("t12", "t13", "t15", "t16", "k4", "k5", "k6"):
    b = next((x for x in all2 if x["id"] == bid), None)
    if b:
        say("  %s x0=%.1f x1=%.1f x_room=%.1f first_y=%.1f y_bot=%.1f "
            "size=%.2f %r" % (bid, b["x0"], b["x1"],
                              b.get("x_room", b["x1"]), b["first_y"],
                              b["y_bot"], b["size"], b["text"][:36]))
say("t12 translation has newline=%s text=%r"
    % ("\n" in tr2.get("t12", ""), tr2.get("t12", "")[:120]))

io.open(os.path.join("dev", "diag_merge_log.txt"), "w",
        encoding="utf-8").write("\n".join(lines))