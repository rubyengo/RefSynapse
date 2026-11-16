"""PDF content editing for the RefSynapse agent - batch 2.

board_pdf_extract_text   - read the text of chosen pages (plain or layout)
board_pdf_extract_images - save the pictures inside a PDF to image files
board_pdf_add_text       - draw text on top of pages (stamp, header, number)
board_pdf_add_image      - draw a picture on top of pages (logo, watermark)
board_pdf_replace_text   - cover old wording and draw new wording over it
board_pdf_replace_image  - swap a picture inside a page for another file
board_pdf_metadata       - read or set title / author / subject / keywords

WHAT PDF EDITING CAN AND CANNOT DO. A PDF stores drawn glyphs at fixed
coordinates, not paragraphs. Adding, covering and swapping content is exact;
reflowing a paragraph like a word processor is not possible. replace_text
therefore PAINTS OVER the old wording and draws the new one: the old glyphs
stay inside the file and remain extractable. Every such call says so in its
result. Never use it to hide confidential data.

COORDINATES. One rule for x, y, width, height and box: a value <= 1 is a
fraction of the page, a value > 1 is a length in points (72 pt = 1 inch).
x grows to the right, y grows DOWN from the TOP edge - like on screen, not
like the PDF internals. 'anchor' says which point of the drawn block sits at
(x, y); default 'center'.

SANDBOX. Reads and writes stay inside the agent folder; 'uid' may point at a
PDF the user placed on the board (board content is user data). Results are
always written into the sandbox.

Registered from board_pdf_tools.register_pdf_tools() - no change to
board_tools.py is needed.
"""

import io
import os
import re

from .board_tools import ToolError
from .sandbox import sandbox_root, is_inside
from .board_pdf_tools import (INSTALL_HINT, _need_pdf, _font_name, _source,
                              _reader, _pdf_target, _page_numbers, _save,
                              _unique, _read_path, _prepare_image, _cleanup,
                              _color_hex, _wrap)

try:
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.colors import HexColor
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen import canvas
except Exception:                      # reported by _need_pdf() at call time
    pass

try:
    from PIL import Image
    _PIL = True
except Exception:
    _PIL = False

PDF_EDIT_TOOL_NAMES = ("board_pdf_extract_text", "board_pdf_extract_images",
                       "board_pdf_add_text", "board_pdf_add_image",
                       "board_pdf_replace_text", "board_pdf_replace_image",
                       "board_pdf_metadata")

ANCHORS = ("top-left", "top-center", "top-right",
           "center-left", "center", "center-right",
           "bottom-left", "bottom-center", "bottom-right")
MAX_EDIT_PAGES = 500
MAX_DRAW_CHARS = 20_000
MAX_RETURN_CHARS = 20_000
MAX_EXTRACT_IMAGES = 200
COVER_WARNING = ("The old wording is painted over, not erased: it stays inside "
                 "the file and can still be extracted. Do not use this to hide "
                 "confidential data.")


# ------------------------------------------------------------------ geometry
def _length(value, span, key, default=None):
    """<=1 -> fraction of the page, >1 -> points. Never silently clamped."""
    if value is None:
        if default is None:
            raise ToolError("'%s' is required" % key)
        value = default
    if isinstance(value, bool):
        raise ToolError("'%s' must be a number" % key)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ToolError("'%s' must be a number (<=1 fraction, >1 points)" % key)
    if number < 0:
        raise ToolError("'%s' must not be negative" % key)
    result = number * span if number <= 1 else number
    if result > span * 4:
        raise ToolError("'%s' = %g is far outside the page (%g pt). Values <=1 "
                        "are fractions, >1 are points." % (key, number, span))
    return float(result)


def _anchor_box(anchor, px, py, width, height):
    """Bottom-left corner of a block whose 'anchor' point sits at (px, py)."""
    if anchor not in ANCHORS:
        raise ToolError("Unknown anchor '%s' (use one of: %s)"
                        % (anchor, ", ".join(ANCHORS)))
    vertical, _, horizontal = anchor.partition("-")
    if anchor == "center":
        vertical, horizontal = "center", "center"
    x = {"left": px, "center": px - width / 2.0, "right": px - width}[horizontal]
    y = {"top": py - height, "center": py - height / 2.0, "bottom": py}[vertical]
    return float(x), float(y)


def _page_frame(page):
    box = page.mediabox
    return float(box.width), float(box.height), float(box.left), float(box.bottom)


def _rotation_note(page):
    turn = int(page.rotation or 0) % 360
    if turn:
        return ("This page is rotated by %d degrees: drawings use the unrotated "
                "page frame, so they will appear turned on screen." % turn)
    return ""


# ---------------------------------------------------------------- text search
def _char_width(font_dict, size):
    """Average glyph width from the font, or a safe estimate."""
    try:
        widths = font_dict.get("/Widths") if font_dict else None
        if widths:
            values = [float(v) for v in widths]
            if values:
                return sum(values) / len(values) / 1000.0 * size
    except Exception:
        pass
    return 0.5 * size


def _spans(page):
    """Every drawn piece of text with its position and size (PDF coordinates)."""
    spans = []

    def visitor(text, cm, tm, font_dict, font_size):
        if not text or not text.strip():
            return
        try:
            a, b, c, d, e, f = list(cm)
            g, h, i, j, k, l = list(tm)
            scale_y = (g * b + h * d) or (i * b + j * d) or 1.0
            x = k * a + l * c + e
            y = k * b + l * d + f
            size = abs(float(font_size) * scale_y) or float(font_size)
            clean = text.replace("\n", " ").replace("\r", " ")
            spans.append({"text": clean, "x": float(x), "y": float(y),
                          "size": float(size),
                          "width": _char_width(font_dict, float(size)) * len(clean)})
        except Exception:
            return

    try:
        page.extract_text(visitor_text=visitor)
    except Exception:
        return []
    return spans


def _find_boxes(spans, needle, ignore_case=True):
    """Rectangles [x0, y0, x1, y1] covering every occurrence of 'needle'."""
    haystack = "".join(s["text"] for s in spans)
    pattern = needle
    if ignore_case:
        haystack, pattern = haystack.lower(), needle.lower()
    boxes, start = [], 0
    while pattern:
        index = haystack.find(pattern, start)
        if index < 0:
            break
        end = index + len(pattern)
        position, chosen = 0, []
        for span in spans:
            following = position + len(span["text"])
            if following > index and position < end:
                chosen.append((span, max(0, index - position),
                               min(len(span["text"]), end - position)))
            position = following
        if chosen:
            x0 = min(s["x"] + s["width"] * (a / max(1, len(s["text"])))
                     for s, a, _ in chosen)
            x1 = max(s["x"] + s["width"] * (b / max(1, len(s["text"])))
                     for s, _, b in chosen)
            y0 = min(s["y"] - 0.25 * s["size"] for s, _, _ in chosen)
            y1 = max(s["y"] + 0.95 * s["size"] for s, _, _ in chosen)
            size = max(s["size"] for s, _, _ in chosen)
            boxes.append({"box": [x0, y0, x1, y1], "font_size": size})
        start = index + max(1, len(pattern))
    return boxes


def _box_arg(raw, page_w, page_h, origin_x, origin_y):
    """[x0, y0, x1, y1] measured from the TOP-left -> PDF rectangle."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise ToolError("'box' must be four numbers [x0, y0, x1, y1] measured "
                        "from the top-left corner")
    x0 = _length(raw[0], page_w, "box[0]")
    y0 = _length(raw[1], page_h, "box[1]")
    x1 = _length(raw[2], page_w, "box[2]")
    y1 = _length(raw[3], page_h, "box[3]")
    if x1 <= x0 or y1 <= y0:
        raise ToolError("'box' must go left->right and top->bottom "
                        "(x1 > x0 and y1 > y0)")
    return [origin_x + x0, origin_y + page_h - y1,
            origin_x + x1, origin_y + page_h - y0]


def _safe_name(raw, fallback):
    """PDF image names may contain anything - never trust them as file names."""
    name = os.path.basename(str(raw or "")).strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name[:60] or fallback


# ------------------------------------------------------------------- drawing
def _overlay_page(page_w, page_h, drawer):
    """A one-page PDF drawn by reportlab, ready to be merged onto a page."""
    buffer = io.BytesIO()
    doc = canvas.Canvas(buffer, pagesize=(page_w, page_h))
    drawer(doc)
    doc.showPage()
    doc.save()
    buffer.seek(0)
    return PdfReader(buffer).pages[0]


def _draw_lines(doc, lines, x, y_top, size, leading, font, color, opacity):
    doc.saveState()
    try:
        doc.setFillAlpha(opacity)
    except Exception:
        pass
    doc.setFillColor(HexColor(color))
    doc.setFont(font, size)
    baseline = y_top - size
    for line in lines:
        if line:
            doc.drawString(x, baseline, line)
        baseline -= size * leading
    doc.restoreState()


def _rotated(doc, degrees, cx, cy, body):
    if not degrees:
        body()
        return
    doc.saveState()
    doc.translate(cx, cy)
    doc.rotate(degrees)
    doc.translate(-cx, -cy)
    body()
    doc.restoreState()


def _opacity_image(path, opacity, tmp_dir, tmp_files):
    """Bake transparency into the picture itself - works in every viewer."""
    if opacity >= 0.999 or not _PIL:
        return path
    try:
        image = Image.open(path).convert("RGBA")
        alpha = image.getchannel("A").point(lambda v: int(v * opacity))
        image.putalpha(alpha)
        os.makedirs(tmp_dir, exist_ok=True)
        target = os.path.join(tmp_dir, "fade_%d.png" % len(tmp_files))
        image.save(target, "PNG")
    except Exception as ex:
        raise ToolError("Could not apply opacity to '%s': %s"
                        % (os.path.basename(path), ex))
    tmp_files.append(target)
    return target


def _alpha_arg(a, default=1.0):
    try:
        opacity = float(a.get("opacity", default))
    except (TypeError, ValueError):
        raise ToolError("'opacity' must be a number between 0 and 1")
    if not 0.05 <= opacity <= 1.0:
        raise ToolError("'opacity' must be between 0.05 and 1.0")
    return opacity


def _degrees(a):
    try:
        degrees = float(a.get("rotate", 0))
    except (TypeError, ValueError):
        raise ToolError("'rotate' must be a number of degrees")
    if not -360 <= degrees <= 360:
        raise ToolError("'rotate' must be between -360 and 360 degrees")
    return degrees


# -------------------------------------------------------------------- output
def _edit_target(tools, a, src):
    """Where the edited copy goes: 'out', or the source itself with overwrite."""
    if a.get("out"):
        return _pdf_target(tools.win, a, key="out"), False
    if not is_inside(tools.win, src):
        raise ToolError("'%s' is a board file outside the agent folder - give "
                        "'out' inside '%s'"
                        % (os.path.basename(src), sandbox_root(tools.win)))
    if not a.get("overwrite"):
        raise ToolError("This would rewrite '%s' in place - pass overwrite:true, "
                        "or give 'out' for a new file" % os.path.basename(src))
    return src, True


def _writer_of(src, password=None):
    reader = _reader(src, password)
    try:
        return PdfWriter(clone_from=reader), reader
    except Exception as ex:
        raise ToolError("Could not open '%s' for editing: %s"
                        % (os.path.basename(src), ex))


def _select_pages(a, total):
    indices = _page_numbers(a.get("pages"), total)
    if len(indices) > MAX_EDIT_PAGES:
        raise ToolError("This call would touch %d pages (max %d) - split it into "
                        "smaller page ranges" % (len(indices), MAX_EDIT_PAGES))
    return indices


# --------------------------------------------------------------- registration
def register_pdf_edit_tools(tools):
    """Batch 2: change what is drawn INSIDE the pages."""
    win = tools.win
    S = tools._schema
    sb = sandbox_root(win)
    tmp_dir = os.path.join(sb, "_pdf_tmp")

    path_arg = {"type": "string",
                "description": "PDF inside the agent folder '%s'" % sb}
    uid_arg = {"type": "string",
               "description": "uid of a PDF document already on the board"}
    out_arg = {"type": "string",
               "description": "Result file inside the agent folder; omit to "
                              "rewrite the source (needs overwrite:true)"}
    pages_arg = {"type": "string",
                 "description": "Pages to touch, 1-based: '1-3,5' or 'all' "
                                "(default all)"}
    over_arg = {"type": "boolean",
                "description": "Allow replacing an existing file / the source"}
    pwd_arg = {"type": "string"}
    anchor_arg = {"type": "string", "enum": list(ANCHORS),
                  "description": "Which point of the block sits at x/y "
                                 "(default center)"}
    xy_desc = ("Position from the TOP-left corner: <=1 is a fraction of the "
               "page, >1 is points (72 pt = 1 inch)")

    # ---- shared targets --------------------------------------------------
    def _txt_target(window, raw, allow_overwrite):
        from .sandbox import sandbox_path
        target = sandbox_path(window, raw, key="save_to")
        if os.path.isdir(target):
            raise ToolError("'%s' is a folder - give a full .txt file name" % target)
        if not target.lower().endswith(".txt"):
            target += ".txt"
        if os.path.exists(target) and not allow_overwrite:
            raise ToolError("File already exists - pass overwrite:true")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        return target

    def _folder(window, raw):
        from .sandbox import sandbox_path
        target = sandbox_path(window, raw, key="folder", folder=True)
        if os.path.isfile(target):
            raise ToolError("'%s' is a file - give a folder" % target)
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as ex:
            raise ToolError("Cannot create the folder: %s" % ex)
        return target

    # ---- read the text ---------------------------------------------------
    def t_extract_text(a):
        src = _source(tools, a)
        reader = _reader(src, a.get("password"))
        indices = _select_pages(a, len(reader.pages))
        mode = a.get("mode", "plain")
        if mode not in ("plain", "layout"):
            raise ToolError("Unknown mode '%s' (plain or layout)" % mode)
        limit = max(200, min(MAX_RETURN_CHARS, int(a.get("max_chars", 4000))))
        per_page, chunks = [], []
        for index in indices:
            try:
                text = reader.pages[index].extract_text(extraction_mode=mode) or ""
            except Exception:
                text = ""
            per_page.append({"page": index + 1, "chars": len(text)})
            chunks.append("--- page %d ---\n%s" % (index + 1, text))
        whole = "\n\n".join(chunks)
        saved = None
        if a.get("save_to"):
            target = _txt_target(win, a.get("save_to"), bool(a.get("overwrite")))
            try:
                with open(target, "w", encoding="utf-8") as stream:
                    stream.write(whole)
            except OSError as ex:
                raise ToolError("Cannot write '%s': %s" % (target, ex))
            saved = target
        return {"path": src, "mode": mode, "pages": per_page,
                "total_chars": len(whole), "text": whole[:limit],
                "truncated": len(whole) > limit, "saved_to": saved,
                "hint": "Empty text means the page is a scan or pure artwork - "
                        "look at it with board_export_pages instead."}

    # ---- pull the pictures out ------------------------------------------
    def t_extract_images(a):
        src = _source(tools, a)
        reader = _reader(src, a.get("password"))
        indices = _select_pages(a, len(reader.pages))
        folder = _folder(win, a.get("folder"))
        limit = max(1, min(MAX_EXTRACT_IMAGES, int(a.get("max_images", 50))))
        allow = bool(a.get("overwrite"))
        files, skipped, seen = [], 0, 0
        for index in indices:
            try:
                images = list(reader.pages[index].images)
            except Exception:
                continue
            for image in images:
                if seen >= limit:
                    break
                seen += 1
                stem = "p%03d_%s" % (index + 1,
                                     _safe_name(getattr(image, "name", ""), "img"))
                stem = os.path.splitext(stem)[0]
                picture = getattr(image, "image", None) if _PIL else None
                width = height = 0
                try:
                    if picture is not None:
                        if picture.mode not in ("RGB", "RGBA", "L"):
                            picture = picture.convert("RGBA" if "A" in picture.mode
                                                      else "RGB")
                        target = os.path.join(folder, stem + ".png")
                        if os.path.exists(target) and not allow:
                            target = _unique(target)
                        picture.save(target, "PNG")
                        width, height = picture.size
                    else:
                        target = os.path.join(folder, stem + ".bin")
                        if os.path.exists(target) and not allow:
                            target = _unique(target)
                        with open(target, "wb") as stream:
                            stream.write(image.data)
                except Exception:
                    skipped += 1
                    continue
                files.append({"file": os.path.basename(target), "page": index + 1,
                              "width": width, "height": height})
            if seen >= limit:
                break
        return {"folder": folder, "extracted": len(files), "skipped": skipped,
                "files": files[:40], "truncated": seen >= limit,
                "hint": "Use these files with board_pdf_replace_image or "
                        "board_add_image."}

    # ---- draw text on top ------------------------------------------------
    def t_add_text(a):
        _need_pdf()
        text = a.get("text")
        if text is None or not str(text).strip():
            raise ToolError("'text' must not be empty")
        if len(str(text)) > MAX_DRAW_CHARS:
            raise ToolError("'text' is too long (%d chars, max %d)"
                            % (len(str(text)), MAX_DRAW_CHARS))
        size = float(a.get("font_size", 18))
        if not 2 <= size <= 400:
            raise ToolError("'font_size' must be between 2 and 400 points")
        leading = float(a.get("leading", 1.3))
        if not 0.8 <= leading <= 4:
            raise ToolError("'leading' must be between 0.8 and 4")
        color = _color_hex(a.get("color"))
        opacity = _alpha_arg(a)
        degrees = _degrees(a)
        anchor = a.get("anchor", "center")
        font = _font_name()
        src = _source(tools, a)
        writer, reader = _writer_of(src, a.get("password"))
        indices = _select_pages(a, len(reader.pages))
        target, in_place = _edit_target(tools, a, src)
        placed, notes = [], []
        for index in indices:
            page = writer.pages[index]
            page_w, page_h, ox, oy = _page_frame(page)
            wrap = (_length(a["width"], page_w, "width")
                    if a.get("width") is not None else None)
            lines = (_wrap(str(text), font, size, wrap) if wrap
                     else str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"))
            block_w = max([pdfmetrics.stringWidth(line, font, size)
                           for line in lines] or [0])
            block_h = len(lines) * size * leading
            px = ox + _length(a.get("x"), page_w, "x", 0.5)
            py = oy + page_h - _length(a.get("y"), page_h, "y", 0.5)
            bx, by = _anchor_box(anchor, px, py, block_w, block_h)
            if bx < ox - 1 or by < oy - 1 or bx + block_w > ox + page_w + 1 \
                    or by + block_h > oy + page_h + 1:
                raise ToolError("The text block (%.0fx%.0f pt at %.0f,%.0f) does "
                                "not fit page %d (%.0fx%.0f pt). Move x/y toward "
                                "0.5 or lower font_size."
                                % (block_w, block_h, bx, by, index + 1, page_w, page_h))
            note = _rotation_note(page)
            if note and note not in notes:
                notes.append(note)

            def drawer(doc, lines=lines, bx=bx, by=by, block_h=block_h,
                       block_w=block_w, size=size, leading=leading, font=font,
                       color=color, opacity=opacity, degrees=degrees):
                _rotated(doc, degrees, bx + block_w / 2.0, by + block_h / 2.0,
                         lambda: _draw_lines(doc, lines, bx, by + block_h,
                                             size, leading, font, color, opacity))

            page.merge_page(_overlay_page(page_w, page_h, drawer))
            placed.append({"page": index + 1,
                           "box_pt": [round(bx, 1), round(by, 1),
                                      round(bx + block_w, 1), round(by + block_h, 1)],
                           "lines": len(lines)})
        bytes_written = _save(writer, target)
        return {"path": target, "in_place": in_place, "pages_touched": len(placed),
                "placed": placed[:20], "font_size": size, "opacity": opacity,
                "rotate": degrees, "warnings": notes, "bytes": bytes_written,
                "hint": "Check the result: board_add_image with this path, then "
                        "board_export_pages."}

    # ---- draw a picture on top ------------------------------------------
    def t_add_image(a):
        _need_pdf()
        raw_image = a.get("image")
        if not raw_image:
            raise ToolError("'image' is required - a picture inside the agent "
                            "folder '%s'" % sb)
        picture = _read_path(win, raw_image, key="image")
        opacity = _alpha_arg(a)
        degrees = _degrees(a)
        anchor = a.get("anchor", "center")
        src = _source(tools, a)
        writer, reader = _writer_of(src, a.get("password"))
        indices = _select_pages(a, len(reader.pages))
        target, in_place = _edit_target(tools, a, src)
        tmp_files, placed, notes = [], [], []
        try:
            base, img_w, img_h = _prepare_image(picture, tmp_dir, tmp_files)
            faded = _opacity_image(base, opacity, tmp_dir, tmp_files)
            for index in indices:
                page = writer.pages[index]
                page_w, page_h, ox, oy = _page_frame(page)
                draw_w = _length(a.get("width"), page_w, "width", 0.4)
                if a.get("height") is not None:
                    draw_h = _length(a["height"], page_h, "height")
                else:
                    draw_h = draw_w * img_h / float(img_w)
                if draw_w <= 0 or draw_h <= 0:
                    raise ToolError("The drawn size must be positive")
                px = ox + _length(a.get("x"), page_w, "x", 0.5)
                py = oy + page_h - _length(a.get("y"), page_h, "y", 0.5)
                bx, by = _anchor_box(anchor, px, py, draw_w, draw_h)
                if bx < ox - 1 or by < oy - 1 or bx + draw_w > ox + page_w + 1 \
                        or by + draw_h > oy + page_h + 1:
                    raise ToolError("The picture (%.0fx%.0f pt at %.0f,%.0f) does "
                                    "not fit page %d (%.0fx%.0f pt). Lower "
                                    "'width' or move x/y toward 0.5."
                                    % (draw_w, draw_h, bx, by, index + 1,
                                       page_w, page_h))
                note = _rotation_note(page)
                if note and note not in notes:
                    notes.append(note)

                def drawer(doc, bx=bx, by=by, draw_w=draw_w, draw_h=draw_h,
                           faded=faded, degrees=degrees, opacity=opacity):
                    def body():
                        try:
                            doc.setFillAlpha(opacity)
                        except Exception:
                            pass
                        doc.drawImage(ImageReader(faded), bx, by,
                                      width=draw_w, height=draw_h,
                                      preserveAspectRatio=False, mask="auto")
                    _rotated(doc, degrees, bx + draw_w / 2.0, by + draw_h / 2.0, body)

                page.merge_page(_overlay_page(page_w, page_h, drawer))
                placed.append({"page": index + 1,
                               "box_pt": [round(bx, 1), round(by, 1),
                                          round(bx + draw_w, 1), round(by + draw_h, 1)]})
            bytes_written = _save(writer, target)
        finally:
            _cleanup(tmp_files, tmp_dir)
        return {"path": target, "in_place": in_place, "pages_touched": len(placed),
                "placed": placed[:20], "image_pixels": [img_w, img_h],
                "opacity": opacity, "rotate": degrees, "warnings": notes,
                "bytes": bytes_written}

    # ---- cover old wording, draw new ------------------------------------
    def t_replace_text(a):
        _need_pdf()
        new_text = a.get("text")
        if new_text is None or not str(new_text).strip():
            raise ToolError("'text' (the new wording) must not be empty")
        needle = a.get("find")
        box_arg = a.get("box")
        if not needle and box_arg is None:
            raise ToolError("Provide 'find' (the wording to look for) or 'box' "
                            "([x0,y0,x1,y1] from the top-left corner)")
        if needle and box_arg is not None:
            raise ToolError("Use either 'find' or 'box', not both")
        cover = _color_hex(a.get("cover"), "#ffffff")
        color = _color_hex(a.get("color"))
        pad = float(a.get("pad", 2))
        if not 0 <= pad <= 40:
            raise ToolError("'pad' must be between 0 and 40 points")
        ignore_case = bool(a.get("ignore_case", True))
        font = _font_name()
        src = _source(tools, a)
        writer, reader = _writer_of(src, a.get("password"))
        indices = _select_pages(a, len(reader.pages))
        target, in_place = _edit_target(tools, a, src)
        done, notes, overflow = [], [], []
        for index in indices:
            page = writer.pages[index]
            page_w, page_h, ox, oy = _page_frame(page)
            if box_arg is not None:
                rect = _box_arg(box_arg, page_w, page_h, ox, oy)
                size = float(a.get("font_size", 12))
                found = [{"box": rect, "font_size": size}]
            else:
                found = _find_boxes(_spans(reader.pages[index]), str(needle),
                                    ignore_case)
                if not found:
                    continue
                if a.get("font_size") is not None:
                    for entry in found:
                        entry["font_size"] = float(a["font_size"])
            for entry in found:
                if not 2 <= entry["font_size"] <= 400:
                    raise ToolError("'font_size' must be between 2 and 400 points")
            note = _rotation_note(page)
            if note and note not in notes:
                notes.append(note)
            rects = []
            for entry in found:
                x0, y0, x1, y1 = entry["box"]
                rects.append([x0 - pad, y0 - pad, x1 + pad, y1 + pad])
            lines = str(new_text).replace("\r\n", "\n").replace("\r", "\n").split("\n")

            def drawer(doc, rects=rects, found=found, lines=lines, font=font,
                       cover=cover, color=color):
                doc.saveState()
                doc.setFillColor(HexColor(cover))
                for x0, y0, x1, y1 in rects:
                    doc.rect(x0, y0, x1 - x0, y1 - y0, stroke=0, fill=1)
                doc.restoreState()
                first = found[0]
                size = first["font_size"]
                x0, y0, x1, y1 = first["box"]
                _draw_lines(doc, lines, x0, y0 + size * 1.25,
                            size, 1.25, font, color, 1.0)

            page.merge_page(_overlay_page(page_w, page_h, drawer))
            width_needed = max([pdfmetrics.stringWidth(line, font,
                                                       found[0]["font_size"])
                                for line in lines] or [0])
            room = found[0]["box"][2] - found[0]["box"][0]
            if width_needed > room + 2 * pad:
                overflow.append({"page": index + 1,
                                 "needed_pt": round(width_needed, 1),
                                 "available_pt": round(room + 2 * pad, 1)})
            if len(lines) > 1:
                extra = ("The new wording has %d lines: the first line sits where "
                         "the old one was, the rest are drawn below it and may "
                         "overlap other content." % len(lines))
                if extra not in notes:
                    notes.append(extra)
            done.append({"page": index + 1, "matches": len(found),
                         "boxes_pt": [[round(v, 1) for v in e["box"]] for e in found],
                         "font_size": round(found[0]["font_size"], 1)})
        if not done:
            sample = []
            for index in indices[:3]:
                try:
                    raw = reader.pages[index].extract_text() or ""
                except Exception:
                    raw = ""
                for line in raw.splitlines():
                    if line.strip():
                        sample.append(line.strip()[:80])
                    if len(sample) >= 8:
                        break
            raise ToolError("'%s' was not found on the selected pages. Text really "
                            "present: %s. Use board_pdf_extract_text for the full "
                            "text, or pass 'box' to cover an exact rectangle."
                            % (needle, " | ".join(sample) or "(none - scanned page?)"))
        bytes_written = _save(writer, target)
        return {"path": target, "in_place": in_place, "replaced": done,
                "pages_touched": len(done), "cover_color": cover,
                "old_text_still_extractable": True, "warnings": notes,
                "overflow": overflow, "notice": COVER_WARNING,
                "bytes": bytes_written,
                "hint": "If 'overflow' is not empty the new wording is wider than "
                        "the old one: lower font_size or widen 'box'."}

    # ---- swap a picture inside a page -----------------------------------
    def t_replace_image(a):
        _need_pdf()
        if not _PIL:
            raise ToolError("Replacing pictures needs Pillow in the portable "
                            "python: %s" % INSTALL_HINT)
        if a.get("page") is None:
            raise ToolError("'page' is required (1-based, one page per call)")
        raw_image = a.get("image")
        if not raw_image:
            raise ToolError("'image' is required - the new picture inside '%s'" % sb)
        picture_path = _read_path(win, raw_image, key="image")
        src = _source(tools, a)
        writer, reader = _writer_of(src, a.get("password"))
        index = _page_numbers(a["page"], len(reader.pages), what="page")[0]
        target, in_place = _edit_target(tools, a, src)
        page = writer.pages[index]
        try:
            images = list(page.images)
        except Exception as ex:
            raise ToolError("Could not list the pictures on page %d: %s"
                            % (index + 1, ex))
        if not images:
            raise ToolError("Page %d contains no pictures. To put one ON TOP use "
                            "board_pdf_add_image instead." % (index + 1))
        chosen = None
        if a.get("name"):
            wanted = str(a["name"])
            for image in images:
                if getattr(image, "name", "") == wanted:
                    chosen = image
                    break
            if chosen is None:
                raise ToolError("No picture named '%s' on page %d. Available: %s"
                                % (wanted, index + 1,
                                   ", ".join(getattr(i, "name", "?") for i in images)))
        else:
            position = int(a.get("index", 0))
            if not 0 <= position < len(images):
                raise ToolError("'index' %d is out of range - page %d has %d "
                                "pictures (0-%d)"
                                % (position, index + 1, len(images), len(images) - 1))
            chosen = images[position]
        if getattr(chosen, "is_inline", False):
            raise ToolError("That picture is drawn inline and cannot be swapped. "
                            "Cover it with board_pdf_add_image instead.")
        try:
            new_image = Image.open(picture_path)
            new_image.load()
        except Exception as ex:
            raise ToolError("Cannot read '%s': %s" % (os.path.basename(picture_path), ex))
        old_picture = getattr(chosen, "image", None)
        old_size = list(old_picture.size) if old_picture is not None else [0, 0]
        old_alpha = old_picture is not None and old_picture.mode in ("RGBA", "LA", "PA")
        jpeg_slot = False
        try:
            reference = getattr(chosen, "indirect_reference", None)
            stream = reference.get_object() if reference is not None else {}
            jpeg_slot = "DCTDecode" in str(stream.get("/Filter", ""))
        except Exception:
            jpeg_slot = False
        if new_image.mode not in ("RGB", "RGBA", "L", "LA"):
            new_image = new_image.convert("RGBA" if "A" in new_image.mode else "RGB")
        # A JPEG slot cannot store transparency: flatten onto white instead of
        # letting the save fail halfway through.
        if new_image.mode in ("RGBA", "LA") and (jpeg_slot or not old_alpha):
            flat = Image.new("RGB", new_image.size, (255, 255, 255))
            flat.paste(new_image, mask=new_image.getchannel("A"))
            new_image = flat
        try:
            chosen.replace(new_image)
        except Exception as ex:
            raise ToolError("Could not replace the picture: %s" % ex)
        bytes_written = _save(writer, target)
        notes = []
        if old_size[0] and (abs(old_size[0] / float(old_size[1] or 1)
                                - new_image.width / float(new_image.height or 1)) > 0.02):
            notes.append("The new picture has a different shape (%dx%d vs %dx%d): "
                         "it is stretched into the old rectangle. Match the shape "
                         "or use board_pdf_add_image for exact placement."
                         % (new_image.width, new_image.height, old_size[0], old_size[1]))
        return {"path": target, "in_place": in_place, "page": index + 1,
                "image_name": getattr(chosen, "name", ""),
                "old_pixels": old_size,
                "new_pixels": [new_image.width, new_image.height],
                "pictures_on_page": len(images), "warnings": notes,
                "bytes": bytes_written}

    # ---- title / author --------------------------------------------------
    def t_metadata(a):
        _need_pdf()
        keys = {"title": "/Title", "author": "/Author", "subject": "/Subject",
                "keywords": "/Keywords", "creator": "/Creator"}
        changes = {}
        for key, tag in keys.items():
            if key in a:
                value = a[key]
                if value is None:
                    value = ""
                changes[tag] = str(value)[:500]
        src = _source(tools, a)
        reader = _reader(src, a.get("password"))
        current = {}
        try:
            for key, value in (reader.metadata or {}).items():
                if value:
                    current[str(key).lstrip("/")] = str(value)[:200]
        except Exception:
            pass
        if not changes:
            return {"path": src, "metadata": current, "changed": {},
                    "hint": "Pass title / author / subject / keywords / creator "
                            "to change them."}
        writer, _ = _writer_of(src, a.get("password"))
        target, in_place = _edit_target(tools, a, src)
        writer.add_metadata(changes)
        bytes_written = _save(writer, target)
        after = {}
        try:
            for key, value in (PdfReader(target).metadata or {}).items():
                if value:
                    after[str(key).lstrip("/")] = str(value)[:200]
        except Exception:
            pass
        return {"path": target, "in_place": in_place, "metadata": after,
                "changed": {k.lstrip("/"): v for k, v in changes.items()},
                "bytes": bytes_written}

    # ---- registry --------------------------------------------------------
    tools._add("board_pdf_extract_text",
               "Read the text of a PDF. Source is 'path' INSIDE the agent folder "
               "'%s' or 'uid' of a board document. mode 'plain' (default) or "
               "'layout' (keeps columns). Returns per-page character counts and "
               "the text (max_chars, default 4000); save_to writes the whole text "
               "to a .txt inside the sandbox. Call this BEFORE replace_text so you "
               "know the exact wording." % sb,
               S({"path": path_arg, "uid": uid_arg, "pages": pages_arg,
                  "mode": {"type": "string", "enum": ["plain", "layout"]},
                  "max_chars": {"type": "integer"},
                  "save_to": {"type": "string"},
                  "password": pwd_arg, "overwrite": over_arg},
               []),
               t_extract_text)

    tools._add("board_pdf_extract_images",
               "Save the pictures stored inside a PDF as image files in 'folder' "
               "INSIDE the agent folder '%s'. Source is 'path' or a board 'uid'. "
               "max_images limits the run (default 50, cap 200). Returns file "
               "names with their page and pixel size. Names are made unique "
               "unless overwrite:true." % sb,
               S({"path": path_arg, "uid": uid_arg, "pages": pages_arg,
                  "folder": {"type": "string"},
                  "max_images": {"type": "integer"}, "password": pwd_arg,
                  "overwrite": over_arg},
               ["folder"]),
               t_extract_images)

    tools._add("board_pdf_add_text",
               "Draw text ON TOP of PDF pages: stamp, header, page label, "
               "watermark. Coordinates from the TOP-left: x/y <=1 are fractions "
               "of the page, >1 are points; 'anchor' (default center) says which "
               "point of the text block sits there. font_size in points "
               "(default 18), color hex, opacity 0.05-1 (0.3 = watermark), rotate "
               "in degrees counter-clockwise, width enables word wrap. Cyrillic "
               "works. Write to 'out' inside '%s' or rewrite the source with "
               "overwrite:true. Nothing existing is erased." % sb,
               S({"path": path_arg, "uid": uid_arg, "out": out_arg,
                  "pages": pages_arg, "text": {"type": "string"},
                  "x": {"type": "number", "description": xy_desc},
                  "y": {"type": "number", "description": xy_desc},
                  "anchor": anchor_arg,
                  "font_size": {"type": "number"}, "leading": {"type": "number"},
                  "width": {"type": "number"}, "color": {"type": "string"},
                  "opacity": {"type": "number"}, "rotate": {"type": "number"},
                  "password": pwd_arg, "overwrite": over_arg},
               ["text"]),
               t_add_text)

    tools._add("board_pdf_add_image",
               "Draw a picture ON TOP of PDF pages: logo, signature, watermark. "
               "'image' must be inside the agent folder '%s'. x/y from the "
               "TOP-left (<=1 fraction, >1 points), anchor default center, "
               "width default 0.4 (40%% of the page width), height keeps the "
               "shape when omitted, opacity 0.05-1, rotate in degrees. Write to "
               "'out' or rewrite the source with overwrite:true." % sb,
               S({"path": path_arg, "uid": uid_arg, "out": out_arg,
                  "pages": pages_arg, "image": {"type": "string"},
                  "x": {"type": "number", "description": xy_desc},
                  "y": {"type": "number", "description": xy_desc},
                  "anchor": anchor_arg,
                  "width": {"type": "number"}, "height": {"type": "number"},
                  "opacity": {"type": "number"}, "rotate": {"type": "number"},
                  "password": pwd_arg, "overwrite": over_arg},
               ["image"]),
               t_add_image)

    tools._add("board_pdf_replace_text",
               "Change wording on PDF pages: find the old text, paint a rectangle "
               "over it ('cover', default white) and draw the new text. Give "
               "'find' + 'text', or an exact 'box' [x0,y0,x1,y1] from the TOP-left "
               "+ 'text'. font_size defaults to the size found on the page. "
               "IMPORTANT: the old glyphs stay inside the file and remain "
               "extractable - this is visual replacement, NOT secure redaction. "
               "If the wording is not found the call fails and lists the text "
               "that really is there. Result reports 'overflow' when the new text "
               "is wider than the old. Write to 'out' inside '%s' or use "
               "overwrite:true." % sb,
               S({"path": path_arg, "uid": uid_arg, "out": out_arg,
                  "pages": pages_arg,
                  "find": {"type": "string"}, "text": {"type": "string"},
                  "box": {"type": "array", "items": {"type": "number"},
                          "minItems": 4, "maxItems": 4},
                  "cover": {"type": "string"}, "color": {"type": "string"},
                  "font_size": {"type": "number"}, "pad": {"type": "number"},
                  "ignore_case": {"type": "boolean"},
                  "password": pwd_arg, "overwrite": over_arg},
               ["text"]),
               t_replace_text)

    tools._add("board_pdf_replace_image",
               "Swap a picture INSIDE one PDF page for another file (keeps the "
               "original rectangle and position). 'page' is 1-based and required; "
               "pick the picture with 'index' (0-based, default 0) or its 'name' "
               "from board_pdf_extract_images. 'image' must be inside '%s'. "
               "A different shape is stretched - the result warns about it. "
               "Inline artwork cannot be swapped. Write to 'out' or "
               "overwrite:true." % sb,
               S({"path": path_arg, "uid": uid_arg, "out": out_arg,
                  "page": {"type": "integer"},
                  "index": {"type": "integer"}, "name": {"type": "string"},
                  "image": {"type": "string"},
                  "password": pwd_arg, "overwrite": over_arg},
               ["page", "image"]),
               t_replace_image)

    tools._add("board_pdf_metadata",
               "Read or change the document info of a PDF. No arguments = read. "
               "Pass title / author / subject / keywords / creator to change "
               "them; write to 'out' inside the agent folder '%s' or rewrite the "
               "source with overwrite:true." % sb,
               S({"path": path_arg, "uid": uid_arg, "out": out_arg,
                  "title": {"type": "string"}, "author": {"type": "string"},
                  "subject": {"type": "string"}, "keywords": {"type": "string"},
                  "creator": {"type": "string"},
                  "password": pwd_arg, "overwrite": over_arg},
               []),
               t_metadata)


# ------------------------------------------------------------------ self-test
def _selftest():
    """python -m refsynapse.ai.board_pdf_edit_tools  (opens the real window)"""
    import sys
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication
    from ..main_window import MainWindow
    from .board_tools import BoardTools

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    tools = BoardTools(win)
    print("Registered tools:", len(tools.names()))
    for name in PDF_EDIT_TOOL_NAMES:
        print("  has", name, "->", name in tools.names())
    sb = sandbox_root(win)
    print("agent sandbox ->", sb)

    def run():
        folder = os.path.join(sb, "pdf_edit_selftest")
        os.makedirs(folder, exist_ok=True)

        def p(name):
            return os.path.join(folder, name)

        def show(label, result, *keys):
            print(label, "->", result.get("ok"),
                  [result.get(k) for k in keys], result.get("error", ""))

        made = tools.call("board_pdf_create",
                          {"path": p("src.pdf"),
                           "text": "Invoice number 12345\nTotal due: 100 USD\n"
                                   "Thank you for your business.",
                           "font_size": 14, "overwrite": True})
        show("create source", made, "pages")
        if not made.get("ok"):
            print("DONE - close the window to exit")
            return
        read = tools.call("board_pdf_extract_text",
                          {"path": p("src.pdf"), "save_to": p("text.txt"),
                           "overwrite": True})
        show("extract text", read, "total_chars", "saved_to")
        print("  wording found ->", "Invoice number 12345" in (read.get("text") or ""),
              "(expected True)")
        image = QImage(300, 200, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        painter.fillRect(image.rect(), QColor("#e07a5f"))
        painter.end()
        image.save(p("stamp.png"))
        mark = tools.call("board_pdf_add_text",
                          {"path": p("src.pdf"), "out": p("marked.pdf"),
                           "text": "ЧЕРНОВИК", "font_size": 60, "opacity": 0.3,
                           "rotate": 30, "color": "#cc0000", "overwrite": True})
        show("watermark", mark, "pages_touched")
        logo = tools.call("board_pdf_add_image",
                          {"path": p("marked.pdf"), "out": p("logo.pdf"),
                           "image": p("stamp.png"), "x": 0.95, "y": 0.95,
                           "anchor": "bottom-right", "width": 0.2,
                           "overwrite": True})
        show("logo", logo, "pages_touched", "placed")
        swap = tools.call("board_pdf_replace_text",
                          {"path": p("src.pdf"), "out": p("edited.pdf"),
                           "find": "Invoice number 12345",
                           "text": "Счёт номер 99999", "overwrite": True})
        show("replace text", swap, "pages_touched", "replaced")
        print("  honest about old text ->", swap.get("old_text_still_extractable"),
              "(expected True)")
        if swap.get("ok"):
            after = _reader(p("edited.pdf")).pages[0].extract_text() or ""
            print("  new wording visible ->", "99999" in after, "(expected True)")
            print("  old wording still extractable ->", "12345" in after,
                  "(expected True - painted over, not erased)")
        miss = tools.call("board_pdf_replace_text",
                          {"path": p("src.pdf"), "out": p("no.pdf"),
                           "find": "no such wording", "text": "x"})
        print("missing wording ->", miss.get("ok"), "(expected False)",
              miss.get("error", "")[:120])
        boxed = tools.call("board_pdf_replace_text",
                           {"path": p("src.pdf"), "out": p("boxed.pdf"),
                            "box": [0.08, 0.05, 0.7, 0.12], "text": "BOXED",
                            "font_size": 16, "overwrite": True})
        show("box replace", boxed, "pages_touched")
        meta = tools.call("board_pdf_metadata",
                          {"path": p("src.pdf"), "out": p("meta.pdf"),
                           "title": "Счёт", "author": "RefSynapse",
                           "overwrite": True})
        show("metadata", meta, "changed")
        print("  title kept ->", (meta.get("metadata") or {}).get("Title") == "Счёт",
              "(expected True)")
        album = tools.call("board_pdf_from_images",
                           {"path": p("album.pdf"), "images": [p("stamp.png")],
                            "overwrite": True})
        show("album", album, "pages")
        pulled = tools.call("board_pdf_extract_images",
                            {"path": p("album.pdf"), "folder": p("pulled")})
        show("extract images", pulled, "extracted", "files")
        other = QImage(300, 200, QImage.Format.Format_ARGB32)
        painter = QPainter(other)
        painter.fillRect(other.rect(), QColor("#3a6ea5"))
        painter.end()
        other.save(p("other.png"))
        swapped = tools.call("board_pdf_replace_image",
                             {"path": p("album.pdf"), "out": p("swapped.pdf"),
                              "page": 1, "index": 0, "image": p("other.png"),
                              "overwrite": True})
        show("replace image", swapped, "page", "old_pixels", "new_pixels")
        bad = tools.call("board_pdf_add_text",
                         {"path": p("src.pdf"), "out": p("bad.pdf"),
                          "text": "XXXXXXXX", "x": 0.99, "y": 0.99,
                          "anchor": "top-left", "font_size": 80})
        print("off-page text ->", bad.get("ok"), "(expected False)")
        bad = tools.call("board_pdf_add_text",
                         {"path": p("src.pdf"), "out": p("bad.pdf"),
                          "text": "x", "opacity": 5})
        print("bad opacity ->", bad.get("ok"), "(expected False)")
        bad = tools.call("board_pdf_add_text",
                         {"path": p("src.pdf"), "out": p("bad.pdf"),
                          "text": "x", "anchor": "middle"})
        print("bad anchor ->", bad.get("ok"), "(expected False)")
        bad = tools.call("board_pdf_replace_image",
                         {"path": p("album.pdf"), "out": p("bad.pdf"),
                          "page": 1, "index": 9, "image": p("other.png")})
        print("bad index ->", bad.get("ok"), "(expected False)")
        bad = tools.call("board_pdf_replace_image",
                         {"path": p("src.pdf"), "out": p("bad.pdf"),
                          "page": 1, "image": p("other.png")})
        print("no pictures ->", bad.get("ok"), "(expected False)")
        bad = tools.call("board_pdf_add_text", {"path": p("src.pdf"), "text": "x"})
        print("in-place guard ->", bad.get("ok"), "(expected False)")
        outside = os.path.join(os.path.dirname(sb), "pdf_edit_escape.pdf")
        bad = tools.call("board_pdf_add_text",
                         {"path": p("src.pdf"), "out": outside, "text": "escape"})
        print("write outside ->", bad.get("ok"), "(expected False)")
        print("outside created ->", os.path.exists(outside), "(expected False)")
        bad = tools.call("board_pdf_extract_images",
                         {"path": p("album.pdf"),
                          "folder": os.path.join(sb, "..", "escape_imgs")})
        print("folder outside ->", bad.get("ok"), "(expected False)")
        again = tools.call("board_pdf_add_text",
                           {"path": p("marked.pdf"), "text": "IN PLACE",
                            "x": 0.5, "y": 0.1, "font_size": 20, "overwrite": True})
        show("in place", again, "in_place", "pages_touched")
        print("files are in ->", folder)
        print("DONE - close the window to exit")

    QTimer.singleShot(800, run)
    sys.exit(app.exec())


if __name__ == "__main__":
    _selftest()