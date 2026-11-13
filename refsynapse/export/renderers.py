"""Per-item renderers: documents, PDF pages, video frames and 3D views."""
from PySide6.QtCore import Qt, QRectF, QSize, QSizeF
from PySide6.QtGui import QColor, QImage, QPainter

PAPER = "#f4f4f2"
MAX_SIDE = 10000


def _new_image(width, height, background=None):
    width = max(1, int(round(width)))
    height = max(1, int(round(height)))
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(background) if background else QColor(Qt.transparent))
    return image


def _clamped(width, height, scale):
    width *= scale
    height *= scale
    longest = max(width, height)
    if longest > MAX_SIDE:
        factor = MAX_SIDE / longest
        width *= factor
        height *= factor
    return max(1, int(round(width))), max(1, int(round(height)))


def flatten(image, background="#ffffff"):
    """JPEG and BMP have no alpha channel; composite before saving."""
    if image is None or image.isNull() or not image.hasAlphaChannel():
        return image
    result = QImage(image.size(), QImage.Format_RGB32)
    result.fill(QColor(background))
    painter = QPainter(result)
    try:
        painter.drawImage(0, 0, image)
    finally:
        painter.end()
    return result


def save_image(image, path, quality=95):
    if image is None or image.isNull():
        return False
    lowered = path.lower()
    if lowered.endswith((".jpg", ".jpeg", ".bmp")):
        image = flatten(image)
        if lowered.endswith(".bmp"):
            return image.save(path)
        return image.save(path, quality=quality)
    if lowered.endswith(".webp"):
        return image.save(path, quality=quality)
    return image.save(path)


# ---------------------------------------------------------------- PDF
def pdf_document(item):
    return getattr(item, "_pdf", None)


def pdf_page_image(item, page, scale=2.0):
    pdf = pdf_document(item)
    if pdf is None:
        return QImage()
    page = max(0, min(int(page), pdf.pageCount() - 1))
    size = pdf.pagePointSize(page)
    if size.isEmpty() or size.width() <= 0 or size.height() <= 0:
        size = QSizeF(595, 842)
    width, height = _clamped(size.width(), size.height(), min(float(scale), 8.0))
    image = pdf.render(page, QSize(width, height))
    return image if image is not None else QImage()


# ---------------------------------------------------------------- SVG
def svg_image(item, scale=2.0):
    renderer = getattr(item, "_svg", None)
    if renderer is None or not renderer.isValid():
        return QImage()
    size = renderer.defaultSize()
    if not size.isValid() or size.width() <= 0 or size.height() <= 0:
        size = QSize(900, 700)
    width, height = _clamped(size.width(), size.height(), min(float(scale), 8.0))
    image = _new_image(width, height)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        renderer.render(painter, QRectF(0, 0, width, height))
    finally:
        painter.end()
    return image


# ------------------------------------------------------- text documents
def text_page_image(item, page=None, scale=2.0):
    doc = getattr(item, "_text", None)
    if doc is None:
        return QImage()
    content = item.content_rect()
    if content.width() <= 0 or content.height() <= 0:
        return QImage()
    page = item.current_page() if page is None else max(0, int(page))
    offset = page * content.height()
    width, height = _clamped(content.width(), content.height(), min(float(scale), 8.0))
    image = _new_image(width, height, PAPER)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.scale(width / content.width(), height / content.height())
        painter.translate(0, -offset)
        doc.drawContents(painter, QRectF(0, offset, content.width(), content.height()))
    finally:
        painter.end()
    return image


def document_page_image(item, page, scale=2.0):
    kind = getattr(item, "document_kind", "text")
    if kind == "pdf":
        return pdf_page_image(item, page, scale)
    if kind == "svg":
        return svg_image(item, scale)
    return text_page_image(item, page, scale)


def document_image(item, scale=2.0):
    return document_page_image(item, item.current_page(), scale)


# -------------------------------------------------------------- video
def video_image(item):
    image = item.current_frame_image()
    return image if image is not None else QImage()


# ----------------------------------------------------------------- 3D
def model_image(item):
    pixmap = getattr(item, "_pixmap", None)
    if pixmap is None or pixmap.isNull():
        return QImage()
    return pixmap.toImage()


def preview_image(item, scale=2.0):
    """Content-only render, or None when the item must be drawn by the scene."""
    kind = getattr(item, "kind", None)
    if kind == "document":
        return document_image(item, scale)
    if kind == "video":
        return video_image(item)
    if kind == "model3d":
        return model_image(item)
    return None