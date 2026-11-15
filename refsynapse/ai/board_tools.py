"""Board tools for the RefSynapse agent.

Every tool is a thin, non-interactive wrapper around MainWindow commands.
Two rules make this safe:

THREADING. The agent runs in a worker thread; QGraphicsScene may only be
touched from the GUI thread. BoardBridge hops every call over with a queued
signal and waits for the result. Calling from the GUI thread runs inline.

NO MODAL DIALOGS. MainWindow.group_selected() and friends open QInputDialog,
which would freeze the agent. Grouping / text creation are reimplemented
here directly on the scene instead.

SANDBOX. A model-supplied file path is only accepted inside the agent folder
(see sandbox.py). This is enforced in code, so it holds for local and cloud
providers alike. App-generated paths (base64 uploads) bypass the check
because the model never chooses them.

Tool results are plain JSON-able dicts: {"ok": True, ...} or {"ok": False,
"error": "..."}. A result may also carry "_image": {"data": <base64>, "mime":
"image/png"} - the agent must strip it and send it as a separate user message,
because the OpenAI "tool" role cannot carry images.
"""

import base64
import os
import sys
import threading

from PySide6.QtCore import (QObject, Signal, Qt, QThread, QPointF, QRectF,
                            QBuffer, QByteArray, QIODevice, QTimer)
from PySide6.QtGui import QColor, QImage, QPainter, QFont

from ..items import (TextItem, FrameItem, GroupItem, walk, note_text,
                     set_note, place_note)
from ..item_tools import is_locked, set_locked

PREVIEW_MAX_SIDE = 1280
PREVIEW_MAX_PIXELS = 1_200_000
CALL_TIMEOUT = 120.0


class ToolError(Exception):
    """A tool was called with unusable arguments or hit a board problem."""


# --------------------------------------------------------------------- bridge
class BoardBridge(QObject):
    """Executes callables in the GUI thread and returns their result."""

    request = Signal(object)

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.request.connect(self._execute, Qt.ConnectionType.QueuedConnection)

    def _execute(self, job):
        try:
            job["result"] = job["fn"]()
        except Exception as ex:
            job["error"] = "%s: %s" % (type(ex).__name__, ex)
        finally:
            job["done"].set()

    def call(self, fn, timeout=CALL_TIMEOUT):
        if QThread.currentThread() is self.thread():
            return fn()                      # already in the GUI thread
        job = {"fn": fn, "done": threading.Event()}
        self.request.emit(job)
        if not job["done"].wait(timeout):
            raise ToolError("Board call timed out after %ss (GUI thread busy?)" % timeout)
        if "error" in job:
            raise ToolError(job["error"])
        return job.get("result")


# ---------------------------------------------------------------------- tools
class BoardTools:
    """Registry + dispatcher for everything the agent may do on the board."""

    def __init__(self, win):
        self.win = win
        self.bridge = BoardBridge(win)
        self._specs = {}
        self._register()

    # ---- public API ----------------------------------------------------
    def schemas(self):
        """Tool list in the normalised provider format."""
        return [{"name": spec["name"],
                 "description": spec["description"],
                 "parameters": spec["parameters"]}
                for spec in self._specs.values()]

    def names(self):
        return list(self._specs)

    def is_dangerous(self, name):
        return bool(self._specs.get(name, {}).get("danger"))

    def mutates(self, name):
        return bool(self._specs.get(name, {}).get("mutates"))

    def call(self, name, arguments=None):
        """Run a tool from any thread. Never raises: errors come back as data."""
        spec = self._specs.get(name)
        if spec is None:
            return {"ok": False, "error": "Unknown tool '%s'" % name}
        arguments = dict(arguments or {})
        if arguments.get("__error__"):
            return {"ok": False, "error": arguments["__error__"],
                    "raw": arguments.get("__raw__", "")}
        try:
            return self.bridge.call(lambda: self._invoke(spec, arguments))
        except ToolError as ex:
            return {"ok": False, "error": str(ex)}
        except Exception as ex:
            return {"ok": False, "error": "%s: %s" % (type(ex).__name__, ex)}

    def _invoke(self, spec, arguments):
        if spec.get("mutates"):
            self._quiesce()
        result = spec["fn"](arguments)
        if spec.get("mutates"):
            self.win.record_history()
            self.win.view.viewport().update()
            self.win.view.schedule_media_update()
        if isinstance(result, dict):
            result.setdefault("ok", True)
            return result
        return {"ok": True, "value": result}

    # ---- helpers (GUI thread only) -------------------------------------
    def _quiesce(self):
        """Leave interactive modes so a tool never fights an active gesture."""
        view = self.win.view
        if getattr(view, "_resize", None):
            view.resize_end(False)
        if getattr(view, "_tf", None):
            view.tf_end(False, quiet=True)
        if getattr(view, "_crop", None):
            view.finish_crop(False)

    def _item(self, uid):
        for item in self.win.objects:
            if item.uid == uid:
                return item
        raise ToolError("No item with uid '%s'" % uid)

    def _items(self, uids):
        if isinstance(uids, str):
            uids = [uids]
        return [self._item(uid) for uid in (uids or [])]

    def _targets(self, arguments, allow_all=False):
        """Explicit uids, else current selection, else (optionally) everything."""
        uids = arguments.get("uids")
        if uids:
            return self._items(uids)
        selected = self.win.selected_top()
        if selected:
            return selected
        if allow_all:
            return list(self.win.top_items())
        raise ToolError("Nothing selected. Pass 'uids' or select items first.")

    def _select(self, items):
        self.win.scene.clearSelection()
        for item in items:
            item.setSelected(True)

    @staticmethod
    def _name(item):
        if getattr(item, "kind", None) == "group":
            return getattr(item, "title", "Group")
        path = getattr(item, "path", None)
        # StrokeItem inherits QGraphicsPathItem.path() - a METHOD, not a file
        # path. Only a real string may be treated as a file name.
        if isinstance(path, str) and path:
            return os.path.basename(path)
        return getattr(item, "kind", "item")

    @staticmethod
    def _color(value, default="#ffffff"):
        color = QColor(value or default)
        if not color.isValid():
            raise ToolError("Invalid color '%s' (use #rrggbb)" % value)
        return color

    def _brief(self, item):
        rect = self.win.full_rect(item)
        return {"uid": item.uid,
                "kind": item.kind,
                "name": self._name(item),
                "x": round(rect.left(), 1), "y": round(rect.top(), 1),
                "w": round(rect.width(), 1), "h": round(rect.height(), 1),
                "scale": round(item.scale(), 4),
                "rotation": round(item.rotation(), 2),
                "z": round(item.zValue(), 2),
                "locked": is_locked(item),
                "selected": item.isSelected(),
                "note": note_text(item)[:120]}

    def _render(self, rect, max_side):
        """Clean scene render: no selection handles, no view overlays."""
        if rect.isNull() or rect.isEmpty():
            raise ToolError("Nothing to render (empty area)")
        scale = min(1.0, max_side / max(rect.width(), rect.height()))
        if rect.width() * rect.height() * scale * scale > PREVIEW_MAX_PIXELS:
            scale *= (PREVIEW_MAX_PIXELS / (rect.width() * rect.height() * scale * scale)) ** 0.5
        width = max(1, int(rect.width() * scale))
        height = max(1, int(rect.height() * scale))
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        image.fill(QColor("#2b2b2b"))
        selected = self.win.scene.selectedItems()
        self.win.scene.clearSelection()
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            self.win.scene.render(painter, QRectF(0, 0, width, height), rect)
        finally:
            painter.end()
            for item in selected:
                item.setSelected(True)
        ba = QByteArray()                      # keep a reference: QBuffer does not own it
        buffer = QBuffer(ba)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise ToolError("PNG encoding failed")
        buffer.close()
        data = base64.b64encode(bytes(ba)).decode("ascii")
        return {"width": width, "height": height,
                "_image": {"data": data, "mime": "image/png"}}

    # ================================================================ READ
    def t_summary(self, a):
        win = self.win
        counts = {}
        for item in win.top_items():
            counts[item.kind] = counts.get(item.kind, 0) + 1
        bounds = QRectF()
        for item in win.top_items():
            bounds = bounds.united(win.full_rect(item))
        return {"project": os.path.basename(win.project_path) if win.project_path else "Untitled",
                "top_level_items": len(win.top_items()),
                "total_objects": len(win.objects),
                "by_kind": counts,
                "selected": [i.uid for i in win.selected_top()],
                "bounds": None if bounds.isNull() else {
                    "x": round(bounds.left(), 1), "y": round(bounds.top(), 1),
                    "w": round(bounds.width(), 1), "h": round(bounds.height(), 1)},
                "zoom": round(win.view.cur_zoom(), 4),
                "dirty": bool(win._dirty)}

    def t_list(self, a):
        kind = a.get("kind")
        limit = max(1, min(500, int(a.get("limit", 200))))
        items = self.win.top_items()
        if kind:
            items = [i for i in items if i.kind == kind]
        items = sorted(items, key=lambda i: i.zValue())
        return {"count": len(items),
                "items": [self._brief(i) for i in items[:limit]],
                "truncated": len(items) > limit}

    def t_get(self, a):
        item = self._item(a["uid"])
        data = self.win.dump(item, None)
        return {"item": self._brief(item), "data": data}

    def t_find(self, a):
        query = (a.get("query") or "").casefold().strip()
        if not query:
            raise ToolError("'query' must not be empty")
        hits = []
        for top in self.win.top_items():
            for item in walk(top):
                path = getattr(item, "path", "")
                if not isinstance(path, str):
                    path = ""                # StrokeItem.path is a method
                haystack = " ".join(str(v) for v in (
                    item.kind, self._name(item), note_text(item),
                    getattr(item, "toPlainText", lambda: "")(),
                    path)).casefold()
                if query in haystack:
                    hits.append(self._brief(item))
                    break
        return {"count": len(hits), "items": hits[:50]}

    def t_preview(self, a):
        scope = a.get("scope", "board")
        max_side = max(256, min(2048, int(a.get("max_size", PREVIEW_MAX_SIDE))))
        if scope == "item":
            rect = self.win.full_rect(self._item(a["uid"]))
        elif scope == "selection":
            items = self.win.selected_top()
            if not items:
                raise ToolError("Nothing selected for a selection preview")
            rect = QRectF()
            for item in items:
                rect = rect.united(self.win.full_rect(item))
        else:
            items = self.win.top_items()
            if not items:
                raise ToolError("The board is empty")
            rect = QRectF()
            for item in items:
                rect = rect.united(self.win.full_rect(item))
        rect = rect.adjusted(-20, -20, 20, 20)
        result = self._render(rect, max_side)
        result["scope"] = scope
        result["message"] = "Preview image attached in the next message."
        return result

    # ============================================================== CREATE
    def t_add_image(self, a):
        pos = QPointF(float(a.get("x", 0)), float(a.get("y", 0)))
        if not a.get("x") and not a.get("y"):
            pos = self.win.view_center()
        path = a.get("path")
        internal = False
        if a.get("image_base64"):
            raw = base64.b64decode(a["image_base64"])
            image = QImage.fromData(raw)
            if image.isNull():
                raise ToolError("image_base64 is not a decodable image")
            path = self.win.save_image(image)
            if not path:
                raise ToolError("Could not save the decoded image")
            internal = True      # app-generated path, never chosen by the model
        if not path:
            raise ToolError("Provide 'path' (inside the agent folder) or 'image_base64'")
        path = os.path.abspath(os.path.expanduser(path))
        if not internal:
            # SANDBOX: a model-supplied path must stay inside the agent folder.
            # Lazy import keeps this module importable on its own.
            from .sandbox import sandbox_path
            path = sandbox_path(self.win, path)
        if not os.path.isfile(path):
            raise ToolError("File not found: %s" % path)
        item = self.win.add_file(path, pos)
        if item is None:
            raise ToolError("Unsupported or unreadable file: %s" % path)
        self._select([item])
        return {"item": self._brief(item)}

    def t_add_text(self, a):
        text = a.get("text")
        if not text:
            raise ToolError("'text' must not be empty")
        width = float(a.get("width", 360))
        height = float(a.get("height", 100))
        color = self._color(a.get("color"), self.win.draw_color)
        item = TextItem(text, color.name(), QFont("Segoe UI"), height)
        item.setTextWidth(width)
        self.win.objects.add(item)
        self.win.scene.addItem(item)
        item.setPos(float(a.get("x", 0)), float(a.get("y", 0)))
        item.setZValue(self.win.max_z() + 1)
        self._select([item])
        return {"item": self._brief(item)}

    def t_add_frame(self, a):
        color = self._color(a.get("color"), self.win.draw_color)
        item = FrameItem(float(a.get("width", 320)), float(a.get("height", 200)),
                         color=color.name(), line_width=float(a.get("line_width", 28)))
        self.win.objects.add(item)
        self.win.scene.addItem(item)
        item.setPos(float(a.get("x", 0)), float(a.get("y", 0)))
        item.setZValue(self.win.max_z() + 1)
        self._select([item])
        return {"item": self._brief(item)}

    # ============================================================= MODIFY
    def t_move(self, a):
        items = self._targets(a)
        movable = [i for i in items if not is_locked(i)]
        skipped = [i.uid for i in items if is_locked(i)]
        if not movable:
            raise ToolError("All target items are locked")
        if "dx" in a or "dy" in a:
            dx, dy = float(a.get("dx", 0)), float(a.get("dy", 0))
        else:
            if "x" not in a and "y" not in a:
                raise ToolError("Provide x/y (absolute) or dx/dy (relative)")
            if len(movable) == 1:
                movable[0].setPos(float(a.get("x", movable[0].pos().x())),
                                  float(a.get("y", movable[0].pos().y())))
                place_note(movable[0])
                return {"moved": [i.uid for i in movable], "skipped_locked": skipped}
            bounds = QRectF()
            for item in movable:
                bounds = bounds.united(self.win.full_rect(item))
            dx = float(a.get("x", bounds.left())) - bounds.left()
            dy = float(a.get("y", bounds.top())) - bounds.top()
        for item in movable:
            item.moveBy(dx, dy)
            place_note(item)
        return {"moved": [i.uid for i in movable], "skipped_locked": skipped}

    def t_transform(self, a):
        items = [i for i in self._targets(a) if not is_locked(i)]
        if not items:
            raise ToolError("No unlocked target items")
        scale = a.get("scale")
        rotation = a.get("rotation")
        if scale is None and rotation is None:
            raise ToolError("Provide 'scale' and/or 'rotation'")
        for item in items:
            self.win._set_keep_center(item,
                                      scale=float(scale) if scale is not None else None,
                                      rot=float(rotation) if rotation is not None else None)
        return {"transformed": [self._brief(i) for i in items]}

    def t_recolor(self, a):
        item = self._item(a["uid"])
        color = self._color(a.get("color"))
        recolor = getattr(self.win.view, "_recolor_item", None)
        if not callable(recolor):
            raise ToolError("Recoloring is unavailable in this build")
        if item.kind not in ("frame", "stroke", "text"):
            raise ToolError("Recolor supports frame, stroke and text; '%s' is not supported"
                            % item.kind)
        if not recolor(item, color):
            raise ToolError("The item exposed no color attribute to change")
        return {"recolored": item.uid, "color": color.name()}

    def t_annotate(self, a):
        item = self._item(a["uid"])
        set_note(item, a.get("text", ""))
        top = item.topLevelItem()
        if top is not item and hasattr(top, "refresh"):
            top.refresh()
        return {"uid": item.uid, "note": note_text(item)}

    def t_lock(self, a):
        items = self._targets(a, allow_all=True)
        locked = bool(a.get("locked", True))
        for item in items:
            set_locked(item, locked)
        return {"locked" if locked else "unlocked": [i.uid for i in items]}

    def t_group(self, a):
        items = self._items(a.get("uids"))
        if len(items) < 2:
            raise ToolError("Grouping needs at least two uids")
        group = GroupItem(a.get("title") or "Group")
        z = self.win.max_z() + 1
        self.win.objects.add(group)
        self.win.scene.addItem(group)
        for item in sorted(items, key=lambda i: i.zValue()):
            item.setSelected(False)
            group.addToGroup(item)
        group.setZValue(z)
        group.refresh()
        self._select([group])
        return {"group": self._brief(group), "members": [i.uid for i in items]}

    def t_ungroup(self, a):
        groups = [i for i in self._targets(a) if i.kind == "group"]
        if not groups:
            raise ToolError("No group among the targets")
        released = []
        for group in groups:
            if group.note:
                self.win.scene.removeItem(group.note)
                group.note = None
            for child in group.members():
                group.removeFromGroup(child)
                child.setZValue(self.win.max_z() + 1)
                place_note(child)
                child.setSelected(True)
                released.append(child.uid)
            self.win.objects.discard(group)
            self.win.scene.removeItem(group)
        return {"released": released}

    def t_arrange(self, a):
        mode = a.get("mode")
        items = self._targets(a, allow_all=(mode == "pack"))
        self._select(items)
        win = self.win
        if mode in ("align_left", "align_right", "align_top", "align_bottom"):
            win.align(mode.split("_", 1)[1])
        elif mode == "row":
            win.stack(True)
        elif mode == "column":
            win.stack(False)
        elif mode == "distribute_h":
            win.distribute(True)
        elif mode == "distribute_v":
            win.distribute(False)
        elif mode == "pack":
            win.pack()
        elif mode == "normalize_height":
            win.normalize(True)
        elif mode == "normalize_width":
            win.normalize(False)
        elif mode == "bring_front":
            win.restack(True)
        elif mode == "send_back":
            win.restack(False)
        else:
            raise ToolError("Unknown arrange mode '%s'" % mode)
        return {"mode": mode, "affected": [i.uid for i in items]}

    def t_delete(self, a):
        items = self._targets(a)
        removed = []
        for item in items:
            removed.append(item.uid)
            self.win.remove_item(item)
        return {"deleted": removed}

    def t_duplicate(self, a):
        items = self._targets(a)
        copies = []
        self.win.scene.clearSelection()
        for item in items:
            clone = self.win.build(self.win.dump(item, None), None, new_ids=True)
            if clone is not None:
                clone.setZValue(self.win.max_z() + 1)
                clone.moveBy(float(a.get("dx", 40)), float(a.get("dy", 40)))
                clone.setSelected(True)
                copies.append(self._brief(clone))
        return {"copies": copies}

    # =============================================================== VIEW
    def t_select(self, a):
        uids = a.get("uids")
        if not uids:
            self.win.scene.clearSelection()
            return {"selected": []}
        items = self._items(uids)
        self._select(items)
        return {"selected": [i.uid for i in items]}

    def t_view(self, a):
        action = a.get("action", "fit_all")
        if action == "fit_all":
            self.win.fit_all()
        elif action == "fit_selection":
            self.win.fit_selected()
        elif action == "zoom_100":
            self.win.zoom_reset()
        elif action == "set":
            zoom = float(a.get("zoom", 1.0))
            if zoom <= 0:
                raise ToolError("'zoom' must be positive")
            self.win.view.set_view(zoom, QPointF(float(a.get("cx", 0)), float(a.get("cy", 0))))
        else:
            raise ToolError("Unknown view action '%s'" % action)
        return {"zoom": round(self.win.view.cur_zoom(), 4)}

    def t_undo(self, a):
        self.win.undo()
        return {"undone": True}

    def t_redo(self, a):
        self.win.redo()
        return {"redone": True}

    # =========================================================== REGISTRY
    def _add(self, name, description, parameters, fn, mutates=False, danger=False):
        self._specs[name] = {"name": name, "description": description,
                             "parameters": parameters, "fn": fn,
                             "mutates": mutates, "danger": danger}

    @staticmethod
    def _schema(properties, required=()):
        return {"type": "object", "properties": properties,
                "required": list(required), "additionalProperties": False}

    def _register(self):
        S = self._schema
        uid = {"type": "string", "description": "Item uid from board_list_items"}
        uids = {"type": "array", "items": {"type": "string"},
                "description": "Item uids; omit to use the current selection"}
        color = {"type": "string", "description": "Hex color, e.g. #ff8800"}

        self._add("board_summary",
                  "Compact overview of the board: item counts, bounds, zoom, selection. "
                  "Call this first to understand the current state.",
                  S({}), self.t_summary)

        self._add("board_list_items",
                  "List top-level items with uid, kind, name, position and size. "
                  "Use the returned uids in every other tool.",
                  S({"kind": {"type": "string",
                              "enum": ["image", "video", "text", "frame", "stroke",
                                       "group", "document", "model3d", "missing"]},
                     "limit": {"type": "integer", "description": "Max items (default 200)"}}),
                  self.t_list)

        self._add("board_get_item", "Full serialized data of one item, including its note.",
                  S({"uid": uid}, ["uid"]), self.t_get)

        self._add("board_find", "Search items by file name, group title, note or text content.",
                  S({"query": {"type": "string"}}, ["query"]), self.t_find)

        self._add("board_preview",
                  "Render a clean PNG of the board, the selection or one item and show it "
                  "to you. Use it to check the visual result of your changes.",
                  S({"scope": {"type": "string", "enum": ["board", "selection", "item"]},
                     "uid": uid,
                     "max_size": {"type": "integer", "description": "Longest side in px"}}),
                  self.t_preview)

        self._add("board_add_image",
                  "Place an image (or any supported media) on the board. A local 'path' "
                  "is only accepted INSIDE the agent folder; files elsewhere are refused. "
                  "Alternatively pass 'image_base64' (stored by the app itself).",
                  S({"path": {"type": "string"}, "image_base64": {"type": "string"},
                     "x": {"type": "number"}, "y": {"type": "number"}}),
                  self.t_add_image, mutates=True)

        self._add("board_add_text", "Create a text object on the board.",
                  S({"text": {"type": "string"}, "x": {"type": "number"},
                     "y": {"type": "number"}, "width": {"type": "number"},
                     "height": {"type": "number"}, "color": color}, ["text"]),
                  self.t_add_text, mutates=True)

        self._add("board_add_frame", "Create an empty rectangular frame (a labelled area).",
                  S({"x": {"type": "number"}, "y": {"type": "number"},
                     "width": {"type": "number"}, "height": {"type": "number"},
                     "line_width": {"type": "number"}, "color": color}),
                  self.t_add_frame, mutates=True)

        self._add("board_move",
                  "Move items. x/y are absolute scene coordinates (for several items the "
                  "bounding box corner is placed there); dx/dy move relative. Locked items "
                  "are skipped.",
                  S({"uids": uids, "x": {"type": "number"}, "y": {"type": "number"},
                     "dx": {"type": "number"}, "dy": {"type": "number"}}),
                  self.t_move, mutates=True)

        self._add("board_transform",
                  "Set absolute scale multiplier and/or rotation in degrees, keeping the "
                  "visual center in place.",
                  S({"uids": uids, "scale": {"type": "number"},
                     "rotation": {"type": "number"}}),
                  self.t_transform, mutates=True)

        self._add("board_recolor",
                  "Recolor a frame, stroke or text object in place (no extra layers).",
                  S({"uid": uid, "color": color}, ["uid", "color"]),
                  self.t_recolor, mutates=True)

        self._add("board_annotate", "Attach or replace the yellow sticky note under an item.",
                  S({"uid": uid, "text": {"type": "string"}}, ["uid"]),
                  self.t_annotate, mutates=True)

        self._add("board_lock", "Lock or unlock items so their geometry cannot be changed.",
                  S({"uids": uids, "locked": {"type": "boolean"}}),
                  self.t_lock, mutates=True)

        self._add("board_group", "Group two or more items under a title.",
                  S({"uids": uids, "title": {"type": "string"}}, ["uids"]),
                  self.t_group, mutates=True)

        self._add("board_ungroup", "Dissolve groups and release their members.",
                  S({"uids": uids}), self.t_ungroup, mutates=True)

        self._add("board_arrange",
                  "Arrange items: align_*, row, column, distribute_h/v, pack, "
                  "normalize_height/width, bring_front, send_back. "
                  "Most modes need 2+ items, distribute needs 3+.",
                  S({"mode": {"type": "string",
                              "enum": ["align_left", "align_right", "align_top", "align_bottom",
                                       "row", "column", "distribute_h", "distribute_v",
                                       "pack", "normalize_height", "normalize_width",
                                       "bring_front", "send_back"]},
                     "uids": uids}, ["mode"]),
                  self.t_arrange, mutates=True)

        self._add("board_duplicate", "Copy items with a small offset.",
                  S({"uids": uids, "dx": {"type": "number"}, "dy": {"type": "number"}}),
                  self.t_duplicate, mutates=True)

        self._add("board_delete", "Delete items from the board. Destructive - ask the user first.",
                  S({"uids": uids}), self.t_delete, mutates=True, danger=True)

        self._add("board_select", "Select items (or clear the selection with an empty list).",
                  S({"uids": uids}), self.t_select)

        self._add("board_view", "Move the camera: fit_all, fit_selection, zoom_100 or set.",
                  S({"action": {"type": "string",
                                "enum": ["fit_all", "fit_selection", "zoom_100", "set"]},
                     "zoom": {"type": "number"}, "cx": {"type": "number"},
                     "cy": {"type": "number"}}),
                  self.t_view)

        self._add("board_undo", "Undo the last board change.", S({}), self.t_undo, mutates=True)
        self._add("board_redo", "Redo the last undone change.", S({}), self.t_redo, mutates=True)

        # Crop tools live in their own module: board_crop_tools.py
        from .board_crop_tools import register_crop_tools
        register_crop_tools(self)

        # Color/ink/media/project/settings tools live in board_extra_tools.py
        from .board_extra_tools import register_extra_tools
        register_extra_tools(self)


# ------------------------------------------------------------------ self-test
def _selftest():
    """python -m refsynapse.ai.board_tools  (opens the real window)"""
    from PySide6.QtWidgets import QApplication
    from ..main_window import MainWindow

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    tools = BoardTools(win)
    print("Registered tools:", len(tools.names()))

    def run():
        steps = [
            ("board_summary", {}),
            ("board_add_frame", {"x": 0, "y": 0, "width": 400, "height": 300,
                                 "color": "#ff8800"}),
            ("board_add_text", {"text": "Agent was here", "x": 20, "y": 340}),
            ("board_add_frame", {"x": 500, "y": 0, "width": 400, "height": 300,
                                 "color": "#4fc3f7"}),
        ]
        created = []
        for name, args in steps:
            result = tools.call(name, args)
            print(name, "->", {k: v for k, v in result.items() if k != "_image"})
            item = result.get("item")
            if item:
                created.append(item["uid"])
        if len(created) >= 2:
            print("group ->", tools.call("board_group",
                                         {"uids": created, "title": "Agent group"}))
        print("arrange ->", tools.call("board_arrange", {"mode": "row"}))
        print("view ->", tools.call("board_view", {"action": "fit_all"}))
        preview = tools.call("board_preview", {"scope": "board", "max_size": 640})
        print("preview ->", {k: v for k, v in preview.items() if k != "_image"},
              "| base64 chars:", len(preview.get("_image", {}).get("data", "")))
        print("undo ->", tools.call("board_undo", {}))
        print("DONE - close the window to exit")

    QTimer.singleShot(800, run)
    sys.exit(app.exec())


if __name__ == "__main__":
    _selftest()