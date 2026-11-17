"""Searchable, paginated document, PDF and SVG objects for the canvas."""
import math
import os
import re
import zipfile
import xml.etree.ElementTree as ET

from PySide6.QtCore import Qt, QRectF, QSize, QSizeF
from PySide6.QtGui import (QColor, QFont, QPainter, QPainterPath, QPixmap,
                           QPen, QTextDocument)
from PySide6.QtWidgets import QGraphicsItem

try:
    from PySide6.QtPdf import QPdfDocument
except ImportError:  # Optional in unusually trimmed PySide6 installations.
    QPdfDocument = None
try:
    from PySide6.QtSvg import QSvgRenderer
except ImportError:
    QSvgRenderer = None

from .config import FLAGS, ev, new_id
from .items import draw_selection, place_note


TEXT_EXT = {".txt", ".md", ".markdown", ".rtf", ".html", ".htm",
            ".docx", ".odt", ".csv", ".json", ".log", ".xml", ".py"}
PDF_EXT = {".pdf"}
SVG_EXT = {".svg", ".svgz"}
DOCUMENT_EXT = TEXT_EXT | PDF_EXT | SVG_EXT


def decode_text(data):
    """Decode common plaintext encodings, including Russian Windows files."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("cp1251")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def docx_text(path):
    """Extract DOCX paragraphs using only the Python standard library."""
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    paragraphs = []
    for paragraph in root.iter():
        if not paragraph.tag.endswith("}p"):
            continue
        chunks = []
        for node in paragraph.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag in ("t", "delText", "instrText") and node.text:
                chunks.append(node.text)
            elif tag == "tab":
                chunks.append("\t")
            elif tag in ("br", "cr"):
                chunks.append("\n")
        paragraphs.append("".join(chunks))
    return "\n".join(paragraphs)


def odt_text(path):
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("content.xml"))
    paragraphs = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] in ("p", "h"):
            paragraphs.append("".join(node.itertext()))
    return "\n".join(paragraphs)


def rtf_text(data):
    """Small dependency-free RTF reader for readable reference documents."""
    text = decode_text(data)
    output, stack = [], []
    ignorable, uc_skip = 0, 1
    i = 0
    destinations = {"fonttbl", "colortbl", "stylesheet", "info", "pict",
                    "object", "header", "footer", "generator", "datastore"}
    while i < len(text):
        char = text[i]
        if char == "{":
            stack.append((ignorable, uc_skip))
            i += 1
        elif char == "}":
            if stack:
                ignorable, uc_skip = stack.pop()
            i += 1
        elif char == "\\":
            i += 1
            if i >= len(text):
                break
            if text[i] == "'":
                hex_digits = text[i + 1:i + 3]
                if not ignorable and len(hex_digits) == 2:
                    try:
                        output.append(bytes.fromhex(hex_digits).decode("cp1251"))
                    except (ValueError, UnicodeDecodeError):
                        pass
                i += 3
                continue
            if not text[i].isalpha():
                if not ignorable and text[i] in ("\\", "{", "}"):
                    output.append(text[i])
                elif not ignorable and text[i] in ("\n", "\r"):
                    output.append("\n")
                i += 1
                continue
            match = re.match(r"([a-zA-Z]+)(-?\d+)? ?", text[i:])
            if not match:
                i += 1
                continue
            word, number = match.group(1), match.group(2)
            i += match.end()
            if word in destinations:
                ignorable += 1
            elif word == "uc":
                uc_skip = int(number or 1)
            elif word == "u" and not ignorable:
                try:
                    code = int(number)
                    output.append(chr(code + 65536 if code < 0 else code))
                except (ValueError, OverflowError):
                    pass
                # Consume the fallback character(s) following \uN.
                i += uc_skip
            elif not ignorable:
                if word in ("par", "line", "row", "sect"):
                    output.append("\n")
                elif word == "tab":
                    output.append("\t")
                elif word == "emdash":
                    output.append("—")
                elif word == "endash":
                    output.append("–")
        elif char in ("\r", "\n"):
            i += 1
        else:
            if not ignorable:
                output.append(char)
            i += 1
    return "".join(output)


def read_text_document(path):
    """Return (plain_text, html_or_none) without external Python packages."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return docx_text(path), None
    if ext == ".odt":
        return odt_text(path), None
    with open(path, "rb") as stream:
        data = stream.read()
    if ext == ".rtf":
        return rtf_text(data), None
    text = decode_text(data)
    if ext in (".html", ".htm"):
        return "", text
    return text, None


class DocumentItem(QGraphicsItem):
    """A paper-like object with text/PDF pagination or scalable SVG rendering."""
    kind = "document"

    def __init__(self, path):
        super().__init__()
        self.uid, self.path, self.note = new_id(), path, None
        self.document_kind = "text"
        self.plain_text = ""
        self.error = ""
        self._valid = False
        self._page = 0
        self._page_total = 1
        self._width, self._height = 720.0, 900.0
        self._text = None
        self._pdf = None
        self._svg = None
        self._pixmap = QPixmap()
        self.setFlags(FLAGS)
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".pdf":
                self.document_kind = "pdf"
                self._load_pdf(path)
            elif ext in SVG_EXT:
                self.document_kind = "svg"
                self._load_svg(path)
            else:
                self._load_text(path)
        except Exception as exc:
            self.error = str(exc)
            self._valid = False

    def _load_text(self, path):
        plain, html = read_text_document(path)
        self._text = QTextDocument()
        self._text.setDocumentMargin(0)
        font = QFont("Segoe UI", 14)
        self._text.setDefaultFont(font)
        if html is not None:
            self._text.setHtml(html)
            self.plain_text = self._text.toPlainText()
        else:
            self._text.setPlainText(plain)
            self.plain_text = plain
        self._text.setTextWidth(self.content_rect().width())
        self._page_total = max(1, math.ceil(self._text.size().height()
                                            / self.content_rect().height()))
        self._valid = True

    def _load_pdf(self, path):
        if QPdfDocument is None:
            raise RuntimeError("PySide6.QtPdf is not installed in the portable Python")
        self._pdf = QPdfDocument()
        if ev(self._pdf.load(path)) != 0:
            raise RuntimeError("Qt could not open this PDF")
        self._page_total = self._pdf.pageCount()
        if self._page_total < 1:
            raise RuntimeError("The PDF has no pages")
        self._width = 900.0
        self._render_pdf_page()
        self._valid = not self._pixmap.isNull()

    def _load_svg(self, path):
        if QSvgRenderer is None:
            raise RuntimeError("PySide6.QtSvg is not installed in the portable Python")
        self._svg = QSvgRenderer(path)
        if not self._svg.isValid():
            raise RuntimeError("Qt could not open this SVG")
        size = self._svg.defaultSize()
        if not size.isValid() or size.width() <= 0 or size.height() <= 0:
            size = QSize(900, 700)
        self._width = 900.0
        content_width = self.content_rect().width()
        content_height = content_width * size.height() / size.width()
        self._height = content_height + 72.0
        self._valid = True

    def _render_pdf_page(self):
        size = self._pdf.pagePointSize(self._page)
        if size.isEmpty() or size.width() <= 0 or size.height() <= 0:
            size = QSizeF(595, 842)
        content_width = self._width - 36.0
        content_height = content_width * size.height() / size.width()
        self._height = content_height + 72.0
        image = self._pdf.render(self._page, QSize(round(content_width * 1.5),
                                                  round(content_height * 1.5)))
        self._pixmap = QPixmap.fromImage(image)

    def is_valid(self):
        return self._valid

    def toPlainText(self):
        return self.plain_text

    def page_count(self):
        return self._page_total

    def current_page(self):
        return self._page

    def set_page(self, page):
        page = max(0, min(int(page), self._page_total - 1))
        if page == self._page:
            return
        self.prepareGeometryChange()
        self._page = page
        if self.document_kind == "pdf":
            self._render_pdf_page()
        place_note(self)
        self.update()

    def change_page(self, delta):
        self.set_page(self._page + delta)

    def boundingRect(self):
        return QRectF(0, 0, self._width, self._height)

    def content_rect(self):
        return self.boundingRect().adjusted(18, 42, -18, -30)

    def shape(self):
        path = QPainterPath()
        path.addRect(self.boundingRect())
        return path

    def close(self):
        if self._pdf is not None:
            self._pdf.close()

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.fillRect(self.boundingRect(), QColor("#f4f4f2"))
        painter.setPen(QPen(QColor("#8b9096"), 1))
        painter.drawRect(self.boundingRect())
        painter.setPen(QColor("#454a50"))
        font = QFont("Segoe UI", 10)
        painter.setFont(font)
        header = QRectF(18, 8, self._width - 36, 26)
        painter.drawText(header, Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine,
                         os.path.basename(self.path))
        content = self.content_rect()
        painter.save()
        painter.setClipRect(content)
        if self.document_kind == "text":
            offset = self._page * content.height()
            painter.translate(content.left(), content.top() - offset)
            self._text.drawContents(painter, QRectF(0, offset, content.width(),
                                                    content.height()))
        elif self.document_kind == "pdf":
            painter.drawPixmap(content, self._pixmap, QRectF(self._pixmap.rect()))
        elif self.document_kind == "svg":
            self._svg.render(painter, content)
        painter.restore()
        if self._page_total > 1:
            painter.setPen(QColor("#666b70"))
            painter.drawText(QRectF(18, self._height - 25, self._width - 36, 18),
                             Qt.AlignCenter | Qt.TextSingleLine,
                             f"Page {self._page + 1} / {self._page_total}   •   PgUp / PgDown / Ctrl+Wheel")
        if self.isSelected():
            draw_selection(self, painter)
