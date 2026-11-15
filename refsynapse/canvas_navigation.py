"""View math, navigation (pan / zoom / wheel) and cursor glyphs."""
import math

from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QTransform, QCursor
from PySide6.QtWidgets import QApplication

from .config import MIN_ZOOM, MAX_ZOOM, nav_match, mods_of


class NavigationMixin:
    # ---------- view math ----------
    def cur_zoom(self):
        return self.transform().m11() or 1.0

    def vp_center(self):
        return QPointF(self.viewport().rect().center())

    def cursor_vp(self):
        return QPointF(self.viewport().mapFromGlobal(QCursor.pos()))

    def to_scene(self, vp):
        return self.mapToScene(QPointF(vp).toPoint())

    def scroll_by(self, dx, dy):
        if hasattr(self, "crop_scissors"):
            self.crop_scissors.hide()
        h, v = self.horizontalScrollBar(), self.verticalScrollBar()
        h.setValue(h.value() + round(dx))
        v.setValue(v.value() + round(dy))
        self.schedule_media_update()

    def zoom_at(self, f, vp_pos=None):
        vp_pos = self.vp_center() if vp_pos is None else QPointF(vp_pos)
        z = self.cur_zoom()
        f = max(MIN_ZOOM, min(MAX_ZOOM, z * f)) / z
        if abs(f - 1) < 1e-6:
            return
        anchor = self.to_scene(vp_pos)
        self.scale(f, f)
        p = self.mapFromScene(anchor)
        self.scroll_by(p.x() - vp_pos.x(), p.y() - vp_pos.y())

    def set_view(self, zoom, center):
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        self.setTransform(QTransform.fromScale(zoom, zoom))
        self.centerOn(center)
        self.schedule_media_update()

    def show_zoom(self):
        self.toast.show_text(f"Zoom: {round(self.cur_zoom() * 100)}%")

    # ---------- cursor glyphs ----------
    def _hide_cursor_glyphs(self):
        if hasattr(self, "cursor_glyphs"):
            for widget in self.cursor_glyphs.values():
                widget.hide()

    def _place_cursor_glyph(self, widget, pos=None):
        pos = self.cursor_vp() if pos is None else QPointF(pos)
        x = min(max(round(pos.x() - widget.width() / 2), 4),
                max(4, self.viewport().width() - widget.width() - 4))
        y = min(max(round(pos.y() - widget.height() / 2), 4),
                max(4, self.viewport().height() - widget.height() - 4))
        widget.move(x, y)
        widget.show()
        widget.raise_()

    def _show_cursor_glyph(self, kind, pos=None):
        widget = self.cursor_glyphs[kind]
        self._hide_cursor_glyphs()
        self.crop_scissors.hide()
        self.viewport().setCursor(Qt.BlankCursor)
        self._place_cursor_glyph(widget, pos)

    @staticmethod
    def _scale_glyph_kind(handle):
        if handle is None:
            return "scale_center"
        if handle in (4, 5):
            return "scale_horizontal"
        if handle in (6, 7):
            return "scale_vertical"
        # TL/BR use the NW–SE diagonal; TR/BL use its mirror (NE–SW).
        if handle in (0, 3):
            return "scale_diagonal_left"
        return "scale_diagonal"

    def _transform_glyph_kind(self, transform):
        mode = transform["mode"]
        if mode == "scale":
            return self._scale_glyph_kind(transform.get("handle"))
        if mode == "rotate":
            return "rotate_cycle"
        return "move"

    def _restore_cursor(self, vp_pos=None):
        self._hide_cursor_glyphs()
        if self._tf:
            self._show_cursor_glyph(self._transform_glyph_kind(self._tf), vp_pos)
            return
        if self._held and not self._op:
            mods = mods_of(QApplication.keyboardModifiers())
            op = nav_match(self.win.nav, "left", mods, self._held)
            if op == "pan":
                self._show_cursor_glyph("pan", vp_pos)
            elif op == "zoom":
                self._show_cursor_glyph("zoom_in", vp_pos)
            else:
                self.viewport().setCursor(Qt.CrossCursor)
        elif self._crop and not self._op:
            self.viewport().setCursor(Qt.CrossCursor)
        elif self.tool == "draw" and not self._op:
            self._show_cursor_glyph("draw", vp_pos)
        else:
            self.viewport().unsetCursor()

    # ---------- navigation drag ----------
    def begin_op(self, op, pos, button):
        if op == "move_window":
            self.win.windowHandle().startSystemMove()
            return
        self._op, self._op_btn = op, button
        self._start, self._last = QPointF(pos), QPointF(pos)
        if op == "pan":
            self._show_cursor_glyph("pan", pos)
        elif op == "zoom":
            self._show_cursor_glyph("zoom_in", pos)
        else:
            self._hide_cursor_glyphs()
            self.viewport().setCursor(Qt.SizeVerCursor)

    def update_op(self, pos):
        d = pos - self._last
        self._last = QPointF(pos)
        if self._op == "pan":
            self.scroll_by(-d.x(), -d.y())
            self._show_cursor_glyph("pan", pos)
        elif self._op == "zoom":
            f = math.exp((d.x() - d.y()) * 0.007 * float(self.win.nav["zoom_drag_speed"]))
            self._show_cursor_glyph("zoom_in" if f >= 1.0 else "zoom_out", pos)
            self.zoom_at(f, self._start)
            self.show_zoom()

    def wheelEvent(self, e):
        if self._tf:
            return
        nav = self.win.nav
        dy, dx = e.angleDelta().y(), e.angleDelta().x()
        d = dy or dx
        if not d:
            return
        m = e.modifiers()
        if bool(m & Qt.ControlModifier) and not bool(m & Qt.ShiftModifier):
            document = self.pdf_item_at(e.position())
            if document is not None:
                self._show_cursor_glyph("pdf_scroll", e.position())
                self._hover_cur = True
                old_page = document.current_page()
                document.change_page(-1 if d > 0 else 1)
                if document.current_page() != old_page:
                    self.win.record_history()
                    self.toast.show_text(
                        f"PDF: {document.current_page() + 1} / {document.page_count()}")
                e.accept()
                return
        if bool(m & Qt.ControlModifier) and bool(m & Qt.ShiftModifier):
            self.win.change_opacity(0.05 if d > 0 else -0.05)
            return
        model = self.model_item_at(e.position())
        if self.model_input_active(model):
            if nav["invert_wheel"]:
                d = -d
            step = 1 + float(nav["zoom_step"]) / 100
            if model.zoom(step ** (d / 120)):
                self.win.record_history()
            self.toast.show_text(f"3D zoom: {round(model.model_zoom * 100)}%")
            e.accept()
            return
        if nav["wheel"] == "zoom" or bool(m & Qt.AltModifier):
            if nav["invert_wheel"]:
                d = -d
            step = 1 + float(nav["zoom_step"]) / 100
            self.zoom_at(step ** (d / 120), e.position())
            return
        if bool(m & (Qt.ControlModifier | Qt.ShiftModifier)) or (dx and not dy):
            self.scroll_by(-d, 0)
        else:
            self.scroll_by(0, -d)