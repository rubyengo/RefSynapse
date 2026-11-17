"""Serialization and in-place restoration of scene objects."""
import os

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QFont, QColor

from .config import rel, DEFAULT_TEXT_COLOR
from .items import (GroupItem, TextItem, FrameItem, StrokeItem, MissingItem,
                    set_note, note_text, place_note, smooth_stroke_path)
from .item_tools import set_locked


def dump_item(window, item, base):
    pos = item.pos()
    data = {"id": item.uid, "kind": item.kind, "note": note_text(item), "z": item.zValue(),
            "x": pos.x(), "y": pos.y(), "scale": item.scale(), "rotation": item.rotation()}
    if getattr(item, "note_style", None):
        data["note_style"] = dict(item.note_style)
    if getattr(item, "locked", False):
        data["locked"] = True
    if item.kind == "group":
        data.update(title=item.title,
                    children=[dump_item(window, c, base) for c in item.members()])
    elif item.kind == "text":
        data.update(text=item.toPlainText(), font=item.font().toString(),
                    color=item.defaultTextColor().name(), width=item.textWidth(),
                    height=item.frame_height)
    elif item.kind == "frame":
        data.update(width=item.rect().width(), height=item.rect().height(),
                    color=item.color, line_width=item.line_width)
    elif item.kind == "stroke":
        points = getattr(item, "points", None)
        if points is None:
            points = [QPointF(item.path().elementAt(i).x, item.path().elementAt(i).y)
                      for i in range(item.path().elementCount())]
        data.update(color=item.color, width=item.width,
                    points=[[point.x(), point.y()] for point in points])
    elif item.kind == "missing":
        data.update(path=rel(item.path, base), media_kind=item.media_kind,
                    width=item.rect().width(), height=item.rect().height())
    else:
        data["path"] = rel(item.path, base)
        data["width"] = item.boundingRect().width()
        data["height"] = item.boundingRect().height()
        if item.kind == "image":
            data["flip"] = item.flipped
            if item.crop is not None:
                data["crop"] = [item.crop.x(), item.crop.y(),
                                item.crop.width(), item.crop.height()]
        elif item.kind == "document":
            data["document_kind"] = item.document_kind
            data["page"] = item.current_page()
        elif item.kind == "video":
            data.update(position=max(0, item.current_position()),
                        volume=item.audio.volume(), muted=item.audio.isMuted())
            if item.poster_position >= 0:
                data["poster_position"] = item.poster_position
        elif item.kind == "model3d":
            data.update(yaw=item.yaw, pitch=item.pitch, model_zoom=item.model_zoom,
                        pan_x=item.pan_x, pan_y=item.pan_y, smooth=item.smooth)
    return data


def build_item(window, data, base, new_ids=False):
    kind = data.get("kind")
    if kind == "group":
        result = GroupItem(data.get("title", "Group"))
        window.objects.add(result)
        window.scene.addItem(result)
        for child_data in data.get("children", []):
            child = build_item(window, child_data, base, new_ids)
            if child:
                result.addToGroup(child)
        if not result.members():
            window.objects.discard(result)
            window.scene.removeItem(result)
            return None
        result.setPos(data.get("x", 0), data.get("y", 0))
        result.refresh()
    elif kind == "text":
        font = QFont()
        font.fromString(data.get("font", ""))
        result = TextItem(data.get("text", ""), data.get("color", DEFAULT_TEXT_COLOR),
                          font, data.get("height", 100))
        result.setTextWidth(data.get("width", 360))
        result.fit_font()
        window.objects.add(result)
        window.scene.addItem(result)
        result.setPos(data.get("x", 0), data.get("y", 0))
    elif kind == "frame":
        result = FrameItem(data.get("width", 320), data.get("height", 200),
                           data.get("color", "#ffffff"), data.get("line_width", 28))
        window.objects.add(result)
        window.scene.addItem(result)
        result.setPos(data.get("x", 0), data.get("y", 0))
    elif kind == "stroke":
        result = StrokeItem(data.get("color", "#ff5252"), data.get("width", 10))
        result.points = [QPointF(x, y) for x, y in data.get("points", [])]
        result.setPath(smooth_stroke_path(result.points))
        window.objects.add(result)
        window.scene.addItem(result)
        result.setPos(data.get("x", 0), data.get("y", 0))
    elif kind == "missing":
        result = MissingItem(data.get("path", ""), data.get("media_kind", "image"),
                             data.get("width", 480), data.get("height", 270))
        window.objects.add(result)
        window.scene.addItem(result)
        result.setPos(data.get("x", 0), data.get("y", 0))
    else:
        path = data.get("path", "")
        if base and not os.path.isabs(path):
            path = os.path.normpath(os.path.join(base, path))
        result = window.add_file(path, QPointF(data.get("x", 0), data.get("y", 0)))
        if result is None:
            window.missing.append(path)
            result = MissingItem(path, kind if kind in ("image", "video", "document", "model3d") else "image",
                                 data.get("width", 480), data.get("height", 270))
            window.objects.add(result)
            window.scene.addItem(result)
            result.setPos(data.get("x", 0), data.get("y", 0))
        if result.kind == "image":
            if data.get("flip"):
                result.set_flip(True)
            crop = data.get("crop")
            if isinstance(crop, list) and len(crop) == 4:
                result.set_crop(QRectF(*[float(v) for v in crop]))
        elif result.kind == "document":
            result.set_page(data.get("page", 0))
        elif result.kind == "video":
            result.restore_state(data)
        elif result.kind == "model3d":
            result.restore_state(data)
    result.setScale(data.get("scale", 1.0))
    result.setRotation(data.get("rotation", 0.0))
    if not new_ids:
        result.uid = data.get("id", result.uid)
    result.setZValue(data.get("z", 0))
    set_note(result, data.get("note", ""), data.get("note_style"))
    set_locked(result, data.get("locked", False))
    return result


def update_item(item, data):
    """Apply a non-structural history patch without recreating the object."""
    item.setPos(data.get("x", 0), data.get("y", 0))
    item.setScale(data.get("scale", 1.0))
    item.setRotation(data.get("rotation", 0.0))
    item.setZValue(data.get("z", 0))
    if item.kind == "group":
        item.title = data.get("title", item.title)
        item.refresh()
    elif item.kind == "text":
        font = QFont()
        font.fromString(data.get("font", item.font().toString()))
        item.setFont(font)
        item.setDefaultTextColor(QColor(data.get("color", "#ffffff")))
        item.setPlainText(data.get("text", ""))
        item.set_frame_size(data.get("width", 360), data.get("height", 100))
    elif item.kind == "frame":
        item.color = data.get("color", item.color)
        item.line_width = data.get("line_width", item.line_width)
        item.setRect(0, 0, data.get("width", 320), data.get("height", 200))
        pen = item.pen()
        pen.setColor(QColor(item.color))
        pen.setWidthF(item.line_width)
        item.setPen(pen)
    elif item.kind == "stroke":
        item.color = data.get("color", item.color)
        item.width = data.get("width", item.width)
        pen = item.pen()
        pen.setColor(QColor(item.color))
        pen.setWidthF(item.width)
        item.setPen(pen)
        item.points = [QPointF(x, y) for x, y in data.get("points", [])]
        item.setPath(smooth_stroke_path(item.points))
    elif item.kind == "image":
        item.set_flip(bool(data.get("flip", False)))
        crop = data.get("crop")
        item.set_crop(QRectF(*crop) if isinstance(crop, list) and len(crop) == 4 else None)
    elif item.kind == "document":
        item.set_page(data.get("page", 0))
    elif item.kind == "video":
        item.restore_state(data)
    elif item.kind == "model3d":
        item.restore_state(data)
    set_note(item, data.get("note", ""), data.get("note_style"))
    set_locked(item, data.get("locked", False))
    place_note(item)
    item.update()