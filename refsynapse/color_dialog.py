"""Color dialog with Photoshop-style board eyedropper and paint bucket buttons."""
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QColorDialog, QDialogButtonBox, QHBoxLayout,
                               QPushButton, QWidget)

def _icon_canvas(size=20):
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    return pm

def eyedropper_icon(color=QColor("#e6e6e6")):
    """A pipette tilted like the Photoshop eyedropper."""
    pm = _icon_canvas()
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.translate(10, 10)
    p.rotate(45)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawRoundedRect(QRectF(-3.0, -8.6, 6.0, 4.2), 1.6, 1.6)   # bulb
    p.drawRoundedRect(QRectF(-1.8, -5.0, 3.6, 7.0), 0.8, 0.8)   # barrel
    p.drawPolygon(QPolygonF([QPointF(-1.8, 2.0), QPointF(1.8, 2.0),
                             QPointF(0.0, 7.6)]))               # tip
    p.end()
    return QIcon(pm)

def bucket_icon(paint=QColor("#4fc3f7"), body=QColor("#e6e6e6")):
    """A tilted paint bucket; the drop carries the color about to be applied."""
    pm = _icon_canvas()
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.translate(9.5, 10)
    p.rotate(-25)
    p.setPen(QPen(body, 1.4))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(QRectF(-5.4, -9.4, 10.8, 10.8), 0, 180 * 16)      # handle
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(body)
    p.drawPolygon(QPolygonF([QPointF(-5.4, -3.4), QPointF(5.4, -3.4),
                             QPointF(3.7, 6.4), QPointF(-3.7, 6.4)]))
    p.setBrush(paint)
    p.drawEllipse(QPointF(-6.6, 4.2), 1.9, 2.5)                 # falling drop
    p.end()
    return QIcon(pm)

class CanvasColorDialog(QColorDialog):
    """Standard Qt color dialog plus two board tools.

    Pick from board: closes the dialog and lets the main window run the canvas
    eyedropper with no modal dialog alive (a live modal dialog disables the
    board and the pick would never arrive — the old freeze). The window then
    reopens this dialog with the sampled color.
    Fill on board: accepts the dialog and arms the paint bucket, so the chosen
    color can be applied to frames, strokes, text and images right away.
    """

    def __init__(self, color, win):
        super().__init__(color, win)
        self.win = win
        self.fill_requested = False
        self.pick_requested = False
        # The native dialog gives no access to its layout or button box.
        self.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
        self.setWindowTitle("Canvas tools color")

        view = getattr(win, "view", None)
        self.pick_btn = QPushButton(eyedropper_icon(), "Pick from board")
        self.pick_btn.setToolTip("Sample a color from the board with the magnifier")
        self.pick_btn.setEnabled(callable(getattr(view, "arm_picker", None)))
        self.pick_btn.clicked.connect(self._pick_from_board)

        self.fill_btn = QPushButton(bucket_icon(QColor(color)), "Fill on board")
        self.fill_btn.setToolTip("Apply this color with the paint bucket")
        self.fill_btn.setEnabled(callable(getattr(view, "_bucket_activate", None)))
        self.fill_btn.clicked.connect(self._fill_on_board)

        # ActionRole keeps the buttons in the OK/Cancel row without making
        # them close the dialog on their own.
        box = self.findChild(QDialogButtonBox)
        if box is not None:
            box.addButton(self.pick_btn, QDialogButtonBox.ButtonRole.ActionRole)
            box.addButton(self.fill_btn, QDialogButtonBox.ButtonRole.ActionRole)
        else:
            bar = QWidget(self)
            row = QHBoxLayout(bar)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            row.addWidget(self.pick_btn)
            row.addWidget(self.fill_btn)
            row.addStretch(1)
            layout = self.layout()
            layout.addWidget(bar, layout.rowCount(), 0, 1, layout.columnCount())

        self.currentColorChanged.connect(self._sync_fill_icon)

    def _sync_fill_icon(self, color):
        self.fill_btn.setIcon(bucket_icon(QColor(color)))

    # ---------- board eyedropper ----------
    def _pick_from_board(self):
        # Close ourselves instead of hiding a live modal dialog: while exec()
        # is running the board is disabled and would never receive the click.
        self.pick_requested = True
        self.accept()

    # ---------- paint bucket ----------
    def _fill_on_board(self):
        self.fill_requested = True
        self.accept()