"""Export menu, dialogs and actions. The only place main_window talks to."""
import os
import shutil

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QInputDialog, QMessageBox,
                               QProgressDialog, QVBoxLayout)

from ..config import APP_DIR, APP_NAME
from ..items import walk
from .board import render_board, render_items
from .names import base_name, crop_suffix, default_name, safe_filename, unique_path
from .renderers import (document_page_image, preview_image, save_image, video_image)

IMAGE_FILTERS = "PNG (*.png);;JPEG (*.jpg *.jpeg);;WebP (*.webp);;BMP (*.bmp)"
SCALES = (("100%", 1.0), ("150%", 1.5), ("200%", 2.0), ("300%", 3.0))
MEDIA_KINDS = ("image", "video", "document", "model3d")


class PngOptionsDialog(QDialog):
    """Scale and background for board / selection pictures."""

    def __init__(self, parent, title):
        super().__init__(parent)
        self.setWindowTitle(title)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.scale = QComboBox()
        for label, value in SCALES:
            self.scale.addItem(label, value)
        self.scale.setCurrentIndex(1)
        self.background = QComboBox()
        self.background.addItem("Dark (#2b2b2b)", "#2b2b2b")
        self.background.addItem("Transparent", None)
        self.background.addItem("White", "#ffffff")
        form.addRow("Scale:", self.scale)
        form.addRow("Background:", self.background)
        lay.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def values(self):
        return self.scale.currentData(), self.background.currentData()


class ExportController:
    def __init__(self, win):
        self.win = win

    # ------------------------------------------------------- installation
    def install(self):
        win = self.win

        def A(key, text, shortcut, slot):
            action = QAction(text, win)
            if shortcut:
                keys = shortcut if isinstance(shortcut, list) else [shortcut]
                action.setShortcuts([QKeySequence(k) for k in keys])
            action.triggered.connect(lambda *_, s=slot: s())
            win.addAction(action)
            win.act[key] = action

        A("export", "Export Selected...", "Ctrl+E", self.export_selected)
        A("export_selection_png", "Export Selection as PNG...", None, self.export_selection_png)
        A("export_board", "Export Board as PNG...", "Ctrl+Shift+E", self.export_board_png)
        A("export_pages", "Export Document Pages...", None, self.export_document_pages)
        A("export_originals", "Export Original Files...", None, self.export_originals)
        self.update_actions()

    def add_menu(self, menu):
        self.update_actions()
        sub = menu.addMenu("Export")
        for key in ("export", "export_selection_png", "export_board", None,
                    "export_pages", "export_originals"):
            if key is None:
                sub.addSeparator()
            else:
                sub.addAction(self.win.act[key])
        return sub

    def update_actions(self):
        act = getattr(self.win, "act", None)
        if not act or "export" not in act:
            return
        targets = self.targets()
        act["export"].setEnabled(bool(targets))
        act["export_selection_png"].setEnabled(bool(targets))
        act["export_board"].setEnabled(bool(self.win.top_items()))
        act["export_pages"].setEnabled(len(self.documents()) == 1)
        act["export_originals"].setEnabled(bool(self.media_items()))

    # ---------------------------------------------------------- selection
    def targets(self):
        return self.win.selected_top()

    def media_items(self):
        return [x for it in self.targets() for x in walk(it) if x.kind in MEDIA_KINDS]

    def documents(self):
        return [x for x in self.media_items() if x.kind == "document"]

    # ------------------------------------------------------------ helpers
    def _finish_crop(self):
        view = getattr(self.win, "view", None)
        if view is not None and getattr(view, "_crop", None):
            view.finish_crop(False)

    def _start_dir(self, item=None):
        path = getattr(item, "path", "") if item is not None else ""
        folder = os.path.dirname(path) if path else ""
        if folder and os.path.isdir(folder):
            return folder
        if self.win.project_path:
            return os.path.dirname(self.win.project_path)
        return APP_DIR

    def _save_dialog(self, default_path):
        path, _ = QFileDialog.getSaveFileName(self.win, "Export", default_path, IMAGE_FILTERS)
        return path or None

    def _folder_dialog(self, title, item=None):
        return QFileDialog.getExistingDirectory(self.win, title, self._start_dir(item)) or None

    def _fail(self, path):
        QMessageBox.warning(self.win, APP_NAME, "Could not export:\n" + path)

    def _report(self, done, failed):
        if failed:
            QMessageBox.warning(self.win, APP_NAME,
                                f"Exported {done} file(s).\nCould not export:\n"
                                + "\n".join(failed[:10]))
        elif done:
            self.win.toast(f"Exported {done} file(s)")
        else:
            self.win.toast("Nothing was exported")

    def _write_item(self, item, path):
        kind = getattr(item, "kind", None)
        if kind == "image" and hasattr(item, "export_image"):
            return bool(item.export_image(path))
        if kind == "video":
            image = video_image(item)
            if image.isNull():
                self.win.toast("No decoded video frame — play or seek the video first")
                return False
            return save_image(image, path)
        image = preview_image(item)
        if image is None:
            image = render_items(self.win, [item], scale=2.0)
        return save_image(image, path)

    # ------------------------------------------------------------ actions
    def export_selected(self):
        targets = self.targets()
        if not targets:
            self.win.toast("Select something to export")
            return
        self._finish_crop()
        if len(targets) == 1:
            item = targets[0]
            default = default_name(item, crop_suffix(item))
            path = self._save_dialog(os.path.join(self._start_dir(item), default))
            if not path:
                return
            if self._write_item(item, path):
                self.win.toast("Exported: " + os.path.basename(path))
            else:
                self._fail(path)
            return
        folder = self._folder_dialog("Export Selected Items", targets[0])
        if not folder:
            return
        done, failed = 0, []
        for item in targets:
            path = unique_path(os.path.join(folder, default_name(item, crop_suffix(item))))
            if self._write_item(item, path):
                done += 1
            else:
                failed.append(base_name(item))
        self._report(done, failed)

    def export_selection_png(self):
        targets = self.targets()
        if not targets:
            self.win.toast("Select items to export")
            return
        self._finish_crop()
        options = PngOptionsDialog(self.win, "Export Selection as PNG")
        if not options.exec():
            return
        scale, background = options.values()
        path = self._save_dialog(os.path.join(self._start_dir(targets[0]), "selection.png"))
        if not path:
            return
        image = render_items(self.win, targets, scale, background)
        if save_image(image, path):
            self.win.toast(f"Exported selection ({image.width()} x {image.height()})")
        else:
            self._fail(path)

    def export_board_png(self):
        if not self.win.top_items():
            self.win.toast("The board is empty")
            return
        self._finish_crop()
        options = PngOptionsDialog(self.win, "Export Board as PNG")
        if not options.exec():
            return
        scale, background = options.values()
        path = self._save_dialog(os.path.join(self._start_dir(), "board.png"))
        if not path:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            image = render_board(self.win, scale, background)
        finally:
            QApplication.restoreOverrideCursor()
        if save_image(image, path):
            self.win.toast(f"Exported board ({image.width()} x {image.height()})")
        else:
            self._fail(path)

    def export_document_pages(self):
        documents = self.documents()
        if len(documents) != 1:
            self.win.toast("Select one document")
            return
        item = documents[0]
        total = max(1, item.page_count())
        labels = [label for label, _ in SCALES]
        choice, ok = QInputDialog.getItem(self.win, "Document Pages",
                                          "Render scale:", labels, 2, False)
        if not ok:
            return
        scale = dict(SCALES)[choice]
        folder = self._folder_dialog("Export Document Pages", item)
        if not folder:
            return
        progress = QProgressDialog("Rendering pages…", "Cancel", 0, total, self.win)
        progress.setWindowTitle(APP_NAME)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        done, failed = 0, []
        base = base_name(item)
        try:
            for page in range(total):
                if progress.wasCanceled():
                    break
                progress.setValue(page)
                QApplication.processEvents()
                suffix = f"_page{page + 1:03d}" if total > 1 else ""
                path = unique_path(os.path.join(folder, f"{base}{suffix}.png"))
                image = document_page_image(item, page, scale)
                if save_image(image, path):
                    done += 1
                else:
                    failed.append(f"page {page + 1}")
        finally:
            progress.close()
        self._report(done, failed)

    def export_originals(self):
        items = self.media_items()
        if not items:
            self.win.toast("Select media to export")
            return
        folder = self._folder_dialog("Export Original Files", items[0])
        if not folder:
            return
        done, failed = 0, []
        for item in items:
            source = getattr(item, "path", "") or ""
            if not os.path.isfile(source):
                failed.append(base_name(item) + " (missing)")
                continue
            ext = os.path.splitext(source)[1]
            target = unique_path(os.path.join(folder, safe_filename(base_name(item) + ext)))
            try:
                shutil.copy2(source, target)
                done += 1
            except OSError:
                failed.append(os.path.basename(source))
        self._report(done, failed)