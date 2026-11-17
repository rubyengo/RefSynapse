import io, json, os, re, sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.getcwd())

from pypdf import PdfReader
from refsynapse.ai import board_pdf_translate_tools as T

JOB = os.path.join("agent", "translate", "mk-740_742_manual_no_ru", "job.json")
OUT = os.path.join("agent", "mk-740_742_manual_no_ru_ru.pdf")

job = json.load(io.open(JOB, encoding="utf-8"))
reader = PdfReader(OUT)
lines = []


def say(s):
    lines.append(s)
    print(s)


say("PAGES %d modes %s" % (len(reader.pages), job["mode"]))

for key in sorted(job["blocks"], key=int):
    i = int(key) - 1
    if i >= len(reader.pages):
        continue
    text = re.sub(r"\s", "", reader.pages[i].extract_text() or "")
    trans = job["translations"].get(key, {})
    blocks = job["blocks"][key]
    locked = job.get("locked", {}).get(key, [])
    say("== page %s mode=%s blocks=%d" % (key, job["mode"].get(key), len(blocks)))
    for b in blocks:
        needle = re.sub(r"\s", "", b["text"].split("\n")[0])[:40]
        if len(needle) >= 6 and needle in text:
            say("  ENGLISH SURVIVED %s %r" % (b["id"], b["text"][:60]))
    allb = blocks + locked
    drawn = []
    for b in blocks:
        try:
            t = trans.get(b["id"], b["text"])
            target = T._expanded_block(b, allb)
            ls, size, over, lead, hs = T._fit_block(
                target, t, T._block_font(b), T.MIRROR_SCALE,
                floor_abs=T.MIRROR_FLOOR, tight=True)
            bottom = b["first_y"] - (len(ls) - 1) * size * lead - 0.30 * size
            drawn.append((b, bottom))
        except Exception as e:
            say("  FIT ERROR %s %s: %s" % (b["id"], type(e).__name__, e))
    for b, bottom in drawn:
        for o in allb:
            if o is b:
                continue
            top = o["first_y"] + 0.75 * o["size"]
            if top < b["first_y"] and top > bottom - 0.5 \
               and o["x0"] < b.get("x_room", b["x1"]) \
               and b["x0"] < o.get("x_room", o["x1"]):
                say("  OVERLAP %s into %s bottom=%.1f top=%.1f"
                    % (b["id"], o["id"], bottom, top))

io.open(os.path.join("dev", "diag_overlap_log.txt"), "w",
        encoding="utf-8").write("\n".join(lines))