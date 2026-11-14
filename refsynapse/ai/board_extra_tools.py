"""Extra agent tools for RefSynapse.

Batch 2 (color/ink):
  board_pick_color     - sample the color at scene coordinates (clean render)
  board_fill           - paint bucket: vector recolor or raster flood overlay
  board_draw_stroke    - marker stroke from a list of scene points
  board_set_draw_color - set the active drawing color

Batch 3 (media):
  board_video          - play / pause / toggle / seek / status
  board_video_sound    - mute / unmute / volume (0-100)
  board_flip           - horizontal mirror of an image
  board_export_item    - save one item to a file (original / frame / render)
  board_document_page  - flip through PDF / text document pages (1-based)

Batch 4 (project & files):
  board_save_project   - save the project (explicit path for first save / save-as)
  board_export_png     - render board or selection to a PNG file
  board_export_pages   - render document pages to PNGs in a folder
  board_import_url     - download an image from an http(s) URL onto the board
                         (polite: per-host gap + auto-retry on 429/503)

Batch 5 (settings):
  board_settings       - read / change tool settings (validated, saved, UI synced)

Batch 6 (web search, lives in board_web_tools.py):
  board_web_search_images - numbered contact sheet of web image results

SANDBOX. Every path the agent reads or writes on its own initiative must stay
inside one folder (default <repo>/agent, override settings["agent_sandbox"]).
The rule lives in sandbox.py and is enforced in code, so it holds for local
and cloud providers alike. Downloads are relocated into <sandbox>/downloads.

Registered from BoardTools._register() via register_extra_tools(tools).
"""

import math
import os
import re
import shutil
import time
import urllib.parse

from PySide6.QtCore import QPointF, QRectF, QEventLoop, QTimer
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtMultimedia import QMediaPlayer

from ..config import save_settings, PROJECT_EXT
from ..item_tools import is_locked
from ..items import smooth_stroke_path
from .board_tools import ToolError
from .sandbox import sandbox_root, sandbox_path

PAINTABLE = ("image", "stroke", "frame", "text")
EXPORT_MAX_PIXELS = 24_000_000

# Keeps running QThread objects alive until they finish (import_url timeout).
_DOWNLOADS = []

# ---- polite web import -----------------------------------------------------
IMPORT_HOST_GAP = 2.0             # min seconds between downloads from one host
IMPORT_RETRY_WAITS = (5.0, 12.0)  # pauses before automatic retries on 429/503
_LAST_IMPORT = {}                 # host -> time.monotonic() of the last request


def _clean_url(url):
    """Drop tracking parameters (utm_*) - same picture, cleaner request."""
    try:
        parts = urllib.parse.urlsplit(url)
        if not parts.query:
            return url
        pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        kept = [(k, v) for k, v in pairs if not k.lower().startswith("utm_")]
        if len(kept) == len(pairs):
            return url
        return urllib.parse.urlunsplit(
            parts._replace(query=urllib.parse.urlencode(kept)))
    except ValueError:
        return url


def _import_host(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _host_wait(host):
    """Seconds left before the next request to this host is polite."""
    last = _LAST_IMPORT.get(host)
    if last is None:
        return 0.0
    return max(0.0, IMPORT_HOST_GAP - (time.monotonic() - last))


def _busy_error(error):
    """True for 'server is busy' answers worth retrying (429/503)."""
    low = str(error).lower()
    return ("too many requests" in low or "service unavailable" in low
            or re.search(r"\b(?:429|503)\b", low) is not None)


def register_extra_tools(tools):
    """Add the color/ink tools (batch 2), then the later batches."""
    win = tools.win
    S = tools._schema
    uid = {"type": "string", "description": "Item uid from board_list_items"}
    number = {"type": "number"}

    # ---- helpers (GUI thread only) -----------------------------------
    def point_arg(a):
        try:
            return QPointF(float(a["x"]), float(a["y"]))
        except (KeyError, TypeError, ValueError):
            raise ToolError("'x' and 'y' (scene coordinates) are required numbers")

    def color_arg(a, key="color"):
        value = a.get(key)
        if value is None:
            value = win.draw_color
        color = QColor(value)
        if not color.isValid():
            raise ToolError("Invalid color '%s' (use #rrggbb)" % value)
        return color

    def apply_draw_color(color):
        name = color.name()
        win.draw_color = name
        win.titlebar.set_color(name)
        win.settings["draw_color"] = name
        save_settings(win.settings)

    # ---- eyedropper ----------------------------------------------------
    def t_pick_color(a):
        pos = point_arg(a)
        scene = win.scene
        selected = scene.selectedItems()
        scene.clearSelection()
        image = QImage(1, 1, QImage.Format.Format_ARGB32)
        image.fill(QColor(0, 0, 0, 0))
        painter = QPainter(image)
        try:
            scene.render(painter, QRectF(0, 0, 1, 1),
                         QRectF(pos.x() - 0.5, pos.y() - 0.5, 1.0, 1.0))
        finally:
            painter.end()
            for item in selected:
                item.setSelected(True)
        color = image.pixelColor(0, 0)
        result = {"x": pos.x(), "y": pos.y(),
                  "hex": color.name(),
                  "rgba": [color.red(), color.green(), color.blue(), color.alpha()],
                  "transparent": color.alpha() == 0}
        if a.get("set_draw_color"):
            apply_draw_color(color)
            result["draw_color"] = color.name()
        return result

    def t_set_draw_color(a):
        color = color_arg(a)
        apply_draw_color(color)
        return {"draw_color": color.name()}

    # ---- paint bucket ----------------------------------------------------
    def fill_target(a, pos):
        if a.get("uid"):
            item = tools._item(a["uid"])
            if getattr(item, "kind", None) not in PAINTABLE:
                raise ToolError("'%s' items cannot be filled" % item.kind)
            if not item.mapRectToScene(item.boundingRect()).contains(pos):
                raise ToolError("The point is outside item '%s'" % item.uid)
            return item
        for item in win.scene.items(pos):
            if getattr(item, "_refsynapse_fill", False):
                continue
            if getattr(item, "kind", None) in PAINTABLE:
                return item
        raise ToolError("No fillable item at that point (image/stroke/frame/text)")

    def t_fill(a):
        pos = point_arg(a)
        color = color_arg(a)
        target = fill_target(a, pos)
        if is_locked(target):
            raise ToolError("Item is locked - unlock it with board_lock first")
        try:
            tolerance = int(a.get("tolerance", win.settings.get("fill_tolerance", 24)))
        except (TypeError, ValueError):
            tolerance = 24
        tolerance = max(0, min(255, tolerance))

        view = win.view
        # Vector objects are recolored in place, exactly like Shift+I does.
        if target.kind != "image":
            if not view._recolor_item(target, color):
                raise ToolError("The item exposed no color attribute to change")
            return {"mode": "vector", "uid": target.uid, "color": color.name()}

        # Raster images get a flood-fill overlay above the picture.
        image, source, scale = view._bucket_render(target)
        sx = round((pos.x() - source.left()) * image.width() / source.width())
        sy = round((pos.y() - source.top()) * image.height() / source.height())
        sx = max(0, min(image.width() - 1, sx))
        sy = max(0, min(image.height() - 1, sy))
        result = view._bucket_flood(image, sx, sy, color, tolerance)
        if result is None:
            raise ToolError("No matching area at that point (try a higher tolerance)")
        path = win.save_image(result)
        if not path:
            raise ToolError("Could not save the fill result")
        overlay = win.add_file(path, source.topLeft())
        if overlay is None:
            raise ToolError("Could not add the fill overlay")
        overlay.setScale(1.0 / scale)
        overlay._refsynapse_fill = True
        tools._select([overlay])
        return {"mode": "raster", "target": target.uid,
                "overlay": tools._brief(overlay),
                "color": color.name(), "tolerance": tolerance}

    # ---- marker stroke ----------------------------------------------------
    def t_draw_stroke(a):
        raw = a.get("points") or []
        if len(raw) < 1:
            raise ToolError("'points' must contain at least one [x, y] pair")
        if len(raw) > 2000:
            raise ToolError("Too many points (max 2000)")
        try:
            pts = [QPointF(float(p[0]), float(p[1])) for p in raw]
        except (TypeError, ValueError, IndexError):
            raise ToolError("'points' must be a list of [x, y] number pairs")
        item = win.add_stroke(pts[0])
        if item is None:
            raise ToolError("Could not create a stroke")
        item.points = [p - pts[0] for p in pts]
        item.setPath(smooth_stroke_path(item.points))
        pen = item.pen()
        if a.get("color") is not None:
            color = color_arg(a)
            item.color = color.name()
            pen.setColor(color)
        if a.get("width") is not None:
            width = float(a["width"])
            if not (0.5 <= width <= 200):
                raise ToolError("'width' must be between 0.5 and 200")
            item.width = width
            pen.setWidthF(width)
        item.setPen(pen)
        tools._select([item])
        return {"item": tools._brief(item), "points": len(pts)}

    # ---- registry (batch 2) -------------------------------------------
    tools._add("board_pick_color",
               "Sample the color at scene coordinates (x, y) from a clean render "
               "of the board - selection handles are never captured. Optionally "
               "sets the active drawing color.",
               S({"x": number, "y": number, "set_draw_color": {"type": "boolean"}},
                 ["x", "y"]),
               t_pick_color)

    tools._add("board_fill",
               "Paint bucket at scene coordinates (x, y). Frames, strokes and text "
               "are recolored in place; images get a flood-fill overlay of the "
               "matching area (tolerance 0-255, default from settings). Optionally "
               "restrict the target with 'uid'.",
               S({"x": number, "y": number, "uid": uid,
                  "color": {"type": "string", "description": "Hex color; default = active draw color"},
                  "tolerance": {"type": "integer"}},
                 ["x", "y"]),
               t_fill, mutates=True)

    tools._add("board_draw_stroke",
               "Draw a smooth marker stroke through a list of scene points "
               "[[x1,y1],[x2,y2],...]. One point makes a dot. Color and width "
               "are optional (default = active draw color/width).",
               S({"points": {"type": "array",
                             "items": {"type": "array", "items": {"type": "number"},
                                       "minItems": 2, "maxItems": 2}},
                  "color": {"type": "string"},
                  "width": number},
                 ["points"]),
               t_draw_stroke, mutates=True)

    tools._add("board_set_draw_color",
               "Set the active drawing color used by the marker, new frames/text "
               "and the fill tool.",
               S({"color": {"type": "string"}}, ["color"]),
               t_set_draw_color)

    # ---- next batches -------------------------------------------------
    register_media_tools(tools)
    register_project_tools(tools)
    register_settings_tools(tools)

    # Web image search (batch 6) lives in its own module: board_web_tools.py
    from .board_web_tools import register_web_tools
    register_web_tools(tools)


def register_media_tools(tools):
    """Batch 3: video control, sound, horizontal flip, item export, pages."""
    win = tools.win
    S = tools._schema
    uid = {"type": "string", "description": "Item uid from board_list_items"}
    sb = sandbox_root(win)

    # ---- helpers (GUI thread only) -----------------------------------
    def video_of(a):
        item = tools._item(a["uid"])
        if getattr(item, "kind", None) != "video":
            raise ToolError("'%s' is not a video" % getattr(item, "kind", "?"))
        return item

    def video_status(item):
        playing = (item.player.playbackState()
                   == QMediaPlayer.PlaybackState.PlayingState)
        return {"uid": item.uid,
                "playing": playing,
                "position_ms": int(item.current_position()),
                "duration_ms": int(item.player.duration()),
                "muted": bool(item.audio.isMuted()),
                "volume": round(item.audio.volume() * 100)}

    def render_item_image(item, scale, background):
        rect = win.full_rect(item)
        if rect.isEmpty():
            raise ToolError("Nothing to export (empty item)")
        if background == "transparent":
            fill = QColor(0, 0, 0, 0)
        else:
            fill = QColor(background)
            if not fill.isValid():
                raise ToolError("Invalid background '%s' (hex or 'transparent')"
                                % background)
        if rect.width() * rect.height() * scale * scale > EXPORT_MAX_PIXELS:
            scale = math.sqrt(EXPORT_MAX_PIXELS / (rect.width() * rect.height()))
        w = max(1, int(rect.width() * scale))
        h = max(1, int(rect.height() * scale))
        image = QImage(w, h, QImage.Format.Format_ARGB32)
        image.fill(fill)
        selected = win.scene.selectedItems()
        win.scene.clearSelection()
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            win.scene.render(painter, QRectF(0, 0, w, h), rect)
        finally:
            painter.end()
            for it in selected:
                it.setSelected(True)
        return image

    # ---- video -----------------------------------------------------------
    def t_video(a):
        item = video_of(a)
        action = a.get("action", "status")
        playing = (item.player.playbackState()
                   == QMediaPlayer.PlaybackState.PlayingState)
        if action == "toggle":
            item.toggle()
        elif action == "play":
            if not playing:
                item.toggle()
        elif action == "pause":
            if playing:
                item.toggle()
        elif action == "seek":
            if "seconds" in a:
                ms = float(a["seconds"]) * 1000.0
            elif "position_ms" in a:
                ms = float(a["position_ms"])
            else:
                raise ToolError("seek needs 'seconds' or 'position_ms'")
            if ms < 0:
                raise ToolError("Position must not be negative")
            item.seek(int(ms))
        elif action != "status":
            raise ToolError("Unknown video action '%s'" % action)
        return video_status(item)

    def t_video_sound(a):
        item = video_of(a)
        if "muted" in a:
            item.audio.setMuted(bool(a["muted"]))
        elif "volume" not in a:
            item.toggle_mute()
        if "volume" in a:
            volume = float(a["volume"])
            if not (0 <= volume <= 100):
                raise ToolError("'volume' must be between 0 and 100")
            item.audio.setVolume(volume / 100.0)
        return video_status(item)

    # ---- flip --------------------------------------------------------------
    def t_flip(a):
        item = tools._item(a["uid"])
        if getattr(item, "kind", None) != "image":
            raise ToolError("Only images can be flipped")
        if is_locked(item):
            raise ToolError("Item is locked - unlock it with board_lock first")
        if "flipped" in a:
            new = bool(a["flipped"])
        else:
            new = not bool(getattr(item, "flipped", False))
        item.set_flip(new)
        return {"uid": item.uid, "flipped": new, "item": tools._brief(item)}

    # ---- export one item -----------------------------------------------------
    def t_export_item(a):
        item = tools._item(a["uid"])
        path = sandbox_path(win, a.get("path"))
        if os.path.isdir(path):
            raise ToolError("'%s' is a folder - give a full file name" % path)
        if os.path.exists(path) and not a.get("overwrite"):
            raise ToolError("File already exists - pass overwrite:true to replace it")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError as ex:
            raise ToolError("Cannot create the folder: %s" % ex)
        if item.kind == "image" and hasattr(item, "export_image"):
            if not item.export_image(path):
                raise ToolError("Image export failed (unreadable source?)")
            mode = "original"
        elif item.kind == "video":
            image = item.current_frame_image()
            if image.isNull():
                raise ToolError("No decoded video frame yet - play or seek first")
            if not image.save(path):
                raise ToolError("Could not save the video frame")
            mode = "frame"
        else:
            scale = float(a.get("scale", 2.0))
            if not (0.25 <= scale <= 8):
                raise ToolError("'scale' must be between 0.25 and 8")
            image = render_item_image(item, scale,
                                      a.get("background", "#2b2b2b"))
            if not image.save(path):
                raise ToolError("Could not save the render")
            mode = "render"
        return {"uid": item.uid, "path": path, "mode": mode,
                "bytes": os.path.getsize(path)}

    # ---- document pages -----------------------------------------------------
    def t_document_page(a):
        item = tools._item(a["uid"])
        if getattr(item, "kind", None) != "document":
            raise ToolError("'%s' is not a document" % getattr(item, "kind", "?"))
        if "page" in a:
            item.set_page(int(a["page"]) - 1)   # tool is 1-based, item is 0-based
        elif "delta" in a:
            item.change_page(int(a["delta"]))
        return {"uid": item.uid,
                "page": item.current_page() + 1,
                "pages": item.page_count(),
                "document_kind": getattr(item, "document_kind", "?")}

    # ---- registry (batch 3) -------------------------------------------
    tools._add("board_video",
               "Control a video item: play, pause, toggle, seek (to 'seconds' or "
               "'position_ms') or just read status. Always returns playing, "
               "position_ms, duration_ms, muted, volume.",
               S({"uid": uid,
                  "action": {"type": "string",
                             "enum": ["status", "play", "pause", "toggle", "seek"]},
                  "seconds": {"type": "number"},
                  "position_ms": {"type": "integer"}},
                 ["uid"]),
               t_video, mutates=True)

    tools._add("board_video_sound",
               "Sound of a video: set 'muted' true/false and/or 'volume' 0-100. "
               "With no arguments it toggles mute, like the M key.",
               S({"uid": uid,
                  "muted": {"type": "boolean"},
                  "volume": {"type": "number"}},
                 ["uid"]),
               t_video_sound, mutates=True)

    tools._add("board_flip",
               "Mirror an image horizontally. Omit 'flipped' to toggle. "
               "Non-destructive: the file on disk is never modified.",
               S({"uid": uid, "flipped": {"type": "boolean"}}, ["uid"]),
               t_flip, mutates=True)

    tools._add("board_export_item",
               "Save one item to a file INSIDE the agent folder '%s' (paths outside "
               "are refused). Images export at original resolution with crop and "
               "flip applied; videos export the current frame; other items are "
               "rendered ('scale', 'background': hex or 'transparent'). Existing "
               "files are kept unless overwrite:true." % sb,
               S({"uid": uid,
                  "path": {"type": "string", "description": "Target file inside the agent folder"},
                  "overwrite": {"type": "boolean"},
                  "scale": {"type": "number"},
                  "background": {"type": "string"}},
                 ["uid", "path"]),
               t_export_item)

    tools._add("board_document_page",
               "Turn pages of a PDF/text document. 'page' is 1-based (like the "
               "on-screen label), 'delta' steps relative (+1 next, -1 previous). "
               "No arguments just reports the current page.",
               S({"uid": uid,
                  "page": {"type": "integer"},
                  "delta": {"type": "integer"}},
                 ["uid"]),
               t_document_page, mutates=True)


def register_project_tools(tools):
    """Batch 4: project save, PNG export, page export, web import."""
    win = tools.win
    S = tools._schema
    uid = {"type": "string", "description": "Item uid from board_list_items"}
    sb = sandbox_root(win)

    # Import the app's own export helpers through the controller module.
    # Guarded: a missing/changed export package must never break app startup.
    try:
        from ..export.controller import (render_board, render_items,
                                         document_page_image, unique_path,
                                         base_name, save_image as write_image)
        export_available = True
    except Exception:
        export_available = False

    try:
        from ..web_import import DownloadThread
        web_available = True
    except Exception:
        web_available = False

    # ---- helpers (GUI thread only) -----------------------------------
    def drain(ms):
        """Let queued signals (save completion) land without blocking hard."""
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def check_target_file(path, a):
        if os.path.isdir(path):
            raise ToolError("'%s' is a folder - give a full file name" % path)
        if os.path.exists(path) and not a.get("overwrite"):
            raise ToolError("File already exists - pass overwrite:true to replace it")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError as ex:
            raise ToolError("Cannot create the folder: %s" % ex)

    def background_arg(a):
        value = a.get("background", "#2b2b2b")
        if value == "transparent":
            return None
        if not QColor(value).isValid():
            raise ToolError("Invalid background '%s' (hex or 'transparent')" % value)
        return value

    # ---- save project ----------------------------------------------------
    def t_save_project(a):
        tools._quiesce()
        old = win.project_path
        old_real = os.path.realpath(old) if old else None
        raw = a.get("path")
        if raw:
            target = os.path.normpath(os.path.abspath(os.path.expanduser(str(raw))))
            if not target.lower().endswith(PROJECT_EXT):
                target += PROJECT_EXT
            target = os.path.realpath(target)
            # Saving the open project back to its own file is always allowed;
            # any OTHER location must be inside the agent sandbox.
            if target != old_real:
                target = sandbox_path(win, target)
            if os.path.isdir(target):
                raise ToolError("'%s' is a folder - give a full .refsyn file name" % target)
            if (os.path.exists(target) and target != old_real
                    and not a.get("overwrite")):
                raise ToolError("File already exists - pass overwrite:true to replace it")
            win.project_path = target
        elif not old:
            raise ToolError("The project has never been saved - provide 'path' "
                            "inside the agent folder '%s'" % sb)
        started = time.time()
        try:
            returned = bool(win.save_project(save_as=False, wait=True))
        except Exception as ex:
            win.project_path = old
            raise ToolError("Save failed: %s" % ex)
        drain(300)   # let the queued save-completed signal arrive
        target = win.project_path
        exists = os.path.isfile(target)
        fresh = exists and os.path.getmtime(target) >= started - 2
        saved = returned or bool(getattr(win, "_last_save_success", False)) or fresh
        if not saved:
            win.project_path = old
            raise ToolError("Project save failed (see refsynapse-error.log)")
        if hasattr(win, "update_title"):
            win.update_title()
        return {"saved": True, "path": target,
                "bytes": os.path.getsize(target) if exists else 0}

    # ---- export board / selection to PNG ----------------------------------
    def t_export_png(a):
        if not export_available:
            raise ToolError("Export subsystem is unavailable in this build")
        tools._quiesce()
        scope = a.get("scope", "board")
        if scope == "board":
            items = win.top_items()
        elif scope == "selection":
            items = win.selected_top()
        else:
            raise ToolError("Unknown scope '%s' (board or selection)" % scope)
        if not items:
            raise ToolError("Nothing to export (%s is empty)" % scope)
        path = sandbox_path(win, a.get("path"))
        check_target_file(path, a)
        scale = float(a.get("scale", 1.0))
        if not (0.25 <= scale <= 8):
            raise ToolError("'scale' must be between 0.25 and 8")
        rect = QRectF()
        for item in items:
            rect = rect.united(win.full_rect(item))
        if rect.isEmpty():
            raise ToolError("Nothing to export (empty area)")
        if rect.width() * rect.height() * scale * scale > EXPORT_MAX_PIXELS:
            scale = math.sqrt(EXPORT_MAX_PIXELS / (rect.width() * rect.height()))
        background = background_arg(a)
        if scope == "board":
            image = render_board(win, scale, background)
        else:
            image = render_items(win, items, scale, background)
        if image.isNull() or not write_image(image, path):
            raise ToolError("Could not save the PNG")
        return {"scope": scope, "path": path,
                "width": image.width(), "height": image.height(),
                "scale": round(scale, 3), "bytes": os.path.getsize(path)}

    # ---- export document pages -------------------------------------------
    def t_export_pages(a):
        if not export_available:
            raise ToolError("Export subsystem is unavailable in this build")
        item = tools._item(a["uid"])
        if getattr(item, "kind", None) != "document":
            raise ToolError("'%s' is not a document" % getattr(item, "kind", "?"))
        folder = sandbox_path(win, a.get("folder"), key="folder", folder=True)
        if os.path.isfile(folder):
            raise ToolError("'%s' is a file - give a folder" % folder)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as ex:
            raise ToolError("Cannot create the folder: %s" % ex)
        scale = float(a.get("scale", 2.0))
        if not (0.5 <= scale <= 4):
            raise ToolError("'scale' must be between 0.5 and 4")
        total = max(1, item.page_count())
        count = min(total, max(1, min(200, int(a.get("max_pages", 50)))))
        base = base_name(item)
        files, failed = [], 0
        for page in range(count):
            image = document_page_image(item, page, scale)
            target = unique_path(os.path.join(folder, "%s_page%03d.png" % (base, page + 1)))
            if image.isNull() or not write_image(image, target):
                failed += 1
                continue
            files.append(os.path.basename(target))
        return {"folder": folder, "exported": len(files), "failed": failed,
                "pages_total": total, "files": files[:12]}

    # ---- import image from the web -----------------------------------------
    def download_once(url):
        """One DownloadThread run; returns {'path':.., 'error':..} or {}."""
        holder = {}
        loop = QEventLoop()
        thread = DownloadThread(url)
        _DOWNLOADS.append(thread)

        def done(path, error):
            holder["path"], holder["error"] = path, error
            loop.quit()

        def cleanup():
            if thread in _DOWNLOADS:
                _DOWNLOADS.remove(thread)

        thread.completed.connect(done)
        thread.finished.connect(cleanup)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        QTimer.singleShot(60000, loop.quit)   # safety net, keeps the GUI alive
        loop.exec()
        return holder

    def t_import_url(a):
        if not web_available:
            raise ToolError("Web import is unavailable in this build")
        url = str(a.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ToolError("Only http(s) URLs are supported")
        url = _clean_url(url)
        host = _import_host(url)
        pos = (QPointF(float(a["x"]), float(a["y"]))
               if "x" in a and "y" in a else win.view_center())

        # Politeness: space out requests to one host and retry "server busy"
        # answers (429/503) automatically, so transient limits stay invisible.
        for attempt in range(len(IMPORT_RETRY_WAITS) + 1):
            wait = _host_wait(host)
            if wait > 0:
                drain(int(wait * 1000) + 20)   # GUI stays alive while waiting
            _LAST_IMPORT[host] = time.monotonic()
            holder = download_once(url)
            if "path" not in holder:
                raise ToolError("Download timed out (60 s)")
            error = holder.get("error")
            if not error:
                break
            if _busy_error(error) and attempt < len(IMPORT_RETRY_WAITS):
                drain(int(IMPORT_RETRY_WAITS[attempt] * 1000))
                continue
            raise ToolError("Download failed: %s" % error)

        # Relocate the download into the sandbox so the agent never keeps
        # files outside its own folder.
        dest_dir = os.path.join(sandbox_root(win), "downloads")
        os.makedirs(dest_dir, exist_ok=True)
        name = os.path.basename(holder["path"])
        dest = os.path.join(dest_dir, name)
        if os.path.exists(dest):
            base, ext = os.path.splitext(name)
            dest = os.path.join(dest_dir, "%s_%d%s" % (base, int(time.time()), ext))
        try:
            shutil.move(holder["path"], dest)
        except OSError as ex:
            raise ToolError("Could not move the download into the agent folder: %s" % ex)
        item = win.add_file(dest, pos)
        if item is None:
            raise ToolError("The downloaded file is not supported media")
        tools._select([item])
        return {"item": tools._brief(item), "url": url, "saved_to": dest}

    # ---- registry (batch 4) -------------------------------------------
    tools._add("board_save_project",
               "Save the project to its current .refsyn file. For the first save "
               "or save-as provide 'path' INSIDE the agent folder '%s' (other "
               "locations are refused). An existing different file is kept unless "
               "overwrite:true. No dialogs are shown." % sb,
               S({"path": {"type": "string", "description": "Target .refsyn file inside the agent folder"},
                  "overwrite": {"type": "boolean"}}),
               t_save_project)

    tools._add("board_export_png",
               "Export the whole board or the current selection to a PNG file "
               "INSIDE the agent folder '%s' (same renderer as Ctrl+Shift+E). "
               "'scale' 0.25-8, 'background' hex or 'transparent'. Existing files "
               "need overwrite:true." % sb,
               S({"scope": {"type": "string", "enum": ["board", "selection"]},
                  "path": {"type": "string"},
                  "scale": {"type": "number"},
                  "background": {"type": "string"},
                  "overwrite": {"type": "boolean"}},
                 ["path"]),
               t_export_png)

    tools._add("board_export_pages",
               "Render pages of a PDF/text document to PNG files in 'folder' "
               "INSIDE the agent folder '%s'. max_pages limits the run (default "
               "50, hard cap 200). File names are made unique, nothing is "
               "overwritten." % sb,
               S({"uid": uid,
                  "folder": {"type": "string"},
                  "scale": {"type": "number"},
                  "max_pages": {"type": "integer"}},
                 ["uid", "folder"]),
               t_export_pages)

    tools._add("board_import_url",
               "Download an image from an http(s) URL and place it on the board. "
               "A page URL works too - its preview image (og:image) is used. "
               "The file is stored in <agent folder>/downloads. Requests to the "
               "same host are spaced out automatically and 'too many requests' "
               "answers are retried - call it once per image and be patient.",
               S({"url": {"type": "string"},
                  "x": {"type": "number"}, "y": {"type": "number"}},
                 ["url"]),
               t_import_url, mutates=True)


def register_settings_tools(tools):
    """Batch 5: read / change tool settings. Validated, saved, UI synced."""
    win = tools.win
    S = tools._schema

    BOOL_KEYS = ("snap_enabled", "snap_grid", "grid_visible")
    DEFAULTS = {"fill_tolerance": 24, "grid_size": 50, "snap_tolerance": 10,
                "snap_enabled": True, "snap_grid": False, "grid_visible": False}

    def current():
        data = {key: win.settings.get(key, default)
                for key, default in DEFAULTS.items()}
        data["draw_width"] = round(float(getattr(win, "draw_width", 10)), 2)
        data["draw_color"] = getattr(win, "draw_color", "#ff5252")
        return data

    def sync_ui():
        act = getattr(win, "act", None) or {}
        for action_key, setting in (("snapping", "snap_enabled"),
                                    ("grid_snap", "snap_grid"),
                                    ("grid_visible", "grid_visible")):
            action = act.get(action_key)
            if action is not None:
                action.setChecked(bool(win.settings.get(setting, DEFAULTS[setting])))
        win.view.viewport().update()

    def t_settings(a):
        # Validate everything first: one bad value must not apply the rest.
        updates = {}
        if "fill_tolerance" in a:
            value = int(a["fill_tolerance"])
            if not (0 <= value <= 255):
                raise ToolError("'fill_tolerance' must be between 0 and 255")
            updates["fill_tolerance"] = value
        if "draw_width" in a:
            value = float(a["draw_width"])
            if not (0.5 <= value <= 200):
                raise ToolError("'draw_width' must be between 0.5 and 200")
            updates["draw_width"] = value
        if "grid_size" in a:
            value = int(a["grid_size"])
            if not (5 <= value <= 500):
                raise ToolError("'grid_size' must be between 5 and 500")
            updates["grid_size"] = value
        if "snap_tolerance" in a:
            value = float(a["snap_tolerance"])
            if not (1 <= value <= 100):
                raise ToolError("'snap_tolerance' must be between 1 and 100")
            updates["snap_tolerance"] = value
        for key in BOOL_KEYS:
            if key in a:
                updates[key] = bool(a[key])
        if updates:
            for key, value in updates.items():
                if key == "draw_width":
                    win.draw_width = value
                win.settings[key] = value
            save_settings(win.settings)
            sync_ui()
        return {"settings": current(), "changed": updates}

    tools._add("board_settings",
               "Read or change tool settings. No arguments = read. Keys: "
               "fill_tolerance 0-255, draw_width 0.5-200 (marker thickness), "
               "grid_size 5-500, snap_tolerance 1-100 px, snap_enabled / "
               "snap_grid / grid_visible true/false. Changes are validated, "
               "saved immediately and the menu checkboxes stay in sync. "
               "Use board_set_draw_color to change the active color.",
               S({"fill_tolerance": {"type": "integer"},
                  "draw_width": {"type": "number"},
                  "grid_size": {"type": "integer"},
                  "snap_tolerance": {"type": "number"},
                  "snap_enabled": {"type": "boolean"},
                  "snap_grid": {"type": "boolean"},
                  "grid_visible": {"type": "boolean"}}),
               t_settings)


# ------------------------------------------------------------------ self-test
def _selftest():
    """python -m refsynapse.ai.board_extra_tools  (opens the real window)"""
    import base64
    import sys
    import tempfile
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice
    from PySide6.QtWidgets import QApplication
    from ..main_window import MainWindow
    from .board_tools import BoardTools

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    tools = BoardTools(win)
    print("Registered tools:", len(tools.names()))
    print("  has board_web_search_images ->", "board_web_search_images" in tools.names())
    sb = sandbox_root(win)
    print("agent sandbox ->", sb)

    def run():
        # --- source image INSIDE the sandbox (read guard allows it) -----
        image = QImage(800, 600, QImage.Format.Format_RGB32)
        painter = QPainter(image)
        painter.fillRect(image.rect(), QColor("#3a6ea5"))
        painter.fillRect(100, 80, 600, 440, QColor("#e07a5f"))
        painter.end()
        src = os.path.join(sb, "selftest_source.png")
        image.save(src)
        added = tools.call("board_add_image", {"path": src, "x": 0, "y": 0})
        print("add_image (inside) ->", added.get("ok"), added.get("error", ""))
        if not added.get("ok"):
            print("DONE - close the window to exit")
            return
        iuid = added["item"]["uid"]

        # --- READ guard: a path outside the sandbox must be refused ------
        outside_img = os.path.join(tempfile.gettempdir(), "refsynapse_outside_source.png")
        image.save(outside_img)
        bad_read = tools.call("board_add_image", {"path": outside_img})
        print("read outside ->", bad_read.get("ok"), "(expected False)",
              bad_read.get("error", ""))

        # --- base64 upload still works (app-generated path) --------------
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buf, "PNG")
        buf.close()
        ok64 = tools.call("board_add_image",
                          {"image_base64": base64.b64encode(bytes(ba)).decode(),
                           "x": 900, "y": 600})
        print("add base64 ->", ok64.get("ok"), ok64.get("error", ""))

        # --- batch 2 regression -------------------------------------
        pick = tools.call("board_pick_color", {"x": 400, "y": 300})
        print("pick center ->", pick.get("hex"), pick.get("error", ""))
        fill = tools.call("board_fill", {"x": 50, "y": 50,
                                         "color": "#2ecc71", "tolerance": 10})
        print("fill image ->", fill.get("mode"), fill.get("error", ""))
        frame = tools.call("board_add_frame", {"x": 900, "y": 0,
                                               "width": 300, "height": 200})
        fuid = frame.get("item", {}).get("uid")
        vfill = tools.call("board_fill", {"uid": fuid, "x": 900, "y": 0,
                                          "color": "#ff8800"})
        print("fill frame ->", vfill.get("mode"), vfill.get("error", ""))
        stroke = tools.call("board_draw_stroke",
                            {"points": [[0, 700], [200, 760], [400, 700]],
                             "color": "#ffffff", "width": 12})
        print("stroke ->", stroke.get("points"), stroke.get("error", ""))

        # --- batch 3 regression ---------------------------------------
        f1 = tools.call("board_flip", {"uid": iuid})
        print("flip toggle ->", f1.get("flipped"), f1.get("error", ""))
        tools.call("board_flip", {"uid": iuid, "flipped": False})

        txt = os.path.join(tempfile.gettempdir(), "refsynapse_agent_doc_test.txt")
        with open(txt, "w", encoding="utf-8") as stream:
            for i in range(300):
                stream.write("Line %d - the quick brown fox jumps over the lazy dog\n" % i)
        doc = win.add_file(txt, QPointF(1400, 0))
        print("document ->", "ok" if doc is not None else "skipped")

        # --- batch 4: everything now lives in the sandbox -----------------
        proj = os.path.join(sb, "selftest.refsyn")
        if os.path.exists(proj):
            os.remove(proj)
        s1 = tools.call("board_save_project", {"path": proj})
        print("save project ->", s1.get("ok"), s1.get("saved"),
              s1.get("bytes"), s1.get("error", ""))
        out = os.path.join(sb, "selftest_board.png")
        if os.path.exists(out):
            os.remove(out)
        b1 = tools.call("board_export_png", {"scope": "board", "path": out})
        print("export board ->", b1.get("ok"), b1.get("width"), "x",
              b1.get("height"), b1.get("error", ""))
        if doc is not None:
            p1 = tools.call("board_export_pages",
                            {"uid": doc.uid,
                             "folder": os.path.join(sb, "selftest_pages"),
                             "max_pages": 2})
            print("export pages ->", p1.get("exported"), p1.get("error", ""))

        # --- WRITE guard: outside paths must be refused ------------------
        outside = os.path.join(tempfile.gettempdir(), "refsynapse_outside_test.png")
        if os.path.exists(outside):
            os.remove(outside)
        bad = tools.call("board_export_png", {"scope": "board", "path": outside})
        print("outside sandbox ->", bad.get("ok"), "(expected False)",
              bad.get("error", ""))
        print("outside file created ->", os.path.exists(outside), "(expected False)")
        bad2 = tools.call("board_export_item",
                          {"uid": iuid, "path": os.path.join(sb, "..", "escape.png")})
        print("dot-dot escape ->", bad2.get("ok"), "(expected False)",
              bad2.get("error", ""))
        u1 = tools.call("board_import_url", {"url": "ftp://example.com/x.png"})
        print("import bad scheme ->", u1.get("ok"), u1.get("error", ""))

        # --- polite import helpers (offline) ------------------------------
        cleaned = _clean_url("https://upload.wikimedia.org/x/a.jpg"
                             "?utm_source=commons&utm_content=original")
        print("utm stripped ->", cleaned == "https://upload.wikimedia.org/x/a.jpg",
              "(expected True)")
        kept_url = _clean_url("https://x.example/a.jpg?width=10&utm_source=z")
        print("other params kept ->", kept_url == "https://x.example/a.jpg?width=10",
              "(expected True)")
        print("busy 429 ->", _busy_error("HTTP Error 429: Too many requests"),
              "(expected True)")
        print("busy 404 ->", _busy_error("HTTP Error 404: Not Found"),
              "(expected False)")
        _LAST_IMPORT["selftest.example"] = time.monotonic()
        print("throttle wait ->", _host_wait("selftest.example") > 1.0,
              "(expected True)")
        print("throttle fresh ->", _host_wait("other.example") == 0.0,
              "(expected True)")
        _LAST_IMPORT.clear()

        # --- batch 5: settings (originals are restored at the end) --------
        from ..config import load_settings
        originals = dict(win.settings)
        old_width = getattr(win, "draw_width", 10)
        s_set = tools.call("board_settings", {"fill_tolerance": 40,
                                              "draw_width": 14,
                                              "grid_visible": True,
                                              "grid_size": 80})
        print("settings set ->", s_set.get("changed"), s_set.get("error", ""))
        persisted = load_settings()
        print("persisted ->", persisted.get("fill_tolerance"),
              persisted.get("grid_size"))
        bad3 = tools.call("board_settings", {"fill_tolerance": 999})
        print("settings bad ->", bad3.get("ok"), "(expected False)",
              bad3.get("error", ""))
        defaults = {"fill_tolerance": 24, "grid_size": 50, "snap_tolerance": 10,
                    "snap_enabled": True, "snap_grid": False, "grid_visible": False}
        restore = {k: originals.get(k, d) for k, d in defaults.items()}
        restore["draw_width"] = old_width
        tools.call("board_settings", restore)
        after = tools.call("board_settings", {}).get("settings", {})
        print("settings restored ->", after.get("fill_tolerance"),
              after.get("draw_width"))

        print("undo ->", tools.call("board_undo", {}))
        print("DONE - close the window to exit")

    QTimer.singleShot(800, run)
    sys.exit(app.exec())


if __name__ == "__main__":
    _selftest()