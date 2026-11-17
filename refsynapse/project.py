"""Portable project packages, background writing and crash recovery files."""
import json
import math
import os
import shutil
import tempfile
import uuid
import zipfile
from PySide6.QtCore import QThread, Signal
from .config import APP_DIR, walk_item_data


def recovery_file():
    return os.path.join(APP_DIR, "data", "recovery.refsyn")


def prepare_package(data):
    """Deduplicate available media and preserve missing references as placeholders."""
    media = {}
    missing = []
    for item in walk_item_data(data.get("items", [])):
        if item.get("kind") not in ("image", "video", "document", "model3d"):
            continue
        kind = item["kind"]
        source = os.path.abspath(item.get("path", ""))
        if not os.path.isfile(source):
            item["kind"] = "missing"
            item["media_kind"] = kind
            missing.append(source)
            continue
        key = os.path.normcase(os.path.normpath(source))
        if key not in media:
            archive_name = f"media/{len(media) + 1:04d}{os.path.splitext(source)[1].lower()}"
            media[key] = (archive_name, source)
        item["path"] = media[key][0]
    return media, missing


def write_package(target, data, media):
    target = os.fspath(target)
    temporary = target + ".tmp-" + uuid.uuid4().hex[:8]
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED,
                             allowZip64=True) as archive:
            archive.writestr("project.json", json.dumps(data, ensure_ascii=False, indent=2))
            for archive_name, source in media.values():
                archive.write(source, archive_name, compress_type=zipfile.ZIP_STORED)
        if target != recovery_file() and os.path.isfile(target):
            shutil.copy2(target, target + ".bak")
        os.replace(temporary, target)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


class ProjectSaveThread(QThread):
    completed = Signal(str, str)  # target, error (empty on success)

    def __init__(self, target, data, media, parent=None):
        super().__init__(parent)
        self.target = target
        self.data = data
        self.media = media

    def run(self):
        try:
            write_package(self.target, self.data, self.media)
            self.completed.emit(self.target, "")
        except Exception as exc:
            self.completed.emit(self.target, str(exc))


def validate_project(data):
    """Reject malformed numeric/object data before replacing the current scene."""
    allowed = {"image", "video", "document", "model3d", "group", "text", "frame", "stroke", "missing"}
    seen = set()

    def number(value, name):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"Invalid numeric project field: {name}")

    def visit(item):
        if not isinstance(item, dict) or item.get("kind") not in allowed:
            raise ValueError("Unknown or invalid object in project")
        uid = item.get("id")
        if uid is not None:
            if not isinstance(uid, str) or uid in seen:
                raise ValueError("Duplicate or invalid object ID in project")
            seen.add(uid)
        for key in ("x", "y", "z", "scale", "rotation", "width", "height", "line_width", "page",
                    "position", "volume", "poster_position", "yaw", "pitch", "model_zoom",
                    "pan_x", "pan_y"):
            if key in item:
                number(item[key], key)
        if item.get("kind") == "group":
            if not isinstance(item.get("children", []), list):
                raise ValueError("Invalid group children")
            for child in item.get("children", []):
                visit(child)
        if item.get("kind") == "stroke":
            for point in item.get("points", []):
                if not isinstance(point, list) or len(point) != 2:
                    raise ValueError("Invalid stroke point")
                number(point[0], "stroke.x")
                number(point[1], "stroke.y")
        if "crop" in item:
            crop = item["crop"]
            if not isinstance(crop, list) or len(crop) != 4:
                raise ValueError("Invalid image crop")
            for value in crop:
                number(value, "crop")

    for top in data.get("items", []):
        visit(top)


def load_package(path):
    """Read old JSON projects and new self-contained ZIP projects.

    Returns (data, temporary_media_directory). Invalid archives raise an exception;
    absent media references are converted to placeholders without discarding layout.
    """
    path = os.fspath(path)
    media_dir = None
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                members = set(archive.namelist())
                if "project.json" not in members:
                    raise ValueError("The project package has no project.json.")
                with archive.open("project.json") as stream:
                    data = json.load(stream)
                if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
                    raise ValueError("This is not a valid RefSynapse project.")
                media_dir = tempfile.TemporaryDirectory(prefix="RefSynapse-media-",
                                                        ignore_cleanup_errors=True)
                count = 0
                for item in walk_item_data(data.get("items", [])):
                    if item.get("kind") not in ("image", "video", "document", "model3d"):
                        continue
                    member = item.get("path", "")
                    if (not member.startswith("media/") or member.endswith("/") or
                            ".." in member.split("/")):
                        raise ValueError("Unsafe embedded project media path: " + member)
                    if member not in members:
                        original_kind = item["kind"]
                        item["kind"] = "missing"
                        item["media_kind"] = original_kind
                        continue
                    count += 1
                    target = os.path.join(media_dir.name,
                                          f"media_{count:04d}{os.path.splitext(member)[1]}")
                    with archive.open(member) as source, open(target, "wb") as destination:
                        shutil.copyfileobj(source, destination)
                    item["path"] = target
        else:
            with open(path, encoding="utf-8") as stream:
                data = json.load(stream)
        if isinstance(data, list):
            data = {"items": data}
        if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
            raise ValueError("This is not a valid RefSynapse project.")
        validate_project(data)
        return data, media_dir
    except Exception:
        if media_dir is not None:
            media_dir.cleanup()
        raise
