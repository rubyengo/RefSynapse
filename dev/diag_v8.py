# dev\diag_v8.py - inspect the FRESH translation made by the assistant (v8b).
import io, json, os, re, sys, time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.getcwd())

from pypdf import PdfReader
from refsynapse.ai import board_pdf_translate_tools as T

FOLDER = os.path.join("agent", "translate", "mk-740_742_manual_no_ru")
job = json.load(io.open(os.path.join(FOLDER, "job.json"), encoding="utf-8"))

lines = []
def say(s):
    lines.append(s); print(s)

say("finished field -> %r" % job.get("finished"))
cands = [os.path.join("agent", "mk-740_742_manual_no_ru_ru.pdf"),
         os.path.join("agent", "mk-740_742_manual_no_ru_ru_v8.pdf")]
fin = job.get("finished")
if fin:
    cands.insert(0, fin)
for c in cands:
    if os.path.isfile(c):
        say("file %s  %d bytes  %s" % (
            c, os.path.getsize(c),
            time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(c)))))
    else:
        say("file %s  MISSING" % c)

OUT = fin if fin and os.path.isfile(fin) else cands[1]
say("CHECKING -> %s" % OUT)
reader = PdfReader(OUT)
say("pages %d modes %s" % (len(reader.pages), job["mode"]))

bad, clash_total = [], 0
for key in sorted(job["blocks"], key=int):
    i = int(key) - 1
    page = reader.pages[i]
    trans = job["translations"].get(key, {})
    blocks = job["blocks"][key]
    locked = job["locked"].get(key, [])
    allb = blocks + locked
    changed, same = [], []
    for b in blocks:
        now = trans.get(b["id"], b["text"])
        (same if T._norm(now) == T._norm(b["text"]) else changed).append(b)
    forbidden = [T._norm(b.get("text", "")) for b in locked + same]
    forbidden += [T._norm(v) for v in trans.values()]
    gone = T._wording_gone(page, changed, forbidden)
    after = T._norm(page.extract_text() or "")
    say("== page %s mode=%s status=%s changed=%d same=%d wording_gone=%s"
        % (key, job["mode"].get(key), job["status"].get(key),
           len(changed), len(same), gone))
    if not gone:
        bad.append(int(key))
        for b in changed:
            for needle in T._block_needles(b, forbidden):
                if needle in after:
                    say("  SURVIVED %s %r" % (b["id"], needle[:60]))
    small = 99.0
    for b in changed:
        t = trans.get(b["id"], b["text"])
        target = T._expanded_block(b, allb)
        ls, size, over, lead, hs = T._fit_block(
            target, t, T._block_font(b), T.MIRROR_SCALE,
            floor_abs=T.MIRROR_FLOOR, tight=True)
        small = min(small, size)
        bottom = b["first_y"] - (len(ls) - 1) * size * lead - 0.30 * size
        for o in allb:
            if o is b:
                continue
            top = o["first_y"] + 0.75 * o["size"]
            if top < b["first_y"] and bottom < top - 0.05 \
               and o["x0"] < b.get("x_room", b["x1"]) \
               and b["x0"] < o.get("x_room", o["x1"]):
                clash_total += 1
                say("  CLASH %s into %s bottom=%.1f top=%.1f"
                    % (b["id"], o["id"], bottom, top))
    if changed:
        say("  smallest fitted font %.2f pt" % small)

say("SUMMARY bad_wording=%s clashes=%d" % (bad, clash_total))
io.open(os.path.join("dev", "diag_v8_log.txt"), "w",
        encoding="utf-8").write("\n".join(lines))