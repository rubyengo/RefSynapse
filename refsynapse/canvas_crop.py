"""Non-destructive image crop: overlay, handles, snapping and apply/cancel."""
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtWidgets import QApplication

from .config import HANDLE
from .items import CropOverlay, handle_positions
from .item_tools import is_locked
from .canvas_helpers import handle_cursor


class CropMixin:
    def begin_crop(self, item):
        if is_locked(item):
            self.toast.show_text("Locked — Ctrl+L to unlock", 1500)
            return
        if self._crop:
            self.finish_crop(False)
        original_crop = QRectF(item.crop) if item.crop is not None else None
        original_pos = QPointF(item.pos())
        item.set_crop_editing(True, adjust_position=True)
        overlay = CropOverlay(item)
        self._crop = {"item": item, "overlay": overlay, "mode": None,
                      "handle": None, "start": QPointF(), "rect": QRectF(overlay.rect),
                      "original_crop": original_crop, "original_pos": original_pos}
        self.clear_guides()
        self.crop_scissors.hide()
        self.set_tool("crop")
        self.toast.show_text("Crop: drag frame or borders • hold Ctrl to snap • Enter / LMB outside applies • Esc cancels", 4000)
        self.viewport().update()

    def crop_point(self, viewport_pos):
        crop = self._crop
        return crop["item"].mapFromScene(self.mapToScene(viewport_pos.toPoint()))

    def crop_handle_at(self, point):
        crop = self._crop
        zoom = max(0.01, self.cur_zoom() * (crop["item"].scale() or 1.0))
        tolerance = (HANDLE + 5) / zoom
        rect = crop["overlay"].rect
        for index, (handle, _) in enumerate(handle_positions(rect)):
            if abs(point.x() - handle.x()) <= tolerance and abs(point.y() - handle.y()) <= tolerance:
                return index
        # The whole border is a grab target, not only its small square handles.
        # Indices 4/5/6/7 reuse the existing left/right/top/bottom resize logic.
        if not rect.adjusted(-tolerance, -tolerance, tolerance, tolerance).contains(point):
            return None
        edges = ((4, abs(point.x() - rect.left())),
                 (5, abs(point.x() - rect.right())),
                 (6, abs(point.y() - rect.top())),
                 (7, abs(point.y() - rect.bottom())))
        index, distance = min(edges, key=lambda edge: edge[1])
        return index if distance <= tolerance else None

    def update_crop_scissors(self, pos):
        self._hide_cursor_glyphs()
        if not self._crop or self._op or self._held:
            self.crop_scissors.hide()
            return
        handle = self.crop_handle_at(self.crop_point(pos))
        if handle is None:
            self.crop_scissors.hide()
            self.viewport().setCursor(Qt.CrossCursor)
            return
        # Replace the native resize cursor instead of drawing a second pointer.
        self.viewport().setCursor(Qt.BlankCursor)
        x = min(max(round(pos.x() - self.crop_scissors.width() / 2), 4),
                self.viewport().width() - self.crop_scissors.width() - 4)
        y = min(max(round(pos.y() - self.crop_scissors.height() / 2), 4),
                self.viewport().height() - self.crop_scissors.height() - 4)
        self.crop_scissors.move(x, y)
        self.crop_scissors.show()
        self.crop_scissors.raise_()

    def crop_press(self, pos):
        point = self.crop_point(pos)
        crop = self._crop
        handle = self.crop_handle_at(point)
        rect = QRectF(crop["overlay"].rect)
        # The scissors are hover-only; restore a visible cursor while dragging.
        self.viewport().setCursor(handle_cursor(handle) if handle is not None
                                  else Qt.CrossCursor)
        if handle is not None:
            mode = "resize"
        elif rect.contains(point):
            mode = "move"
        else:
            mode, handle = "resize", 3
            rect = QRectF(point, point)
            crop["overlay"].set_rect(rect)
        crop.update(mode=mode, handle=handle, start=point, rect=rect)
        if QApplication.keyboardModifiers() & Qt.ControlModifier:
            self._show_cursor_glyph("snap", pos)

    def apply_crop_snapping(self, rect, mode, handle, outer, active):
        """Snap crop borders in scene space, including rotated/scaled images."""
        self._guides = []
        settings = self.win.settings
        if not active or not (settings.get("snap_enabled", True) or settings.get("snap_grid", False)):
            return rect
        crop = self._crop
        item = crop["item"]
        excluded = item.topLevelItem()
        tolerance = float(settings.get("snap_tolerance", 10)) / max(0.01, self.cur_zoom())
        grid_size = max(5, int(settings.get("grid_size", 50)))

        def best_delta(values, axis):
            best = None
            if settings.get("snap_enabled", True):
                for other in self.win.top_items():
                    if other is excluded:
                        continue
                    target_rect = self.win.full_rect(other)
                    if axis == "x":
                        targets = (target_rect.left(), target_rect.center().x(), target_rect.right())
                    else:
                        targets = (target_rect.top(), target_rect.center().y(), target_rect.bottom())
                    for value in values:
                        for target in targets:
                            delta = target - value
                            if best is None or abs(delta) < abs(best[0]):
                                best = (delta, target)
            snapped = best is not None and abs(best[0]) <= tolerance
            if not snapped and settings.get("snap_grid", False):
                value = values[0]
                target = round(value / grid_size) * grid_size
                best, snapped = (target - value, target), True
            return best if snapped else None

        if mode == "move":
            scene_rect = item.mapRectToScene(rect)
            best_x = best_delta((scene_rect.left(), scene_rect.center().x(), scene_rect.right()), "x")
            best_y = best_delta((scene_rect.top(), scene_rect.center().y(), scene_rect.bottom()), "y")
            dx = best_x[0] if best_x else 0.0
            dy = best_y[0] if best_y else 0.0
            if dx or dy:
                scene_origin = item.mapToScene(QPointF(0, 0))
                local_origin = item.mapFromScene(scene_origin)
                shifted = item.mapFromScene(scene_origin + QPointF(dx, dy))
                rect.translate(shifted - local_origin)
                # Snapping must never push the crop outside the source image.
                if rect.left() < outer.left():
                    rect.moveLeft(outer.left())
                if rect.right() > outer.right():
                    rect.moveRight(outer.right())
                if rect.top() < outer.top():
                    rect.moveTop(outer.top())
                if rect.bottom() > outer.bottom():
                    rect.moveBottom(outer.bottom())
            if best_x:
                self._guides.append(("v", best_x[1], abs(dx) * self.cur_zoom()))
            if best_y:
                self._guides.append(("h", best_y[1], abs(dy) * self.cur_zoom()))
            return rect
        # For resize, only the dragged handle moves; the opposite border stays put.
        scene_origin = item.mapToScene(QPointF(0, 0))
        x_vector = item.mapToScene(QPointF(1, 0)) - scene_origin
        y_vector = item.mapToScene(QPointF(0, 1)) - scene_origin
        point = handle_positions(rect)[handle][0]
        scene_point = item.mapToScene(point)
        if handle in (0, 1, 2, 3, 4, 5) and abs(x_vector.x()) > 1e-6:
            best_x = best_delta((scene_point.x(),), "x")
            if best_x:
                local_delta = (best_x[1] - scene_point.x()) / x_vector.x()
                if handle in (0, 2, 4):
                    rect.setLeft(rect.left() + local_delta)
                else:
                    rect.setRight(rect.right() + local_delta)
                self._guides.append(("v", best_x[1], abs(best_x[0]) * self.cur_zoom()))
        scene_point = item.mapToScene(handle_positions(rect)[handle][0])
        if handle in (0, 1, 2, 3, 6, 7) and abs(y_vector.y()) > 1e-6:
            best_y = best_delta((scene_point.y(),), "y")
            if best_y:
                local_delta = (best_y[1] - scene_point.y()) / y_vector.y()
                if handle in (0, 1, 6):
                    rect.setTop(rect.top() + local_delta)
                else:
                    rect.setBottom(rect.bottom() + local_delta)
                self._guides.append(("h", best_y[1], abs(best_y[0]) * self.cur_zoom()))
        return rect.normalized().intersected(outer)

    def crop_move(self, pos, mods=None):
        crop = self._crop
        if not crop or not crop.get("mode"):
            return
        point = self.crop_point(pos)
        start, original = crop["start"], QRectF(crop["rect"])
        outer = crop["item"].full_rect_local()
        if crop["mode"] == "move":
            delta = point - start
            dx = min(max(delta.x(), outer.left() - original.left()), outer.right() - original.right())
            dy = min(max(delta.y(), outer.top() - original.top()), outer.bottom() - original.bottom())
            rect = original.translated(dx, dy)
        else:
            rect = QRectF(original)
            handle = crop["handle"]
            if handle in (0, 2, 4):
                rect.setLeft(point.x())
            if handle in (1, 3, 5):
                rect.setRight(point.x())
            if handle in (0, 1, 6):
                rect.setTop(point.y())
            if handle in (2, 3, 7):
                rect.setBottom(point.y())
            rect = rect.normalized().intersected(outer)
        modifiers = QApplication.keyboardModifiers() if mods is None else mods
        snapping = bool(modifiers & Qt.ControlModifier)
        rect = self.apply_crop_snapping(rect, crop["mode"], crop["handle"], outer,
                                        snapping)
        if snapping:
            self._show_cursor_glyph("snap", pos)
        else:
            self._hide_cursor_glyphs()
            self.viewport().setCursor(handle_cursor(crop["handle"])
                                      if crop["mode"] == "resize" else Qt.CrossCursor)
        if rect.width() >= 4 and rect.height() >= 4:
            crop["overlay"].set_rect(rect)
        self.viewport().update()

    def finish_crop(self, confirm):
        self.crop_scissors.hide()
        crop = self._crop
        if not crop:
            return
        self._crop = None
        self.clear_guides()
        item, overlay = crop["item"], crop["overlay"]
        rect = QRectF(overlay.rect)
        self.scene().removeItem(overlay)
        item.set_crop_editing(False)
        if confirm:
            item.set_crop(rect, adjust_position=True)
            self.win.record_history()
        else:
            # Editing temporarily reveals the full source; restore both geometry
            # and position so Esc / RMB is a perfectly lossless cancellation.
            item.set_crop(crop["original_crop"])
            item.setPos(crop["original_pos"])
        self.set_tool("select")
        self.viewport().update()