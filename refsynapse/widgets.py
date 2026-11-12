"""Small canvas overlay widgets."""
import os
from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, QEvent
from PySide6.QtGui import QPainter, QColor, QPen, QPolygonF
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QLabel, QWidget, QHBoxLayout, QLineEdit, QPushButton, QSlider
from .items import walk, note_text


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
    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(26, 26)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.scissors = QSvgRenderer(os.path.join(os.path.dirname(__file__),
                                                  "scissors_ghost.svg"), self)
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#ff5252"), 1.5))
        painter.setBrush(QColor(255, 255, 255, 24))
        painter.drawEllipse(QPointF(13, 13), 11.6, 11.6)
        painter.save()
        painter.translate(13, 13)
        painter.rotate(-45)
        self.scissors.render(painter, QRectF(-8.5, -8.5, 17, 17))
        painter.restore()


class CursorGlyph(QWidget):
    """A 26 px Ghost-style cursor replacement with a transparent hit area."""
    def __init__(self, parent, asset_name):
        super().__init__(parent)
        self.setFixedSize(26, 26)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.glyph = QSvgRenderer(os.path.join(os.path.dirname(__file__), asset_name), self)
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#ff5252"), 1.5))
        painter.setBrush(QColor(255, 255, 255, 24))
        painter.drawEllipse(QPointF(13, 13), 11.6, 11.6)
        self.glyph.render(painter, QRectF(4.25, 4.25, 17.5, 17.5))


class VideoControls(QWidget):
    """Compact transport controls shown for one selected video."""
    PREFERRED_WIDTH = 620       # screen pixels; independent of canvas zoom
    MIN_WIDTH = 170

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas, self.video = canvas, None
        self._seeking = False
        self.setFixedHeight(46)
        self.setStyleSheet("QWidget#videoControls { background: #20242a; border: 1px solid #59636f; "
                           "border-radius: 8px; } QPushButton { padding: 3px 8px; }")
        self.setObjectName("videoControls")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(7)
        self.play = QPushButton("Play")
        self.play.setFixedWidth(52)
        self.play.clicked.connect(self.toggle)
        layout.addWidget(self.play)
        self.time = QLabel("0:00 / 0:00")
        self.time.setMinimumWidth(92)
        layout.addWidget(self.time)
        self.position = QSlider(Qt.Horizontal)
        self.position.setTracking(False)
        self.position.sliderPressed.connect(self.seek_start)
        self.position.sliderReleased.connect(self.seek_end)
        layout.addWidget(self.position, 1)
        self.mute = QPushButton("Mute")
        self.mute.clicked.connect(self.toggle_mute)
        layout.addWidget(self.mute)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(80)
        self.volume.valueChanged.connect(self.set_volume)
        self.volume.sliderReleased.connect(self.commit_state)
        layout.addWidget(self.volume)
        self.poster = QPushButton("Poster")
        self.poster.setToolTip("Use the current frame as the paused preview")
        self.poster.clicked.connect(self.set_poster)
        layout.addWidget(self.poster)
        self.capture = QPushButton("Frame → Image")
        self.capture.clicked.connect(self.capture_frame)
        layout.addWidget(self.capture)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
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
        self.volume.blockSignals(True)
        self.volume.setValue(round(video.audio.volume() * 100))
        self.volume.blockSignals(False)
        self.show()
        self.raise_()
        self.canvas.position_video_controls()
        self.timer.start()
        self.refresh()

    def update_compact_layout(self, width):
        """Keep every visible control inside narrow videos without scaling it."""
        compact = width < 560
        tiny = width < 320
        for widget in (self.poster, self.capture, self.volume):
            widget.setVisible(not compact)
        self.time.setVisible(not tiny)
        self.mute.setVisible(not tiny)

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

    def set_volume(self, value):
        if self.video:
            self.video.audio.setVolume(value / 100)
            if value and self.video.audio.isMuted():
                self.video.audio.setMuted(False)
            self.refresh()

    def seek_start(self):
        self._seeking = True

    def seek_end(self):
        if self.video:
            self.video.seek(self.position.value())
        self._seeking = False
        self.refresh()
        self.commit_state()

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
        self.position.setRange(0, duration)
        if not self._seeking:
            self.position.setValue(position)
        shown = self.position.value() if self._seeking else position
        self.time.setText(f"{self.timestamp(shown)} / {self.timestamp(duration)}")
        playing = player.playbackState() == player.PlaybackState.PlayingState
        self.play.setText("Pause" if playing else "Play")
        self.mute.setText("Unmute" if self.video.audio.isMuted() else "Mute")


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
        import os
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
