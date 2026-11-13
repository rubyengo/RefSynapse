"""Clipboard pictures: original pixels for a single item, a scene render for the rest."""
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QImage

from .board import render_items
from .renderers import preview_image


def _cropped_source(item):
    """Visible crop of an image at the best available resolution (same math as export)."""
    pm = item.source_pixmap()
    if pm is None or pm.isNull():
        pm = item._display
    if pm is None or pm.isNull():
        return QImage()
    rect = item.crop or item.full_rect_local()
    sx = pm.width() / max(1, item.native_size.width())
    sy = pm.height() / max(1, item.native_size.height())
    region = QRectF(rect.x() * sx, rect.y() * sy, rect.width() * sx, rect.height() * sy)
    region = region.intersected(QRectF(0, 0, pm.width(), pm.height()))
    if region.isEmpty():
        return QImage()
    image = pm.copy(region.toAlignedRect()).toImage()
    if item.flipped:
        image = (image.flipped(Qt.Horizontal) if hasattr(image, "flipped")
                 else image.mirrored(True, False))
    return image


def item_image(win, item, scale=2.0):
    """One item as QImage: full-res crop / decoded frame / page, else a scene render."""
    if getattr(item, "kind", None) == "image":
        image = _cropped_source(item)
        if not image.isNull():
            return image
    image = preview_image(item, scale)
    if image is None or image.isNull():
        image = render_items(win, [item], scale)
    return image


def selection_image(win, items, scale=2.0):
    """A lone item keeps its pixels; several items are rendered as one picture."""
    if len(items) == 1:
        return item_image(win, items[0], scale)
    return render_items(win, items, scale)