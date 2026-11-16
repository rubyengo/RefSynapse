"""Action registry, keyboard shortcuts and the layout-independent key map.

The mixin expects the host window to provide: act, settings, nav, view,
and the command methods referenced by the actions below. Item tools
(lock, drag-out) come from ItemToolsMixin.
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence

from .config import TRANSFORM_KEYS
from .item_tools import ItemToolsMixin


class ActionsMixin(ItemToolsMixin):
    def layout_action(self, key, mods):
        ctrl = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)
        alt = bool(mods & Qt.AltModifier)
        combo = (key, ctrl, shift, alt)
        actions = {
            ("N", True, False, False): "new", ("O", True, False, False): "open",
            ("S", True, False, False): "save", ("S", True, True, False): "save_as",
            ("I", True, False, False): "add", ("Q", True, False, False): "quit",
            ("F", True, False, False): "search", ("M", True, False, False): "minimap",
            ("Z", True, False, False): "undo", ("Y", True, False, False): "redo",
            ("Z", True, True, False): "redo", ("V", True, False, False): "paste",
            ("C", True, False, False): "copy", ("X", True, False, False): "cut",
            ("D", False, True, False): "duplicate", ("A", True, False, False): "select_all",
            ("G", True, False, False): "group", ("G", True, True, False): "ungroup",
            ("L", True, False, False): "lock",
            ("H", True, True, False): "row", ("V", True, True, False): "column",
            ("P", True, False, False): "pack", ("T", True, False, False): "on_top",
            ("B", True, False, False): "frameless", ("=", True, False, False): "zoom_in",
            ("-", True, False, False): "zoom_out", (",", True, False, False): "settings",
            ("]", True, False, False): "front", ("[", True, False, False): "back",
            ("E", True, False, False): "export", ("E", True, True, False): "export_board",
            ("S", False, False, True): "reset_scale", ("R", False, False, True): "reset_rot",
            ("H", False, False, True): "norm_h", ("W", False, False, True): "norm_w",
            ("H", False, True, False): "flip", ("N", False, False, False): "note",
            ("C", False, False, False): "crop",
            ("V", False, False, False): "tool_select",
            ("M", False, False, False): "mute", ("Z", False, False, False): "fit_sel",
            ("X", False, False, False): "delete",
            ("]", False, False, False): "bigger", ("[", False, False, False): "smaller",
        }
        if combo in actions:
            return actions[combo]
        if not (ctrl or shift or alt):
            for mode, letter in self.transform_letters().items():
                if key == letter:
                    return "tf_" + mode
        return None

    def _build_actions(self):
        self.act = {}

        def A(key, text, keys, fn, checkable=False, record=True):
            a = QAction(text, self)
            if keys:
                a.setShortcuts([QKeySequence(k) for k in (keys if isinstance(keys, list) else [keys])])
            a.setCheckable(checkable)
            def run_action(*_, f=fn, save_history=record):
                f()
                if save_history:
                    self.record_history()
            a.triggered.connect(run_action)
            self.addAction(a)
            self.act[key] = a

        # File
        A("new", "New Project", "Ctrl+N", self.new_project)
        A("open", "Open...", "Ctrl+O", self.open_project)
        A("save", "Save", "Ctrl+S", lambda: self.save_project(False))
        A("save_as", "Save As...", "Ctrl+Shift+S", lambda: self.save_project(True))
        A("add", "Add Files...", "Ctrl+I", self.add_files)
        A("settings", "Settings...", ["Ctrl+,", "F12"], self.open_settings)
        A("quit", "Exit", "Ctrl+Q", self.close)
        # Edit
        A("undo", "Undo", "Ctrl+Z", self.undo)
        A("redo", "Redo", ["Ctrl+Y", "Ctrl+Shift+Z"], self.redo)
        A("copy", "Copy", "Ctrl+C", self.copy_selected, record=False)
        A("cut", "Cut", "Ctrl+X", self.cut_selected, record=False)
        A("paste", "Paste", "Ctrl+V", self.paste)
        A("duplicate", "Duplicate + Move", "Shift+D", self.duplicate)
        A("select_all", "Select All", "Ctrl+A", self.select_all)
        A("delete", "Delete", ["Delete", "Backspace", "X"], self.delete_selected)
        A("note", "Annotation...", "N", self.edit_note_selected)
        A("lock", "Lock / Unlock", "Ctrl+L", self.toggle_lock_selected, True)
        A("tool_select", "Select Tool", "V", lambda: self.set_tool("select"))
        # D is handled as a temporary hold tool in eventFilter, not as a QAction
        # shortcut (a plain-letter shortcut would also steal D from search text).
        A("tool_draw", "Draw on Canvas (hold D)", None, lambda: self.set_tool("draw"))
        A("tool_text", "Text Tool (T)", None, lambda: self.toggle_tool("text"))
        A("tool_frame", "Frame Tool (F)", None, lambda: self.toggle_tool("frame"))
        A("group", "Group", "Ctrl+G", self.group_selected)
        A("ungroup", "Ungroup", "Ctrl+Shift+G", self.ungroup_selected)
        A("rename", "Rename Group...", "F2", self.rename_selected)
        # Transform (keys are set by apply_transform_keys)
        A("tf_move", "Move", None, lambda: self.start_transform("move"))
        A("tf_rotate", "Rotate", None, lambda: self.start_transform("rotate"))
        A("tf_scale", "Scale", None, lambda: self.start_transform("scale"))
        A("reset_scale", "Reset Scale", "Alt+S", lambda: self.reset_transform(scale=True))
        A("reset_rot", "Reset Rotation", "Alt+R", lambda: self.reset_transform(rot=True))
        A("flip", "Flip Horizontally", "Shift+H", self.flip_selected)
        # Crop records only on confirmation. Recording at entry would capture the
        # temporary full-source position used while editing an existing crop.
        A("crop", "Crop Image...", "C", self.crop_selected, record=False)
        # Export actions are registered by ExportController (refsynapse/export/).
        A("doc_prev", "Previous Document Page", "PgUp", lambda: self.document_page(-1))
        A("doc_next", "Next Document Page", "PgDown", lambda: self.document_page(1))
        A("relink", "Relink Media...", None, self.relink_selected)
        A("bigger", "Scale Up 10%", "]", lambda: self.scale_selected(1.1))
        A("smaller", "Scale Down 10%", "[", lambda: self.scale_selected(1 / 1.1))
        # Video
        A("play", "Play / Pause", "Space", self.toggle_videos)
        A("mute", "Mute / Unmute", "M", self.toggle_mute)
        # Arrange
        A("align_left", "Align Left", "Alt+Left", lambda: self.align("left"))
        A("align_right", "Align Right", "Alt+Right", lambda: self.align("right"))
        A("align_top", "Align Top", "Alt+Up", lambda: self.align("top"))
        A("align_bottom", "Align Bottom", "Alt+Down", lambda: self.align("bottom"))
        A("row", "Arrange in Row", "Ctrl+Shift+H", lambda: self.stack(True))
        A("column", "Arrange in Column", "Ctrl+Shift+V", lambda: self.stack(False))
        A("dist_h", "Distribute Horizontally", None, lambda: self.distribute(True))
        A("dist_v", "Distribute Vertically", None, lambda: self.distribute(False))
        A("pack", "Pack (Optimize Layout)", "Ctrl+P", self.pack)
        A("norm_h", "Same Height", "Alt+H", lambda: self.normalize(True))
        A("norm_w", "Same Width", "Alt+W", lambda: self.normalize(False))
        A("front", "Bring to Front", "Ctrl+]", lambda: self.restack(True))
        A("back", "Send to Back", "Ctrl+[", lambda: self.restack(False))
        # View
        A("fit_all", "Fit All", "Home", self.fit_all)
        A("fit_sel", "Zoom to Selected", "Z", self.fit_selected)
        A("zoom100", "Zoom 100%", "Ctrl+0", self.zoom_reset)
        A("zoom_in", "Zoom In", ["Ctrl+=", "Ctrl++"], lambda: self.zoom_by(1.25))
        A("zoom_out", "Zoom Out", "Ctrl+-", lambda: self.zoom_by(0.8))
        A("search", "Search Board...", "Ctrl+F", self.show_search)
        A("minimap", "Show Minimap", "Ctrl+M", self.toggle_minimap, True)
        A("snapping", "Snap to Objects", None, self.toggle_snapping, True)
        A("grid_snap", "Snap to Grid", None, self.toggle_grid_snap, True)
        A("grid_visible", "Show Grid", None, self.toggle_grid_visible, True)
        self.act["minimap"].setChecked(self.settings.get("minimap", True))
        for key, setting in (("snapping", "snap_enabled"), ("grid_snap", "snap_grid"),
                             ("grid_visible", "grid_visible")):
            self.act[key].setChecked(bool(self.settings[setting]))
        # Window
        A("on_top", "Always on Top", "Ctrl+T", self.toggle_on_top, True)
        A("frameless", "Frameless", "Ctrl+B", self.toggle_frameless, True)
        A("fullscreen", "Fullscreen", "F11", self.toggle_fullscreen)
        A("minimize", "Minimize", None, self.showMinimized)
        A("help", "Help / Shortcuts", "F1", self.show_help)
        self.act["on_top"].setChecked(self.on_top)
        self.act["frameless"].setChecked(self.frameless)

    def transform_letters(self):
        return TRANSFORM_KEYS.get(self.nav.get("transform_keys"), TRANSFORM_KEYS["blender"])[1]

    def apply_transform_keys(self):
        for mode, letter in self.transform_letters().items():
            self.act["tf_" + mode].setShortcut(QKeySequence(letter))

    def tf_mode_for_key(self, k):
        for mode, letter in self.transform_letters().items():
            if k == ord(letter):
                return mode
        return None