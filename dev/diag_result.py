r"""Compare the job's translations with block sizes; find overlaps and leftovers."""
import json
import os
import re

from pypdf import PdfReader

JOB = os.path.join("agent", "translate", "manual_en", "job.json")
RES = os.path.join("agent", "manual_en_ru.pdf")
PAGES = [3, 7, 17, 19, 20]

with open(JOB, encoding="utf-8") as f:
    job = json.load(f)
print("status:", {k: job["status"].get(k) for k in map(str, PAGES)})

reader = PdfReader(RES) if os.path.isfile(RES) else None
for pg in PAGES:
    key = str(pg)
    blocks = job["blocks"].get(key, [])
    trans = job["translations"].get(key, {})
    print("--- page %d: %d blocks, %d translated" % (pg, len(blocks), len(trans)))
    worst = []
    for b in blocks:
        text = trans.get(b["id"])
        if text is None:
            continue
        width = b.get("x_room", b["x1"]) - b["x0"]
        lines_orig = b["text"].count("\n") + 1
        chars_per_line = max(1, int(width / (0.5 * b["size"])))
        need = len(text) / chars_per_line
        if need > lines_orig * 1.35 or width < 60 and len(text) > 40:
            worst.append((round(need - lines_orig, 1), b["id"], round(width),
                          len(text), b["text"][:40], text[:40]))
    worst.sort(reverse=True)
    for w in worst[:6]:
        print("   OVERFLOW? extra_lines=%.1f %s width=%d chars=%d" % w[:4])
        print("      orig: %s" % w[4])
        print("      sent: %s" % w[5])
    missing = [b["id"] for b in blocks if b["id"] not in trans]
    print("   not translated:", missing[:15])
    if reader is not None:
        text = reader.pages[pg - 1].extract_text() or ""
        print("   result chars=%d | 'Power (5V)' in result: %s | 'Follow the steps' in result: %s"
              % (len(text), "Power (5V)" in text, "Follow the steps" in text))
print("DONE")