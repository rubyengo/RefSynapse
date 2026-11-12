"""Item tools: locking and dragging objects out to other applications.

Export helpers are imported inside the methods: this module is loaded very
early (scene_io depends on it) and must not pull the export package with it.
"""
import os
import shutil
import tempfile

from PySide6.QtCore import Qt, QPoint, QMimeData, QUrl
from PySide6.QtGui import QDrag, QPixmap
from PySide6.QtWidgets import QGraphicsItem


def is_locked(item):
    return bool(getattr(item, "locked", False))


def set_locked(item, on):
    """A locked object keeps its geometry: Qt itself refuses to move it."""
    item.locked = bool(on)
    item.setFlag(QGraphicsItem.ItemIsMovable, not item.locked)
    item.update()


class ItemToolsMixin:
    # ------------------------------------------------------------------ lock
    def transformable_selected(self):
        """Selected top-level items that are allowed to change geometry."""
        return [i for i in self.selected_top() if not is_locked(i)]

    def toggle_lock_selected(self):
        sel = self.selected_top()
        if not sel:
            self.toast("Select something to lock")
            return
        lock = not all(is_locked(i) for i in sel)
        if lock and getattr(self.view, "_tf", None):
            self.view.tf_end(False, quiet=True)
        for item in sel:
            set_locked(item, lock)
        if "lock" in self.act:
            self.act["lock"].setChecked(lock)
        self.view.viewport().update()
        self.toast("Locked — geometry protected" if lock else "Unlocked")

    # -------------------------------------------------------------- drag out
    def drag_files(self, items):
        """Write items into a fresh temp folder and return the written paths."""
        from .export.clipboard import item_image
        from .export.names import crop_suffix, default_name, unique_path
        from .export.renderers import save_image

        folder = tempfile.mkdtemp(prefix="refsynapse-drag-")
        previous = getattr(self, "_drag_dir", None)
        self._drag_dir = folder
        if previous:
            shutil.rmtree(previous, ignore_errors=True)
        paths = []
        for item in items:
            path = unique_path(os.path.join(folder, default_name(item, crop_suffix(item))))
            if item.kind == "image" and hasattr(item, "export_image"):
                ok = item.export_image(path)
            else:
                ok = save_image(item_image(self, item), path)
            if ok:
                paths.append(path)
        return paths

    def start_drag_out(self, widget, items):
        """Hand the objects to another application as real files."""
        from .export.clipboard import item_image

        items = [i for i in items if i is not None]
        if not items:
            return False
        paths = self.drag_files(items)
        if not paths:
            self.toast("Nothing to drag out")
            return False
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        drag = QDrag(widget)
        drag.setMimeData(mime)
        image = item_image(self, items[0])
        if image is not None and not image.isNull():
            pm = QPixmap.fromImage(image)
            if pm.width() > 160 or pm.height() > 160:
                pm = pm.scaled(160, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            drag.setPixmap(pm)
            drag.setHotSpot(QPoint(pm.width() // 2, pm.height() // 2))
        drag.exec(Qt.CopyAction)
        self.toast(f"Dragged out {len(paths)} file(s)")
        return True

    def clear_drag_dir(self):
        folder, self._drag_dir = getattr(self, "_drag_dir", None), None
        if folder:
            shutil.rmtree(folder, ignore_errors=True)