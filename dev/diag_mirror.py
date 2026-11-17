"""How many pages of the saved job can be mirrored in place? Read-only."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from refsynapse.ai.board_pdf_translate_tools import (
    _fit_block, _block_font, _bad_glyphs)


def say(*parts):
    line = " ".join(str(p) for p in parts)
    print(line.encode("ascii", "backslashreplace").decode("ascii"))


def main():
    path = os.path.join(ROOT, "agent", "translate", "manual_en", "job.json")
    with open(path, "r", encoding="utf-8") as stream:
        job = json.load(stream)
    say("pages:", job["pages_total"],
        "inplace:", sum(1 for m in job["mode"].values() if m == "inplace"),
        "facing:", sum(1 for m in job["mode"].values() if m == "facing"))

    def expanded(block, all_blocks):
        top = block["first_y"] + 0.75 * block["size"]
        below = [o for o in all_blocks
                 if o is not block
                 and o["first_y"] + 0.75 * o["size"] < top - 1.0
                 and o["x0"] < block.get("x_room", block["x1"])
                 and block["x0"] < o.get("x_room", o["x1"])]
        nb = max(below, key=lambda o: o["first_y"]) if below else None
        room = (nb["first_y"] + 0.75 * nb["size"] - 2.0) if nb else 24.0
        out = dict(block)
        out["y_bot"] = min(block["y_bot"], room)
        return out

    def overflows(blocks, trans, scale, use_room, all_blocks):
        bad = 0
        for b in blocks:
            text = trans.get(b["id"])
            if text is None:
                continue
            bb = expanded(b, all_blocks) if use_room else b
            lines, size, over = _fit_block(bb, text, _block_font(b), scale)
            if over:
                bad += 1
        return bad

    clean = 0
    say("page mode blocks | over@0.7 over@0.6 over@0.6+room | notes")
    for n in range(1, job["pages_total"] + 1):
        key = str(n)
        mode = job["mode"].get(key, "kept")
        blocks = job["blocks"].get(key, [])
        locked = job["locked"].get(key, [])
        trans = job["translations"].get(key, {})
        allb = blocks + locked
        notes = []
        if mode == "facing":
            if any(_bad_glyphs(b["text"]) for b in locked):
                notes.append("boxes")
            if any((b.get("x_room", b["x1"]) - b["x0"]) < 40
                   and len(b["text"]) > 25 for b in blocks):
                notes.append("cramped")
            if len(blocks) > 45:
                notes.append("many")
            if not notes:
                notes.append("flipped-later")
        a = overflows(blocks, trans, 0.7, False, allb)
        b = overflows(blocks, trans, 0.6, False, allb)
        c = overflows(blocks, trans, 0.6, True, allb)
        if c == 0 and mode in ("inplace", "facing"):
            clean += 1
        say("p%-2d %-7s %3d | %3d %3d %3d | %s"
            % (n, mode, len(blocks), a, b, c, ",".join(notes)))
    say("pages that fit cleanly with extra room at 0.6:", clean, "of",
        job["pages_total"])
    say("DONE")


if __name__ == "__main__":
    main()