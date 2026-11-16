"""PDF tools for the RefSynapse agent - batch 1: create pages, cut and join.

board_pdf_create      - text -> PDF (word wrap, many pages, Cyrillic)
board_pdf_from_images - images -> PDF, one image per page
board_pdf_from_board  - board / selection / chosen items -> PDF
board_pdf_info        - page count, page sizes, rotation, metadata, encryption
board_pdf_merge       - join PDFs (whole files or page ranges)
board_pdf_split       - cut into single pages, chunks or explicit ranges
board_pdf_pages       - reorder, delete, duplicate, extract, rotate

Editing what is INSIDE a page (text, stamps, pictures) lives in
board_pdf_edit_tools.py; it is hooked up at the end of register_pdf_tools().

SANDBOX. Every file the agent reads or writes must stay inside the agent
folder (see sandbox.py). One deliberate exception: 'uid' points at a document
the USER placed on the board, so that item's own file may be read - board
content is user data, not agent initiative. Results always go to the sandbox.

Libraries: pypdf (page surgery), reportlab (drawing), Pillow (image decoding).
All optional: when they are missing the tools explain how to install them
instead of breaking application startup.

Registered from BoardTools._register() via register_pdf_tools(tools).
"""

import io
import os
import re

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor

from .board_tools import ToolError
from .sandbox import sandbox_root, sandbox_path, is_inside

try:
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A3, A4, A5, A6, LEGAL, LETTER, TABLOID
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    PDF_AVAILABLE = True
    PDF_IMPORT_ERROR = ""
except Exception as ex:                 # a missing library must not break the app
    PDF_AVAILABLE = False
    PDF_IMPORT_ERROR = "%s: %s" % (type(ex).__name__, ex)

try:
    from PIL import Image
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

PDF_TOOL_NAMES = ("board_pdf_create", "board_pdf_from_images", "board_pdf_from_board",
                  "board_pdf_info", "board_pdf_merge", "board_pdf_split",
                  "board_pdf_pages")

MAX_TEXT_CHARS = 200_000
MAX_IMAGES = 300
MAX_OUTPUT_PAGES = 2000
IMAGE_MAX_PIXELS = 40_000_000
EXPORT_MAX_PIXELS = 24_000_000
INSTALL_HINT = r".\python\python.exe -m pip install pypdf reportlab cryptography"


# ------------------------------------------------------------------ helpers
def _need_pdf():
    if not PDF_AVAILABLE:
        raise ToolError("PDF libraries are unavailable (%s). Install them into the "
                        "portable python: %s" % (PDF_IMPORT_ERROR, INSTALL_HINT))


_FONT = {}


def _font_name():
    """A Windows font that knows Cyrillic - reportlab's built-ins do not."""
    if _FONT.get("name"):
        return _FONT["name"]
    folder = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for file_name in ("segoeui.ttf", "arial.ttf", "calibri.ttf", "tahoma.ttf",
                      "verdana.ttf"):
        path = os.path.join(folder, file_name)
        if not os.path.isfile(path):
            continue
        try:
            pdfmetrics.registerFont(TTFont("RefSans", path))
        except Exception:
            continue
        _FONT["name"] = "RefSans"
        _FONT["file"] = path
        return "RefSans"
    raise ToolError("No usable TrueType font in '%s' - PDF text needs a Unicode "
                    "font (segoeui.ttf / arial.ttf)" % folder)


def _read_path(win, raw, key="path"):
    path = sandbox_path(win, raw, key=key)
    if not os.path.isfile(path):
        raise ToolError("File not found: %s" % path)
    return path


def _pdf_target(win, a, key="path"):
    """Output file: inside the sandbox, .pdf extension, never a silent overwrite."""
    path = sandbox_path(win, a.get(key), key=key)
    if os.path.isdir(path):
        raise ToolError("'%s' is a folder - give a full file name" % path)
    if not path.lower().endswith(".pdf"):
        path += ".pdf"
    if os.path.exists(path) and not a.get("overwrite"):
        raise ToolError("File already exists - pass overwrite:true to replace it")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as ex:
        raise ToolError("Cannot create the folder: %s" % ex)
    return path


def _source(tools, a, key="path"):
    """A PDF to read: a sandbox 'path' or a document the user put on the board."""
    if a.get("uid"):
        item = tools._item(a["uid"])
        if getattr(item, "kind", None) != "document":
            raise ToolError("'%s' is not a document" % getattr(item, "kind", "?"))
        path = getattr(item, "path", "")
        if not isinstance(path, str) or not os.path.isfile(path):
            raise ToolError("That document has no readable file on disk")
        if not path.lower().endswith(".pdf"):
            raise ToolError("'%s' is not a PDF" % os.path.basename(path))
        return os.path.realpath(path)
    return _read_path(tools.win, a.get(key), key=key)


def _reader(path, password=None):
    """Read through memory: the file stays unlocked, so it can be rewritten."""
    _need_pdf()
    try:
        with open(path, "rb") as stream:
            data = stream.read()
    except OSError as ex:
        raise ToolError("Cannot read '%s': %s" % (os.path.basename(path), ex))
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(password or ""):
            raise ToolError("'%s' is password-protected - pass the correct 'password'"
                            % os.path.basename(path))
    except ToolError:
        raise
    except Exception as ex:
        raise ToolError("'%s' is not a readable PDF: %s"
                        % (os.path.basename(path), ex))
    if not len(reader.pages):
        raise ToolError("'%s' has no pages" % os.path.basename(path))
    return reader


def _page_numbers(spec, total, what="pages"):
    """'1-3,5' / [1,2,3] / None -> 0-based indices. Never clamps silently."""
    if spec is None or spec == "" or spec == "all":
        return list(range(total))
    if isinstance(spec, bool):
        raise ToolError("'%s' must be page numbers" % what)
    if isinstance(spec, (int, float)):
        spec = [spec]
    if isinstance(spec, str):
        parts = [p.strip() for p in spec.replace(";", ",").split(",") if p.strip()]
    elif isinstance(spec, (list, tuple)):
        parts = list(spec)
    else:
        raise ToolError("'%s' must look like '1-3,5' or [1,2,3]" % what)
    out = []
    for part in parts:
        if isinstance(part, bool):
            raise ToolError("'%s' must be page numbers" % what)
        if isinstance(part, (int, float)):
            numbers = [int(part)]
        else:
            text = str(part).strip().lower()
            if text in ("all", "*"):
                out.extend(range(total))
                continue
            match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", text)
            if match:
                first, last = int(match.group(1)), int(match.group(2))
                if first > last:
                    raise ToolError("Range '%s' is backwards (start must be <= end)"
                                    % text)
                numbers = list(range(first, last + 1))
            elif re.fullmatch(r"\d+", text):
                numbers = [int(text)]
            else:
                raise ToolError("Cannot understand %s '%s' (use '1-3,5' or [1,2,3])"
                                % (what, part))
        for number in numbers:
            if not 1 <= number <= total:
                raise ToolError("Page %d does not exist - the document has %d pages "
                                "(valid range 1-%d)" % (number, total, total))
            out.append(number - 1)
    if not out:
        raise ToolError("'%s' selected no pages" % what)
    return out


def _save(writer, path):
    try:
        with open(path, "wb") as stream:
            writer.write(stream)
    except OSError as ex:
        raise ToolError("Cannot write '%s': %s" % (path, ex))
    return os.path.getsize(path)


def _unique(path):
    base, ext = os.path.splitext(path)
    number = 2
    while os.path.exists("%s_%d%s" % (base, number, ext)):
        number += 1
    return "%s_%d%s" % (base, number, ext)


def _page_size(name, orientation=None):
    sizes = {"a3": A3, "a4": A4, "a5": A5, "a6": A6,
             "letter": LETTER, "legal": LEGAL, "tabloid": TABLOID}
    key = str(name or "a4").strip().lower()
    if key not in sizes:
        raise ToolError("Unknown page size '%s' (a3, a4, a5, a6, letter, legal, "
                        "tabloid)" % name)
    mode = str(orientation or "portrait").strip().lower()
    if mode not in ("portrait", "landscape"):
        raise ToolError("'orientation' must be portrait or landscape")
    width, height = sizes[key]
    if mode == "landscape":
        width, height = height, width
    return float(width), float(height)


def _margin(value, default):
    if value is None:
        return float(default)
    try:
        margin = float(value)
    except (TypeError, ValueError):
        raise ToolError("'margin' must be a number in points (72 pt = 1 inch)")
    if not 0 <= margin <= 200:
        raise ToolError("'margin' must be between 0 and 200 points")
    return margin


def _color_hex(value, default="#000000"):
    color = QColor(value or default)
    if not color.isValid():
        raise ToolError("Invalid color '%s' (use #rrggbb)" % value)
    return color.name()


def _wrap(text, font, size, width):
    """Word wrap by real font metrics; over-long words are split, never dropped."""
    lines = []
    for raw in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw.strip():
            lines.append("")
            continue
        line = ""
        for word in raw.split():
            trial = word if not line else line + " " + word
            if pdfmetrics.stringWidth(trial, font, size) <= width:
                line = trial
                continue
            if line:
                lines.append(line)
                line = ""
            while pdfmetrics.stringWidth(word, font, size) > width and len(word) > 1:
                cut = len(word) - 1
                while cut > 1 and pdfmetrics.stringWidth(word[:cut], font, size) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines


def _text_pages(text, font, size, width, height, leading):
    step = size * leading
    per_page = max(1, int(height // step))
    pages = []
    for chunk in str(text).split("\f"):        # \f = forced page break
        lines = _wrap(chunk, font, size, width) or [""]
        for start in range(0, len(lines), per_page):
            pages.append(lines[start:start + per_page])
    return pages or [[""]]


def _prepare_image(path, tmp_dir, tmp_files):
    """(file, width_px, height_px) that reportlab can digest; converts if needed."""
    if not PIL_AVAILABLE:
        try:
            width, height = ImageReader(path).getSize()
        except Exception as ex:
            raise ToolError("Cannot read image '%s': %s" % (os.path.basename(path), ex))
        return path, int(width), int(height)
    try:
        image = Image.open(path)
    except Exception as ex:
        raise ToolError("Cannot read image '%s': %s" % (os.path.basename(path), ex))
    with image:
        width, height = image.size
        if width < 1 or height < 1:
            raise ToolError("Image '%s' is empty" % os.path.basename(path))
        simple = ((image.format or "").upper() in ("PNG", "JPEG")
                  and image.mode in ("RGB", "RGBA", "L"))
        if simple and width * height <= IMAGE_MAX_PIXELS:
            return path, int(width), int(height)
        try:
            copy = image.convert("RGBA" if image.mode in ("P", "LA", "PA", "RGBA")
                                 else "RGB")
        except Exception:
            copy = image.convert("L").convert("RGB")
    if copy.width * copy.height > IMAGE_MAX_PIXELS:
        ratio = (IMAGE_MAX_PIXELS / float(copy.width * copy.height)) ** 0.5
        copy = copy.resize((max(1, int(copy.width * ratio)),
                            max(1, int(copy.height * ratio))))
    try:
        os.makedirs(tmp_dir, exist_ok=True)
        target = os.path.join(tmp_dir, "img_%d.png" % len(tmp_files))
        copy.save(target, "PNG")
    except Exception as ex:
        raise ToolError("Cannot convert image '%s': %s" % (os.path.basename(path), ex))
    tmp_files.append(target)
    return target, copy.width, copy.height


def _images_pdf(prepared, path, size_name, orientation, margin, dpi, title=None):
    """One page per image. 'fit' makes the page exactly as big as the picture."""
    sizes = []
    for _, width, height in prepared:
        if size_name == "fit":
            page_w, page_h = width * 72.0 / dpi, height * 72.0 / dpi
        else:
            page_w, page_h = _page_size(size_name, orientation)
        if page_w - 2 * margin <= 0 or page_h - 2 * margin <= 0:
            raise ToolError("'margin' %g pt leaves no room on a %.0fx%.0f pt page"
                            % (margin, page_w, page_h))
        sizes.append((page_w, page_h))
    doc = canvas.Canvas(path, pagesize=sizes[0])
    if title:
        doc.setTitle(str(title))
    for (file_path, width, height), (page_w, page_h) in zip(prepared, sizes):
        doc.setPageSize((page_w, page_h))
        factor = min((page_w - 2 * margin) / width, (page_h - 2 * margin) / height)
        draw_w, draw_h = width * factor, height * factor
        doc.drawImage(ImageReader(file_path),
                      (page_w - draw_w) / 2.0, (page_h - draw_h) / 2.0,
                      width=draw_w, height=draw_h,
                      preserveAspectRatio=False, mask="auto")
        doc.showPage()
    doc.save()
    return [[round(w, 1), round(h, 1)] for w, h in sizes]


def _cleanup(tmp_files, tmp_dir):
    for path in tmp_files:
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        if os.path.isdir(tmp_dir) and not os.listdir(tmp_dir):
            os.rmdir(tmp_dir)
    except OSError:
        pass


# --------------------------------------------------------------- registration
def register_pdf_tools(tools):
    """Batch 1: make PDFs and operate on whole pages."""
    win = tools.win
    S = tools._schema
    sb = sandbox_root(win)
    tmp_dir = os.path.join(sb, "_pdf_tmp")

    try:
        from ..export.controller import render_board, render_items
        export_available = True
    except Exception:
        export_available = False

    path_arg = {"type": "string",
                "description": "File inside the agent folder '%s'" % sb}
    uid_arg = {"type": "string",
               "description": "uid of a PDF document already on the board"}
    pages_arg = {"type": "string",
                 "description": "Page selection, 1-based: '1-3,5' or 'all'"}
    size_arg = {"type": "string",
                "description": "a3, a4, a5, a6, letter, legal, tabloid (default a4)"}
    orient_arg = {"type": "string", "enum": ["portrait", "landscape"]}
    over_arg = {"type": "boolean",
                "description": "Replace an existing file (default false)"}

    # ---- text -> PDF -----------------------------------------------------
    def t_create(a):
        _need_pdf()
        text = a.get("text")
        if text is None or not str(text).strip():
            raise ToolError("'text' must not be empty")
        if len(str(text)) > MAX_TEXT_CHARS:
            raise ToolError("'text' is too long (%d chars, max %d)"
                            % (len(str(text)), MAX_TEXT_CHARS))
        size = float(a.get("font_size", 12))
        if not 4 <= size <= 144:
            raise ToolError("'font_size' must be between 4 and 144")
        leading = float(a.get("leading", 1.35))
        if not 1.0 <= leading <= 3.0:
            raise ToolError("'leading' must be between 1.0 and 3.0")
        page_w, page_h = _page_size(a.get("page_size"), a.get("orientation"))
        margin = _margin(a.get("margin"), 48)
        if page_w - 2 * margin < size or page_h - 2 * margin < size * leading:
            raise ToolError("'margin' %g pt leaves no room on a %.0fx%.0f pt page"
                            % (margin, page_w, page_h))
        color = _color_hex(a.get("color"))
        path = _pdf_target(win, a)
        font = _font_name()
        pages = _text_pages(text, font, size, page_w - 2 * margin,
                            page_h - 2 * margin, leading)
        doc = canvas.Canvas(path, pagesize=(page_w, page_h))
        if a.get("title"):
            doc.setTitle(str(a["title"]))
        for lines in pages:
            doc.setFont(font, size)
            doc.setFillColor(HexColor(color))
            y = page_h - margin - size
            for line in lines:
                if line:
                    doc.drawString(margin, y, line)
                y -= size * leading
            doc.showPage()
        doc.save()
        return {"path": path, "pages": len(pages),
                "page_size_pt": [round(page_w, 1), round(page_h, 1)],
                "font": _FONT.get("file", ""), "bytes": os.path.getsize(path)}

    # ---- images -> PDF ---------------------------------------------------
    def t_from_images(a):
        _need_pdf()
        raw = a.get("images") or []
        if isinstance(raw, str):
            raw = [raw]
        if not raw:
            raise ToolError("'images' must list at least one image file inside "
                            "the agent folder '%s'" % sb)
        if len(raw) > MAX_IMAGES:
            raise ToolError("Too many images (%d, max %d)" % (len(raw), MAX_IMAGES))
        size_name = str(a.get("page_size") or "fit").strip().lower()
        dpi = float(a.get("dpi", 96))
        if not 36 <= dpi <= 600:
            raise ToolError("'dpi' must be between 36 and 600")
        margin = _margin(a.get("margin"), 0 if size_name == "fit" else 24)
        sources = [_read_path(win, item, key="images") for item in raw]
        path = _pdf_target(win, a)
        tmp_files = []
        try:
            prepared = [_prepare_image(p, tmp_dir, tmp_files) for p in sources]
            sizes = _images_pdf(prepared, path, size_name, a.get("orientation"),
                                margin, dpi, a.get("title"))
        finally:
            _cleanup(tmp_files, tmp_dir)
        return {"path": path, "pages": len(sizes), "page_size_pt": sizes[0],
                "page_size_mode": size_name, "bytes": os.path.getsize(path)}

    # ---- board -> PDF ----------------------------------------------------
    def t_from_board(a):
        _need_pdf()
        if not export_available:
            raise ToolError("Export subsystem is unavailable in this build")
        tools._quiesce()
        scope = a.get("scope", "board")
        layout = a.get("layout", "single")
        if layout not in ("single", "per_item"):
            raise ToolError("Unknown layout '%s' (single or per_item)" % layout)
        if scope == "board":
            items = win.top_items()
        elif scope == "selection":
            items = win.selected_top()
        elif scope == "items":
            items = tools._items(a.get("uids"))
        else:
            raise ToolError("Unknown scope '%s' (board, selection, items)" % scope)
        if not items:
            raise ToolError("Nothing to export (%s is empty)" % scope)
        groups = [items] if layout == "single" else [[item] for item in items]
        if len(groups) > MAX_OUTPUT_PAGES:
            raise ToolError("This would create %d pages (max %d)"
                            % (len(groups), MAX_OUTPUT_PAGES))
        scale = float(a.get("scale", 1.0))
        if not 0.25 <= scale <= 4:
            raise ToolError("'scale' must be between 0.25 and 4")
        background = a.get("background", "#ffffff")
        if background == "transparent":
            background = None
        elif not QColor(background).isValid():
            raise ToolError("Invalid background '%s' (hex or 'transparent')"
                            % background)
        size_name = str(a.get("page_size") or "fit").strip().lower()
        dpi = float(a.get("dpi", 96))
        if not 36 <= dpi <= 600:
            raise ToolError("'dpi' must be between 36 and 600")
        margin = _margin(a.get("margin"), 0 if size_name == "fit" else 24)
        path = _pdf_target(win, a)
        tmp_files, prepared = [], []
        try:
            for index, group in enumerate(groups):
                rect = QRectF()
                for item in group:
                    rect = rect.united(win.full_rect(item))
                if rect.isEmpty():
                    raise ToolError("Nothing to export (empty area)")
                step = scale
                if rect.width() * rect.height() * step * step > EXPORT_MAX_PIXELS:
                    step = (EXPORT_MAX_PIXELS
                            / (rect.width() * rect.height())) ** 0.5
                if scope == "board" and layout == "single":
                    image = render_board(win, step, background)
                else:
                    image = render_items(win, group, step, background)
                if image.isNull():
                    raise ToolError("Rendering failed for page %d" % (index + 1))
                os.makedirs(tmp_dir, exist_ok=True)
                shot = os.path.join(tmp_dir, "board_%d.png" % index)
                if not image.save(shot):
                    raise ToolError("Could not save the render of page %d"
                                    % (index + 1))
                tmp_files.append(shot)
                prepared.append(_prepare_image(shot, tmp_dir, tmp_files))
            sizes = _images_pdf(prepared, path, size_name, a.get("orientation"),
                                margin, dpi, a.get("title"))
        finally:
            _cleanup(tmp_files, tmp_dir)
        return {"path": path, "pages": len(sizes), "scope": scope, "layout": layout,
                "page_size_pt": sizes[0], "bytes": os.path.getsize(path)}

    # ---- info ------------------------------------------------------------
    def t_info(a):
        src = _source(tools, a)
        reader = _reader(src, a.get("password"))
        total = len(reader.pages)
        limit = max(1, min(500, int(a.get("max_pages", 50))))
        probe = bool(a.get("text_probe", True))
        info = []
        for index in range(min(total, limit)):
            page = reader.pages[index]
            box = page.mediabox
            entry = {"page": index + 1,
                     "width_pt": round(float(box.width), 1),
                     "height_pt": round(float(box.height), 1),
                     "rotation": int(page.rotation)}
            if probe:
                try:
                    entry["text_chars"] = len(page.extract_text() or "")
                except Exception:
                    entry["text_chars"] = -1
            info.append(entry)
        meta = {}
        try:
            for key, value in (reader.metadata or {}).items():
                if value:
                    meta[str(key).lstrip("/")] = str(value)[:200]
        except Exception:
            pass
        return {"path": src, "pages": total, "encrypted": bool(reader.is_encrypted),
                "metadata": meta, "page_info": info, "truncated": total > len(info),
                "hint": "Look at pages with board_export_pages; change order or "
                        "rotation with board_pdf_pages."}

    # ---- merge -----------------------------------------------------------
    def t_merge(a):
        _need_pdf()
        raw = a.get("sources") or []
        if isinstance(raw, (str, dict)):
            raw = [raw]
        if not raw:
            raise ToolError("'sources' must list the PDFs to join, e.g. "
                            "['a.pdf', {'path': 'b.pdf', 'pages': '1-2'}]")
        if len(raw) > 50:
            raise ToolError("Too many sources (%d, max 50)" % len(raw))
        path = _pdf_target(win, a)
        writer = PdfWriter()
        parts, marks = [], []
        for entry in raw:
            if isinstance(entry, str):
                spec = {"path": entry}
            elif isinstance(entry, dict):
                spec = entry
            else:
                raise ToolError("Each source must be a path string or "
                                "{'path': ..., 'pages': '1-3'}")
            src = _source(tools, spec)
            reader = _reader(src, spec.get("password") or a.get("password"))
            indices = _page_numbers(spec.get("pages"), len(reader.pages))
            if len(writer.pages) + len(indices) > MAX_OUTPUT_PAGES:
                raise ToolError("The result would exceed %d pages" % MAX_OUTPUT_PAGES)
            start = len(writer.pages)
            for index in indices:
                writer.add_page(reader.pages[index])
            parts.append({"file": os.path.basename(src), "pages": len(indices),
                          "start_page": start + 1})
            marks.append((os.path.splitext(os.path.basename(src))[0], start))
        if a.get("bookmarks"):
            for title, start in marks:
                writer.add_outline_item(title=title, page_number=start)
        if a.get("title"):
            writer.add_metadata({"/Title": str(a["title"])})
        size = _save(writer, path)
        return {"path": path, "pages": len(writer.pages), "sources": parts,
                "bytes": size}

    # ---- split -----------------------------------------------------------
    def t_split(a):
        src = _source(tools, a)
        reader = _reader(src, a.get("password"))
        total = len(reader.pages)
        folder = sandbox_path(win, a.get("folder"), key="folder", folder=True)
        if os.path.isfile(folder):
            raise ToolError("'%s' is a file - give a folder" % folder)
        mode = a.get("mode", "each")
        if mode == "each":
            groups = [[index] for index in range(total)]
        elif mode == "chunks":
            every = int(a.get("every", 0))
            if not 1 <= every <= 500:
                raise ToolError("mode 'chunks' needs 'every' between 1 and 500")
            groups = [list(range(start, min(start + every, total)))
                      for start in range(0, total, every)]
        elif mode == "ranges":
            raw = a.get("ranges") or []
            if isinstance(raw, str):
                raw = [raw]
            if not raw:
                raise ToolError("mode 'ranges' needs 'ranges', e.g. ['1-3','4-6']")
            groups = [_page_numbers(item, total, what="ranges") for item in raw]
        else:
            raise ToolError("Unknown mode '%s' (each, chunks, ranges)" % mode)
        if len(groups) > 500:
            raise ToolError("This would create %d files (max 500) - use mode "
                            "'chunks' or 'ranges'" % len(groups))
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as ex:
            raise ToolError("Cannot create the folder: %s" % ex)
        stem = os.path.splitext(os.path.basename(src))[0]
        files = []
        for number, group in enumerate(groups, 1):
            writer = PdfWriter()
            for index in group:
                writer.add_page(reader.pages[index])
            label = ("%s_p%03d" % (stem, group[0] + 1) if mode == "each"
                     else "%s_part%02d" % (stem, number))
            target = os.path.join(folder, label + ".pdf")
            if os.path.exists(target) and not a.get("overwrite"):
                target = _unique(target)
            _save(writer, target)
            files.append({"file": os.path.basename(target), "pages": len(group),
                          "from": group[0] + 1, "to": group[-1] + 1})
        return {"folder": folder, "created": len(files), "pages_total": total,
                "mode": mode, "files": files[:50], "truncated": len(files) > 50}

    # ---- order / rotate --------------------------------------------------
    def t_pages(a):
        src = _source(tools, a)
        reader = _reader(src, a.get("password"))
        total = len(reader.pages)
        order, rotate = a.get("order"), a.get("rotate")
        if order is None and rotate is None:
            raise ToolError("Nothing to do - pass 'order' (e.g. [3,1,2]) and/or "
                            "'rotate' (90, 180 or 270)")
        indices = (_page_numbers(order, total, what="order") if order is not None
                   else list(range(total)))
        degrees = 0
        targets = []
        if rotate is not None:
            degrees = int(rotate)
            if degrees % 90 or abs(degrees) > 270:
                raise ToolError("'rotate' must be -270, -180, -90, 0, 90, 180 or 270")
            if degrees % 360:
                targets = _page_numbers(a.get("rotate_pages"), len(indices),
                                        what="rotate_pages")
        if indices == list(range(total)) and not targets:
            raise ToolError("This call changes nothing (order is already 1..%d and "
                            "no page is turned) - send a different order or a "
                            "rotation of 90/180/270" % total)
        raw_out = a.get("out")
        if raw_out:
            path = _pdf_target(win, a, key="out")
        else:
            if not is_inside(win, src):
                raise ToolError("'%s' is a board file outside the agent folder - "
                                "give 'out' inside '%s'" % (os.path.basename(src), sb))
            if not a.get("overwrite"):
                raise ToolError("This would rewrite '%s' in place - pass "
                                "overwrite:true, or give 'out' for a new file"
                                % os.path.basename(src))
            path = src
        writer = PdfWriter()
        for index in indices:
            writer.add_page(reader.pages[index])
        for position in targets:
            writer.pages[position].rotate(degrees)
        size = _save(writer, path)
        return {"path": path, "pages_before": total, "pages_after": len(indices),
                "order": [index + 1 for index in indices],
                "rotated": [position + 1 for position in targets],
                "rotate_degrees": degrees, "in_place": not raw_out, "bytes": size}

    # ---- registry --------------------------------------------------------
    tools._add("board_pdf_create",
               "Create a PDF from text INSIDE the agent folder '%s'. Text is word "
               "wrapped and split into as many pages as needed; Cyrillic works "
               "(a Windows font is embedded). '\\f' in the text forces a new page. "
               "font_size in points (default 12), margin in points (default 48), "
               "leading = line spacing factor (default 1.35). Existing files need "
               "overwrite:true." % sb,
               S({"path": path_arg, "text": {"type": "string"},
                  "title": {"type": "string"},
                  "page_size": size_arg, "orientation": orient_arg,
                  "font_size": {"type": "number"}, "margin": {"type": "number"},
                  "leading": {"type": "number"},
                  "color": {"type": "string", "description": "Hex text color"},
                  "overwrite": over_arg},
               ["path", "text"]),
               t_create)

    tools._add("board_pdf_from_images",
               "Create a PDF from image files, one image per page, in the given "
               "order. All paths must be INSIDE the agent folder '%s'. "
               "page_size 'fit' (default) makes each page exactly the size of its "
               "image at 'dpi' (default 96); or name a sheet (a4, a3, letter...) "
               "and the image is centred and scaled to fit inside 'margin' "
               "(default 24 pt). Existing files need overwrite:true." % sb,
               S({"path": path_arg,
                  "images": {"type": "array", "items": {"type": "string"},
                             "description": "Image files inside the agent folder"},
                  "title": {"type": "string"},
                  "page_size": {"type": "string",
                                "description": "'fit' (default) or a3/a4/a5/a6/"
                                               "letter/legal/tabloid"},
                  "orientation": orient_arg,
                  "margin": {"type": "number"}, "dpi": {"type": "number"},
                  "overwrite": over_arg},
               ["path", "images"]),
               t_from_images)

    tools._add("board_pdf_from_board",
               "Export the board to a PDF INSIDE the agent folder '%s' using the "
               "same renderer as PNG export. scope: board (default), selection or "
               "items (with 'uids'). layout: 'single' = everything on one page, "
               "'per_item' = one page per object. page_size 'fit' (default) or a "
               "named sheet; background hex or 'transparent' (default white); "
               "scale 0.25-4. Existing files need overwrite:true." % sb,
               S({"path": path_arg,
                  "scope": {"type": "string", "enum": ["board", "selection", "items"]},
                  "uids": {"type": "array", "items": {"type": "string"}},
                  "layout": {"type": "string", "enum": ["single", "per_item"]},
                  "page_size": {"type": "string"}, "orientation": orient_arg,
                  "margin": {"type": "number"}, "dpi": {"type": "number"},
                  "scale": {"type": "number"}, "background": {"type": "string"},
                  "title": {"type": "string"}, "overwrite": over_arg},
               ["path"]),
               t_from_board)

    tools._add("board_pdf_info",
               "Inspect a PDF: page count, page sizes in points, rotation, "
               "metadata and whether pages contain text. Source is 'path' INSIDE "
               "the agent folder '%s' or 'uid' of a PDF document on the board. "
               "max_pages limits the per-page report (default 50). Call this "
               "BEFORE cutting, joining or reordering." % sb,
               S({"path": path_arg, "uid": uid_arg,
                  "password": {"type": "string"},
                  "max_pages": {"type": "integer"},
                  "text_probe": {"type": "boolean"}},
               []),
               t_info)

    tools._add("board_pdf_merge",
               "Join PDFs into one file INSIDE the agent folder '%s' - two files "
               "into one, or any number of them. 'sources' is a list of paths or "
               "{'path': ..., 'pages': '1-3,5'} objects; order of the list is the "
               "order in the result. Each source may be a 'uid' of a board "
               "document. bookmarks:true adds one bookmark per source. Existing "
               "files need overwrite:true." % sb,
               S({"path": path_arg,
                  "sources": {"type": "array",
                              "items": {"type": ["string", "object"]},
                              "description": "Paths or {path/uid, pages, password}"},
                  "title": {"type": "string"},
                  "bookmarks": {"type": "boolean"},
                  "password": {"type": "string"},
                  "overwrite": over_arg},
               ["path", "sources"]),
               t_merge)

    tools._add("board_pdf_split",
               "Cut a PDF into several files in 'folder' INSIDE the agent folder "
               "'%s'. mode 'each' = one file per page, 'chunks' = every 'every' "
               "pages, 'ranges' = one file per entry of 'ranges' "
               "(['1-3','4-6']). Source is 'path' or a board document 'uid'. "
               "Names are made unique unless overwrite:true." % sb,
               S({"path": path_arg, "uid": uid_arg,
                  "folder": {"type": "string",
                             "description": "Target folder inside the agent folder"},
                  "mode": {"type": "string", "enum": ["each", "chunks", "ranges"]},
                  "every": {"type": "integer"},
                  "ranges": {"type": "array", "items": {"type": "string"}},
                  "password": {"type": "string"}, "overwrite": over_arg},
               ["folder"]),
               t_split)

    tools._add("board_pdf_pages",
               "Reorder, delete, duplicate, extract or turn pages of a PDF. "
               "'order' is the new sequence of 1-based page numbers: [3,1,2] "
               "reorders, [1,3] drops page 2, [1,1,2] duplicates page 1. "
               "'rotate' turns pages clockwise by 90/180/270 (optionally only "
               "'rotate_pages' of the RESULT). Write to 'out' INSIDE the agent "
               "folder '%s', or rewrite the source with overwrite:true. "
               "Source is 'path' or a board document 'uid'." % sb,
               S({"path": path_arg, "uid": uid_arg,
                  "out": {"type": "string",
                          "description": "New file inside the agent folder"},
                  "order": {"type": "array", "items": {"type": "integer"}},
                  "rotate": {"type": "integer"},
                  "rotate_pages": {"type": "array", "items": {"type": "integer"}},
                  "password": {"type": "string"}, "overwrite": over_arg},
               []),
               t_pages)

    # Content editing (batch 2) lives in board_pdf_edit_tools.py.
    try:
        from .board_pdf_edit_tools import register_pdf_edit_tools
    except ImportError:
        register_pdf_edit_tools = None
    if register_pdf_edit_tools is not None:
        register_pdf_edit_tools(tools)


# ------------------------------------------------------------------ self-test
def _selftest():
    """python -m refsynapse.ai.board_pdf_tools  (opens the real window)"""
    import sys
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication
    from ..main_window import MainWindow
    from .board_tools import BoardTools

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    tools = BoardTools(win)
    print("Registered tools:", len(tools.names()))
    print("pdf libs ->", PDF_AVAILABLE, PDF_IMPORT_ERROR)
    for name in PDF_TOOL_NAMES:
        print("  has", name, "->", name in tools.names())
    sb = sandbox_root(win)
    print("agent sandbox ->", sb)

    def run():
        folder = os.path.join(sb, "pdf_selftest")
        os.makedirs(folder, exist_ok=True)

        def p(name):
            return os.path.join(folder, name)

        def show(label, result, *keys):
            values = [result.get(k) for k in keys]
            print(label, "->", result.get("ok"), values, result.get("error", ""))

        # 1. text -> PDF, Cyrillic, several pages
        text = "Привет, PDF! Ёжики и снег.\n" * 200
        r = tools.call("board_pdf_create",
                       {"path": p("text.pdf"), "text": text,
                        "title": "Самотест", "overwrite": True})
        show("create text", r, "pages", "bytes")
        if r.get("ok"):
            reader = _reader(p("text.pdf"))
            first = reader.pages[0].extract_text() or ""
            print("  pages >= 2 ->", len(reader.pages) >= 2, "(expected True)")
            print("  cyrillic kept ->", "Привет" in first, "(expected True)")
            print("  title kept ->",
                  str((reader.metadata or {}).get("/Title")) == "Самотест",
                  "(expected True)")

        # 2. guards on creation
        bad = tools.call("board_pdf_create", {"path": p("text.pdf"), "text": "x"})
        print("overwrite guard ->", bad.get("ok"), "(expected False)",
              bad.get("error", ""))
        empty = tools.call("board_pdf_create", {"path": p("empty.pdf"), "text": "  "})
        print("empty text ->", empty.get("ok"), "(expected False)")

        # 3. images -> PDF (one plain, one with transparency)
        shots = []
        for index, color in enumerate(("#3a6ea5", "#e07a5f")):
            image = QImage(800, 600, QImage.Format.Format_ARGB32)
            painter = QPainter(image)
            painter.fillRect(image.rect(), QColor(color))
            painter.end()
            shot = p("pic%d.png" % index)
            image.save(shot)
            shots.append(shot)
        r = tools.call("board_pdf_from_images",
                       {"path": p("images.pdf"), "images": shots,
                        "overwrite": True})
        show("images fit", r, "pages", "page_size_pt")
        r = tools.call("board_pdf_from_images",
                       {"path": p("images_a4.pdf"), "images": shots,
                        "page_size": "a4", "orientation": "landscape",
                        "overwrite": True})
        show("images a4", r, "pages", "page_size_pt")

        # 4. board -> PDF
        frame = tools.call("board_add_frame", {"x": 0, "y": 0, "width": 400,
                                               "height": 300, "color": "#ff8800"})
        label = tools.call("board_add_text", {"text": "PDF самотест",
                                              "x": 20, "y": 340})
        uids = [frame.get("item", {}).get("uid"), label.get("item", {}).get("uid")]
        r = tools.call("board_pdf_from_board",
                       {"path": p("board.pdf"), "scope": "board",
                        "page_size": "a4", "orientation": "landscape",
                        "overwrite": True})
        show("board single", r, "pages", "page_size_pt")
        r = tools.call("board_pdf_from_board",
                       {"path": p("board_items.pdf"), "scope": "items",
                        "uids": uids, "layout": "per_item", "overwrite": True})
        show("board per_item", r, "pages")

        # 5. info
        r = tools.call("board_pdf_info", {"path": p("images.pdf")})
        show("info", r, "pages", "encrypted")
        print("  page_info ->", r.get("page_info"))

        # 6. merge with a page range + bookmarks
        r = tools.call("board_pdf_merge",
                       {"path": p("merged.pdf"),
                        "sources": [{"path": p("text.pdf"), "pages": "1-2"},
                                    p("images.pdf")],
                        "bookmarks": True, "title": "Склейка", "overwrite": True})
        show("merge", r, "pages", "sources")
        merged_pages = r.get("pages", 0)

        # 7. reorder + rotate into a new file
        r = tools.call("board_pdf_pages",
                       {"path": p("merged.pdf"), "out": p("ordered.pdf"),
                        "order": [4, 1, 1], "rotate": 90, "rotate_pages": [1],
                        "overwrite": True})
        show("pages", r, "pages_before", "pages_after", "rotated")
        if r.get("ok"):
            reader = _reader(p("ordered.pdf"))
            print("  rotations ->", [int(pg.rotation) for pg in reader.pages],
                  "(expected [90, 0, 0])")

        # 8. refusals with numbers, never a silent no-op
        bad = tools.call("board_pdf_pages",
                         {"path": p("ordered.pdf"), "out": p("no.pdf"),
                          "order": [99]})
        print("bad page ->", bad.get("ok"), "(expected False)", bad.get("error", ""))
        noop = tools.call("board_pdf_pages",
                          {"path": p("ordered.pdf"), "out": p("no.pdf"),
                           "order": [1, 2, 3]})
        print("no-op ->", noop.get("ok"), "(expected False)", noop.get("error", ""))
        nothing = tools.call("board_pdf_pages", {"path": p("ordered.pdf"),
                                                 "out": p("no.pdf")})
        print("no arguments ->", nothing.get("ok"), "(expected False)")

        # 9. split
        r = tools.call("board_pdf_split",
                       {"path": p("merged.pdf"), "folder": p("parts"),
                        "mode": "each", "overwrite": True})
        show("split each", r, "created", "pages_total")
        r = tools.call("board_pdf_split",
                       {"path": p("merged.pdf"), "folder": p("parts"),
                        "mode": "ranges", "ranges": ["1-2", "3-4"],
                        "overwrite": True})
        show("split ranges", r, "created", "files")
        r = tools.call("board_pdf_split",
                       {"path": p("merged.pdf"), "folder": p("parts"),
                        "mode": "chunks", "every": 3, "overwrite": True})
        show("split chunks", r, "created")

        # 10. sandbox guards
        outside = os.path.join(os.path.dirname(sb), "pdf_escape.pdf")
        bad = tools.call("board_pdf_create",
                         {"path": outside, "text": "escape", "overwrite": True})
        print("write outside ->", bad.get("ok"), "(expected False)",
              bad.get("error", ""))
        print("outside created ->", os.path.exists(outside), "(expected False)")
        bad = tools.call("board_pdf_merge",
                         {"path": p("x.pdf"), "sources": [outside]})
        print("read outside ->", bad.get("ok"), "(expected False)")
        bad = tools.call("board_pdf_create",
                         {"path": p("dot.pdf"), "text": "x", "overwrite": True})
        bad2 = tools.call("board_pdf_pages",
                          {"path": p("dot.pdf"), "out": os.path.join(sb, "..", "esc.pdf"),
                           "order": [1], "rotate": 90})
        print("dot-dot escape ->", bad2.get("ok"), "(expected False)")

        # 11. in-place rewrite of a sandbox file
        r = tools.call("board_pdf_pages",
                       {"path": p("ordered.pdf"), "order": [3, 2, 1],
                        "overwrite": True})
        show("in place", r, "pages_after", "in_place")
        r = tools.call("board_pdf_pages", {"path": p("ordered.pdf"),
                                           "order": [3, 2, 1]})
        print("in-place guard ->", r.get("ok"), "(expected False)", r.get("error", ""))

        print("merged pages ->", merged_pages, "(expected 4)")
        print("files are in ->", folder)
        tools.call("board_delete", {"uids": uids})
        print("DONE - close the window to exit")

    QTimer.singleShot(800, run)
    sys.exit(app.exec())


if __name__ == "__main__":
    _selftest()