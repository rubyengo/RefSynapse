"""Crop tools for the RefSynapse agent (non-destructive image crop).

Kept in a separate module so the core board_tools.py stays untouched:
register_crop_tools(tools) is called at the end of BoardTools._register().

All rectangles are expressed in SOURCE PIXELS relative to the top-left of the
original, uncropped image - never in scene coordinates. The numbers therefore
do not depend on the item's scale, rotation or position on the board.

The crop is non-destructive: the source file is never modified and
board_crop_reset always brings the full picture back.
"""

from PySide6.QtCore import QRectF

from ..item_tools import is_locked
from ..items import place_note
from .board_tools import ToolError

MIN_CROP_SIDE = 4.0

ANCHORS = {"center": (0.5, 0.5),
           "top": (0.5, 0.0), "bottom": (0.5, 1.0),
           "left": (0.0, 0.5), "right": (1.0, 0.5),
           "top_left": (0.0, 0.0), "top_right": (1.0, 0.0),
           "bottom_left": (0.0, 1.0), "bottom_right": (1.0, 1.0)}


def _aspect_ratio(value):
    """Accept '16:9', '16/9' or a plain number like 1.777."""
    text = str(value if value is not None else "").strip().replace(" ", "")
    if not text:
        raise ToolError("'aspect' must not be empty (use '1:1', '16:9' or 1.5)")
    try:
        if ":" in text:
            left, right = text.split(":", 1)
            ratio = float(left) / float(right)
        elif "/" in text:
            left, right = text.split("/", 1)
            ratio = float(left) / float(right)
        else:
            ratio = float(text)
    except (ValueError, ZeroDivisionError):
        raise ToolError("Cannot read aspect '%s' (use '16:9' or a number)" % value)
    if ratio <= 0 or ratio != ratio or ratio == float("inf"):
        raise ToolError("'aspect' must be a positive finite number")
    return ratio


def register_crop_tools(tools):
    """Add the crop tools to an existing BoardTools registry."""
    win = tools.win
    S = tools._schema
    uid = {"type": "string", "description": "Item uid from board_list_items"}

    # ---- helpers (GUI thread only) -----------------------------------
    def pick(a):
        item = tools._item(a["uid"])
        if not (hasattr(item, "set_crop") and hasattr(item, "full_rect_local")):
            raise ToolError("'%s' items cannot be cropped (images only)"
                            % getattr(item, "kind", "?"))
        return item

    def editable(a):
        item = pick(a)
        if is_locked(item):
            raise ToolError("Item is locked - unlock it with board_lock first")
        return item

    def outer_of(item):
        rect = QRectF(item.full_rect_local())
        if rect.width() < 1 or rect.height() < 1:
            raise ToolError("The source image has no usable size")
        return rect

    def current_of(item, outer):
        crop = getattr(item, "crop", None)
        rect = QRectF(crop) if crop is not None else QRectF(outer)
        rect = rect.intersected(outer)
        if rect.width() < 1 or rect.height() < 1:
            rect = QRectF(outer)
        return rect

    def report(item, outer, rect):
        full = (abs(rect.width() - outer.width()) < 0.5
                and abs(rect.height() - outer.height()) < 0.5)
        return {"uid": item.uid,
                "source": {"w": round(outer.width(), 1),
                           "h": round(outer.height(), 1)},
                "crop": {"x": round(rect.left() - outer.left(), 1),
                         "y": round(rect.top() - outer.top(), 1),
                         "w": round(rect.width(), 1),
                         "h": round(rect.height(), 1)},
                "cropped": not full}

    def touch_up(item):
        place_note(item)
        top = item.topLevelItem()
        if top is not item and hasattr(top, "refresh"):
            top.refresh()

    def apply(item, outer, rect):
        rect = QRectF(rect).normalized().intersected(outer)
        if rect.width() < MIN_CROP_SIDE or rect.height() < MIN_CROP_SIDE:
            raise ToolError("Crop would be smaller than %dx%d source pixels"
                            % (MIN_CROP_SIDE, MIN_CROP_SIDE))
        item.set_crop(rect, adjust_position=True)
        touch_up(item)
        return rect

    # ---- tools -------------------------------------------------------
    def t_crop_info(a):
        item = pick(a)
        outer = outer_of(item)
        return report(item, outer, current_of(item, outer))

    def t_crop(a):
        item = editable(a)
        outer = outer_of(item)
        base = current_of(item, outer)
        has_rect = any(k in a for k in ("x", "y", "w", "h"))
        has_inset = any(k in a for k in ("left", "right", "top", "bottom"))
        if has_rect and has_inset:
            raise ToolError("Use either x/y/w/h or left/right/top/bottom, not both")
        if has_rect:
            if not all(k in a for k in ("x", "y", "w", "h")):
                raise ToolError("A crop rectangle needs all four: x, y, w, h")
            rect = QRectF(outer.left() + float(a["x"]),
                          outer.top() + float(a["y"]),
                          float(a["w"]), float(a["h"]))
        elif has_inset:
            rect = QRectF(base)
            rect.setLeft(rect.left() + float(a.get("left", 0)))
            rect.setTop(rect.top() + float(a.get("top", 0)))
            rect.setRight(rect.right() - float(a.get("right", 0)))
            rect.setBottom(rect.bottom() - float(a.get("bottom", 0)))
        else:
            raise ToolError("Provide x/y/w/h (source pixels) or "
                            "left/right/top/bottom insets")
        rect = apply(item, outer, rect)
        result = report(item, outer, rect)
        result["item"] = tools._brief(item)
        return result

    def t_crop_aspect(a):
        item = editable(a)
        outer = outer_of(item)
        base = current_of(item, outer)
        ratio = _aspect_ratio(a.get("aspect"))
        anchor = str(a.get("anchor") or "center").strip().lower()
        if anchor not in ANCHORS:
            raise ToolError("Unknown anchor '%s' (use center, top, bottom_left, ...)"
                            % anchor)
        width = base.width()
        height = width / ratio
        if height > base.height():
            height = base.height()
            width = height * ratio
        fx, fy = ANCHORS[anchor]
        x = base.left() + (base.width() - width) * fx
        y = base.top() + (base.height() - height) * fy
        rect = apply(item, outer, QRectF(x, y, width, height))
        result = report(item, outer, rect)
        result["aspect"] = round(ratio, 6)
        result["anchor"] = anchor
        result["item"] = tools._brief(item)
        return result

    def t_crop_reset(a):
        item = editable(a)
        outer = outer_of(item)
        item.set_crop(None, adjust_position=True)
        touch_up(item)
        result = report(item, outer, QRectF(outer))
        result["item"] = tools._brief(item)
        return result

    # ---- registry ----------------------------------------------------
    tools._add("board_crop_info",
               "Source size and current crop of an image, in source pixels. "
               "Call this before cropping so you know the real dimensions.",
               S({"uid": uid}, ["uid"]), t_crop_info)

    tools._add("board_crop",
               "Crop an image non-destructively. Either x/y/w/h in source pixels "
               "measured from the top-left of the ORIGINAL image, or "
               "left/right/top/bottom insets trimmed from the CURRENT crop. "
               "The file on disk is never modified; board_crop_reset restores it.",
               S({"uid": uid,
                  "x": {"type": "number"}, "y": {"type": "number"},
                  "w": {"type": "number"}, "h": {"type": "number"},
                  "left": {"type": "number"}, "right": {"type": "number"},
                  "top": {"type": "number"}, "bottom": {"type": "number"}},
                 ["uid"]),
               t_crop, mutates=True)

    tools._add("board_crop_aspect",
               "Crop an image to an aspect ratio ('1:1', '16:9', '560:504' or a "
               "number), keeping the largest possible area of the current crop. "
               "Use it to make two pictures share the same proportions before "
               "normalizing their size.",
               S({"uid": uid,
                  "aspect": {"type": "string"},
                  "anchor": {"type": "string",
                             "enum": list(ANCHORS)}},
                 ["uid", "aspect"]),
               t_crop_aspect, mutates=True)

    tools._add("board_crop_reset",
               "Remove the crop and show the full original image again.",
               S({"uid": uid}, ["uid"]), t_crop_reset, mutates=True)


# ------------------------------------------------------------------ self-test
def _selftest():
    """python -m refsynapse.ai.board_crop_tools  (opens the real window)"""
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
    for name in ("board_crop_info", "board_crop",
                 "board_crop_aspect", "board_crop_reset"):
        print("  has", name, "->", name in tools.names())

    def run():
        image = QImage(800, 600, QImage.Format.Format_RGB32)
        painter = QPainter(image)
        painter.fillRect(image.rect(), QColor("#3a6ea5"))
        painter.fillRect(100, 80, 600, 440, QColor("#e07a5f"))
        painter.end()
        path = win.save_image(image)
        added = tools.call("board_add_image", {"path": path, "x": 0, "y": 0})
        print("add_image ->", added.get("ok"), added.get("error", ""))
        if not added.get("ok"):
            print("DONE - close the window to exit")
            return
        target = added["item"]["uid"]

        info = tools.call("board_crop_info", {"uid": target})
        print("crop_info ->", info)
        rect = tools.call("board_crop", {"uid": target,
                                         "x": 100, "y": 80, "w": 600, "h": 440})
        print("crop rect ->", rect.get("crop"), rect.get("error", ""))
        square = tools.call("board_crop_aspect", {"uid": target, "aspect": "1:1"})
        print("crop 1:1 ->", square.get("crop"), square.get("error", ""))
        wide = tools.call("board_crop_aspect", {"uid": target,
                                                "aspect": "16:9",
                                                "anchor": "top_left"})
        print("crop 16:9 ->", wide.get("crop"), wide.get("error", ""))
        back = tools.call("board_crop_reset", {"uid": target})
        print("crop_reset ->", back.get("crop"), "cropped:", back.get("cropped"))
        print("undo ->", tools.call("board_undo", {}))
        print("DONE - close the window to exit")

    QTimer.singleShot(800, run)
    sys.exit(app.exec())


if __name__ == "__main__":
    _selftest()