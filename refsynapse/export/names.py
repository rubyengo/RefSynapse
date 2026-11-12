"""File naming helpers shared by every exporter."""
import os
import re

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_NAME = 120


def safe_filename(name, fallback="item"):
    """Strip characters Windows refuses to store and keep the name readable."""
    name = _INVALID.sub("_", str(name or "")).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if len(name) > MAX_NAME:
        name = name[:MAX_NAME].rstrip()
    return name or fallback


def base_name(item):
    path = getattr(item, "path", "") or ""
    base = os.path.splitext(os.path.basename(path))[0]
    if base:
        return safe_filename(base)
    kind = getattr(item, "kind", "item")
    title = getattr(item, "title", "") if kind == "group" else ""
    return safe_filename(title or kind)


def crop_suffix(item):
    return "_crop" if getattr(item, "crop", None) is not None else ""


def default_name(item, suffix="", ext=".png"):
    return f"{base_name(item)}{suffix}{ext}"


def unique_path(path):
    """Never overwrite an existing export: photo.png -> photo_2.png -> ..."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    index = 2
    while True:
        candidate = f"{stem}_{index}{ext}"
        if not os.path.exists(candidate):
            return candidate
        index += 1