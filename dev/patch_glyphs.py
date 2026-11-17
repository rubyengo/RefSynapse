r"""Stop locking every multi-line paragraph.

The 'unreadable glyph' guard counted code 10 - the line break the tool
itself inserts between the lines of a block - as junk, so two thirds of
the manual was locked and stayed English. Real boxes (code 9633) only
occur in diagram labels; those stay locked. Mostly-readable blocks with
a stray control code are cleaned and offered instead of lost.
"""
import io
import py_compile
import shutil

PATH = r"refsynapse\ai\board_pdf_translate_tools.py"
BACKUP = r"dev\board_pdf_translate_tools.before_glyphs.py"

A1 = "def _bad_glyphs(text):"
A2 = "def _capacity(block):"
B1 = '''            unreadable = (_bad_glyphs("".join(s["text"] for s in spans))
                          or _outside(page, spans))'''

NEW_FUNCS = r'''def _junk(ch):
    """One character that no system font can honestly redraw."""
    if ch in "\n\r\t ":
        return False
    code = ord(ch)
    return (code < 32 or 0x80 <= code <= 0x9F
            or 0xE000 <= code <= 0xF8FF
            or code in (0xFFFD, 0x25A1, 0x25A0))


def _bad_glyphs(text):
    """True when the wording contains characters a system font cannot draw.

    The line breaks the tool itself puts between lines are NOT junk: they
    are separators, not glyphs. Counting them once locked every multi-line
    paragraph and left two thirds of the manual in English.
    """
    return any(_junk(ch) for ch in text)


def _junk_ratio(text):
    solid = re.sub(r"\s", "", text)
    if not solid:
        return 0.0
    return sum(1 for ch in solid if _junk(ch)) / float(len(solid))


def _classify(blocks):
    """(locked, translatable) - markers, numbers and unreadable labels stay.

    A mostly readable block with a stray unmapped glyph is cleaned (the
    glyph becomes a space) and offered; a block that is mostly boxes is a
    diagram label and stays locked.
    """
    locked, ready = [], []
    for block in blocks:
        text = block["text"]
        if not re.search(r"[A-Za-z\u0400-\u04FF]", text):
            locked.append(block)
            continue
        if _junk_ratio(text) > 0.2:
            locked.append(block)
            continue
        if _bad_glyphs(text):
            cleaned = "".join(" " if _junk(ch) else ch for ch in text)
            cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
            block = dict(block)
            block["text"] = cleaned
        ready.append(block)
    return locked, ready


'''

NEW_CHECK = '''            joined = "".join(s["text"] for s in spans)
            unreadable = (_junk_ratio(joined) > 0.05
                          or _outside(page, spans)
                          or any(_bad_glyphs(b["text"]) for b in keep_blocks))'''

with io.open(PATH, "r", encoding="utf-8-sig") as stream:
    src = stream.read()

if "_junk_ratio" in src:
    print("already patched - nothing to do")
    py_compile.compile(PATH, doraise=True)
    print("COMPILE OK")
    raise SystemExit(0)

for marker in (A1, A2, B1):
    n = src.count(marker)
    print("marker x%d (%s...)" % (n, marker[:40].replace("\n", " ")))
    if n != 1:
        raise SystemExit("STOP: marker not unique, file NOT changed")
i1, i2 = src.index(A1), src.index(A2)
if i1 >= i2:
    raise SystemExit("STOP: markers out of order, file NOT changed")

shutil.copyfile(PATH, BACKUP)
new = src[:i1] + NEW_FUNCS + src[i2:]
new = new.replace(B1, NEW_CHECK, 1)

with io.open(PATH, "w", encoding="utf-8", newline="\n") as stream:
    stream.write(new)
py_compile.compile(PATH, doraise=True)
print("backup ->", BACKUP)
print("lines: %d -> %d" % (len(src.split("\n")), len(new.split("\n"))))
print("COMPILE OK")