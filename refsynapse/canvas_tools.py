"""Direct manipulation tools: draw, text, frame, selection band, eyedropper, fill, tool mode."""
import math

from PySide6.QtCore import Qt, QRect, QRectF, QPointF, QPoint, QEvent
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QImage
from PySide6.QtWidgets import QGraphicsView, QGraphicsItem, QRubberBand, QWidget

from .config import save_settings, physical_key, ev
from .items import smooth_stroke_path
from .item_tools import is_locked

# Temporary diagnostics: toast every key the canvas filter sees.
PICKER_DEBUG = False

# Raster flood-fill working resolution cap (images only).
FILL_MAX_PIXELS = 4_000_000
FILL_DEFAULT_TOLERANCE = 24
FILL_OVERSAMPLE = 2.0


class ColorLoupe(QWidget):
    """Magnified pixel grid + HEX readout following the eyedropper cursor."""
    GRID = 9
    CELL = 11
    PAD = 8
    LABEL = 26

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._image = None
        self._color = QColor("#000000")
        side = self.GRID * self.CELL + self.PAD * 2
        self.setFixedSize(side, side + self.LABEL)
        self.hide()

    def set_sample(self, image, color):
        self._image = image
        self._color = QColor(color)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect())
        p.setPen(QPen(QColor(255, 255, 255, 70), 1))
        p.setBrush(QColor(18, 18, 22, 235))
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        grid = self.GRID
        cell = self.CELL
        ox = self.PAD
        oy = self.PAD
        if self._image is not None:
            for y in range(grid):
                for x in range(grid):
                    p.fillRect(QRectF(ox + x * cell, oy + y * cell, cell, cell),
                               self._image.pixelColor(x, y))
        # Center pixel ring.
        c = grid // 2
        ring = QRectF(ox + c * cell - 1, oy + c * cell - 1, cell + 2, cell + 2)
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(ring)
        p.setPen(QPen(QColor("#000000"), 1))
        p.drawRect(ring.adjusted(-1, -1, 1, 1))
        # Swatch + HEX.
        ty = oy + grid * cell + 6
        p.setPen(Qt.NoPen)
        p.setBrush(self._color)
        p.drawRoundedRect(QRectF(ox, ty, 16, 16), 3, 3)
        p.setPen(QColor("#f0f0f0"))
        f = QFont("Segoe UI", 9)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(ox + 22, ty, self.width() - ox - 22, 16),
                   Qt.AlignVCenter | Qt.AlignLeft, self._color.name().upper())
        p.end()


class ToolsMixin:
    def __init__(self, *args, **kwargs):
        # Cooperative init: Canvas.__init__ calls super().__init__(scene),
        # which reaches us through the MRO. Install the key watcher here so
        # the hotkeys work from the very first moment, before any tool use.
        super().__init__(*args, **kwargs)
        self._tool_click_consumed = False
        self._picker_done = None
        self._ensure_picker_filter()

    def _ensure_picker_filter(self):
        if not getattr(self, "_picker_filter", False):
            self.installEventFilter(self)
            self.viewport().installEventFilter(self)
            self._picker_filter = True

    def _dbg(self, text):
        if PICKER_DEBUG:
            win = getattr(self, "win", None)
            if win is not None:
                win.toast("TOOL DEBUG: " + text)

    def set_tool(self, tool):
        self._ensure_picker_filter()
        # The picker owns its activation (loupe + filter); routing it here
        # keeps every entry point (hotkey, UI, state restore) safe.
        if tool == "picker":
            self._picker_activate()
            return
        self.tool = tool
        self._hide_cursor_glyphs()
        loupe = getattr(self, "_loupe", None)
        if loupe is not None:
            loupe.hide()
        self.setDragMode(QGraphicsView.NoDrag if tool in ("draw", "text", "frame", "crop", "picker", "bucket")
                         else QGraphicsView.RubberBandDrag)
        if tool == "draw":
            self._show_cursor_glyph("draw")
        else:
            self.viewport().setCursor(Qt.CrossCursor if tool in ("text", "frame", "crop", "picker", "bucket")
                                      else Qt.ArrowCursor)

    def begin_draw_hold(self):
        self._draw_hold = True
        self.win.set_tool("draw")

    def end_draw_hold(self):
        self._draw_hold = False
        if self._stroke:
            self.end_stroke()
        else:
            self.win.set_tool("select")

    # ---------- freehand stroke ----------
    def begin_stroke(self, pos):
        self.scene().clearSelection()
        self._stroke = self.win.add_stroke(pos)
        self._stroke.points = [QPointF(0, 0)]
        self._stroke.setPath(smooth_stroke_path(self._stroke.points))

    def extend_stroke(self, pos):
        if self._stroke:
            point = self._stroke.mapFromScene(pos)
            points = self._stroke.points
            if points:
                last = points[-1]
                min_step = max(0.35, min(1.5, self._stroke.width * 0.05))
                if math.hypot(point.x() - last.x(), point.y() - last.y()) < min_step:
                    return
            points.append(point)
            self._stroke.setPath(smooth_stroke_path(points))

    def end_stroke(self):
        if self._stroke:
            self._stroke = None
            self.win.record_history()
            if not self._draw_hold:
                self.win.set_tool("select")

    # ---------- text band ----------
    def begin_text_band(self, pos):
        self.scene().clearSelection()
        self._text_start_vp = pos.toPoint()
        self._text_band = QRubberBand(QRubberBand.Rectangle, self.viewport())
        self._text_band.setGeometry(QRect(self._text_start_vp, self._text_start_vp))
        self._text_band.show()

    def update_text_band(self, pos):
        if self._text_band:
            self._text_band.setGeometry(QRect(self._text_start_vp, pos.toPoint()).normalized())

    def finish_text_band(self, pos):
        if not self._text_band:
            return
        rect = QRect(self._text_start_vp, pos.toPoint()).normalized()
        self._text_band.hide()
        self._text_band.deleteLater()
        self._text_band = None
        scene_rect = QRectF(self.mapToScene(rect.topLeft()),
                            self.mapToScene(rect.bottomRight())).normalized()
        if scene_rect.width() < 40:
            scene_rect.setWidth(320 / self.cur_zoom())
        if scene_rect.height() < 24:
            scene_rect.setHeight(100 / self.cur_zoom())
        self.win.add_text_in_rect(scene_rect)
        # Frame creation is complete; text editing can continue in Select mode.
        self.win.set_tool("select")

    # ---------- frame band ----------
    def begin_frame_band(self, pos):
        self.scene().clearSelection()
        self._frame_start_vp = pos.toPoint()
        self._frame_band = QRubberBand(QRubberBand.Rectangle, self.viewport())
        self._frame_band.setGeometry(QRect(self._frame_start_vp, self._frame_start_vp))
        self._frame_band.show()

    def update_frame_band(self, pos):
        if self._frame_band:
            self._frame_band.setGeometry(QRect(self._frame_start_vp, pos.toPoint()).normalized())

    def finish_frame_band(self, pos):
        if not self._frame_band:
            return
        rect = QRect(self._frame_start_vp, pos.toPoint()).normalized()
        self._frame_band.hide()
        self._frame_band.deleteLater()
        self._frame_band = None
        scene_rect = QRectF(self.mapToScene(rect.topLeft()),
                            self.mapToScene(rect.bottomRight())).normalized()
        if scene_rect.width() < 40:
            scene_rect.setWidth(320 / self.cur_zoom())
        if scene_rect.height() < 40:
            scene_rect.setHeight(200 / self.cur_zoom())
        self.win.add_frame_in_rect(scene_rect)
        self.win.set_tool("select")

    # ---------- selection band ----------
    def begin_selection_band(self, pos, mode):
        self._select_start_vp = pos.toPoint()
        self._select_mode = mode
        self._select_band = QRubberBand(QRubberBand.Rectangle, self.viewport())
        self._select_band.setGeometry(QRect(self._select_start_vp, self._select_start_vp))
        self._select_band.show()

    def update_selection_band(self, pos):
        if self._select_band:
            self._select_band.setGeometry(QRect(self._select_start_vp, pos.toPoint()).normalized())

    def finish_selection_band(self, pos):
        if not self._select_band:
            return
        rect = QRect(self._select_start_vp, pos.toPoint()).normalized()
        self._select_band.hide()
        self._select_band.deleteLater()
        self._select_band = None
        scene_rect = QRectF(self.mapToScene(rect.topLeft()),
                            self.mapToScene(rect.bottomRight())).normalized()
        for item in self.win.top_items():
            if scene_rect.intersects(self.win.full_rect(item)):
                item.setSelected(self._select_mode == "add")

    # ---------- eyedropper / fill hotkeys and mouse routing ----------
    def _picker_key(self, e):
        """Physical key code, tolerant to helper quirks and event types."""
        try:
            key = physical_key(e)
            if isinstance(key, str) and len(key) == 1 and key.isalpha():
                return ord(key.upper())
        except Exception:
            pass
        return ev(e.key())

    def _tool_cancel(self):
        if self.tool == "picker":
            self._picker_cancel()
        elif self.tool == "bucket":
            self._bucket_cancel()

    def _nav_active(self):
        # Let the normal navigation mixin keep doing pan / zoom while a
        # color tool is armed. _held is the held Space/Z key, _op is the
        # active mouse navigation drag.
        return bool(getattr(self, "_op", None) or getattr(self, "_held", None))

    def eventFilter(self, obj, e):
        # Suppress the release that belongs to a click we already consumed,
        # even if the tool switched back to Select during the click.
        if obj is self.viewport() and e.type() == QEvent.Type.MouseButtonRelease:
            if getattr(self, "_tool_click_consumed", False):
                self._tool_click_consumed = False
                return True

        if obj is self and e.type() in (QEvent.Type.KeyPress, QEvent.Type.ShortcutOverride):
            k = self._picker_key(e)
            is_i = (k == ev(Qt.Key_I)) or (e.key() == Qt.Key_I)
            mods = e.modifiers()
            plain_i = is_i and mods == Qt.NoModifier
            shift_i = is_i and mods == Qt.ShiftModifier
            if PICKER_DEBUG and e.type() == QEvent.Type.KeyPress and not e.isAutoRepeat():
                self._dbg(f"key={e.key()} phys={k} I={is_i} mods={mods} "
                          f"tf={bool(self._tf)} crop={bool(self._crop)} edit={bool(self._editing_text)}")
            if (plain_i or shift_i) and not self._tf and not self._crop and not self._editing_text:
                if e.type() == QEvent.Type.ShortcutOverride:
                    # Claim I / Shift+I before any QAction shortcut can eat them.
                    e.accept()
                    return True
                if not e.isAutoRepeat():
                    target_tool = "picker" if plain_i else "bucket"
                    if self.tool == target_tool:
                        self._tool_cancel()
                    elif target_tool == "picker":
                        self._picker_activate()
                    else:
                        self._bucket_activate()
                return True

        if self.tool not in ("picker", "bucket"):
            return False

        if obj is self.viewport():
            if e.type() == QEvent.Type.MouseMove:
                if self._nav_active():
                    loupe = getattr(self, "_loupe", None)
                    if loupe is not None:
                        loupe.hide()
                    return False
                if self.tool == "picker":
                    self._picker_update(e.position())
                    return True
                return False

            if e.type() == QEvent.Type.MouseButtonPress:
                # Navigation gestures pass through: held Space/Z + drag,
                # middle button, or any already active navigation drag.
                if self._nav_active() or e.button() not in (Qt.LeftButton, Qt.RightButton):
                    return False
                if e.button() == Qt.LeftButton:
                    self._tool_click_consumed = True
                    if self.tool == "picker":
                        self._picker_pick(e.position())
                    else:
                        self._bucket_fill(e.position())
                else:
                    self._tool_cancel()
                return True

            if e.type() == QEvent.Type.MouseButtonRelease:
                return False

            if e.type() == QEvent.Type.Wheel:
                loupe = getattr(self, "_loupe", None)
                if loupe is not None:
                    loupe.hide()
                return False

            if e.type() == QEvent.Type.Leave:
                loupe = getattr(self, "_loupe", None)
                if loupe is not None:
                    loupe.hide()
                return False

        if obj is self and e.type() == QEvent.Type.KeyPress:
            if e.key() == Qt.Key_Escape:
                self._tool_cancel()
                return True
        return False

    # ---------- eyedropper ----------
    def arm_picker(self, callback):
        """Run the eyedropper once; callback(QColor) on pick, callback(None) on cancel.
        Used by the color dialog to sample the board without touching draw_color."""
        self._picker_done = callback
        self._picker_activate()

    def _picker_activate(self):
        self._dbg("picker activate")
        self.setFocus()
        self.tool = "picker"
        self.setDragMode(QGraphicsView.NoDrag)
        self._hide_cursor_glyphs()
        self.viewport().setCursor(Qt.CrossCursor)
        if getattr(self, "_loupe", None) is None:
            self._loupe = ColorLoupe(self)
        self._ensure_picker_filter()
        self.win.titlebar.set_tool("select")
        self.win.toast("Eyedropper: LMB pick color • Space+LMB pan • Esc / RMB cancel")
        self._picker_update(self.cursor_vp())

    def _picker_cancel(self):
        done = getattr(self, "_picker_done", None)
        self._picker_done = None
        if getattr(self, "_loupe", None) is not None:
            self._loupe.hide()
        self.win.set_tool("select")
        if done is not None:
            done(None)

    def _picker_sample(self, pos):
        p = pos.toPoint()
        grid = ColorLoupe.GRID
        half = grid // 2
        rect = QRect(p.x() - half, p.y() - half, grid, grid)
        image = self.viewport().grab(rect).toImage()
        if image.width() != grid or image.height() != grid:
            image = image.scaled(grid, grid, Qt.IgnoreAspectRatio, Qt.FastTransformation)
        return image, image.pixelColor(half, half)

    def _picker_update(self, pos):
        loupe = getattr(self, "_loupe", None)
        if loupe is None:
            return
        image, color = self._picker_sample(pos)
        loupe.set_sample(image, color)
        vw, vh = self.viewport().width(), self.viewport().height()
        x = round(pos.x()) + 18
        y = round(pos.y()) + 18
        if x + loupe.width() > vw - 4:
            x = round(pos.x()) - loupe.width() - 18
        if y + loupe.height() > vh - 4:
            y = round(pos.y()) - loupe.height() - 18
        loupe.move(max(4, x), max(4, y))
        loupe.show()
        loupe.raise_()

    def _picker_pick(self, pos):
        _, color = self._picker_sample(pos)
        done = getattr(self, "_picker_done", None)
        self._picker_done = None
        if done is not None:
            # Dialog mode: hand the color back, leave draw_color untouched.
            self._picker_cancel()
            done(QColor(color))
            return
        name = color.name()
        self.win.draw_color = name
        self.win.titlebar.set_color(name)
        self.win.settings["draw_color"] = name
        save_settings(self.win.settings)
        self.win.toast(f"Picked {name.upper()}")
        self._picker_cancel()

    # ---------- paint bucket ----------
    def _bucket_activate(self):
        self._dbg("bucket activate")
        self.setFocus()
        self.tool = "bucket"
        self.setDragMode(QGraphicsView.NoDrag)
        self._hide_cursor_glyphs()
        self.viewport().setCursor(Qt.CrossCursor)
        loupe = getattr(self, "_loupe", None)
        if loupe is not None:
            loupe.hide()
        self.win.titlebar.set_tool("select")
        self.win.toast("Fill: LMB recolor • Space+LMB pan • Shift+I / Esc / RMB exit")

    def _bucket_cancel(self):
        self.win.set_tool("select")

    def _bucket_target(self, pos):
        """Topmost paintable item under the cursor, ignoring our fill overlays."""
        for item in self.items(pos.toPoint()):
            if getattr(item, "_refsynapse_fill", False):
                continue
            kind = getattr(item, "kind", None)
            if kind in ("image", "stroke", "frame", "text"):
                return item
        return None

    def _recolor_item(self, target, color):
        """Recolor a vector object in place: no overlay, no group, full coverage.

        Frames / strokes / text keep their paint in a color attribute, a pen,
        or (for text items) the default text color. The brush is deliberately
        left alone so a frame's interior is not flooded with the border color.
        """
        changed = False
        for attr in ("color", "_color"):
            current = getattr(target, attr, None)
            if isinstance(current, QColor):
                setattr(target, attr, QColor(color))
                changed = True
            elif isinstance(current, str) and current.startswith("#"):
                setattr(target, attr, color.name())
                changed = True
        pen_getter = getattr(target, "pen", None)
        if callable(pen_getter):
            try:
                pen = pen_getter()
            except TypeError:
                pen = None
            if isinstance(pen, QPen) and pen.style() != Qt.NoPen:
                pen.setColor(color)
                target.setPen(pen)
                changed = True
        if hasattr(target, "setDefaultTextColor"):
            target.setDefaultTextColor(color)
            changed = True
        if changed:
            target.update()
            if hasattr(target, "refresh"):
                try:
                    target.refresh()
                except TypeError:
                    pass
        return changed

    def _bucket_render(self, target):
        """Render the target's scene bounds without selection decorations."""
        source = target.mapRectToScene(target.boundingRect())
        if source.isEmpty():
            raise ValueError("empty fill target")
        zoom = max(0.01, self.cur_zoom()) * FILL_OVERSAMPLE
        base_w = max(1.0, source.width() * zoom)
        base_h = max(1.0, source.height() * zoom)
        render_scale = 1.0
        if base_w * base_h > FILL_MAX_PIXELS:
            render_scale = math.sqrt(FILL_MAX_PIXELS / (base_w * base_h))
        w = max(1, round(base_w * render_scale))
        h = max(1, round(base_h * render_scale))
        image = QImage(w, h, QImage.Format_RGBA8888)
        image.fill(Qt.transparent)

        selected = self.scene().selectedItems()
        self.scene().clearSelection()
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            self.scene().render(painter, QRectF(0, 0, w, h), source, Qt.IgnoreAspectRatio)
        finally:
            painter.end()
            for item in selected:
                item.setSelected(True)
        effective_scale = w / source.width()
        return image, source, effective_scale

    def _bucket_flood(self, image, sx, sy, color, tolerance):
        """Scanline flood fill; returns a transparent overlay QImage or None."""
        image = image.convertToFormat(QImage.Format_RGBA8888)
        w, h = image.width(), image.height()
        if not (0 <= sx < w and 0 <= sy < h):
            return None
        bits = image.bits()
        try:
            data = bytearray(bits.tobytes())
        except AttributeError:
            data = bytearray(bits)
        stride = image.bytesPerLine()
        visited = bytearray(w * h)
        seed = sy * stride + sx * 4
        sr, sg, sb, sa = data[seed:seed + 4]

        result = QImage(w, h, QImage.Format_ARGB32)
        result.fill(Qt.transparent)
        painter = QPainter(result)

        def matches(x, y):
            idx = y * w + x
            if visited[idx]:
                return False
            o = y * stride + x * 4
            return (abs(data[o] - sr) <= tolerance and
                    abs(data[o + 1] - sg) <= tolerance and
                    abs(data[o + 2] - sb) <= tolerance and
                    abs(data[o + 3] - sa) <= tolerance)

        stack = [(sx, sy)]
        filled = 0
        try:
            while stack:
                x, y = stack.pop()
                if visited[y * w + x] or not matches(x, y):
                    continue
                left = x
                while left > 0 and matches(left - 1, y):
                    left -= 1
                right = x
                while right + 1 < w and matches(right + 1, y):
                    right += 1
                for xx in range(left, right + 1):
                    visited[y * w + xx] = 1
                painter.fillRect(left, y, right - left + 1, 1, color)
                filled += right - left + 1
                for ny in (y - 1, y + 1):
                    if 0 <= ny < h:
                        xx = left
                        while xx <= right:
                            if matches(xx, ny):
                                stack.append((xx, ny))
                                while xx <= right and matches(xx, ny):
                                    xx += 1
                            else:
                                xx += 1
        finally:
            painter.end()
        return result if filled else None

    def _bucket_fill(self, pos):
        try:
            target = self._bucket_target(pos)
            if target is None:
                self.win.toast("Fill: click a frame, stroke, text or image")
                return
            color = QColor(self.win.draw_color)
            if not color.isValid():
                color = QColor("#000000")

            # Vector objects are recolored directly: complete coverage, no
            # extra layers, no group title, no dashed group outline.
            if getattr(target, "kind", None) != "image" and self._recolor_item(target, color):
                self.win.record_history()
                self.win.toast(f"Recolored {color.name().upper()} • Shift+I or Esc to exit")
                return

            # Raster fallback: a plain overlay image above the picture.
            image, source, scale = self._bucket_render(target)
            scene_pos = self.mapToScene(pos.toPoint())
            sx = round((scene_pos.x() - source.left()) * image.width() / source.width())
            sy = round((scene_pos.y() - source.top()) * image.height() / source.height())
            sx = max(0, min(image.width() - 1, sx))
            sy = max(0, min(image.height() - 1, sy))
            try:
                tolerance = int(self.win.settings.get("fill_tolerance", FILL_DEFAULT_TOLERANCE))
            except (TypeError, ValueError):
                tolerance = FILL_DEFAULT_TOLERANCE
            tolerance = max(0, min(255, tolerance))

            result = self._bucket_flood(image, sx, sy, color, tolerance)
            if result is None:
                self.win.toast("Fill: no matching area under the cursor")
                return
            path = self.win.save_image(result)
            if not path:
                self.win.toast("Fill: could not save the result")
                return
            fill = self.win.add_file(path, source.topLeft())
            if fill is None:
                self.win.toast("Fill: could not add the result")
                return
            fill.setScale(1.0 / scale)
            fill._refsynapse_fill = True
            self.scene().clearSelection()
            fill.setSelected(True)
            self.win.record_history()
            self.win.toast(f"Filled {color.name().upper()} • Shift+I or Esc to exit")
        except Exception as ex:
            self.win.toast("Fill failed: " + str(ex))

    # ---------- text editing ----------
    def text_editing_finished(self, item):
        if self._editing_text is not item:
            return
        self._editing_text = None
        if self.win._text_action_shortcuts is not None:
            for key, shortcuts in self.win._text_action_shortcuts.items():
                self.win.act[key].setShortcuts(shortcuts)
            self.win._text_action_shortcuts = None
        item.setTextInteractionFlags(Qt.NoTextInteraction)
        item.setFlag(QGraphicsItem.ItemIsMovable, not is_locked(item))
        if not item.toPlainText().strip():
            self.win.remove_item(item)
        else:
            item.setSelected(True)
        self.win.record_history()
        self.win.set_tool("select")