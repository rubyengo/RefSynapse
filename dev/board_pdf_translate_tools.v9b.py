"""PDF translation for the RefSynapse agent - batch 3, engine v8c.

board_pdf_translate_start  - open a job: copy the source, remember where every
                             text block sat (with its colour), hand out the
                             first batch of blocks to translate
board_pdf_translate_fill   - accept translated blocks, track progress, hand
                             out the next batch
board_pdf_translate_finish - assemble the final PDF and place it on the board
                             next to the source document

HOW IT WORKS. A PDF page is drawing commands. Text-showing commands are
removed from a working copy, which leaves every line, curve, table rule and
picture untouched. The original text is read WITH its coordinates first,
grouped into blocks (paragraphs, headings, table cells), and the translation
is drawn back into the same rectangles with word wrap. The model in the chat
does the translating; these tools orchestrate, draw and keep score.

TEXT INSIDE CONTAINERS (v7). Many PDFs keep their wording inside form
XObjects - reusable sub-drawings. The lift-out, the colour reader and the
full-strip all walk INTO those containers (recursively, with the placement
matrix and the fill colour at the moment of use), and each page gets private
copies of its containers so a shared form is never damaged for other pages.
Without this, a cover whose text lives in a form could not be cleaned and
wrongly fell back to extra pages.

MAXIMUM LIKENESS (v6/v7). finish defaults to facing:'mirror' and that is the
STANDARD delivery: the SAME page count and layout as the source, always.
Only translated wording is lifted out; pictures, table rules and pin labels
stay; Russian is drawn in the same places with the same colour, bold/italic
weight and serif/sans character. Fitting ladder: free space below the block,
shrink to half size, tighten line spacing, shrink to 4 pt, then squeeze
horizontally to 55 percent as the very last resort. Mirror NEVER adds pages: if the surgical lift-out cannot
be verified the page is fully stripped and redrawn (wording + readable
labels); if even that is impossible the page ships untouched and is reported
in one line. Modes are never discussed with the user; 'append' and 'replace'
exist solely for an explicit user request.

V8 RELIABILITY. Every translated line is verified gone after the
lift-out (zero tolerance); identical translations are never
touched; the full-strip fallback walks every container by
resources and removes whole text objects, then verifies again.
V8C HONESTY. Text extraction from an in-memory page can differ
from the saved document, so every check runs on a file that was
written and read back, and the finished PDF is audited and
repaired once. Hard line breaks are reflowed in mirror, and a
block never grows sideways into a neighbour's ink.
Mirror fitting keeps a 1 pt vertical gap above the neighbour
below, with no baseline slack; the last rungs squeeze horizontally
to 60 and 55 percent instead of touching a neighbour. Kept wording
is protected by its own ink width, and list markers are redrawn
when a page had to be cleaned fully.
PARTIAL ACCEPT (v4). What fits is saved immediately; what does not fit comes
back in 'rejected' with its character budget. A block refused twice moves its
page to 'facing' internally, where length never matters.

HONEST LIMITS. Text inside raster screenshots is a picture - it stays in the
original language. Bookmarks are preserved and remapped. There is no OCR.
Shrinking to 4 pt or squeezing is reported, not hidden.

SANDBOX. Everything lives inside agent\\translate\\<job>\\ while working; the
final file goes into the agent folder. No silent overwrites.

Registered from board_pdf_edit_tools.register_pdf_edit_tools().
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
    from pypdf.generic import (ContentStream, IndirectObject, DictionaryObject,
                               NameObject, DecodedStreamObject)
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
MAX_FILL_CHARS = 40_000
BATCH_CHARS = 6000
BATCH_PAGES = 6
MAX_REJECTS = 2
MAX_SPANS_PER_PAGE = 4000
MAX_BLOCKS_PER_PAGE = 400
MIRROR_SCALE = 0.5
MIRROR_FLOOR = 4.0
MIRROR_GAP = 1.0
MIRROR_SQUEEZE = (100, 90, 80, 70, 60, 55)

SHOW_OPS = {b"Tj", b"TJ", b"'", b'"'}
BULLET_STARTS = (chr(0x2022), chr(0x25CF), chr(0x25AA), chr(0xF0B7), chr(0x2D), chr(0x2013))

WORKFLOW = ("Workflow: start -> translate every item of 'next' (use its id and "
            "its original text) -> fill with {page, blocks:{id: text}} -> "
            "repeat until 'next' is null -> finish. Wording that fits is saved "
            "at once; wording that is too long comes back in 'rejected' with "
            "its character budget - shorten ONLY those blocks and send them "
            "again. A page counts as finished only when all its blocks are "
            "translated. keep:true leaves a page alone. At finish the source "
            "appearance and page count are preserved by default (mirror) - "
            "never discuss modes with the user, just deliver the look-alike "
            "file.")


# ------------------------------------------------------------------ job files
JOB_VERSION = 3


def _job_id(src):
    stem = os.path.splitext(os.path.basename(src))[0]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "", stem).strip("._")
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
    job.setdefault("rejects", {})
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
    if ch in "\n\r\t ":
        return False
    code = ord(ch)
    return (code < 32 or 0x80 <= code <= 0x9F
            or 0xE000 <= code <= 0xF8FF
            or code in (0xFFFD, 0x25A1, 0x25A0))


def _bad_glyphs(text):
    return any(_junk(ch) for ch in text)


def _junk_ratio(text):
    solid = re.sub(r"\s", "", text)
    if not solid:
        return 0.0
    return sum(1 for ch in solid if _junk(ch)) / float(len(solid))


def _classify(blocks):
    locked, ready = [], []
    for block in blocks:
        text = block["text"]
        if not re.search(r"[A-Za-zЀ-ӿ]", text):
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
    text = str(block.get("text", ""))
    if not re.search(r"[A-Za-z0-9Ѐ-ӿ]", text):
        return False
    return _junk_ratio(text) <= 0.2


def _capacity(block):
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


def _resolve(obj):
    return obj.get_object() if isinstance(obj, IndirectObject) else obj


def _mat_mul(a, b):
    return [a[0] * b[0] + a[1] * b[2],
            a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2],
            a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4],
            a[4] * b[1] + a[5] * b[3] + b[5]]


def _rect_of(block):
    return (block["x0"] - 1.5,
            block["y_bot"] - 2.0,
            float(block.get("x_room", block["x1"])) + 1.5,
            block["first_y"] + 0.85 * block["size"])


def _in_rect(rect, x, y):
    return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


def _image_rects(page):
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
        if ink[0] < r[2] - 1 and ink[2] > r[0] + 1 \
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
    """Point every internal jump at the LIVE page object of the result.
    Object numbers differ between files, so match annotations by their
    ORDER on the matching source page, read the intended target there,
    and aim it at the result page via the assembly mapping."""
    src_ids = _page_ids(src)
    inv = {v: k for k, v in mapping.items()}
    fixed = 0
    for oi, page in enumerate(out.pages):
        si = inv.get(oi)
        if si is None or si >= len(src.pages):
            continue
        s_ann = _annots_of(src.pages[si])
        o_ann = _annots_of(page)
        for k, obj in enumerate(o_ann):
            if k >= len(s_ann):
                break
            d = _dest_array(obj)
            sd = _dest_array(s_ann[k])
            if d is None or sd is None:
                continue
            head = sd[0]
            if not isinstance(head, IndirectObject):
                continue
            t = src_ids.get(head.idnum)
            if t is None:
                continue
            live = mapping.get(t, t)
            if live >= len(out.pages):
                continue
            ref = out.pages[live].indirect_reference
            if ref is not None:
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
    return re.sub(r"\s", "", text or "")


# ------------------------------------------- containers (form XObjects), v7
def _private_resources(owner, writer):
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

def _hex_of(values):
    out = [max(0, min(255, int(round(255.0 * float(v))))) for v in values]
    return "#%02x%02x%02x" % (out[0], out[1], out[2])


def _cmyk_hex(c, m, y, k):
    return _hex_of([(1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)])


def _replay(operations, ctm, fill, res, pdf, writer, mode,
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
                elif walk and mode in ("colors", "runs"):
                    seen.add(fid)
                if walk:
                    try:
                        if writer is not None and mode not in ("colors", "runs"):
                            child_res = _private_resources(form, writer)
                            child_seen = seen | {fid}
                        else:
                            child_res = (_resolve(form.get("/Resources"))
                                         or res)
                            child_seen = (seen if mode in ("colors", "runs")
                                          else seen | {fid})
                        inner = ContentStream(form, pdf).operations
                        kept_child = _replay(inner, ctm[:], fill,
                                             child_res, pdf, writer, mode,
                                             trans_rects, lock_rects,
                                             samples, depth + 1, child_seen)
                        if writer is not None and mode not in ("colors", "runs"):
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
                if mode in ("colors", "runs"):
                    if mode == "colors":
                        samples.append((x, y, fill))
                    else:
                        raw = operands[-1] if operands else ""
                        if isinstance(raw, list):
                            raw = "".join(str(v) for v in raw
                                          if not isinstance(v, (int, float)))
                        samples.append((x, y, fill, str(raw)))
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

def _text_free(operations):
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


def _freeze(writer, folder, name):
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

def _block_needles(block, forbidden):
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


def _lock_rect_of(block):
    """Protection area of kept wording: its OWN ink, never the room it
    could grow into (x_room). An inflated bullet rect used to shield the
    first show command of the neighbouring translated line.
    """
    return (block["x0"] - 1.0,
            block["y_bot"] - 1.0,
            block["x1"] + 1.0,
            block["first_y"] + 0.85 * block["size"])


def _marker_locked(block):
    """True for a pure list marker (bullet, dash). It is redrawn with the
    wording when a page had to be cleaned fully, so lists keep bullets.
    """
    text = str(block.get("text", "")).strip()
    if not text or len(text) > 3:
        return False
    return all(ch in BULLET_STARTS or ch in ".)]" for ch in text)


def _surgical_strip(writer, index, trans_blocks, locked_blocks):
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

def _text_colors(page):
    """(x, y, '#rrggbb') of every show command, page AND containers."""
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
            None, "colors", [], [], samples)
    return samples


def _block_color(block, samples):
    rect = _rect_of(block)
    hits = [c for x, y, c in samples if _in_rect(rect, x, y)]
    if not hits:
        return "#000000"
    return max(set(hits), key=hits.count)


def _color_runs(page):
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


def _block_accents(block, runs):
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


def _expanded_block(block, all_blocks):
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
    for r in block.get("avoid") or []:
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

_METRICS = {}


def _font_metrics(font_dict):
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
    table, default = _font_metrics(font_dict)
    total = 0.0
    for ch in text:
        total += table.get(ch, default)
    return total / 1000.0 * size


def _page_spans(page):
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
    name = str(font_name).lower()
    bold = any(t in name for t in ("bold", "black", "heavy", "semibold"))
    italic = "italic" in name or "oblique" in name
    return bold, italic


def _serif_name(font_name):
    name = str(font_name).lower()
    if "sans" in name:
        return False
    return any(t in name for t in ("times", "serif", "georgia", "cambria",
                                   "garamond", "palatino", "minion",
                                   "century", "book antiqua"))


def _group_blocks(spans, page_w=595.0):
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


def _variant(kind, serif=False):
    family = "RefSerif " if serif else "RefSans "
    key = family + kind
    if key in _VARIANTS:
        return _VARIANTS[key]
    folder = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    if serif:
        choices = {"Bold": ("timesbd.ttf",),
                   "Italic": ("timesi.ttf",),
                   "BoldItalic": ("timesbi.ttf",)}.get(kind, ("times.ttf",))
    else:
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
    _font_name()
    name = str(block.get("font", "")).lower()
    bold = any(t in name for t in ("bold", "black", "heavy", "semibold"))
    italic = "italic" in name or "oblique" in name
    serif = _serif_name(name)
    if bold and italic:
        return _variant("BoldItalic", serif)
    if bold:
        return _variant("Bold", serif)
    if italic:
        return _variant("Italic", serif)
    if serif:
        return _variant("Regular", True)
    return _font_name()


def _fit_block(block, text, font, min_scale, floor_abs=5.0, tight=False):
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
        text, _unused_marks = _strip_marks(text)
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
            doc.setFillColor(HexColor(block.get("color") or "#000000"))
            doc.setFont(face, size)
            if line:
                doc.drawString(margin, y - size, line)
            y -= step
        y -= 4
    doc.showPage()
    doc.save()
    buffer.seek(0)
    return list(PdfReader(buffer).pages)


_URL_RE = re.compile(r"(?:https?://|www\.)[^\s\]\)>,;\"]+")
_MARK_RE = re.compile(r"\[\[(#[0-9a-fA-F]{6})\]\](.*?)\[\[/\]\]", re.S)


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
    norm = re.sub(r"\s+", " ", plain).strip()
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
    url_col = next((c for t, c in accents
                    if "http" in t or "www." in t), None)
    if url_col:
        for m in _URL_RE.finditer(line):
            if free(m.start(), m.end()):
                cuts.append((m.start(), m.end(), url_col))
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


def _render(job, folder, target, partial_ok, meta_title=None, custom=None,
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
            runs = _color_runs(src.pages[index])
            imgs = _image_rects(src.pages[index])
            for block in all_blocks:
                block["color"] = _block_color(block, samples)
                block["accent_parts"] = _block_accents(block, runs)
                block["avoid"] = _avoid_for(block, imgs)
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

                placed = []

                def drawer(doc, p=p, page_over=page_over, key=key,
                           min_scale=min_scale, placed=placed):
                    _draw_blocks(doc, p["all"], p["trans"], p["all"],
                                 page_over, key, min_scale, 5.0,
                                 False, False, placed)
                page.merge_page(_overlay_page(width, height, drawer))
                _move_links(page, p["all"], placed)
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
        _relink_annots(src, out, mapping)
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

# --------------------------------------------------------------- registration
def register_pdf_translate_tools(tools):
    """Batch 3: translate a PDF, keeping its exact appearance."""
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
                                "measured - mirror will redraw it fully"
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
                                        "out cleanly - mirror will redraw it "
                                        "fully" % number)
            samples = _text_colors(page)
            runs = _color_runs(page)
            imgs = _image_rects(page)
            ready_out = []
            for position, block in enumerate(ready, 1):
                item = dict(block)
                item["id"] = "t%d" % position
                item["max_chars"] = _capacity(item)
                item["color"] = _block_color(item, samples)
                item["accent_parts"] = _block_accents(item, runs)
                item["avoid"] = _avoid_for(item, imgs)
                ready_out.append(item)
            locked_out = []
            for position, block in enumerate(keep_blocks, 1):
                item = dict(block)
                item["id"] = "k%d" % position
                item["color"] = _block_color(item, samples)
                item["accent_parts"] = _block_accents(item, runs)
                item["avoid"] = _avoid_for(item, imgs)
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
                    lines, size, over, lead, hs = _fit_block(
                        block, value, _block_font(block), min_scale)
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
        overflow, marks, facing, pages, mirror_info = _render(
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
        facing_style = str(a.get("facing") or "mirror").lower()
        if facing_style not in ("append", "replace", "mirror"):
            raise ToolError("'facing' must be 'mirror' (default - same page "
                            "count and layout), 'append' or 'replace'; use "
                            "the last two only on an explicit user request")
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
        overflow, marks, facing, pages, mirror_info = _render(
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
        if mirror_info["requested"]:
            if mirror_info["redrawn"]:
                warnings.append("pages %s were fully redrawn (wording and "
                                "labels) to keep the layout"
                                % mirror_info["redrawn"])
            if mirror_info["fallback"]:
                warnings.append("pages %s could not be cleaned and ship in "
                                "the original language"
                                % mirror_info["fallback"])
            if mirror_info.get("leftovers"):
                warnings.append("pages %s still show some original wording"
                                % mirror_info["leftovers"])
            if mirror_info["unplaced"]:
                warnings.append("pages %s have wording squeezed to the limit"
                                % mirror_info["unplaced"])
            if mirror_info["min_font"] is not None \
                    and mirror_info["min_font"] < 5.0:
                warnings.append("mirror shrank some fine print to %.1f pt "
                                "to keep the layout" % mirror_info["min_font"])
        if place_note:
            warnings.append(place_note)
        return {"path": target, "pages": pages,
                "pages_source": job["pages_total"],
                "bytes": os.path.getsize(target),
                "placed_item": placed, "bookmarks": marks,
                "facing_style": facing_style,
                "mirror": mirror_info,
                "russian_pages_added": facing,
                "left_in_original": pending if partial else [],
                "kept_pages": sorted(int(k) for k, v in job["mode"].items()
                                     if v == "kept"),
                "empty_pages": sorted(int(k) for k, v in job["mode"].items()
                                      if v == "empty"),
                "overflow": overflow[:40], "warnings": warnings[:20],
                "hint": "Done - same page count and layout as the source, "
                        "colours and font character kept. Screenshots stay "
                        "in the original language."}

    tools._add("board_pdf_translate_start",
               "Begin translating a PDF while KEEPING its exact appearance. "
               "Source is 'path' INSIDE the agent folder '%s' or 'uid' of a "
               "board document. The result ALWAYS keeps the source layout, "
               "page count, colours and font character - never ask the user "
               "about modes, just translate and deliver. Returns a job id and "
               "'next' - the first batch of blocks with original text and "
               "max_chars. Translate them, call board_pdf_translate_fill, "
               "repeat until 'next' is null, then board_pdf_translate_finish. "
               "Markers, pin labels and unreadable labels are never offered. "
               "skip_pages leaves pages alone. Calling start again resumes; "
               "restart:true rebuilds." % sb,
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
               "never invent ids. Colour accents: URLs and unchanged names keep their original colour automatically; you may wrap a translated fragment in [[#rrggbb]]...[[/]] to force its colour. PARTIAL ACCEPT: wording that fits is saved "
               "immediately; wording that is too long comes back in "
               "'rejected' with its max_chars budget - shorten ONLY those "
               "blocks and send them again. A block refused twice moves its "
               "page to 'facing' internally; {page: n, on_overflow: 'facing'} "
               "does that at once. A page is finished only when every block "
               "is translated; 'still_missing' lists what is left. "
               "{page: n, keep: true} keeps a page untouched.",
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
               "unfinished unless allow_partial:true. THE DEFAULT AND "
               "STANDARD delivery is 'mirror': SAME page count and layout, "
               "always - mirror never adds pages. Only translated wording is "
               "lifted out (including text inside form containers); pictures, "
               "table rules, pin labels, colours and serif/sans character "
               "stay. Fitting ladder: free space below, half size, tighter "
               "spacing, 4 pt floor, horizontal squeeze to 55 percent as the last resort. Never discuss "
               "modes with the user. 'append' and 'replace' only on explicit "
               "user request. Bookmarks are remapped; pass 'outline' "
               "[{title, page, parent}] for translated titles. Existing files "
               "need overwrite:true." % sb,
               S({"job": job_arg,
                  "out": {"type": "string",
                          "description": "Result file inside the agent folder"},
                  "facing": {"type": "string",
                             "description": "'mirror' (default, keeps the "
                                            "source appearance and page "
                                            "count); 'append' or 'replace' "
                                            "only on explicit user request"},
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
        expect("default style mirror", fin.get("facing_style") == "mirror")
        fin_m0 = tools.call("board_pdf_translate_finish",
                            {"job": job_id, "out": p("final_mirror.pdf"),
                             "facing": "mirror", "overwrite": True,
                             "place": False})
        show("finish mirror inplace", fin_m0, "pages", "mirror")
        expect("mirror inplace one page", fin_m0.get("pages") == 1)
        expect("mirror inplace listed",
               1 in ((fin_m0.get("mirror") or {}).get("mirrored") or []))
        if fin_m0.get("ok"):
            tm0 = _reader(p("final_mirror.pdf")).pages[0].extract_text() or ""
            expect("mirror inplace russian", "Привет" in tm0)
            expect("mirror inplace english gone", "Hello world" not in tm0)
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
                                "facing": "append",
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
            fin_m = tools.call("board_pdf_translate_finish",
                               {"job": tiny_id, "out": p("tiny_mirror.pdf"),
                                "facing": "mirror",
                                "overwrite": True, "place": False})
            show("finish mirror", fin_m, "pages", "mirror")
            expect("mirror keeps one page", fin_m.get("pages") == 1)
            mir = fin_m.get("mirror") or {}
            expect("mirror listed page", 1 in (mir.get("mirrored") or []))
            expect("mirror no fallback", not mir.get("fallback"))
            if fin_m.get("ok"):
                rm = _reader(p("tiny_mirror.pdf"))
                mt = rm.pages[0].extract_text() or ""
                expect("mirror russian in place", "Очень" in mt)
                expect("mirror english lifted",
                       "A very long paragraph" not in mt)
            fin_d = tools.call("board_pdf_translate_finish",
                               {"job": tiny_id, "out": p("tiny_default.pdf"),
                                "overwrite": True, "place": False})
            show("finish default facing job", fin_d, "pages", "facing_style")
            expect("default keeps one page", fin_d.get("pages") == 1)
            expect("default is mirror", fin_d.get("facing_style") == "mirror")
        refuse("bad facing style",
               tools.call("board_pdf_translate_finish",
                          {"job": tiny_id, "out": p("bad.pdf"),
                           "facing": "sideways", "place": False}))
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
                           "facing": "append",
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
        fin6 = tools.call("board_pdf_translate_finish",
                          {"job": flow_id, "out": p("flow_mirror.pdf"),
                           "facing": "mirror",
                           "overwrite": True, "place": False})
        show("flow finish mirror", fin6, "pages", "mirror")
        expect("flow mirror one page", fin6.get("pages") == 1)
        if fin6.get("ok"):
            reader6 = _reader(p("flow_mirror.pdf"))
            mt6 = reader6.pages[0].extract_text() or ""
            expect("flow mirror russian", "Этот" in mt6)
            expect("flow mirror english lifted",
                   "This section explains" not in mt6)
            expect("flow mirror locked kept", "12345" in mt6)
        # ---- colour + serif fidelity (v6) ------------------------------
        import io as _io
        from reportlab.pdfgen import canvas as _cv
        buf = _io.BytesIO()
        cv = _cv.Canvas(buf, pagesize=(595, 842))
        cv.setFont(_font_name(), 16)
        cv.setFillColor(HexColor("#cc0000"))
        cv.drawString(72, 760, "Color heading")
        cv.setFillColor(HexColor("#000000"))
        cv.setFont(_font_name(), 11)
        cv.drawString(72, 720, "Black body text about the device.")
        cv.showPage()
        cv.save()
        with open(p("color.pdf"), "wb") as stream:
            stream.write(buf.getvalue())
        st_c = tools.call("board_pdf_translate_start",
                          {"path": p("color.pdf"), "restart": True})
        show("start color", st_c, "modes")
        cid = st_c.get("job")
        jc = job_of(cid)
        head = next((b for b in jc["blocks"].get("1", [])
                     if b["text"].startswith("Color")), None)
        body = next((b for b in jc["blocks"].get("1", [])
                     if b["text"].startswith("Black")), None)
        expect("heading colour stored",
               head is not None and head.get("color") == "#cc0000")
        expect("body colour stored",
               body is not None and body.get("color") == "#000000")
        expect("serif detected", _serif_name("/ABC+TimesNewRomanPS-BoldMT"))
        expect("sans not serif", not _serif_name("/ABC+ArialMT"))
        if head and body:
            fc = tools.call("board_pdf_translate_fill",
                            {"job": cid, "page": 1,
                             "blocks": {head["id"]: "Цветной заголовок",
                                        body["id"]: "Чёрный текст об устройстве."}})
            show("color fill", fc, "progress")
            fin_c = tools.call("board_pdf_translate_finish",
                               {"job": cid, "out": p("color_mirror.pdf"),
                                "overwrite": True, "place": False})
            show("finish color default", fin_c, "pages", "facing_style")
            expect("color job one page", fin_c.get("pages") == 1)
            expect("color job default mirror",
                   fin_c.get("facing_style") == "mirror")
            if fin_c.get("ok"):
                out_page = _reader(p("color_mirror.pdf")).pages[0]
                cols = {c for _, _, c in _text_colors(out_page)}
                expect("red survives mirror", "#cc0000" in cols)
                expect("black survives mirror", "#000000" in cols)
                ct = out_page.extract_text() or ""
                expect("color russian present", "Цветной" in ct)
        # ---- text inside a form container (v7, the cover case) ---------
        from pypdf import PdfReader as _PR
        from pypdf.generic import (ArrayObject, FloatObject,
                                   DictionaryObject as _DO,
                                   NameObject as _NO)
        buf = _io.BytesIO()
        cv = _cv.Canvas(buf, pagesize=(297, 420))
        cv.setFillColor(HexColor("#808080"))
        cv.rect(40, 40, 217, 340, fill=1, stroke=0)
        cv.showPage()
        cv.save()
        w = PdfWriter(clone_from=_PR(_io.BytesIO(buf.getvalue())))
        pg = w.pages[0]
        helv = _DO({_NO("/Type"): _NO("/Font"),
                    _NO("/Subtype"): _NO("/Type1"),
                    _NO("/BaseFont"): _NO("/Helvetica")})
        form = DecodedStreamObject()
        form.set_data(b"1 1 1 rg BT /F1 16 Tf 1 0 0 1 60 380 Tm "
                      b"(COVER TITLE) Tj ET")
        form[_NO("/Type")] = _NO("/XObject")
        form[_NO("/Subtype")] = _NO("/Form")
        form[_NO("/BBox")] = ArrayObject([FloatObject(v)
                                          for v in (0, 0, 297, 420)])
        form[_NO("/Resources")] = _DO(
            {_NO("/Font"): _DO({_NO("/F1"): helv})})
        fref = w._add_object(form)
        res = _DO(pg.get("/Resources") or {})
        res[_NO("/XObject")] = _DO({_NO("/Fm0"): fref})
        pg[_NO("/Resources")] = res
        cs = ContentStream(pg.get_contents(), w)
        cs.operations.append(([_NO("/Fm0")], b"Do"))
        pg.replace_contents(cs)
        _save(w, p("formcover.pdf"))
        st_f = tools.call("board_pdf_translate_start",
                          {"path": p("formcover.pdf"), "restart": True})
        show("start formcover", st_f, "modes")
        fid = st_f.get("job")
        jf = job_of(fid)
        fb = next((b for b in jf["blocks"].get("1", [])
                   if "COVER" in b["text"]), None)
        expect("form text found", fb is not None)
        expect("form white colour", fb is not None
               and fb.get("color") == "#ffffff")
        if fb:
            tools.call("board_pdf_translate_fill",
                       {"job": fid, "page": 1,
                        "blocks": {fb["id"]: "ОБЛОЖКА"}})
            fin_f = tools.call("board_pdf_translate_finish",
                               {"job": fid, "out": p("formcover_ru.pdf"),
                                "overwrite": True, "place": False})
            show("finish formcover", fin_f, "pages", "mirror")
            expect("formcover one page", fin_f.get("pages") == 1)
            mf = fin_f.get("mirror") or {}
            expect("formcover mirrored", 1 in (mf.get("mirrored") or []))
            expect("formcover no extra", not mf.get("fallback"))
            if fin_f.get("ok"):
                fp = _reader(p("formcover_ru.pdf")).pages[0]
                ft = fp.extract_text() or ""
                expect("formcover russian", "ОБЛОЖКА" in ft)
                expect("formcover english lifted", "COVER TITLE" not in ft)
                fcols = {c for _, _, c in _text_colors(fp)}
                expect("formcover white kept", "#ffffff" in fcols)
        # ---- narrow column: horizontal squeeze, never extra pages ------
        buf = _io.BytesIO()
        cv = _cv.Canvas(buf, pagesize=(297, 420))
        cv.setFont(_font_name(), 12)
        cv.drawString(72, 380, "OK")
        cv.drawString(72, 360, "Next line.")
        cv.showPage()
        cv.save()
        with open(p("narrow.pdf"), "wb") as stream:
            stream.write(buf.getvalue())
        st_n = tools.call("board_pdf_translate_start",
                          {"path": p("narrow.pdf"), "restart": True})
        nid = st_n.get("job")
        jn = job_of(nid)
        nb = next((b for b in jn["blocks"].get("1", [])
                   if b["text"].strip() == "OK"), None)
        ob = next((b for b in jn["blocks"].get("1", [])
                   if b["text"].strip() == "Next line."), None)
        if nb and ob:
            tools.call("board_pdf_translate_fill",
                       {"job": nid, "page": 1,
                        "blocks": {nb["id"]: long_ru,
                                   ob["id"]: "Next line."},
                        "on_overflow": "facing"})
            fin_n = tools.call("board_pdf_translate_finish",
                               {"job": nid, "out": p("narrow_ru.pdf"),
                                "overwrite": True, "place": False})
            show("finish narrow", fin_n, "pages", "mirror")
            expect("narrow one page", fin_n.get("pages") == 1)
            mn = fin_n.get("mirror") or {}
            expect("narrow mirrored", 1 in (mn.get("mirrored") or []))
            expect("narrow squeezed fits", not mn.get("unplaced"))
            if fin_n.get("ok"):
                nt = _reader(p("narrow_ru.pdf")).pages[0].extract_text() or ""
                expect("narrow russian present", "Очень" in nt)
                expect("narrow neighbour kept", "Next line." in nt)
        # ---- v8 guard: vertical clearance to the neighbour below -------
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
                if top < b["first_y"] and bottom < top + 0.9 \
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
        # ---- v8c guard: hard breaks reflow, no clash, file audited -----
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
        # ---- v9a guard: text never grows into a photo --------------------
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
        img.set_data(b"\xff\x00\x00")
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
                               (100, 0, 0, 90, 190, 250)], b"cm"))
        cs.operations.append(([_NO("/Im0")], b"Do"))
        pg.replace_contents(cs)
        _save(w, p("photo.pdf"))
        st_ph = tools.call("board_pdf_translate_start",
                           {"path": p("photo.pdf"), "restart": True})
        phid = st_ph.get("job")
        jph = job_of(phid)
        expect("photo obstacle stored",
               any(b.get("avoid") for b in jph["blocks"].get("1", [])))
        send_ph = {b["id"]: ("\u041e\u0447\u0435\u043d\u044c \u0434\u043b\u0438\u043d\u043d\u0430\u044f "
                            "\u0440\u0443\u0441\u0441\u043a\u0430\u044f \u0441\u0442\u0440\u043e\u043a\u0430, "
                            "\u043a\u043e\u0442\u043e\u0440\u0430\u044f \u043d\u0435 \u0434\u043e\u043b\u0436\u043d\u0430 "
                            "\u043b\u0435\u0437\u0442\u044c \u043d\u0430 \u0444\u043e\u0442\u043e\u0433\u0440\u0430\u0444\u0438\u044e.")
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
                     if 249 < s["y"] < 341 and s["x"] + s["w"] > 189]
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
        send_l = {b["id"]: ("\u041f\u043e\u0441\u0435\u0442\u0438\u0442\u0435 "
                           "http://example.com \u0441\u0435\u0433\u043e\u0434\u043d\u044f.")
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
        # ---- v9b guard: inline colour accents survive translation ------
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
                send_ac[b["id"]] = ("\u0427\u0438\u0442\u0430\u0439\u0442\u0435 "
                                    "\u0440\u0443\u043a\u043e\u0432\u043e\u0434\u0441\u0442\u0432\u043e "
                                    "\u043d\u0430 http://example.com "
                                    "\u0440\u0430\u0434\u0438 \u0434\u0435\u0442\u0430\u043b\u0435\u0439.")
            elif b["text"].startswith("See"):
                send_ac[b["id"]] = ("\u0421\u043c\u043e\u0442\u0440\u0438\u0442\u0435 "
                                    "HowStuffWorks "
                                    "\u043f\u043e\u0434\u0440\u043e\u0431\u043d\u0435\u0435.")
            else:
                send_ac[b["id"]] = ("\u041f\u043e\u0441\u0435\u0442\u0438\u0442\u0435 "
                                    "[[#6b9f25]]\u0410\u043d\u0430\u043d\u0434\u0422\u0435\u0445[[/]] "
                                    "\u0441\u0435\u0439\u0447\u0430\u0441.")
        tools.call("board_pdf_translate_fill",
                   {"job": acid, "page": 1, "blocks": send_ac,
                    "on_overflow": "facing"})
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
        print("GUARD FAILS:", len(fails), fails)
        print("files are in ->", folder)
        print("DONE - close the window to exit")

    QTimer.singleShot(800, run)
    sys.exit(app.exec())


if __name__ == "__main__":
    _selftest()