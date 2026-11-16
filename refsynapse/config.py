import sys, os, json, copy, uuid
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QGraphicsItem
APP_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                          else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if APP_DIR not in sys.path:          # needed for the embeddable Python (._pth file)
    sys.path.insert(0, APP_DIR)
APP_NAME = "RefSynapse"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
PROJECT_EXT = ".refsyn"
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
ACCENT = "#4fc3f7"
ICON_PATH = os.path.join(APP_DIR, "refsynapse.ico")
DEFAULT_DRAW_COLOR = "#ff5252"
DEFAULT_TEXT_COLOR = "#ffffff"
GAP = 10
HANDLE = 4.5                    # resize handle size in screen pixels
MIN_ZOOM, MAX_ZOOM = 0.01, 64.0
FLAGS = QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable
HINT = ("Drop images, videos, documents or 3D models here   \u2022   Ctrl+V to paste\n"
        "Right-click for menu   \u2022   F1 for help   \u2022   Ctrl+, for settings")


def new_id():
    return uuid.uuid4().hex[:8]


def ev(x):
    """Enum -> int (works with any PySide6 enum flavour)."""
    return getattr(x, "value", x)


def physical_key(e):
    """Layout-independent key name (notably for Russian Windows keyboards)."""
    vk = e.nativeVirtualKey()
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    special = {0xBB: "=", 0xBD: "-", 0xDB: "[", 0xDD: "]",
               0xBC: ",", 0xBE: "."}
    if vk in special:
        return special[vk]
    key = ev(e.key())
    if 32 <= key <= 126:
        return chr(key).upper()
    return ""


# =====================================================================
#  Navigation and transform presets
# =====================================================================
BUTTONS = {"left": Qt.LeftButton, "middle": Qt.MiddleButton, "right": Qt.RightButton}
BUTTON_LABEL = {"left": "LMB", "middle": "MMB", "right": "RMB"}
HOLD_KEYS = {ev(Qt.Key_Space): "space", ev(Qt.Key_Z): "z"}
MOD_ORDER = ("ctrl", "shift", "alt")
NAV_ACTIONS = [("pan", "Pan"), ("zoom", "Zoom (drag)"), ("move_window", "Move window")]
TRANSFORM_KEYS = {
    "blender": ("Blender (G / R / S)", {"move": "G", "rotate": "R", "scale": "S"}),
    "max": ("3ds Max / Maya (W / E / R)", {"move": "W", "rotate": "E", "scale": "R"}),
}


def button_name(b):
    for k, v in BUTTONS.items():
        if b == v:
            return k
    return None


def mods_of(m):
    return [n for n, f in (("ctrl", Qt.ControlModifier), ("shift", Qt.ShiftModifier),
                           ("alt", Qt.AltModifier)) if bool(m & f)]


def B(button, *mods, key=""):
    return {"button": button, "mods": [m for m in MOD_ORDER if m in mods], "key": key}


def binding_text(b):
    if not b:
        return "(none)"
    parts = [m.capitalize() for m in MOD_ORDER if m in b.get("mods", [])]
    if b.get("key"):
        parts.append(b["key"].capitalize())
    parts.append(BUTTON_LABEL.get(b.get("button"), "?"))
    return " + ".join(parts)


NAV_BASE = {"wheel": "zoom", "zoom_step": 15, "invert_wheel": False,
            "zoom_drag_speed": 1.0, "transform_keys": "blender"}

PRESETS = {
    "RefSynapse (PureRef-like)": dict(bindings={
        "pan": [B("middle"), B("right")],
        "zoom": [B("middle", "ctrl"), B("right", "ctrl")],
        "move_window": [B("left", "alt"), None]}),
    "Photoshop": dict(wheel="scroll", bindings={
        "pan": [B("left", key="space"), B("middle")],
        "zoom": [B("left", "ctrl", key="space"), B("left", key="z")],
        "move_window": [B("left", "alt"), None]}),
    "3ds Max": dict(transform_keys="max", bindings={
        "pan": [B("middle"), None],
        "zoom": [B("middle", "ctrl", "alt"), None],
        "move_window": [B("left", "alt"), None]}),
    "Blender": dict(bindings={
        "pan": [B("middle"), B("middle", "shift")],
        "zoom": [B("middle", "ctrl"), None],
        "move_window": [B("left", "alt"), None]}),
    "Maya": dict(transform_keys="max", bindings={
        "pan": [B("middle", "alt"), B("middle")],
        "zoom": [B("right", "alt"), None],
        "move_window": [B("left", "ctrl", "alt"), None]}),
}
DEFAULT_PRESET = "RefSynapse (PureRef-like)"


def preset_nav(name):
    nav = copy.deepcopy(NAV_BASE)
    nav.update(copy.deepcopy(PRESETS[name]))
    nav["preset"] = name
    return nav


def load_nav(d):
    if not isinstance(d, dict):
        return preset_nav(DEFAULT_PRESET)
    name = d.get("preset")
    if name not in PRESETS and name != "Custom":     # preset from an older version
        return preset_nav(DEFAULT_PRESET)
    nav = preset_nav(name if name in PRESETS else DEFAULT_PRESET)
    for k in NAV_BASE:
        if k in d:
            nav[k] = d[k]
    if nav["transform_keys"] not in TRANSFORM_KEYS:
        nav["transform_keys"] = "blender"
    b = d.get("bindings")
    if isinstance(b, dict):
        for k, _ in NAV_ACTIONS:
            if isinstance(b.get(k), list):
                nav["bindings"][k] = (b[k] + [None, None])[:2]
    nav["preset"] = name
    return nav


def nav_match(nav, button, mods, key):
    for action, _ in NAV_ACTIONS:
        for b in nav["bindings"].get(action, []):
            if (b and b.get("button") == button and sorted(b.get("mods", [])) == sorted(mods)
                    and (b.get("key") or "") == (key or "")):
                return action
    return None


def nav_is_hold(nav, key, mods):
    """Should this key press be captured as a 'hold' key (Space / Z)?"""
    if not key:
        return False
    for action, _ in NAV_ACTIONS:
        for b in nav["bindings"].get(action, []):
            if b and b.get("key") == key and (not mods or sorted(b.get("mods", [])) == sorted(mods)):
                return True
    return False


def model_nav_preset(nav):
    """DCC preset used inside a model viewport, including Custom settings."""
    name = nav.get("preset")
    if name in ("Blender", "3ds Max", "Maya"):
        return name
    if name == "Custom":
        return "3ds Max" if nav.get("transform_keys") == "max" else "Blender"
    return "RefSynapse"


def model_gesture(nav, button, mods):
    """Return orbit / pan / zoom for a model viewport, or None for the canvas."""
    preset = model_nav_preset(nav)
    mods = sorted(mods)
    gestures = {
        "Blender": {("middle", ()): "orbit",
                    ("middle", ("shift",)): "pan",
                    ("middle", ("ctrl",)): "zoom"},
        "3ds Max": {("middle", ("alt",)): "orbit",
                    ("middle", ()): "pan",
                    ("middle", ("alt", "ctrl")): "zoom"},
        "Maya": {("left", ("alt",)): "orbit",
                 ("middle", ("alt",)): "pan",
                 ("right", ("alt",)): "zoom"},
        "RefSynapse": {("left", ("alt",)): "orbit",
                       ("left", ("alt", "shift")): "pan",
                       ("left", ("alt", "ctrl")): "zoom"},
    }
    return gestures[preset].get((button, tuple(mods)))


def model_navigation_hint(nav):
    return {
        "Blender": "MMB orbit  •  Shift+MMB pan  •  Wheel zoom",
        "3ds Max": "Alt+MMB orbit  •  MMB pan  •  Wheel zoom",
        "Maya": "Alt+LMB orbit  •  Alt+MMB pan  •  Alt+RMB zoom",
        "RefSynapse": "Alt+LMB orbit  •  Alt+Shift+LMB pan  •  Wheel zoom",
    }[model_nav_preset(nav)]


def nav_conflicts(nav):
    seen, out = {}, []
    for action, label in NAV_ACTIONS:
        for b in nav["bindings"].get(action, []):
            if not b:
                continue
            sig = (b["button"], tuple(sorted(b.get("mods", []))), b.get("key", ""))
            if sig in seen and seen[sig] != label:
                out.append(f"{binding_text(b)} is used by {seen[sig]} and {label}")
            seen.setdefault(sig, label)
    return out


# =====================================================================
#  Portable settings / paths
# =====================================================================
def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(s):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def to_portable(p):
    try:
        return os.path.relpath(p, APP_DIR)
    except ValueError:
        return os.path.abspath(p)


def from_portable(p):
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(APP_DIR, p))


def rel(p, base):
    if not base:
        return os.path.abspath(p)
    try:
        return os.path.relpath(p, base)
    except ValueError:
        return os.path.abspath(p)


def walk_item_data(items):
    """Yield item dictionaries recursively, including grouped media."""
    for d in items:
        if not isinstance(d, dict):
            continue
        yield d
        if d.get("kind") == "group":
            yield from walk_item_data(d.get("children", []))


def mime_image(md):
    data = md.imageData()
    if isinstance(data, QImage):
        return data
    if isinstance(data, QPixmap):
        return data.toImage()
    return QImage()
