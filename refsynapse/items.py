"""Canvas item types and annotations."""
import copy
from PySide6.QtCore import Qt, QRectF, QSizeF, QPointF, QUrl, QTimer
from PySide6.QtGui import (QPixmap, QImage, QPainter, QColor, QPen, QFont,
                           QPainterPath, QPainterPathStroker, QImageReader, QMovie)
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsPixmapItem, QGraphicsTextItem,
                               QGraphicsItemGroup, QGraphicsPathItem, QGraphicsRectItem,
                               QDialog, QVBoxLayout, QHBoxLayout, QTextEdit, QFontComboBox,
                               QSpinBox, QPushButton, QColorDialog, QDialogButtonBox)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from .config import (ACCENT, DEFAULT_DRAW_COLOR, DEFAULT_TEXT_COLOR, FLAGS, HANDLE,
                     new_id, ev)

# =====================================================================
#  Annotations
# =====================================================================
class NoteItem(QGraphicsTextItem):
    def __init__(self, owner):
        super().__init__(owner)
        self.setDefaultTextColor(QColor("#1a1a1a"))
        self.setTextWidth(260)

    def paint(self, p, opt, w=None):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 226, 110, 235))
        p.drawRoundedRect(self.boundingRect(), 4, 4)
        super().paint(p, opt, w)


def note_text(item):
    return item.note.toPlainText() if item.note else ""


def content_rect(item):
    return item.members_rect() if item.kind == "group" else item.boundingRect()


def handle_rect(item):
    """Rectangle (in item coordinates) whose corners and sides carry handles."""
    if item.kind == "group":
        return item.frame()
    if item.kind == "frame":
        return item.rect()
    return item.boundingRect()


def place_note(item):
    if not item.note:
        return
    s = item.scale() or 1.0
    item.note.setScale(1 / s)
    r = content_rect(item)
    item.note.setPos(r.left(), r.bottom() + 6 / s)
    if item.kind == "group":
        item.refresh()


def set_note(item, text, style=None):
    text = (text or "").strip()
    if style:
        item.note_style = copy.deepcopy(style)
    if not text:
        if item.note:
            item.scene().removeItem(item.note)
            item.note = None
    else:
        if item.note is None:
            item.note = NoteItem(item)
        item.note.setPlainText(text)
        st = getattr(item, "note_style", {})
        font = QFont()
        if st.get("font"):
            font.fromString(st["font"])
        item.note.setFont(font)
        item.note.setDefaultTextColor(QColor(st.get("color", "#1a1a1a")))
    if item.kind == "group":
        item.refresh()
    place_note(item)


def smooth_stroke_path(points, tension=0.72):
    """Build a rounded Catmull-Rom spline through the sampled ink points."""
    pts = [QPointF(*p) if isinstance(p, (tuple, list)) else QPointF(p) for p in points]
    path = QPainterPath()
    if not pts:
        return path
    path.moveTo(pts[0])
    if len(pts) == 1:
        # A click becomes a small round dot instead of an empty path.
        path.lineTo(pts[0] + QPointF(0.01, 0.0))
        return path
    if len(pts) == 2:
        path.lineTo(pts[1])
        return path
    for i in range(len(pts) - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < len(pts) else p2
        c1 = p1 + (p2 - p0) * (tension / 6.0)
        c2 = p2 - (p3 - p1) * (tension / 6.0)
        path.cubicTo(c1, c2, p2)
    return path


class StrokeItem(QGraphicsPathItem):
    """A freehand line stored as a normal project object."""
    kind = "stroke"

    def __init__(self, color=DEFAULT_DRAW_COLOR, width=10):
        super().__init__()
        self.uid, self.note = new_id(), None
        self.color, self.width = color, width
        self.points = []
        self.setFlags(FLAGS)
        # Select-tool clicks pass through ink to normal Qt hit-testing. Canvas
        # handles a stroke manually only when there is no object underneath it.
        self.setAcceptedMouseButtons(Qt.NoButton)
        pen = QPen(QColor(color), width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        self.setPen(pen)
        self.setBrush(Qt.NoBrush)

    def paint(self, p, opt, w=None):
        super().paint(p, opt, w)
        if self.isSelected():
            draw_selection(self, p)


class TextItem(QGraphicsTextItem):
    """A movable, independently formatted text annotation."""
    kind = "text"

    def __init__(self, text="", color=DEFAULT_TEXT_COLOR, font=None, height=100):
        super().__init__(text)
        self.uid, self.path, self.note = new_id(), None, None
        self.frame_height = max(24.0, float(height))
        self.setFlags(FLAGS)
        self.setDefaultTextColor(QColor(color))
        self.setFont(font or QFont("Segoe UI", 18))
        self.setTextWidth(360)
        self._edit_original = ""
        self._cancel_edit = False
        self._fitting_font = False
        self.document().contentsChanged.connect(self.fit_font)
        self.fit_font()

    def fit_font(self):
        """Use the largest font that fits the fixed text frame."""
        if self._fitting_font:
            return
        self._fitting_font = True
        font = self.font()
        low, high = 4.0, max(6.0, self.frame_height)
        best = low
        for _ in range(12):
            size = (low + high) / 2
            font.setPointSizeF(size)
            self.setFont(font)
            doc = self.document()
            fits = (doc.size().height() <= self.frame_height + 0.5
                    and doc.idealWidth() <= self.textWidth() + 0.5)
            if fits:
                best = size
                low = size
            else:
                high = size
        font.setPointSizeF(best)
        self.setFont(font)
        self._fitting_font = False
        self.update()

    def boundingRect(self):
        r = super().boundingRect()
        return QRectF(r.x(), r.y(), r.width(), max(r.height(), self.frame_height))

    def set_frame_size(self, width, height):
        self.prepareGeometryChange()
        self.setTextWidth(max(40.0, float(width)))
        self.frame_height = max(24.0, float(height))
        self.fit_font()
        place_note(self)
        parent = self.parentItem()
        if parent is not None and hasattr(parent, "refresh"):
            parent.refresh()
        self.update()

    def shape(self):
        path = QPainterPath()
        path.addRect(self.boundingRect())
        return path

    def keyPressEvent(self, e):
        if ev(e.key()) == ev(Qt.Key_Escape):
            self._cancel_edit = True
            self.clearFocus()
            e.accept()
            return
        if (ev(e.key()) in (ev(Qt.Key_Return), ev(Qt.Key_Enter))
                and e.modifiers() & Qt.ControlModifier):
            self.clearFocus()
            e.accept()
            return
        super().keyPressEvent(e)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        if self._cancel_edit:
            self.setPlainText(self._edit_original)
        self._cancel_edit = False
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        sc = self.scene()
        if sc:
            for view in sc.views():
                if hasattr(view, "text_editing_finished"):
                    view.text_editing_finished(self)

    def paint(self, p, opt, w=None):
        editing = self.textInteractionFlags() != Qt.NoTextInteraction
        super().paint(p, opt, w)
        if self.isSelected() or editing:
            draw_selection(self, p)


class FrameItem(QGraphicsRectItem):
    """A standalone transparent frame with a thick rounded outline."""
    kind = "frame"

    def __init__(self, width=320, height=200, color="#ffffff", line_width=28):
        super().__init__(QRectF(0, 0, width, height))
        self.uid, self.path, self.note = new_id(), None, None
        self.color, self.line_width = color, line_width
        self.setFlags(FLAGS)
        self.setPen(QPen(QColor(color), line_width))
        self.setBrush(Qt.NoBrush)

    def set_frame_rect(self, rect):
        """Resize the frame itself without scaling its outline or children."""
        self.prepareGeometryChange()
        self.setRect(QRectF(rect))
        place_note(self)
        parent = self.parentItem()
        if parent is not None and hasattr(parent, "refresh"):
            parent.refresh()
        self.update()

    def shape(self):
        # Only the outline intercepts clicks; objects inside remain selectable.
        path = QPainterPath()
        path.addRoundedRect(self.rect(), 15, 15)
        stroker = QPainterPathStroker()
        stroker.setWidth(self.line_width)
        stroker.setJoinStyle(Qt.RoundJoin)
        return stroker.createStroke(path)

    def contains(self, point):
        return self.shape().contains(point)

    def paint(self, p, opt, w=None):
        p.setPen(self.pen())
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(self.rect(), 15, 15)
        if self.isSelected():
            draw_selection(self, p)


class AnnotationDialog(QDialog):
    """Text, font family, size and color in one compact dialog."""

    def __init__(self, parent, text="", font=None, color=DEFAULT_TEXT_COLOR):
        super().__init__(parent)
        self.setWindowTitle("Annotation")
        self.color = QColor(color)
        lay = QVBoxLayout(self)
        self.editor = QTextEdit()
        self.editor.setPlainText(text)
        self.editor.setMinimumSize(420, 160)
        lay.addWidget(self.editor)
        row = QHBoxLayout()
        self.family = QFontComboBox()
        self.family.setCurrentFont(font or QFont("Segoe UI", 18))
        self.size = QSpinBox()
        self.size.setRange(6, 144)
        self.size.setValue((font or QFont("Segoe UI", 18)).pointSize())
        self.color_button = QPushButton("Color…")
        self.color_button.clicked.connect(self.choose_color)
        row.addWidget(self.family, 1)
        row.addWidget(self.size)
        row.addWidget(self.color_button)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.editor.setFocus()
        self.editor.selectAll()

    def choose_color(self):
        c = QColorDialog.getColor(self.color, self, "Text color")
        if c.isValid():
            self.color = c

    def values(self):
        font = self.family.currentFont()
        font.setPointSize(self.size.value())
        return self.editor.toPlainText().strip(), font, self.color.name()


# =====================================================================
#  Canvas items
# =====================================================================
def view_zoom(item):
    sc = item.scene()
    views = sc.views() if sc else []
    return views[0].transform().m11() if views else 1.0


def handle_positions(r):
    """Handle point and opposite fixed anchor for every corner and side."""
    return (
        (r.topLeft(), r.bottomRight()), (r.topRight(), r.bottomLeft()),
        (r.bottomLeft(), r.topRight()), (r.bottomRight(), r.topLeft()),
        (QPointF(r.left(), r.center().y()), QPointF(r.right(), r.center().y())),
        (QPointF(r.right(), r.center().y()), QPointF(r.left(), r.center().y())),
        (QPointF(r.center().x(), r.top()), QPointF(r.center().x(), r.bottom())),
        (QPointF(r.center().x(), r.bottom()), QPointF(r.center().x(), r.top())),
    )


def draw_handle(item, p, r):
    if item.parentItem() is not None:
        return
    scene = item.scene()
    views = scene.views() if scene else []
    if views and hasattr(views[0], "win") and len(views[0].win.selected_top()) > 1:
        return  # Canvas draws one shared handle set for the whole selection.
    h = HANDLE / (view_zoom(item) * (item.scale() or 1.0))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(ACCENT))
    for point, _ in handle_positions(r):
        p.drawRect(QRectF(point.x() - h / 2, point.y() - h / 2, h, h))


def draw_selection(item, p):
    r = handle_rect(item)
    pen = QPen(QColor(ACCENT), 1)
    pen.setCosmetic(True)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawRect(r)
    draw_handle(item, p, r)


class ImageItem(QGraphicsPixmapItem):
    """Image with a lightweight preview, on-demand original and non-destructive crop.

    Animated files (GIF, and WebP when its Qt plugin is available) are driven by
    QMovie: every decoded frame replaces the displayed pixmap, while crop, flip
    and export keep working on the current frame.
    """
    kind = "image"
    PREVIEW_MAX = 2048

    def __init__(self, path):
        super().__init__()
        self.uid, self.path, self.note, self.flipped = new_id(), path, None, False
        self.crop = None
        self.crop_editing = False
        self._source = None
        self._movie = None
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        reader.setDecideFormatFromContent(True)
        self.native_size = reader.size()
        animated = reader.supportsAnimation() and reader.imageCount() != 1
        # Animation frames must stay at native size: the movie replaces the
        # preview every frame, so a downscaled preview would only cause jumps.
        if (not animated and self.native_size.isValid()
                and max(self.native_size.width(), self.native_size.height()) > self.PREVIEW_MAX):
            factor = self.PREVIEW_MAX / max(self.native_size.width(), self.native_size.height())
            reader.setScaledSize(reader.size() * factor)
        image = reader.read()
        self._display = QPixmap.fromImage(image)
        self._preview = self._display
        if not self.native_size.isValid() and not image.isNull():
            self.native_size = image.size()
        self.setFlags(FLAGS)
        self.setTransformationMode(Qt.SmoothTransformation)
        if animated and self.is_valid():
            self._start_movie()

    # ---------- animation ----------
    def _start_movie(self):
        # QGraphicsItem is not a QObject, so the movie has no parent: this
        # attribute is the only reference keeping the decoder alive.
        movie = QMovie(self.path)
        if not movie.isValid() or movie.frameCount() == 1:
            return
        movie.setCacheMode(QMovie.CacheNone)
        movie.frameChanged.connect(self._movie_frame)
        self._movie = movie
        movie.start()
        self._movie_frame()

    def _movie_frame(self, *_):
        if self._movie is None:
            return
        frame = self._movie.currentPixmap()
        if not frame.isNull():
            self._display = frame
            self.update()

    def is_animated(self):
        return self._movie is not None

    def is_valid(self):
        return not self._display.isNull() and self.native_size.isValid()

    def pixmap(self):
        return self._display

    def source_pixmap(self):
        if self._movie is not None:
            # Every frame is already full resolution; never swap in a still.
            return self._display
        if self._source is None:
            reader = QImageReader(self.path)
            reader.setAutoTransform(True)
            reader.setDecideFormatFromContent(True)
            image = reader.read()
            if not image.isNull():
                self.prepareGeometryChange()
                self._source = QPixmap.fromImage(image)
                self.native_size = image.size()
                self._display = self._source
        return self._source

    def ensure_quality(self, zoom):
        if self._movie is not None:
            return
        if zoom >= 1.25 and self._source is None:
            self.source_pixmap()
            self.update()
        elif zoom < 0.5 and self._source is not None and not self.crop_editing:
            self._source = None
            self._display = self._preview
            self.update()

    def full_rect_local(self):
        return QRectF(QPointF(0, 0), QSizeF(self.native_size))

    def boundingRect(self):
        if self.crop_editing or self.crop is None:
            return self.full_rect_local()
        return QRectF(QPointF(0, 0), self.crop.size())

    def shape(self):
        path = QPainterPath()
        path.addRect(self.boundingRect())
        return path

    def crop_origin_offset(self, rect):
        """Crop origin as a parent-space offset, including scale and rotation."""
        return self.mapToParent(rect.topLeft()) - self.mapToParent(QPointF(0, 0))

    def set_crop(self, rect, adjust_position=False):
        if rect is not None:
            rect = rect.normalized().intersected(self.full_rect_local())
            if rect.width() < 1 or rect.height() < 1:
                return
        if adjust_position and rect is not None:
            # The painted crop starts at local (0, 0). Move its origin so the
            # retained pixels stay exactly where they were on the canvas.
            offset = self.crop_origin_offset(rect)
            self.moveBy(offset.x(), offset.y())
        self.prepareGeometryChange()
        self.crop = None if rect is None else QRectF(rect)
        place_note(self)
        self.update()

    def set_crop_editing(self, enabled, adjust_position=False):
        enabled = bool(enabled)
        if enabled and adjust_position and self.crop is not None:
            # Reveal the full source around the existing crop without moving it.
            offset = self.crop_origin_offset(self.crop)
            self.moveBy(-offset.x(), -offset.y())
        self.crop_editing = enabled
        if enabled:
            self.source_pixmap()
        self.prepareGeometryChange()
        place_note(self)
        self.update()

    def set_flip(self, on):
        self.flipped = bool(on)
        self.update()

    def export_image(self, path, quality=95):
        """Save the visible crop and flip at the best available resolution.

        An animated image exports its current frame as a still picture.
        """
        pm = self.source_pixmap()
        if pm is None or pm.isNull():
            pm = self._display
        if pm.isNull():
            return False
        rect = self.crop or self.full_rect_local()
        sx = pm.width() / max(1, self.native_size.width())
        sy = pm.height() / max(1, self.native_size.height())
        region = QRectF(rect.x() * sx, rect.y() * sy, rect.width() * sx, rect.height() * sy)
        region = region.intersected(QRectF(0, 0, pm.width(), pm.height()))
        if region.isEmpty():
            return False
        image = pm.copy(region.toAlignedRect()).toImage()
        if self.flipped:
            image = (image.flipped(Qt.Horizontal) if hasattr(image, "flipped")
                     else image.mirrored(True, False))
        if path.lower().endswith((".jpg", ".jpeg", ".webp")):
            return image.save(path, quality=quality)
        return image.save(path)

    def paint(self, painter, option, widget=None):
        display = self._source or self._display
        if display.isNull():
            return
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        target = self.boundingRect()
        source = self.full_rect_local() if self.crop_editing else (self.crop or self.full_rect_local())
        sx = display.width() / max(1, self.native_size.width())
        sy = display.height() / max(1, self.native_size.height())
        source = QRectF(source.x() * sx, source.y() * sy,
                        source.width() * sx, source.height() * sy)
        painter.save()
        if self.flipped:
            painter.translate(target.width(), 0)
            painter.scale(-1, 1)
        painter.drawPixmap(target, display, source)
        painter.restore()
        if self.isSelected() and not self.crop_editing:
            draw_selection(self, painter)


class CropOverlay(QGraphicsItem):
    """Canvas-native crop frame drawn in the image's original coordinates."""
    def __init__(self, image):
        super().__init__(image)
        self.image = image
        self.rect = QRectF(image.crop or image.full_rect_local())
        self.setZValue(10000)

    def boundingRect(self):
        return self.image.full_rect_local().adjusted(-20, -20, 20, 20)

    def set_rect(self, rect):
        self.rect = QRectF(rect)
        self.update()

    def paint(self, painter, option, widget=None):
        outer = self.image.full_rect_local()
        shade = QPainterPath()
        shade.setFillRule(Qt.OddEvenFill)
        shade.addRect(outer)
        shade.addRect(self.rect)
        painter.fillPath(shade, QColor(0, 0, 0, 115))
        pen = QPen(QColor(ACCENT), 2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(self.rect)
        h = HANDLE / max(0.01, view_zoom(self) * (self.image.scale() or 1.0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(ACCENT))
        for point, _ in handle_positions(self.rect):
            painter.drawRect(QRectF(point.x() - h / 2, point.y() - h / 2, h, h))


class MissingItem(QGraphicsRectItem):
    """Persistent placeholder for media that cannot currently be found."""
    kind = "missing"

    def __init__(self, path, media_kind="image", width=480, height=270):
        super().__init__(QRectF(0, 0, max(80, width), max(60, height)))
        self.uid, self.path, self.note = new_id(), path, None
        self.media_kind = media_kind
        self.setFlags(FLAGS)

    def paint(self, painter, option, widget=None):
        painter.setBrush(QColor(45, 45, 45))
        painter.setPen(QPen(QColor("#e0a060"), 2, Qt.DashLine))
        painter.drawRect(self.rect())
        painter.setPen(QColor("#e0e0e0"))
        text = f"Missing {self.media_kind}\n{self.path}\n\nRight-click → Relink Media"
        painter.drawText(self.rect().adjusted(12, 12, -12, -12),
                         Qt.AlignCenter | Qt.TextWordWrap, text)
        if self.isSelected():
            draw_selection(self, painter)


class VideoItem(QGraphicsVideoItem):
    kind = "video"

    def __init__(self, path):
        super().__init__()
        self.uid, self.path, self.note = new_id(), path, None
        self.setFlags(FLAGS)
        self.setSize(QSizeF(480, 270))
        self.player = QMediaPlayer()
        self.audio = QAudioOutput()
        self.audio.setMuted(True)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self)
        self.player.setLoops(-1)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.nativeSizeChanged.connect(self._fit)
        self.player.errorOccurred.connect(self._media_error)
        self.player.playbackStateChanged.connect(self._playback_changed)
        self.player.mediaStatusChanged.connect(self._media_status)
        self.player.positionChanged.connect(self._position_changed)
        self._frame_serial = 0
        self._restore_frame_serial = 0
        self.videoSink().videoFrameChanged.connect(self._frame_changed)
        self._auto_paused = False
        self._user_paused = True
        self._poster = QPixmap()
        self.poster_position = -1
        self._pending_position = None
        self._pending_poster = None
        self._requested_position = None
        self._restore_seek_sent = False
        self._restore_attempts = 0
        self._restore_timer = QTimer(self)
        self._restore_timer.setInterval(50)
        self._restore_timer.timeout.connect(self._restore_step)
        # Decode on demand instead of playing every movie as soon as the board opens.
        self.player.pause()

    def _frame_changed(self, *_):
        self._frame_serial += 1

    def _playback_changed(self, *_):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.clear_poster()
        self.update()

    def _fit(self, s):
        if s.width() > 0:
            self.setSize(QSizeF(480, 480 * s.height() / s.width()))
            place_note(self)
            par = self.parentItem()
            if par is not None and hasattr(par, "refresh"):
                par.refresh()

    def decoded_frame_image(self):
        """Return the latest frame decoded by Qt without forcing playback."""
        frame = self.videoSink().videoFrame()
        return frame.toImage() if frame.isValid() else QImage()

    def current_frame_image(self):
        image = self.decoded_frame_image()
        if image.isNull() and not self._poster.isNull():
            image = self._poster.toImage()
        return image

    def clear_poster(self):
        if not self._poster.isNull():
            self._poster = QPixmap()
            self.update()
        self.poster_position = -1

    def capture_poster(self):
        image = self.decoded_frame_image()
        if image.isNull():
            return False
        self._poster = QPixmap.fromImage(image)
        self.poster_position = max(0, self.player.position())
        self.update()
        return True

    def _position_changed(self, position):
        if self._requested_position is not None and abs(position - self._requested_position) <= 250:
            self._requested_position = None

    def current_position(self):
        if self._pending_position is not None:
            return self._pending_position
        if self._requested_position is not None:
            return self._requested_position
        return self.player.position()

    def seek(self, position):
        """Seek and reveal the new decoded frame instead of a stale poster."""
        duration = self.player.duration()
        position = max(0, int(position))
        if duration > 0:
            position = min(position, duration)
        self._restore_timer.stop()
        self._pending_position = self._pending_poster = None
        self._restore_seek_sent = False
        self.clear_poster()
        self._requested_position = position
        self.player.setPosition(position)
        self.update()

    def restore_state(self, data):
        """Restore sound immediately and seek once the media backend is ready."""
        self.audio.setVolume(max(0.0, min(1.0, float(data.get("volume", 1.0)))))
        self.audio.setMuted(bool(data.get("muted", True)))
        position = max(0, int(data.get("position", 0)))
        poster = data.get("poster_position")
        self._pending_poster = max(0, int(poster)) if poster is not None else None
        self._pending_position = self._pending_poster if self._pending_poster is not None else position
        self._requested_position = None
        self.poster_position = self._pending_poster if self._pending_poster is not None else -1
        self._poster = QPixmap()
        self._restore_seek_sent = False
        self._restore_attempts = 0
        self._restore_timer.start()
        self.update()

    def _media_status(self, *_):
        if self._pending_position is not None:
            self._restore_seek_sent = False
            self._restore_attempts = 0
            self._restore_timer.start()

    def _restore_step(self):
        if self._pending_position is None:
            self._restore_timer.stop()
            return
        status = self.player.mediaStatus()
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._pending_position = self._pending_poster = None
            self.poster_position = -1
            self._restore_timer.stop()
            return
        if status in (QMediaPlayer.MediaStatus.NoMedia, QMediaPlayer.MediaStatus.LoadingMedia):
            self._restore_attempts += 1
            if self._restore_attempts > 100:
                self._restore_timer.stop()
            return
        target = self._pending_position
        if not self._restore_seek_sent:
            duration = self.player.duration()
            if duration > 0:
                target = min(target, duration)
                self._pending_position = target
                if self._pending_poster is not None:
                    self._pending_poster = target
                    self.poster_position = target
            if self._pending_poster is not None and abs(self.player.position() - target) <= 250:
                image = self.decoded_frame_image()
                if not image.isNull():
                    self._poster = QPixmap.fromImage(image)
                    self.poster_position = self._pending_poster
                    self._pending_position = self._pending_poster = None
                    self._restore_timer.stop()
                    self.update()
                    return
            self._restore_frame_serial = self._frame_serial
            self.player.setPosition(target)
            self._restore_seek_sent = True
            self._restore_attempts = 0
            return
        self._restore_attempts += 1
        difference = abs(self.player.position() - target)
        position_ready = difference <= 250
        if self._pending_poster is None:
            if position_ready or self._restore_attempts > 12:
                self._pending_position = None
                self._restore_timer.stop()
            return
        image = self.decoded_frame_image()
        if (not image.isNull() and position_ready
                and self._frame_serial != self._restore_frame_serial):
            self._poster = QPixmap.fromImage(image)
            self.poster_position = self._pending_poster
            self._pending_position = self._pending_poster = None
            self._restore_timer.stop()
            self.update()
        elif self._restore_attempts > 60:
            # A backend may not expose frames while paused. Keep the restored
            # playback position and let a later seek or Play provide a frame.
            self._pending_position = self._pending_poster = None
            self.poster_position = -1
            self._restore_timer.stop()

    def paint(self, p, opt, w=None):
        paused = self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState
        if not paused or self._poster.isNull():
            super().paint(p, opt, w)
        else:
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawPixmap(self.boundingRect(), self._poster,
                         QRectF(0, 0, self._poster.width(), self._poster.height()))
        if paused:
            r = self.boundingRect()
            center = r.center()
            radius = min(34.0, r.width() / 8, r.height() / 8)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 150))
            p.drawEllipse(center, radius, radius)
            triangle = QPainterPath()
            triangle.moveTo(center.x() - radius * 0.25, center.y() - radius * 0.45)
            triangle.lineTo(center.x() + radius * 0.5, center.y())
            triangle.lineTo(center.x() - radius * 0.25, center.y() + radius * 0.45)
            triangle.closeSubpath()
            p.setBrush(QColor(255, 255, 255, 230))
            p.drawPath(triangle)
        if self.isSelected():
            draw_selection(self, p)

    def notify(self, text):
        scene = self.scene()
        views = scene.views() if scene else []
        if views and hasattr(views[0], "win"):
            views[0].win.toast(text)

    def _media_error(self, error, message):
        if message:
            self.notify("Video error: " + message)

    def toggle(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._user_paused = True
            self._auto_paused = False
            self.player.pause()
        else:
            self._user_paused = False
            self._auto_paused = False
            self.player.play()
            self.notify("Playing video • double-click or Space to pause")

    def set_view_visible(self, visible):
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        if not visible and playing:
            self._auto_paused = True
            self.player.pause()
        elif visible and self._auto_paused and not self._user_paused:
            self._auto_paused = False
            self.player.play()

    def toggle_mute(self):
        self.audio.setMuted(not self.audio.isMuted())

    def close(self):
        self._restore_timer.stop()
        self.player.stop()
        self.player.setSource(QUrl())


class GroupItem(QGraphicsItemGroup):
    kind = "group"

    def __init__(self, title="Group"):
        super().__init__()
        self.uid, self.title, self.note, self.path = new_id(), title, None, None
        self.setFlags(FLAGS)

    def members(self):
        return [c for c in self.childItems() if not isinstance(c, NoteItem)]

    def members_rect(self):
        r = QRectF()
        for c in self.members():
            r = r.united(c.mapRectToParent(c.boundingRect().united(c.childrenBoundingRect())))
        return r

    def frame(self):
        return self.members_rect().adjusted(-12, -30, 12, 12)

    def boundingRect(self):
        return self.frame().united(self.childrenBoundingRect())

    def refresh(self):
        self.prepareGeometryChange()
        self.update()

    def paint(self, p, opt, w=None):
        r = self.frame()
        pen = QPen(QColor(ACCENT) if self.isSelected() else QColor("#8a8a8a"), 2, Qt.DashLine)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(QColor(255, 255, 255, 14))
        p.drawRoundedRect(r, 6, 6)
        p.setPen(QColor("#e0e0e0"))
        p.drawText(r.adjusted(10, 6, -10, 0), Qt.AlignLeft | Qt.AlignTop, self.title)
        if self.isSelected():
            draw_handle(self, p, r)


def walk(item):
    yield item
    if item.kind == "group":
        for c in item.members():
            yield from walk(c)