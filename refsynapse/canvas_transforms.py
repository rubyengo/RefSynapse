"""Transforms (move / rotate / scale), snapping, frame resize and handles."""
import math

from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtWidgets import QApplication

from .config import HANDLE
from .items import handle_positions, handle_rect, place_note
from .item_tools import is_locked


class TransformsMixin:
    # ---------- selection rect & guides ----------
    def selection_scene_rect(self):
        rect = QRectF()
        for item in self.win.transformable_selected():
            rect = rect.united(self.win.full_rect(item))
        return rect

    def clear_guides(self):
        if self._guides:
            self._guides = []
            self.viewport().update()

    # ---------- snapping ----------
    def apply_cursor_snapping(self, items, allow_x, allow_y, cursor):
        """Translate the active virtual handle to cursor-selected object lines."""
        settings = self.win.settings
        rect = QRectF()
        for item in items:
            rect = rect.united(self.win.full_rect(item))
        if rect.isNull() or rect.isEmpty():
            return
        zoom = max(0.01, self.cur_zoom())
        tolerance = float(settings.get("snap_tolerance", 10)) / zoom
        cursor_radius = max(64.0, float(settings.get("snap_tolerance", 10)) * 3.0)
        handles = handle_positions(rect)
        handle = min(range(len(handles)), key=lambda i: (
            (cursor.x() - handles[i][0].x()) ** 2 +
            (cursor.y() - handles[i][0].y()) ** 2))
        source_x = {
            0: rect.left(), 1: rect.right(), 2: rect.left(), 3: rect.right(),
            4: rect.left(), 5: rect.right(),
        }.get(handle)
        source_y = {
            0: rect.top(), 1: rect.top(), 2: rect.bottom(), 3: rect.bottom(),
            6: rect.top(), 7: rect.bottom(),
        }.get(handle)
        selected = set(items)
        candidates = []

        def consider(source, target, axis, bounds):
            pointer = cursor.x() if axis == "v" else cursor.y()
            error = target - pointer
            if abs(error) > tolerance:
                return
            dx = max(bounds.left() - cursor.x(), 0.0, cursor.x() - bounds.right())
            dy = max(bounds.top() - cursor.y(), 0.0, cursor.y() - bounds.bottom())
            object_distance = math.hypot(dx, dy) * zoom
            center = bounds.center().x() if axis == "v" else bounds.center().y()
            candidates.append((axis, target - source, target, abs(error) * zoom,
                               int(target == center), object_distance))

        if settings.get("snap_enabled", True):
            for other in self.win.top_items():
                if other in selected:
                    continue
                target = self.win.full_rect(other)
                if source_x is not None:
                    for value in (target.left(), target.center().x(), target.right()):
                        consider(source_x, value, "v", target)
                if source_y is not None:
                    for value in (target.top(), target.center().y(), target.bottom()):
                        consider(source_y, value, "h", target)
        nearby = [entry for entry in candidates if entry[5] <= cursor_radius]
        best_x = best_y = None
        if nearby:
            nearest = min(entry[5] for entry in nearby)
            targets = [entry for entry in nearby if entry[5] <= nearest + 6.0]
            for entry in targets:
                if entry[0] == "v" and (best_x is None or
                        (entry[3], entry[4], abs(entry[1])) <
                        (best_x[3], best_x[4], abs(best_x[1]))):
                    best_x = entry
                elif entry[0] == "h" and (best_y is None or
                        (entry[3], entry[4], abs(entry[1])) <
                        (best_y[3], best_y[4], abs(best_y[1]))):
                    best_y = entry
        if settings.get("snap_grid", False):
            grid = max(5, int(settings.get("grid_size", 50)))
            if best_x is None and source_x is not None:
                target = round(source_x / grid) * grid
                if abs(target - source_x) <= tolerance:
                    best_x = ("v", target - source_x, target, abs(target - source_x) * zoom, 0, 0)
            if best_y is None and source_y is not None:
                target = round(source_y / grid) * grid
                if abs(target - source_y) <= tolerance:
                    best_y = ("h", target - source_y, target, abs(target - source_y) * zoom, 0, 0)
        dx = best_x[1] if allow_x and best_x is not None else 0.0
        dy = best_y[1] if allow_y and best_y is not None else 0.0
        if dx or dy:
            for item in items:
                item.moveBy(dx, dy)
                place_note(item)
        if dx and best_x is not None:
            self._guides.append(("v", best_x[2], abs(dx) * zoom))
        if dy and best_y is not None:
            self._guides.append(("h", best_y[2], abs(dy) * zoom))
        self.viewport().update()

    def apply_snapping(self, items, allow_x=True, allow_y=True, active=False, cursor=None):
        """Align the combined selection while Ctrl explicitly enables snapping."""
        self._guides = []
        settings = self.win.settings
        if (not active or not items or
                not (settings.get("snap_enabled", True) or settings.get("snap_grid", False))):
            self.viewport().update()
            return
        if cursor is not None:
            self.apply_cursor_snapping(items, allow_x, allow_y, cursor)
            return
        rect = QRectF()
        for item in items:
            rect = rect.united(self.win.full_rect(item))
        if rect.isNull() or rect.isEmpty():
            return
        best_x = best_y = None
        if settings.get("snap_enabled", True):
            selected = set(items)
            others = [item for item in self.win.top_items() if item not in selected]
            xs = (rect.left(), rect.center().x(), rect.right())
            ys = (rect.top(), rect.center().y(), rect.bottom())
            for other in others:
                target_rect = self.win.full_rect(other)
                for value in xs:
                    for target in (target_rect.left(), target_rect.center().x(), target_rect.right()):
                        delta = target - value
                        if best_x is None or abs(delta) < abs(best_x[0]):
                            best_x = (delta, target)
                for value in ys:
                    for target in (target_rect.top(), target_rect.center().y(), target_rect.bottom()):
                        delta = target - value
                        if best_y is None or abs(delta) < abs(best_y[0]):
                            best_y = (delta, target)
        tolerance = float(settings.get("snap_tolerance", 10)) / max(0.01, self.cur_zoom())
        snap_x = best_x is not None and abs(best_x[0]) <= tolerance
        snap_y = best_y is not None and abs(best_y[0]) <= tolerance
        dx = best_x[0] if snap_x else 0.0
        dy = best_y[0] if snap_y else 0.0
        grid_size = max(5, int(settings.get("grid_size", 50)))
        if settings.get("snap_grid", False):
            if not snap_x:
                target = round(rect.left() / grid_size) * grid_size
                dx = target - rect.left()
                best_x, snap_x = (dx, target), True
            if not snap_y:
                target = round(rect.top() / grid_size) * grid_size
                dy = target - rect.top()
                best_y, snap_y = (dy, target), True
        if not allow_x:
            dx, snap_x = 0.0, False
        if not allow_y:
            dy, snap_y = 0.0, False
        if dx or dy:
            for item in items:
                item.moveBy(dx, dy)
                place_note(item)
        if snap_x and best_x:
            self._guides.append(("v", best_x[1], abs(dx) * self.cur_zoom()))
        if snap_y and best_y:
            self._guides.append(("h", best_y[1], abs(dy) * self.cur_zoom()))
        self.viewport().update()

    @staticmethod
    def _scale_factor(transform, point):
        """Measure side-handle drags on their axis, not by radial mouse distance."""
        start, pivot = transform["start"], transform["pivot"]
        sx, sy = start.x() - pivot.x(), start.y() - pivot.y()
        dx, dy = point.x() - pivot.x(), point.y() - pivot.y()
        handle = transform.get("handle")
        if handle in (4, 5):
            return dx / sx if abs(sx) > 1e-9 else 1.0
        if handle in (6, 7):
            return dy / sy if abs(sy) > 1e-9 else 1.0
        distance = math.hypot(sx, sy)
        if distance < 1e-9:
            return 1.0
        if handle is None:
            return math.hypot(dx, dy) / distance
        # Corner scaling follows the initial diagonal and ignores perpendicular
        # pointer jitter instead of changing size whenever the mouse moves aside.
        return (dx * sx + dy * sy) / (distance * distance)

    def snap_scale(self, transform, factor, cursor=None):
        """Snap the active edge, preferring the target nearest the cursor."""
        self._guides = []
        settings = self.win.settings
        if not (settings.get("snap_enabled", True) or settings.get("snap_grid", False)):
            return factor
        rect = transform.get("rect")
        if rect is None or rect.isNull() or rect.isEmpty():
            return factor
        pivot = transform["pivot"]
        handle = transform.get("handle")
        cursor_led = cursor is not None and (handle is None or handle in (0, 1, 2, 3))
        zoom = max(0.01, self.cur_zoom())
        tolerance_px = float(settings.get("snap_tolerance", 10))
        tolerance = tolerance_px / zoom
        cursor_radius = max(64.0, tolerance_px * 3.0)
        object_candidates = []
        grid_candidates = []

        def interval_distance(value, lower, upper):
            if value < lower:
                return lower - value
            if value > upper:
                return value - upper
            return 0.0

        def consider(source, target, axis, bounds=None):
            origin = pivot.x() if axis == "v" else pivot.y()
            distance = source - origin
            if abs(distance) < 1e-9:
                return
            candidate = (target - origin) / distance
            if candidate < 0.01:
                return
            if cursor_led and bounds is not None:
                # Corner and S object snapping targets the line under the pointer.
                # Grid snapping keeps the stable raw-edge error used by side handles.
                pointer = cursor.x() if axis == "v" else cursor.y()
                error = target - pointer
            else:
                error = (candidate - factor) * distance
            if abs(error) > tolerance:
                return
            cursor_distance = 0.0
            object_distance = 0.0
            if cursor is not None and bounds is not None:
                # The axis distance chooses an edge; the two-dimensional rect
                # distance chooses the object and rejects distant guide owners.
                if axis == "v":
                    cursor_distance = abs(cursor.x() - target) * zoom
                else:
                    cursor_distance = abs(cursor.y() - target) * zoom
                dx = interval_distance(cursor.x(), bounds.left(), bounds.right())
                dy = interval_distance(cursor.y(), bounds.top(), bounds.bottom())
                object_distance = math.hypot(dx, dy) * zoom
            center_target = bool(bounds is not None and (
                (axis == "v" and target == bounds.center().x()) or
                (axis == "h" and target == bounds.center().y())))
            entry = (error, candidate, target, axis, cursor_distance, object_distance,
                     int(center_target))
            (grid_candidates if bounds is None else object_candidates).append(entry)

        free_scale = handle is None
        if free_scale and cursor is not None:
            # S keeps its normal center pivot. Only the snapping source is a
            # virtual handle nearest the pointer on the scaled selection rect.
            left = pivot.x() + (rect.left() - pivot.x()) * factor
            right = pivot.x() + (rect.right() - pivot.x()) * factor
            top = pivot.y() + (rect.top() - pivot.y()) * factor
            bottom = pivot.y() + (rect.bottom() - pivot.y()) * factor
            center_x = pivot.x() + (rect.center().x() - pivot.x()) * factor
            center_y = pivot.y() + (rect.center().y() - pivot.y()) * factor
            virtual = (
                (0, left, top), (1, right, top), (2, left, bottom), (3, right, bottom),
                (4, left, center_y), (5, right, center_y),
                (6, center_x, top), (7, center_x, bottom),
            )
            handle = min(virtual, key=lambda entry: (
                (cursor.x() - entry[1]) ** 2 + (cursor.y() - entry[2]) ** 2))[0]
        if handle is None:
            xs = (rect.left(), rect.center().x(), rect.right())
            ys = (rect.top(), rect.center().y(), rect.bottom())
        else:
            source_x = {
                0: rect.left(), 1: rect.right(), 2: rect.left(), 3: rect.right(),
                4: rect.left(), 5: rect.right(),
            }.get(handle)
            source_y = {
                0: rect.top(), 1: rect.top(), 2: rect.bottom(), 3: rect.bottom(),
                6: rect.top(), 7: rect.bottom(),
            }.get(handle)
            xs = () if source_x is None else (source_x,)
            ys = () if source_y is None else (source_y,)
        if settings.get("snap_enabled", True):
            selected = {entry[0] for entry in transform["orig"]}
            for other in self.win.top_items():
                if other in selected:
                    continue
                target = self.win.full_rect(other)
                for source in xs:
                    for value in (target.left(), target.center().x(), target.right()):
                        consider(source, value, "v", target)
                for source in ys:
                    for value in (target.top(), target.center().y(), target.bottom()):
                        consider(source, value, "h", target)
        if settings.get("snap_grid", False):
            grid = max(5, int(settings.get("grid_size", 50)))
            for axis, values, origin in (("v", xs, pivot.x()), ("h", ys, pivot.y())):
                for source in values:
                    current = origin + (source - origin) * factor
                    consider(source, round(current / grid) * grid, axis)

        def closest_correction(entries):
            best_error = min(abs(entry[0]) for entry in entries)
            near_best = [entry for entry in entries
                         if abs(entry[0]) <= best_error + 0.75 / zoom]
            return min(near_best, key=lambda entry: (entry[4], abs(entry[0])))

        chosen = None
        candidates = object_candidates + grid_candidates
        if cursor is not None:
            # First select the object near the pointer, not an infinite guide
            # from another part of the canvas. A generous radius still permits
            # corner snapping when the pointer is just outside the target rect.
            nearby = [entry for entry in object_candidates if entry[5] <= cursor_radius]
            if nearby:
                nearest = min(entry[5] for entry in nearby)
                target_candidates = [entry for entry in nearby if entry[5] <= nearest + 6.0]
                if cursor_led:
                    start = transform.get("start")
                    preferred_axis = None
                    if start is not None:
                        movement_x = abs(cursor.x() - start.x())
                        movement_y = abs(cursor.y() - start.y())
                        preferred_axis = "v" if movement_x >= movement_y else "h"
                    chosen = min(target_candidates, key=lambda entry: (
                        entry[4], entry[6],
                        0 if preferred_axis is None or entry[3] == preferred_axis else 1,
                        abs(entry[0])))
                else:
                    chosen = closest_correction(target_candidates)
            elif grid_candidates:
                chosen = min(grid_candidates, key=lambda entry: abs(entry[0]))
        elif candidates:
            chosen = closest_correction(candidates)
        if chosen is not None:
            error, factor, target, axis, _, _, _ = chosen
            self._guides.append((axis, target, abs(error) * zoom))
        self.viewport().update()
        return factor

    def snap_rotate(self, transform, angle, cursor):
        """Rotate the active virtual handle toward a cursor-selected target point."""
        settings = self.win.settings
        if not (settings.get("snap_enabled", True) or settings.get("snap_grid", False)):
            return angle
        rect = transform.get("rect")
        if rect is None or rect.isNull() or rect.isEmpty():
            return angle
        pivot = transform["pivot"]
        zoom = max(0.01, self.cur_zoom())
        tolerance = float(settings.get("snap_tolerance", 10)) / zoom
        cursor_radius = max(64.0, float(settings.get("snap_tolerance", 10)) * 3.0)
        handles = handle_positions(rect)
        start = transform.get("start", cursor)
        index = min(range(len(handles)), key=lambda i: (
            (start.x() - handles[i][0].x()) ** 2 +
            (start.y() - handles[i][0].y()) ** 2))
        source = handles[index][0]
        source_angle = math.atan2(source.y() - pivot.y(), source.x() - pivot.x())
        candidates = []
        selected = {entry[0] for entry in transform["orig"]}
        if settings.get("snap_enabled", True):
            for other in self.win.top_items():
                if other in selected:
                    continue
                bounds = self.win.full_rect(other)
                targets = (
                    (bounds.topLeft(), 0),
                    (QPointF(bounds.center().x(), bounds.top()), 0),
                    (bounds.topRight(), 0),
                    (QPointF(bounds.left(), bounds.center().y()), 0),
                    (QPointF(bounds.right(), bounds.center().y()), 0),
                    (bounds.bottomLeft(), 0),
                    (QPointF(bounds.center().x(), bounds.bottom()), 0),
                    (bounds.bottomRight(), 0),
                    (bounds.center(), 1),
                )
                dx = max(bounds.left() - cursor.x(), 0.0, cursor.x() - bounds.right())
                dy = max(bounds.top() - cursor.y(), 0.0, cursor.y() - bounds.bottom())
                object_distance = math.hypot(dx, dy) * zoom
                for target, center in targets:
                    error = math.hypot(cursor.x() - target.x(),
                                       cursor.y() - target.y())
                    if error > tolerance:
                        continue
                    target_angle = math.atan2(target.y() - pivot.y(),
                                              target.x() - pivot.x())
                    desired = math.degrees(target_angle - source_angle)
                    correction = (desired - angle + 180) % 360 - 180
                    candidate = angle + correction
                    candidates.append((candidate, target, error * zoom, center,
                                       object_distance, correction))
        nearby = [entry for entry in candidates if entry[4] <= cursor_radius]
        if nearby:
            nearest = min(entry[4] for entry in nearby)
            targets = [entry for entry in nearby if entry[4] <= nearest + 6.0]
            chosen = min(targets, key=lambda entry: (
                entry[2], entry[3], abs(entry[5])))
            angle, target, distance = chosen[0], chosen[1], chosen[2]
            self._guides.append(("ray", (QPointF(pivot), QPointF(target)), distance))
        elif settings.get("snap_grid", False):
            angle = round(angle / 15) * 15
        self.viewport().update()
        return angle

    # ---------- handles ----------
    def handle_at(self, vp):
        selected = self.win.transformable_selected()
        if len(selected) > 1:
            # One temporary screen-aligned box controls the whole selection.
            rect = self.selection_scene_rect()
            for index, (point, anchor) in enumerate(handle_positions(rect)):
                c = QPointF(self.mapFromScene(point))
                if abs(c.x() - vp.x()) <= HANDLE + 2 and abs(c.y() - vp.y()) <= HANDLE + 2:
                    return selected, anchor, index
            return None
        for it in selected:
            r = handle_rect(it)
            for index, (point, anchor) in enumerate(handle_positions(r)):
                c = QPointF(self.mapFromScene(it.mapToScene(point)))
                if abs(c.x() - vp.x()) <= HANDLE + 2 and abs(c.y() - vp.y()) <= HANDLE + 2:
                    return [it], it.mapToScene(anchor), index
        return None

    # ---------- independent frame borders ----------
    def resize_start(self, item, handle, vp_pos):
        if is_locked(item):
            self.toast.show_text("Locked — Ctrl+L to unlock", 1500)
            return
        if self._tf:
            self.tf_end(False, quiet=True)
        self._resize = {"item": item, "handle": handle,
                        "rect": QRectF(handle_rect(item)), "pos": QPointF(item.pos())}
        self._show_cursor_glyph(self._scale_glyph_kind(handle), vp_pos)
        self.resize_update(vp_pos)

    @staticmethod
    def apply_resized_rect(state, rect):
        item = state["item"]
        if item.kind == "text":
            # Text starts at local (0,0), so compensate a moved top/left border
            # in parent space just as a movable frame border would behave.
            original = state["rect"]
            delta = (item.mapToParent(rect.topLeft())
                     - item.mapToParent(original.topLeft()))
            item.setPos(state["pos"] + delta)
            item.set_frame_size(rect.width(), rect.height())
        else:
            item.set_frame_rect(rect)

    def resize_update(self, vp_pos):
        state = self._resize
        if not state:
            return
        self._show_cursor_glyph(self._scale_glyph_kind(state["handle"]), vp_pos)
        item, handle, original = state["item"], state["handle"], state["rect"]
        point = item.mapFromScene(self.mapToScene(vp_pos.toPoint()))
        if item.kind == "text":
            # Keep pointer coordinates tied to the original frame even after a
            # top/left drag has moved the TextItem's origin.
            point -= item.mapFromParent(state["pos"])
        rect = QRectF(original)
        minimum = 20.0 / max(0.01, item.scale())
        # handle_positions: TL TR BL BR L R T B. The opposite border stays fixed.
        if handle in (0, 2, 4):
            rect.setLeft(min(point.x(), original.right() - minimum))
        if handle in (1, 3, 5):
            rect.setRight(max(point.x(), original.left() + minimum))
        if handle in (0, 1, 6):
            rect.setTop(min(point.y(), original.bottom() - minimum))
        if handle in (2, 3, 7):
            rect.setBottom(max(point.y(), original.top() + minimum))
        self.apply_resized_rect(state, rect)
        label = "Text" if item.kind == "text" else "Frame"
        self.toast.show_text(f"{label}: {rect.width():.0f} × {rect.height():.0f} • release to apply", 1000)

    def resize_end(self, confirm):
        state, self._resize = self._resize, None
        if not state:
            return
        if not confirm:
            self.apply_resized_rect(state, state["rect"])
        else:
            self.win.record_history()
        self._restore_cursor()

    # ---------- transforms ----------
    def tf_start(self, mode, items, pivot=None, drag=False, vp_pos=None, cursor=None,
                 handle=None):
        # Locked objects never enter a transform, no matter which path started it:
        # handles, G/R/S, stroke drag or a menu command.
        items = [i for i in items if not is_locked(i)]
        if not items:
            self.toast.show_text("Locked — Ctrl+L to unlock", 1500)
            return
        if self._tf:
            self.tf_end(False, quiet=True)
        vp = self.cursor_vp() if vp_pos is None else QPointF(vp_pos)
        r = QRectF()
        for i in items:
            r = r.united(self.win.full_rect(i))
        start = self.to_scene(vp)
        if pivot is None:
            pivot = r.center()
        self._op, self._rpending = None, None
        self._native_drag = None
        self.clear_guides()
        self._tf = {"mode": mode, "drag": drag, "axis": None,
                    "pivot": QPointF(pivot), "start": start, "rect": QRectF(r),
                    "handle": handle,
                    "orig": [(i, QPointF(i.pos()), i.rotation(), i.scale()) for i in items]}
        self._show_cursor_glyph(self._transform_glyph_kind(self._tf), vp)
        self.tf_update(vp, QApplication.keyboardModifiers())

    def tf_update(self, vp, mods):
        t = self._tf
        if not t:
            return
        snap = bool(mods & Qt.ControlModifier)
        m, s0, pv = self.to_scene(vp), t["start"], t["pivot"]
        if t["mode"] != "move":
            self.clear_guides()
        if t["mode"] == "move":
            dx, dy = m.x() - s0.x(), m.y() - s0.y()
            if t["axis"] == "x":
                dy = 0.0
            elif t["axis"] == "y":
                dx = 0.0
            for i, p, r, s in t["orig"]:
                i.setPos(p.x() + dx, p.y() + dy)
            moved = [entry[0] for entry in t["orig"]]
            self.apply_snapping(moved, allow_x=t["axis"] != "y",
                                allow_y=t["axis"] != "x", active=snap, cursor=m)
            axis = f" (axis {t['axis'].upper()})" if t["axis"] else ""
            msg = f"Move: {dx:.0f}, {dy:.0f}{axis}"
            keys = "X / Y axis, Ctrl snap"
        elif t["mode"] == "scale":
            f = max(self._scale_factor(t, m), 0.01)
            if snap:
                f = self.snap_scale(t, f, m)
            for i, p, r, s in t["orig"]:
                i.setScale(s * f)
                i.setPos(pv.x() + (p.x() - pv.x()) * f, pv.y() + (p.y() - pv.y()) * f)
                place_note(i)
            msg = f"Scale: {f:.2f}x"
            keys = "Ctrl object/grid snap"
        else:
            a = math.degrees(math.atan2(m.y() - pv.y(), m.x() - pv.x())
                             - math.atan2(s0.y() - pv.y(), s0.x() - pv.x()))
            a = (a + 180) % 360 - 180
            if snap:
                # Ctrl always quantizes rotation, independently of object/grid snapping.
                a = round(a / 15.0) * 15.0
            c, sn = math.cos(math.radians(a)), math.sin(math.radians(a))
            for i, p, r, s in t["orig"]:
                vx, vy = p.x() - pv.x(), p.y() - pv.y()
                i.setRotation(r + a)
                i.setPos(pv.x() + vx * c - vy * sn, pv.y() + vx * sn + vy * c)
            msg = f"Rotate: {a:.0f}\u00b0"
            keys = "Ctrl 15\u00b0 steps"
        self._show_cursor_glyph("snap" if snap else self._transform_glyph_kind(t), vp)
        tail = ("release to finish" if t["drag"]
                else "LMB / Enter confirm  \u2022  RMB / Esc cancel")
        self.toast.show_text(f"{msg}   \u2022   {keys}   \u2022   {tail}", 4000)

    def tf_end(self, confirm, quiet=False):
        self.clear_guides()
        t, self._tf = self._tf, None
        if not t:
            return
        if not confirm:
            for i, p, r, s in t["orig"]:
                i.setPos(p)
                i.setRotation(r)
                i.setScale(s)
                place_note(i)
        for i, *_ in t["orig"]:
            if i.kind == "group":
                i.refresh()
        if not quiet:
            self.toast.show_text("Done" if confirm else "Cancelled", 700)
        self._restore_cursor()
        if confirm:
            self.win.record_history()