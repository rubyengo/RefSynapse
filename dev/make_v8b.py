# dev\make_v8b.py - v8b: honest 1 pt clearance, two extra squeeze rungs,
# lock rects by real ink, list markers survive a full redraw.
import io
import os
import re
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PATH = os.path.join("refsynapse", "ai", "board_pdf_translate_tools.py")
BACKUP = os.path.join("dev", "board_pdf_translate_tools.v8a.py")

src = io.open(PATH, "r", encoding="utf-8", newline="").read()
if "\r\n" in src:
    src = src.replace("\r\n", "\n")
old_lines = src.count("\n") + 1

if not os.path.isfile(BACKUP):
    shutil.copyfile(PATH, BACKUP)
    print("backup ->", BACKUP)
else:
    print("backup exists ->", BACKUP)


def sub(text, anchor, new, label):
    if text.count(anchor) != 1:
        raise SystemExit("ANCHOR FAIL %s: %d matches"
                         % (label, text.count(anchor)))
    print(label)
    return text.replace(anchor, new, 1)


def replace_func(text, name, new):
    pat = re.compile(r"(?m)^def " + re.escape(name) + r"\(")
    hits = list(pat.finditer(text))
    if len(hits) != 1:
        raise SystemExit("ANCHOR FAIL %s: %d matches" % (name, len(hits)))
    start = hits[0].start()
    rest = text[start:].split("\n")
    end = start + len(rest[0]) + 1
    for ln in rest[1:]:
        if ln[:1].strip() != "":
            break
        end += len(ln) + 1
    return text[:start] + new.rstrip("\n") + "\n\n" + text[end:]


src = sub(src, "MIRROR_FLOOR = 4.0",
          "MIRROR_FLOOR = 4.0\nMIRROR_GAP = 1.0",
          "gap constant added")

src = sub(src, "MIRROR_SQUEEZE = (100, 90, 80, 70)",
          "MIRROR_SQUEEZE = (100, 90, 80, 70, 60, 55)",
          "squeeze ladder extended")

src = sub(src,
          '    room = (neighbour["first_y"] + 0.75 * neighbour["size"] '
          '+ 2.0) \\',
          '    room = (neighbour["first_y"] + 0.75 * neighbour["size"] '
          '+ MIRROR_GAP) \\',
          "expanded_block uses MIRROR_GAP")

src = sub(src,
          '"""Room below the block, stopping 2 pt ABOVE the neighbour\'s top.',
          '"""Room below the block, stopping a small gap ABOVE the neighbour.',
          "expanded_block docstring")

NEW_SURGICAL = r'''def _lock_rect_of(block):
    """Protection area of kept wording: its OWN ink, never the room it
    could grow into (x_room). An inflated bullet rect used to shield the
    first show command of the neighbouring translated line.
    """
    return (block["x0"] - 1.0,
            block["y_bot"] - 1.0,
            block["x1"] + 1.0,
            block["first_y"] + 0.85 * block["size"])


def _marker_locked(block):
    """True for a pure list marker (bullet, dash). It is redrawn with the
    wording when a page had to be cleaned fully, so lists keep bullets.
    """
    text = str(block.get("text", "")).strip()
    if not text or len(text) > 3:
        return False
    return all(ch in BULLET_STARTS or ch in ".)]" for ch in text)


def _surgical_strip(writer, index, trans_blocks, locked_blocks):
    """Remove ONLY the translated wording, on the page and inside forms.
    Every line of every translated block must vanish - zero tolerance.
    Lines that also occur in kept wording are not used as evidence.
    """
    page = writer.pages[index]
    contents = page.get_contents()
    if contents is None:
        return False
    forbidden = [_norm(b.get("text", "")) for b in locked_blocks]
    trans_rects = [_rect_of(b) for b in trans_blocks]
    lock_rects = [_lock_rect_of(b) for b in locked_blocks]
    try:
        res = _private_resources(page, writer)
        stream = ContentStream(contents, writer)
    except Exception:
        return False
    kept = _replay(stream.operations, [1, 0, 0, 1, 0, 0], "#000000", res,
                   writer, writer, "surgical", trans_rects, lock_rects, [])
    cs = ContentStream(None, writer)
    cs.operations = kept
    try:
        page.replace_contents(cs)
    except Exception:
        return False
    return _wording_gone(page, trans_blocks, forbidden)
'''

src = replace_func(src, "_surgical_strip", NEW_SURGICAL)
print("lock rects by real ink")

src = sub(src,
          "b for b in k_blocks if _readable_locked(b)]",
          "b for b in k_blocks\n"
          "                            if _readable_locked(b) or _marker_locked(b)]",
          "markers redrawn")

src = sub(src, "bottom < top + 1.9", "bottom < top + 0.9",
          "guard threshold aligned")

src = sub(src, "horizontally to 70%.",
          "horizontally to 55 percent as the very last resort.",
          "module ladder wording")

src = sub(src, "horizontal squeeze to 70 percent.",
          "horizontal squeeze to 55 percent as the last resort.",
          "tool ladder wording")

src = sub(src,
          "Mirror fitting keeps a 2 pt vertical gap above the neighbour\n"
          "below, with no baseline slack.",
          "Mirror fitting keeps a 1 pt vertical gap above the neighbour\n"
          "below, with no baseline slack; the last rungs squeeze horizontally\n"
          "to 60 and 55 percent instead of touching a neighbour. Kept wording\n"
          "is protected by its own ink width, and list markers are redrawn\n"
          "when a page had to be cleaned fully.",
          "v8 note updated")
print("docs updated")

try:
    compile(src, PATH, "exec")
except SyntaxError as ex:
    raise SystemExit("SYNTAX FAIL line %s: %s" % (ex.lineno, ex.msg))

with io.open(PATH, "w", encoding="utf-8", newline="") as stream:
    stream.write(src)
print("lines %d -> %d" % (old_lines, src.count("\n") + 1))
print("SYNTAX OK")