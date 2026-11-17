# dev/make_v9c.py - anti-drift gate, cached analysis, per-line image obstacles.
import io, os, py_compile, shutil, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SRC = r"refsynapse\ai\board_pdf_translate_tools.py"
BAK = r"dev\board_pdf_translate_tools.v9b.py"
text = io.open(SRC, encoding="utf-8").read()
before = text.count("\n") + 1
if not os.path.isfile(BAK):
    shutil.copyfile(SRC, BAK)
    print("backup ->", BAK)
else:
    print("backup exists ->", BAK)

R = []

# 1. remember the ink of every original line
R.append((
'"font": block["font"],',
'"font": block["font"],\n'
'                       "line_rects": [[round(l["x0"], 2), round(l["y"], 2),\n'
'                                       round(l["x1"], 2)] for l in lines],'))

# 2. obstacles judged per LINE, not by the whole block box
R.append((
'''    ink = (block["x0"], block["y_bot"], block["x1"],
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
''',
'''    size = block["size"]
    rows = block.get("line_rects") or []
    if rows:
        inks = [(a, y - 0.30 * size, c, y + 0.85 * size)
                for a, y, c in rows]
    else:
        inks = [(block["x0"], block["y_bot"], block["x1"],
                 block["first_y"] + 0.85 * size)]
    out = []
    for r in imgs:
        if r[2] - r[0] < 6 or r[3] - r[1] < 6:
            continue
        sat = False
        for ink in inks:
            if ink[0] < r[2] - 1 and ink[2] > r[0] + 1 \\
               and ink[1] < r[3] - 1 and ink[3] > r[1] + 1:
                sat = True
                break
        if sat:
            continue
        out.append([round(v, 2) for v in r])
    return out
'''))

# 3. a photo on the right narrows the column for the whole block
R.append((
'''    for r in block.get("avoid") or []:
        if r[2] <= block["x0"] + 1:
            continue
        if r[1] < top and r[3] > bottom:
            if r[0] < right:
                right = min(right, r[0] - 2.0)
        elif r[3] <= bottom + 1 and r[0] < right:
            room = max(room, r[3] + MIRROR_GAP)
    out = dict(block)
    out["y_bot"] = min(bottom, room)
    out["x_room"] = round(max(block["x1"], right), 2)
    return out
''',
'''    hard_right = None
    for r in block.get("avoid") or []:
        if r[2] <= block["x0"] + 1:
            continue
        if r[1] < top and r[3] > bottom:
            if r[0] > block["x0"] + 24.0:
                edge = r[0] - 2.0
                hard_right = edge if hard_right is None \\
                    else min(hard_right, edge)
                right = min(right, edge)
        elif r[3] <= bottom + 1 and r[0] < right:
            room = max(room, r[3] + MIRROR_GAP)
    out = dict(block)
    out["y_bot"] = min(bottom, room)
    wide = max(block["x1"], right)
    if hard_right is not None:
        wide = max(block["x0"] + 24.0, min(wide, hard_right))
    out["x_room"] = round(wide, 2)
    return out
'''))

# 4. the alignment gate
R.append((
"def _fit_block(block, text, font, min_scale, floor_abs=5.0, tight=False):\n",
'''_ADDR_RE = re.compile(r"(?:https?://|www\\.)[^\\s]+"
                      r"|[A-Za-z0-9.-]+\\.(?:com|net|org|ru|io|co|tw|de)\\b")


def _addresses(text):
    return {m.group(0).rstrip(".,;:)]|").lower()
            for m in _ADDR_RE.finditer(text or "")}


def _plain_len(text):
    """Length without whitespace and without the dot leaders of contents."""
    bare = re.sub(r"[.\\u2026]{2,}", "", text or "")
    return len(re.sub(r"\\s+", "", bare))


def _suspect(block, value):
    """Does this wording look like the translation of ANOTHER block?
    Models drift along the id list: a caption receives the footer link,
    a paragraph receives 'page 5'. Such a pairing is refused, not drawn.
    """
    src = str(block.get("text", ""))
    a, b = _plain_len(src), _plain_len(value)
    if a >= 60 and b < 0.45 * a:
        return "far too short for this block"
    if a <= 40 and b > 3.0 * a + 25:
        return "far too long for this block"
    sa, sb = _addresses(src), _addresses(value)
    if sa and not (sa & sb):
        return "the address of this block is missing"
    if sb - sa:
        return "carries an address owned by another block"
    if a <= 40 and re.search(r"\\d", src) and not re.search(r"\\d", value):
        return "the number of this block is missing"
    return ""


def _fit_block(block, text, font, min_scale, floor_abs=5.0, tight=False):
'''))

# 5. one analysis per source page, reused by every later call
R.append((
"def _render(job, folder, target, partial_ok, meta_title=None, custom=None,\n",
'''_ANALYSIS = {}


def _analysis(path, index, page):
    """Colour samples, colour runs and image rects of a SOURCE page.
    They never change during a job, so they are measured once."""
    try:
        stamp = (os.path.getmtime(path), os.path.getsize(path))
    except OSError:
        stamp = (0, 0)
    key = (path, stamp, index)
    hit = _ANALYSIS.get(key)
    if hit is None:
        hit = (_text_colors(page), _color_runs(page), _image_rects(page))
        if len(_ANALYSIS) > 400:
            _ANALYSIS.clear()
        _ANALYSIS[key] = hit
    return hit


def _render(job, folder, target, partial_ok, meta_title=None, custom=None,
'''))

R.append((
'''        if ready:
            samples = _text_colors(src.pages[index])
            runs = _color_runs(src.pages[index])
            imgs = _image_rects(src.pages[index])
''',
'''        if ready:
            samples, runs, imgs = _analysis(job["source"], index,
                                            src.pages[index])
'''))

R.append((
'''            samples = _text_colors(page)
            runs = _color_runs(page)
            imgs = _image_rects(page)
''',
'''            samples, runs, imgs = _analysis(src, index, page)
'''))

# 6. gate wiring inside t_fill
R.append((
"        rejected, notes, touched = [], [], []\n",
"        rejected, notes, touched = [], [], []\n"
"        misaligned = []\n"))

R.append((
'''            if job["mode"].get(key) == "inplace":
                known = {b["id"]: b for b in job["blocks"].get(key, [])}
                flip = False
                for bid, value in texts.items():
                    block = known[bid]
                    lines, size, over, lead, hs = _fit_block(
                        block, value, _block_font(block), min_scale)
                    if not over:
                        accepted[bid] = value
                        page_rejects.pop(bid, None)
                        continue
''',
'''            if job["mode"].get(key) == "inplace":
                known = {b["id"]: b for b in job["blocks"].get(key, [])}
                flip = False
                guards = job.setdefault("misalign", {}).setdefault(key, {})
                for bid, value in texts.items():
                    block = known[bid]
                    lines, size, over, lead, hs = _fit_block(
                        block, value, _block_font(block), min_scale)
                    if not over:
                        why = _suspect(block, value)
                        tried = int(guards.get(bid, 0))
                        if why and tried < 1:
                            guards[bid] = tried + 1
                            misaligned.append(
                                {"page": int(key), "id": bid,
                                 "reason": why,
                                 "original": block["text"][:70],
                                 "got": value[:70]})
                            continue
                        if why:
                            guards[bid] = tried + 1
                            notes.append("page %s block %s accepted on the "
                                         "second attempt although it looks "
                                         "misaligned" % (key, bid))
                        else:
                            guards.pop(bid, None)
                        accepted[bid] = value
                        page_rejects.pop(bid, None)
                        continue
'''))

# 7. no full rebuild after every batch of a big document
R.append((
'''        _dump_job(job, folder)
        overflow, marks, facing, pages, mirror_info = _render(
            job, folder, os.path.join(folder, "work.pdf"), partial_ok=True)
        payload = _payload(job, folder)
''',
'''        _dump_job(job, folder)
        busy = any(v == "pending" for v in job["status"].values())
        if job["pages_total"] <= 8 or not busy:
            overflow, marks, facing, pages, mirror_info = _render(
                job, folder, os.path.join(folder, "work.pdf"), partial_ok=True)
        else:
            overflow = []
        payload = _payload(job, folder)
'''))

R.append((
'''        payload["overflow"] = [o for o in overflow if o["page"] in touched]
        if rejected:
''',
'''        payload["overflow"] = [o for o in overflow if o["page"] in touched]
        payload["misaligned"] = misaligned
        if misaligned:
            payload["alignment_hint"] = (
                "These translations belong to OTHER blocks (the id list was "
                "shifted or copied). Nothing of them was saved. Read the "
                "'original' of every listed id and send the translation of "
                "THAT text. Never summarise, merge, split or reorder blocks.")
        if rejected:
'''))

R.append((
    "never invent ids. Colour accents:",
    "never invent ids. Every id gets the translation of ITS OWN text - "
    "never summarise, merge, split or shift blocks; wording that belongs "
    "to another block is refused and listed in 'misaligned'. "
    "Colour accents:"))

# 8. guards
R.append((
'''        if placed:
            tools.call("board_delete", {"uids": placed})
''',
'''        # ---- v9c guard: a shifted translation is refused ----------------
        buf = _io.BytesIO()
        cv = _cv.Canvas(buf, pagesize=(595, 420))
        cv.setFont(_font_name(), 10)
        cv.drawString(30, 380, "This section explains the setup of the "
                               "device in great detail and covers every step.")
        cv.drawString(30, 340, "Hard drive goes where?")
        cv.showPage()
        cv.save()
        with open(p("drift.pdf"), "wb") as stream:
            stream.write(buf.getvalue())
        st_dr = tools.call("board_pdf_translate_start",
                           {"path": p("drift.pdf"), "restart": True})
        drid = st_dr.get("job")
        jdr = job_of(drid)
        para = next((b for b in jdr["blocks"].get("1", [])
                     if b["text"].startswith("This section")), None)
        cap = next((b for b in jdr["blocks"].get("1", [])
                    if b["text"].startswith("Hard drive")), None)
        expect("drift blocks found", para is not None and cap is not None)
        if para and cap:
            shift = tools.call(
                "board_pdf_translate_fill",
                {"job": drid, "page": 1,
                 "blocks": {para["id"]: "\\u0421\\u0442\\u0440. 1",
                            cap["id"]: ("\\u041a\\u0443\\u0434\\u0430 \\u0434\\u0435\\u043b\\u0441\\u044f "
                                        "\\u0434\\u0438\\u0441\\u043a?")}})
            show("drift fill", shift, "misaligned", "progress")
            expect("shifted wording refused",
                   any(m["id"] == para["id"]
                       for m in shift.get("misaligned") or []))
            expect("shifted page still pending",
                   (shift.get("progress") or {}).get("pending") == 1)
            expect("alignment hint given", bool(shift.get("alignment_hint")))
            honest = tools.call(
                "board_pdf_translate_fill",
                {"job": drid, "page": 1,
                 "blocks": {para["id"]: ("\\u042d\\u0442\\u043e\\u0442 \\u0440\\u0430\\u0437\\u0434\\u0435\\u043b "
                                         "\\u043f\\u043e\\u0434\\u0440\\u043e\\u0431\\u043d\\u043e \\u043e\\u0431\\u044a\\u044f\\u0441\\u043d\\u044f\\u0435\\u0442 "
                                         "\\u043d\\u0430\\u0441\\u0442\\u0440\\u043e\\u0439\\u043a\\u0443 \\u0443\\u0441\\u0442\\u0440\\u043e\\u0439\\u0441\\u0442\\u0432\\u0430 "
                                         "\\u0438 \\u043e\\u0445\\u0432\\u0430\\u0442\\u044b\\u0432\\u0430\\u0435\\u0442 \\u0432\\u0441\\u0435 \\u0448\\u0430\\u0433\\u0438.")}})
            show("drift refill", honest, "misaligned", "progress")
            expect("honest wording accepted", not honest.get("misaligned"))
            expect("drift page done",
                   (honest.get("progress") or {}).get("pending") == 0)
        # ---- v9c guard: a paragraph wrapping around a photo -------------
        buf = _io.BytesIO()
        cv = _cv.Canvas(buf, pagesize=(400, 420))
        cv.setFont(_font_name(), 8)
        side = "Narrow line of text beside the photo here now."
        for k in range(6):
            cv.drawString(30, 380 - 10 * k, side)
        wide_row = ("Wide line below the photo that runs far to the right "
                    "edge of this page area today.")
        cv.drawString(30, 320, wide_row)
        cv.drawString(30, 310, wide_row)
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
                               (120, 0, 0, 60, 260, 330)], b"cm"))
        cs.operations.append(([_NO("/Im0")], b"Do"))
        pg.replace_contents(cs)
        _save(w, p("wrap.pdf"))
        st_w = tools.call("board_pdf_translate_start",
                          {"path": p("wrap.pdf"), "restart": True})
        wid = st_w.get("job")
        jw = job_of(wid)
        wb = jw["blocks"].get("1", [])
        expect("wrap block joined", len(wb) == 1)
        expect("wrap obstacle stored", bool(wb) and bool(wb[0].get("avoid")))
        long_wrap = ("\\u041e\\u0447\\u0435\\u043d\\u044c \\u0434\\u043b\\u0438\\u043d\\u043d\\u0430\\u044f \\u0440\\u0443\\u0441\\u0441\\u043a\\u0430\\u044f "
                     "\\u0441\\u0442\\u0440\\u043e\\u043a\\u0430 \\u043f\\u0440\\u043e \\u043d\\u0430\\u043a\\u043e\\u043f\\u0438\\u0442\\u0435\\u043b\\u0438, "
                     "\\u043a\\u043e\\u0442\\u043e\\u0440\\u0430\\u044f \\u043e\\u0431\\u044f\\u0437\\u0430\\u043d\\u0430 \\u043e\\u0441\\u0442\\u0430\\u0442\\u044c\\u0441\\u044f "
                     "\\u0441\\u043b\\u0435\\u0432\\u0430 \\u043e\\u0442 \\u0444\\u043e\\u0442\\u043e\\u0433\\u0440\\u0430\\u0444\\u0438\\u0438. ") * 3
        tools.call("board_pdf_translate_fill",
                   {"job": wid, "page": 1,
                    "blocks": {b["id"]: long_wrap for b in wb},
                    "on_overflow": "facing"})
        fin_w = tools.call("board_pdf_translate_finish",
                           {"job": wid, "out": p("wrap_ru.pdf"),
                            "overwrite": True, "place": False})
        show("finish wrap guard", fin_w, "pages", "mirror")
        expect("wrap one page", fin_w.get("pages") == 1)
        if fin_w.get("ok"):
            wsp = _page_spans(_reader(p("wrap_ru.pdf")).pages[0])
            over_img = [s for s in wsp
                        if 329 < s["y"] < 391 and s["x"] + s["w"] > 261]
            expect("wrap text off photo", not over_img)
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
print("replacements applied:", len(R))