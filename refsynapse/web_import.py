"""Download images dropped or pasted from a web browser."""
import base64
import binascii
import os
import re
import uuid
import urllib.request
from urllib.parse import urljoin, urlparse

from PySide6.QtCore import QBuffer, QThread, Signal
from PySide6.QtGui import QImage, QImageReader

from .config import APP_DIR

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_BYTES = 40 * 1024 * 1024
TIMEOUT = 20

IMAGE_TYPES = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif", "image/bmp": ".bmp",
    "image/svg+xml": ".svg", "image/avif": ".avif", "image/tiff": ".tif",
}
KNOWN_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
             ".svg", ".avif", ".tif", ".tiff")

_OG = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["'](?:og:image(?::secure_url)?|twitter:image)["'][^>]*>""",
    re.I)
_IMG = re.compile(r"""<img[^>]+src\s*=\s*["']([^"']+)["']""", re.I)
_CONTENT = re.compile(r"""content\s*=\s*["']([^"']+)["']""", re.I)


def is_remote(url):
    return url.scheme() in ("http", "https") and bool(url.host())


def web_dir():
    folder = os.path.join(APP_DIR, "data", "web")
    os.makedirs(folder, exist_ok=True)
    return folder


def _read(response):
    chunks, total = [], 0
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_BYTES:
            raise ValueError("the file is larger than 40 MB")
        chunks.append(chunk)
    return b"".join(chunks)


def _fetch(url):
    parts = urlparse(url)
    request = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        "Referer": f"{parts.scheme}://{parts.netloc}/",
    })
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return _read(response), ctype, response.geturl()


def _extension(url, ctype):
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext in KNOWN_EXT:
        return ".jpg" if ext == ".jpeg" else ext
    return IMAGE_TYPES.get(ctype, ".png")


def _looks_like_image(data):
    """SVG starts with '<' too, so content sniffing must run before the HTML branch."""
    if not data:
        return False
    buffer = QBuffer()
    buffer.setData(data)
    buffer.open(QBuffer.ReadOnly)
    reader = QImageReader()
    reader.setDevice(buffer)
    if reader.canRead():
        return True
    # Some plugins refuse a sequential probe but decode the bytes just fine.
    return not QImage.fromData(data).isNull()


def _is_html(data, ctype):
    if ctype in ("text/html", "application/xhtml+xml"):
        return True
    head = data[:400].lstrip().lower()
    return (head.startswith((b"<!doctype html", b"<html", b"<?xml"))
            and b"<svg" not in head)


def _image_from_html(html, base):
    """A page URL was dropped - take its preview image."""
    text = html.decode("utf-8", "ignore")
    for tag in _OG.findall(text):
        found = _CONTENT.search(tag)
        if found:
            candidate = urljoin(base, found.group(1).strip())
            if candidate.startswith(("http://", "https://")):
                return candidate
    for src in _IMG.findall(text):
        candidate = urljoin(base, src.strip())
        if candidate.startswith(("http://", "https://")):
            return candidate
    return None


def save_data_uri(text):
    """data:image/png;base64,... - some sites put the picture itself on the clipboard."""
    if not text.lower().startswith("data:image/"):
        return None
    try:
        header, payload = text.split(",", 1)
    except ValueError:
        return None
    meta = header[5:]
    ctype = meta.split(";")[0].strip().lower()
    if "base64" not in meta.lower():
        return None
    try:
        data = base64.b64decode(re.sub(r"\s+", "", payload), validate=False)
    except (binascii.Error, ValueError):
        return None
    if not _looks_like_image(data):
        return None
    path = os.path.join(web_dir(), uuid.uuid4().hex[:12] + IMAGE_TYPES.get(ctype, ".png"))
    try:
        with open(path, "wb") as stream:
            stream.write(data)
    except OSError:
        return None
    return path


class DownloadThread(QThread):
    """Fetch one URL off the GUI thread; emits (saved_path, error_text)."""
    completed = Signal(str, str)

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self.url = url

    def run(self):
        try:
            data, ctype, final = _fetch(self.url)
            if not _looks_like_image(data):
                if not _is_html(data, ctype):
                    self.completed.emit("", "the link is not an image")
                    return
                candidate = _image_from_html(data, final)
                if not candidate:
                    self.completed.emit("", "no image found on that page")
                    return
                data, ctype, final = _fetch(candidate)
                if not _looks_like_image(data):
                    self.completed.emit("", "the link is not an image")
                    return
            name = uuid.uuid4().hex[:12] + _extension(final, ctype)
            path = os.path.join(web_dir(), name)
            with open(path, "wb") as stream:
                stream.write(data)
            self.completed.emit(path, "")
        except Exception as ex:
            self.completed.emit("", str(ex) or "download failed")