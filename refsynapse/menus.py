"""Context menu and File menu construction (the whole UI, like PureRef).

The mixin expects: act, scene, settings, nav, export_controller, selected_top(),
set_preset(), open_settings(), set_opacity(), open_project().
"""
import os

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QMenu

from .config import PRESETS, from_portable
from .items import walk
from .item_tools import is_locked


class MenusMixin:
    def build_file_menu(self):
        menu = QMenu(self)
        for key in ("new", "open"):
            menu.addAction(self.act[key])
        recent = menu.addMenu("Recent")
        for saved in self.settings.get("recent", []):
            full = from_portable(saved)
            if os.path.isfile(full):
                recent.addAction(os.path.basename(full),
                                 lambda *_, p=full: self.open_project(p))
        recent.setEnabled(not recent.isEmpty())
        menu.addSeparator()
        for key in ("save", "save_as"):
            menu.addAction(self.act[key])
        menu.addSeparator()
        menu.addAction(self.act["add"])
        menu.addSeparator()
        menu.addAction(self.act["settings"])
        menu.addSeparator()
        menu.addAction(self.act["quit"])
        return menu

    def show_file_menu(self, anchor):
        pos = anchor.mapToGlobal(QPoint(0, anchor.height()))
        self.build_file_menu().exec(pos)

    def build_menu(self, it):
        if it is not None and not hasattr(it, "kind"):
            it = None
        if it is not None and not it.isSelected():
            self.scene.clearSelection()
            it.setSelected(True)
        a, sel = self.act, self.selected_top()
        kinds = {x.kind for s in sel for x in walk(s)}
        top_kinds = {s.kind for s in sel}
        m = QMenu(self)

        def fill(menu, keys):
            for k in keys:
                menu.addSeparator() if k is None else menu.addAction(a[k])

        if sel:
            m.addAction(a["note"])
            a["lock"].setChecked(all(is_locked(s) for s in sel))
            m.addAction(a["lock"])
            t = m.addMenu("Transform")
            fill(t, ("tf_move", "tf_rotate", "tf_scale", None, "bigger", "smaller", None,
                     "reset_scale", "reset_rot"))
            if "image" in top_kinds:
                t.addAction(a["flip"])
                if len(sel) == 1:
                    t.addAction(a["crop"])
            if "document" in top_kinds:
                pages = m.addMenu("Document Pages")
                fill(pages, ("doc_prev", "doc_next"))
            if "missing" in top_kinds and len(sel) == 1:
                m.addAction(a["relink"])
            if "video" in kinds:
                fill(m, ("play", "mute"))
            if "group" in top_kinds:
                if len(sel) == 1:
                    m.addAction(a["rename"])
                m.addAction(a["ungroup"])
            if len(sel) > 1:
                m.addAction(a["group"])
            fill(m, ("duplicate", "delete", None))

        self.export_controller.add_menu(m)

        tools = m.addMenu("Canvas Tools")
        fill(tools, ("tool_select", "tool_draw", "tool_text", "tool_frame", None, "note"))

        f = m.addMenu("File")
        fill(f, ("new", "open"))
        recent = f.addMenu("Recent")
        for x in self.settings.get("recent", []):
            full = from_portable(x)
            if os.path.isfile(full):
                recent.addAction(os.path.basename(full), lambda *_, p=full: self.open_project(p))
        recent.setEnabled(not recent.isEmpty())
        fill(f, ("save", "save_as", None, "add"))

        fill(m.addMenu("Edit"), ("undo", "redo", None, "cut", "copy", "paste", "duplicate",
                                 "select_all", "delete", None, "lock", "group", "ungroup",
                                 "rename", "note"))
        r = m.addMenu("Arrange")
        fill(r.addMenu("Align"), ("align_left", "align_right", "align_top", "align_bottom"))
        fill(r, ("row", "column", "dist_h", "dist_v", "pack", None, "norm_h", "norm_w",
                 None, "front", "back"))
        fill(m.addMenu("View"), ("fit_all", "fit_sel", None, "zoom100", "zoom_in", "zoom_out",
                                 None, "search", "minimap", None,
                                 "snapping", "grid_snap", "grid_visible"))

        n = m.addMenu("Navigation")
        for name in PRESETS:
            act = n.addAction(name, lambda *_, p=name: self.set_preset(p))
            act.setCheckable(True)
            act.setChecked(self.nav.get("preset") == name)
        n.addSeparator()
        n.addAction("Customize...", self.open_settings)

        w = m.addMenu("Window")
        fill(w, ("on_top", "frameless"))
        op = w.addMenu("Opacity")
        cur = round(self.windowOpacity() * 100)
        for val in (100, 90, 80, 70, 50, 30):
            act = op.addAction(f"{val}%", lambda *_, v=val: self.set_opacity(v / 100))
            act.setCheckable(True)
            act.setChecked(abs(cur - val) < 3)
        fill(w, (None, "fullscreen", "minimize"))

        fill(m, (None, "settings", "help", "quit"))
        return m

    def show_menu(self, it, gpos):
        self.build_menu(it).exec(gpos)