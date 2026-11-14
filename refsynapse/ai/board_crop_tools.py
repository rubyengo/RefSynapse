"""Crop tools for the RefSynapse agent (non-destructive image crop).

Kept in a separate module so the core board_tools.py stays untouched:
register_crop_tools(tools) is called at the end of BoardTools._register().

All rectangles are expressed in SOURCE PIXELS relative to the top-left of the
original, uncropped image - never in scene coordinates. The numbers therefore
do not depend on the item's scale, rotation or position on the board.

The crop is non-destructive: the source file is never modified and
board_crop_reset always brings the full picture back.

NO SILENT CLAMPING. A window that does not fit the source is REFUSED with the
exact numbers (source size, allowed x/y range, largest possible square) instead
of being quietly trimmed. Quiet trimming used to make the model believe it had
a 1:1 square while it actually got 5:4, and it then wasted many turns "fixing"
a crop that was never what it asked for. Pass clamp:true to accept a trim
deliberately; the answer always reports the rectangle really applied.

CENTRE MODE. Instead of computing a top-left corner, the model may pass
cx/cy as FRACTIONS of the source (0..1) plus w/h: the window is centred on that
point and shifted (never shrunk) if it would leave the picture. This removes
the classic "increasing x moves the subject left" confusion.
"""

from PySide6.QtCore import QRectF

from ..item_tools import is_locked
from ..items import place_note
from .board_tools import ToolError

MIN_CROP_SIDE = 4.0
FIT_TOLERANCE = 0.5          # pixels of float slack when checking the bounds

ANCHORS = {"center": (0.5, 0.5),
           "top": (0.5, 0.0), "bottom": (0.5, 1.0),
           "left": (0.0, 0.5), "right": (1.0, 0.5),
           "top_left": (0.0, 0.0), "top_right": (1.0, 0.0),
           "bottom_left": (0.0, 1.0), "bottom_right": (1.0, 1.0)}

GEOMETRY_HINT = ("x,y is the TOP-LEFT of the window in source pixels; increasing "
                 "x moves the window RIGHT, so the subject appears to move LEFT "
                 "in the frame. To centre a subject seen at fraction fx of the "
                 "width: x = fx*source_w - w/2 (same for y), or simply pass "
                 "cx/cy fractions with w/h and let the tool do the maths.")


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

    def number_arg(a, key):
        try:
            value = float(a[key])
        except (TypeError, ValueError):
            raise ToolError("'%s' must be a number" % key)
        if value != value or value in (float("inf"), float("-inf")):
            raise ToolError("'%s' must be a finite number" % key)
        return value

    def limits_of(outer, width=None, height=None):
        """What fits in this source, so the model never has to guess."""
        side = min(outer.width(), outer.height())
        data = {"source_w": round(outer.width(), 1),
                "source_h": round(outer.height(), 1),
                "max_square_side": round(side, 1)}
        if width is not None:
            data["max_x_for_w"] = round(max(0.0, outer.width() - width), 1)
        if height is not None:
            data["max_y_for_h"] = round(max(0.0, outer.height() - height), 1)
        return data

    def report(item, outer, rect):
        full = (abs(rect.width() - outer.width()) < 0.5
                and abs(rect.height() - outer.height()) < 0.5)
        ratio = rect.width() / rect.height() if rect.height() else 0.0
        return {"uid": item.uid,
                "source": {"w": round(outer.width(), 1),
                           "h": round(outer.height(), 1)},
                "crop": {"x": round(rect.left() - outer.left(), 1),
                         "y": round(rect.top() - outer.top(), 1),
                         "w": round(rect.width(), 1),
                         "h": round(rect.height(), 1)},
                "aspect": round(ratio, 4),
                "cropped": not full,
                "limits": limits_of(outer, rect.width(), rect.height())}

    def touch_up(item):
        place_note(item)
        top = item.topLevelItem()
        if top is not item and hasattr(top, "refresh"):
            top.refresh()

    def fit_error(outer, rect, width, height):
        """Explain an impossible window with numbers instead of trimming it."""
        problems = []
        if width > outer.width() + FIT_TOLERANCE:
            problems.append("w=%g is wider than the source (%g)"
                            % (width, outer.width()))
        if height > outer.height() + FIT_TOLERANCE:
            problems.append("h=%g is taller than the source (%g)"
                            % (height, outer.height()))
        x = rect.left() - outer.left()
        y = rect.top() - outer.top()
        if x < -FIT_TOLERANCE or y < -FIT_TOLERANCE:
            problems.append("x/y must not be negative (got x=%g, y=%g)" % (x, y))
        if x + width > outer.width() + FIT_TOLERANCE:
            problems.append("x+w=%g exceeds the source width %g (max x=%g)"
                            % (x + width, outer.width(),
                               max(0.0, outer.width() - width)))
        if y + height > outer.height() + FIT_TOLERANCE:
            problems.append("y+h=%g exceeds the source height %g (max y=%g)"
                            % (y + height, outer.height(),
                               max(0.0, outer.height() - height)))
        side = min(outer.width(), outer.height())
        return ToolError(
            "Crop window %gx%g at (%g, %g) does not fit the source %gx%g: %s. "
            "Largest possible square here is %gx%g. Nothing was changed - "
            "recompute from these numbers and call again (or pass clamp:true to "
            "accept a trimmed window, or cx/cy fractions to centre a window "
            "that is shifted into the picture automatically)."
            % (width, height, x, y, outer.width(), outer.height(),
               "; ".join(problems), side, side))

    def apply(item, outer, rect, clamp=False):
        rect = QRectF(rect).normalized()
        width, height = rect.width(), rect.height()
        if width < MIN_CROP_SIDE or height < MIN_CROP_SIDE:
            raise ToolError("Crop would be smaller than %gx%g source pixels"
                            % (MIN_CROP_SIDE, MIN_CROP_SIDE))
        clamped = False
        if not clamp:
            fits = (rect.left() >= outer.left() - FIT_TOLERANCE
                    and rect.top() >= outer.top() - FIT_TOLERANCE
                    and rect.right() <= outer.right() + FIT_TOLERANCE
                    and rect.bottom() <= outer.bottom() + FIT_TOLERANCE)
            if not fits:
                raise fit_error(outer, rect, width, height)
            rect = rect.intersected(outer)      # float slack only
        else:
            trimmed = QRectF(rect).intersected(outer)
            clamped = (abs(trimmed.width() - width) > FIT_TOLERANCE
                       or abs(trimmed.height() - height) > FIT_TOLERANCE
                       or abs(trimmed.left() - rect.left()) > FIT_TOLERANCE
                       or abs(trimmed.top() - rect.top()) > FIT_TOLERANCE)
            rect = trimmed
            if rect.width() < MIN_CROP_SIDE or rect.height() < MIN_CROP_SIDE:
                raise ToolError("Clamped crop would be smaller than %gx%g source "
                                "pixels - the window barely overlaps the picture"
                                % (MIN_CROP_SIDE, MIN_CROP_SIDE))
        item.set_crop(rect, adjust_position=True)
        touch_up(item)
        return rect, clamped

    def centre_rect(outer, a, width, height):
        """Window of w/h centred on cx/cy fractions, shifted to stay inside."""
        cx = number_arg(a, "cx")
        cy = number_arg(a, "cy")
        if not (0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0):
            raise ToolError("'cx' and 'cy' are fractions of the source between "
                            "0 and 1 (got cx=%g, cy=%g)" % (cx, cy))
        if width > outer.width() + FIT_TOLERANCE:
            raise ToolError("w=%g is wider than the source (%g); the largest "
                            "square here is %gx%g. Nothing was changed."
                            % (width, outer.width(),
                               min(outer.width(), outer.height()),
                               min(outer.width(), outer.height())))
        if height > outer.height() + FIT_TOLERANCE:
            raise ToolError("h=%g is taller than the source (%g); the largest "
                            "square here is %gx%g. Nothing was changed."
                            % (height, outer.height(),
                               min(outer.width(), outer.height()),
                               min(outer.width(), outer.height())))
        width = min(width, outer.width())
        height = min(height, outer.height())
        x = cx * outer.width() - width / 2.0
        y = cy * outer.height() - height / 2.0
        x = max(0.0, min(x, outer.width() - width))
        y = max(0.0, min(y, outer.height() - height))
        return QRectF(outer.left() + x, outer.top() + y, width, height)

    # ---- tools -------------------------------------------------------
    def t_crop_info(a):
        item = pick(a)
        outer = outer_of(item)
        result = report(item, outer, current_of(item, outer))
        result["hint"] = GEOMETRY_HINT
        return result

    def t_crop(a):
        item = editable(a)
        outer = outer_of(item)
        base = current_of(item, outer)
        clamp = bool(a.get("clamp"))
        has_rect = any(k in a for k in ("x", "y"))
        has_size = any(k in a for k in ("w", "h"))
        has_centre = any(k in a for k in ("cx", "cy"))
        has_inset = any(k in a for k in ("left", "right", "top", "bottom"))
        modes = [m for m in (has_rect, has_centre, has_inset) if m]
        if len(modes) > 1:
            raise ToolError("Use ONE of: x/y/w/h, cx/cy/w/h, or "
                            "left/right/top/bottom insets")
        shifted = False
        if has_centre:
            if not ("w" in a and "h" in a):
                raise ToolError("Centre mode needs 'cx', 'cy', 'w' and 'h'")
            width = number_arg(a, "w")
            height = number_arg(a, "h")
            if width <= 0 or height <= 0:
                raise ToolError("'w' and 'h' must be positive")
            rect = centre_rect(outer, a, width, height)
            wanted_x = number_arg(a, "cx") * outer.width() - width / 2.0
            shifted = abs((rect.left() - outer.left()) - wanted_x) > FIT_TOLERANCE
        elif has_rect or has_size:
            if not all(k in a for k in ("x", "y", "w", "h")):
                raise ToolError("A crop rectangle needs all four: x, y, w, h "
                                "(or use cx/cy with w/h to centre a window)")
            width = number_arg(a, "w")
            height = number_arg(a, "h")
            if width <= 0 or height <= 0:
                raise ToolError("'w' and 'h' must be positive")
            rect = QRectF(outer.left() + number_arg(a, "x"),
                          outer.top() + number_arg(a, "y"),
                          width, height)
        elif has_inset:
            rect = QRectF(base)
            if "left" in a:
                rect.setLeft(rect.left() + number_arg(a, "left"))
            if "top" in a:
                rect.setTop(rect.top() + number_arg(a, "top"))
            if "right" in a:
                rect.setRight(rect.right() - number_arg(a, "right"))
            if "bottom" in a:
                rect.setBottom(rect.bottom() - number_arg(a, "bottom"))
        else:
            raise ToolError("Provide x/y/w/h (source pixels), cx/cy/w/h "
                            "(fractions + size), or left/right/top/bottom insets")
        rect, clamped = apply(item, outer, rect, clamp)
        result = report(item, outer, rect)
        result["item"] = tools._brief(item)
        if clamped:
            result["clamped"] = True
            result["note"] = ("The window was trimmed to the picture bounds - the "
                              "applied crop above is what you really got, not what "
                              "you asked for.")
        if shifted:
            result["centre_shifted"] = True
            result["note"] = ("The window was shifted to stay inside the picture; "
                              "the applied crop above shows the real position.")
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
        rect, _clamped = apply(item, outer, QRectF(x, y, width, height), clamp=True)
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
               "Source size, current crop, aspect and fit limits of an image, in "
               "source pixels. Call this before cropping so you know the real "
               "dimensions and the largest window that fits.",
               S({"uid": uid}, ["uid"]), t_crop_info)

    tools._add("board_crop",
               "Crop an image non-destructively, in SOURCE PIXELS from the "
               "top-left of the ORIGINAL image. Three ways: (1) x/y/w/h; "
               "(2) cx/cy as fractions 0..1 of the source plus w/h - the window "
               "is centred there and shifted to stay inside (easiest for "
               "'put the subject in the middle'); (3) left/right/top/bottom "
               "insets trimmed from the CURRENT crop. A window that does not fit "
               "is REFUSED with the exact limits, never silently trimmed (pass "
               "clamp:true to accept a trim). The answer reports the crop really "
               "applied plus source size and limits. The file on disk is never "
               "modified; board_crop_reset restores it.",
               S({"uid": uid,
                  "x": {"type": "number"}, "y": {"type": "number"},
                  "w": {"type": "number"}, "h": {"type": "number"},
                  "cx": {"type": "number",
                         "description": "Centre X as a fraction 0..1 of the source; needs w/h"},
                  "cy": {"type": "number",
                         "description": "Centre Y as a fraction 0..1 of the source; needs w/h"},
                  "left": {"type": "number"}, "right": {"type": "number"},
                  "top": {"type": "number"}, "bottom": {"type": "number"},
                  "clamp": {"type": "boolean",
                            "description": "Accept a window trimmed to the picture bounds"}},
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
    import os
    import sys
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication
    from ..main_window import MainWindow
    from .board_tools import BoardTools
    from .sandbox import sandbox_root

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    tools = BoardTools(win)
    print("Registered tools:", len(tools.names()))
    for name in ("board_crop_info", "board_crop",
                 "board_crop_aspect", "board_crop_reset"):
        print("  has", name, "->", name in tools.names())
    sb = sandbox_root(win)
    print("agent sandbox ->", sb)

    def run():
        image = QImage(800, 600, QImage.Format.Format_RGB32)
        painter = QPainter(image)
        painter.fillRect(image.rect(), QColor("#3a6ea5"))
        painter.fillRect(100, 80, 600, 440, QColor("#e07a5f"))
        painter.end()
        # The read guard only accepts files inside the agent sandbox, so the
        # test picture is written there (win.save_image would land outside).
        path = os.path.join(sb, "selftest_crop_source.png")
        image.save(path)
        added = tools.call("board_add_image", {"path": path, "x": 0, "y": 0})
        print("add_image ->", added.get("ok"), added.get("error", ""))
        if not added.get("ok"):
            print("DONE - close the window to exit")
            return
        target = added["item"]["uid"]

        info = tools.call("board_crop_info", {"uid": target})
        print("crop_info ->", info.get("source"), info.get("crop"),
              "aspect:", info.get("aspect"))
        print("max square ->", (info.get("limits") or {}).get("max_square_side"),
              "(expected 600.0)")

        rect = tools.call("board_crop", {"uid": target,
                                         "x": 100, "y": 80, "w": 600, "h": 440})
        print("crop rect ->", rect.get("crop"), rect.get("error", ""))

        # --- the real bug: an impossible square must be REFUSED ----------
        before = tools.call("board_crop_info", {"uid": target}).get("crop")
        too_big = tools.call("board_crop", {"uid": target,
                                            "x": 100, "y": 0, "w": 800, "h": 800})
        print("oversize refused ->", too_big.get("ok"), "(expected False)")
        print("refusal has numbers ->",
              "600" in str(too_big.get("error", "")), "(expected True)")
        after = tools.call("board_crop_info", {"uid": target}).get("crop")
        print("nothing changed ->", before == after, "(expected True)")

        outside = tools.call("board_crop", {"uid": target,
                                            "x": 500, "y": 0, "w": 600, "h": 400})
        print("out of bounds refused ->", outside.get("ok"), "(expected False)")
        print("refusal gives max x ->", "max x=200" in str(outside.get("error", "")),
              "(expected True)")

        clamped = tools.call("board_crop", {"uid": target, "x": 500, "y": 0,
                                            "w": 600, "h": 400, "clamp": True})
        print("clamp accepted ->", clamped.get("ok"), "clamped:", clamped.get("clamped"),
              clamped.get("crop"))

        # --- centre mode: fractions instead of pixel guessing ------------
        tools.call("board_crop_reset", {"uid": target})
        centre = tools.call("board_crop", {"uid": target, "cx": 0.5, "cy": 0.5,
                                           "w": 400, "h": 400})
        print("centre crop ->", centre.get("crop"), "(expected x=200 y=100)")
        edge = tools.call("board_crop", {"uid": target, "cx": 0.02, "cy": 0.5,
                                         "w": 400, "h": 400})
        print("edge shifted ->", edge.get("crop"), "shifted:", edge.get("centre_shifted"),
              "(expected x=0)")
        bad_centre = tools.call("board_crop", {"uid": target, "cx": 0.5, "cy": 0.5,
                                               "w": 900, "h": 900})
        print("oversize centre refused ->", bad_centre.get("ok"), "(expected False)")
        mixed = tools.call("board_crop", {"uid": target, "x": 0, "y": 0,
                                          "cx": 0.5, "cy": 0.5, "w": 100, "h": 100})
        print("mixed modes refused ->", mixed.get("ok"), "(expected False)")

        # --- the old regression checks -----------------------------------
        tools.call("board_crop_reset", {"uid": target})
        tools.call("board_crop", {"uid": target, "x": 100, "y": 80,
                                  "w": 600, "h": 440})
        square = tools.call("board_crop_aspect", {"uid": target, "aspect": "1:1"})
        print("crop 1:1 ->", square.get("crop"), "aspect:", square.get("aspect"),
              square.get("error", ""))
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