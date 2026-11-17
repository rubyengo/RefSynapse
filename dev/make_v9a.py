# dev/make_v9a.py - v9a: image obstacles + link rects follow text + relink dests.
import io, os, py_compile, shutil, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SRC = r"refsynapse\ai\board_pdf_translate_tools.py"
BAK = r"dev\board_pdf_translate_tools.v8c.py"
text = io.open(SRC, encoding="utf-8").read()
orig_lines = text.count("\n") + 1
if not os.path.isfile(BAK):
    shutil.copyfile(SRC, BAK)
    print("backup ->", BAK)
else:
    print("backup exists ->", BAK)

R = []

# ---- 1. new helpers before _norm -----------------------------------------
R.append((
"def _norm(text):\n",
'''def _image_rects(page):
    """Device-space rects of every raster image, page AND form containers.
    Form /Matrix is honoured; the cycle guard is per branch."""
    out = []
    try:
        contents = page.get_contents()
        if contents is None:
            return out
        stream = ContentStream(contents, page.pdf)
        res = _resolve(page.get("/Resources")) or {}
    except Exception:
        return out

    def walk(ops, res, ctm, depth, seen):
        if depth > 12:
            return
        stack = []
        for operands, op in ops:
            try:
                if op == b"q":
                    stack.append(ctm[:])
                elif op == b"Q":
                    if stack:
                        ctm = stack.pop()
                elif op == b"cm":
                    ctm = _mat_mul([float(v) for v in operands], ctm)
                elif op == b"Do":
                    xo = _resolve(res.get("/XObject")) or {}
                    obj = _resolve(xo.get(str(operands[0])))
                    if obj is None:
                        continue
                    sub = obj.get("/Subtype")
                    if sub == "/Image":
                        pts = [(x * ctm[0] + y * ctm[2] + ctm[4],
                                x * ctm[1] + y * ctm[3] + ctm[5])
                               for x, y in ((0, 0), (1, 0), (0, 1), (1, 1))]
                        xs = [p[0] for p in pts]
                        ys = [p[1] for p in pts]
                        out.append((min(xs), min(ys), max(xs), max(ys)))
                    elif sub == "/Form" and id(obj) not in seen:
                        fm = obj.get("/Matrix")
                        mat = ([float(v) for v in fm] if fm
                               else [1, 0, 0, 1, 0, 0])
                        child = _resolve(obj.get("/Resources")) or res
                        walk(ContentStream(obj, page.pdf).operations,
                             child, _mat_mul(mat, ctm), depth + 1,
                             seen | {id(obj)})
            except Exception:
                continue
    try:
        walk(stream.operations, res, [1, 0, 0, 1, 0, 0], 0, set())
    except Exception:
        pass
    return out


def _avoid_for(block, imgs):
    """Images this block must not grow into. An image the ORIGINAL wording
    already sat on is not an obstacle (captions, masked logos, backgrounds)."""
    ink = (block["x0"], block["y_bot"], block["x1"],
           block["first_y"] + 0.85 * block["size"])
    out = []
    for r in imgs:
        if r[2] - r[0] < 6 or r[3] - r[1] < 6:
            continue
        if ink[0] < r[2] - 1 and ink[2] > r[0] + 1 \\
           and ink[1] < r[3] - 1 and ink[3] > r[1] + 1:
            continue
        out.append([round(v, 2) for v in r])
    return out


def _page_ids(reader):
    ids = {}
    for i, pg in enumerate(reader.pages):
        ref = pg.indirect_reference
        if ref is not None:
            ids[ref.idnum] = i
    return ids


def _annots_of(page):
    a = page.get("/Annots")
    if a is None:
        return []
    try:
        return [ref.get_object() for ref in a.get_object()]
    except Exception:
        return []


def _dest_array(obj):
    d = obj.get("/Dest")
    act = obj.get("/A")
    if d is None and act is not None:
        act = act.get_object()
        if act.get("/S") == "/GoTo":
            d = act.get("/D")
    if d is None:
        return None
    d = _resolve(d)
    if isinstance(d, list) and d:
        return d
    return None


def _relink_annots(src, out, mapping):
    """Point every internal jump at the LIVE page object of the result."""
    src_ids = _page_ids(src)
    fixed = 0
    for page in out.pages:
        for obj in _annots_of(page):
            d = _dest_array(obj)
            if d is None:
                continue
            head = d[0]
            if not isinstance(head, IndirectObject):
                continue
            orig = src_ids.get(head.idnum)
            if orig is None:
                continue
            live = mapping.get(orig, orig)
            if live >= len(out.pages):
                continue
            ref = out.pages[live].indirect_reference
            if ref is not None and head.idnum != ref.idnum:
                d[0] = ref
                fixed += 1
    return fixed


def _move_links(page, blocks, placed):
    """Drag link rectangles onto the wording they belong to."""
    from pypdf.generic import ArrayObject, FloatObject
    if not placed:
        return 0
    moved = 0
    for obj in _annots_of(page):
        if obj.get("/Subtype") != "/Link":
            continue
        try:
            r = [float(v) for v in obj.get("/Rect")]
        except Exception:
            continue
        cy = (r[1] + r[3]) / 2.0
        best, best_area = None, 0.0
        for b in blocks:
            top = b["first_y"] + 0.85 * b["size"]
            ix = min(r[2], b["x1"]) - max(r[0], b["x0"])
            iy = min(r[3], top) - max(r[1], b["y_bot"])
            if ix > 0 and iy > 0 and ix * iy > best_area:
                best, best_area = b, ix * iy
        if best is None:
            continue
        new = next((q for q in placed if q["id"] == best["id"]), None)
        if new is None or not new["lines"]:
            continue
        old_top = best["first_y"] + 0.85 * best["size"]
        old_h = max(old_top - best["y_bot"], 1.0)
        frac = min(max((old_top - cy) / old_h, 0.0), 1.0)
        lines = new["lines"]
        pos = frac * (len(lines) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(lines) - 1)
        t = pos - lo
        y = lines[lo][2] * (1 - t) + lines[hi][2] * t
        sz = lines[lo][3]
        obj[NameObject("/Rect")] = ArrayObject(
            [FloatObject(v) for v in (lines[lo][0], y - 0.30 * sz,
                                      lines[lo][1], y + 0.85 * sz)])
        moved += 1
    return moved


def _norm(text):
'''))

# ---- 2. _expanded_block respects avoid rects ------------------------------
R.append((
"""    neighbour = max(below, key=lambda o: o["first_y"]) if below else None
    room = (neighbour["first_y"] + 0.75 * neighbour["size"] + MIRROR_GAP) \\
        if neighbour else 24.0
    out = dict(block)
""",
"""    neighbour = max(below, key=lambda o: o["first_y"]) if below else None
    room = (neighbour["first_y"] + 0.75 * neighbour["size"] + MIRROR_GAP) \\
        if neighbour else 24.0
    for r in block.get("avoid") or []:
        if r[2] <= block["x0"] + 1:
            continue
        if r[1] < top and r[3] > bottom:
            if r[0] < right:
                right = min(right, r[0] - 2.0)
        elif r[3] <= bottom + 1 and r[0] < right:
            room = max(room, r[3] + MIRROR_GAP)
    out = dict(block)
"""))

# ---- 3. _draw_blocks records placed line rects ----------------------------
R.append((
"""def _draw_blocks(doc, blocks, trans, all_blocks, page_over, key,
                 min_scale, floor_abs, tight, expand):
    smallest = 99.0
""",
"""def _draw_blocks(doc, blocks, trans, all_blocks, page_over, key,
                 min_scale, floor_abs, tight, expand, placed=None):
    from reportlab.pdfbase import pdfmetrics as _pm
    smallest = 99.0
"""))
R.append((
"""        doc.setFillColor(HexColor(block.get("color") or "#000000"))
        y = block["first_y"]
        for line in lines:
            if line:
                to = doc.beginText(block["x0"], y)
                to.setFont(font, size)
                if hscale != 100:
                    to.setHorizScale(hscale)
                to.textOut(line)
                doc.drawText(to)
            y -= size * lead
    return smallest
""",
"""        doc.setFillColor(HexColor(block.get("color") or "#000000"))
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
"""))

# ---- 4. mirror assembly: move links after merge ---------------------------
R.append((
"""                page_over = []
                used = [99.0]
                draw_list = p["draw"]
                if draw_list:
                    def drawer(doc, draw_list=draw_list, p=p,
                               page_over=page_over, used=used):
                        used[0] = _draw_blocks(
                            doc, draw_list, p["trans"], p["all"],
                            page_over, p["key"], MIRROR_SCALE,
                            MIRROR_FLOOR, True, True)
                    page.merge_page(_overlay_page(width, height, drawer))
                    min_font[0] = min(min_font[0], used[0])
""",
"""                page_over = []
                used = [99.0]
                placed = []
                draw_list = p["draw"]
                if draw_list:
                    def drawer(doc, draw_list=draw_list, p=p,
                               page_over=page_over, used=used,
                               placed=placed):
                        used[0] = _draw_blocks(
                            doc, draw_list, p["trans"], p["all"],
                            page_over, p["key"], MIRROR_SCALE,
                            MIRROR_FLOOR, True, True, placed)
                    page.merge_page(_overlay_page(width, height, drawer))
                    min_font[0] = min(min_font[0], used[0])
                    _move_links(page, draw_list, placed)
"""))

# ---- 5. inplace assembly: move links too ----------------------------------
R.append((
"""                def drawer(doc, p=p, page_over=page_over, key=key,
                           min_scale=min_scale):
                    _draw_blocks(doc, p["all"], p["trans"], p["all"],
                                 page_over, key, min_scale, 5.0,
                                 False, False)
                page.merge_page(_overlay_page(width, height, drawer))
                overflow.extend(page_over)
""",
"""                placed = []

                def drawer(doc, p=p, page_over=page_over, key=key,
                           min_scale=min_scale, placed=placed):
                    _draw_blocks(doc, p["all"], p["trans"], p["all"],
                                 page_over, key, min_scale, 5.0,
                                 False, False, placed)
                page.merge_page(_overlay_page(width, height, drawer))
                _move_links(page, p["all"], placed)
                overflow.extend(page_over)
"""))

# ---- 6. relink internal dests at finish -----------------------------------
R.append((
"""    def finish_file(out):
        count = _copy_outline(src, out, mapping, custom)
""",
"""    def finish_file(out):
        _relink_annots(src, out, mapping)
        count = _copy_outline(src, out, mapping, custom)
"""))

# ---- 7. t_start stores avoid rects ----------------------------------------
R.append((
"""            samples = _text_colors(page)
            ready_out = []
""",
"""            samples = _text_colors(page)
            imgs = _image_rects(page)
            ready_out = []
"""))
R.append((
"""                item["color"] = _block_color(item, samples)
                ready_out.append(item)
""",
"""                item["color"] = _block_color(item, samples)
                item["avoid"] = _avoid_for(item, imgs)
                ready_out.append(item)
"""))
R.append((
"""                item["color"] = _block_color(item, samples)
                locked_out.append(item)
""",
"""                item["color"] = _block_color(item, samples)
                item["avoid"] = _avoid_for(item, imgs)
                locked_out.append(item)
"""))

# ---- 8. _render refreshes avoid rects for old jobs -------------------------
R.append((
"""        if ready:
            samples = _text_colors(src.pages[index])
            for block in all_blocks:
                block["color"] = _block_color(block, samples)
""",
"""        if ready:
            samples = _text_colors(src.pages[index])
            imgs = _image_rects(src.pages[index])
            for block in all_blocks:
                block["color"] = _block_color(block, samples)
                block["avoid"] = _avoid_for(block, imgs)
"""))

# ---- 9. new selftest guards ------------------------------------------------
R.append((
"""        if placed:
            tools.call("board_delete", {"uids": placed})
""",
'''        # ---- v9a guard: text never grows into a photo --------------------
        buf = _io.BytesIO()
        cv = _cv.Canvas(buf, pagesize=(297, 420))
        cv.setFont(_font_name(), 10)
        cv.drawString(30, 330, "Short line beside the photo.")
        cv.drawString(30, 318, "Another short line here.")
        cv.drawString(30, 306, "Third short line of text.")
        cv.showPage()
        cv.save()
        w = PdfWriter(clone_from=_PR(_io.BytesIO(buf.getvalue())))
        img = DecodedStreamObject()
        img.set_data(b"\\xff\\x00\\x00")
        img[_NO("/Type")] = _NO("/XObject")
        img[_NO("/Subtype")] = _NO("/Image")
        img[_NO("/Width")] = FloatObject(1)
        img[_NO("/Height")] = FloatObject(1)
        img[_NO("/ColorSpace")] = _NO("/DeviceRGB")
        img[_NO("/BitsPerComponent")] = FloatObject(8)
        iref = w._add_object(img)
        pg = w.pages[0]
        res = _DO(pg.get("/Resources") or {})
        res[_NO("/XObject")] = _DO({_NO("/Im0"): iref})
        pg[_NO("/Resources")] = res
        cs = ContentStream(pg.get_contents(), w)
        cs.operations.append(([FloatObject(v) for v in
                               (120, 0, 0, 90, 150, 250)], b"cm"))
        cs.operations.append(([_NO("/Im0")], b"Do"))
        pg.replace_contents(cs)
        _save(w, p("photo.pdf"))
        st_ph = tools.call("board_pdf_translate_start",
                           {"path": p("photo.pdf"), "restart": True})
        phid = st_ph.get("job")
        jph = job_of(phid)
        expect("photo obstacle stored",
               any(b.get("avoid") for b in jph["blocks"].get("1", [])))
        send_ph = {b["id"]: ("\\u041e\\u0447\\u0435\\u043d\\u044c \\u0434\\u043b\\u0438\\u043d\\u043d\\u0430\\u044f "
                            "\\u0440\\u0443\\u0441\\u0441\\u043a\\u0430\\u044f \\u0441\\u0442\\u0440\\u043e\\u043a\\u0430, "
                            "\\u043a\\u043e\\u0442\\u043e\\u0440\\u0430\\u044f \\u043d\\u0435 \\u0434\\u043e\\u043b\\u0436\\u043d\\u0430 "
                            "\\u043b\\u0435\\u0437\\u0442\\u044c \\u043d\\u0430 \\u0444\\u043e\\u0442\\u043e\\u0433\\u0440\\u0430\\u0444\\u0438\\u044e.")
                   for b in jph["blocks"].get("1", [])}
        tools.call("board_pdf_translate_fill",
                   {"job": phid, "page": 1, "blocks": send_ph,
                    "on_overflow": "facing"})
        fin_ph = tools.call("board_pdf_translate_finish",
                            {"job": phid, "out": p("photo_ru.pdf"),
                             "overwrite": True, "place": False})
        show("finish photo guard", fin_ph, "pages", "mirror")
        expect("photo one page", fin_ph.get("pages") == 1)
        if fin_ph.get("ok"):
            psp = _page_spans(_reader(p("photo_ru.pdf")).pages[0])
            cross = [s for s in psp
                     if 249 < s["y"] < 341 and s["x"] + s["w"] > 149]
            expect("text keeps off photo", not cross)
        # ---- v9a guard: links follow text and dests stay live ------------
        buf = _io.BytesIO()
        cv = _cv.Canvas(buf, pagesize=(297, 420))
        cv.setFont(_font_name(), 10)
        cv.drawString(40, 380, "Visit http://example.com today.")
        cv.showPage()
        cv.drawString(40, 380, "Target page.")
        cv.showPage()
        cv.save()
        w = PdfWriter(clone_from=_PR(_io.BytesIO(buf.getvalue())))
        from pypdf.generic import (ArrayObject as _AO,
                                   FloatObject as _FO,
                                   TextStringObject as _TS)
        p0 = w.pages[0]
        uri = _DO({_NO("/Type"): _NO("/Annot"),
                   _NO("/Subtype"): _NO("/Link"),
                   _NO("/Rect"): _AO([_FO(v) for v in (90, 375, 200, 392)]),
                   _NO("/A"): _DO({_NO("/S"): _NO("/URI"),
                                   _NO("/URI"): _TS("http://example.com")})})
        goto = _DO({_NO("/Type"): _NO("/Annot"),
                    _NO("/Subtype"): _NO("/Link"),
                    _NO("/Rect"): _AO([_FO(v) for v in (40, 375, 80, 392)]),
                    _NO("/Dest"): _AO([w.pages[1].indirect_reference,
                                       _NO("/Fit")])})
        p0[_NO("/Annots")] = _AO([w._add_object(uri), w._add_object(goto)])
        _save(w, p("links.pdf"))
        st_l = tools.call("board_pdf_translate_start",
                          {"path": p("links.pdf"), "restart": True})
        lid = st_l.get("job")
        jl = job_of(lid)
        send_l = {b["id"]: ("\\u041f\\u043e\\u0441\\u0435\\u0442\\u0438\\u0442\\u0435 "
                           "http://example.com \\u0441\\u0435\\u0433\\u043e\\u0434\\u043d\\u044f.")
                  for b in jl["blocks"].get("1", [])}
        tools.call("board_pdf_translate_fill",
                   {"job": lid, "pages": [{"page": 1, "blocks": send_l},
                                          {"page": 2, "keep": True}]})
        fin_l = tools.call("board_pdf_translate_finish",
                           {"job": lid, "out": p("links_ru.pdf"),
                            "overwrite": True, "place": False})
        show("finish links guard", fin_l, "pages", "mirror")
        expect("links two pages", fin_l.get("pages") == 2)
        if fin_l.get("ok"):
            lr = _reader(p("links_ru.pdf"))
            live = _page_ids(lr)
            a0 = _annots_of(lr.pages[0])
            g = next((o for o in a0 if o.get("/Dest") is not None), None)
            expect("goto dest live",
                   g is not None and isinstance(g["/Dest"][0], IndirectObject)
                   and g["/Dest"][0].idnum in live)
            u = next((o for o in a0 if o.get("/A") is not None), None)
            lsp = _page_spans(lr.pages[0])
            url = next((s for s in lsp if "http" in s["text"]), None)
            ok_rect = False
            if u is not None and url is not None:
                r = [float(v) for v in u["/Rect"]]
                ok_rect = (r[0] < url["x"] + url["w"] and r[2] > url["x"]
                           and r[1] < url["y"] + url["size"]
                           and r[3] > url["y"] - 3)
            expect("uri rect on new text", ok_rect)
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
print("lines: %d -> %d" % (orig_lines, text.count("\n") + 1))
print("replacements applied:", len(R))