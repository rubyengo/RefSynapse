# dev\make_v8c.py - v8c: verify on the SAVED file, reflow hard breaks,
# stop sideways growth into a neighbour's ink.
import io
import os
import re
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PATH = os.path.join("refsynapse", "ai", "board_pdf_translate_tools.py")
BACKUP = os.path.join("dev", "board_pdf_translate_tools.v8b.py")

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


def replace_func(text, name, new, label=None):
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
    print(label or ("replaced " + name))
    return text[:start] + new.rstrip("\n") + "\n\n" + text[end:]


NEW_STRIP = r'''def _freeze(writer, folder, name):
    """Save the working writer and read it back.
    Text extraction from an in-memory page can differ from extraction of
    the very same bytes after a save/load round trip (font and string
    objects are rebuilt). Every honesty check therefore runs on a file
    that was written and re-read, never on the in-memory writer.
    """
    import io
    from pypdf import PdfReader
    path = os.path.join(folder, name)
    _save(writer, path)
    with open(path, "rb") as stream:
        data = stream.read()
    return PdfReader(io.BytesIO(data))


def _set_contents(page, writer, operations):
    """Store page commands as plain bytes, not as a live ContentStream."""
    cs = ContentStream(None, writer)
    cs.operations = operations
    stream = DecodedStreamObject()
    stream.set_data(cs.get_data())
    page[NameObject("/Contents")] = writer._add_object(stream)


def _strip_page(writer, index):
    """Remove every text-showing command on the page AND in its forms."""
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
    _set_contents(page, writer, kept)
    return True
'''

NEW_SURGICAL = r'''def _surgical_strip(writer, index, trans_blocks, locked_blocks):
    """Remove ONLY the translated wording, on the page and inside forms.
    The returned flag is a first reading only - the caller re-checks on
    a saved and re-read file, which is the one that must be honest.
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
    try:
        _set_contents(page, writer, kept)
    except Exception:
        return False
    return _wording_gone(page, trans_blocks, forbidden)
'''

NEW_EXPANDED = r'''def _expanded_block(block, all_blocks):
    """Room for the translation: stop a small gap ABOVE the neighbour
    below, and never grow sideways into ink sitting on the same rows
    (a dash or a marker at the right edge used to be overrun).
    """
    top = block["first_y"] + 0.75 * block["size"]
    bottom = block["y_bot"]
    right0 = float(block.get("x_room", block["x1"]))
    right = right0
    below = []
    for other in all_blocks:
        if other is block:
            continue
        o_top = other["first_y"] + 0.75 * other["size"]
        o_bot = other["y_bot"]
        if o_top < top - 1.0 and other["x0"] < right0 \
           and block["x0"] < other.get("x_room", other["x1"]):
            below.append(other)
        if other["x0"] >= block["x1"] - 0.5 \
           and o_bot <= top and o_top >= bottom:
            right = min(right, other["x0"] - 1.0)
    neighbour = max(below, key=lambda o: o["first_y"]) if below else None
    room = (neighbour["first_y"] + 0.75 * neighbour["size"] + MIRROR_GAP) \
        if neighbour else 24.0
    out = dict(block)
    out["y_bot"] = min(bottom, room)
    out["x_room"] = round(max(block["x1"], right), 2)
    return out
'''

NEW_FIT = r'''def _fit_block(block, text, font, min_scale, floor_abs=5.0, tight=False):
    """Word wrap into the rectangle; the mirror fitting ladder.
    Returns (lines, size, overflow, leading, hscale). Ladder: shrink to
    min_scale, tighten leading to 1.0, shrink to floor_abs, then squeeze
    horizontally. In tight (mirror) mode hard line breaks are reflowed -
    the rectangle matters, not where the model pressed Enter - and the
    last line's descender must stay at or above y_bot.
    """
    text = str(text)
    if tight:
        text = re.sub(r"\s+", " ", text).strip()
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
    Every check runs on a saved and re-read file. Stages per page:
    surgical lift-out -> verify -> full strip and redraw -> verify ->
    original page shipped untouched and reported. The finished document
    is audited once more and repaired once if any wording survived.
    """
    import io
    from pypdf import PdfReader
    src = _reader(job["source"])
    base = _reader(os.path.join(folder, "base.pdf"))
    mirror = facing_style == "mirror"
    overflow, facing, mapping = [], [], {}
    mirrored, fallback, redrawn, unplaced = [], [], [], []
    leftovers = []
    min_font = [99.0]
    min_scale = float(job.get("min_scale", 0.7))

    plan = {}
    for index in range(job["pages_total"]):
        key = str(index + 1)
        mode = job["mode"].get(key, "kept")
        ready = job["status"].get(key) == "done"
        trans = job["translations"].get(key, {})
        t_blocks = job["blocks"].get(key, [])
        k_blocks = job["locked"].get(key, [])
        all_blocks = t_blocks + k_blocks
        if ready:
            samples = _text_colors(src.pages[index])
            for block in all_blocks:
                block["color"] = _block_color(block, samples)
        changed, same = [], []
        for block in t_blocks:
            now = trans.get(block["id"], block["text"])
            if _norm(now) == _norm(block["text"]):
                same.append(block)
            else:
                changed.append(block)
        entry = {"key": key, "mode": mode, "ready": ready, "trans": trans,
                 "all": all_blocks, "changed": changed, "same": same,
                 "locked": k_blocks, "draw": t_blocks, "page": None,
                 "dead": False,
                 "mirror": bool(mirror and ready
                                and mode in ("inplace", "facing"))}
        if entry["mirror"]:
            if mode == "inplace":
                entry["page"] = base.pages[index]
                entry["draw"] = all_blocks
            elif not changed:
                entry["page"] = src.pages[index]
                entry["draw"] = []
        plan[index] = entry

    live = None
    if mirror:
        todo = [i for i in sorted(plan)
                if plan[i]["mirror"] and plan[i]["mode"] == "facing"
                and plan[i]["changed"]]
        if todo:
            surg = PdfWriter(clone_from=src)
            for i in todo:
                p = plan[i]
                try:
                    _surgical_strip(surg, i, p["changed"],
                                    p["locked"] + p["same"])
                except Exception:
                    pass
            live = _freeze(surg, folder, "stage_surgical.pdf")
            bad = []
            for i in todo:
                p = plan[i]
                forbidden = [_norm(b.get("text", ""))
                             for b in p["locked"] + p["same"]]
                if _wording_gone(live.pages[i], p["changed"], forbidden):
                    p["page"] = live.pages[i]
                    p["draw"] = p["changed"]
                else:
                    bad.append(i)
            if bad:
                full = PdfWriter(clone_from=live)
                for i in bad:
                    try:
                        _strip_page(full, i)
                    except Exception:
                        pass
                live = _freeze(full, folder, "stage_full.pdf")
                for i in bad:
                    p = plan[i]
                    if _wording_gone(live.pages[i], p["changed"], []):
                        p["page"] = live.pages[i]
                        p["draw"] = p["changed"] + p["same"] + [
                            b for b in p["locked"]
                            if _readable_locked(b) or _marker_locked(b)]
                        redrawn.append(i + 1)
                    else:
                        p["dead"] = True

    def readable_extras(p):
        return p["changed"] + p["same"] + [
            b for b in p["locked"]
            if _readable_locked(b) or _marker_locked(b)]

    def assemble():
        out = PdfWriter()
        mapping.clear()
        del overflow[:]
        del facing[:]
        del mirrored[:]
        del fallback[:]
        del unplaced[:]
        min_font[0] = 99.0
        for index in range(job["pages_total"]):
            p = plan[index]
            key = p["key"]
            mapping[index] = len(out.pages)
            if p["mirror"] and p["dead"]:
                fallback.append(index + 1)
                out.add_page(src.pages[index])
                continue
            if p["mirror"]:
                origin = p["page"] if p["page"] is not None \
                    else src.pages[index]
                out.add_page(origin)
                page = out.pages[-1]
                width = float(page.mediabox.width)
                height = float(page.mediabox.height)
                page_over = []
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
                mirrored.append(index + 1)
                if page_over:
                    unplaced.append(index + 1)
                    overflow.extend(page_over)
                continue
            if p["mode"] == "inplace" and p["ready"]:
                out.add_page(base.pages[index])
                page = out.pages[-1]
                width = float(page.mediabox.width)
                height = float(page.mediabox.height)
                page_over = []

                def drawer(doc, p=p, page_over=page_over, key=key,
                           min_scale=min_scale):
                    _draw_blocks(doc, p["all"], p["trans"], p["all"],
                                 page_over, key, min_scale, 5.0,
                                 False, False)
                page.merge_page(_overlay_page(width, height, drawer))
                overflow.extend(page_over)
            elif p["mode"] == "facing" and p["ready"]:
                box = src.pages[index].mediabox
                extra = _facing_pages(job, key, index + 1,
                                      float(box.width), float(box.height))
                replaced = facing_style == "replace"
                if not replaced:
                    out.add_page(src.pages[index])
                for piece in extra:
                    out.add_page(piece)
                facing.append({"page": index + 1, "added": len(extra),
                               "replaced": replaced})
            else:
                out.add_page(src.pages[index])
        return out

    def audit(path):
        bad = []
        try:
            with open(path, "rb") as stream:
                check = PdfReader(io.BytesIO(stream.read()))
            for number in mirrored:
                p = plan[number - 1]
                if not p["changed"]:
                    continue
                forbidden = [_norm(b.get("text", ""))
                             for b in p["locked"] + p["same"]]
                forbidden += [_norm(v) for v in p["trans"].values()]
                if not _wording_gone(check.pages[number - 1],
                                     p["changed"], forbidden):
                    bad.append(number - 1)
        except Exception:
            return []
        return bad

    def finish_file(out):
        count = _copy_outline(src, out, mapping, custom)
        if meta_title:
            out.add_metadata({"/Title": meta_title})
        _save(out, target)
        return count

    writer = assemble()
    marks = finish_file(writer)
    if mirror and mirrored:
        bad = audit(target)
        if bad:
            try:
                fix = PdfWriter(clone_from=live if live is not None
                                else src)
                for i in bad:
                    _strip_page(fix, i)
                fixed = _freeze(fix, folder, "stage_repair.pdf")
                for i in bad:
                    p = plan[i]
                    if _wording_gone(fixed.pages[i], p["changed"], []):
                        p["page"] = fixed.pages[i]
                        p["draw"] = readable_extras(p)
                        redrawn.append(i + 1)
                    else:
                        p["dead"] = True
                writer = assemble()
                marks = finish_file(writer)
                leftovers = [i + 1 for i in audit(target)]
            except Exception:
                leftovers = [i + 1 for i in bad]
    mirror_info = {"requested": mirror, "mirrored": mirrored,
                   "fallback": fallback, "redrawn": sorted(set(redrawn)),
                   "unplaced": sorted(set(unplaced)),
                   "leftovers": sorted(set(leftovers)),
                   "min_font": round(min_font[0], 2) if mirrored else None}
    return overflow, marks, facing, len(writer.pages), mirror_info
'''

src = replace_func(src, "_strip_page", NEW_STRIP,
                   "replaced _strip_page (+freeze, +plain contents)")
src = replace_func(src, "_surgical_strip", NEW_SURGICAL)
src = replace_func(src, "_expanded_block", NEW_EXPANDED)
src = replace_func(src, "_fit_block", NEW_FIT)
src = replace_func(src, "_render", NEW_RENDER)

src = sub(src,
          '            if mirror_info["unplaced"]:',
          '            if mirror_info.get("leftovers"):\n'
          '                warnings.append("pages %s still show some '
          'original wording"\n'
          '                                % mirror_info["leftovers"])\n'
          '            if mirror_info["unplaced"]:',
          "finish reports leftovers")

src = sub(src, "batch 3, engine v8.", "batch 3, engine v8c.",
          "docs updated")

src = sub(src,
          "Mirror fitting keeps a 1 pt vertical gap above the neighbour",
          "V8C HONESTY. Text extraction from an in-memory page can differ\n"
          "from the saved document, so every check runs on a file that was\n"
          "written and read back, and the finished PDF is audited and\n"
          "repaired once. Hard line breaks are reflowed in mirror, and a\n"
          "block never grows sideways into a neighbour's ink.\n"
          "Mirror fitting keeps a 1 pt vertical gap above the neighbour",
          "v8c note added")

GUARD = r'''        # ---- v8c guard: hard breaks reflow, no clash, file audited -----
        buf = _io.BytesIO()
        cv = _cv.Canvas(buf, pagesize=(297, 420))
        cv.setFont(_font_name(), 10)
        cv.drawString(30, 380, "First line of the paragraph goes here.")
        cv.drawString(30, 368, "Second line follows right below it.")
        cv.showPage()
        cv.save()
        with open(p("reflow.pdf"), "wb") as stream:
            stream.write(buf.getvalue())
        st_r = tools.call("board_pdf_translate_start",
                          {"path": p("reflow.pdf"), "restart": True})
        rid = st_r.get("job")
        jr = job_of(rid)
        send_r = {}
        for b in jr["blocks"].get("1", []):
            if b["text"].startswith("First"):
                send_r[b["id"]] = ("Первая строка абзаца расположена здесь.\n"
                                   "Проверьте остальное содержимое коробки.")
            else:
                send_r[b["id"]] = "Вторая строка идёт сразу под ней."
        tools.call("board_pdf_translate_fill",
                   {"job": rid, "page": 1, "blocks": send_r,
                    "on_overflow": "facing"})
        fin_rf = tools.call("board_pdf_translate_finish",
                            {"job": rid, "out": p("reflow_ru.pdf"),
                             "overwrite": True, "place": False})
        show("finish reflow", fin_rf, "pages", "mirror")
        expect("reflow one page", fin_rf.get("pages") == 1)
        expect("reflow no leftovers",
               not (fin_rf.get("mirror") or {}).get("leftovers"))
        expect("formcover audited",
               not (fin_f.get("mirror") or {}).get("leftovers"))
        jr = job_of(rid)
        allr = jr["blocks"].get("1", []) + jr["locked"].get("1", [])
        trr = jr["translations"].get("1", {})
        clash_r = 0
        for b in jr["blocks"].get("1", []):
            t = trr.get(b["id"], b["text"])
            tgt = _expanded_block(b, allr)
            ls, size, over, lead, hs = _fit_block(
                tgt, t, _block_font(b), MIRROR_SCALE,
                floor_abs=MIRROR_FLOOR, tight=True)
            bottom = b["first_y"] - (len(ls) - 1) * size * lead - 0.30 * size
            for o in allr:
                if o is b:
                    continue
                top = o["first_y"] + 0.75 * o["size"]
                if top < b["first_y"] and bottom < top + 0.9 \
                   and o["x0"] < tgt.get("x_room", tgt["x1"]) \
                   and tgt["x0"] < o.get("x_room", o["x1"]):
                    clash_r += 1
        expect("reflow no clash", clash_r == 0)
        if fin_rf.get("ok"):
            rt = _reader(p("reflow_ru.pdf")).pages[0].extract_text() or ""
            expect("reflow russian present", "Первая строка" in rt)
            expect("reflow english lifted", "First line" not in rt)
'''

src = sub(src,
          '        if placed:\n            tools.call("board_delete", '
          '{"uids": placed})',
          GUARD.rstrip("\n") + "\n"
          '        if placed:\n            tools.call("board_delete", '
          '{"uids": placed})',
          "selftest guards inserted")

try:
    compile(src, PATH, "exec")
except SyntaxError as ex:
    raise SystemExit("SYNTAX FAIL line %s: %s" % (ex.lineno, ex.msg))

with io.open(PATH, "w", encoding="utf-8", newline="") as stream:
    stream.write(src)
print("lines %d -> %d" % (old_lines, src.count("\n") + 1))
print("SYNTAX OK")