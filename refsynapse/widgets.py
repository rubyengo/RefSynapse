"""Small canvas overlay widgets."""
import os
from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, QEvent, Signal
from PySide6.QtGui import (QPainter, QColor, QPen, QPolygonF, QImage, QPixmap,
                           QLinearGradient, QBrush)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QLabel, QWidget, QHBoxLayout, QVBoxLayout,
                               QLineEdit, QPushButton)
from .items import walk, note_text

# =====================================================================
#  Shared badge palette: dark plate, light rim, amber glyph.
#  Every canvas badge (padlock, cursor glyphs, crop scissors) uses it.
# =====================================================================
BADGE_PLATE = QColor(15, 15, 18, 220)
BADGE_RIM = QColor(255, 255, 255, 110)
BADGE_ACCENT = QColor("#ffca28")
BADGE_HOLE = QColor(15, 15, 18, 240)
BADGE_RIM_WIDTH = 1.4

LOCK_BADGE_SIZE = 27        # was 34: 20% smaller
LOCK_BASE = 34.0            # the padlock geometry below was tuned for this size

# Video transport icons are plain white, like a classic player bar.
TRANSPORT_ICON = QColor("#f2f2f2")
# YouTube-style scrubber: red -> pink played gradient, red playhead.
SEEK_PLAYED_A = QColor("#ff3355")
SEEK_PLAYED_B = QColor("#ff0050")
SEEK_HEAD = QColor("#ff0050")


def paint_badge_plate(painter, rect, radius=None, inset=1.0):
    """Dark plate with a light rim; round when radius is None."""
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(QPen(BADGE_RIM, BADGE_RIM_WIDTH))
    painter.setBrush(BADGE_PLATE)
    r = QRectF(rect).adjusted(inset, inset, -inset, -inset)
    if radius is None:
        painter.drawEllipse(r)
    else:
        painter.drawRoundedRect(r, radius, radius)
    painter.restore()


def tinted_glyph(renderer, size, color=BADGE_ACCENT, dpr=1.0):
    """Render an SVG once and repaint every opaque pixel in the accent color.

    SourceIn keeps the vector alpha (antialiasing) and replaces the artwork's
    own colors, so the ghost assets themselves need no editing.
    """
    side = max(1, round(size * dpr))
    image = QImage(side, side, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    renderer.render(p, QRectF(0, 0, side, side))
    p.setCompositionMode(QPainter.CompositionMode_SourceIn)
    p.fillRect(image.rect(), color)
    p.end()
    pm = QPixmap.fromImage(image)
    pm.setDevicePixelRatio(dpr)
    return pm


def paint_lock_badge(painter, x, y, size=LOCK_BADGE_SIZE):
    """Padlock in viewport coordinates; every stroke scales with the badge."""
    s = float(size)
    k = s / LOCK_BASE
    painter.save()
    painter.translate(x, y)
    painter.setRenderHint(QPainter.Antialiasing, True)
    paint_badge_plate(painter, QRectF(0, 0, s, s), radius=9 * k, inset=1.0 * k)
    # Shackle.
    painter.setPen(QPen(BADGE_ACCENT, 3.2 * k))
    painter.setBrush(Qt.NoBrush)
    painter.drawArc(QRectF(s * 0.30, s * 0.13, s * 0.40, s * 0.46), 0, 180 * 16)
    # Body.
    painter.setPen(Qt.NoPen)
    painter.setBrush(BADGE_ACCENT)
    painter.drawRoundedRect(QRectF(s * 0.22, s * 0.40, s * 0.56, s * 0.42), 4 * k, 4 * k)
    # Keyhole.
    painter.setBrush(BADGE_HOLE)
    cx, cy = s * 0.5, s * 0.55
    painter.drawEllipse(QPointF(cx, cy), 2.3 * k, 2.3 * k)
    painter.drawRect(QRectF(cx - 1.1 * k, cy, 2.2 * k, s * 0.15))
    painter.restore()


class Toast(QLabel):
    """Short message at the bottom of the canvas."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setStyleSheet("background: rgba(0,0,0,170); color: #eee; "
                           "padding: 6px 14px; border-radius: 6px;")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hide()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)

    def show_text(self, text, ms=1400):
        self.setText(text)
        self.adjustSize()
        p = self.parentWidget()
        self.move((p.width() - self.width()) // 2, p.height() - self.height() - 30)
        self.show()
        self.raise_()
        self.timer.start(ms)


class CropScissors(QWidget):
    """Variant 07 — Ghost, using the exact solid vector from the selection."""
    SIZE = 26
    GLYPH = 17.0

    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.scissors = QSvgRenderer(os.path.join(os.path.dirname(__file__),
                                                  "scissors_ghost.svg"), self)
        self._pm = None
        self._pm_dpr = None
        self.hide()

    def glyph_pixmap(self):
        dpr = self.devicePixelRatioF()
        if self._pm is None or self._pm_dpr != dpr:
            self._pm = tinted_glyph(self.scissors, self.GLYPH, BADGE_ACCENT, dpr)
            self._pm_dpr = dpr
        return self._pm

    def paintEvent(self, event):
        painter = QPainter(self)
        paint_badge_plate(painter, self.rect(), inset=1.4)
        painter.save()
        painter.translate(13, 13)
        painter.rotate(-45)
        painter.drawPixmap(QPointF(-self.GLYPH / 2, -self.GLYPH / 2), self.glyph_pixmap())
        painter.restore()


class CursorGlyph(QWidget):
    """A 26 px Ghost-style cursor replacement with a transparent hit area."""
    SIZE = 26
    GLYPH = 17.5
    OFFSET = 4.25

    def __init__(self, parent, asset_name):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.glyph = QSvgRenderer(os.path.join(os.path.dirname(__file__), asset_name), self)
        self._pm = None
        self._pm_dpr = None
        self.hide()

    def glyph_pixmap(self):
        # Cached: the SVG is rendered once per DPI, not on every repaint.
        dpr = self.devicePixelRatioF()
        if self._pm is None or self._pm_dpr != dpr:
            self._pm = tinted_glyph(self.glyph, self.GLYPH, BADGE_ACCENT, dpr)
            self._pm_dpr = dpr
        return self._pm

    def paintEvent(self, event):
        painter = QPainter(self)
        paint_badge_plate(painter, self.rect(), inset=1.4)
        painter.drawPixmap(QPointF(self.OFFSET, self.OFFSET), self.glyph_pixmap())


class IconButton(QPushButton):
    """Flat button that paints its own vector glyph: no assets, crisp at any DPI."""

    def __init__(self, parent=None, size=30):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 6px; }"
            "QPushButton:hover { background: rgba(255,255,255,30); }"
            "QPushButton:pressed { background: rgba(255,255,255,55); }")

    def paintEvent(self, event):
        super().paintEvent(event)          # hover / pressed background
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        self.paint_glyph(painter, QRectF(self.rect()))

    def paint_glyph(self, painter, rect):
        raise NotImplementedError


class PlayButton(IconButton):
    """Triangle when paused, two bars while playing."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._playing = False
        self.setToolTip("Play / Pause — single click on the video or Space")

    def set_playing(self, on):
        on = bool(on)
        if on != self._playing:
            self._playing = on
            self.update()

    def paint_glyph(self, painter, rect):
        painter.setPen(Qt.NoPen)
        painter.setBrush(TRANSPORT_ICON)
        cx, cy = rect.center().x(), rect.center().y()
        if self._playing:
            w, h, gap = 3.6, 13.0, 3.4
            painter.drawRoundedRect(QRectF(cx - gap / 2 - w, cy - h / 2, w, h), 1.2, 1.2)
            painter.drawRoundedRect(QRectF(cx + gap / 2, cy - h / 2, w, h), 1.2, 1.2)
        else:
            w, h = 12.0, 14.0
            x = cx - w / 2 + 1.0      # a triangle looks centered only when nudged right
            painter.drawPolygon(QPolygonF([QPointF(x, cy - h / 2),
                                           QPointF(x, cy + h / 2),
                                           QPointF(x + w, cy)]))


class MuteButton(IconButton):
    """Speaker with a cross when muted, with waves when the sound is on."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._muted = True
        self.setToolTip("Mute / Unmute (M)")

    def set_muted(self, on):
        on = bool(on)
        if on != self._muted:
            self._muted = on
            self.update()

    def paint_glyph(self, painter, rect):
        cx, cy = rect.center().x(), rect.center().y()
        painter.setPen(Qt.NoPen)
        painter.setBrush(TRANSPORT_ICON)
        painter.drawPolygon(QPolygonF([
            QPointF(cx - 8.0, cy - 3.2), QPointF(cx - 4.2, cy - 3.2),
            QPointF(cx - 0.4, cy - 7.2), QPointF(cx - 0.4, cy + 7.2),
            QPointF(cx - 4.2, cy + 3.2), QPointF(cx - 8.0, cy + 3.2)]))
        pen = QPen(TRANSPORT_ICON, 1.9, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        if self._muted:
            painter.drawLine(QPointF(cx + 3.0, cy - 4.0), QPointF(cx + 9.0, cy + 4.0))
            painter.drawLine(QPointF(cx + 9.0, cy - 4.0), QPointF(cx + 3.0, cy + 4.0))
        else:
            painter.drawArc(QRectF(cx - 1.0, cy - 5.0, 8.0, 10.0), -55 * 16, 110 * 16)
            painter.drawArc(QRectF(cx - 1.0, cy - 8.5, 13.0, 17.0), -55 * 16, 110 * 16)


class SeekBar(QWidget):
    """YouTube-style scrubber: thin full-width track, red gradient played part,
    round red playhead, thicker track + bigger head on hover, click / drag to seek.

    Emits scrubbed(ms) live while dragging and committed(ms) on release.
    """
    scrubbed = Signal(int)
    committed = Signal(int)
    HEIGHT = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self._duration = 0
        self._position = 0
        self._hover = False
        self._hover_x = None
        self._dragging = False
        self._drag_value = 0

    def is_scrubbing(self):
        return self._dragging

    def set_duration(self, ms):
        ms = max(0, int(ms))
        if ms != self._duration:
            self._duration = ms
            self.update()

    def set_position(self, ms):
        ms = max(0, int(ms))
        if ms != self._position:
            self._position = ms
            if not self._dragging:
                self.update()

    def _ratio(self):
        if self._duration <= 0:
            return 0.0
        value = self._drag_value if self._dragging else self._position
        return max(0.0, min(1.0, value / self._duration))

    def _value_at(self, x):
        if self._duration <= 0:
            return 0
        return int(max(0.0, min(1.0, x / max(1, self.width()))) * self._duration)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        active = self._hover or self._dragging
        th = 5.0 if active else 3.0
        y = (h - th) / 2
        painter.setPen(Qt.NoPen)
        # Unplayed remainder.
        painter.setBrush(QColor(255, 255, 255, 60))
        painter.drawRoundedRect(QRectF(0, y, w, th), th / 2, th / 2)
        played_w = w * self._ratio()
        # Lighter preview of the segment under the cursor (YouTube hover).
        if self._hover and self._hover_x is not None and not self._dragging:
            hx = max(0.0, min(float(w), self._hover_x))
            if hx > played_w:
                painter.setBrush(QColor(255, 255, 255, 45))
                painter.drawRoundedRect(QRectF(played_w, y, hx - played_w, th), th / 2, th / 2)
        # Played gradient.
        if played_w > 0:
            grad = QLinearGradient(0, 0, w, 0)
            grad.setColorAt(0.0, SEEK_PLAYED_A)
            grad.setColorAt(1.0, SEEK_PLAYED_B)
            painter.setBrush(QBrush(grad))
            painter.drawRoundedRect(QRectF(0, y, played_w, th), th / 2, th / 2)
        # Playhead with a soft glow.
        if self._duration > 0:
            r = 7.0 if active else 5.0
            cx = max(r, min(w - r, played_w))
            cy = h / 2
            painter.setBrush(QColor(255, 0, 80, 55))
            painter.drawEllipse(QPointF(cx, cy), r + 3, r + 3)
            painter.setBrush(SEEK_HEAD)
            painter.drawEllipse(QPointF(cx, cy), r, r)
        painter.end()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self._hover_x = None
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._duration > 0:
            self._dragging = True
            self._drag_value = self._value_at(event.position().x())
            self.scrubbed.emit(self._drag_value)
            self.update()
            event.accept()

    def mouseMoveEvent(self, event):
        self._hover_x = event.position().x()
        if self._dragging:
            self._drag_value = self._value_at(event.position().x())
            self.scrubbed.emit(self._drag_value)
        self.update()

    def mouseReleaseEvent(self, event):
        if self._dragging and event.button() == Qt.LeftButton:
            self._dragging = False
            self._drag_value = self._value_at(event.position().x())
            self._position = self._drag_value
            self.committed.emit(self._drag_value)
            self.update()
            event.accept()


class VideoControls(QWidget):
    """Transport bar for one selected video: YouTube-style seek bar on top,
    then play, mute, time and Frame → Image."""
    PREFERRED_WIDTH = 420       # screen pixels; independent of canvas zoom
    MIN_WIDTH = 150

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas, self.video = canvas, None
        self.setFixedHeight(56)
        self.setObjectName("videoControls")
        self.setStyleSheet(
            "QWidget#videoControls { background: #20242a; border: 1px solid #59636f;"
            "                        border-radius: 8px; }"
            "QWidget#videoControls QLabel { color: #f2f2f2; border: none;"
            "                               background: transparent; font-size: 13px; }"
            "QPushButton#textButton { color: #f2f2f2; background: #2b3138;"
            "                         border: 1px solid #59636f; border-radius: 6px;"
            "                         padding: 4px 9px; }"
            "QPushButton#textButton:hover { background: #39414a; }")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.seekbar = SeekBar(self)
        self.seekbar.scrubbed.connect(self._on_scrub)
        self.seekbar.committed.connect(self._on_seek)
        outer.addWidget(self.seekbar)
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(8, 2, 8, 6)
        layout.setSpacing(6)
        self.play_btn = PlayButton(row)
        self.play_btn.clicked.connect(self.toggle)
        layout.addWidget(self.play_btn)
        self.mute_btn = MuteButton(row)
        self.mute_btn.clicked.connect(self.toggle_mute)
        layout.addWidget(self.mute_btn)
        self.time = QLabel("0:00 / 0:00")
        self.time.setMinimumWidth(96)
        layout.addWidget(self.time)
        layout.addStretch(1)
        self.capture = QPushButton("Frame → Image")
        self.capture.setObjectName("textButton")
        self.capture.setFocusPolicy(Qt.NoFocus)
        self.capture.setCursor(Qt.PointingHandCursor)
        self.capture.setToolTip("Save the current frame as a new image on the board")
        self.capture.clicked.connect(self.capture_frame)
        layout.addWidget(self.capture)
        outer.addWidget(row)
        # Kept as attributes so existing callers never hit an AttributeError.
        self.play, self.mute, self.poster = self.play_btn, self.mute_btn, None
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.refresh)
        self.hide()

    @staticmethod
    def timestamp(milliseconds):
        seconds = max(0, int(milliseconds // 1000))
        return f"{seconds // 60}:{seconds % 60:02d}"

    def set_video(self, video):
        self.video = video
        if video is None:
            self.timer.stop()
            self.hide()
            return
        self.seekbar.set_duration(0)
        self.seekbar.set_position(0)
        self.show()
        self.raise_()
        self.canvas.position_video_controls()
        self.timer.start()
        self.refresh()

    def update_compact_layout(self, width):
        """Keep every visible control inside narrow videos without scaling it."""
        self.capture.setVisible(width >= 250)
        self.time.setVisible(width >= 170)

    def toggle(self):
        if self.video:
            self.video.toggle()
            self.refresh()

    def toggle_mute(self):
        if self.video:
            self.video.toggle_mute()
            self.refresh()
            self.commit_state()

    def commit_state(self):
        if self.video:
            self.canvas.win.record_history()

    def _on_scrub(self, value):
        if self.video:
            duration = max(0, self.video.player.duration())
            self.time.setText(f"{self.timestamp(value)} / {self.timestamp(duration)}")

    def _on_seek(self, value):
        if self.video:
            self.video.seek(value)
            self.refresh()
            self.commit_state()

    def set_volume(self, value):
        """No slider any more; kept for callers that set the volume directly."""
        if self.video:
            self.video.audio.setVolume(max(0.0, min(1.0, value / 100)))
            if value and self.video.audio.isMuted():
                self.video.audio.setMuted(False)
            self.refresh()

    def set_poster(self):
        if self.video:
            self.canvas.win.capture_video_poster(self.video)

    def capture_frame(self):
        if self.video:
            self.canvas.win.capture_video_frame(self.video)

    def refresh(self):
        if not self.video:
            return
        player = self.video.player
        duration, position = max(0, player.duration()), max(0, player.position())
        self.seekbar.set_duration(duration)
        if not self.seekbar.is_scrubbing():
            self.seekbar.set_position(position)
            self.time.setText(f"{self.timestamp(position)} / {self.timestamp(duration)}")
        self.play_btn.set_playing(player.playbackState() == player.PlaybackState.PlayingState)
        self.mute_btn.set_muted(self.video.audio.isMuted())


class MiniMap(QWidget):
    """Lightweight board overview; click or drag to move the camera."""

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.setFixedSize(220, 150)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Board overview — click or drag to navigate")
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self.update)
        self._bounds = QRectF()
        self._scale = 1.0
        self._offset = QPointF()

    def schedule(self, *_):
        if self.isVisible():
            self._timer.start()

    def _content(self):
        result = QRectF()
        for item in self.canvas.win.top_items():
            result = result.united(self.canvas.win.full_rect(item))
        if result.isNull() or result.isEmpty():
            result = self.canvas.mapToScene(self.canvas.viewport().rect()).boundingRect()
        return result.adjusted(-30, -30, 30, 30)

    def _map_point(self, point):
        return QPointF(self._offset.x() + (point.x() - self._bounds.left()) * self._scale,
                       self._offset.y() + (point.y() - self._bounds.top()) * self._scale)

    def _map_rect(self, rect):
        return QRectF(self._map_point(rect.topLeft()), self._map_point(rect.bottomRight()))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(24, 28, 34, 235))
        self._bounds = self._content()
        margin = 8
        sx = (self.width() - 2 * margin) / max(1.0, self._bounds.width())
        sy = (self.height() - 2 * margin) / max(1.0, self._bounds.height())
        self._scale = min(sx, sy)
        used_w, used_h = self._bounds.width() * self._scale, self._bounds.height() * self._scale
        self._offset = QPointF((self.width() - used_w) / 2, (self.height() - used_h) / 2)
        colors = {"image": QColor("#6fa8dc"), "video": QColor("#a878c8"),
                  "group": QColor("#999999"), "text": QColor("#e5d567"),
                  "missing": QColor("#d57c5f"), "stroke": QColor("#d66565"),
                  "frame": QColor("#bbbbbb"), "document": QColor("#e8e8df"),
                  "model3d": QColor("#65c2a5")}
        for item in self.canvas.win.top_items():
            rect = self._map_rect(self.canvas.win.full_rect(item))
            painter.fillRect(rect, colors.get(item.kind, QColor("#888888")))
        visible = self.canvas.mapToScene(self.canvas.viewport().rect())
        polygon = QPolygonF([self._map_point(point) for point in visible])
        painter.setPen(QPen(QColor("#ffffff"), 1.4))
        painter.setBrush(QColor(255, 255, 255, 22))
        painter.drawPolygon(polygon)
        painter.setPen(QColor("#4a515b"))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def _navigate(self, position):
        if self._bounds.isEmpty():
            return
        scene_point = QPointF(self._bounds.left() + (position.x() - self._offset.x()) / self._scale,
                              self._bounds.top() + (position.y() - self._offset.y()) / self._scale)
        self.canvas.centerOn(scene_point)
        self.canvas.schedule_media_update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._navigate(event.position())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._navigate(event.position())


class CanvasSearch(QWidget):
    """Permanent large search field; keyboard navigation needs no extra buttons."""

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self._matches = []
        self._index = -1
        self.setFixedHeight(66)
        self.setStyleSheet("""
            QWidget#canvasSearch { background: #20242a; border: 2px solid #4fc3f7;
                                   border-radius: 10px; }
            QLineEdit { background: #15181d; color: white; border: 1px solid #59636f;
                        border-radius: 6px; padding: 6px 12px; font-size: 19px; }
            QLineEdit:focus { border: 1px solid #4fc3f7; }
            QLabel { color: #c9d3dd; border: none; background: transparent; }
        """)
        self.setObjectName("canvasSearch")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        self.query = QLineEdit()
        self.query.setPlaceholderText("Search references, groups, notes and text…")
        self.query.setMinimumHeight(44)
        self.query.installEventFilter(self)
        layout.addWidget(self.query, 1)
        self.status = QLabel("")
        self.status.setMinimumWidth(70)
        self.status.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status)
        self.query.textChanged.connect(self.refresh)
        self.show()

    def eventFilter(self, obj, event):
        if obj is self.query and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key_Escape:
                self.finish_search()
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Down):
                self.step(1)
                return True
            if key == Qt.Key_Up:
                self.step(-1)
                return True
        return super().eventFilter(obj, event)

    def show_panel(self):
        # The field is permanent; Ctrl+F merely focuses it.
        self.show()
        self.canvas.position_search()
        self.raise_()
        self.query.setFocus(Qt.ShortcutFocusReason)
        self.query.selectAll()

    def finish_search(self):
        if self.query.text():
            self.query.clear()
        self.canvas.setFocus(Qt.OtherFocusReason)

    @staticmethod
    def matches(values, query):
        """Unicode-aware multiword matching against every textual item property."""
        haystack = " ".join(str(value) for value in values if value).casefold()
        terms = query.casefold().split()
        return bool(terms) and all(term in haystack for term in terms)

    def refresh(self):
        query = self.query.text().strip()
        self._matches = []
        if query:
            for top in self.canvas.win.top_items():
                for item in walk(top):
                    values = [item.kind, note_text(item), getattr(item, "path", "") or "",
                              getattr(item, "title", "") or ""]
                    # Read the live document directly, including text being edited.
                    if hasattr(item, "toPlainText"):
                        values.append(item.toPlainText())
                    if self.matches(values, query):
                        name = item.title if item.kind == "group" else (
                            os.path.basename(item.path) if getattr(item, "path", None) else item.kind)
                        self._matches.append((item.uid, name))
        self._index = 0 if self._matches else -1
        if self._matches:
            self.activate_current()
        else:
            self.status.setText("No results" if query else "")

    def step(self, direction):
        if not self._matches:
            return
        self._index = (self._index + direction) % len(self._matches)
        self.activate_current()

    def activate_current(self):
        if self._index < 0 or self._index >= len(self._matches):
            return
        uid, _ = self._matches[self._index]
        item = next((obj for top in self.canvas.win.top_items() for obj in walk(top)
                     if obj.uid == uid), None)
        if item is None:
            self.refresh()
            return
        top = item.topLevelItem()
        self.canvas.win.scene.clearSelection()
        if top in self.canvas.win.objects:
            top.setSelected(True)
        self.canvas.win.fit_rect(self.canvas.win.full_rect(item))
        self.canvas.schedule_media_update()
        self.status.setText(f"{self._index + 1} / {len(self._matches)}")