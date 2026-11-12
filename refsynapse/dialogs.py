"""Settings, startup, search and title-bar UI."""
import os, copy
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QColor
from PySide6.QtWidgets import (QFrame, QLabel, QVBoxLayout, QHBoxLayout,
                               QGridLayout, QFormLayout, QComboBox, QPushButton, QSpinBox,
                               QDoubleSpinBox, QCheckBox, QDialogButtonBox, QGroupBox,
                               QDialog, QToolButton, QSizePolicy)
from .config import (APP_NAME, ICON_PATH, PRESETS, DEFAULT_PRESET, NAV_ACTIONS,
                     TRANSFORM_KEYS, HOLD_KEYS, preset_nav, binding_text,
                     nav_conflicts, mods_of, button_name, B, from_portable, ev)

try:
    from .color_dialog import eyedropper_icon, bucket_icon
except ImportError:      # color_dialog.py not installed yet: fall back to text buttons
    eyedropper_icon = bucket_icon = None


class CaptureArea(QFrame):
    """Click here with the desired modifiers / held key to record a binding."""

    def __init__(self, dlg):
        super().__init__(dlg)
        self.dlg, self.key = dlg, ""
        self.setMinimumSize(420, 150)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setStyleSheet("QFrame { background: #1e1e1e; border: 1px dashed #666; }")
        lay = QVBoxLayout(self)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet("border: none; color: #ccc;")
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay.addWidget(self.label)
        self.refresh()

    def refresh(self):
        held = self.key.capitalize() if self.key else "none"
        self.label.setText("Hold Ctrl / Shift / Alt and, optionally, Space or Z,\n"
                           "then click a mouse button inside this box.\n\n"
                           f"Held key: {held}")

    def keyPressEvent(self, e):
        name = HOLD_KEYS.get(ev(e.key()))
        if name:
            self.key = name
            self.refresh()
            return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        name = HOLD_KEYS.get(ev(e.key()))
        if name and name == self.key and not e.isAutoRepeat():
            self.key = ""
            self.refresh()
            return
        super().keyReleaseEvent(e)

    def mousePressEvent(self, e):
        b = button_name(e.button())
        if b:
            self.dlg.binding = B(b, *mods_of(e.modifiers()), key=self.key)
            self.dlg.accept()

    def contextMenuEvent(self, e):
        e.accept()


class CaptureDialog(QDialog):
    def __init__(self, parent, title):
        super().__init__(parent)
        self.setWindowTitle(f"Binding: {title}")
        self.binding, self.cleared = None, False
        lay = QVBoxLayout(self)
        self.area = CaptureArea(self)
        lay.addWidget(self.area)
        row = QHBoxLayout()
        clear, cancel = QPushButton("Clear binding"), QPushButton("Cancel")
        for b in (clear, cancel):
            b.setFocusPolicy(Qt.NoFocus)
            row.addWidget(b)
        clear.clicked.connect(self._clear)
        cancel.clicked.connect(self.reject)
        lay.addLayout(row)
        self.area.setFocus()

    def _clear(self):
        self.cleared = True
        self.accept()


class BindingButton(QPushButton):
    def __init__(self, title, on_change):
        super().__init__()
        self.title, self.on_change, self.binding = title, on_change, None
        self.setMinimumWidth(190)
        self.clicked.connect(self.capture)

    def set_binding(self, b):
        self.binding = copy.deepcopy(b)
        self.setText(binding_text(b))

    def capture(self):
        d = CaptureDialog(self.window(), self.title)
        if d.exec():
            self.set_binding(None if d.cleared else d.binding)
            self.on_change()


class SettingsDialog(QDialog):
    def __init__(self, nav, parent):
        super().__init__(parent)
        self.setWindowTitle("RefSynapse - Settings")
        self._loading = False
        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Preset:"))
        self.preset = QComboBox()
        self.preset.addItems(list(PRESETS) + ["Custom"])
        top.addWidget(self.preset, 1)
        lay.addLayout(top)

        gb = QGroupBox("Navigation - mouse (click a button to change it)")
        g = QGridLayout(gb)
        g.addWidget(QLabel("Primary"), 0, 1)
        g.addWidget(QLabel("Secondary"), 0, 2)
        self.buttons = {}
        for r, (key, label) in enumerate(NAV_ACTIONS, 1):
            g.addWidget(QLabel(label), r, 0)
            self.buttons[key] = [BindingButton(label, self.changed), BindingButton(label, self.changed)]
            g.addWidget(self.buttons[key][0], r, 1)
            g.addWidget(self.buttons[key][1], r, 2)
        lay.addWidget(gb)

        gb2 = QGroupBox("Wheel and zoom")
        f = QFormLayout(gb2)
        self.wheel = QComboBox()
        self.wheel.addItem("Zoom", "zoom")
        self.wheel.addItem("Scroll (Alt + Wheel = zoom, Ctrl/Shift + Wheel = horizontal)", "scroll")
        self.zoom_step = QSpinBox()
        self.zoom_step.setRange(2, 100)
        self.zoom_step.setSuffix(" %")
        self.invert = QCheckBox("Invert wheel zoom direction")
        self.zoom_drag = QDoubleSpinBox()
        self.zoom_drag.setRange(0.1, 5.0)
        self.zoom_drag.setSingleStep(0.1)
        f.addRow("Mouse wheel:", self.wheel)
        f.addRow("Wheel zoom step:", self.zoom_step)
        f.addRow("", self.invert)
        f.addRow("Drag zoom speed:", self.zoom_drag)
        lay.addWidget(gb2)

        gb3 = QGroupBox("Item transforms")
        f3 = QFormLayout(gb3)
        self.tf_keys = QComboBox()
        for k, (label, _) in TRANSFORM_KEYS.items():
            self.tf_keys.addItem(label, k)
        f3.addRow("Move / Rotate / Scale keys:", self.tf_keys)
        lay.addWidget(gb3)

        snap_box = QGroupBox("Snapping and grid")
        snap_form = QFormLayout(snap_box)
        settings = parent.settings if parent is not None else {}
        self.snap_enabled = QCheckBox("Hold Ctrl to align to object edges and centers")
        self.snap_enabled.setChecked(bool(settings.get("snap_enabled", True)))
        self.snap_grid = QCheckBox("Hold Ctrl to snap movement and crop to grid")
        self.snap_grid.setChecked(bool(settings.get("snap_grid", False)))
        self.grid_visible = QCheckBox("Show grid on canvas")
        self.grid_visible.setChecked(bool(settings.get("grid_visible", False)))
        self.grid_size = QSpinBox()
        self.grid_size.setRange(5, 1000)
        self.grid_size.setSuffix(" px")
        self.grid_size.setValue(int(settings.get("grid_size", 50)))
        self.snap_tolerance = QSpinBox()
        self.snap_tolerance.setRange(2, 40)
        self.snap_tolerance.setSuffix(" px")
        self.snap_tolerance.setValue(int(settings.get("snap_tolerance", 10)))
        snap_form.addRow(self.snap_enabled)
        snap_form.addRow(self.snap_grid)
        snap_form.addRow(self.grid_visible)
        snap_form.addRow("Grid size:", self.grid_size)
        snap_form.addRow("Snap distance:", self.snap_tolerance)
        lay.addWidget(snap_box)

        self.warn = QLabel()
        self.warn.setStyleSheet("color: #ff8a65;")
        self.warn.setWordWrap(True)
        lay.addWidget(self.warn)

        info = QLabel("A right click without dragging always opens the menu.  "
                      "Ctrl+Shift+Wheel changes window opacity.\n"
                      "Transforms: press the key, move the mouse, LMB / Enter to confirm, "
                      "RMB / Esc to cancel, Ctrl to snap, X / Y to lock an axis while moving.")
        info.setStyleSheet("color: #999;")
        info.setWordWrap(True)
        lay.addWidget(info)

        SB = QDialogButtonBox.StandardButton
        bb = QDialogButtonBox(SB.Ok | SB.Cancel | SB.RestoreDefaults)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bb.button(SB.RestoreDefaults).clicked.connect(lambda: self.load(preset_nav(DEFAULT_PRESET)))
        lay.addWidget(bb)

        self.load(nav)
        self.preset.currentTextChanged.connect(self.on_preset)
        for w in (self.wheel, self.tf_keys):
            w.currentIndexChanged.connect(self.changed)
        for w in (self.zoom_step, self.zoom_drag):
            w.valueChanged.connect(self.changed)
        self.invert.toggled.connect(self.changed)

    def load(self, nav):
        self._loading = True
        p = nav.get("preset")
        self.preset.setCurrentText(p if p in PRESETS else "Custom")
        for key, _ in NAV_ACTIONS:
            bl = list(nav["bindings"].get(key) or []) + [None, None]
            self.buttons[key][0].set_binding(bl[0])
            self.buttons[key][1].set_binding(bl[1])
        self.wheel.setCurrentIndex(max(0, self.wheel.findData(nav["wheel"])))
        self.zoom_step.setValue(int(nav["zoom_step"]))
        self.invert.setChecked(bool(nav["invert_wheel"]))
        self.zoom_drag.setValue(float(nav["zoom_drag_speed"]))
        self.tf_keys.setCurrentIndex(max(0, self.tf_keys.findData(nav["transform_keys"])))
        self._loading = False
        self.check()

    def on_preset(self, name):
        if not self._loading and name in PRESETS:
            self.load(preset_nav(name))

    def changed(self, *_):
        if self._loading:
            return
        self._loading = True
        self.preset.setCurrentText("Custom")
        self._loading = False
        self.check()

    def result_nav(self):
        return {
            "preset": self.preset.currentText(),
            "wheel": self.wheel.currentData(),
            "zoom_step": self.zoom_step.value(),
            "invert_wheel": self.invert.isChecked(),
            "zoom_drag_speed": round(self.zoom_drag.value(), 2),
            "transform_keys": self.tf_keys.currentData(),
            "bindings": {k: [copy.deepcopy(b.binding) for b in self.buttons[k]]
                         for k, _ in NAV_ACTIONS},
        }

    def result_snap(self):
        return {"snap_enabled": self.snap_enabled.isChecked(),
                "snap_grid": self.snap_grid.isChecked(),
                "grid_visible": self.grid_visible.isChecked(),
                "grid_size": self.grid_size.value(),
                "snap_tolerance": self.snap_tolerance.value()}

    def check(self):
        c = nav_conflicts(self.result_nav())
        self.warn.setText("Conflict: " + "; ".join(c) if c else "")


# =====================================================================
# Main window
# =====================================================================
class StartupDialog(QDialog):
    """Blender-like start page: new, open and recent projects."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle(f"{APP_NAME} — Start")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 22)
        lay.setSpacing(10)

        header = QHBoxLayout()
        icon = QLabel()
        pm = QPixmap(ICON_PATH)
        if not pm.isNull():
            icon.setPixmap(pm.scaled(52, 52, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        header.addWidget(icon)
        title = QLabel(f"<h2>{APP_NAME}</h2><div>Reference board</div>")
        header.addWidget(title, 1)
        lay.addLayout(header)
        lay.addSpacing(10)

        new_button = QPushButton("New Project")
        open_button = QPushButton("Open…")
        for button in (new_button, open_button):
            button.setMinimumHeight(38)
            lay.addWidget(button)
        new_button.clicked.connect(self.accept)
        open_button.clicked.connect(lambda: self.open_project())

        recent_label = QLabel("<b>Recent</b>")
        lay.addWidget(recent_label)
        count = 0
        for saved in self.win.settings.get("recent", []):
            full = from_portable(saved)
            if not os.path.isfile(full):
                continue
            button = QPushButton(os.path.basename(full))
            button.setToolTip(full)
            button.setMinimumHeight(32)
            button.clicked.connect(lambda *_, p=full: self.open_project(p))
            lay.addWidget(button)
            count += 1
            if count >= 6:
                break
        if not count:
            empty = QLabel("No recent projects")
            empty.setStyleSheet("color: #888888;")
            lay.addWidget(empty)
        lay.addStretch(1)

    def open_project(self, path=None):
        self.win.open_project(path)
        if self.win.project_path:
            self.accept()


class TitleBar(QFrame):
    """Visible project title, drawing tools and native window controls."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setObjectName("titleBar")
        self.setFixedHeight(42)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 6, 4)
        lay.setSpacing(6)

        icon = QLabel()
        pm = QPixmap(ICON_PATH)
        if not pm.isNull():
            icon.setPixmap(pm.scaled(22, 22, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        lay.addWidget(icon)

        self.file_button = QToolButton()
        self.file_button.setText("File")
        self.file_button.clicked.connect(lambda: self.win.show_file_menu(self.file_button))
        lay.addWidget(self.file_button)

        self.tools = {}
        for key, label in (("select", "Select"), ("draw", "Draw"),
                           ("text", "Text"), ("frame", "Frame")):
            b = QToolButton()
            b.setText(label)
            b.setCheckable(True)
            b.clicked.connect(lambda *_, k=key: self.win.toggle_tool(k)
                              if k in ("text", "frame") else self.win.set_tool(k))
            self.tools[key] = b
            lay.addWidget(b)
        self.tools["select"].setChecked(True)

        # Color tools: icon-only eyedropper and paint bucket, Photoshop style.
        for key, tip in (("picker", "Eyedropper — pick a color from the board (I)"),
                         ("bucket", "Paint bucket — recolor a frame, stroke, text or image (Shift+I)")):
            b = QToolButton()
            icon_factory = eyedropper_icon if key == "picker" else bucket_icon
            if icon_factory is not None:
                b.setIcon(icon_factory())
            else:
                b.setText("Pick" if key == "picker" else "Fill")
            b.setCheckable(True)
            b.setToolTip(tip)
            b.clicked.connect(lambda *_, k=key: self.toggle_color_tool(k))
            self.tools[key] = b
            lay.addWidget(b)

        self.color_button = QPushButton()
        self.color_button.setFixedWidth(30)
        self.color_button.setToolTip("Drawing, text and frame color")
        self.color_button.clicked.connect(self.win.choose_draw_color)
        lay.addWidget(self.color_button)

        self.width = QSpinBox()
        self.width.setRange(1, 60)
        self.width.setValue(10)
        self.width.setSuffix(" px")
        self.width.setToolTip("Brush size")
        self.width.valueChanged.connect(self.win.set_draw_width)
        lay.addWidget(self.width)

        lay.addStretch(1)
        self.project = QLabel(APP_NAME)
        self.project.setObjectName("appTitle")
        self.project.setAlignment(Qt.AlignCenter)
        self.project.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.project.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        lay.addWidget(self.project)
        lay.addStretch(1)

        for text, tip, fn in (("—", "Minimize", win.showMinimized),
                              ("□", "Maximize / Restore", win.toggle_maximized),
                              ("×", "Close", win.close)):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setObjectName("windowButton" if text != "×" else "closeButton")
            b.setFixedSize(34, 28)
            b.clicked.connect(fn)
            lay.addWidget(b)

    def toggle_color_tool(self, key):
        """Arm / disarm the eyedropper or the bucket, with their own activation
        (loupe, event filter, hint toast) instead of a bare tool switch."""
        view = getattr(self.win, "view", None)
        if view is None:
            return
        if getattr(view, "tool", None) == key:
            self.win.set_tool("select")
            return
        name = "_picker_activate" if key == "picker" else "_bucket_activate"
        activate = getattr(view, name, None)
        if callable(activate):
            activate()
        else:
            self.win.set_tool(key)

    def set_project(self, name):
        self.project.setText(name)
        self.project.setToolTip(name)

    def set_color(self, color):
        self.color_button.setStyleSheet(
            f"background: {color}; border: 2px solid #dddddd; border-radius: 4px;")
        # The bucket drop previews the color that is about to be applied.
        if bucket_icon is not None and "bucket" in self.tools:
            self.tools["bucket"].setIcon(bucket_icon(QColor(color)))

    def set_tool(self, tool):
        # The canvas may already have switched tools itself (hotkeys, picker
        # activation), so trust its live state over the requested one.
        view = getattr(self.win, "view", None)
        actual = getattr(view, "tool", None) or tool
        for key, button in self.tools.items():
            button.setChecked(key == actual)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and not self.win.isMaximized():
            self.win.windowHandle().startSystemMove()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.win.toggle_maximized()
            e.accept()
            return
        super().mouseDoubleClickEvent(e)