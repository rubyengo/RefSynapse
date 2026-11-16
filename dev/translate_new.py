"""PDF translation for the RefSynapse agent - batch 3, engine v4.

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

PARTIAL ACCEPT (v4). Russian is longer than English, so a wording sometimes
does not fit its original box. What fits is saved IMMEDIATELY; what does not
fit comes back in 'rejected' with its character budget, and only those blocks
need a shorter wording. Progress is never thrown away. A block refused twice
moves its whole page to 'facing' automatically, and on_overflow:'facing' does
the same on request - there the wording flows freely and length never matters.

TWO WAYS TO SHIP A FACING PAGE. finish accepts facing:'append' (default) - the
original page stays and a Russian page follows it, so every drawing survives;
or facing:'replace' - only the Russian page is kept, giving a fully Russian
document at the cost of the drawings on those pages (reported honestly).

HONEST LIMITS. Text inside raster screenshots is a picture - it stays in the
original language. Text inside vector drawings (form XObjects) is left as-is
and reported. Bookmarks are preserved and remapped; their titles stay original
unless 'outline' is passed to finish. There is no OCR.

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
BATCH_CHARS = 6000
BATCH_PAGES = 6
MAX_REJECTS = 2
MAX_SPANS_PER_PAGE = 4000
MAX_BLOCKS_PER_PAGE = 400

SHOW_OPS = {b"Tj", b"TJ", b"'", b'"'}
BULLET_STARTS = ("\u2022", "\u25cf", "\u25aa", "\uf0b7", "-", "\u2013")

WORKFLOW = ("Workflow: start -> translate every item of 'next' (use its id and "
            "its original text) -> fill with {page, blocks:{id: text}} -> "
            "repeat until 'next' is null -> finish. Wording that fits is saved "
            "at once; wording that is too long comes back in 'rejected' with "
            "its character budget - shorten ONLY those blocks and send them "
            "again. A block refused twice moves its whole page to 'facing' "
            "(the original page plus a Russian page after it), where length "
            "does not matter; {page: n, on_overflow: 'facing'} does that "
            "immediately. A page counts as finished only when all its blocks "
            "are translated. keep:true leaves a page alone.")


# ------------------------------------------------------------------ job files

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
    job.setdefault("rejects", {})      # jobs written before v4 have none
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
    counts["refused_blocks"] = sum(len(v) for v in
                                   job.get("rejects", {}).values())
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

def _junk(ch):
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


def _readable_locked(block):
    """True for a locked block that still carries real information.

    Pin numbers, part codes, addresses and URLs are locked only because they
    are not wording to translate. On a free flowing Russian page they can be
    shown safely, so the page stays complete. Labels made of unmapped boxes
    are not readable and are skipped.
    """
    text = str(block.get("text", ""))
    if not re.search(r"[A-Za-z0-9\u0400-\u04FF]", text):
        return False
    return _junk_ratio(text) <= 0.2


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


def _resolve(obj):
    return obj.get_object() if isinstance(obj, IndirectObject) else obj


_METRICS = {}


def _font_metrics(font_dict):
    """letter -> advance (1/1000 em) for one font, plus a fallback advance.

    Simple fonts carry /Widths directly. Composite (Type0) fonts keep their
    widths in the descendant font, addressed by CID, and only the ToUnicode
    table links those CIDs back to letters. Without that link every line had
    to be guessed at half an em per character, which made paragraphs look a
    third wider than they are and glued neighbouring table cells together.
    """
    try:
        name = str(font_dict.get("/BaseFont", "")) or str(id(font_dict))
    except Exception:
        name = str(id(font_dict))
    if name in _METRICS:
        return _METRICS[name]
    table, values = {}, []
    try:
        widths = font_dict.get("/Widths")
        if widths:
            first = int(font_dict.get("/FirstChar", 0))
            for index, value in enumerate(widths):
                value = float(value)
                if value > 0:
                    table[chr(first + index)] = value
                    values.append(value)
        else:
            descendants = font_dict.get("/DescendantFonts")
            child = descendants[0].get_object() if descendants else None
            if child is not None:
                advances = {}
                raw = [x.get_object() if hasattr(x, "get_object") else x
                       for x in child.get("/W", [])]
                index = 0
                while index < len(raw):
                    if index + 1 < len(raw) and isinstance(raw[index + 1],
                                                           (list, tuple)):
                        for offset, value in enumerate(raw[index + 1]):
                            advances[int(raw[index]) + offset] = float(value)
                        index += 2
                    elif index + 2 < len(raw):
                        for cid in range(int(raw[index]),
                                         int(raw[index + 1]) + 1):
                            advances[cid] = float(raw[index + 2])
                        index += 3
                    else:
                        break
                values = [v for v in advances.values() if v > 0]
                try:
                    data = (font_dict["/ToUnicode"].get_object()
                            .get_data().decode("latin-1"))
                except Exception:
                    data = ""
                for chunk in re.findall(r"beginbfchar(.*?)endbfchar", data, re.S):
                    for src, dst in re.findall(
                            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", chunk):
                        cid = int(src, 16)
                        if cid not in advances or len(dst) % 4:
                            continue
                        try:
                            letter = "".join(chr(int(dst[k:k + 4], 16))
                                             for k in range(0, len(dst), 4))
                        except ValueError:
                            continue
                        if len(letter) == 1:
                            table[letter] = advances[cid]
                for chunk in re.findall(r"beginbfrange(.*?)endbfrange",
                                        data, re.S):
                    for lo, hi, dst in re.findall(
                            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>"
                            r"\s*<([0-9A-Fa-f]+)>", chunk):
                        first, last, base = int(lo, 16), int(hi, 16), int(dst, 16)
                        for cid in range(first, min(last, first + 255) + 1):
                            code = base + cid - first
                            if cid in advances and 0 <= code <= 0x10FFFF:
                                table[chr(code)] = advances[cid]
    except Exception:
        table, values = {}, []
    good = sorted(v for v in values if v > 0)
    default = good[len(good) // 2] if good else 500.0
    _METRICS[name] = (table, default)
    return _METRICS[name]


def _span_width(font_dict, size, text):
    """Width of a piece of text in points, from the font's own advances."""
    table, default = _font_metrics(font_dict)
    total = 0.0
    for ch in text:
        total += table.get(ch, default)
    return total / 1000.0 * size


def _page_spans(page):
    """Every drawn piece of text with position, real size and font name.

    Refuses a page whose text cannot be measured completely: such text would
    be removed from the working copy without being redrawn, i.e. lost.
    """
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
        whole = page.extract_text(visitor_text=visitor) or ""
    except Exception:
        return []
    if spans:
        wanted = len(re.sub(r"\s", "", whole))
        got = sum(len(re.sub(r"\s", "", s["text"])) for s in spans)
        if wanted and got < 0.85 * wanted:
            raise ToolError("too hard to measure (only %d%% of its text was "
                            "located)" % int(100.0 * got / wanted))
    return spans


def _style(font_name):
    """(bold, italic) - a heading must never melt into the paragraph below."""
    name = str(font_name).lower()
    bold = any(t in name for t in ("bold", "black", "heavy", "semibold"))
    italic = "italic" in name or "oblique" in name
    return bold, italic


def _group_blocks(spans, page_w=595.0):
    """Spans -> line segments -> blocks (paragraph / heading / table cell).

    Three rules keep the layout honest:
      * an x position where several lines begin is a table column, so a new
        cell starts there even when the gap to the previous cell is tiny;
      * a line only continues into the next one when it is a full prose line
        of the same weight, so headings and cells stay on their own;
      * 'x_room' is how far the wording may reach to the right - up to the next
        column, never past the widest original line, never off the page.
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

    starts = {}
    for line in lines:
        for x in {round(p["x"], 1) for p in line["parts"]}:
            starts[x] = starts.get(x, 0) + 1
    columns = {x for x, count in starts.items() if count >= 2}

    segments = []
    for line in lines:
        parts = sorted(line["parts"], key=lambda s: s["x"])
        current = [parts[0]]
        for span in parts[1:]:
            prev = current[-1]
            gap = span["x"] - (prev["x"] + prev["w"])
            if (gap > max(4.0, 0.9 * line["size"])
                    or (round(span["x"], 1) in columns and gap > -2.0)):
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
    if not rows:
        return []

    right = max(r["x1"] for r in rows)
    for row in rows:
        neighbours = [other["x0"] for other in rows
                      if other is not row
                      and abs(other["y"] - row["y"]) <= max(
                          1.5, 0.35 * row["size"])
                      and other["x0"] > row["x1"] - 0.5]
        limit = min(neighbours) - 2.0 if neighbours else page_w - 8.0
        row["room"] = max(row["x1"],
                          min(limit, row["x1"] + 2.0 * row["size"],
                              right + 2.0, page_w - 4.0))
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
                    and _style(row["font"]) == _style(block["font"])
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
    """A fresh page with the Russian wording, free flowing - nothing to overlap.

    Readable locked blocks (pin numbers, codes, addresses, URLs) are shown
    too, in reading order, so the Russian page is complete. Labels made of
    unmapped boxes are skipped; stray unmapped glyphs become spaces.
    """
    import io
    from pypdf import PdfReader
    from reportlab.pdfgen import canvas
    items = list(job["blocks"].get(key, []))
    for block in job["locked"].get(key, []):
        if _readable_locked(block):
            items.append(block)
    items.sort(key=lambda b: (-b["first_y"], b["x0"]))
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
    for block in items:
        text = trans.get(block["id"], block["text"])
        if _bad_glyphs(text):
            text = "".join(" " if _junk(ch) else ch for ch in text)
            text = re.sub(r"[ \t]+", " ", text).strip()
        if not text:
            continue
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


def _render(job, folder, target, partial_ok, meta_title=None, custom=None,
            facing_style="append"):
    """Rebuild the result from the source, the stripped base and the wording.

    inplace pages: stripped base + Russian drawn into the original rectangles.
    facing pages, style 'append': the original page untouched, a Russian page
    added after it. Style 'replace': only the Russian page is kept.
    kept / empty / unfinished pages are always the original.
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
            joined = "".join(s["text"] for s in spans)
            unreadable = (_junk_ratio(joined) > 0.05
                          or _outside(page, spans)
                          or any(_bad_glyphs(b["text"]) for b in keep_blocks))
            cramped = [b for b in ready
                       if (b.get("x_room", b["x1"]) - b["x0"]) < 40
                       and len(b["text"]) > 25]
            whole = re.sub(r"\s", "", page.extract_text() or "")
            captured = sum(len(re.sub(r"\s", "", b["text"]))
                           for b in keep_blocks + ready)
            covered = (captured / float(len(whole))) if whole else 1.0
            if covered < 0.92:
                warnings.append("page %d: only %d%% of its wording could be "
                                "measured - the page is kept as is and a "
                                "Russian page follows it"
                                % (number, int(100 * covered)))
            chosen = "facing" if (unreadable or cramped or len(ready) > 45
                                  or covered < 0.92) else "inplace"
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
               "locked": locked, "translations": {}, "rejects": {},
               "warnings": warnings}
        _dump_job(job, folder)
        return _payload(job, folder)

    # ---- accept translations -------------------------------------------
    def t_fill(a):
        _need_pdf()
        job, folder = _open_job(sb, a)
        entries = a.get("pages")
        if entries is None and a.get("page") is not None:
            entries = [{"page": a["page"], "blocks": a.get("blocks"),
                        "keep": a.get("keep"),
                        "on_overflow": a.get("on_overflow")}]
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
        rejects = job.setdefault("rejects", {})

        # validation pass: structural mistakes refuse the WHOLE call,
        # so a shifted id never mixes with a good translation
        staged = []
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
                staged.append((key, None, False))
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
            force = str(entry.get("on_overflow") or "").lower() == "facing"
            staged.append((key, texts, force))

        # accept pass: what fits is saved at once, the rest is reported
        rejected, notes, touched = [], [], []
        for key, texts, force in staged:
            if texts is None:
                if job["status"][key] in ("pending", "done"):
                    job["status"][key] = "kept"
                    job["mode"][key] = "kept"
                    job["translations"].pop(key, None)
                    rejects.pop(key, None)
                touched.append(int(key))
                continue
            if force and job["mode"].get(key) == "inplace":
                job["mode"][key] = "facing"
                rejects.pop(key, None)
                notes.append("page %s moved to facing on request - length "
                             "no longer matters there" % key)
            page_rejects = rejects.setdefault(key, {})
            accepted = {}
            if job["mode"].get(key) == "inplace":
                known = {b["id"]: b for b in job["blocks"].get(key, [])}
                flip = False
                for bid, value in texts.items():
                    block = known[bid]
                    lines, size, over = _fit_block(block, value,
                                                   _block_font(block),
                                                   min_scale)
                    if not over:
                        accepted[bid] = value
                        page_rejects.pop(bid, None)
                        continue
                    count = int(page_rejects.get(bid, 0)) + 1
                    page_rejects[bid] = count
                    if count >= MAX_REJECTS:
                        flip = True
                    else:
                        rejected.append(
                            {"page": int(key), "id": bid,
                             "chars": len(value),
                             "max_chars": block["max_chars"],
                             "width_pt": round(block.get("x_room",
                                                         block["x1"])
                                               - block["x0"], 1),
                             "original": block["text"][:60]})
                if flip:
                    job["mode"][key] = "facing"
                    accepted = dict(texts)
                    rejected = [r for r in rejected if r["page"] != int(key)]
                    rejects.pop(key, None)
                    notes.append("page %s moved to facing after %d refusals "
                                 "of one block - all its wording is accepted "
                                 "there" % (key, MAX_REJECTS))
            else:
                accepted = dict(texts)
            if accepted:
                job["translations"].setdefault(key, {}).update(accepted)
            job["status"][key] = ("done" if not _missing(job, key)
                                  else "pending")
            touched.append(int(key))

        _dump_job(job, folder)
        overflow, marks, facing, pages = _render(
            job, folder, os.path.join(folder, "work.pdf"), partial_ok=True)
        payload = _payload(job, folder)
        payload["filled_pages"] = touched
        payload["rejected"] = rejected
        payload["notes"] = notes
        payload["still_missing"] = {k: _missing(job, k) for k in job["status"]
                                    if job["status"][k] == "pending"}
        payload["overflow"] = [o for o in overflow if o["page"] in touched]
        if rejected:
            payload["hint"] = ("Everything that fits is ALREADY SAVED. Shorten "
                               "only the blocks listed in 'rejected' to their "
                               "max_chars and send just those again - or send "
                               "the page with on_overflow:'facing'. " + WORKFLOW)
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
        facing_style = str(a.get("facing") or "append").lower()
        if facing_style not in ("append", "replace"):
            raise ToolError("'facing' must be 'append' (original page plus a "
                            "Russian page after it) or 'replace' (only the "
                            "Russian page is kept)")
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
            meta_title=title, custom=custom, facing_style=facing_style)
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
        replaced = [f["page"] for f in facing if f.get("replaced")]
        if replaced:
            warnings.append("facing:'replace' - pages %s were replaced by "
                            "Russian pages; the drawings of those pages are "
                            "not in the result" % replaced)
        if place_note:
            warnings.append(place_note)
        return {"path": target, "pages": pages,
                "pages_source": job["pages_total"],
                "bytes": os.path.getsize(target),
                "placed_item": placed, "bookmarks": marks,
                "facing_style": facing_style,
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
               "Russian page is added right after it (used for dense tables, "
               "unreadable fonts and pages whose text cannot be fully "
               "measured). Returns a job id and 'next' - the first batch of "
               "blocks, each with its original text and max_chars. Translate "
               "them, call board_pdf_translate_fill, repeat until 'next' is "
               "null, then board_pdf_translate_finish. Markers, pin labels "
               "and unreadable labels are never offered. skip_pages leaves "
               "pages alone. Calling start again resumes; restart:true "
               "rebuilds." % sb,
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
               "never invent ids. PARTIAL ACCEPT: on 'inplace' pages every "
               "wording that fits its box is saved immediately; wording that "
               "is too long comes back in 'rejected' with its max_chars "
               "budget - shorten ONLY those blocks and send them again. A "
               "block refused twice moves its whole page to 'facing' "
               "automatically; {page: n, on_overflow: 'facing'} does that at "
               "once, and on a facing page length never matters. A page is "
               "finished only when every block is translated; 'still_missing' "
               "lists what is left. {page: n, keep: true} keeps a page "
               "untouched.",
               S({"job": job_arg,
                  "pages": {"type": "array",
                            "items": {"type": "object"},
                            "description": "[{page, blocks:{id:text}, "
                                           "on_overflow:'facing'} or "
                                           "{page, keep:true}]"},
                  "page": {"type": "integer"},
                  "blocks": {"type": "object"},
                  "on_overflow": {"type": "string",
                                  "description": "'facing' to move this page "
                                                 "to facing mode right away"},
                  "keep": {"type": "boolean"}},
                 ["job"]),
               t_fill)

    tools._add("board_pdf_translate_finish",
               "Assemble the translated PDF INSIDE the agent folder '%s' "
               "(default name <job>_ru.pdf) and place it on the board next to "
               "the source (place:false to skip). Refuses while pages are "
               "unfinished unless allow_partial:true. 'facing' pages ship in "
               "one of two styles: facing:'append' (default) keeps the "
               "original page and adds a Russian page after it, so the result "
               "may have more pages than the source; facing:'replace' keeps "
               "only the Russian page, giving a fully Russian document but "
               "losing the drawings of those pages (reported in warnings). "
               "Bookmarks are remapped to the first output page of each "
               "source page. Pass 'outline' [{title, page, parent}] to "
               "replace them with translated titles (page numbers of the "
               "RESULT). Existing files need overwrite:true." % sb,
               S({"job": job_arg,
                  "out": {"type": "string",
                          "description": "Result file inside the agent folder"},
                  "facing": {"type": "string",
                             "description": "'append' (default) or 'replace'"},
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

        def refuse(label, result):
            print("%s -> %s (expected False) %s"
                  % (label, result.get("ok"), result.get("error", "")[:110]))
            if result.get("ok"):
                fails.append(label)

        placed = []

        def remember(result):
            uid = (result.get("placed_item") or {}).get("uid")
            if uid:
                placed.append(uid)

        def job_of(job_id):
            return _load_job(_job_paths(sb, job_id)[1])

        # ---- basic inplace flow -------------------------------------
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
        job_id = st.get("job")
        expect("page is inplace", (st.get("modes") or {}).get("1") == "inplace")
        items = (st.get("next") or {}).get("items") or []
        expect("blocks found", len(items) >= 2)
        expect("ids are t*", all(i["id"].startswith("t") for i in items))
        expect("budget offered", all(i.get("max_chars", 0) > 0 for i in items))
        words = ["Глава первая",
                 "Привет, мир. Здесь описана установка модулей памяти.",
                 "Второй абзац о кулере процессора."]
        first = items[0]
        part = tools.call("board_pdf_translate_fill",
                          {"job": job_id, "page": 1,
                           "blocks": {first["id"]: words[0]}})
        show("partial fill", part, "progress")
        expect("page still pending",
               (part.get("progress") or {}).get("pending") == 1)
        expect("missing listed",
               bool((part.get("still_missing") or {}).get("1")))
        # a job written before v4 has no 'rejects' - loading must tolerate it
        old = job_of(job_id)
        old.pop("rejects", None)
        _dump_job(old, _job_paths(sb, job_id)[0])
        rest = {item["id"]: words[index % len(words)]
                for index, item in enumerate(items)}
        full = tools.call("board_pdf_translate_fill",
                          {"job": job_id, "page": 1, "blocks": rest})
        show("full fill", full, "progress")
        expect("old job tolerated", full.get("ok") is True)
        expect("no pending", (full.get("progress") or {}).get("pending") == 0)
        expect("next is null", full.get("next") is None)
        expect("nothing rejected", not full.get("rejected"))
        expect("no overflow", not full.get("overflow"))
        work = os.path.join(sb, "translate", job_id or "", "work.pdf")
        text = ""
        if os.path.isfile(work):
            text = _reader(work).pages[0].extract_text() or ""
        expect("russian visible", "Привет" in text)
        expect("english gone", "Hello world" not in text)
        tools.call("board_pdf_translate_fill",
                   {"job": job_id, "page": 1,
                    "blocks": {first["id"]: "Глава первая"}})
        text2 = ""
        if os.path.isfile(work):
            text2 = _reader(work).pages[0].extract_text() or ""
        expect("redraw not doubled", text2.count("Глава первая") == 1)
        refuse("unknown block id",
               tools.call("board_pdf_translate_fill",
                          {"job": job_id, "page": 1,
                           "blocks": {"t999": "x"}}))
        refuse("empty translation",
               tools.call("board_pdf_translate_fill",
                          {"job": job_id, "page": 1,
                           "blocks": {first["id"]: "   "}}))
        refuse("page outside job",
               tools.call("board_pdf_translate_fill",
                          {"job": job_id, "page": 9,
                           "blocks": {"t1": "x"}}))
        fin = tools.call("board_pdf_translate_finish",
                         {"job": job_id, "out": p("final.pdf"),
                          "overwrite": True})
        show("finish", fin, "pages", "bookmarks", "facing_style")
        remember(fin)
        expect("placed on board",
               (fin.get("placed_item") or {}).get("kind") == "document")
        expect("no extra pages", fin.get("pages") == 1)
        expect("default style append", fin.get("facing_style") == "append")
        refuse("unknown job",
               tools.call("board_pdf_translate_finish",
                          {"job": "no_such_job"}))
        outside = os.path.join(os.path.dirname(sb), "tr_escape.pdf")
        refuse("write outside",
               tools.call("board_pdf_translate_finish",
                          {"job": job_id, "out": outside}))
        print("outside created ->", os.path.exists(outside),
              "(expected False)")
        if os.path.exists(outside):
            fails.append("sandbox escape")

        # ---- skip / early finish / allow_partial / resume --------------
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
        text3 = ""
        if fin2.get("ok"):
            text3 = _reader(p("kept.pdf")).pages[0].extract_text() or ""
        expect("original preserved", "Hello world" in text3)
        st3 = tools.call("board_pdf_translate_start",
                         {"path": p("src.pdf"), "restart": True})
        refuse("finish too early",
               tools.call("board_pdf_translate_finish",
                          {"job": st3.get("job"), "out": p("early.pdf")}))
        part2 = tools.call("board_pdf_translate_finish",
                           {"job": st3.get("job"), "out": p("partial.pdf"),
                            "allow_partial": True, "place": False,
                            "overwrite": True})
        show("allow_partial", part2, "left_in_original")
        text4 = ""
        if part2.get("ok"):
            text4 = _reader(p("partial.pdf")).pages[0].extract_text() or ""
        expect("partial keeps english", "Hello world" in text4)
        resume = tools.call("board_pdf_translate_start",
                            {"path": p("src.pdf")})
        expect("resume works", resume.get("resumed") is True)

        # ---- partial accept + auto facing + append / replace -----------
        made2 = tools.call("board_pdf_create",
                           {"path": p("tiny.pdf"),
                            "text": "OK\n\n" + ("A very long paragraph about "
                                                "memory modules. " * 6),
                            "font_size": 12, "overwrite": True})
        show("create tiny", made2, "pages")
        st_t = tools.call("board_pdf_translate_start",
                          {"path": p("tiny.pdf"), "restart": True})
        show("start tiny", st_t, "modes")
        tiny_id = st_t.get("job")
        items_t = (st_t.get("next") or {}).get("items") or []
        target = None
        for item in items_t:
            if item["text"].strip() == "OK":
                target = item
                break
        if target is None:
            fails.append("no small block found")
        else:
            long_ru = ("Очень длинный русский текст, который никак "
                       "не помещается в маленькую клетку заголовка.")
            others = {i["id"]: "Короткая русская строка."
                      for i in items_t if i["id"] != target["id"]}
            send = dict(others)
            send[target["id"]] = long_ru
            mix = tools.call("board_pdf_translate_fill",
                             {"job": tiny_id, "page": 1, "blocks": send})
            show("mixed fill", mix, "rejected", "progress")
            expect("call accepted", mix.get("ok") is True)
            rej = mix.get("rejected") or []
            expect("long block rejected",
                   any(r["id"] == target["id"] for r in rej))
            expect("budget in rejected",
                   all(r.get("max_chars", 0) > 0 and "width_pt" in r
                       for r in rej))
            expect("page still pending",
                   (mix.get("progress") or {}).get("pending") == 1)
            saved = job_of(tiny_id)
            expect("fitting part saved",
                   all(bid in saved["translations"].get("1", {})
                       for bid in others))
            expect("long part not saved",
                   target["id"] not in saved["translations"].get("1", {}))
            expect("refusal counted",
                   saved.get("rejects", {}).get("1", {})
                   .get(target["id"]) == 1)
            again = tools.call("board_pdf_translate_fill",
                               {"job": tiny_id, "page": 1,
                                "blocks": {target["id"]: long_ru}})
            show("second refusal", again, "modes", "notes")
            expect("auto facing",
                   (again.get("modes") or {}).get("1") == "facing")
            expect("all accepted after flip", not again.get("rejected"))
            expect("page done after flip",
                   (again.get("progress") or {}).get("pending") == 0)
            fin_a = tools.call("board_pdf_translate_finish",
                               {"job": tiny_id, "out": p("tiny_append.pdf"),
                                "overwrite": True, "place": False})
            show("finish append", fin_a, "pages", "facing_style")
            expect("append adds a page", fin_a.get("pages") == 2)
            if fin_a.get("ok"):
                ra = _reader(p("tiny_append.pdf"))
                expect("append keeps original",
                       "A very long paragraph"
                       in (ra.pages[0].extract_text() or ""))
                expect("append russian page",
                       "Очень" in (ra.pages[1].extract_text() or ""))
            fin_r = tools.call("board_pdf_translate_finish",
                               {"job": tiny_id, "out": p("tiny_replace.pdf"),
                                "facing": "replace",
                                "overwrite": True, "place": False})
            show("finish replace", fin_r, "pages", "facing_style", "warnings")
            expect("replace is ru only", fin_r.get("pages") == 1)
            expect("replace style reported",
                   fin_r.get("facing_style") == "replace")
            expect("replace warns honestly",
                   any("replace" in str(w)
                       for w in fin_r.get("warnings") or []))
            if fin_r.get("ok"):
                rr = _reader(p("tiny_replace.pdf"))
                all_text = " ".join(pg.extract_text() or ""
                                    for pg in rr.pages)
                expect("replace has russian", "Очень" in all_text)
                expect("replace dropped english",
                       "A very long paragraph" not in all_text)
        refuse("bad facing style",
               tools.call("board_pdf_translate_finish",
                          {"job": tiny_id, "out": p("bad.pdf"),
                           "facing": "sideways", "place": False}))

        # ---- on_overflow facing + readable locked blocks ----------------
        made3 = tools.call("board_pdf_create",
                           {"path": p("flow.pdf"),
                            "text": "Chapter Two\n\n12345\n\n" +
                                    ("This section explains the setup. " * 12),
                            "font_size": 11, "overwrite": True})
        show("create flow", made3, "pages")
        st4 = tools.call("board_pdf_translate_start",
                         {"path": p("flow.pdf"), "restart": True})
        show("start flow", st4, "modes")
        flow_id = st4.get("job")
        job4 = job_of(flow_id)
        expect("flow is inplace", job4["mode"].get("1") == "inplace")
        expect("numeric block locked",
               any(b["text"].strip() == "12345"
                   for b in job4["locked"].get("1", [])))
        blocks4 = {}
        for b in job4["blocks"]["1"]:
            if b["text"].startswith("Chapter"):
                blocks4[b["id"]] = "Глава вторая"
            else:
                blocks4[b["id"]] = "Этот раздел объясняет настройку. " * 20
        fill4 = tools.call("board_pdf_translate_fill",
                           {"job": flow_id, "page": 1, "blocks": blocks4,
                            "on_overflow": "facing"})
        show("on_overflow fill", fill4, "modes", "rejected")
        expect("forced facing",
               (fill4.get("modes") or {}).get("1") == "facing")
        expect("nothing rejected on facing", not fill4.get("rejected"))
        expect("flow done",
               (fill4.get("progress") or {}).get("pending") == 0)
        fin4 = tools.call("board_pdf_translate_finish",
                          {"job": flow_id, "out": p("flow_append.pdf"),
                           "overwrite": True, "place": False})
        show("flow finish append", fin4, "pages", "russian_pages_added")
        expect("flow page added", (fin4.get("pages") or 0) >= 2)
        if fin4.get("ok"):
            reader4 = _reader(p("flow_append.pdf"))
            expect("flow original untouched",
                   "This section explains"
                   in (reader4.pages[0].extract_text() or ""))
            ru_text = reader4.pages[1].extract_text() or ""
            expect("flow russian readable", "Этот" in ru_text)
            expect("readable locked shown", "12345" in ru_text)
        fin5 = tools.call("board_pdf_translate_finish",
                          {"job": flow_id, "out": p("flow_replace.pdf"),
                           "facing": "replace",
                           "overwrite": True, "place": False})
        show("flow finish replace", fin5, "pages")
        if fin5.get("ok"):
            reader5 = _reader(p("flow_replace.pdf"))
            all5 = " ".join(pg.extract_text() or "" for pg in reader5.pages)
            expect("flow replace ru only",
                   "This section explains" not in all5 and "Этот" in all5)

        if placed:
            tools.call("board_delete", {"uids": placed})
        print("GUARD FAILS:", len(fails), fails)
        print("files are in ->", folder)
        print("DONE - close the window to exit")

    QTimer.singleShot(800, run)
    sys.exit(app.exec())


if __name__ == "__main__":
    _selftest()