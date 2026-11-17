"""Crop tools for the RefSynapse agent (non-destructive image crop).

Kept in a separate module so the core board_tools.py stays untouched:
register_crop_tools(tools) is called at the end of BoardTools._register().

All rectangles are expressed in SOURCE PIXELS relative to the top-left of the
original, uncropped image - never in scene coordinates. The numbers therefore
do not depend on the item's scale, rotation or position on the board.

The crop is non-destructive: the source file is never modified and
board_crop_reset always brings the full picture back.

NO SILENT CLAMPING. A window that does not fit the source is REFUSED with the
exact numbers and a READY-TO-SEND corrected call, instead of being quietly
trimmed. Pass clamp:true to accept a trim deliberately; the answer always
reports the rectangle really applied.

CENTRE MODE. cx/cy are FRACTIONS of the source (0..1). With w/h the window is
centred on that point; WITHOUT w/h the CURRENT window size is kept and only its
centre moves (a pure shift). w/h > 1 are source pixels, 0 < w/h <= 1 are
fractions of the source - so one call can stay in one unit system. The window
is shifted (never shrunk) to stay inside the picture.

INSET MODE. left/right/top/bottom trim the CURRENT crop. A value > 1 is source
pixels; 0 < value <= 1 is a FRACTION of the current crop's width (left/right)
or height (top/bottom), so {"top": 0.2} trims 20% off the top. 0 means "no
change on this side". An inset that would trim less than half a pixel, or a
call that changes nothing, is REFUSED - a silent no-op used to make the model
believe it had cropped when it had not.

SUBJECT MODE (the easy path for "centre the animal, same size everywhere"):
1. board_crop_view  -> full ORIGINAL image with a labelled 0..1 grid and the
   current crop drawn in red. The model reads coordinates instead of guessing.
2. board_crop_subject -> the model passes only the subject bounding box (as
   grid fractions), a target fill and an aspect ratio. The TOOL computes the
   window: the subject is guaranteed to be fully inside, centred, with air
   around it, and the answer reports the fill actually achieved (subject.fill_w).
   Equal fill_w + equal aspect + normalize_height = equally large subjects.
"""

import base64
import json
import os

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen

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

GEOMETRY_HINT = ("Easiest path for 'centre the subject': board_crop_view (grid "
                 "over the FULL original) then board_crop_subject (box + fill). "
                 "Manual modes: x/y/w/h in source pixels (x,y = TOP-LEFT; "
                 "increasing x moves the window RIGHT, so the subject appears "
                 "to move LEFT), or cx/cy fractions 0..1 with optional w/h "
                 "(>1 = pixels, <=1 = fraction of source; omit w/h to keep the "
                 "current window size and just shift its centre). Insets "
                 "left/right/top/bottom: >1 = pixels, 0<v<=1 = fraction of the "
                 "current crop (0.2 = 20%).")

SUBJECT_HINT = ("The grid shows the FULL ORIGINAL image; fractions 0..1 run "
                "left->right and top->bottom and are ALWAYS relative to the "
                "ORIGINAL, never to the current crop. Read the subject's "
                "bounding box off the grid, then call board_crop_subject with "
                "{\"uid\": ..., \"box\": [x0, y0, x1, y1], \"fill\": 0.6, "
                "\"aspect\": \"3:2\"}. Use the SAME fill and aspect for every "
                "image so subjects look equally large. The red rectangle is "
                "the current crop. After cropping, trust the reported "
                "subject.fill_w (the achieved share of the FRAME) - do not "
                "re-estimate it from this grid.")

EQUALIZE_HINT = ("Equal subjects at equal frame height = equal subject.fill_w "
                 "+ equal aspect, then board_arrange normalize_height + row. "
                 "If one subject still looks smaller, raise ITS fill (try "
                 "0.8-0.9) and call board_crop_subject again. Do NOT fix "
                 "subject size with board_transform scale followed by "
                 "normalize_height: normalize resets every scale and undoes "
                 "the fix.")


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


def _subject_window(src_w, src_h, box, fill, ratio, margin):
    """Pure maths: window that keeps the subject WHOLE, centred, with air.

    box = (x0, y0, x1, y1) fractions of the source. Returns (x, y, W, H, info)
    in source pixels. Guarantees (when the subject box is truthful):
      * the window never leaves the source;
      * the subject box is fully inside the window (W >= subject_w etc., and
        the centred window is only ever shifted TOWARD the subject's side);
      * the requested aspect/fill/margin are kept whenever the source allows;
        whatever had to give way is reported in info, never hidden.
    """
    x0, y0, x1, y1 = box
    sw = (x1 - x0) * src_w
    sh = (y1 - y0) * src_h
    if sw < 2.0 or sh < 2.0:
        raise ToolError("The subject box spans only %gx%g source pixels - too "
                        "small to frame. Re-read the grid with board_crop_view "
                        "and pass a box that covers the whole subject."
                        % (sw, sh))
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    usable = 1.0 - 2.0 * margin
    notes = []
    W = sw / fill
    H = W / ratio
    if sw > W * usable:                      # fill too ambitious for the margin
        W = sw / usable
        H = W / ratio
    if sh > H * usable:                      # subject taller than the frame
        k = sh / (H * usable)
        W *= k
        H *= k
    if W > src_w:
        W = src_w
        H = W / ratio
        notes.append("window limited by the source width")
    if H > src_h:
        H = src_h
        W = H * ratio
        notes.append("window limited by the source height")
    # containment beats the aspect ratio: the subject must fit whole
    if W < sw:
        W = sw
        notes.append("aspect widened so the subject fits whole")
    if H < sh:
        H = sh
        notes.append("aspect heightened so the subject fits whole")
    W = min(W, src_w)
    H = min(H, src_h)
    if W < MIN_CROP_SIDE or H < MIN_CROP_SIDE:
        raise ToolError("The computed window %gx%g is below the %gx%g minimum"
                        % (W, H, MIN_CROP_SIDE, MIN_CROP_SIDE))
    x = max(0.0, min(cx * src_w - W / 2.0, src_w - W))
    y = max(0.0, min(cy * src_h - H / 2.0, src_h - H))
    info = {"fill_w": sw / W, "fill_h": sh / H, "aspect": W / H,
            "notes": notes,
            "margin_ok": sw <= W * usable + FIT_TOLERANCE
                         and sh <= H * usable + FIT_TOLERANCE}
    return x, y, W, H, info


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

    def example_call(item_uid, **kw):
        """A ready-to-send corrected call - models copy these reliably."""
        args = {"uid": item_uid}
        args.update(kw)
        return "board_crop " + json.dumps(args, ensure_ascii=False)

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

    def fit_error(item, outer, rect, width, height):
        """Explain an impossible window with numbers AND a corrected call."""
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
        fx = max(0.0, min(1.0, (x + width / 2.0) / outer.width()))
        fy = max(0.0, min(1.0, (y + height / 2.0) / outer.height()))
        fix = example_call(item.uid, cx=round(fx, 3), cy=round(fy, 3),
                           w=round(min(width, outer.width()), 1),
                           h=round(min(height, outer.height()), 1))
        return ToolError(
            "Crop window %gx%g at (%g, %g) does not fit the source %gx%g: %s. "
            "Largest possible square here is %gx%g. Nothing was changed. "
            "Ready-to-send fix (centre mode shifts the window inside "
            "automatically): %s - or pass clamp:true to accept a trimmed "
            "window. For 'subject centred and fully visible' prefer "
            "board_crop_view then board_crop_subject."
            % (width, height, x, y, outer.width(), outer.height(),
               "; ".join(problems), side, side, fix))

    def apply(item, outer, rect, clamp=False):
        rect = QRectF(rect).normalized()
        width, height = rect.width(), rect.height()
        if width < MIN_CROP_SIDE or height < MIN_CROP_SIDE:
            raise ToolError("Crop would be smaller than %gx%g source pixels "
                            "(if you meant fractions of the source, use centre "
                            "mode cx/cy with w/h <= 1)"
                            % (MIN_CROP_SIDE, MIN_CROP_SIDE))
        clamped = False
        if not clamp:
            fits = (rect.left() >= outer.left() - FIT_TOLERANCE
                    and rect.top() >= outer.top() - FIT_TOLERANCE
                    and rect.right() <= outer.right() + FIT_TOLERANCE
                    and rect.bottom() <= outer.bottom() + FIT_TOLERANCE)
            if not fits:
                raise fit_error(item, outer, rect, width, height)
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

    def centre_rect(outer, cx, cy, width, height):
        """Window of w/h centred on cx/cy fractions, shifted to stay inside."""
        width = min(width, outer.width())
        height = min(height, outer.height())
        x = cx * outer.width() - width / 2.0
        y = cy * outer.height() - height / 2.0
        x = max(0.0, min(x, outer.width() - width))
        y = max(0.0, min(y, outer.height() - height))
        return QRectF(outer.left() + x, outer.top() + y, width, height)

    def inset_pixels(a, key, span, horizontal):
        """Resolve one inset to pixels; refuse silent no-ops.

        >1 = source pixels; 0<v<=1 = fraction of the current crop's span;
        0 = no change on this side. A positive inset under half a pixel is
        refused so the model never believes it trimmed when it did not.
        """
        value = number_arg(a, key)
        if value < 0:
            raise ToolError("inset '%s' must not be negative (got %g)" % (key, value))
        if value == 0:
            return 0.0, False
        is_fraction = 0.0 < value <= 1.0
        px = value * span if is_fraction else value
        if px < FIT_TOLERANCE:
            raise ToolError("inset '%s'=%g would trim only %.2f px - nothing "
                            "would change. For a share of the current crop use "
                            "a fraction 0..1 (0.2 = 20%%); for pixels use a "
                            "value > 1. Nothing was changed." % (key, value, px))
        if px >= span - FIT_TOLERANCE:
            raise ToolError("inset '%s'=%g removes the entire %s of the crop. "
                            "Nothing was changed."
                            % (key, value, "width" if horizontal else "height"))
        return px, is_fraction

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
            raise ToolError("Use ONE of: x/y/w/h, cx/cy[/w/h], or "
                            "left/right/top/bottom insets. Ready-to-send "
                            "example: %s"
                            % example_call(item.uid, cx=0.5, cy=0.5,
                                           w=round(outer.width() / 2, 1),
                                           h=round(outer.height() / 2, 1)))
        shifted = False
        kept_size = False
        inset_fraction = False
        insets_px = {}
        if has_centre:
            base_cx = ((base.left() - outer.left()) + base.width() / 2.0) / outer.width()
            base_cy = ((base.top() - outer.top()) + base.height() / 2.0) / outer.height()
            cx = number_arg(a, "cx") if "cx" in a else base_cx
            cy = number_arg(a, "cy") if "cy" in a else base_cy
            if not (0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0):
                raise ToolError("'cx' and 'cy' are fractions of the source "
                                "between 0 and 1 (got cx=%g, cy=%g). Example: %s"
                                % (cx, cy, example_call(item.uid, cx=0.5, cy=0.5)))
            if has_size:
                if not ("w" in a and "h" in a):
                    raise ToolError("Centre mode needs BOTH 'w' and 'h', or "
                                    "neither (then the current window size is "
                                    "kept and only its centre moves). "
                                    "Ready-to-send example: %s"
                                    % example_call(item.uid, cx=round(cx, 3),
                                                   cy=round(cy, 3),
                                                   w=round(base.width(), 1),
                                                   h=round(base.height(), 1)))
                width = number_arg(a, "w")
                height = number_arg(a, "h")
                if width <= 0 or height <= 0:
                    raise ToolError("'w' and 'h' must be positive")
                if width <= 1.0:
                    width *= outer.width()
                if height <= 1.0:
                    height *= outer.height()
                if width > outer.width() + FIT_TOLERANCE:
                    raise ToolError("w=%g is wider than the source (%g). "
                                    "Ready-to-send fix: %s (w/h <= 1 are "
                                    "fractions of the source, > 1 are pixels)."
                                    % (width, outer.width(),
                                       example_call(item.uid, cx=round(cx, 3),
                                                    cy=round(cy, 3),
                                                    w=round(outer.width(), 1),
                                                    h=round(min(height, outer.height()), 1))))
                if height > outer.height() + FIT_TOLERANCE:
                    raise ToolError("h=%g is taller than the source (%g). "
                                    "Ready-to-send fix: %s (w/h <= 1 are "
                                    "fractions of the source, > 1 are pixels)."
                                    % (height, outer.height(),
                                       example_call(item.uid, cx=round(cx, 3),
                                                    cy=round(cy, 3),
                                                    w=round(min(width, outer.width()), 1),
                                                    h=round(outer.height(), 1))))
            else:
                width = base.width()
                height = base.height()
                kept_size = True
            rect = centre_rect(outer, cx, cy, width, height)
            wanted_x = cx * outer.width() - width / 2.0
            wanted_y = cy * outer.height() - height / 2.0
            shifted = (abs((rect.left() - outer.left()) - wanted_x) > FIT_TOLERANCE
                       or abs((rect.top() - outer.top()) - wanted_y) > FIT_TOLERANCE)
        elif has_rect or has_size:
            if not all(k in a for k in ("x", "y", "w", "h")):
                raise ToolError("A crop rectangle needs all four: x, y, w, h "
                                "(source pixels). Ready-to-send example: %s - "
                                "or use cx/cy fractions to centre a window."
                                % example_call(item.uid, x=0, y=0,
                                               w=round(outer.width() / 2, 1),
                                               h=round(outer.height() / 2, 1)))
            width = number_arg(a, "w")
            height = number_arg(a, "h")
            if width <= 0 or height <= 0:
                raise ToolError("'w' and 'h' must be positive")
            if width <= 1.0 or height <= 1.0:
                raise ToolError("With x/y, 'w' and 'h' are SOURCE PIXELS and "
                                "must be > 1. For fractions use cx/cy with "
                                "w/h <= 1. Ready-to-send example: %s"
                                % example_call(item.uid, cx=0.5, cy=0.5,
                                               w=0.5, h=0.5))
            rect = QRectF(outer.left() + number_arg(a, "x"),
                          outer.top() + number_arg(a, "y"),
                          width, height)
        elif has_inset:
            rect = QRectF(base)
            for key, horizontal in (("left", True), ("right", True),
                                    ("top", False), ("bottom", False)):
                if key not in a:
                    continue
                span = base.width() if horizontal else base.height()
                px, is_fraction = inset_pixels(a, key, span, horizontal)
                inset_fraction = inset_fraction or is_fraction
                insets_px[key] = round(px, 1)
                if px == 0:
                    continue
                if key == "left":
                    rect.setLeft(rect.left() + px)
                elif key == "right":
                    rect.setRight(rect.right() - px)
                elif key == "top":
                    rect.setTop(rect.top() + px)
                else:
                    rect.setBottom(rect.bottom() - px)
            unchanged = (abs(rect.left() - base.left()) < FIT_TOLERANCE
                         and abs(rect.top() - base.top()) < FIT_TOLERANCE
                         and abs(rect.right() - base.right()) < FIT_TOLERANCE
                         and abs(rect.bottom() - base.bottom()) < FIT_TOLERANCE)
            if unchanged:
                raise ToolError("These insets change nothing (all zero or too "
                                "small). To trim a share of the current crop "
                                "use fractions 0..1, e.g. {\"top\": 0.2} = 20%% "
                                "off the top; for pixels use values > 1. "
                                "Nothing was changed.")
        else:
            raise ToolError("Provide x/y/w/h (source pixels), cx/cy with "
                            "optional w/h (fractions; omit w/h to keep the "
                            "current size), or left/right/top/bottom insets. "
                            "Ready-to-send example: %s"
                            % example_call(item.uid, cx=0.5, cy=0.5))
        rect, clamped = apply(item, outer, rect, clamp)
        result = report(item, outer, rect)
        result["item"] = tools._brief(item)
        notes = []
        if clamped:
            result["clamped"] = True
            notes.append("The window was trimmed to the picture bounds - the "
                         "applied crop above is what you really got, not what "
                         "you asked for.")
        if shifted:
            result["centre_shifted"] = True
            notes.append("The window was shifted to stay inside the picture; "
                         "the applied crop above shows the real position.")
        if kept_size:
            result["kept_window_size"] = True
            notes.append("No w/h given - kept the current window size %gx%g "
                         "and only moved its centre."
                         % (round(rect.width(), 1), round(rect.height(), 1)))
        if has_inset:
            result["insets_px"] = insets_px
            if inset_fraction:
                notes.append("Insets <= 1 were read as fractions of the "
                             "current crop; insets_px shows the real pixels "
                             "trimmed per side.")
        if notes:
            result["note"] = " ".join(notes)
        return result

    def t_crop_subject(a):
        item = editable(a)
        outer = outer_of(item)
        box = a.get("box")
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            raise ToolError("'box' must be [x0, y0, x1, y1] fractions of the "
                            "source, read off the board_crop_view grid. "
                            "Example: {\"uid\": \"%s\", \"box\": [0.2, 0.1, "
                            "0.8, 0.9], \"fill\": 0.6, \"aspect\": \"3:2\"}"
                            % item.uid)
        try:
            vals = [float(v) for v in box]
        except (TypeError, ValueError):
            raise ToolError("'box' values must be numbers (fractions 0..1)")
        if any(v != v or v < 0.0 or v > 1.0 for v in vals):
            raise ToolError("'box' fractions must be between 0 and 1 (got %s)"
                            % vals)
        x0, y0, x1, y1 = vals
        if x1 <= x0 or y1 <= y0:
            raise ToolError("'box' must be [x0, y0, x1, y1] with x1 > x0 and "
                            "y1 > y0 (got %s)" % vals)
        fill = number_arg(a, "fill") if a.get("fill") is not None else 0.6
        if not (0.05 <= fill <= 0.95):
            raise ToolError("'fill' must be between 0.05 and 0.95 (got %g)" % fill)
        margin = number_arg(a, "margin") if a.get("margin") is not None else 0.08
        if not (0.0 <= margin <= 0.4):
            raise ToolError("'margin' must be between 0 and 0.4 (got %g)" % margin)
        ratio = _aspect_ratio(a.get("aspect") if a.get("aspect") is not None else "3:2")
        x, y, W, H, info = _subject_window(outer.width(), outer.height(),
                                           vals, fill, ratio, margin)
        rect = QRectF(outer.left() + x, outer.top() + y, W, H)
        rect, _clamped = apply(item, outer, rect, clamp=False)
        result = report(item, outer, rect)
        result["subject"] = {"box": [round(v, 4) for v in vals],
                             "requested_fill": round(fill, 3),
                             "fill_w": round(info["fill_w"], 3),
                             "fill_h": round(info["fill_h"], 3),
                             "fully_inside": True,
                             "margin_ok": bool(info["margin_ok"])}
        if info["notes"] or not info["margin_ok"]:
            result["note"] = ("The exact fill/aspect/margin could not all be "
                              "met (%s); the subject is still FULLY inside the "
                              "frame. Achieved fill_w=%.2f, fill_h=%.2f. To "
                              "match the other frames you may raise fill and "
                              "call again."
                              % ("; ".join(info["notes"]) or "less air than requested",
                                 info["fill_w"], info["fill_h"]))
        result["hint"] = ("Trust subject.fill_w (the achieved share of the "
                          "FRAME), not your eye. " + EQUALIZE_HINT)
        result["item"] = tools._brief(item)
        return result

    def t_crop_view(a):
        item = pick(a)
        outer = outer_of(item)
        path = getattr(item, "path", None)
        if not isinstance(path, str) or not os.path.isfile(path):
            raise ToolError("Cannot read the source file of '%s' - "
                            "board_crop_view needs an image on disk" % item.uid)
        image = QImage(path)
        if image.isNull():
            raise ToolError("The source file is not a readable image: %s" % path)
        max_side = max(256, min(1600, int(number_arg(a, "max_size"))
                                if a.get("max_size") is not None else 900))
        iw, ih = image.width(), image.height()
        scale = min(1.0, max_side / float(max(iw, ih)))
        w = max(1, int(round(iw * scale)))
        h = max(1, int(round(ih * scale)))
        scaled = image.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        w, h = scaled.width(), scaled.height()
        pad_l, pad_t, pad_r, pad_b = 44, 26, 12, 12
        canvas = QImage(w + pad_l + pad_r, h + pad_t + pad_b,
                        QImage.Format.Format_ARGB32)
        canvas.fill(QColor("#1e1e1e"))
        painter = QPainter(canvas)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.drawImage(pad_l, pad_t, scaled)
            painter.setFont(QFont("Segoe UI", 8))
            for i in range(11):
                f = i / 10.0
                gx = int(round(pad_l + f * w))
                gy = int(round(pad_t + f * h))
                major = (i == 5)
                painter.setPen(QPen(QColor(255, 255, 255, 170 if major else 80),
                                    1.6 if major else 1.0))
                painter.drawLine(gx, pad_t, gx, pad_t + h)
                painter.drawLine(pad_l, gy, pad_l + w, gy)
            painter.setPen(QPen(QColor("#ffd54f")))
            for i in range(11):
                f = i / 10.0
                painter.drawText(int(round(pad_l + f * w)) - 9, pad_t - 8, "%.1f" % f)
                painter.drawText(6, int(round(pad_t + f * h)) + 4, "%.1f" % f)
            cur = current_of(item, outer)
            fx0 = (cur.left() - outer.left()) / outer.width()
            fy0 = (cur.top() - outer.top()) / outer.height()
            fx1 = fx0 + cur.width() / outer.width()
            fy1 = fy0 + cur.height() / outer.height()
            cropped = not (abs(cur.width() - outer.width()) < 0.5
                           and abs(cur.height() - outer.height()) < 0.5)
            if cropped:
                painter.setPen(QPen(QColor("#ff5252"), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(QRectF(pad_l + fx0 * w, pad_t + fy0 * h,
                                        (fx1 - fx0) * w, (fy1 - fy0) * h))
                painter.drawText(int(round(pad_l + fx0 * w)) + 4,
                                 int(round(pad_t + fy0 * h)) + 14, "current crop")
        finally:
            painter.end()
        ba = QByteArray()
        buffer = QBuffer(ba)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not canvas.save(buffer, "PNG"):
            raise ToolError("PNG encoding failed")
        buffer.close()
        data = base64.b64encode(bytes(ba)).decode("ascii")
        result = {"uid": item.uid,
                  "source": {"w": iw, "h": ih},
                  "grid_step": 0.1,
                  "crop_fractions": ({"x0": round(fx0, 3), "y0": round(fy0, 3),
                                      "x1": round(fx1, 3), "y1": round(fy1, 3)}
                                     if cropped else None),
                  "hint": SUBJECT_HINT,
                  "message": "Preview image attached in the next message.",
                  "_image": {"data": data, "mime": "image/png"}}
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
               "source pixels. Call this before manual cropping so you know the "
               "real dimensions and the largest window that fits.",
               S({"uid": uid}, ["uid"]), t_crop_info)
    tools._add("board_crop_view",
               "Show the FULL ORIGINAL image with a labelled 0..1 grid (step "
               "0.1) and the current crop drawn in red. Use this BEFORE "
               "board_crop_subject: read the subject's bounding box off the "
               "grid as fractions. Always shows the original, even when the "
               "item is cropped - so you never lose the full picture. Grid "
               "fractions are of the ORIGINAL, not the current frame.",
               S({"uid": uid,
                  "max_size": {"type": "integer",
                               "description": "Longest side in px (256..1600, default 900)"}},
                 ["uid"]),
               t_crop_view)
    tools._add("board_crop_subject",
               "Crop so a subject is FULLY inside, centred, and fills a chosen "
               "share of the frame - the tool computes the window, you only "
               "read the subject box off the board_crop_view grid. 'box' = "
               "[x0, y0, x1, y1] fractions of the ORIGINAL image. 'fill' = "
               "target share of the frame WIDTH the subject should span. "
               "Equal fill + equal aspect for every image, then normalize_height "
               "+ row, gives equally large subjects at equal frame height. If "
               "one subject still looks smaller, raise ITS fill (0.8-0.9) and "
               "call again - do NOT fix it with board_transform scale + "
               "normalize_height (normalize resets scales). The answer reports "
               "the achieved subject.fill_w; trust it over your eye. "
               "Non-destructive; board_crop_reset restores.",
               S({"uid": uid,
                  "box": {"type": "array", "items": {"type": "number"},
                          "description": "[x0, y0, x1, y1] fractions 0..1 of the original"},
                  "fill": {"type": "number",
                           "description": "Target subject width as a share of the frame (0.05..0.95, default 0.6)"},
                  "aspect": {"type": "string",
                             "description": "Frame aspect, e.g. '3:2', '1:1', '16:9' (default '3:2')"},
                  "margin": {"type": "number",
                             "description": "Minimum air around the subject per side (0..0.4, default 0.08)"}},
                 ["uid", "box"]),
               t_crop_subject, mutates=True)
    tools._add("board_crop",
               "Crop an image non-destructively, in SOURCE PIXELS from the "
               "top-left of the ORIGINAL image. Three ways: (1) x/y/w/h in "
               "pixels; (2) cx/cy as fractions 0..1 of the source - with w/h "
               "(>1 = pixels, <=1 = fraction of source) the window is centred "
               "there, WITHOUT w/h the current window size is kept and only "
               "its centre moves; (3) left/right/top/bottom insets trimmed "
               "from the CURRENT crop (>1 = pixels, 0<v<=1 = fraction of the "
               "current crop, 0 = no change). A window that does not fit, or "
               "an inset that changes nothing, is REFUSED with the exact "
               "limits and a ready-to-send corrected call (pass clamp:true to "
               "accept a trim). For 'centre the subject, same size everywhere' "
               "prefer board_crop_view + board_crop_subject. The file on disk "
               "is never modified.",
               S({"uid": uid,
                  "x": {"type": "number"}, "y": {"type": "number"},
                  "w": {"type": "number",
                         "description": "Window width: >1 source pixels; 0<w<=1 fraction of source (centre mode)"},
                  "h": {"type": "number",
                         "description": "Window height: >1 source pixels; 0<h<=1 fraction of source (centre mode)"},
                  "cx": {"type": "number",
                         "description": "Centre X as a fraction 0..1 of the source; without w/h the current size is kept"},
                  "cy": {"type": "number",
                         "description": "Centre Y as a fraction 0..1 of the source; without w/h the current size is kept"},
                  "left": {"type": "number",
                           "description": "Trim from left: >1 pixels, 0<v<=1 fraction of current width"},
                  "right": {"type": "number",
                            "description": "Trim from right: >1 pixels, 0<v<=1 fraction of current width"},
                  "top": {"type": "number",
                          "description": "Trim from top: >1 pixels, 0<v<=1 fraction of current height"},
                  "bottom": {"type": "number",
                             "description": "Trim from bottom: >1 pixels, 0<v<=1 fraction of current height"},
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
    for name in ("board_crop_info", "board_crop_view", "board_crop_subject",
                 "board_crop", "board_crop_aspect", "board_crop_reset"):
        print("  has", name, "->", name in tools.names())
    sb = sandbox_root(win)
    print("agent sandbox ->", sb)

    def run():
        image = QImage(800, 600, QImage.Format.Format_RGB32)
        painter = QPainter(image)
        painter.fillRect(image.rect(), QColor("#3a6ea5"))
        painter.fillRect(100, 80, 600, 440, QColor("#e07a5f"))
        painter.end()
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
        before = tools.call("board_crop_info", {"uid": target}).get("crop")
        too_big = tools.call("board_crop", {"uid": target,
                                            "x": 100, "y": 0, "w": 800, "h": 800})
        print("oversize refused ->", too_big.get("ok"), "(expected False)")
        print("refusal has numbers ->",
              "600" in str(too_big.get("error", "")), "(expected True)")
        print("refusal has ready fix ->",
              "board_crop {" in str(too_big.get("error", ""))
              and '"cx"' in str(too_big.get("error", "")), "(expected True)")
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
        tools.call("board_crop_reset", {"uid": target})
        centre = tools.call("board_crop", {"uid": target, "cx": 0.5, "cy": 0.5,
                                           "w": 400, "h": 400})
        print("centre crop ->", centre.get("crop"), "(expected x=200 y=100)")
        edge = tools.call("board_crop", {"uid": target, "cx": 0.02, "cy": 0.5,
                                         "w": 400, "h": 400})
        print("edge shifted ->", edge.get("crop"), "shifted:", edge.get("centre_shifted"),
              "(expected x=0)")
        shift = tools.call("board_crop", {"uid": target, "cx": 0.9})
        print("shift keeps size ->", shift.get("crop"),
              "kept:", shift.get("kept_window_size"),
              "(expected w=400 h=400, x=400)")
        frac = tools.call("board_crop", {"uid": target, "cx": 0.5, "cy": 0.5,
                                         "w": 0.5, "h": 0.5})
        print("fractional size ->", frac.get("crop"),
              "(expected x=200 y=150 w=400 h=300)")
        bad_centre = tools.call("board_crop", {"uid": target, "cx": 0.5, "cy": 0.5,
                                               "w": 900, "h": 900})
        print("oversize centre refused ->", bad_centre.get("ok"), "(expected False)")
        print("centre refusal has fix ->",
              "board_crop {" in str(bad_centre.get("error", "")), "(expected True)")
        mixed = tools.call("board_crop", {"uid": target, "x": 0, "y": 0,
                                          "cx": 0.5, "cy": 0.5, "w": 100, "h": 100})
        print("mixed modes refused ->", mixed.get("ok"), "(expected False)")
        # --- NEW: fractional insets + no-op refusal ----------------------
        tools.call("board_crop_reset", {"uid": target})
        tools.call("board_crop", {"uid": target, "x": 100, "y": 80,
                                  "w": 600, "h": 440})
        frac_inset = tools.call("board_crop", {"uid": target, "top": 0.25})
        print("fractional inset ->", frac_inset.get("crop"),
              "insets_px:", frac_inset.get("insets_px"),
              "(expected y=190 h=330, top=110)")
        tiny = tools.call("board_crop", {"uid": target, "top": 0.0001})
        print("tiny inset refused ->", tiny.get("ok"), "(expected False)")
        zero = tools.call("board_crop", {"uid": target, "top": 0, "bottom": 0})
        print("zero inset refused ->", zero.get("ok"), "(expected False)")
        # --- subject mode, the hedgehog workflow -------------------------
        tools.call("board_crop_reset", {"uid": target})
        view = tools.call("board_crop_view", {"uid": target, "max_size": 700})
        print("crop_view ->", view.get("ok"), "source:", view.get("source"),
              "| base64 chars:", len(view.get("_image", {}).get("data", "")),
              "(expected > 1000)")
        subj = tools.call("board_crop_subject",
                          {"uid": target, "box": [0.125, 0.1333, 0.875, 0.8667],
                           "fill": 0.6, "aspect": "3:2"})
        print("subject crop ->", subj.get("crop"), subj.get("error", ""))
        print("subject fill ->", (subj.get("subject") or {}).get("fill_w"),
              "(expected ~0.75: source too narrow for 0.6)")
        print("subject fully inside ->", (subj.get("subject") or {}).get("fully_inside"),
              "(expected True)")
        print("subject hint has equalize ->",
              "normalize_height" in str(subj.get("hint", "")), "(expected True)")
        tall = tools.call("board_crop_subject",
                          {"uid": target, "box": [0.4, 0.05, 0.6, 0.95],
                           "fill": 0.6, "aspect": "3:2"})
        tc = tall.get("crop") or {}
        print("tall subject inside ->",
              tc.get("y", 99) <= 30 + 0.5 and tc.get("y", 0) + tc.get("h", 0) >= 570 - 0.5,
              "(expected True: subject spans y 30..570)")
        bad_box = tools.call("board_crop_subject", {"uid": target, "box": [0.5, 0.5, 0.2, 0.9]})
        print("bad box refused ->", bad_box.get("ok"), "(expected False)")
        view2 = tools.call("board_crop_view", {"uid": target})
        print("view shows current crop ->", view2.get("crop_fractions") is not None,
              "(expected True)")
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
        inset = tools.call("board_crop", {"uid": target, "left": 10, "top": 5})
        print("insets ->", inset.get("crop"), inset.get("error", ""))
        back = tools.call("board_crop_reset", {"uid": target})
        print("crop_reset ->", back.get("crop"), "cropped:", back.get("cropped"))
        print("undo ->", tools.call("board_undo", {}))
        print("DONE - close the window to exit")

    QTimer.singleShot(800, run)
    sys.exit(app.exec())


if __name__ == "__main__":
    _selftest()