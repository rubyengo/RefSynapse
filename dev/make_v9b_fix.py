# dev/make_v9b_fix.py - read fragments via font tables, honest URL repaint,
# guard fill via facing, strip markers on facing pages.
import io, py_compile, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SRC = r"refsynapse\ai\board_pdf_translate_tools.py"
text = io.open(SRC, encoding="utf-8").read()
before = text.count("\n") + 1
R = []

R.append((
'''def _color_runs(page):
    """(x, y, '#rrggbb', text) of every show command, page AND forms."""
    try:
        contents = page.get_contents()
        if contents is None:
            return []
        stream = ContentStream(contents, page.pdf)
        res = _resolve(page.get("/Resources")) or {}
    except Exception:
        return []
    samples = []
    _replay(stream.operations, [1, 0, 0, 1, 0, 0], "#000000", res, page.pdf,
            None, "runs", [], [], samples)
    return samples
''',
'''def _color_runs(page):
    """(x, y, '#rrggbb', text) per text piece. Text comes from the font
    tables (visitor), colour from the nearest show-command sample -
    raw string bytes of subset fonts are NOT readable text."""
    try:
        spans = _page_spans(page)
    except Exception:
        return []
    samples = _text_colors(page)
    out = []
    for s in spans:
        best, bd = None, 4.0
        for x, y, c in samples:
            d = (x - s["x"]) ** 2 + (y - s["y"]) ** 2
            if d < bd:
                bd, best = d, c
        if best is not None:
            out.append((s["x"], s["y"], best, s["text"]))
    return out
'''))

R.append((
'''def _block_accents(block, runs):
    """Coloured fragments inside a block whose colour differs from the
    block's own (majority) colour: inline links, brand names, etc."""
    rect = _rect_of(block)
    base = block.get("color") or "#000000"
    seen = {}
    for x, y, c, t in runs:
        if c == base or c == "#000000":
            continue
        if not _in_rect(rect, x, y):
            continue
        t = t.strip()
        if len(t) < 4:
            continue
        seen[(t, c)] = seen.get((t, c), 0) + 1
    out = [{"text": t, "color": c} for (t, c) in seen]
    out.sort(key=lambda d: -len(d["text"]))
    return out[:12]
''',
'''def _block_accents(block, runs):
    """Coloured fragments inside a block whose colour differs from the
    block's own (majority) colour: inline links, brand names, etc.
    The search area is the block's OWN ink, never its growth room."""
    rect = (block["x0"] - 1.5, block["y_bot"] - 2.0,
            block["x1"] + 1.5, block["first_y"] + 0.85 * block["size"])
    base = block.get("color") or "#000000"
    seen = {}
    for x, y, c, t in runs:
        if c == base:
            continue
        if not _in_rect(rect, x, y):
            continue
        t = t.strip()
        if len(t) < 4:
            continue
        seen[(t, c)] = seen.get((t, c), 0) + 1
    out = [{"text": t, "color": c} for (t, c) in seen]
    out.sort(key=lambda d: -len(d["text"]))
    return out[:12]
'''))

R.append((
'''    if accents:
        for m in _URL_RE.finditer(line):
            if free(m.start(), m.end()):
                cuts.append((m.start(), m.end(), accents[0][1]))
''',
'''    url_col = next((c for t, c in accents
                    if "http" in t or "www." in t), None)
    if url_col:
        for m in _URL_RE.finditer(line):
            if free(m.start(), m.end()):
                cuts.append((m.start(), m.end(), url_col))
'''))

R.append((
'''        tools.call("board_pdf_translate_fill",
                   {"job": acid, "page": 1, "blocks": send_ac})
''',
'''        tools.call("board_pdf_translate_fill",
                   {"job": acid, "page": 1, "blocks": send_ac,
                    "on_overflow": "facing"})
'''))

R.append((
'''        text = trans.get(block["id"], block["text"])
        if _bad_glyphs(text):
''',
'''        text = trans.get(block["id"], block["text"])
        text, _unused_marks = _strip_marks(text)
        if _bad_glyphs(text):
'''))

for i, (old, new) in enumerate(R, 1):
    n = text.count(old)
    if n != 1:
        print("ANCHOR %d FOUND %d TIMES - ABORT, file untouched" % (i, n))
        sys.exit(1)
    text = text.replace(old, new)
io.open(SRC, "w", encoding="utf-8", newline="\n").write(text)
py_compile.compile(SRC, doraise=True)
print("SYNTAX OK")
print("lines: %d -> %d" % (before, text.count("\n") + 1))
print("replacements applied:", len(R))