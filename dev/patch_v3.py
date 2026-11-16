r"""Upgrade board_pdf_translate_tools.py to job version 3.

Fixes three failures seen on the real manual:
  * a paragraph was drawn into a 12 pt wide marker box -> a ladder of words
    over the table. Now markers/labels are never offered, and wording that
    does not fit its box is REFUSED with a character budget.
  * a page counted as translated with 15 of 48 blocks done -> half the page
    stayed English. Now a page is finished only when every block is done.
  * labels in fonts without a character map were redrawn as visible squares.
    Such pages are now left untouched and a Russian page is added after them.
Aborts without touching the file if any marker is missing or duplicated.
"""
import io
import py_compile
import shutil

PATH = r"refsynapse\ai\board_pdf_translate_tools.py"
BACKUP = r"dev\board_pdf_translate_tools.before_v3.py"

A1 = "# ------------------------------------------------------------------ job files"
A2 = "def _resolve(obj):"
B1 = "# ----------------------------------------------------------------- rendering"
B2 = "# ------------------------------------------------------------------ self-test"

OLD_WORKFLOW = 'WORKFLOW = ("Workflow: start -> translate the blocks of \'next\' -> fill with "\n            "{page, blocks:{id: text}} -> repeat until \'next\' is null -> "\n            "finish. Blocks you omit keep their original wording. Use "\n            "keep:true for pages of pure connector/pin labels.")'

NEW_WORKFLOW = 'WORKFLOW = ("Workflow: start -> translate every item of \'next\' (use its id and "\n            "its original text) -> fill with {page, blocks:{id: text}} -> "\n            "repeat until \'next\' is null -> finish. A page counts as finished "\n            "only when all its blocks are translated. On inplace pages keep "\n            "each wording within max_chars. keep:true leaves a page alone.")'

PART1 = r'''# ------------------------------------------------------------------ job files
JOB_VERSION = 3


def _job_id(src):
    stem = os.path.splitext(os.path.basename(src))[0]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
    return (stem or "document")[:60]


def _job_paths(sb, job_id):
    folder = os.path.join(sb, "translate", job_id)
    return folder, os.path.join(folder, "job.json")


def _load_job(job_file):
    try:
        with open(job_file, "r", encoding="utf-8") as stream:
            job = json.load(stream)
    except Exception as ex:
        raise ToolError("Job file is damaged (%s) - call start with "
                        "restart:true to rebuild it" % ex)
    if job.get("version") != JOB_VERSION:
        raise ToolError("This job was made by an older version of the tool - "
                        "call board_pdf_translate_start with restart:true")
    for key in ("id", "source", "source_original", "pages_total", "mode",
                "status", "blocks", "locked", "translations"):
        if key not in job:
            raise ToolError("Job file lacks '%s' - call start with "
                            "restart:true" % key)
    return job


def _dump_job(job, folder):
    try:
        with open(os.path.join(folder, "job.json"), "w",
                  encoding="utf-8") as stream:
            json.dump(job, stream, ensure_ascii=False, indent=1)
    except OSError as ex:
        raise ToolError("Cannot save the job file: %s" % ex)


def _open_job(sb, a):
    job_id = str(a.get("job") or "").strip()
    if not job_id:
        raise ToolError("'job' is required (the id returned by "
                        "board_pdf_translate_start)")
    folder, job_file = _job_paths(sb, job_id)
    if not os.path.isfile(job_file):
        raise ToolError("Unknown job '%s' - call board_pdf_translate_start "
                        "first" % job_id)
    return _load_job(job_file), folder


def _progress(job):
    counts = {"done": 0, "kept": 0, "empty": 0, "pending": 0}
    for state in job["status"].values():
        counts[state] = counts.get(state, 0) + 1
    counts["selected"] = len(job["status"])
    counts["pages_total"] = job["pages_total"]
    counts["inplace"] = sum(1 for m in job["mode"].values() if m == "inplace")
    counts["facing"] = sum(1 for m in job["mode"].values() if m == "facing")
    return counts


def _missing(job, key):
    done = job["translations"].get(key, {})
    return [b["id"] for b in job["blocks"].get(key, []) if b["id"] not in done]


def _next_batch(job):
    pending = sorted(int(k) for k, v in job["status"].items() if v == "pending")
    if not pending:
        return None
    items, chars, pages = [], 0, []
    for number in pending:
        key = str(number)
        gone = set(job["translations"].get(key, {}))
        blocks = [b for b in job["blocks"].get(key, []) if b["id"] not in gone]
        if not blocks:
            continue
        pages.append(number)
        for block in blocks:
            item = {"page": number, "id": block["id"],
                    "size": round(block["size"], 1), "text": block["text"]}
            if job["mode"].get(key) == "inplace":
                item["max_chars"] = block["max_chars"]
            items.append(item)
            chars += len(block["text"])
        if chars >= BATCH_CHARS or len(pages) >= BATCH_PAGES:
            break
    if not items:
        return None
    return {"pages": pages, "items": items, "chars": chars,
            "pending_pages_left": len(pending) - len(pages)}


def _payload(job, folder, resumed=False):
    work = os.path.join(folder, "work.pdf")
    return {"job": job["id"], "resumed": resumed,
            "source": job["source_original"],
            "progress": _progress(job),
            "modes": {k: v for k, v in sorted(job["mode"].items(),
                                              key=lambda kv: int(kv[0]))},
            "warnings": job.get("warnings", [])[:20],
            "next": _next_batch(job),
            "work_file": work if os.path.isfile(work) else None,
            "hint": WORKFLOW}


# ------------------------------------------------------- what may be touched
def _bad_glyphs(text):
    """True when the extracted wording is not really readable.

    Some fonts in a PDF carry no usable character map: their text comes out
    as boxes, control codes or private-use codes. Redrawing such wording with
    a system font turns it into visible squares, so it must never be touched.
    """
    for ch in text:
        code = ord(ch)
        if code < 32 or 0xE000 <= code <= 0xF8FF:
            return True
        if code in (0xFFFD, 0x25A1, 0x25A0):
            return True
    return False


def _classify(blocks):
    """(locked, translatable) - markers, numbers and unreadable labels stay."""
    locked, ready = [], []
    for block in blocks:
        text = block["text"]
        if _bad_glyphs(text) or not re.search(r"[A-Za-z\u0400-\u04FF]", text):
            locked.append(block)
        else:
            ready.append(block)
    return locked, ready


def _capacity(block):
    """Rough character budget of the original rectangle (a hint for the model)."""
    width = max(block.get("x_room", block["x1"]) - block["x0"] + 2.0, 8.0)
    step = block["size"] * block["leading"]
    rows = max(1, int((block["first_y"] - block["y_bot"]) / step) + 1)
    per_row = max(1, int(width / (0.52 * block["size"])))
    return max(4, int(per_row * rows))


def _outside(page, spans):
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    for span in spans:
        if (span["x"] < -1 or span["y"] < -1
                or span["x"] > width + 1 or span["y"] > height + 1):
            return True
    return False


def _strip_page(writer, index):
    """Remove every text-showing command; drawings and pictures stay."""
    contents = writer.pages[index].get_contents()
    if contents is None:
        return False
    stream = ContentStream(contents, writer)
    stream.operations = [(ops, op) for ops, op in stream.operations
                         if op not in SHOW_OPS]
    writer.pages[index].replace_contents(stream)
    return True


'''

PART2 = r'''# ----------------------------------------------------------------- rendering
def _copy_outline(reader, writer, mapping, custom=None):
    if custom is not None:
        nodes = {}
        for index, entry in enumerate(custom, 1):
            if not isinstance(entry, dict):
                raise ToolError("'outline' entries must be objects "
                                "{title, page, parent}")
            title = str(entry.get("title") or "").strip()
            page = int(entry.get("page", 0)) - 1
            if not title or not 0 <= page < len(writer.pages):
                raise ToolError("outline entry %d needs a title and a page "
                                "between 1 and %d" % (index, len(writer.pages)))
            parent_index = int(entry.get("parent") or 0)
            if parent_index and parent_index not in nodes:
                raise ToolError("outline entry %d: parent %d must be an "
                                "earlier entry" % (index, parent_index))
            nodes[index] = writer.add_outline_item(
                title=title, page_number=page,
                parent=nodes.get(parent_index))
        return len(custom)

    count = 0

    def walk(items, parent):
        nonlocal count
        last = None
        for item in items:
            if isinstance(item, list):
                if last is not None:
                    walk(item, last)
                continue
            try:
                page = reader.get_destination_page_number(item)
            except Exception:
                continue
            target = mapping.get(page)
            if target is None:
                continue
            last = writer.add_outline_item(title=str(item.title),
                                           page_number=target, parent=parent)
            count += 1

    try:
        walk(reader.outline, None)
    except Exception:
        pass
    return count


def _facing_pages(job, key, number, width, height):
    """A fresh page with the Russian wording, free flowing - nothing to overlap."""
    import io
    from pypdf import PdfReader
    from reportlab.pdfgen import canvas

    blocks = job["blocks"].get(key, [])
    trans = job["translations"].get(key, {})
    base_font = _font_name()
    margin = 36.0
    usable = max(width - 2 * margin, 60.0)
    buffer = io.BytesIO()
    doc = canvas.Canvas(buffer, pagesize=(width, height))
    doc.setFillColor(HexColor("#000000"))
    doc.setFont(base_font, 9)
    doc.drawString(margin, height - margin - 9,
                   "Страница %d — перевод" % number)
    y = height - margin - 26
    for block in blocks:
        text = trans.get(block["id"], block["text"])
        face = _block_font(block)
        size = min(max(float(block["size"]), 7.0), 12.0)
        step = size * 1.3
        for line in _wrap(text, face, size, usable):
            if y - size < margin:
                doc.showPage()
                y = height - margin
            doc.setFillColor(HexColor("#000000"))
            doc.setFont(face, size)
            if line:
                doc.drawString(margin, y - size, line)
            y -= step
        y -= 4
    doc.showPage()
    doc.save()
    buffer.seek(0)
    return list(PdfReader(buffer).pages)


def _render(job, folder, target, partial_ok, meta_title=None, custom=None):
    """Rebuild the result from the source, the stripped base and the wording.

    inplace pages: stripped base + Russian drawn into the original rectangles.
    facing pages: the original page untouched, a Russian page added after it.
    Idempotent - a page is always drawn from a clean base, never twice.
    """
    src = _reader(job["source"])
    base = _reader(os.path.join(folder, "base.pdf"))
    writer = PdfWriter()
    overflow, facing, mapping = [], [], {}
    min_scale = float(job.get("min_scale", 0.7))
    for index in range(job["pages_total"]):
        key = str(index + 1)
        mode = job["mode"].get(key, "kept")
        state = job["status"].get(key)
        mapping[index] = len(writer.pages)
        ready = state == "done"
        if mode == "inplace" and ready:
            writer.add_page(base.pages[index])
            page = writer.pages[-1]
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            blocks = job["blocks"].get(key, []) + job["locked"].get(key, [])
            trans = job["translations"].get(key, {})
            page_over = []

            def drawer(doc, blocks=blocks, trans=trans, page_over=page_over,
                       key=key, min_scale=min_scale):
                for block in blocks:
                    text = trans.get(block["id"], block["text"])
                    font = _block_font(block)
                    lines, size, over = _fit_block(block, text, font, min_scale)
                    if over:
                        page_over.append({"page": int(key), "id": block["id"],
                                          "font_size": round(size, 2)})
                    doc.setFillColor(HexColor("#000000"))
                    doc.setFont(font, size)
                    y = block["first_y"]
                    for line in lines:
                        if line:
                            doc.drawString(block["x0"], y, line)
                        y -= size * block["leading"]

            page.merge_page(_overlay_page(width, height, drawer))
            overflow.extend(page_over)
        else:
            writer.add_page(src.pages[index])
            if mode == "facing" and ready:
                box = src.pages[index].mediabox
                extra = _facing_pages(job, key, index + 1,
                                      float(box.width), float(box.height))
                for piece in extra:
                    writer.add_page(piece)
                facing.append({"page": index + 1, "added": len(extra)})
    marks = _copy_outline(src, writer, mapping, custom)
    if meta_title:
        writer.add_metadata({"/Title": meta_title})
    _save(writer, target)
    return overflow, marks, facing, len(writer.pages)


# --------------------------------------------------------------- registration
def register_pdf_translate_tools(tools):
    """Batch 3: translate a PDF, keeping every illustration."""
    win = tools.win
    S = tools._schema
    sb = sandbox_root(win)

    path_arg = {"type": "string",
                "description": "PDF inside the agent folder '%s'" % sb}
    uid_arg = {"type": "string",
               "description": "uid of a PDF document already on the board"}
    job_arg = {"type": "string",
               "description": "Job id returned by board_pdf_translate_start"}
    over_arg = {"type": "boolean",
                "description": "Allow replacing an existing file"}

    # ---- open / resume a job -------------------------------------------
    def t_start(a):
        _need_pdf()
        src = _source(tools, a)
        job_id = _job_id(src)
        folder, job_file = _job_paths(sb, job_id)
        if os.path.isfile(job_file) and not a.get("restart"):
            job = _load_job(job_file)
            if os.path.realpath(job["source_original"]) != os.path.realpath(src):
                raise ToolError("Job '%s' belongs to a different file (%s). "
                                "Pass restart:true to rebuild it, or rename "
                                "the new file."
                                % (job_id, job["source_original"]))
            return _payload(job, folder, resumed=True)
        if os.path.isdir(folder) and a.get("restart"):
            try:
                shutil.rmtree(folder)
            except OSError as ex:
                raise ToolError("Cannot reset the job folder: %s" % ex)

        reader = _reader(src, a.get("password"))
        total = len(reader.pages)
        indices = _page_numbers(a.get("pages"), total)
        if len(indices) > MAX_JOB_PAGES:
            raise ToolError("This job would cover %d pages (max %d) - use "
                            "'pages' to work in ranges"
                            % (len(indices), MAX_JOB_PAGES))
        skip = set()
        if a.get("skip_pages"):
            skip = set(_page_numbers(a.get("skip_pages"), total,
                                     what="skip_pages"))
        min_scale = float(a.get("min_scale", 0.7))
        if not 0.5 <= min_scale <= 1.0:
            raise ToolError("'min_scale' must be between 0.5 and 1.0")

        try:
            os.makedirs(folder, exist_ok=True)
            shutil.copyfile(src, os.path.join(folder, "source.pdf"))
        except OSError as ex:
            raise ToolError("Cannot prepare the job folder: %s" % ex)

        base_writer = PdfWriter(clone_from=reader)
        mode, status, blocks, locked = {}, {}, {}, {}
        warnings = []
        for index in indices:
            number = index + 1
            key = str(number)
            page = reader.pages[index]
            try:
                spans = _page_spans(page)
            except ToolError as ex:
                mode[key], status[key] = "kept", "kept"
                warnings.append("page %d: %s - left in the original language"
                                % (number, ex))
                continue
            if not spans:
                mode[key], status[key] = "empty", "empty"
                continue
            if index in skip:
                mode[key], status[key] = "kept", "kept"
                continue
            try:
                found = _group_blocks(spans, float(page.mediabox.width))
            except ToolError as ex:
                mode[key], status[key] = "kept", "kept"
                warnings.append("page %d is %s - left in the original language"
                                % (number, ex))
                continue
            keep_blocks, ready = _classify(found)
            if not ready:
                mode[key], status[key] = "kept", "kept"
                continue
            unreadable = (_bad_glyphs("".join(s["text"] for s in spans))
                          or _outside(page, spans))
            cramped = [b for b in ready
                       if (b.get("x_room", b["x1"]) - b["x0"]) < 40
                       and len(b["text"]) > 25]
            chosen = "facing" if (unreadable or cramped or len(ready) > 45) \
                else "inplace"
            if chosen == "inplace":
                try:
                    stripped = _strip_page(base_writer, index)
                except Exception:
                    stripped = False
                if not stripped:
                    chosen = "facing"
                else:
                    before = len(re.sub(r"\s", "", page.extract_text() or ""))
                    after = len(re.sub(r"\s", "",
                                       base_writer.pages[index].extract_text()
                                       or ""))
                    if after > max(20, 0.05 * before):
                        chosen = "facing"
                        warnings.append("page %d: its wording cannot be lifted "
                                        "out cleanly - the page is kept as is "
                                        "and a Russian page follows it"
                                        % number)
            ready_out = []
            for position, block in enumerate(ready, 1):
                item = dict(block)
                item["id"] = "t%d" % position
                item["max_chars"] = _capacity(item)
                ready_out.append(item)
            locked_out = []
            for position, block in enumerate(keep_blocks, 1):
                item = dict(block)
                item["id"] = "k%d" % position
                locked_out.append(item)
            mode[key], status[key] = chosen, "pending"
            blocks[key], locked[key] = ready_out, locked_out
            if int(page.rotation or 0) % 360:
                warnings.append("page %d is rotated; the wording uses the "
                                "unrotated frame" % number)

        _save(base_writer, os.path.join(folder, "base.pdf"))
        job = {"version": JOB_VERSION, "id": job_id,
               "source": os.path.join(folder, "source.pdf"),
               "source_original": os.path.realpath(src),
               "source_uid": a.get("uid") or "",
               "pages_total": total, "min_scale": min_scale,
               "mode": mode, "status": status, "blocks": blocks,
               "locked": locked, "translations": {}, "warnings": warnings}
        _dump_job(job, folder)
        return _payload(job, folder)

    # ---- accept translations -------------------------------------------
    def t_fill(a):
        _need_pdf()
        job, folder = _open_job(sb, a)
        entries = a.get("pages")
        if entries is None and a.get("page") is not None:
            entries = [{"page": a["page"], "blocks": a.get("blocks"),
                        "keep": a.get("keep")}]
        if isinstance(entries, dict):
            entries = [entries]
        if not entries or not isinstance(entries, list):
            raise ToolError("'pages' must be a list like "
                            "[{page: 3, blocks: {t1: '...'}}]")
        if len(entries) > MAX_FILL_PAGES:
            raise ToolError("This call covers %d pages (max %d)"
                            % (len(entries), MAX_FILL_PAGES))
        chars = 0
        for entry in entries:
            for value in (entry.get("blocks") or {}).values():
                if isinstance(value, str):
                    chars += len(value)
        if chars > MAX_FILL_CHARS:
            raise ToolError("This call carries %d characters (max %d) - "
                            "send fewer pages" % (chars, MAX_FILL_CHARS))

        min_scale = float(job.get("min_scale", 0.7))
        staged, problems = [], []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ToolError("Each 'pages' entry must be an object")
            try:
                number = int(entry.get("page", 0))
            except (TypeError, ValueError):
                raise ToolError("'page' must be a number")
            key = str(number)
            if key not in job["status"]:
                raise ToolError("Page %d is not part of this job (selected "
                                "pages: %s)"
                                % (number,
                                   sorted(int(k) for k in job["status"])[:30]))
            if entry.get("keep"):
                staged.append((key, None))
                continue
            texts = entry.get("blocks")
            if not isinstance(texts, dict) or not texts:
                raise ToolError("Page %d: 'blocks' must be a non-empty object "
                                "{block_id: translated text}" % number)
            if job["mode"].get(key) not in ("inplace", "facing"):
                raise ToolError("Page %d is kept in the original language - "
                                "nothing is drawn there" % number)
            known = {b["id"]: b for b in job["blocks"].get(key, [])}
            unknown = [k for k in texts if k not in known]
            if unknown:
                raise ToolError("Page %d: unknown block ids %s. Valid ids: %s"
                                % (number, unknown[:10], list(known)[:30]))
            for bid, value in texts.items():
                if not isinstance(value, str) or not value.strip():
                    raise ToolError("Page %d block %s: the translation must "
                                    "be a non-empty string" % (number, bid))
            if job["mode"].get(key) == "inplace":
                for bid, value in texts.items():
                    block = known[bid]
                    lines, size, over = _fit_block(block, value,
                                                   _block_font(block),
                                                   min_scale)
                    if over:
                        problems.append(
                            "page %d %s: %d chars do not fit a %.0f pt wide "
                            "box (about %d chars max, original there: %s)"
                            % (number, bid, len(value),
                               block.get("x_room", block["x1"]) - block["x0"],
                               block["max_chars"], block["text"][:40]))
            staged.append((key, texts))

        if problems:
            raise ToolError("Nothing was saved - the wording is too long or "
                            "the ids are shifted. Shorten it and send again: "
                            + " | ".join(problems[:6]))

        touched = []
        for key, texts in staged:
            if texts is None:
                if job["status"][key] in ("pending", "done"):
                    job["status"][key] = "kept"
                    job["mode"][key] = "kept"
                    job["translations"].pop(key, None)
                touched.append(int(key))
                continue
            job["translations"].setdefault(key, {}).update(texts)
            job["status"][key] = "done" if not _missing(job, key) else "pending"
            touched.append(int(key))

        _dump_job(job, folder)
        overflow, marks, facing, pages = _render(
            job, folder, os.path.join(folder, "work.pdf"), partial_ok=True)
        payload = _payload(job, folder)
        payload["filled_pages"] = touched
        payload["still_missing"] = {k: _missing(job, k) for k in job["status"]
                                    if job["status"][k] == "pending"}
        payload["overflow"] = [o for o in overflow if o["page"] in touched]
        return payload

    # ---- assemble and place --------------------------------------------
    def t_finish(a):
        _need_pdf()
        job, folder = _open_job(sb, a)
        pending = sorted(int(k) for k, v in job["status"].items()
                         if v == "pending")
        partial = bool(a.get("allow_partial"))
        if pending and not partial:
            raise ToolError("%d pages are not finished: %s. Send the missing "
                            "blocks (see 'still_missing' of the last fill), "
                            "or pass allow_partial:true to leave them in the "
                            "original language." % (len(pending), pending[:25]))
        custom = a.get("outline")
        if custom is not None and not isinstance(custom, list):
            raise ToolError("'outline' must be a list of {title, page, parent}")
        default_out = os.path.join(sb, job["id"] + "_ru.pdf")
        target = _pdf_target(win, {"path": a.get("out") or default_out,
                                   "overwrite": a.get("overwrite")})
        title = None
        try:
            meta = _reader(job["source"]).metadata or {}
            old = str(meta.get("/Title") or "")
            title = (old + " - RU") if old else job["id"] + " (RU)"
        except Exception:
            pass
        overflow, marks, facing, pages = _render(
            job, folder, target, partial_ok=True,
            meta_title=title, custom=custom)
        placed, place_note = None, ""
        if a.get("place", True):
            args = {"path": target}
            uid = job.get("source_uid")
            if uid:
                try:
                    rect = win.full_rect(tools._item(uid))
                    args["x"] = rect.right() + 40
                    args["y"] = rect.top()
                except Exception:
                    pass
            try:
                placed = tools.t_add_image(args).get("item")
            except Exception as ex:
                place_note = ("The file is saved but could not be placed on "
                              "the board: %s" % ex)
        job["finished"] = target
        _dump_job(job, folder)
        warnings = list(job.get("warnings", []))
        if place_note:
            warnings.append(place_note)
        return {"path": target, "pages": pages,
                "pages_source": job["pages_total"],
                "bytes": os.path.getsize(target),
                "placed_item": placed, "bookmarks": marks,
                "russian_pages_added": facing,
                "left_in_original": pending if partial else [],
                "kept_pages": sorted(int(k) for k, v in job["mode"].items()
                                     if v == "kept"),
                "empty_pages": sorted(int(k) for k, v in job["mode"].items()
                                      if v == "empty"),
                "overflow": overflow[:40], "warnings": warnings[:20],
                "hint": "Open the result and compare a few pages with the "
                        "source. Screenshots keep their original language."}

    # ---- registry --------------------------------------------------------
    tools._add("board_pdf_translate_start",
               "Begin translating a PDF while KEEPING every illustration. "
               "Source is 'path' INSIDE the agent folder '%s' or 'uid' of a "
               "board document. Each page gets a mode: 'inplace' = the "
               "original wording is lifted out and Russian is drawn into the "
               "same rectangles; 'facing' = the page is left untouched and a "
               "Russian page is added right after it (used for dense tables "
               "and fonts whose text cannot be read). Returns a job id and "
               "'next' - the first batch of blocks, each with its original "
               "text and max_chars. Translate them, call "
               "board_pdf_translate_fill, repeat until 'next' is null, then "
               "board_pdf_translate_finish. Markers, pin labels and "
               "unreadable labels are never offered. skip_pages leaves pages "
               "alone. Calling start again resumes; restart:true rebuilds."
               % sb,
               S({"path": path_arg, "uid": uid_arg,
                  "pages": {"type": "string",
                            "description": "Pages to translate, '1-3,5' or "
                                           "'all' (default all)"},
                  "skip_pages": {"type": "string",
                                 "description": "Pages to leave untouched"},
                  "min_scale": {"type": "number",
                                "description": "Lowest font shrink factor "
                                               "0.5-1 (default 0.7)"},
                  "password": {"type": "string"},
                  "restart": {"type": "boolean"}}),
               t_start)

    tools._add("board_pdf_translate_fill",
               "Feed translations into a job. 'pages' is [{page: 3, blocks: "
               "{t1: 'wording', ...}}]; one page may be sent as 'page' + "
               "'blocks'. Use the ids and the original 'text' from 'next' - "
               "never invent ids. On 'inplace' pages each block must fit its "
               "original box: too long wording is REFUSED with the character "
               "budget, nothing is saved. A page counts as finished only when "
               "every block of it is translated; 'still_missing' lists what "
               "is left. {page: n, keep: true} keeps a whole page untouched.",
               S({"job": job_arg,
                  "pages": {"type": "array",
                            "items": {"type": "object"},
                            "description": "[{page, blocks:{id:text}} or "
                                           "{page, keep:true}]"},
                  "page": {"type": "integer"},
                  "blocks": {"type": "object"},
                  "keep": {"type": "boolean"}},
                 ["job"]),
               t_fill)

    tools._add("board_pdf_translate_finish",
               "Assemble the translated PDF INSIDE the agent folder '%s' "
               "(default name <job>_ru.pdf) and place it on the board next to "
               "the source (place:false to skip). Refuses while pages are "
               "unfinished unless allow_partial:true. 'facing' pages add a "
               "Russian page after the original, so the result may have more "
               "pages than the source; bookmarks are remapped. Pass 'outline' "
               "[{title, page, parent}] to replace them with translated "
               "titles (page numbers of the RESULT). Existing files need "
               "overwrite:true." % sb,
               S({"job": job_arg,
                  "out": {"type": "string",
                          "description": "Result file inside the agent folder"},
                  "outline": {"type": "array", "items": {"type": "object"}},
                  "allow_partial": {"type": "boolean"},
                  "place": {"type": "boolean"},
                  "overwrite": over_arg},
                 ["job"]),
               t_finish)


'''

with io.open(PATH, "r", encoding="utf-8-sig") as stream:
    src = stream.read()

if "JOB_VERSION" in src:
    print("already upgraded - nothing to do")
    py_compile.compile(PATH, doraise=True)
    print("COMPILE OK")
    raise SystemExit(0)

spots = []
for marker in (A1, A2, B1, B2):
    n = src.count(marker)
    print("marker %-46s x%d" % (marker[:46], n))
    if n != 1:
        raise SystemExit("STOP: marker not unique, file NOT changed")
    spots.append(src.index(marker))
if spots != sorted(spots):
    raise SystemExit("STOP: markers out of order, file NOT changed")
a1, a2, b1, b2 = spots

for needle in (OLD_WORKFLOW, "got < 0.85 * wanted"):
    n = src.count(needle)
    print("extra target x%d (%s...)" % (n, needle[:28].replace("\n", " ")))
    if n != 1:
        raise SystemExit("STOP: extra target not unique, file NOT changed")

shutil.copyfile(PATH, BACKUP)
new = src[:a1] + PART1 + src[a2:b1] + PART2 + src[b2:]
new = new.replace(OLD_WORKFLOW, NEW_WORKFLOW, 1)
new = new.replace("got < 0.85 * wanted", "got < 0.55 * wanted", 1)

with io.open(PATH, "w", encoding="utf-8", newline="\n") as stream:
    stream.write(new)
py_compile.compile(PATH, doraise=True)
print("backup ->", BACKUP)
print("lines: %d -> %d" % (len(src.split("\n")), len(new.split("\n"))))
print("COMPILE OK")