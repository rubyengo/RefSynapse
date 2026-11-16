"""PDF translation for the RefSynapse agent - batch 3.

board_pdf_translate_start  - open a job: copy the source, remove the original
                             text from the pages (drawings stay!), remember
                             where every text block sat, hand out the first
                             batch of blocks to translate
board_pdf_translate_fill   - accept translated blocks, draw them into the
                             same places, track progress, hand out the next
                             batch
board_pdf_translate_finish - assemble the final PDF and place it on the board
                             next to the source document

HOW IT WORKS. A PDF page is drawing commands. The text-showing commands are
removed from a working copy, which leaves every line, curve, table rule and
picture untouched (verified on a real 29-page manual). The original text is
read WITH its coordinates first, grouped into blocks (paragraphs, headings,
table cells), and the translation is drawn back into the same rectangles
with word wrap. The model in the chat does the translating; these tools
orchestrate, draw and keep score. agent.py and the providers are not touched.

HONEST LIMITS. Text inside raster screenshots is a picture - it stays in the
original language. Text inside vector drawings (form XObjects) is left as-is
and reported. Blocks that do not fit are drawn at a reduced size and listed
in 'overflow' so the translation can be shortened. Bookmarks are preserved;
their titles stay original unless 'outline' is passed to finish.

SANDBOX. Everything lives inside agent\\translate\\<job>\\ while working; the
final file goes into the agent folder. The source may be a board document
('uid') - board content is user data. No silent overwrites.

Registered from board_pdf_edit_tools.register_pdf_edit_tools() - the same
self-hooking pattern batch 2 used, so board_tools.py is not touched again.
"""

import json
import os
import re
import shutil

from .board_tools import ToolError
from .sandbox import sandbox_root
from .board_pdf_tools import (_need_pdf, _font_name, _source, _reader,
                              _pdf_target, _page_numbers, _save, _wrap)
from .board_pdf_edit_tools import _char_width, _overlay_page

try:
    from pypdf import PdfWriter
    from pypdf.generic import ContentStream, IndirectObject
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
except Exception:                      # reported by _need_pdf() at call time
    pass

PDF_TRANSLATE_TOOL_NAMES = ("board_pdf_translate_start",
                            "board_pdf_translate_fill",
                            "board_pdf_translate_finish")

MAX_JOB_PAGES = 100
MAX_FILL_PAGES = 12
MAX_FILL_CHARS = 20_000
BATCH_CHARS = 3000
BATCH_PAGES = 4
MAX_SPANS_PER_PAGE = 4000
MAX_BLOCKS_PER_PAGE = 400
SHOW_OPS = {b"Tj", b"TJ", b"'", b'"'}
BULLET_STARTS = ("\u2022", "\u25cf", "\u25aa", "\uf0b7", "-", "\u2013")

WORKFLOW = ("Workflow: start -> translate the blocks of 'next' -> fill with "
            "{page, blocks:{id: text}} -> repeat until 'next' is null -> "
            "finish. Blocks you omit keep their original wording. Use "
            "keep:true for pages of pure connector/pin labels.")


# ------------------------------------------------------------------ job files
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
    for key in ("id", "source", "source_original", "pages_total",
                "status", "blocks", "translations"):
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
    return counts


def _next_batch(job):
    pending = sorted(int(k) for k, v in job["status"].items()
                     if v == "pending")
    if not pending:
        return None
    items, chars, pages = [], 0, []
    for number in pending:
        blocks = job["blocks"].get(str(number), [])
        pages.append(number)
        for block in blocks:
            items.append({"page": number, "id": block["id"],
                          "size": round(block["size"], 1),
                          "text": block["text"]})
            chars += len(block["text"])
        if chars >= BATCH_CHARS or len(pages) >= BATCH_PAGES:
            break
    return {"pages": pages, "items": items, "chars": chars,
            "pending_pages_left": len(pending) - len(pages)}


def _payload(job, folder, resumed=False):
    work = os.path.join(folder, "work.pdf")
    return {"job": job["id"], "resumed": resumed,
            "source": job["source_original"],
            "progress": _progress(job),
            "warnings": job.get("warnings", [])[:20],
            "next": _next_batch(job),
            "work_file": work if os.path.isfile(work) else None,
            "hint": WORKFLOW}


# ------------------------------------------------------------- text geometry
def _resolve(obj):
    return obj.get_object() if isinstance(obj, IndirectObject) else obj


def _span_width(font_dict, size, text):
    """Width of a piece of text in points, from the font's own glyph widths.

    The average of /Widths is useless for a subset font: it lists hundreds of
    zero entries for glyphs the font does not carry, which makes every line
    look three times shorter than it is. Zeros are ignored and each character
    is looked up by its own code instead.
    """
    total = None
    try:
        widths = font_dict.get("/Widths") if font_dict else None
        if widths:
            values = [float(v) for v in widths]
            good = sorted(v for v in values if v > 0)
            default = good[len(good) // 2] if good else 500.0
            first = int(font_dict.get("/FirstChar", 0))
            total = 0.0
            for ch in text:
                index = ord(ch) - first
                value = values[index] if 0 <= index < len(values) else 0.0
                total += value if value > 0 else default
    except Exception:
        total = None
    if total is None:
        total = 500.0 * len(text)
    return total / 1000.0 * size


def _page_spans(page):
    """Every drawn piece of text with position, real size and font name."""
    spans = []

    def visitor(text, cm, tm, font_dict, font_size):
        if not text or not text.strip():
            return
        try:
            a, b, c, d, e, f = [float(v) for v in cm]
            g, h, i, j, k, l = [float(v) for v in tm]
        except Exception:
            return
        scale_y = (g * b + h * d) or (i * b + j * d) or 1.0
        size = abs(float(font_size) * scale_y) or abs(float(font_size)) or 9.0
        if size < 2.0:
            return
        x = k * a + l * c + e
        y = k * b + l * d + f
        name = ""
        try:
            name = str((font_dict or {}).get("/BaseFont", ""))
        except Exception:
            pass
        clean = text.replace("\n", " ").replace("\r", " ")
        spans.append({"text": clean,
                      "x": float(x), "y": float(y), "size": float(size),
                      "w": _span_width(font_dict, float(size), clean),
                      "font": name})

    try:
        page.extract_text(visitor_text=visitor)
    except Exception:
        return []
    return spans


def _group_blocks(spans, page_w=595.0):
    """Spans -> line segments -> blocks (paragraph / heading / table cell).

    Two rules keep tables and headings intact:
    * a horizontal gap wider than a word space starts a NEW cell, so two
      side-by-side tables never end up in one block;
    * a line only continues into the next one when it is a full prose line
      (15 em wide or more), so a heading or a short cell stays on its own.
    'x_room' is how far the text may reach to the right: up to the next cell
    on the same line, never further than two em past the original wording.
    """
    if len(spans) > MAX_SPANS_PER_PAGE:
        raise ToolError("too dense (%d text pieces)" % len(spans))
    ordered = sorted(spans, key=lambda s: (-s["y"], s["x"]))
    lines = []
    for span in ordered:
        for line in lines:
            if abs(line["y"] - span["y"]) <= max(
                    1.5, 0.35 * max(line["size"], span["size"])):
                line["parts"].append(span)
                break
        else:
            lines.append({"y": span["y"], "size": span["size"],
                          "parts": [span]})

    segments = []
    for line in lines:
        parts = sorted(line["parts"], key=lambda s: s["x"])
        current = [parts[0]]
        for span in parts[1:]:
            prev = current[-1]
            gap = span["x"] - (prev["x"] + prev["w"])
            if gap > max(4.0, 0.9 * line["size"]):
                segments.append(current)
                current = [span]
            else:
                current.append(span)
        segments.append(current)

    rows = []
    for parts in segments:
        main = max(parts, key=lambda s: s["size"])
        text = parts[0]["text"]
        for prev, span in zip(parts, parts[1:]):
            gap = span["x"] - (prev["x"] + prev["w"])
            sep = " " if gap > 0.25 * main["size"] else ""
            if sep and text.endswith(" "):
                sep = ""
            text += sep + span["text"]
        rows.append({"x0": min(s["x"] for s in parts),
                     "x1": max(s["x"] + s["w"] for s in parts),
                     "y": main["y"], "size": main["size"],
                     "text": re.sub(r"\s+", " ", text).strip(),
                     "font": main["font"]})
    rows = [r for r in rows if r["text"]]

    for row in rows:
        neighbours = [other["x0"] for other in rows
                      if other is not row
                      and abs(other["y"] - row["y"]) <= max(
                          1.5, 0.35 * row["size"])
                      and other["x0"] > row["x1"] - 0.5]
        limit = min(neighbours) - 2.0 if neighbours else page_w - 12.0
        row["room"] = max(row["x1"],
                          min(limit, row["x1"] + 2.0 * row["size"]))
    rows.sort(key=lambda r: (-r["y"], r["x0"]))

    blocks = []
    for row in rows:
        joined = False
        if blocks:
            block = blocks[-1]
            last = block["lines"][-1]
            step = last["y"] - row["y"]
            size = block["size"]
            bullet = row["text"].lstrip().startswith(BULLET_STARTS)
            same_x = abs(row["x0"] - block["x0"]) <= 2.5
            indent = 0 < row["x0"] - block["x0"] <= 2.0 * row["size"]
            prose = (last["x1"] - last["x0"]) >= 15.0 * last["size"]
            if (not bullet
                    and prose
                    and (same_x or indent)
                    and row["x0"] < block["x1"] + 2.0
                    and 0.55 * size <= step <= 1.5 * size
                    and 0.8 <= row["size"] / size <= 1.25):
                block["lines"].append(row)
                block["x1"] = max(block["x1"], row["x1"])
                joined = True
        if not joined:
            blocks.append({"x0": row["x0"], "x1": row["x1"],
                           "size": row["size"], "font": row["font"],
                           "lines": [row]})

    result = []
    for number, block in enumerate(blocks, 1):
        lines = block["lines"]
        size = block["size"]
        steps = [a["y"] - b["y"] for a, b in zip(lines, lines[1:])]
        if steps:
            steps.sort()
            leading = steps[len(steps) // 2] / size
            leading = min(1.45, max(1.05, leading))
        else:
            leading = 1.2
        result.append({"id": "b%d" % number,
                       "x0": round(block["x0"], 2),
                       "x1": round(block["x1"], 2),
                       "x_room": round(max(l["room"] for l in lines), 2),
                       "first_y": round(lines[0]["y"], 2),
                       "y_bot": round(lines[-1]["y"] - 0.30 * size, 2),
                       "size": round(size, 2),
                       "leading": round(leading, 3),
                       "font": block["font"],
                       "text": "\n".join(l["text"] for l in lines)})
    if len(result) > MAX_BLOCKS_PER_PAGE:
        raise ToolError("too many text blocks (%d)" % len(result))
    return result


def _forms_have_text(page):
    """True when drawings (form XObjects) contain their own text."""
    try:
        resources = _resolve(page.get("/Resources", {})) or {}
    except Exception:
        return False
    stack, seen = [resources], set()
    pattern = re.compile(rb"(?<![\w])(Tj|TJ)(?![\w])")
    while stack and len(seen) < 60:
        res = _resolve(stack.pop())
        if not res or id(res) in seen:
            continue
        seen.add(id(res))
        try:
            xobjects = _resolve(res.get("/XObject", {})) or {}
        except Exception:
            continue
        for ref in xobjects.values():
            obj = _resolve(ref)
            try:
                if obj.get("/Subtype") != "/Form":
                    continue
                if pattern.search(obj.get_data()):
                    return True
                inner = _resolve(obj.get("/Resources", {}))
                if inner:
                    stack.append(inner)
            except Exception:
                continue
    return False


# -------------------------------------------------------------------- fonts
_VARIANTS = {}


def _variant(kind):
    key = "RefSans" + kind
    if key in _VARIANTS:
        return _VARIANTS[key]
    folder = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    choices = {"Bold": ("segoeuib.ttf", "arialbd.ttf"),
               "Italic": ("segoeuii.ttf", "ariali.ttf"),
               "BoldItalic": ("segoeuiz.ttf", "arialbi.ttf")}.get(kind, ())
    for file_name in choices:
        path = os.path.join(folder, file_name)
        if not os.path.isfile(path):
            continue
        try:
            pdfmetrics.registerFont(TTFont(key, path))
        except Exception:
            continue
        _VARIANTS[key] = key
        return key
    return _font_name()


def _block_font(block):
    _font_name()                       # make sure the base face exists
    name = str(block.get("font", "")).lower()
    bold = any(t in name for t in ("bold", "black", "heavy", "semibold"))
    italic = "italic" in name or "oblique" in name
    if bold and italic:
        return _variant("BoldItalic")
    if bold:
        return _variant("Bold")
    if italic:
        return _variant("Italic")
    return _font_name()


def _fit_block(block, text, font, min_scale):
    """Word wrap into the original rectangle; shrink the font if needed."""
    right = float(block.get("x_room", block["x1"]))
    width = max(right - block["x0"] + 2.0, 8.0)
    size0 = block["size"]
    floor = max(5.0, size0 * min_scale)
    size = size0
    while True:
        lines = _wrap(text, font, size, width)
        step = size * block["leading"]
        bottom = block["first_y"] - (len(lines) - 1) * step - 0.30 * size
        overflow = bottom < block["y_bot"] - 1.5
        if not overflow or size <= floor + 1e-6:
            return lines, size, overflow
        size = max(floor, size - 0.25)


# ----------------------------------------------------------------- rendering
def _copy_outline(reader, writer, custom=None):
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
            last = writer.add_outline_item(title=str(item.title),
                                           page_number=page, parent=parent)
            count += 1

    try:
        walk(reader.outline, None)
    except Exception:
        pass
    return count


def _render(job, folder, target, partial_ok, meta_title=None, custom=None):
    """Rebuild the whole result from the stripped base + stored translations.

    Idempotent: a page is always drawn from the clean base, so re-filling a
    page replaces its translation instead of drawing on top of it.
    """
    src = _reader(job["source"])
    base = _reader(os.path.join(folder, "base.pdf"))
    writer = PdfWriter()
    overflow = []
    min_scale = float(job.get("min_scale", 0.7))
    for index in range(job["pages_total"]):
        key = str(index + 1)
        state = job["status"].get(key)
        if state in ("kept", "empty", None) or (state == "pending" and partial_ok):
            writer.add_page(src.pages[index])
            continue
        writer.add_page(base.pages[index])
        if state != "done":
            continue
        page = writer.pages[-1]
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        blocks = job["blocks"].get(key, [])
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
    marks = _copy_outline(src, writer, custom)
    if meta_title:
        writer.add_metadata({"/Title": meta_title})
    _save(writer, target)
    return overflow, marks


# --------------------------------------------------------------- registration
def register_pdf_translate_tools(tools):
    """Batch 3: translate a whole PDF in place, drawings preserved."""
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
        status, blocks, warnings = {}, {}, []
        for index in indices:
            number = index + 1
            key = str(number)
            page = reader.pages[index]
            spans = _page_spans(page)
            if not spans:
                status[key] = "empty"
                continue
            if index in skip:
                status[key] = "kept"
                continue
            try:
                found = _group_blocks(spans, float(page.mediabox.width))
            except ToolError as ex:
                raise ToolError("Page %d is %s - leave it out with "
                                "skip_pages:'%d'" % (number, ex, number))
            try:
                contents = base_writer.pages[index].get_contents()
                if contents is None:
                    status[key] = "kept"
                    warnings.append("page %d: no content stream, left as-is"
                                    % number)
                    continue
                stream = ContentStream(contents, base_writer)
                stream.operations = [(ops, op) for ops, op in stream.operations
                                     if op not in SHOW_OPS]
                base_writer.pages[index].replace_contents(stream)
            except Exception as ex:
                status[key] = "kept"
                warnings.append("page %d: text could not be removed (%s) - "
                                "left in the original language" % (number, ex))
                continue
            if _forms_have_text(page):
                warnings.append("page %d: text inside drawings was left "
                                "untouched" % number)
            if int(page.rotation or 0) % 360:
                warnings.append("page %d is rotated; the translation uses "
                                "the unrotated frame" % number)
            blocks[key] = found
            status[key] = "pending"

        _save(base_writer, os.path.join(folder, "base.pdf"))
        job = {"id": job_id,
               "source": os.path.join(folder, "source.pdf"),
               "source_original": os.path.realpath(src),
               "source_uid": a.get("uid") or "",
               "pages_total": total,
               "min_scale": min_scale,
               "status": status, "blocks": blocks,
               "translations": {}, "warnings": warnings}
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
                            "[{page: 3, blocks: {b1: '...'}}]")
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

        touched = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ToolError("Each 'pages' entry must be an object")
            try:
                number = int(entry.get("page", 0))
            except (TypeError, ValueError):
                raise ToolError("'page' must be a number")
            key = str(number)
            state = job["status"].get(key)
            if state is None:
                raise ToolError("Page %d is not part of this job (selected "
                                "pages: %s)"
                                % (number,
                                   sorted(int(k) for k in job["status"])[:30]))
            if entry.get("keep"):
                if state in ("pending", "done"):
                    job["status"][key] = "kept"
                    job["translations"].pop(key, None)
                touched.append(number)
                continue
            blocks = entry.get("blocks")
            if not isinstance(blocks, dict) or not blocks:
                raise ToolError("Page %d: 'blocks' must be a non-empty "
                                "object {block_id: translated text}" % number)
            if state in ("kept", "empty"):
                raise ToolError("Page %d is kept in the original language - "
                                "nothing is drawn there. Restart the job "
                                "without skip_pages to translate it." % number)
            valid = {b["id"] for b in job["blocks"].get(key, [])}
            unknown = [k for k in blocks if k not in valid]
            if unknown:
                raise ToolError("Page %d: unknown block ids %s. Valid ids: %s"
                                % (number, unknown[:10],
                                   sorted(valid, key=lambda s: int(s[1:]))[:25]))
            for bid, value in blocks.items():
                if not isinstance(value, str) or not value.strip():
                    raise ToolError("Page %d block %s: the translation must "
                                    "be a non-empty string" % (number, bid))
            job["translations"].setdefault(key, {}).update(blocks)
            job["status"][key] = "done"
            touched.append(number)

        _dump_job(job, folder)
        overflow, _ = _render(job, folder, os.path.join(folder, "work.pdf"),
                              partial_ok=False)
        payload = _payload(job, folder)
        payload["filled_pages"] = touched
        payload["overflow"] = [o for o in overflow if o["page"] in touched]
        if payload["overflow"]:
            payload["hint"] = ("Some blocks do not fit even at the reduced "
                               "size (see 'overflow'). Shorten those "
                               "translations and call fill again for the "
                               "same block ids - pages are redrawn cleanly.")
        return payload

    # ---- assemble and place --------------------------------------------
    def t_finish(a):
        _need_pdf()
        job, folder = _open_job(sb, a)
        pending = sorted(int(k) for k, v in job["status"].items()
                         if v == "pending")
        partial = bool(a.get("allow_partial"))
        if pending and not partial:
            raise ToolError("%d pages are not translated yet: %s. Fill them, "
                            "or pass allow_partial:true to leave those pages "
                            "in the original language."
                            % (len(pending), pending[:25]))
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
        overflow, marks = _render(job, folder, target, partial_ok=True,
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
        kept = sorted(int(k) for k, v in job["status"].items()
                      if v == "kept")
        empty = sorted(int(k) for k, v in job["status"].items()
                       if v == "empty")
        warnings = list(job.get("warnings", []))
        if place_note:
            warnings.append(place_note)
        return {"path": target, "pages": job["pages_total"],
                "bytes": os.path.getsize(target),
                "placed_item": placed, "bookmarks": marks,
                "left_in_original": pending if partial else [],
                "kept_pages": kept, "empty_pages": empty,
                "overflow": overflow[:40], "warnings": warnings[:20],
                "hint": "Open the result and compare a few pages with the "
                        "source. Screenshots inside the pages keep their "
                        "original language."}

    # ---- registry --------------------------------------------------------
    tools._add("board_pdf_translate_start",
               "Begin translating a PDF while KEEPING every illustration: "
               "the original text is removed from a working copy (drawings, "
               "tables and photos stay) and its blocks are remembered with "
               "coordinates. Source is 'path' INSIDE the agent folder '%s' "
               "or 'uid' of a board document. Returns a job id and 'next' - "
               "the first batch of blocks to translate. Translate them, call "
               "board_pdf_translate_fill, repeat until 'next' is null, then "
               "board_pdf_translate_finish. skip_pages leaves pages untouched "
               "(legal notices); pages without text are kept automatically. "
               "Calling start again resumes an interrupted job; restart:true "
               "rebuilds it. Text inside raster screenshots cannot be "
               "translated." % sb,
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
               "Feed translations into a job from board_pdf_translate_start. "
               "'pages' is [{page: 3, blocks: {b1: 'translated text', ...}}]; "
               "a single page may be sent as 'page' + 'blocks'. Blocks you "
               "omit keep their original wording (redrawn with an embedded "
               "font). {page: n, keep: true} keeps the WHOLE page untouched - "
               "use it for pin tables and connector labels. Text is word "
               "wrapped into the original rectangle; the font shrinks down "
               "to the job's min_scale when needed, and blocks that still do "
               "not fit are listed in 'overflow' - shorten them and fill the "
               "same ids again (pages redraw cleanly, never double-drawn). "
               "Returns progress and the next batch.",
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
               "(default name <job>_ru.pdf) and place it on the board next "
               "to the source document (place:false to skip). Refuses while "
               "pages are pending unless allow_partial:true - those pages "
               "then stay in the original language. Bookmarks are preserved; "
               "pass 'outline' [{title, page, parent}] to replace them with "
               "translated titles ('parent' is the 1-based index of an "
               "earlier entry). Existing files need overwrite:true." % sb,
               S({"job": job_arg,
                  "out": {"type": "string",
                          "description": "Result file inside the agent folder"},
                  "outline": {"type": "array", "items": {"type": "object"}},
                  "allow_partial": {"type": "boolean"},
                  "place": {"type": "boolean"},
                  "overwrite": over_arg},
                 ["job"]),
               t_finish)


# ------------------------------------------------------------------ self-test
def _selftest():
    """python -m refsynapse.ai.board_pdf_translate_tools  (opens the window)"""
    import sys
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from ..main_window import MainWindow
    from .board_tools import BoardTools

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    tools = BoardTools(win)
    print("Registered tools:", len(tools.names()))
    for name in PDF_TRANSLATE_TOOL_NAMES:
        print("  has", name, "->", name in tools.names())
    sb = sandbox_root(win)
    print("agent sandbox ->", sb)

    def run():
        folder = os.path.join(sb, "pdf_translate_selftest")
        os.makedirs(folder, exist_ok=True)

        def p(name):
            return os.path.join(folder, name)

        def show(label, result, *keys):
            print(label, "->", result.get("ok"),
                  [result.get(k) for k in keys], result.get("error", ""))

        fails = []

        def expect(label, cond):
            print("  %s -> %s (expected True)" % (label, bool(cond)))
            if not cond:
                fails.append(label)

        made = tools.call("board_pdf_create",
                          {"path": p("src.pdf"),
                           "text": "Chapter One\n\nHello world. This "
                                   "paragraph explains how to install the "
                                   "memory modules.\n\nSecond paragraph "
                                   "about the CPU cooler.",
                           "font_size": 12, "overwrite": True})
        show("create source", made, "pages")
        if not made.get("ok"):
            print("DONE - close the window to exit")
            return

        st = tools.call("board_pdf_translate_start",
                        {"path": p("src.pdf"), "restart": True})
        show("start", st, "job", "progress")
        items = (st.get("next") or {}).get("items") or []
        expect("blocks found", len(items) >= 2)
        job_id = st.get("job")

        words = ["Глава первая", "Привет, мир. Установка модулей памяти.",
                 "Второй абзац про кулер."]
        blocks = {}
        for index, item in enumerate(items):
            blocks[item["id"]] = words[index % len(words)]
        fi = tools.call("board_pdf_translate_fill",
                        {"job": job_id, "page": 1, "blocks": blocks})
        show("fill", fi, "filled_pages", "progress")
        expect("no pending", (fi.get("progress") or {}).get("pending") == 0)
        expect("next is null", fi.get("next") is None)
        expect("no overflow", not fi.get("overflow"))

        again = tools.call("board_pdf_translate_fill",
                           {"job": job_id, "page": 1,
                            "blocks": {items[0]["id"]: "Глава первая"}})
        show("refill one block", again, "filled_pages")
        work = os.path.join(sb, "translate", job_id or "", "work.pdf")
        text = ""
        if os.path.isfile(work):
            text = _reader(work).pages[0].extract_text() or ""
        expect("russian visible", "Привет" in text)
        expect("english gone", "Hello world" not in text)
        expect("redraw not doubled", text.count("Глава первая") == 1)

        fin = tools.call("board_pdf_translate_finish",
                         {"job": job_id, "out": p("final.pdf"),
                          "overwrite": True})
        show("finish", fin, "path", "pages", "bookmarks")
        expect("placed on board",
               (fin.get("placed_item") or {}).get("kind") == "document")

        bad = tools.call("board_pdf_translate_fill",
                         {"job": job_id, "page": 1,
                          "blocks": {"b999": "x"}})
        print("unknown block ->", bad.get("ok"), "(expected False)",
              bad.get("error", "")[:100])
        if bad.get("ok"):
            fails.append("unknown block guard")
        bad = tools.call("board_pdf_translate_finish", {"job": "no_such_job"})
        print("unknown job ->", bad.get("ok"), "(expected False)")
        if bad.get("ok"):
            fails.append("unknown job guard")
        outside = os.path.join(os.path.dirname(sb), "tr_escape.pdf")
        bad = tools.call("board_pdf_translate_finish",
                         {"job": job_id, "out": outside})
        print("write outside ->", bad.get("ok"), "(expected False)",
              bad.get("error", "")[:100])
        if bad.get("ok"):
            fails.append("sandbox guard")
        print("outside created ->", os.path.exists(outside),
              "(expected False)")

        st2 = tools.call("board_pdf_translate_start",
                         {"path": p("src.pdf"), "skip_pages": "1",
                          "restart": True})
        show("start skip", st2, "progress")
        expect("skip leaves no pending",
               (st2.get("progress") or {}).get("pending") == 0)
        fin2 = tools.call("board_pdf_translate_finish",
                          {"job": st2.get("job"), "out": p("kept.pdf"),
                           "overwrite": True, "place": False})
        show("finish kept", fin2, "pages", "kept_pages")
        text2 = ""
        if fin2.get("ok"):
            text2 = _reader(p("kept.pdf")).pages[0].extract_text() or ""
        expect("original preserved", "Hello world" in text2)

        st3 = tools.call("board_pdf_translate_start",
                         {"path": p("src.pdf"), "restart": True})
        bad = tools.call("board_pdf_translate_finish",
                         {"job": st3.get("job"), "out": p("early.pdf")})
        print("finish too early ->", bad.get("ok"), "(expected False)",
              bad.get("error", "")[:100])
        if bad.get("ok"):
            fails.append("early finish guard")
        part = tools.call("board_pdf_translate_finish",
                          {"job": st3.get("job"), "out": p("partial.pdf"),
                           "allow_partial": True, "place": False,
                           "overwrite": True})
        show("allow_partial", part, "left_in_original")
        text3 = ""
        if part.get("ok"):
            text3 = _reader(p("partial.pdf")).pages[0].extract_text() or ""
        expect("partial keeps english", "Hello world" in text3)

        resume = tools.call("board_pdf_translate_start",
                            {"path": p("src.pdf")})
        expect("resume works", resume.get("resumed") is True)

        uid = (fin.get("placed_item") or {}).get("uid")
        if uid:
            tools.call("board_delete", {"uids": [uid]})
        print("GUARD FAILS:", len(fails), fails)
        print("files are in ->", folder)
        print("DONE - close the window to exit")

    QTimer.singleShot(800, run)
    sys.exit(app.exec())


if __name__ == "__main__":
    _selftest()