# dev\make_v8.py - build engine v8 from the good v7 on disk.
# Replaces only the listed functions; keeps everything else byte-intact.
import io
import os
import re
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PATH = os.path.join("refsynapse", "ai", "board_pdf_translate_tools.py")
BACKUP = os.path.join("dev", "board_pdf_translate_tools.v7.py")

src = io.open(PATH, "r", encoding="utf-8", newline="").read()
if "\r\n" in src:
    src = src.replace("\r\n", "\n")
old_lines = src.count("\n") + 1

if not os.path.isfile(BACKUP):
    shutil.copyfile(PATH, BACKUP)
    print("backup ->", BACKUP)
else:
    print("backup exists ->", BACKUP)


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
    print("replaced", name)
    return text[:start] + new.rstrip("\n") + "\n\n" + text[end:]


NEW_PRIVATE = r'''def _private_resources(owner, writer):
    """Give this page/form its own copies of every form it uses.
    v8: resources may be inherited from the Pages tree - resolve them
    before cloning, so containers are never missed.
    """
    res = _resolve(owner.get("/Resources"))
    if res is None:
        parent = owner.get("/Parent")
        hops = 0
        while res is None and parent is not None and hops < 32:
            node = _resolve(parent)
            if node is None:
                break
            res = _resolve(node.get("/Resources"))
            parent = node.get("/Parent")
            hops += 1
    res = res or DictionaryObject()
    new_res = DictionaryObject(res)
    xo = _resolve(res.get("/XObject")) or {}
    if xo:
        new_xo = DictionaryObject()
        for name, ref in xo.items():
            obj = _resolve(ref)
            try:
                is_form = obj.get("/Subtype") == "/Form"
            except Exception:
                is_form = False
            if is_form:
                clone = DecodedStreamObject()
                try:
                    clone.set_data(obj.get_data())
                except Exception:
                    new_xo[NameObject(name)] = ref
                    continue
                for key, value in obj.items():
                    if key not in ("/Length", "/Filter"):
                        clone[NameObject(key)] = value
                new_xo[NameObject(name)] = writer._add_object(clone)
            else:
                new_xo[NameObject(name)] = ref
        new_res[NameObject("/XObject")] = new_xo
    owner[NameObject("/Resources")] = new_res
    return new_res
'''

NEW_REPLAY = r'''def _replay(operations, ctm, fill, res, pdf, writer, mode,
          trans_rects, lock_rects, samples, depth=0, seen=None):
    """Replay drawing commands, walking INTO form containers.
    mode 'surgical': drop show commands inside translated rects (locked
    rects win); mode 'all': drop every show command; mode 'colors': keep
    everything and record (x, y, fill) of every show command.
    v8: deeper recursion (16); the cycle guard is per-branch, so a form
    used several times is rewritten at every placement, not only the
    first one.
    """
    if seen is None:
        seen = set()
    ident = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    tm, tlm = ident[:], ident[:]
    stack = []
    leading, rise = 0.0, 0.0
    kept = []
    for operands, op in operations:
        try:
            if op == b"q":
                stack.append((ctm[:], fill))
            elif op == b"Q":
                if stack:
                    ctm, fill = stack.pop()
            elif op == b"cm":
                ctm = _mat_mul([float(v) for v in operands], ctm)
            elif op == b"g":
                fill = _hex_of([float(operands[0])] * 3)
            elif op == b"rg":
                fill = _hex_of([float(v) for v in operands[:3]])
            elif op == b"k":
                fill = _cmyk_hex(*[float(v) for v in operands[:4]])
            elif op in (b"sc", b"scn"):
                vals = []
                for v in operands:
                    try:
                        vals.append(float(v))
                    except Exception:
                        vals = []
                        break
                if len(vals) == 1:
                    fill = _hex_of(vals * 3)
                elif len(vals) == 3:
                    fill = _hex_of(vals)
                elif len(vals) == 4:
                    fill = _cmyk_hex(*vals)
            elif op == b"BT":
                tm, tlm = ident[:], ident[:]
            elif op == b"Tm":
                tm = tlm = [float(v) for v in operands]
            elif op in (b"Td", b"TD"):
                tx, ty = float(operands[0]), float(operands[1])
                if op == b"TD":
                    leading = -ty
                tlm = _mat_mul([1, 0, 0, 1, tx, ty], tlm)
                tm = tlm[:]
            elif op == b"T*":
                tlm = _mat_mul([1, 0, 0, 1, 0, -leading], tlm)
                tm = tlm[:]
            elif op == b"TL":
                leading = float(operands[0])
            elif op == b"Ts":
                rise = float(operands[0])
            elif op == b"Do" and depth < 16:
                name = str(operands[0])
                xo = _resolve(res.get("/XObject")) or {}
                form = _resolve(xo.get(name))
                walk = False
                try:
                    walk = (form is not None
                            and form.get("/Subtype") == "/Form")
                except Exception:
                    walk = False
                fid = id(form)
                if walk and fid in seen:
                    walk = False
                elif walk and mode == "colors":
                    seen.add(fid)
                if walk:
                    try:
                        if writer is not None and mode != "colors":
                            child_res = _private_resources(form, writer)
                            child_seen = seen | {fid}
                        else:
                            child_res = (_resolve(form.get("/Resources"))
                                         or res)
                            child_seen = (seen if mode == "colors"
                                          else seen | {fid})
                        inner = ContentStream(form, pdf).operations
                        kept_child = _replay(inner, ctm[:], fill,
                                             child_res, pdf, writer, mode,
                                             trans_rects, lock_rects,
                                             samples, depth + 1, child_seen)
                        if writer is not None and mode != "colors":
                            cs = ContentStream(None, pdf)
                            cs.operations = kept_child
                            form.set_data(cs.get_data())
                    except Exception:
                        pass
            elif op in SHOW_OPS:
                if op in (b"'", b'"'):
                    tlm = _mat_mul([1, 0, 0, 1, 0, -leading], tlm)
                    tm = tlm[:]
                device = _mat_mul(tm, ctm)
                x, y = device[4], device[5] + rise * ctm[3]
                if mode == "colors":
                    samples.append((x, y, fill))
                elif mode == "all":
                    continue
                elif any(_in_rect(r, x, y) for r in lock_rects):
                    pass
                elif any(_in_rect(r, x, y) for r in trans_rects):
                    continue
        except Exception:
            pass
        kept.append((operands, op))
    return kept
'''

NEW_STRIP = r'''def _text_free(operations):
    """Structural removal: drop whole BT...ET text objects."""
    out, depth = [], 0
    for operands, op in operations:
        if op == b"BT":
            depth += 1
            continue
        if op == b"ET":
            if depth:
                depth -= 1
            continue
        if depth == 0:
            out.append((operands, op))
    return out


def _forms_text_free(res, writer, seen=None, depth=0):
    """Walk EVERY form by resources (not by Do calls) and remove text.
    Each level gets private clones first, so shared forms stay safe.
    """
    if seen is None:
        seen = set()
    if depth > 16:
        return
    xo = _resolve(res.get("/XObject")) or {}
    for name, ref in xo.items():
        form = _resolve(ref)
        try:
            if form is None or form.get("/Subtype") != "/Form":
                continue
        except Exception:
            continue
        if id(form) in seen:
            continue
        seen.add(id(form))
        try:
            child_res = _private_resources(form, writer)
            ops = ContentStream(form, writer).operations
            cs = ContentStream(None, writer)
            cs.operations = _text_free(ops)
            form.set_data(cs.get_data())
            _forms_text_free(child_res, writer, seen, depth + 1)
        except Exception:
            pass


def _strip_page(writer, index):
    """Remove every text-showing command on the page AND in its forms.
    v8: after the replay pass, whole text objects are removed structurally
    in the page stream and in every container reachable by resources.
    """
    page = writer.pages[index]
    contents = page.get_contents()
    if contents is None:
        return False
    res = _private_resources(page, writer)
    stream = ContentStream(contents, writer)
    kept = _replay(stream.operations, [1, 0, 0, 1, 0, 0], "#000000", res,
                   writer, writer, "all", [], [], [])
    kept = _text_free(kept)
    _forms_text_free(res, writer)
    cs = ContentStream(None, writer)
    cs.operations = kept
    page.replace_contents(cs)
    return True
'''

NEW_SURGICAL = r'''def _block_needles(block, forbidden):
    out = []
    for line in str(block.get("text", "")).split("\n"):
        needle = _norm(line)[:60]
        if len(needle) < 4:
            continue
        if any(needle in f for f in forbidden):
            continue
        out.append(needle)
    return out


def _wording_gone(page, blocks, forbidden):
    try:
        after = _norm(page.extract_text() or "")
    except Exception:
        return False
    for block in blocks:
        for needle in _block_needles(block, forbidden):
            if needle in after:
                return False
    return True


def _surgical_strip(writer, index, trans_blocks, locked_blocks):
    """Remove ONLY the translated wording, on the page and inside forms.
    v8: EVERY line of EVERY translated block must vanish - zero tolerance.
    Lines that also occur in kept wording are not used as evidence.
    """
    page = writer.pages[index]
    contents = page.get_contents()
    if contents is None:
        return False
    forbidden = [_norm(b.get("text", "")) for b in locked_blocks]
    trans_rects = [_rect_of(b) for b in trans_blocks]
    lock_rects = [_rect_of(b) for b in locked_blocks]
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

NEW_EXPANDED = r'''def _expanded_block(block, all_blocks):
    """Room below the block, stopping 2 pt ABOVE the neighbour's top.
    v8 fix: v7 stopped 2 pt BELOW the top, which allowed real overlaps.
    """
    top = block["first_y"] + 0.75 * block["size"]
    below = [o for o in all_blocks
             if o is not block
             and o["first_y"] + 0.75 * o["size"] < top - 1.0
             and o["x0"] < block.get("x_room", block["x1"])
             and block["x0"] < o.get("x_room", o["x1"])]
    neighbour = max(below, key=lambda o: o["first_y"]) if below else None
    room = (neighbour["first_y"] + 0.75 * neighbour["size"] + 2.0) \
        if neighbour else 24.0
    out = dict(block)
    out["y_bot"] = min(block["y_bot"], room)
    return out
'''

NEW_FIT = r'''def _fit_block(block, text, font, min_scale, floor_abs=5.0, tight=False):
    """Word wrap into the rectangle; the mirror fitting ladder.
    Returns (lines, size, overflow, leading, hscale). Ladder: shrink to
    min_scale, tighten leading to 1.0, shrink to floor_abs, then squeeze
    horizontally (mirror only).
    v8: in tight (mirror) mode there is NO baseline slack - the last
    line's descender must stay at or above y_bot, which already includes
    the 2 pt gap to the neighbour below.
    """
    right = float(block.get("x_room", block["x1"]))
    width = max(right - block["x0"] + 2.0, 8.0)
    size0 = block["size"]
    soft = max(floor_abs, size0 * min_scale)
    hard = floor_abs if tight else soft
    size = size0
    lead = float(block["leading"])
    hscale = 100
    slack = 0.0 if tight else 1.5
    while True:
        lines = _wrap(text, font, size, width * 100.0 / hscale)
        step = size * lead
        bottom = block["first_y"] - (len(lines) - 1) * step - 0.30 * size
        overflow = bottom < block["y_bot"] - slack
        if not overflow:
            return lines, size, False, lead, hscale
        if size > soft + 1e-6:
            size = max(soft, size - 0.25)
            continue
        if tight and lead > 1.0 + 1e-6:
            lead = max(1.0, lead - 0.05)
            continue
        if size > hard + 1e-6:
            size = max(hard, size - 0.25)
            continue
        if tight and hscale > MIRROR_SQUEEZE[-1]:
            hscale = MIRROR_SQUEEZE[MIRROR_SQUEEZE.index(hscale) + 1]
            continue
        return lines, size, True, lead, hscale
'''

NEW_RENDER = r'''def _render(job, folder, target, partial_ok, meta_title=None, custom=None,
            facing_style="mirror"):
    """Rebuild the result. Mirror NEVER changes the page count.
    mirror: surgical lift-out (page + containers), verified line by line
    with zero tolerance; identical translations are never touched; if
    unverifiable, full strip (replay + structural, every container) and
    redraw of wording and readable labels, verified again; if even that
    fails, the original page ships untouched and is reported.
    append / replace: only on explicit user request.
    """
    src = _reader(job["source"])
    base = _reader(os.path.join(folder, "base.pdf"))
    mirror = facing_style == "mirror"
    surg = PdfWriter(clone_from=src) if mirror else None
    writer = PdfWriter()
    overflow, facing, mapping = [], [], {}
    mirrored, fallback, redrawn, unplaced = [], [], [], []
    min_font = [99.0]
    min_scale = float(job.get("min_scale", 0.7))
    for index in range(job["pages_total"]):
        key = str(index + 1)
        mode = job["mode"].get(key, "kept")
        state = job["status"].get(key)
        mapping[index] = len(writer.pages)
        ready = state == "done"
        trans = job["translations"].get(key, {})
        t_blocks = job["blocks"].get(key, [])
        k_blocks = job["locked"].get(key, [])
        all_blocks = t_blocks + k_blocks
        samples = _text_colors(src.pages[index]) if ready else []
        for block in all_blocks:
            block["color"] = _block_color(block, samples)
        if ready and mode in ("inplace", "facing") and mirror:
            page_over = []
            used = [99.0]
            ok = True
            candidate = None
            if mode == "inplace":
                candidate = base.pages[index]
                draw_list = all_blocks
            else:
                changed, same = [], []
                for block in t_blocks:
                    now = trans.get(block["id"], block["text"])
                    if _norm(now) == _norm(block["text"]):
                        same.append(block)
                    else:
                        changed.append(block)
                lock_all = k_blocks + same
                draw_list = changed
                if changed:
                    ok = _surgical_strip(surg, index, changed, lock_all)
                if ok:
                    candidate = surg.pages[index]
                else:
                    # last honest resort: clean the whole page (text
                    # only, page + every container) and redraw wording
                    # plus readable labels - then verify again
                    try:
                        ok = _strip_page(surg, index)
                    except Exception:
                        ok = False
                    if ok:
                        ok = _wording_gone(surg.pages[index], changed, [])
                    if ok:
                        candidate = surg.pages[index]
                        draw_list = changed + same + [
                            b for b in k_blocks if _readable_locked(b)]
                        redrawn.append(index + 1)
            if ok and candidate is not None:
                writer.add_page(candidate)
                if draw_list:
                    page = writer.pages[-1]
                    width = float(page.mediabox.width)
                    height = float(page.mediabox.height)

                    def drawer(doc, draw_list=draw_list, trans=trans,
                               all_blocks=all_blocks, page_over=page_over,
                               key=key, used=used):
                        used[0] = _draw_blocks(doc, draw_list, trans,
                                               all_blocks, page_over, key,
                                               MIRROR_SCALE, MIRROR_FLOOR,
                                               True, True)
                    page.merge_page(_overlay_page(width, height, drawer))
                    min_font[0] = min(min_font[0], used[0])
                mirrored.append(index + 1)
                if page_over:
                    unplaced.append(index + 1)
                    overflow.extend(page_over)
                continue
            # even full strip impossible: ship the original, same count
            fallback.append(index + 1)
            writer.add_page(src.pages[index])
            continue
        if mode == "inplace" and ready:
            writer.add_page(base.pages[index])
            page = writer.pages[-1]
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            page_over = []

            def drawer(doc, blocks=all_blocks, trans=trans,
                       page_over=page_over, key=key, min_scale=min_scale):
                _draw_blocks(doc, blocks, trans, all_blocks, page_over, key,
                             min_scale, 5.0, False, False)
            page.merge_page(_overlay_page(width, height, drawer))
            overflow.extend(page_over)
        elif mode == "facing" and ready:
            box = src.pages[index].mediabox
            extra = _facing_pages(job, key, index + 1,
                                  float(box.width), float(box.height))
            replaced = facing_style == "replace"
            if not replaced:
                writer.add_page(src.pages[index])
            for piece in extra:
                writer.add_page(piece)
            facing.append({"page": index + 1, "added": len(extra),
                           "replaced": replaced})
        else:
            writer.add_page(src.pages[index])
    marks = _copy_outline(src, writer, mapping, custom)
    if meta_title:
        writer.add_metadata({"/Title": meta_title})
    _save(writer, target)
    mirror_info = {"requested": mirror, "mirrored": mirrored,
                   "fallback": fallback, "redrawn": redrawn,
                   "unplaced": sorted(set(unplaced)),
                   "min_font": round(min_font[0], 2) if mirrored else None}
    return overflow, marks, facing, len(writer.pages), mirror_info
'''

GUARD = r'''        # ---- v8 guard: vertical clearance to the neighbour below -------
        buf = _io.BytesIO()
        cv = _cv.Canvas(buf, pagesize=(297, 420))
        cv.setFont(_font_name(), 12)
        cv.drawString(40, 380, "TECHNICAL SPECIFICATIONS OF THE DEVICE")
        cv.drawString(40, 366, chr(0x2022) + " Parameter value table header")
        cv.showPage()
        cv.save()
        with open(p("overlap.pdf"), "wb") as stream:
            stream.write(buf.getvalue())
        st_o = tools.call("board_pdf_translate_start",
                          {"path": p("overlap.pdf"), "restart": True})
        oid = st_o.get("job")
        jo = job_of(oid)
        send_o = {}
        for b in jo["blocks"].get("1", []):
            if b["text"].startswith("TECHNICAL"):
                send_o[b["id"]] = ("ТЕХНИЧЕСКИЕ ХАРАКТЕРИСТИКИ "
                                   "УСТРОЙСТВА И КОМПЛЕКТУЮЩИХ")
            else:
                send_o[b["id"]] = "Шапка таблицы параметров и значений"
        tools.call("board_pdf_translate_fill",
                   {"job": oid, "page": 1, "blocks": send_o,
                    "on_overflow": "facing"})
        fin_o = tools.call("board_pdf_translate_finish",
                           {"job": oid, "out": p("overlap_ru.pdf"),
                            "overwrite": True, "place": False})
        show("finish overlap guard", fin_o, "pages", "mirror")
        expect("overlap guard one page", fin_o.get("pages") == 1)
        jo = job_of(oid)
        allb = jo["blocks"].get("1", []) + jo["locked"].get("1", [])
        tr = jo["translations"].get("1", {})
        clash = 0
        for b in jo["blocks"].get("1", []):
            t = tr.get(b["id"], b["text"])
            tgt = _expanded_block(b, allb)
            ls, size, over, lead, hs = _fit_block(
                tgt, t, _block_font(b), MIRROR_SCALE,
                floor_abs=MIRROR_FLOOR, tight=True)
            bottom = b["first_y"] - (len(ls) - 1) * size * lead - 0.30 * size
            for o in allb:
                if o is b:
                    continue
                top = o["first_y"] + 0.75 * o["size"]
                if top < b["first_y"] and bottom < top + 1.9 \
                   and o["x0"] < b.get("x_room", b["x1"]) \
                   and b["x0"] < o.get("x_room", o["x1"]):
                    clash += 1
        expect("no vertical clash", clash == 0)
        # ---- v8 guard: identical translation is left untouched ---------
        buf = _io.BytesIO()
        cv = _cv.Canvas(buf, pagesize=(297, 420))
        cv.setFont(_font_name(), 12)
        cv.drawString(40, 380, "Уже русский текст.")
        cv.showPage()
        cv.save()
        with open(p("same.pdf"), "wb") as stream:
            stream.write(buf.getvalue())
        st_s = tools.call("board_pdf_translate_start",
                          {"path": p("same.pdf"), "restart": True})
        sid = st_s.get("job")
        js = job_of(sid)
        sblocks = js["blocks"].get("1", [])
        expect("identical source blocks", len(sblocks) >= 1)
        tools.call("board_pdf_translate_fill",
                   {"job": sid, "page": 1,
                    "blocks": {b["id"]: b["text"] for b in sblocks}})
        fin_s = tools.call("board_pdf_translate_finish",
                           {"job": sid, "out": p("same_ru.pdf"),
                            "overwrite": True, "place": False})
        show("finish identical", fin_s, "pages", "mirror")
        expect("identical one page", fin_s.get("pages") == 1)
        if fin_s.get("ok"):
            st_text = _reader(p("same_ru.pdf")).pages[0].extract_text() or ""
            expect("identical kept once", st_text.count("Уже русский") == 1)
'''

src = replace_func(src, "_private_resources", NEW_PRIVATE)
src = replace_func(src, "_replay", NEW_REPLAY)
src = replace_func(src, "_strip_page", NEW_STRIP)
src = replace_func(src, "_surgical_strip", NEW_SURGICAL)
src = replace_func(src, "_expanded_block", NEW_EXPANDED)
src = replace_func(src, "_fit_block", NEW_FIT)
src = replace_func(src, "_render", NEW_RENDER)

a = "batch 3, engine v7."
if src.count(a) != 1:
    raise SystemExit("ANCHOR FAIL docstring title")
src = src.replace(a, "batch 3, engine v8.", 1)
a = "PARTIAL ACCEPT (v4)."
if src.count(a) != 1:
    raise SystemExit("ANCHOR FAIL docstring note")
note = ("V8 RELIABILITY. Every translated line is verified gone after the\n"
        "lift-out (zero tolerance); identical translations are never\n"
        "touched; the full-strip fallback walks every container by\n"
        "resources and removes whole text objects, then verifies again.\n"
        "Mirror fitting keeps a 2 pt vertical gap above the neighbour\n"
        "below, with no baseline slack.\n")
src = src.replace(a, note + a, 1)
print("docstring updated")

a = '        if placed:\n            tools.call("board_delete", {"uids": placed})'
if src.count(a) != 1:
    raise SystemExit("ANCHOR FAIL selftest cleanup")
src = src.replace(a, GUARD.rstrip("\n") + "\n" + a, 1)
print("selftest guards inserted")

try:
    compile(src, PATH, "exec")
except SyntaxError as ex:
    raise SystemExit("SYNTAX FAIL line %s: %s" % (ex.lineno, ex.msg))

with io.open(PATH, "w", encoding="utf-8", newline="") as stream:
    stream.write(src)
print("lines %d -> %d" % (old_lines, src.count("\n") + 1))
print("SYNTAX OK")