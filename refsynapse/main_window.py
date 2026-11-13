"""Main RefSynapse window and application commands."""
import os, json, uuid, math, time
from PySide6.QtCore import Qt, QRectF, QPointF, QPoint, QTimer, QEvent, QEventLoop, QUrl
from PySide6.QtGui import QColor, QFont, QPainterPath
from PySide6.QtWidgets import (QApplication, QMainWindow, QGraphicsScene, QGraphicsItem,
                               QInputDialog, QFileDialog, QDialog, QMessageBox,
                               QVBoxLayout, QWidget, QProgressDialog)
from .config import (APP_DIR, APP_NAME, DEFAULT_DRAW_COLOR, GAP,
                     IMAGE_EXT, VIDEO_EXT, PROJECT_EXT,
                     from_portable, load_nav, load_settings, mime_image, mods_of,
                     nav_is_hold, physical_key, preset_nav, save_settings, to_portable,
                     model_navigation_hint)
from .items import (AnnotationDialog, FrameItem, GroupItem, ImageItem, StrokeItem, TextItem,
                    VideoItem, handle_rect, note_text, place_note, set_note, walk)
from .documents import DocumentItem, DOCUMENT_EXT
from .models3d import Model3DItem, MODEL3D_EXT
from .canvas import Canvas
from .dialogs import StartupDialog, TitleBar, SettingsDialog
from .color_dialog import CanvasColorDialog
from .history import HistoryManager
from .scene_io import dump_item, build_item, update_item
from .project import (ProjectSaveThread, prepare_package, load_package, recovery_file)
from .export import ExportController
from .export.clipboard import selection_image
from .web_import import DownloadThread, is_remote, save_data_uri
from .actions import ActionsMixin
from .menus import MenusMixin
from .item_tools import is_locked
from . import help_dialog


class MainWindow(ActionsMixin, MenusMixin, QMainWindow):
    # Shortcuts / key map: actions.py.  Menus: menus.py.  F1 help: help_dialog.py.

    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        for key, default in (("snap_enabled", True), ("snap_grid", False),
                             ("grid_visible", False), ("grid_size", 50),
                             ("snap_tolerance", 10)):
            self.settings.setdefault(key, default)
        self.nav = load_nav(self.settings.get("nav"))
        self.frameless = self.settings.get("frameless", True)
        self.on_top = self.settings.get("on_top", False)
        self.project_path = None
        self.missing = []
        self._media_dir = None       # extracted media owned by the open project
        self.draw_color = self.settings.get("draw_color", DEFAULT_DRAW_COLOR)
        stored_width = int(self.settings.get("draw_width", 10))
        self.draw_width = 10 if stored_width == 4 and not self.settings.get("brush_default_10") else stored_width
        self.settings["brush_default_10"] = True
        # Strong Python references to every item on the board.
        # Without them Python's garbage collector may delete items (the v0.2 bug).
        self.objects = set()
        self.history = HistoryManager(limit=100)
        self._text_action_shortcuts = None
        self._save_thread = None
        self._save_target = None
        self._save_state = None
        self._autosave_thread = None
        self._autosave_state = None
        self._recovery_source = None
        self._dirty = False
        self._downloads = []         # running browser-image downloads
        self._picking = False        # board eyedropper runs from the color dialog
        self.scene = QGraphicsScene(-500000, -500000, 1000000, 1000000)
        self.view = Canvas(self.scene, self)
        self._build_actions()
        self.apply_transform_keys()
        # Export lives in its own package; the window only wires it up.
        self.export_controller = ExportController(self)
        self.export_controller.install()
        self.scene.selectionChanged.connect(self.update_video_controls)
        self.scene.selectionChanged.connect(self.export_controller.update_actions)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.titlebar = TitleBar(self)
        self.titlebar.set_color(self.draw_color)
        self.titlebar.width.setValue(self.draw_width)
        layout.addWidget(self.titlebar)
        layout.addWidget(self.view, 1)
        self.setCentralWidget(root)
        self.setMinimumSize(560, 360)
        self.setWindowFlag(Qt.FramelessWindowHint, self.frameless)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.on_top)
        self.setWindowOpacity(self.settings.get("opacity", 1.0))
        g = self.settings.get("geometry")
        if (isinstance(g, list) and len(g) == 4) and \
                QApplication.screenAt(QPoint(g[0] + g[2] // 2, g[1] + g[3] // 2)):
            self.setGeometry(*g)
        else:
            self.resize(1200, 800)
        self.view.centerOn(0, 0)
        self.view.setFocus()
        self.saved_state = self.snapshot()
        self.reset_history()
        self.update_title()
        QApplication.instance().installEventFilter(self)
        self.view.minimap.setVisible(self.settings.get("minimap", True))
        self.autosave_timer = QTimer(self)
        self.autosave_timer.setInterval(120000)  # recovery snapshot every 2 minutes
        self.autosave_timer.timeout.connect(self.autosave)
        self.autosave_timer.start()

    def eventFilter(self, obj, e):
        # QGraphicsScene forwards scene-mouse events through the application
        # filter too; they are not QEvent and have no type() method.
        if not isinstance(e, QEvent):
            return False
        if getattr(self, "_picking", False) and e.type() == QEvent.Type.KeyPress:
            # The board eyedropper waits for a click inside a nested event
            # loop (pick_board_color). Only Esc may pass: the canvas filter
            # turns it into a pick cancel. Any other hotkey would switch
            # tools and leave the loop waiting forever.
            return e.key() != Qt.Key_Escape
        if e.type() == QEvent.Type.KeyRelease:
            if physical_key(e) == "D" and self.view._draw_hold:
                if not e.isAutoRepeat():
                    self.view.end_draw_hold()
                return True
            return super().eventFilter(obj, e)
        if e.type() != QEvent.Type.KeyPress:
            return super().eventFilter(obj, e)
        if e.key() == Qt.Key_Escape and self.view.search.query.hasFocus():
            self.view.search.finish_search()
            return True
        key = physical_key(e)
        if self.view.search.isVisible() and self.view.search.query.hasFocus():
            return super().eventFilter(obj, e)
        if not key or self.view._editing_text or QApplication.activeModalWidget():
            return super().eventFilter(obj, e)
        mods = e.modifiers()
        plain = not bool(mods & (Qt.ControlModifier | Qt.ShiftModifier | Qt.AltModifier))
        if key == "D" and plain and not self.view._tf and not self.view._crop:
            if not e.isAutoRepeat():
                self.view.begin_draw_hold()
            return True
        # Creation tools T/F remain one-shot.
        if key in ("T", "F") and plain:
            if not e.isAutoRepeat():
                self.set_tool("text" if key == "T" else "frame")
            return True
        if self.view._tf or self.view._crop:
            return super().eventFilter(obj, e)
        if key == "Z" and nav_is_hold(self.nav, "z", mods_of(mods)):
            return super().eventFilter(obj, e)
        action = self.layout_action(key, mods)
        if action:
            if not e.isAutoRepeat() and self.act[action].isEnabled():
                self.act[action].trigger()
            return True
        return super().eventFilter(obj, e)

    # ---------- settings / help ----------
    def show_startup(self):
        if self.offer_recovery():
            return
        StartupDialog(self).exec()
        self.view.setFocus()

    def offer_recovery(self):
        path = recovery_file()
        if not os.path.isfile(path):
            return False
        result = QMessageBox.question(
            self, APP_NAME,
            "An autosaved recovery project was found. Restore the interrupted session?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if result == QMessageBox.StandardButton.Yes:
            before = self.project_path
            self.open_project(path, recovery=True)
            if self.project_path != before or self.top_items():
                self.toast("Recovered autosaved session — save it to keep the changes")
            return True
        try:
            os.remove(path)
        except OSError:
            pass
        return False

    def clear_recovery(self):
        try:
            os.remove(recovery_file())
        except OSError:
            pass

    def set_nav(self, nav):
        self.nav = nav
        self.apply_transform_keys()
        self.settings["nav"] = self.nav
        hint = model_navigation_hint(nav)
        for item in self.objects:
            if item.kind == "model3d":
                item.navigation_hint = hint
                item.update()
        save_settings(self.settings)
        self.toast(f"Preset: {self.nav.get('preset', 'Custom')}")

    def set_preset(self, name):
        self.set_nav(preset_nav(name))

    def open_settings(self):
        d = SettingsDialog(self.nav, self)
        if d.exec():
            self.set_nav(d.result_nav())
            self.settings.update(d.result_snap())
            for key, setting in (("snapping", "snap_enabled"),
                                 ("grid_snap", "snap_grid"),
                                 ("grid_visible", "grid_visible")):
                self.act[key].setChecked(bool(self.settings[setting]))
            save_settings(self.settings)
            self.view.viewport().update()

    def show_search(self):
        self.view.show_search()

    def toggle_minimap(self):
        visible = self.act["minimap"].isChecked()
        self.view.minimap.setVisible(visible)
        self.settings["minimap"] = visible
        save_settings(self.settings)

    def toggle_snapping(self):
        self.settings["snap_enabled"] = self.act["snapping"].isChecked()
        save_settings(self.settings)

    def toggle_grid_snap(self):
        self.settings["snap_grid"] = self.act["grid_snap"].isChecked()
        save_settings(self.settings)

    def toggle_grid_visible(self):
        self.settings["grid_visible"] = self.act["grid_visible"].isChecked()
        save_settings(self.settings)
        self.view.viewport().update()

    def show_help(self):
        help_dialog.show_help(self)

    # ---------- canvas tools ----------
    def toggle_tool(self, tool):
        self.set_tool("select" if self.view.tool == tool else tool)

    def set_tool(self, tool):
        if not self.view._editing_text:
            self.view.setFocus()
        self.view.set_tool(tool)
        self.titlebar.set_tool(tool)
        if tool == "draw":
            self.toast("Hold D and draw with the left mouse button • release D for Select")
        elif tool == "text":
            self.toast("Drag a text frame on the canvas, then type")
        elif tool == "frame":
            self.toast("Drag to create a frame")

    def choose_draw_color(self):
        # A live modal dialog disables the board, so the eyedropper can never
        # run while exec() is spinning (the old freeze). "Pick from board"
        # therefore closes the dialog; we sample the board with a plain nested
        # event loop and reopen the dialog with the picked color.
        color = QColor(self.draw_color)
        while True:
            d = CanvasColorDialog(color, self)
            d.exec()
            if d.pick_requested:
                picked = self.pick_board_color()
                if picked is not None and picked.isValid():
                    color = QColor(picked)
                continue
            if d.result() != QDialog.DialogCode.Accepted:
                return
            c = d.currentColor()
            if c.isValid():
                self.draw_color = c.name()
                self.titlebar.set_color(self.draw_color)
                self.settings["draw_color"] = self.draw_color
                save_settings(self.settings)
            if d.fill_requested:
                activate = getattr(self.view, "_bucket_activate", None)
                if callable(activate):
                    activate()
            return

    def pick_board_color(self):
        """Run the canvas eyedropper and wait for one pick or cancel.

        No modal dialog is alive here, so the board stays fully clickable.
        Stray hotkeys are swallowed by eventFilter while self._picking is
        set; LMB picks, Esc / RMB cancel.
        """
        loop = QEventLoop(self)
        out = []

        def done(color):
            out.append(color)
            if loop.isRunning():
                loop.quit()

        self._picking = True
        self.view.arm_picker(done)
        loop.exec()
        self._picking = False
        return out[0] if out else None

    def set_draw_width(self, value):
        self.draw_width = int(value)

    def add_stroke(self, pos):
        it = StrokeItem(self.draw_color, self.draw_width)
        path = QPainterPath(QPointF(0, 0))
        it.setPath(path)
        self.objects.add(it)
        self.scene.addItem(it)
        it.setPos(pos)
        it.setZValue(self.max_z() + 1)
        return it

    def add_text_in_rect(self, rect):
        font = QFont("Segoe UI")
        it = TextItem("", self.draw_color, font, rect.height())
        it.setTextWidth(rect.width())
        self.objects.add(it)
        self.scene.addItem(it)
        it.setPos(rect.topLeft())
        it.setZValue(self.max_z() + 1)
        self.begin_text_editing(it)

    def add_frame_in_rect(self, rect):
        it = FrameItem(rect.width(), rect.height(), color=self.draw_color)
        self.objects.add(it)
        self.scene.addItem(it)
        it.setPos(rect.topLeft())
        it.setZValue(self.max_z() + 1)
        self.scene.clearSelection()
        it.setSelected(True)
        self.record_history()

    def begin_text_editing(self, it):
        self.scene.clearSelection()
        it.setSelected(True)
        it._edit_original = it.toPlainText()
        it._cancel_edit = False
        self.view._editing_text = it
        # Letter shortcuts such as T/B/V must type characters, not switch tools.
        self._text_action_shortcuts = {key: action.shortcuts()
                                       for key, action in self.act.items()}
        for action in self.act.values():
            action.setShortcuts([])
        it.setFlag(QGraphicsItem.ItemIsMovable, False)
        it.setTextInteractionFlags(Qt.TextEditorInteraction)
        it.setFocus(Qt.MouseFocusReason)
        self.toast("Type text • Ctrl+Enter: finish • Esc: cancel")

    def edit_text(self, it):
        d = AnnotationDialog(self, it.toPlainText(), it.font(), it.defaultTextColor().name())
        if d.exec():
            text, font, color = d.values()
            if text:
                it.setPlainText(text)
                it.setFont(font)
                it.setDefaultTextColor(QColor(color))
                it.fit_font()
                self.record_history()

    # ---------- items ----------
    def top_items(self):
        return [i for i in self.objects if i.parentItem() is None]

    def selected_top(self):
        return [i for i in self.scene.selectedItems()
                if i.parentItem() is None and i in self.objects]

    def max_z(self):
        return max((i.zValue() for i in self.top_items()), default=0)

    def toast(self, text):
        self.view.toast.show_text(text)

    def view_center(self):
        return self.view.mapToScene(self.view.viewport().rect().center())

    def add_file(self, path, pos):
        ext = os.path.splitext(path)[1].lower()
        if ext in IMAGE_EXT:
            it = ImageItem(path)
            if not it.is_valid():
                return None
        elif ext in VIDEO_EXT:
            it = VideoItem(path)
        elif ext in DOCUMENT_EXT:
            it = DocumentItem(path)
            if not it.is_valid():
                return None
        elif ext in MODEL3D_EXT:
            it = Model3DItem(path)
            it.navigation_hint = model_navigation_hint(self.nav)
            if not it.is_valid():
                detail = it.error or "Could not load the model."
                # Keep the importer's real reason visible: the generic
                # "Could not open" toast used to replace it immediately.
                QMessageBox.warning(self, "3D import",
                                    os.path.basename(path) + "\n\n" + detail)
                return None
        else:
            return None
        z = self.max_z() + 1
        self.objects.add(it)
        self.scene.addItem(it)
        it.setZValue(z)
        it.setPos(pos)
        return it

    def save_image(self, img):
        folder = os.path.join(APP_DIR, "data", "pasted")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, uuid.uuid4().hex[:12] + ".png")
        return path if img.save(path) else None

    def add_mime(self, md, pos):
        added, failed, remote = [], [], []
        if md.hasUrls():
            for u in md.urls():
                if is_remote(u):
                    remote.append(u.toString())
                    continue
                if not u.isLocalFile():
                    continue
                p = u.toLocalFile()
                if p.lower().endswith(PROJECT_EXT):
                    self.open_project(p)
                    return
                it = self.add_file(p, pos)
                if it:
                    added.append(it)
                    pos += QPointF(40, 40)
                else:
                    failed.append(os.path.basename(p))
        if not added and not remote and md.hasImage():
            img = mime_image(md)
            path = self.save_image(img) if not img.isNull() else None
            it = self.add_file(path, pos) if path else None
            if it:
                added.append(it)
        # "Copy image address" puts plain text on the clipboard, not a URL object.
        if not added and not remote and md.hasText():
            remote = self.urls_from_text(md.text())
        if added:
            self.scene.clearSelection()
            for it in added:
                it.setSelected(True)
            self.record_history()
            if any(item.kind == "video" for item in added):
                self.toast("Double-click a video or press Space to play / pause")
        if failed:
            self.toast("Could not open: " + ", ".join(failed[:3]))
        for url in remote:
            self.download_url(url, pos)
            pos += QPointF(40, 40)

    def urls_from_text(self, text):
        """Links copied as plain text: one per line, or a data:image URI."""
        urls = []
        for line in (text or "").splitlines():
            line = line.strip().strip('"').strip("'")
            if not line:
                continue
            if line.lower().startswith("data:image/"):
                path = save_data_uri(line)
                if path:
                    it = self.add_file(path, self.view_center())
                    if it:
                        self.scene.clearSelection()
                        it.setSelected(True)
                        self.record_history()
                        self.toast("Image added from the clipboard")
                continue
            url = QUrl(line)
            if is_remote(url):
                urls.append(url.toString())
        return urls

    def download_url(self, url, pos):
        """Fetch a browser image in the background and drop it at pos."""
        self.toast("Downloading image…")
        thread = DownloadThread(url, self)
        thread.completed.connect(lambda path, error, p=QPointF(pos): self._download_done(p, path, error))
        thread.finished.connect(thread.deleteLater)
        self._downloads.append(thread)
        thread.start()

    def _download_done(self, pos, path, error):
        thread = self.sender()
        if thread in self._downloads:
            self._downloads.remove(thread)
        if error or not path:
            self.toast("Could not download: " + (error or "unknown error"))
            return
        it = self.add_file(path, pos)
        if it is None:
            self.toast("Downloaded file is not supported")
            return
        self.scene.clearSelection()
        it.setSelected(True)
        self.record_history()
        self.toast("Image added from the web")

    def paste(self):
        self.add_mime(QApplication.clipboard().mimeData(), self.view_center())

    def copy_selected(self):
        sel = self.selected_top()
        if not sel:
            self.toast("Select something to copy")
            return False
        image = selection_image(self, sel)
        if image is None or image.isNull():
            self.toast("Nothing to copy")
            return False
        QApplication.clipboard().setImage(image)
        self.toast("Copied to clipboard")
        return True

    def cut_selected(self):
        if not self.selected_top():
            self.toast("Select something to cut")
            return
        if self.copy_selected():
            self.delete_selected()
            self.record_history()
            self.toast("Cut to clipboard")

    def add_files(self):
        exts = " ".join("*" + e for e in sorted(IMAGE_EXT | VIDEO_EXT | DOCUMENT_EXT | MODEL3D_EXT))
        paths, _ = QFileDialog.getOpenFileNames(self, "Add Files", APP_DIR,
                                                f"References and documents ({exts})")
        pos = self.view_center()
        added, failed = [], []
        for p in paths:
            if self.add_file(p, pos):
                added.append(p)
                pos += QPointF(40, 40)
            else:
                failed.append(os.path.basename(p))
        if failed:
            self.toast("Could not open: " + ", ".join(failed[:3]))
        elif any(p.lower().endswith(tuple(VIDEO_EXT)) for p in added):
            self.toast("Double-click a video or press Space to play / pause")

    def select_all(self):
        for i in self.top_items():
            i.setSelected(True)

    def remove_item(self, it):
        for x in list(walk(it)):
            if x.kind == "video":
                x.close()
            elif x.kind == "document":
                x.close()
            self.objects.discard(x)
        self.scene.removeItem(it)

    def delete_selected(self):
        for it in self.selected_top():
            self.remove_item(it)

    def duplicate(self):
        selected = self.selected_top()
        self.scene.clearSelection()
        duplicates = []
        for item in selected:
            copy_item = self.build(self.dump(item, None), None, new_ids=True)
            if copy_item:
                copy_item.setZValue(self.max_z() + 1)
                copy_item.setSelected(True)
                duplicates.append(copy_item)
        if duplicates:
            # Blender-style Shift+D: the copy stays in place and immediately enters Move.
            self.view.setFocus()
            QTimer.singleShot(0, lambda: self.view.tf_start("move", duplicates))

    # ---------- annotations and groups ----------
    def edit_note(self, it):
        if isinstance(it, TextItem):
            self.edit_text(it)
            return
        font = it.note.font() if it.note else QFont("Segoe UI", 12)
        color = it.note.defaultTextColor().name() if it.note else "#1a1a1a"
        d = AnnotationDialog(self, note_text(it), font, color)
        if d.exec():
            text, font, color = d.values()
            style = {"font": font.toString(), "color": color}
            set_note(it, text, style)
            top = it.topLevelItem()
            if top is not it and hasattr(top, "refresh"):
                top.refresh()
            self.record_history()

    def edit_note_selected(self):
        sel = self.selected_top()
        if len(sel) == 1:
            self.edit_note(sel[0])
        else:
            self.toast("Select one item or group")

    def group_selected(self):
        items = self.selected_top()
        if len(items) < 2:
            self.toast("Select at least two items")
            return
        title, ok = QInputDialog.getText(self, "Group", "Group name:", text="Group")
        if not ok:
            return
        g = GroupItem(title or "Group")
        z = self.max_z() + 1
        self.objects.add(g)
        self.scene.addItem(g)
        for i in sorted(items, key=lambda x: x.zValue()):
            i.setSelected(False)
            g.addToGroup(i)
        g.setZValue(z)
        g.refresh()
        g.setSelected(True)

    def ungroup_selected(self):
        for g in [i for i in self.selected_top() if i.kind == "group"]:
            if g.note:
                self.scene.removeItem(g.note)
                g.note = None
            for c in g.members():
                g.removeFromGroup(c)
                c.setZValue(self.max_z() + 1)
                place_note(c)
                c.setSelected(True)
            self.objects.discard(g)
            self.scene.removeItem(g)

    def rename_selected(self):
        groups = [i for i in self.selected_top() if i.kind == "group"]
        if len(groups) != 1:
            return
        title, ok = QInputDialog.getText(self, "Group", "Group name:", text=groups[0].title)
        if ok:
            groups[0].title = title
            groups[0].update()

    # ---------- documents / video ----------
    def document_page(self, delta):
        documents = [item for item in self.selected_top() if item.kind == "document"]
        for item in documents:
            item.change_page(delta)
        if documents:
            page = documents[0].current_page() + 1
            total = documents[0].page_count()
            self.toast(f"Page {page} / {total}")

    def videos_in_selection(self):
        return [x for it in self.selected_top() for x in walk(it) if x.kind == "video"]

    def update_video_controls(self):
        videos = self.videos_in_selection()
        self.view.video_controls.set_video(videos[0] if len(videos) == 1 else None)

    def toggle_videos(self):
        for v in self.videos_in_selection():
            v.toggle()
        self.view.video_controls.refresh()

    def capture_video_poster(self, video):
        if video.capture_poster():
            self.toast("Current frame set as the paused preview")
            self.record_history()
        else:
            self.toast("No decoded video frame is available yet")

    def capture_video_frame(self, video):
        image = video.current_frame_image()
        if image.isNull():
            self.toast("Play or seek the video to a decoded frame first")
            return
        path = self.save_image(image)
        if not path:
            self.toast("Could not save the video frame")
            return
        rect = self.full_rect(video)
        item = self.add_file(path, QPointF(rect.right() + 40, rect.top()))
        if item:
            self.scene.clearSelection()
            item.setSelected(True)
            self.record_history()
            self.toast("Video frame added as an image")

    def toggle_mute(self):
        vids = self.videos_in_selection()
        for v in vids:
            v.toggle_mute()
        if vids:
            self.toast("Muted" if vids[0].audio.isMuted() else "Sound on")
        self.view.video_controls.refresh()

    # ---------- transforms ----------
    def start_transform(self, mode):
        sel = self.selected_top()
        if not sel:
            self.toast("Select items first")
            return
        self.view.setFocus()
        self.view.tf_start(mode, sel)

    @staticmethod
    def _set_keep_center(it, scale=None, rot=None):
        """Change scale / rotation of an item while keeping its visual center in place."""
        c_local = handle_rect(it).center()
        before = it.mapToScene(c_local)
        if scale is not None:
            it.setScale(max(scale, 0.01))
        if rot is not None:
            it.setRotation(rot)
        after = it.mapToScene(c_local)
        it.moveBy(before.x() - after.x(), before.y() - after.y())
        place_note(it)

    def scale_selected(self, f):
        for it in self.transformable_selected():
            self._set_keep_center(it, scale=it.scale() * f)

    def reset_transform(self, scale=False, rot=False):
        for it in self.transformable_selected():
            self._set_keep_center(it, scale=1.0 if scale else None, rot=0.0 if rot else None)

    def flip_selected(self):
        for it in self.selected_top():
            if it.kind == "image":
                it.set_flip(not it.flipped)

    def crop_selected(self):
        selected = self.selected_top()
        if len(selected) != 1 or selected[0].kind != "image":
            self.toast("Select one image to crop")
            return
        self.view.begin_crop(selected[0])

    def relink_selected(self):
        selected = self.selected_top()
        if len(selected) != 1 or selected[0].kind != "missing":
            self.toast("Select one missing-media placeholder")
            return
        old = selected[0]
        start = os.path.dirname(old.path) if os.path.isdir(os.path.dirname(old.path)) else APP_DIR
        exts = " ".join("*" + e for e in sorted(IMAGE_EXT | VIDEO_EXT | DOCUMENT_EXT | MODEL3D_EXT))
        path, _ = QFileDialog.getOpenFileName(self, "Relink Media", start,
                                              f"References and documents ({exts})")
        if not path:
            return
        replacement = self.add_file(path, old.pos())
        if replacement is None:
            QMessageBox.warning(self, APP_NAME, "The selected media file could not be loaded.")
            return
        parent = old.parentItem()
        if parent is not None and parent.kind == "group":
            parent.addToGroup(replacement)
        replacement.setPos(old.pos())
        replacement.uid = old.uid
        replacement.setZValue(old.zValue())
        replacement.setScale(old.scale())
        replacement.setRotation(old.rotation())
        set_note(replacement, note_text(old), getattr(old, "note_style", None))
        self.scene.clearSelection()
        replacement.setSelected(True)
        self.remove_item(old)
        if parent is not None and hasattr(parent, "refresh"):
            parent.refresh()
        self.view.schedule_media_update()

    # ---------- arrange ----------
    @staticmethod
    def full_rect(it):
        return it.mapRectToScene(it.boundingRect().united(it.childrenBoundingRect()))

    def align(self, side):
        # Locked objects keep their geometry; arrange only moves unlocked items.
        items = self.transformable_selected()
        if len(items) < 2:
            return
        rects = [(i, self.full_rect(i)) for i in items]
        get = {"left": QRectF.left, "right": QRectF.right,
               "top": QRectF.top, "bottom": QRectF.bottom}[side]
        vals = [get(r) for _, r in rects]
        t = min(vals) if side in ("left", "top") else max(vals)
        for i, r in rects:
            d = t - get(r)
            i.moveBy(d, 0) if side in ("left", "right") else i.moveBy(0, d)

    def stack(self, horizontal):
        items = self.transformable_selected()
        if len(items) < 2:
            return
        items.sort(key=lambda i: self.full_rect(i).left() if horizontal else self.full_rect(i).top())
        r0 = self.full_rect(items[0])
        x, y = r0.left(), r0.top()
        for i in items:
            r = self.full_rect(i)
            i.moveBy(x - r.left(), y - r.top())
            if horizontal:
                x += r.width() + GAP
            else:
                y += r.height() + GAP

    def distribute(self, horizontal):
        """Equal gaps between selected unlocked items; the outer two stay in place."""
        items = self.transformable_selected()
        if len(items) < 3:
            self.toast("Select at least three unlocked items to distribute")
            return
        rects = {i: self.full_rect(i) for i in items}
        if horizontal:
            items.sort(key=lambda i: rects[i].left())
            span = rects[items[-1]].right() - rects[items[0]].left()
            gap = (span - sum(r.width() for r in rects.values())) / (len(items) - 1)
            x = rects[items[0]].left()
            for i in items:
                r = rects[i]
                i.moveBy(x - r.left(), 0)
                x += r.width() + gap
        else:
            items.sort(key=lambda i: rects[i].top())
            span = rects[items[-1]].bottom() - rects[items[0]].top()
            gap = (span - sum(r.height() for r in rects.values())) / (len(items) - 1)
            y = rects[items[0]].top()
            for i in items:
                r = rects[i]
                i.moveBy(0, y - r.top())
                y += r.height() + gap

    def pack(self):
        items = self.transformable_selected() or [i for i in self.top_items() if not is_locked(i)]
        if not items:
            return
        rects = {i: self.full_rect(i) for i in items}
        area = sum(r.width() * r.height() for r in rects.values())
        max_w = max(max(r.width() for r in rects.values()), math.sqrt(area) * 1.3)
        ox = min(r.left() for r in rects.values())
        oy = min(r.top() for r in rects.values())
        items.sort(key=lambda i: -rects[i].height())
        x = y = row_h = 0.0
        for i in items:
            r = rects[i]
            if x > 0 and x + r.width() > max_w:
                x, y, row_h = 0.0, y + row_h + GAP, 0.0
            i.moveBy(ox + x - r.left(), oy + y - r.top())
            x += r.width() + GAP
            row_h = max(row_h, r.height())

    def normalize(self, by_height):
        items = [i for i in self.transformable_selected()
                 if i.kind in ("image", "video", "model3d")]
        if len(items) < 2:
            return
        dim = (lambda i: i.boundingRect().height()) if by_height else (lambda i: i.boundingRect().width())
        target = sum(dim(i) * i.scale() for i in items) / len(items)
        for i in items:
            if dim(i) > 0:
                i.setScale(target / dim(i))
                place_note(i)

    def restack(self, front):
        items = sorted(self.selected_top(), key=lambda i: i.zValue())
        if not items:
            return
        if front:
            z = self.max_z()
            for i in items:
                z += 1
                i.setZValue(z)
        else:
            z = min(i.zValue() for i in self.top_items())
            for i in reversed(items):
                z -= 1
                i.setZValue(z)

    # ---------- view ----------
    def fit_rect(self, r):
        if r.isNull() or r.isEmpty():
            return
        r = r.adjusted(-40, -40, 40, 40)
        vp = self.view.viewport().rect()
        self.view.set_view(min(vp.width() / r.width(), vp.height() / r.height()), r.center())

    def fit_all(self):
        items = self.top_items()
        if not items:
            self.view.set_view(1.0, QPointF(0, 0))
            return
        r = QRectF()
        for i in items:
            r = r.united(self.full_rect(i))
        self.fit_rect(r)

    def fit_selected(self):
        r = QRectF()
        for i in self.selected_top():
            r = r.united(self.full_rect(i))
        self.fit_rect(r)

    def zoom_by(self, f):
        self.view.zoom_at(f)
        self.view.show_zoom()

    def zoom_reset(self):
        self.view.set_view(1.0, self.view_center())
        self.view.show_zoom()

    def update_media_visibility(self):
        visible = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        visible = visible.adjusted(-visible.width() * 0.1, -visible.height() * 0.1,
                                   visible.width() * 0.1, visible.height() * 0.1)
        zoom = self.view.cur_zoom()
        for top in self.top_items():
            for item in walk(top):
                is_visible = visible.intersects(self.full_rect(item))
                if item.kind == "image" and is_visible:
                    item.ensure_quality(zoom * (item.scale() or 1.0))
                elif item.kind == "video":
                    item.set_view_visible(is_visible)
        self.view.minimap.schedule()

    # ---------- window ----------
    def toggle_on_top(self):
        self.on_top = self.act["on_top"].isChecked()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.on_top)
        self.show()
        self.toast("Always on top: " + ("on" if self.on_top else "off"))

    def toggle_frameless(self):
        self.frameless = self.act["frameless"].isChecked()
        self.setWindowFlag(Qt.FramelessWindowHint, self.frameless)
        self.show()
        self.toast("Frameless: " + ("on" if self.frameless else "off"))

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def toggle_maximized(self):
        self.showNormal() if self.isMaximized() else self.showMaximized()

    def set_opacity(self, v):
        v = max(0.2, min(1.0, v))
        self.setWindowOpacity(v)
        self.toast(f"Opacity: {round(v * 100)}%")

    def change_opacity(self, d):
        self.set_opacity(self.windowOpacity() + d)

    def update_title(self):
        name = os.path.basename(self.project_path) if self.project_path else "Untitled"
        if self._dirty:
            name += " *"
        self.setWindowTitle(f"{APP_NAME} - {name}")
        self.view.set_project_name(name)

    # ---------- undo / redo ----------
    def reset_history(self):
        self.history.reset(self.state_data())
        self.update_history_actions()

    def update_history_actions(self):
        if hasattr(self, "act"):
            self.act["undo"].setEnabled(bool(self.history.undo_stack))
            self.act["redo"].setEnabled(bool(self.history.redo_stack))

    def record_history(self):
        current = self.state_data()
        entry = self.history.record(current)
        if entry is None:
            return
        self._dirty = True
        self.update_title()
        self.update_history_actions()
        self.view.minimap.schedule()
        if self.view.search.isVisible():
            self.view.search.refresh()

    def apply_history(self, entry, direction):
        simple = getattr(entry, "simple_" + direction)
        rebuild = getattr(entry, "rebuild_" + direction)
        by_id = {item.uid: item for item in self.objects}
        for uid, data in rebuild.items():
            existing = by_id.get(uid)
            if existing is not None:
                self.remove_item(existing)
            if data is not None:
                build_item(self, data, None)
        by_id = {item.uid: item for item in self.objects}
        for uid, data in simple.items():
            item = by_id.get(uid)
            if item is not None:
                update_item(item, data)
        self.scene.clearSelection()
        self.history.committed(self.state_data())
        self._dirty = self.snapshot() != self.saved_state
        self.update_title()
        self.view.viewport().update()
        self.view.schedule_media_update()
        if self.view.search.isVisible():
            self.view.search.refresh()

    def undo(self):
        operation = self.history.undo()
        if operation is None:
            self.toast("Nothing to undo")
            return
        if self.view._resize:
            self.view.resize_end(False)
        if self.view._tf:
            self.view.tf_end(False, quiet=True)
        if self.view._crop:
            self.view.finish_crop(False)
        self.apply_history(*operation)
        self.update_history_actions()
        self.toast("Undo")

    def redo(self):
        operation = self.history.redo()
        if operation is None:
            self.toast("Nothing to redo")
            return
        self.apply_history(*operation)
        self.update_history_actions()
        self.toast("Redo")

    # ---------- save / load ----------
    def state_data(self):
        return [dump_item(self, item, None) for item in self.ordered_top()]

    def dump(self, item, base):
        return dump_item(self, item, base)

    def build(self, data, base, new_ids=False):
        return build_item(self, data, base, new_ids)

    def ordered_top(self):
        return sorted(self.top_items(), key=lambda i: i.zValue())

    def snapshot(self):
        return json.dumps([self.dump(i, None) for i in self.ordered_top()], ensure_ascii=False)

    def maybe_save(self):
        if self._save_thread is not None:
            self._wait_for_thread(self._save_thread)
        if self._autosave_thread is not None:
            self._wait_for_thread(self._autosave_thread)
        if self.snapshot() == self.saved_state:
            return True
        SB = QMessageBox.StandardButton
        r = QMessageBox.question(self, APP_NAME, "Save changes to the project?",
                                 SB.Save | SB.Discard | SB.Cancel, SB.Save)
        if r == SB.Save:
            return self.save_project(False)
        return r == SB.Discard

    def clear_scene(self):
        if self.view._crop:
            self.view.finish_crop(False)
        for it in self.top_items():
            self.remove_item(it)

    def release_media_dir(self):
        media_dir, self._media_dir = self._media_dir, None
        if media_dir is not None:
            media_dir.cleanup()

    def new_project(self):
        if not self.maybe_save():
            return
        self.clear_scene()
        self.release_media_dir()
        self.clear_recovery()
        self.project_path = None
        self.saved_state = self.snapshot()
        self._dirty = False
        self._autosave_state = None
        self.reset_history()
        self.view.set_view(1.0, QPointF(0, 0))
        self.update_title()

    def add_recent(self, path):
        path = os.path.normpath(os.path.abspath(path))
        lst = [x for x in self.settings.get("recent", []) if from_portable(x) != path]
        self.settings["recent"] = ([to_portable(path)] + lst)[:10]
        save_settings(self.settings)

    def package_data(self, recovery=False):
        center = self.view_center()
        data = {"app": APP_NAME, "version": 8, "container": "zip",
                "view": {"zoom": self.view.cur_zoom(), "cx": center.x(), "cy": center.y()},
                "items": self.state_data()}
        if recovery:
            data["recovery_source"] = self.project_path
            data["recovery_timestamp"] = time.time()
        return data

    def _wait_for_thread(self, thread):
        if thread is None:
            return
        if not thread.isRunning():
            # A very small project can finish before wait=True reaches this method;
            # deliver its queued completion signal before reading the result.
            QApplication.processEvents()
            return
        progress = QProgressDialog("Writing project media…", "", 0, 0, self)
        progress.setWindowTitle(APP_NAME)
        progress.setCancelButton(None)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        loop = QEventLoop()
        thread.completed.connect(loop.quit)
        thread.finished.connect(loop.quit)
        progress.show()
        if thread.isRunning():
            loop.exec()
        progress.close()

    def save_project(self, save_as=False, wait=False):
        if self._save_thread is not None:
            self._wait_for_thread(self._save_thread)
            return self._last_save_success
        if self._autosave_thread is not None:
            self._wait_for_thread(self._autosave_thread)
        target = self.project_path
        if save_as or not target:
            start = os.path.dirname(target) if target else APP_DIR
            target, _ = QFileDialog.getSaveFileName(self, "Save Project", start,
                                                    f"RefSynapse (*{PROJECT_EXT})")
            if not target:
                return False
            if not target.lower().endswith(PROJECT_EXT):
                target += PROJECT_EXT
        target = os.path.normpath(os.path.abspath(target))
        data = self.package_data()
        media, missing = prepare_package(data)
        self._save_target = target
        self._save_state = self.snapshot()
        self._last_save_success = False
        thread = ProjectSaveThread(target, data, media, self)
        self._save_thread = thread
        thread.completed.connect(self._save_completed)
        thread.finished.connect(thread.deleteLater)
        self.act["save"].setEnabled(False)
        self.act["save_as"].setEnabled(False)
        self.toast("Saving in the background…")
        thread.start()
        if wait:
            self._wait_for_thread(thread)
        if missing:
            self.toast("Saved; missing media were preserved as placeholders")
        return self._last_save_success if wait else True

    def _save_completed(self, target, error):
        thread = self._save_thread
        self._save_thread = None
        self.act["save"].setEnabled(True)
        self.act["save_as"].setEnabled(True)
        if error:
            self._last_save_success = False
            QMessageBox.warning(self, APP_NAME, f"Could not save the project:\n{error}")
        else:
            self._last_save_success = True
            self.project_path = target
            self.saved_state = self._save_state
            self._dirty = self.snapshot() != self.saved_state
            self.clear_recovery()
            self._autosave_state = None
            self.add_recent(target)
            self.update_title()
            self.toast("Saved with embedded media")
        if thread is not None and not thread.isRunning():
            thread.deleteLater()

    def autosave(self):
        if not self._dirty or self._save_thread is not None or self._autosave_thread is not None:
            return
        state = self.snapshot()
        if state == self._autosave_state:
            return
        self._autosave_state = state
        data = self.package_data(recovery=True)
        media, _ = prepare_package(data)
        thread = ProjectSaveThread(recovery_file(), data, media, self)
        self._autosave_thread = thread
        thread.completed.connect(self._autosave_completed)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _autosave_completed(self, target, error):
        thread = self._autosave_thread
        self._autosave_thread = None
        if error:
            self._autosave_state = None
            log_path = os.path.join(APP_DIR, "refsynapse-error.log")
            try:
                with open(log_path, "a", encoding="utf-8") as stream:
                    stream.write(f"\nAutosave failed: {error}\n")
            except OSError:
                pass
        if thread is not None and not thread.isRunning():
            thread.deleteLater()

    def open_project(self, path=None, recovery=False):
        if not self.maybe_save():
            return
        if not path:
            start = os.path.dirname(self.project_path) if self.project_path else APP_DIR
            path, _ = QFileDialog.getOpenFileName(self, "Open Project", start,
                                                  f"RefSynapse (*{PROJECT_EXT})")
            if not path:
                return
        try:
            data, new_media_dir = load_package(path)
        except Exception as ex:
            QMessageBox.warning(self, APP_NAME, f"Could not open the project:\n{ex}")
            return
        path = os.path.normpath(os.path.abspath(path))
        if self.view._crop:
            self.view.finish_crop(False)
        self.clear_scene()
        self.release_media_dir()
        self._media_dir = new_media_dir
        self.missing = []
        base = None if new_media_dir is not None else os.path.dirname(path)
        try:
            for item in data.get("items", []):
                if isinstance(item, dict):
                    self.build(item, base)
        except Exception as ex:
            QMessageBox.warning(self, APP_NAME, f"The project contains invalid object data:\n{ex}")
        source = data.get("recovery_source") if recovery else None
        self.project_path = os.path.normpath(source) if source else (None if recovery else path)
        self.saved_state = "__recovered__" if recovery else self.snapshot()
        self._dirty = bool(recovery)
        self._autosave_state = None
        self.reset_history()
        if not recovery:
            self.add_recent(path)
            self.clear_recovery()
        self.update_title()
        view = data.get("view")
        restore_view = isinstance(view, dict) and self.top_items()
        if restore_view:
            try:
                zoom = float(view.get("zoom", 1.0))
                center = QPointF(float(view.get("cx", 0)), float(view.get("cy", 0)))
                if not math.isfinite(zoom) or not math.isfinite(center.x()) or not math.isfinite(center.y()):
                    raise ValueError
                self.view.set_view(zoom, center)
                visible = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
                content = QRectF()
                for item in self.top_items():
                    content = content.united(self.full_rect(item))
                if not visible.intersects(content):
                    restore_view = False
            except (TypeError, ValueError, OverflowError):
                restore_view = False
        if not restore_view:
            QTimer.singleShot(0, self.fit_all)
        self.view.schedule_media_update()
        if self.view.search.isVisible():
            self.view.search.refresh()
        if self.missing:
            QMessageBox.warning(self, APP_NAME,
                                "Some media are missing. Their layout was preserved in placeholders:\n" +
                                "\n".join(self.missing[:10]))

    def closeEvent(self, e):
        if not self.maybe_save():
            e.ignore()
            return
        if self._autosave_thread is not None:
            self._wait_for_thread(self._autosave_thread)
        self.autosave_timer.stop()
        self.clear_recovery()
        if not (self.isFullScreen() or self.isMaximized()):
            g = self.geometry()
            self.settings["geometry"] = [g.x(), g.y(), g.width(), g.height()]
        self.settings.update(frameless=self.frameless, on_top=self.on_top,
                             opacity=round(self.windowOpacity(), 2), nav=self.nav,
                             draw_color=self.draw_color, draw_width=self.draw_width,
                             minimap=self.act["minimap"].isChecked())
        save_settings(self.settings)
        self.clear_scene()
        self.release_media_dir()
        e.accept()