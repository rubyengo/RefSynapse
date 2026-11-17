# dev/make_v9b.py - v9b: per-fragment colour accents (URLs, names, markers).
import io, os, py_compile, shutil, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SRC = r"refsynapse\ai\board_pdf_translate_tools.py"
BAK = r"dev\board_pdf_translate_tools.v9a.py"
text = io.open(SRC, encoding="utf-8").read()
before = text.count("\n") + 1
if not os.path.isfile(BAK):
    shutil.copyfile(SRC, BAK)
    print("backup ->", BAK)
else:
    print("backup exists ->", BAK)

# _replay: mode "runs" behaves like "colors" everywhere (2 occurrences).
old = 'if writer is not None and mode != "colors":'
new = 'if writer is not None and mode not in ("colors", "runs"):'
n = text.count(old)
if n != 2:
    print("PRE ANCHOR FOUND %d TIMES (expected 2) - ABORT" % n)
    sys.exit(1)
text = text.replace(old, new)

R = []

R.append(('elif walk and mode == "colors":',
          'elif walk and mode in ("colors", "runs"):'))
R.append(('child_seen = (seen if mode == "colors"',
          'child_seen = (seen if mode in ("colors", "runs")'))
R.append((
'''                if mode == "colors":
                    samples.append((x, y, fill))
''',
'''                if mode in ("colors", "runs"):
                    if mode == "colors":
                        samples.append((x, y, fill))
                    else:
                        raw = operands[-1] if operands else ""
                        if isinstance(raw, list):
                            raw = "".join(str(v) for v in raw
                                          if not isinstance(v, (int, float)))
                        samples.append((x, y, fill, str(raw)))
'''))

# ---- colour-run sampler + accent collector, before _expanded_block -------
R.append((
"def _expanded_block(block, all_blocks):\n",
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


def _block_accents(block, runs):
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


def _expanded_block(block, all_blocks):
'''))

# ---- per-fragment drawing -------------------------------------------------
R.append((
'''def _draw_blocks(doc, blocks, trans, all_blocks, page_over, key,
                 min_scale, floor_abs, tight, expand, placed=None):
    from reportlab.pdfbase import pdfmetrics as _pm
    smallest = 99.0
    for block in blocks:
        text = trans.get(block["id"], block["text"])
        font = _block_font(block)
        target = _expanded_block(block, all_blocks) if expand else block
        lines, size, over, lead, hscale = _fit_block(
            target, text, font, min_scale, floor_abs=floor_abs, tight=tight)
        smallest = min(smallest, size)
        if over:
            page_over.append({"page": int(key), "id": block["id"],
                              "font_size": round(size, 2),
                              "hscale": hscale})
        doc.setFillColor(HexColor(block.get("color") or "#000000"))
        y = block["first_y"]
        rec = {"id": block["id"], "lines": []}
        for line in lines:
            if line:
                to = doc.beginText(block["x0"], y)
                to.setFont(font, size)
                if hscale != 100:
                    to.setHorizScale(hscale)
                to.textOut(line)
                doc.drawText(to)
                w = _pm.stringWidth(line, font, size) * hscale / 100.0
                rec["lines"].append((block["x0"], block["x0"] + w, y, size))
            y -= size * lead
        if placed is not None:
            placed.append(rec)
    return smallest
''',
'''_URL_RE = re.compile(r"(?:https?://|www\\.)[^\\s\\]\\)>,;\\"]+")
_MARK_RE = re.compile(r"\\[\\[(#[0-9a-fA-F]{6})\\]\\](.*?)\\[\\[/\\]\\]", re.S)


def _strip_marks(text):
    """Pull [[#rrggbb]]...[[/]] colour markers out of the translation.
    Returns (plain text, [(start, end, colour), ...])."""
    ranges, out, pos = [], [], 0
    for m in _MARK_RE.finditer(text):
        out.append(text[pos:m.start()])
        base = sum(len(s) for s in out)
        frag = m.group(2)
        out.append(frag)
        ranges.append((base, base + len(frag), m.group(1).lower()))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out), ranges


def _line_starts(lines, plain):
    norm = re.sub(r"\\s+", " ", plain).strip()
    out, pos = [], 0
    for ln in lines:
        idx = norm.find(ln, pos)
        if idx < 0:
            return None
        out.append(idx)
        pos = idx + len(ln)
    return out


def _line_fragments(line, start, ranges, accents, base):
    """Split a wrapped line into (fragment, colour) pieces: explicit
    markers first, then verbatim accent names, then bare URLs."""
    cuts = []
    for s, e, c in ranges:
        a, b = max(s, start), min(e, start + len(line))
        if a < b:
            cuts.append((a - start, b - start, c))

    def free(s, e):
        return all(e <= a or s >= b for a, b, _ in cuts)

    for txt, col in sorted(accents, key=lambda t: -len(t[0])):
        for m in re.finditer(re.escape(txt), line):
            if free(m.start(), m.end()):
                cuts.append((m.start(), m.end(), col))
    if accents:
        for m in _URL_RE.finditer(line):
            if free(m.start(), m.end()):
                cuts.append((m.start(), m.end(), accents[0][1]))
    cuts.sort()
    out, pos = [], 0
    for s, e, c in cuts:
        if s > pos:
            out.append((line[pos:s], base))
        out.append((line[s:e], c))
        pos = e
    if pos < len(line):
        out.append((line[pos:], base))
    return out or [(line, base)]


def _draw_blocks(doc, blocks, trans, all_blocks, page_over, key,
                 min_scale, floor_abs, tight, expand, placed=None):
    from reportlab.pdfbase import pdfmetrics as _pm
    smallest = 99.0
    for block in blocks:
        raw = trans.get(block["id"], block["text"])
        plain, ranges = _strip_marks(raw)
        font = _block_font(block)
        target = _expanded_block(block, all_blocks) if expand else block
        lines, size, over, lead, hscale = _fit_block(
            target, plain, font, min_scale, floor_abs=floor_abs, tight=tight)
        smallest = min(smallest, size)
        if over:
            page_over.append({"page": int(key), "id": block["id"],
                              "font_size": round(size, 2),
                              "hscale": hscale})
        base = block.get("color") or "#000000"
        accents = [(a["text"], a["color"])
                   for a in (block.get("accent_parts") or [])
                   if a.get("text") and a.get("color")]
        starts = _line_starts(lines, plain)
        if starts is None:
            ranges = []
        y = block["first_y"]
        rec = {"id": block["id"], "lines": []}
        for li, line in enumerate(lines):
            if line:
                frags = _line_fragments(
                    line, starts[li] if starts else 0, ranges, accents, base)
                x = block["x0"]
                for frag, col in frags:
                    if not frag:
                        continue
                    doc.setFillColor(HexColor(col))
                    to = doc.beginText(x, y)
                    to.setFont(font, size)
                    if hscale != 100:
                        to.setHorizScale(hscale)
                    to.textOut(frag)
                    doc.drawText(to)
                    x += _pm.stringWidth(frag, font, size) * hscale / 100.0
                rec["lines"].append((block["x0"], x, y, size))
            y -= size * lead
        if placed is not None:
            placed.append(rec)
    return smallest
'''))

# ---- t_start: store accents ------------------------------------------------
R.append((
'''            samples = _text_colors(page)
            imgs = _image_rects(page)
            ready_out = []
''',
'''            samples = _text_colors(page)
            runs = _color_runs(page)
            imgs = _image_rects(page)
            ready_out = []
'''))
R.append((
'''                item["color"] = _block_color(item, samples)
                item["avoid"] = _avoid_for(item, imgs)
                ready_out.append(item)
''',
'''                item["color"] = _block_color(item, samples)
                item["accent_parts"] = _block_accents(item, runs)
                item["avoid"] = _avoid_for(item, imgs)
                ready_out.append(item)
'''))
R.append((
'''                item["color"] = _block_color(item, samples)
                item["avoid"] = _avoid_for(item, imgs)
                locked_out.append(item)
''',
'''                item["color"] = _block_color(item, samples)
                item["accent_parts"] = _block_accents(item, runs)
                item["avoid"] = _avoid_for(item, imgs)
                locked_out.append(item)
'''))

# ---- _render: refresh accents for old jobs ---------------------------------
R.append((
'''        if ready:
            samples = _text_colors(src.pages[index])
            imgs = _image_rects(src.pages[index])
            for block in all_blocks:
                block["color"] = _block_color(block, samples)
                block["avoid"] = _avoid_for(block, imgs)
''',
'''        if ready:
            samples = _text_colors(src.pages[index])
            runs = _color_runs(src.pages[index])
            imgs = _image_rects(src.pages[index])
            for block in all_blocks:
                block["color"] = _block_color(block, samples)
                block["accent_parts"] = _block_accents(block, runs)
                block["avoid"] = _avoid_for(block, imgs)
'''))

# ---- fill description hint --------------------------------------------------
R.append((
"never invent ids. PARTIAL ACCEPT:",
"never invent ids. Colour accents: URLs and unchanged names keep their "
"original colour automatically; you may wrap a translated fragment in "
"[[#rrggbb]]...[[/]] to force its colour. PARTIAL ACCEPT:"))

# ---- v9b selftest guards -----------------------------------------------------
R.append((
'''        if placed:
            tools.call("board_delete", {"uids": placed})
''',
'''        # ---- v9b guard: inline colour accents survive translation ------
        from reportlab.pdfbase import pdfmetrics as _pmg
        buf = _io.BytesIO()
        cv = _cv.Canvas(buf, pagesize=(297, 420))
        cv.setFont(_font_name(), 10)
        fn = _font_name()
        cv.setFillColor(HexColor("#000000"))
        a1 = "Read the manual at "
        u1 = "http://example.com"
        x1 = 30 + _pmg.stringWidth(a1, fn, 10)
        cv.drawString(30, 380, a1)
        cv.setFillColor(HexColor("#6b9f25"))
        cv.drawString(x1, 380, u1)
        cv.setFillColor(HexColor("#000000"))
        cv.drawString(x1 + _pmg.stringWidth(u1, fn, 10), 380, " for details.")
        cv.drawString(30, 340, "See ")
        xb = 30 + _pmg.stringWidth("See ", fn, 10)
        cv.setFillColor(HexColor("#6b9f25"))
        cv.drawString(xb, 340, "HowStuffWorks")
        cv.setFillColor(HexColor("#000000"))
        cv.drawString(xb + _pmg.stringWidth("HowStuffWorks", fn, 10), 340,
                      " for more.")
        cv.drawString(30, 300, "Visit ")
        xa = 30 + _pmg.stringWidth("Visit ", fn, 10)
        cv.setFillColor(HexColor("#6b9f25"))
        cv.drawString(xa, 300, "AnandTech")
        cv.setFillColor(HexColor("#000000"))
        cv.drawString(xa + _pmg.stringWidth("AnandTech", fn, 10), 300,
                      " now.")
        cv.showPage()
        cv.save()
        with open(p("accent.pdf"), "wb") as stream:
            stream.write(buf.getvalue())
        st_ac = tools.call("board_pdf_translate_start",
                           {"path": p("accent.pdf"), "restart": True})
        acid = st_ac.get("job")
        jac = job_of(acid)
        ab = jac["blocks"].get("1", [])
        expect("accent parts stored",
               any(b.get("accent_parts") for b in ab))
        send_ac = {}
        for b in ab:
            if b["text"].startswith("Read"):
                send_ac[b["id"]] = ("\\u0427\\u0438\\u0442\\u0430\\u0439\\u0442\\u0435 "
                                    "\\u0440\\u0443\\u043a\\u043e\\u0432\\u043e\\u0434\\u0441\\u0442\\u0432\\u043e "
                                    "\\u043d\\u0430 http://example.com "
                                    "\\u0440\\u0430\\u0434\\u0438 \\u0434\\u0435\\u0442\\u0430\\u043b\\u0435\\u0439.")
            elif b["text"].startswith("See"):
                send_ac[b["id"]] = ("\\u0421\\u043c\\u043e\\u0442\\u0440\\u0438\\u0442\\u0435 "
                                    "HowStuffWorks "
                                    "\\u043f\\u043e\\u0434\\u0440\\u043e\\u0431\\u043d\\u0435\\u0435.")
            else:
                send_ac[b["id"]] = ("\\u041f\\u043e\\u0441\\u0435\\u0442\\u0438\\u0442\\u0435 "
                                    "[[#6b9f25]]\\u0410\\u043d\\u0430\\u043d\\u0434\\u0422\\u0435\\u0445[[/]] "
                                    "\\u0441\\u0435\\u0439\\u0447\\u0430\\u0441.")
        tools.call("board_pdf_translate_fill",
                   {"job": acid, "page": 1, "blocks": send_ac})
        fin_ac = tools.call("board_pdf_translate_finish",
                            {"job": acid, "out": p("accent_ru.pdf"),
                             "overwrite": True, "place": False})
        show("finish accent guard", fin_ac, "pages", "mirror")
        expect("accent one page", fin_ac.get("pages") == 1)
        if fin_ac.get("ok"):
            ap = _reader(p("accent_ru.pdf")).pages[0]
            aruns = _color_runs(ap)
            expect("url accent kept",
                   any(c == "#6b9f25" and "http" in t
                       for _, _, c, t in aruns))
            expect("brand accent kept",
                   any(c == "#6b9f25" and "HowStuffWorks" in t
                       for _, _, c, t in aruns))
            expect("marker accent kept",
                   any(c == "#6b9f25" and abs(y - 300) < 2
                       for _, y, c, _ in aruns))
            expect("accent base black kept",
                   any(c == "#000000" for _, _, c, _ in aruns))
            expect("markers stripped", "[[" not in (ap.extract_text() or ""))
        if placed:
            tools.call("board_delete", {"uids": placed})
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
print("replacements applied:", len(R) + 1)