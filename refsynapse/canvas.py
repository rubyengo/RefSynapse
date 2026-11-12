"""Infinite canvas: events, hit-testing and drawing.

Navigation, transforms, crop and tools live in their own mixins.
"""
import math
import os

from PySide6.QtCore import Qt, QRectF, QPointF, QPoint, QLineF, QEvent, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsView, QFrame, QLabel, QApplication

from .config import (APP_DIR, HANDLE, HINT, HOLD_KEYS, nav_match, nav_is_hold, mods_of,
                     physical_key, ev, button_name, model_gesture)
from .items import NoteItem, TextItem, handle_positions
from .item_tools import is_locked
from .widgets import Toast, MiniMap, CanvasSearch, VideoControls, CropScissors, CursorGlyph
from .canvas_helpers import edge_cursor, handle_cursor
from .canvas_navigation import NavigationMixin
from .canvas_transforms import TransformsMixin
from .canvas_crop import CropMixin
from .canvas_tools import ToolsMixin


class LockBadge(QLabel):
    """A padlock overlay pinned to a locked object's corner.

    Rendered once into an isolated pixmap and shown as a real viewport widget,
    exactly like the cursor glyphs, so it never depends on the scene painter.
    """
    SIZE = 34

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFixedSize(self.SIZE, self.SIZE)
        s = float(self.SIZE)
        pm = QPixmap(self.SIZE, self.SIZE)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        # Dark plate with a light rim: readable on both white and black art.
        p.setPen(QPen(QColor(255, 255, 255, 110), 1.4))
        p.setBrush(QColor(15, 15, 18, 220))
        p.drawRoundedRect(QRectF(1, 1, s - 2, s - 2), 9, 9)
        # Shackle.
        p.setPen(QPen(QColor("#ffca28"), 3.2))
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(s * 0.30, s * 0.13, s * 0.40, s * 0.46), 0, 180 * 16)
        # Body.
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffca28"))
        p.drawRoundedRect(QRectF(s * 0.22, s * 0.40, s * 0.56, s * 0.42), 4, 4)
        # Keyhole.
        p.setBrush(QColor(15, 15, 18, 240))
        cx, cy = s * 0.5, s * 0.55
        p.drawEllipse(QPointF(cx, cy), 2.3, 2.3)
        p.drawRect(QRectF(cx - 1.1, cy, 2.2, s * 0.15))
        p.end()
        self.setPixmap(pm)
        self.hide()


class Canvas(NavigationMixin, TransformsMixin, CropMixin, ToolsMixin, QGraphicsView):
    EDGE = 6

    def __init__(self, scene, win):
        super().__init__(scene)
        self.win = win
        self._op = None            # active navigation drag: pan / zoom
        self._op_btn = None
        self._start = QPointF()
        self._last = QPointF()
        self._rpending = None      # right button: menu on click, navigation on drag
        self._held = None          # held key: space / z
        self._held_used = False
        self._hover_cur = False
        self._tf = None            # active transform (move / rotate / scale)
        self._resize = None        # active independent Frame border resize
        self._crop = None          # active non-destructive image crop
        self._draw_hold = False    # D is held: draw continuously, Select on release
        self.tool = "select"
        self._stroke = None
        self._text_band = None
        self._text_start_vp = QPoint()
        self._editing_text = None
        self._frame_band = None
        self._frame_start_vp = QPoint()
        self._select_band = None
        self._select_start_vp = QPoint()
        self._select_mode = "add"
        self._native_drag = None   # native Qt move supplemented with snapping
        self._model_drag = None    # Alt+drag orbit on a 3D reference
        self._dragout = None       # Ctrl+Shift+drag: hand files to another app
        self._guides = []          # active alignment guides in scene coordinates
        self._lock_badges = {}     # uid -> LockBadge overlay widget
        self.setFrameShape(QFrame.NoFrame)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        # Partial viewport updates leave trails behind transformed pixmap items.
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.setBackgroundBrush(QColor("#2b2b2b"))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.toast = Toast(self)
        self.project_label = QLabel("Untitled", self)
        self.project_label.setObjectName("canvasProjectTitle")
        self.project_label.setAlignment(Qt.AlignCenter)
        self.project_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.project_label.show()
        self.minimap = MiniMap(self)
        self.minimap.show()
        self.search = CanvasSearch(self)
        self.video_controls = VideoControls(self)
        self.crop_scissors = CropScissors(self)
        self.cursor_glyphs = {
            "pan": CursorGlyph(self, "pan_directions_ghost.svg"),
            "move": CursorGlyph(self, "move_arrows_ghost.svg"),
            "snap": CursorGlyph(self, "snap_magnet_ghost.svg"),
            "scale_center": CursorGlyph(self, "scale_center_ghost.svg"),
            "scale_diagonal": CursorGlyph(self, "scale_diagonal_ghost.svg"),
            "scale_diagonal_left": CursorGlyph(self, "scale_diagonal_left_ghost.svg"),
            "scale_horizontal": CursorGlyph(self, "scale_horizontal_ghost.svg"),
            "scale_vertical": CursorGlyph(self, "scale_vertical_ghost.svg"),
            "rotate_cycle": CursorGlyph(self, "rotate_cycle_ghost.svg"),
            "draw": CursorGlyph(self, "draw_marker_ghost.svg"),
            "zoom_in": CursorGlyph(self, "zoom_in_ghost.svg"),
            "zoom_out": CursorGlyph(self, "zoom_out_ghost.svg"),
            "pdf_scroll": CursorGlyph(self, "pdf_scroll_ghost.svg"),
        }
        self._media_timer = QTimer(self)
        self._media_timer.setSingleShot(True)
        self._media_timer.setInterval(80)
        self._media_timer.timeout.connect(self.win.update_media_visibility)
        scene.changed.connect(self._scene_changed)
        self.position_project_label()
        self.position_minimap()
        self.position_search()
        self.position_video_controls()

    # ---------- overlays positioning ----------
    def set_project_name(self, name):
        self.project_label.setText(name)
        self.project_label.setToolTip(name)
        self.position_project_label()

    def position_project_label(self):
        if not hasattr(self, "project_label"):
            return
        self.project_label.setMaximumWidth(max(120, self.viewport().width() - 40))
        self.project_label.adjustSize()
        x = (self.viewport().width() - self.project_label.width()) // 2
        self.project_label.move(max(10, x), 12)
        self.project_label.raise_()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.position_project_label()
        self.position_minimap()
        self.position_search()
        self.position_video_controls()
        if hasattr(self, "crop_scissors"):
            self.crop_scissors.hide()
        self._hide_cursor_glyphs()

    def position_search(self):
        if hasattr(self, "search"):
            width = round(min(820, max(360, self.viewport().width() - 48)) * 0.8)
            self.search.setFixedWidth(width)
            centered_x = (self.viewport().width() - width) // 2
            x = min(self.viewport().width() - width - 12, centered_x + 56)
            self.search.move(max(12, x),
                             max(8, self.viewport().height() - self.search.height() - 24))
            self.search.raise_()

    def position_video_controls(self):
        if not hasattr(self, "video_controls"):
            return
        controls = self.video_controls
        video = controls.video
        if video is None or video.scene() is not self.scene():
            return
        # Keep the original video-local placement: centered on the video, near
        # its bottom edge. Only the panel's width is independent of scene zoom.
        video_rect = self.mapFromScene(self.win.full_rect(video)).boundingRect()
        if (video_rect.width() < controls.MIN_WIDTH + 12 or
                video_rect.height() < controls.height() + 16):
            controls.hide()
            return
        width = min(controls.PREFERRED_WIDTH, math.floor(video_rect.width()) - 12)
        controls.update_compact_layout(width)
        controls.setFixedWidth(width)
        x = round(video_rect.center().x() - width / 2)
        y = round(video_rect.bottom() - controls.height() - 8)
        y = max(round(video_rect.top()) + 8, y)
        x = max(round(video_rect.left()) + 6,
                min(x, round(video_rect.right()) - width - 5))
        controls.move(x, y)
        controls.show()
        controls.raise_()

    def _scene_changed(self, *_):
        self.minimap.schedule()
        self.position_video_controls()

    def show_search(self):
        self.search.show_panel()

    def position_minimap(self):
        if hasattr(self, "minimap"):
            self.minimap.move(max(8, self.viewport().width() - self.minimap.width() - 14),
                              max(8, self.viewport().height() - self.minimap.height() - 14))
            self.minimap.raise_()

    def schedule_media_update(self):
        self._media_timer.start()
        self.minimap.schedule()
        self.position_video_controls()

    # ---------- window edges & hit-testing ----------
    def edges_at(self, pos):
        w = self.win
        if not w.frameless or w.isMaximized() or w.isFullScreen():
            return None
        r, m, E = self.viewport().rect(), self.EDGE, Qt.Edge
        e = None
        for cond, flag in ((pos.x() <= m, E.LeftEdge), (pos.x() >= r.width() - m, E.RightEdge),
                           (pos.y() <= m, E.TopEdge), (pos.y() >= r.height() - m, E.BottomEdge)):
            if cond:
                e = flag if e is None else e | flag
        return e

    def model_item_at(self, vp):
        item = self.itemAt(vp.toPoint())
        while item is not None:
            if getattr(item, "kind", None) == "model3d":
                return item
            item = item.parentItem()
        return None

    def pdf_item_at(self, vp):
        item = self.itemAt(vp.toPoint())
        while item is not None and getattr(item, "kind", None) != "document":
            item = item.parentItem()
        return item if item is not None and item.document_kind == "pdf" else None

    @staticmethod
    def model_input_active(model):
        """Capture 3D navigation only after the model has been selected with LMB."""
        return model is not None and model.topLevelItem().isSelected()

    def selection_item_at(self, vp):
        """Prefer a usable object below a stroke, but keep strokes selectable alone."""
        candidates = []
        for item in self.items(vp.toPoint()):
            top = item.topLevelItem()
            if top in self.win.objects and top not in candidates:
                candidates.append(top)
        if candidates:
            return next((item for item in candidates if item.kind != "stroke"), candidates[0])
        return self.stroke_item_at(vp)

    @staticmethod
    def point_segment_distance(point, start, end):
        vx, vy = end.x() - start.x(), end.y() - start.y()
        length2 = vx * vx + vy * vy
        if length2 < 1e-9:
            return math.hypot(point.x() - start.x(), point.y() - start.y())
        t = ((point.x() - start.x()) * vx + (point.y() - start.y()) * vy) / length2
        t = max(0.0, min(1.0, t))
        return math.hypot(point.x() - start.x() - t * vx,
                          point.y() - start.y() - t * vy)

    def stroke_item_at(self, vp):
        """Reliable screen-space hit-test with a forgiving click tolerance."""
        point = QPointF(vp)
        hits = []
        for item in self.win.objects:
            if item.kind != "stroke" or not item.isVisible():
                continue
            path = item.path()
            points = [QPointF(self.mapFromScene(item.mapToScene(
                QPointF(path.elementAt(i).x, path.elementAt(i).y))))
                for i in range(path.elementCount())]
            if not points:
                continue
            tolerance = max(9.0, item.width * item.scale() * self.cur_zoom() / 2 + 6.0)
            distance = min((self.point_segment_distance(point, points[i], points[i + 1])
                            for i in range(len(points) - 1)),
                           default=self.point_segment_distance(point, points[0], points[0]))
            if distance <= tolerance:
                hits.append((item.topLevelItem(), distance))
        if not hits:
            return None
        # Topmost ink wins; distance breaks ties between strokes at one Z level.
        return max(hits, key=lambda hit: (hit[0].zValue(), -hit[1]))[0]

    # ---------- keyboard ----------
    def _hold_name(self, e):
        key = physical_key(e)
        code = ord(key) if len(key) == 1 and key.isalpha() else ev(e.key())
        name = HOLD_KEYS.get(code)
        return name if nav_is_hold(self.win.nav, name, mods_of(e.modifiers())) else None

    def event(self, e):
        # During a transform all keys go to keyPressEvent; Space/Z go here when used for navigation
        if e.type() == QEvent.Type.ShortcutOverride and (self._tf or self._hold_name(e)):
            e.accept()
            return True
        return super().event(e)

    def keyPressEvent(self, e):
        if self._resize:
            if ev(e.key()) == ev(Qt.Key_Escape):
                self.resize_end(False)
            e.accept()
            return
        # Held navigation keys (Space/Z in the Photoshop preset) must continue
        # to work while the crop overlay is active.
        name = self._hold_name(e)
        if name and not self._tf:
            self.crop_scissors.hide()
            if not e.isAutoRepeat():
                self._held, self._held_used = name, False
                self._restore_cursor()
            e.accept()
            return
        if self._crop:
            if ev(e.key()) == ev(Qt.Key_Escape):
                self.finish_crop(False)
            elif ev(e.key()) in (ev(Qt.Key_Return), ev(Qt.Key_Enter)):
                self.finish_crop(True)
            elif ev(e.key()) == ev(Qt.Key_Control) and self._crop.get("mode"):
                self.crop_move(self.cursor_vp(), e.modifiers() | Qt.ControlModifier)
            e.accept()
            return
        if self._tf:
            key = physical_key(e)
            k = ord(key) if len(key) == 1 and key.isalpha() else ev(e.key())
            if k == ev(Qt.Key_Escape):
                self.tf_end(False)
            elif k in (ev(Qt.Key_Return), ev(Qt.Key_Enter), ev(Qt.Key_Space)):
                self.tf_end(True)
            elif k == ev(Qt.Key_Control):
                self.tf_update(self.cursor_vp(), e.modifiers() | Qt.ControlModifier)
            elif self._tf["mode"] == "move" and k in (ev(Qt.Key_X), ev(Qt.Key_Y)):
                ax = "x" if k == ev(Qt.Key_X) else "y"
                self._tf["axis"] = None if self._tf["axis"] == ax else ax
                self.tf_update(self.cursor_vp(), e.modifiers())
            elif not self._tf["drag"]:
                mode = self.win.tf_mode_for_key(k)
                if mode and mode != self._tf["mode"]:          # switch G <-> R <-> S
                    items = [o[0] for o in self._tf["orig"]]
                    pivot = self._tf["pivot"]
                    self.tf_end(False, quiet=True)
                    self.tf_start(mode, items, pivot)
            return
        name = self._hold_name(e)
        if name:
            if not e.isAutoRepeat():
                self._held, self._held_used = name, False
                self._restore_cursor()
            return
        if ev(e.key()) == ev(Qt.Key_Control):
            pos = self.cursor_vp()
            if self._native_drag:
                self._show_cursor_glyph("snap", pos)
                e.accept()
                return
            if (not e.modifiers() & Qt.ShiftModifier
                    and self.pdf_item_at(pos) is not None):
                self._show_cursor_glyph("pdf_scroll", pos)
                self._hover_cur = True
                e.accept()
                return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if self._tf:
            if ev(e.key()) == ev(Qt.Key_Control):
                self.tf_update(self.cursor_vp(), Qt.NoModifier)
            return
        if self._crop:
            key = physical_key(e)
            code = ord(key) if len(key) == 1 and key.isalpha() else ev(e.key())
            name = HOLD_KEYS.get(code)
            if name and name == self._held:
                if e.isAutoRepeat():
                    return
                self._held = None
                self._restore_cursor()
                return
            if ev(e.key()) == ev(Qt.Key_Control) and self._crop.get("mode"):
                self.crop_move(self.cursor_vp(), Qt.NoModifier)
            return
        key = physical_key(e)
        code = ord(key) if len(key) == 1 and key.isalpha() else ev(e.key())
        name = HOLD_KEYS.get(code)
        if name and name == self._held:
            if e.isAutoRepeat():
                return
            used = self._held_used
            self._held = None
            self._restore_cursor()
            if not used and name == "space":     # short tap on Space = play/pause
                self.win.act["play"].trigger()
            return
        if ev(e.key()) == ev(Qt.Key_Control) and self._native_drag:
            self._hide_cursor_glyphs()
            self.viewport().unsetCursor()
            e.accept()
            return
        if (ev(e.key()) == ev(Qt.Key_Control)
                and self.cursor_glyphs["pdf_scroll"].isVisible()):
            self.cursor_glyphs["pdf_scroll"].hide()
            self._restore_cursor()
            e.accept()
            return
        super().keyReleaseEvent(e)

    def focusOutEvent(self, e):
        self._held = None
        self._native_drag = None
        self._model_drag = None
        self._dragout = None
        self.clear_guides()
        if self._draw_hold:
            self.end_draw_hold()
        if self._resize:
            self.resize_end(False)
        if self._crop:
            self.finish_crop(False)
        if self._tf:
            self.tf_end(False)
        self._restore_cursor()
        super().focusOutEvent(e)

    # ---------- mouse ----------
    def mousePressEvent(self, e):
        pos, btn = e.position(), button_name(e.button())
        if self._resize:
            if e.button() == Qt.RightButton:
                self.resize_end(False)
            e.accept()
            return
        if self._crop:
            self.crop_scissors.hide()
            if self._op is not None:
                e.accept()
                return
            mods = mods_of(e.modifiers())
            op = nav_match(self.win.nav, btn, mods, self._held) if btn else None
            if op:
                if self._held:
                    self._held_used = True
                if e.button() == Qt.RightButton:
                    # A right-click still cancels Crop; a right-drag follows the
                    # configured navigation binding instead of cancelling it.
                    self._rpending = (op, QPointF(pos))
                else:
                    self.begin_op(op, pos, e.button())
                e.accept()
                return
            if e.button() == Qt.LeftButton and not self._held:
                item = self._crop["item"]
                point = self.crop_point(pos)
                if (self.crop_handle_at(point) is None and
                        not item.full_rect_local().contains(point)):
                    self.finish_crop(True)  # LMB anywhere outside the image = Enter.
                else:
                    self.crop_press(pos)
            elif e.button() == Qt.RightButton:
                self.finish_crop(False)
            e.accept()
            return
        model = self.model_item_at(pos)
        if (model is not None and e.button() == Qt.LeftButton and not self._held and
                not mods_of(e.modifiers()) and
                model.smooth_hit(model.mapFromScene(self.mapToScene(pos.toPoint())))):
            top = model.topLevelItem()
            if top in self.win.objects and not top.isSelected():
                self.scene().clearSelection()
                top.setSelected(True)
            model.set_smooth(not model.smooth)
            self.win.record_history()
            self.win.toast("Smooth shading" if model.smooth else "Flat shading")
            e.accept()
            return
        gesture = (model_gesture(self.win.nav, btn, mods_of(e.modifiers()))
                   if self.model_input_active(model) else None)
        if model is not None and gesture:
            self._model_drag = {"item": model, "last": QPointF(pos),
                                "mode": gesture, "button": e.button()}
            if gesture == "pan":
                self._show_cursor_glyph("pan", pos)
            elif gesture == "zoom":
                self._show_cursor_glyph("zoom_in", pos)
            else:
                self._hide_cursor_glyphs()
                self.viewport().setCursor(Qt.ClosedHandCursor)
            e.accept()
            return
        if e.button() == Qt.LeftButton and self.tool == "draw":
            self._show_cursor_glyph("draw", pos)
            self.begin_stroke(self.mapToScene(pos.toPoint()))
            e.accept()
            return
        if e.button() == Qt.LeftButton and self.tool == "text":
            self.begin_text_band(pos)
            e.accept()
            return
        if e.button() == Qt.LeftButton and self.tool == "frame":
            self.begin_frame_band(pos)
            e.accept()
            return
        if self._tf:
            if e.button() == Qt.RightButton:
                self.tf_end(False)
            elif e.button() == Qt.LeftButton and not self._tf["drag"]:
                self.tf_end(True)
            return
        if self._op is not None:
            return
        mods = mods_of(e.modifiers())
        if e.button() == Qt.LeftButton:
            edges = self.edges_at(pos.toPoint())
            if edges is not None:
                self.win.windowHandle().startSystemResize(edges)
                return
            if not self._held and not mods:
                h = self.handle_at(pos)
                if h:
                    items = h[0]
                    if len(items) == 1 and items[0].kind in ("frame", "text"):
                        self.resize_start(items[0], h[2], pos)
                    else:
                        self.tf_start("scale", items, h[1], drag=True, vp_pos=pos,
                                      cursor=handle_cursor(h[2]), handle=h[2])
                    return
            # Ctrl+Shift+drag hands the objects to another application as files.
            if (self.tool == "select" and not self._held
                    and "ctrl" in mods and "shift" in mods):
                top = self.selection_item_at(pos)
                if top is not None and hasattr(top, "kind"):
                    if not top.isSelected():
                        self.scene().clearSelection()
                        top.setSelected(True)
                    self._dragout = {"item": top, "start": QPointF(pos)}
                    e.accept()
                    return
        op = nav_match(self.win.nav, btn, mods, self._held) if btn else None
        if self._held:
            self._held_used = True
        if op:
            if e.button() == Qt.RightButton:
                self._rpending = (op, QPointF(pos))
            else:
                self.begin_op(op, pos, e.button())
            return
        if e.button() == Qt.LeftButton and self.tool == "select" and mods:
            if "shift" in mods or "ctrl" in mods:
                top = self.selection_item_at(pos)
                if top is not None and hasattr(top, "kind"):
                    top.setSelected("ctrl" not in mods)
                else:
                    self.begin_selection_band(pos, "remove" if "ctrl" in mods else "add")
                e.accept()
                return
        if e.button() == Qt.RightButton:
            self._rpending = (None, QPointF(pos))
            return
        if e.button() != Qt.LeftButton or self._held:
            return
        preferred = self.selection_item_at(pos)
        # A locked object can be selected and inspected, never dragged.
        if preferred is not None and is_locked(preferred):
            if not preferred.isSelected():
                self.scene().clearSelection()
                preferred.setSelected(True)
            self._native_drag = None
            self.toast.show_text("Locked — Ctrl+L to unlock", 1200)
            e.accept()
            return
        stroke = preferred if preferred is not None and preferred.kind == "stroke" else None
        if stroke is None and preferred is None:
            stroke = self.stroke_item_at(pos)
        if stroke is not None:
            # The first press selects a stroke without changing its position.
            # A drag on an already selected stroke starts the move transform.
            if not stroke.isSelected():
                self.scene().clearSelection()
                stroke.setSelected(True)
                e.accept()
                return
            self.tf_start("move", self.win.selected_top(), drag=True, vp_pos=pos)
            return
        # Select first, move second: a press on an unselected object must never
        # become an accidental drag. Qt keeps its reliable native hit-testing and
        # movement for objects that were already selected before this press.
        native_candidate = preferred is not None and preferred.kind != "stroke"
        if native_candidate and not preferred.isSelected():
            self.scene().clearSelection()
            preferred.setSelected(True)
            self._native_drag = None
            e.accept()
            return
        super().mousePressEvent(e)
        if native_candidate:
            self._native_drag = {"items": self.win.transformable_selected(),
                                 "start": self.mapToScene(pos.toPoint())}

    def mouseMoveEvent(self, e):
        pos = e.position()
        if self._dragout is not None:
            if (e.buttons() & Qt.LeftButton and
                    (pos - self._dragout["start"]).manhattanLength()
                    >= QApplication.startDragDistance()):
                item = self._dragout["item"]
                self._dragout = None
                selected = self.win.selected_top()
                items = selected if item in selected and len(selected) > 1 else [item]
                self.win.start_drag_out(self, items)
            e.accept()
            return
        if self._resize:
            if e.buttons() & Qt.LeftButton:
                self.resize_update(pos)
            e.accept()
            return
        if self._model_drag:
            state = self._model_drag
            if e.buttons() & state["button"]:
                last = state["last"]
                dx, dy = pos.x() - last.x(), pos.y() - last.y()
                if state["mode"] == "orbit":
                    state["item"].orbit(dx, dy)
                elif state["mode"] == "pan":
                    state["item"].pan(dx, dy)
                else:
                    speed = float(self.win.nav["zoom_drag_speed"])
                    zoom_factor = math.exp((dx - dy) * 0.007 * speed)
                    state["item"].zoom(zoom_factor)
                    self._show_cursor_glyph("zoom_in" if zoom_factor >= 1.0 else "zoom_out", pos)
                state["last"] = QPointF(pos)
                if state["mode"] == "pan":
                    self._show_cursor_glyph("pan", pos)
            e.accept()
            return
        if self._crop:
            if self._rpending is not None:
                self.crop_scissors.hide()
                op, p0 = self._rpending
                if (pos - p0).manhattanLength() > 4:
                    self._rpending = None
                    if op:
                        self.begin_op(op, p0, Qt.RightButton)
                e.accept()
                return
            if self._op:
                self.crop_scissors.hide()
                self.update_op(pos)
                e.accept()
                return
            if e.buttons() & Qt.LeftButton:
                self.crop_scissors.hide()
                self.crop_move(pos, e.modifiers())
            elif e.buttons() == Qt.NoButton:
                self.update_crop_scissors(pos)
            else:
                self.crop_scissors.hide()
            e.accept()
            return
        if self._select_band and e.buttons() & Qt.LeftButton:
            self.update_selection_band(pos)
            e.accept()
            return
        if self._frame_band and e.buttons() & Qt.LeftButton:
            self.update_frame_band(pos)
            e.accept()
            return
        if self._text_band and e.buttons() & Qt.LeftButton:
            self.update_text_band(pos)
            e.accept()
            return
        if self._stroke and e.buttons() & Qt.LeftButton:
            self._show_cursor_glyph("draw", pos)
            self.extend_stroke(self.mapToScene(pos.toPoint()))
            e.accept()
            return
        if self._tf:
            self.tf_update(pos, e.modifiers())
            return
        if self._rpending is not None:
            op, p0 = self._rpending
            if (pos - p0).manhattanLength() > 4:
                self._rpending = None
                if op:
                    self.begin_op(op, p0, Qt.RightButton)
            return
        if self._op:
            self.update_op(pos)
            return
        if e.buttons() == Qt.NoButton and self._held:
            self._restore_cursor(pos)
            e.accept()
            return
        if (e.buttons() == Qt.NoButton and e.modifiers() & Qt.ControlModifier
                and not e.modifiers() & Qt.ShiftModifier and self.pdf_item_at(pos) is not None):
            self._show_cursor_glyph("pdf_scroll", pos)
            self._hover_cur = True
            e.accept()
            return
        if e.buttons() == Qt.NoButton and self.tool == "draw":
            self._show_cursor_glyph("draw", pos)
            e.accept()
            return
        if e.buttons() == Qt.NoButton:
            self._hide_cursor_glyphs()
            model = self.model_item_at(pos)
            if (model is not None and not self._held and
                    model.smooth_hit(model.mapFromScene(self.mapToScene(pos.toPoint())))):
                self.viewport().setCursor(Qt.PointingHandCursor)
                self._hover_cur = True
                return
            c = edge_cursor(self.edges_at(pos.toPoint()))
            handle = self.handle_at(pos) if c is None and not self._held else None
            if handle:
                self._show_cursor_glyph(self._scale_glyph_kind(handle[2]), pos)
                self._hover_cur = True
                return
            if c is not None:
                self.viewport().setCursor(c)
                self._hover_cur = True
                return
            if self._hover_cur:
                self._hover_cur = False
                self._restore_cursor()
        super().mouseMoveEvent(e)
        if self._native_drag and e.buttons() & Qt.LeftButton:
            snapping = bool(e.modifiers() & Qt.ControlModifier)
            self.apply_snapping(self._native_drag["items"], active=snapping,
                                cursor=self.mapToScene(pos.toPoint()))
            if snapping:
                self._show_cursor_glyph("snap", pos)
            else:
                self._hide_cursor_glyphs()
                self.viewport().unsetCursor()

    def mouseReleaseEvent(self, e):
        if self._dragout is not None:
            self._dragout = None
            e.accept()
            return
        if self._resize:
            if e.button() == Qt.LeftButton:
                self.resize_end(True)
            e.accept()
            return
        if self._model_drag:
            if e.button() == self._model_drag["button"]:
                self._model_drag = None
                self._restore_cursor()
                self.win.record_history()
            e.accept()
            return
        if self._crop:
            if e.button() == Qt.RightButton and self._rpending is not None:
                self._rpending = None
                self.finish_crop(False)
                e.accept()
                return
            if self._op and e.button() == self._op_btn:
                self._op = None
                self._restore_cursor()
                e.accept()
                return
            if e.button() == Qt.LeftButton and self._crop.get("mode"):
                self._crop["mode"] = None
            self.clear_guides()
            if e.button() == Qt.LeftButton:
                self.update_crop_scissors(e.position())
            e.accept()
            return
        if self._select_band and e.button() == Qt.LeftButton:
            self.finish_selection_band(e.position())
            e.accept()
            return
        if self._frame_band and e.button() == Qt.LeftButton:
            self.finish_frame_band(e.position())
            e.accept()
            return
        if self._text_band and e.button() == Qt.LeftButton:
            self.finish_text_band(e.position())
            e.accept()
            return
        if self._stroke and e.button() == Qt.LeftButton:
            self.extend_stroke(self.mapToScene(e.position().toPoint()))
            self.end_stroke()
            e.accept()
            return
        if self._tf:
            if self._tf["drag"] and e.button() == Qt.LeftButton:
                self.tf_end(True, quiet=True)
            return
        if e.button() == Qt.RightButton and self._rpending is not None:
            self._rpending = None
            self.win.show_menu(self.selection_item_at(e.position()), e.globalPosition().toPoint())
            return
        if self._op and e.button() == self._op_btn:
            self._op = None
            self._restore_cursor()
            return
        if (e.button() == Qt.LeftButton and self.tool == "select"
                and self._native_drag is None
                and e.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier)):
            e.accept()
            return
        if e.button() != Qt.LeftButton:
            return
        super().mouseReleaseEvent(e)
        self._native_drag = None
        self.clear_guides()
        self._restore_cursor(e.position())
        self.win.record_history()

    def mouseDoubleClickEvent(self, e):
        if self._crop:
            if e.button() == Qt.LeftButton:
                self.finish_crop(True)
            e.accept()
            return
        if self._tf:
            return
        btn = button_name(e.button())
        if (e.button() != Qt.LeftButton or self._held or
                nav_match(self.win.nav, btn, mods_of(e.modifiers()), None)):
            self.mousePressEvent(e)
            return
        it = self.itemAt(e.position().toPoint())
        if isinstance(it, NoteItem):
            self.win.edit_note(it.parentItem())
            return
        top = self.selection_item_at(e.position())
        if top is None:
            self.win.fit_all()
            return
        model = self.model_item_at(e.position())
        if model is not None:
            model.reset_view()
            self.win.record_history()
            return
        if top.kind == "video":
            top.toggle()
            return
        if isinstance(top, TextItem):
            self.win.begin_text_editing(top)
            return
        super().mouseDoubleClickEvent(e)

    def leaveEvent(self, e):
        self.crop_scissors.hide()
        self._hide_cursor_glyphs()
        super().leaveEvent(e)

    def contextMenuEvent(self, e):
        e.accept()          # the menu is opened by our own right-click logic

    # ---------- drag & drop ----------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasImage() or e.mimeData().hasText():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasImage() or e.mimeData().hasText():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.win.add_mime(e.mimeData(), self.mapToScene(e.position().toPoint()))
        e.acceptProposedAction()

    # ---------- drawing ----------
    def drawBackground(self, p, rect):
        super().drawBackground(p, rect)
        if not self.win.settings.get("grid_visible", False):
            return
        step = max(5, int(self.win.settings.get("grid_size", 50)))
        while step * self.cur_zoom() < 20:
            step *= 2
        if rect.width() / step > 500 or rect.height() / step > 500:
            return
        pen = QPen(QColor("#3a3f46"), 1)
        pen.setCosmetic(True)
        p.setPen(pen)
        x = math.floor(rect.left() / step) * step
        while x <= rect.right():
            p.drawLine(QLineF(x, rect.top(), x, rect.bottom()))
            x += step
        y = math.floor(rect.top() / step) * step
        while y <= rect.bottom():
            p.drawLine(QLineF(rect.left(), y, rect.right(), y))
            y += step

    def draw_snap_guides(self, painter):
        if not self._guides:
            return
        painter.save()
        painter.resetTransform()
        pen = QPen(QColor("#ff5fa2"), 0.7, Qt.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        viewport = self.viewport().rect()
        for index, (axis, position, distance) in enumerate(self._guides):
            if axis == "ray":
                start, end = position
                painter.drawLine(self.mapFromScene(start), self.mapFromScene(end))
            elif axis == "v":
                x = self.mapFromScene(QPointF(position, 0)).x()
                painter.drawLine(x, 0, x, viewport.height())
            else:
                y = self.mapFromScene(QPointF(0, position)).y()
                painter.drawLine(0, y, viewport.width(), y)
            painter.drawText(10, 46 + index * 18, f"Snap distance: {distance:.1f} px")
        painter.restore()

    def update_lock_badges(self):
        """Pin one padlock overlay widget to the corner of each locked object.

        Real viewport widgets (like the cursor glyphs) are independent of the
        scene painter, so the badge is always visible regardless of zoom or
        any foreground-painter quirk. Called on every repaint to stay in sync.
        """
        try:
            locked = {i.uid: i for i in self.win.top_items() if is_locked(i)}
            for uid in list(self._lock_badges):
                if uid not in locked:
                    widget = self._lock_badges.pop(uid)
                    widget.hide()
                    widget.deleteLater()
            if not locked:
                return
            vw, vh = self.viewport().width(), self.viewport().height()
            for uid, item in locked.items():
                widget = self._lock_badges.get(uid)
                if widget is None:
                    widget = LockBadge(self)
                    self._lock_badges[uid] = widget
                rect = self.mapFromScene(self.win.full_rect(item)).boundingRect()
                size = widget.SIZE
                x = max(4, min(round(rect.right() - size - 6), vw - size - 4))
                y = max(4, min(round(rect.top() + 6), vh - size - 4))
                widget.move(x, y)
                # Manual on-screen test; avoids the unreliable QRectF.intersects.
                on_screen = not (rect.right() < 0 or rect.left() > vw or
                                 rect.bottom() < 0 or rect.top() > vh)
                widget.setVisible(on_screen)
                if on_screen:
                    widget.raise_()
        except Exception:
            import traceback
            try:
                with open(os.path.join(APP_DIR, "lock-badge-error.log"), "w",
                          encoding="utf-8") as stream:
                    stream.write(traceback.format_exc())
            except OSError:
                pass

    def drawForeground(self, p, rect):
        if not self.win.objects:
            p.save()
            p.resetTransform()
            p.setPen(QColor("#777777"))
            f = p.font()
            f.setPointSize(12)
            p.setFont(f)
            p.drawText(self.viewport().rect(), Qt.AlignCenter, HINT)
            p.restore()
        self.draw_snap_guides(p)
        self.update_lock_badges()
        if len(self.win.transformable_selected()) < 2:
            return
        # Temporary shared box in viewport coordinates: constant handle size,
        # independent of item types, zoom and individual rotations.
        scene_rect = self.selection_scene_rect()
        if scene_rect.isNull() or scene_rect.isEmpty():
            return
        view_rect = self.mapFromScene(scene_rect).boundingRect()
        p.save()
        p.resetTransform()
        pen = QPen(QColor("#4fc3f7"), 0.75, Qt.DashLine)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(view_rect)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#4fc3f7"))
        for point, _ in handle_positions(view_rect):
            p.drawRect(QRectF(point.x() - HANDLE / 2, point.y() - HANDLE / 2,
                              HANDLE, HANDLE))
        p.restore()