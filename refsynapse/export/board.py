"""Render the board, a selection or a single item into a QImage."""
import math

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QImage, QPainter

MAX_SIDE = 10000
MAX_PIXELS = 40_000_000


def fit_scale(rect, scale):
    """Clamp the requested scale so an export can never exhaust memory."""
    width = rect.width() * scale
    height = rect.height() * scale
    if width <= 0 or height <= 0:
        return 0.0
    longest = max(width, height)
    if longest > MAX_SIDE:
        scale *= MAX_SIDE / longest
        width = rect.width() * scale
        height = rect.height() * scale
    pixels = width * height
    if pixels > MAX_PIXELS:
        scale *= math.sqrt(MAX_PIXELS / pixels)
    return scale


def render_region(scene, rect, scale=1.0, background=None, keep=None):
    """Draw a scene rectangle.

    Selection is cleared for the duration of the render so no handles or
    selection frames end up inside the exported picture. When ``keep`` is
    given, every other top-level item is hidden temporarily.
    """
    rect = QRectF(rect).normalized()
    if rect.isEmpty():
        return QImage()
    scale = fit_scale(rect, scale)
    if scale <= 0:
        return QImage()
    width = max(1, int(round(rect.width() * scale)))
    height = max(1, int(round(rect.height() * scale)))
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(background) if background else QColor(Qt.transparent))

    selected = scene.selectedItems()
    hidden = []
    scene.clearSelection()
    if keep is not None:
        keep = set(keep)
        for item in scene.items():
            if item.parentItem() is None and item not in keep and item.isVisible():
                item.hide()
                hidden.append(item)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        scene.render(painter, QRectF(0, 0, width, height), rect, Qt.IgnoreAspectRatio)
    finally:
        painter.end()
        for item in hidden:
            item.show()
        for item in selected:
            item.setSelected(True)
    return image


def items_rect(win, items):
    rect = QRectF()
    for item in items:
        rect = rect.united(win.full_rect(item))
    return rect


def render_items(win, items, scale=1.0, background=None, padding=24.0):
    items = [i for i in items if i is not None]
    if not items:
        return QImage()
    rect = items_rect(win, items).adjusted(-padding, -padding, padding, padding)
    return render_region(win.scene, rect, scale, background, keep=items)


def render_board(win, scale=1.0, background=None, padding=40.0):
    items = win.top_items()
    if not items:
        return QImage()
    rect = items_rect(win, items).adjusted(-padding, -padding, padding, padding)
    return render_region(win.scene, rect, scale, background)