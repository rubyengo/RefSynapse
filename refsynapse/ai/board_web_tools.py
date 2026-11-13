"""Web image search for the RefSynapse agent (batch 6).

board_web_search_images - search the web for images, build a NUMBERED contact
sheet and show it to the model. The model looks at the sheet, picks the best
candidates and downloads them full-size with the existing board_import_url
(which enforces the agent sandbox). This tool itself writes NOTHING to disk.

Backends (keyless, best-effort):
  * Pinterest internal search API (+ HTML fallback) - used when site is
    pinterest.com; returns i.pinimg.com pictures without a login;
  * DuckDuckGo image API;
  * Bing HTML / Bing async (known to feed bots decoy images - gated);
  * Openverse public JSON API (general queries only).
Quality gates before results are accepted:
  * site filter  - when site='pinterest.com' only pinterest/pinimg hosts pass;
  * relevance    - for Latin queries without a site, at least one title must
                   share a meaningful word with the query (Cyrillic queries
                   skip this check - the model judges the sheet visually).
Results from passing backends are merged and deduplicated.

The model is explicitly told never to invent image URLs.
"""

import base64
import html as html_mod
import http.cookiejar
import json
import re
import sys
import time
import urllib.parse
import urllib.request

from PySide6.QtCore import QThread, Signal, QEventLoop, QTimer, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QFont

from .board_tools import ToolError

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_HTML = 4 * 1024 * 1024
MAX_THUMB = 1_500_000
THUMB_DEADLINE = 15.0          # seconds spent fetching previews in the worker
SEARCH_TIMEOUT_MS = 90_000

SHEET_COLS = 4
SHEET_CELL = 200
SHEET_LABEL = 26

NO_INVENT = (" Do NOT guess or invent image URLs - they will 404. "
             "Try another query, or ask the user for direct links.")

# CDN aliases: a site's pictures often live on a different host.
SITE_ALIASES = {
    "pinterest.com": ("pinimg.com",),
    "instagram.com": ("cdninstagram.com",),
    "flickr.com": ("staticflickr.com",),
    "deviantart.com": ("deviantart.net",),
    "tumblr.com": ("media.tumblr.com",),
}

STOP_WORDS = {"site", "www", "http", "https", "image", "images", "photo",
              "photos", "picture", "pictures", "wallpaper", "wallpapers"}

# Keeps running QThread objects alive until they finish.
_SEARCHES = []


# ------------------------------------------------------------------ network
def _read_capped(response, cap):
    chunks, total = [], 0
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise ValueError("response too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _fetch_bytes(url, cap, timeout, referer=None, opener=None, extra=None):
    headers = {"User-Agent": UA, "Accept": "*/*",
               "Accept-Language": "en-US,en;q=0.9"}
    if referer:
        headers["Referer"] = referer
    if extra:
        headers.update(extra)
    request = urllib.request.Request(url, headers=headers)
    open_ = opener.open if opener is not None else urllib.request.urlopen
    with open_(request, timeout=timeout) as response:
        return _read_capped(response, cap)


# ------------------------------------------------------------- quality gates
def host_of(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def host_matches(host, site):
    """True if host belongs to the requested site (incl. its CDN aliases)."""
    site = site.lower().strip(".")
    host = host.lower().strip(".")
    roots = (site,) + SITE_ALIASES.get(site, ())
    for root in roots:
        if host == root or host.endswith("." + root):
            return True
    return False


def relevant(results, query):
    """Cheap decoy detector for Latin queries: some title must share a word.

    Cyrillic queries skip the check (titles are usually English); the model
    judges the contact sheet visually anyway. Empty titles also skip it.
    """
    lowered = query.lower()
    if re.search(r"[а-яё]", lowered):
        return True
    words = [w for w in re.findall(r"[a-z0-9]+", lowered)
             if len(w) >= 4 and w not in STOP_WORDS]
    if not words:
        return True
    titles = [str(r.get("title") or "").lower() for r in results]
    titles = [t for t in titles if t]
    if not titles:
        return True
    return any(word in title for title in titles for word in words)


# ------------------------------------------------------------------ backends
def _pins_from_html(text, count):
    """Fallback: pull i.pinimg.com picture URLs straight out of page HTML."""
    text = text.replace("\\/", "/")
    seen, urls = set(), []
    for pattern in (r'https://i\.pinimg\.com/originals/[^"\\\s<>]+?\.(?:jpg|jpeg|png|webp)',
                    r'https://i\.pinimg\.com/736x/[^"\\\s<>]+?\.(?:jpg|jpeg|png|webp)',
                    r'https://i\.pinimg\.com/[^"\\\s<>]+?\.(?:jpg|jpeg|png|webp)'):
        for match in re.finditer(pattern, text, re.I):
            url = match.group(0)
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)
    return [{"title": "", "image": u, "thumbnail": u, "w": 0, "h": 0}
            for u in urls[:count]]


def _pinterest_search(query, count):
    """Pinterest's own search resource (no login) + HTML scrape fallback."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        _fetch_bytes("https://www.pinterest.com/", MAX_HTML, 8, opener=opener)
    except Exception:
        pass                                   # cookies are a bonus, not a must
    options = {"options": {"query": query, "scope": "pins", "rs": "typed",
                           "page_size": max(25, count)}}
    search_url = "https://www.pinterest.com/search/pins/?q=" + urllib.parse.quote(query)
    api = ("https://www.pinterest.com/resource/BaseSearchResource/get/?"
           + urllib.parse.urlencode({"source_url": "/search/pins/?q=" + urllib.parse.quote(query),
                                     "data": json.dumps(options, separators=(",", ":"))}))
    extra = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
             "Referer": search_url}
    csrf = next((c.value for c in jar if c.name == "csrftoken"), "")
    if csrf:
        extra["X-CSRFToken"] = csrf
    try:
        raw = _fetch_bytes(api, MAX_HTML, 10, opener=opener, extra=extra)
        payload = json.loads(raw.decode("utf-8", "ignore"))
        resource = payload.get("resource_response") or {}
        rows = resource.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("results") or []
        results = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            images = row.get("images") or {}
            orig = images.get("orig") or images.get("736x") or {}
            url = orig.get("url") or ""
            if not url.startswith(("http://", "https://")):
                continue
            results.append({"title": str(row.get("grid_title") or row.get("title") or ""),
                            "image": url,
                            "thumbnail": (images.get("236x") or {}).get("url") or url,
                            "w": int(orig.get("width") or 0),
                            "h": int(orig.get("height") or 0)})
            if len(results) >= count:
                break
        if results:
            return results
    except Exception:
        pass                                   # fall through to the HTML scrape
    page = _fetch_bytes(search_url, MAX_HTML, 10, opener=opener,
                        extra={"Referer": "https://www.pinterest.com/"}).decode("utf-8", "ignore")
    return _pins_from_html(page, count)


def _ddg_once(query, count):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    page_url = "https://duckduckgo.com/?" + urllib.parse.urlencode({"q": query})
    page = _fetch_bytes(page_url, MAX_HTML, 8, opener=opener).decode("utf-8", "ignore")
    match = (re.search(r'vqd=["\']?([0-9][0-9-]*)', page)
             or re.search(r'vqd=([0-9-]+)&', page)
             or re.search(r'"vqd"\s*:\s*"([^"]+)"', page))
    if not match:
        raise ValueError("no search token")
    api = "https://duckduckgo.com/i.js?" + urllib.parse.urlencode(
        {"l": "us-en", "o": "json", "q": query, "vqd": match.group(1), "p": "1"})
    raw = _fetch_bytes(api, MAX_HTML, 8, referer="https://duckduckgo.com/",
                       opener=opener,
                       extra={"X-Requested-With": "XMLHttpRequest",
                              "Accept": "application/json"})
    data = json.loads(raw.decode("utf-8", "ignore"))
    results = []
    for row in data.get("results", []):
        image = row.get("image") or ""
        if not image.startswith(("http://", "https://")):
            continue
        results.append({"title": row.get("title") or "",
                        "image": image,
                        "thumbnail": row.get("thumbnail") or image,
                        "w": int(row.get("width") or 0),
                        "h": int(row.get("height") or 0)})
        if len(results) >= count:
            break
    return results


def _ddg_search(query, count, site):
    """DDG with site: in the query; if empty, retry without it + host filter."""
    if site:
        rows = _ddg_once(query + " site:" + site, count)
        if rows:
            return rows
        rows = _ddg_once(query, max(count * 3, 30))
        return [r for r in rows if host_matches(host_of(r["image"]), site)][:count]
    return _ddg_once(query, count)


def parse_bing(html_text, count):
    """Bing embeds each result as an HTML-escaped JSON blob in m="...".

    Some m="..." attributes hold plain JSON scalars (true/false/numbers),
    not result objects - those must be skipped, not crashed on.
    """
    results = []
    for match in re.finditer(r'm="([^"]+)"', html_text):
        try:
            meta = json.loads(html_mod.unescape(match.group(1)))
        except ValueError:
            continue
        if not isinstance(meta, dict):
            continue                      # m="true" etc. - not a picture
        image = meta.get("murl") or ""
        if not isinstance(image, str) or not image.startswith(("http://", "https://")):
            continue
        results.append({"title": str(meta.get("t") or ""),
                        "image": image,
                        "thumbnail": meta.get("turl") or image,
                        "w": int(meta.get("pw") or 0),
                        "h": int(meta.get("ph") or 0)})
        if len(results) >= count:
            break
    return results


def _bing_search(query, count):
    url = ("https://www.bing.com/images/search?"
           + urllib.parse.urlencode({"q": query, "form": "HDRSC2"}))
    text = _fetch_bytes(url, MAX_HTML, 8,
                        referer="https://www.bing.com/").decode("utf-8", "ignore")
    return parse_bing(text, count)


def _bing_async(query, count):
    url = ("https://www.bing.com/images/async?"
           + urllib.parse.urlencode({"q": query, "first": "1", "count": "35",
                                     "relp": "35", "mmasync": "1"}))
    text = _fetch_bytes(url, MAX_HTML, 8,
                        referer="https://www.bing.com/images/search").decode("utf-8", "ignore")
    return parse_bing(text, count)


def _openverse_search(query, count):
    """Openverse: a real public JSON API (no bot games, openly licensed)."""
    url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(
        {"q": query, "page_size": str(max(5, count))})
    data = json.loads(_fetch_bytes(url, MAX_HTML, 8).decode("utf-8", "ignore"))
    results = []
    for row in data.get("results", []):
        image = row.get("url") or ""
        if not image.startswith(("http://", "https://")):
            continue
        results.append({"title": row.get("title") or "",
                        "image": image,
                        "thumbnail": row.get("thumbnail") or image,
                        "w": int(row.get("width") or 0),
                        "h": int(row.get("height") or 0)})
        if len(results) >= count:
            break
    return results


def search_images(query, count, site=None):
    """Merge quality-checked results from several backends, deduplicated."""
    full_query = query + (" site:" + site if site else "")
    backends = []
    if site and host_matches(site, "pinterest.com"):
        backends.append(("pinterest", lambda: _pinterest_search(query, count)))
    backends += [("duckduckgo", lambda: _ddg_search(query, count, site)),
                 ("bing", lambda: _bing_search(full_query, count)),
                 ("bing_async", lambda: _bing_async(full_query, count))]
    if not site:
        backends.append(("openverse", lambda: _openverse_search(query, count)))

    collected, seen, used, errors = [], set(), [], []
    for name, finder in backends:
        try:
            rows = finder()
        except Exception as ex:
            errors.append("%s: %s" % (name, ex))
            continue
        if site:
            rows = [r for r in rows if host_matches(host_of(r["image"]), site)]
        if not rows:
            errors.append("%s: no results" % name)
            continue
        if not site and not relevant(rows, query):
            errors.append("%s: irrelevant results (bot protection?)" % name)
            continue
        used.append(name)
        for row in rows:
            if row["image"] in seen:
                continue
            seen.add(row["image"])
            collected.append(row)
        if len(collected) >= count:
            break
    if not collected:
        raise ValueError("; ".join(errors) or "no results")
    return collected[:count], "+".join(used)


# ------------------------------------------------------------- contact sheet
def contact_sheet(results, thumbs):
    """One numbered grid image so the model can SEE and choose candidates."""
    rows = max(1, (len(results) + SHEET_COLS - 1) // SHEET_COLS)
    width = SHEET_COLS * SHEET_CELL
    height = rows * (SHEET_CELL + SHEET_LABEL)
    sheet = QImage(width, height, QImage.Format.Format_RGB32)
    sheet.fill(QColor("#1e1e1e"))
    painter = QPainter(sheet)
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        font = QFont("Segoe UI", 11)
        font.setBold(True)
        for index in range(len(results)):
            col = index % SHEET_COLS
            row = index // SHEET_COLS
            x = col * SHEET_CELL
            y = row * (SHEET_CELL + SHEET_LABEL)
            painter.fillRect(QRectF(x, y, SHEET_CELL, SHEET_LABEL), QColor("#101014"))
            painter.setFont(font)
            painter.setPen(QColor("#4fc3f7"))
            painter.drawText(QRectF(x + 8, y, SHEET_CELL - 16, SHEET_LABEL),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             "#%d" % (index + 1))
            area = QRectF(x + 4, y + SHEET_LABEL + 4, SHEET_CELL - 8, SHEET_CELL - 8)
            painter.fillRect(area, QColor("#3a3a3a"))
            image = QImage.fromData(thumbs.get(index, b"")) if thumbs.get(index) else QImage()
            if image.isNull():
                painter.setPen(QColor("#888888"))
                painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "?")
            else:
                scaled = image.scaled(int(area.width()), int(area.height()),
                                      Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation)
                painter.drawImage(QRectF(area.x() + (area.width() - scaled.width()) / 2,
                                         area.y() + (area.height() - scaled.height()) / 2,
                                         scaled.width(), scaled.height()), scaled)
            painter.setPen(QColor("#444444"))
            painter.drawRect(area)
    finally:
        painter.end()
    return sheet


def encode_jpeg(image, quality=82):
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice
    ba = QByteArray()
    buffer = QBuffer(ba)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "JPG", quality):
        raise ToolError("Could not encode the contact sheet")
    buffer.close()
    return base64.b64encode(bytes(ba)).decode("ascii")


# ------------------------------------------------------------------- worker
class _SearchThread(QThread):
    """Network off the GUI thread: search + preview downloads, no Qt GUI use."""
    completed = Signal(object)

    def __init__(self, query, count, site):
        super().__init__()
        self.query, self.count, self.site = query, count, site

    def run(self):
        out = {"results": [], "thumbs": {}, "error": "", "backend": ""}
        try:
            results, backend = search_images(self.query, self.count, self.site)
            out["results"], out["backend"] = results, backend
            deadline = time.monotonic() + THUMB_DEADLINE
            for index, row in enumerate(results):
                if time.monotonic() > deadline:
                    break
                try:
                    out["thumbs"][index] = _fetch_bytes(
                        row["thumbnail"] or row["image"], MAX_THUMB, 8)
                except Exception:
                    pass                     # a missing preview is not fatal
        except Exception as ex:
            out["error"] = str(ex) or "search failed"
        self.completed.emit(out)


# ----------------------------------------------------------------- registry
def register_web_tools(tools):
    """Add the web image search tool to an existing BoardTools registry."""
    S = tools._schema

    def t_web_search(a):
        query = str(a.get("query") or "").strip()
        if not query:
            raise ToolError("'query' must not be empty")
        if len(query) > 200:
            raise ToolError("'query' is too long (max 200 characters)")
        count = max(1, min(15, int(a.get("count", 10))))
        site = str(a.get("site") or "").strip().lower()
        if site and not re.fullmatch(r"[a-z0-9.-]+", site):
            raise ToolError("'site' must be a bare host like pinterest.com")

        holder = {}
        loop = QEventLoop()
        thread = _SearchThread(query, count, site or None)
        _SEARCHES.append(thread)

        def done(out):
            holder.update(out)
            loop.quit()

        def cleanup():
            if thread in _SEARCHES:
                _SEARCHES.remove(thread)

        thread.completed.connect(done)
        thread.finished.connect(cleanup)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        QTimer.singleShot(SEARCH_TIMEOUT_MS, loop.quit)
        loop.exec()
        if not holder:
            raise ToolError("Image search timed out (%d s)." % (SEARCH_TIMEOUT_MS // 1000)
                            + NO_INVENT)
        if holder.get("error"):
            raise ToolError("Image search failed: %s." % holder["error"] + NO_INVENT)
        results = holder.get("results") or []
        if not results:
            raise ToolError("No images found - try another query." + NO_INVENT)

        sheet = contact_sheet(results, holder.get("thumbs", {}))
        compact = [{"n": index + 1,
                    "title": row["title"][:60],
                    "url": row["image"],
                    "host": host_of(row["image"]),
                    "w": row["w"], "h": row["h"]}
                   for index, row in enumerate(results)]
        return {"query": query, "site": site or None,
                "backend": holder.get("backend", ""),
                "count": len(compact), "results": compact,
                "_image": {"data": encode_jpeg(sheet), "mime": "image/jpeg"},
                "message": "Look at the numbered contact sheet in the next message, "
                           "pick the best numbers, then download each chosen image "
                           "with board_import_url using its 'url'. Never invent URLs."}

    tools._add("board_web_search_images",
               "Search the web for images and SEE them: returns a numbered contact "
               "sheet plus a result list (n, title, url, host, size). Optionally "
               "restrict to one site, e.g. site='pinterest.com' (uses Pinterest's "
               "own search, no login needed). Writes nothing to disk. Workflow: "
               "search -> look at the sheet -> choose numbers -> board_import_url "
               "for each chosen url. Only download URLs returned by this tool or "
               "given by the user - never invent image URLs.",
               S({"query": {"type": "string"},
                  "count": {"type": "integer", "description": "Candidates 1-15, default 10"},
                  "site": {"type": "string", "description": "Optional host filter, e.g. pinterest.com"}},
                 ["query"]),
               t_web_search)


# ------------------------------------------------------------------ self-test
def _selftest():
    """python -m refsynapse.ai.board_web_tools  (add --live for a real search)"""
    from PySide6.QtWidgets import QApplication
    from ..main_window import MainWindow
    from .board_tools import BoardTools

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    tools = BoardTools(win)
    if "board_web_search_images" not in tools.names():
        register_web_tools(tools)          # standalone run before the hook exists
    print("Registered tools:", len(tools.names()))
    print("  has board_web_search_images ->", "board_web_search_images" in tools.names())

    def run():
        # --- offline: Bing parser, including the m="true" trap -------------
        fixture = (
            '<a class="iusc" m="true"></a>'
            '<a class="iusc" m="{&quot;murl&quot;:&quot;https://i.pinimg.com/originals/aa/bb/cc.jpg&quot;,'
            '&quot;turl&quot;:&quot;https://tse1.example/th1.jpg&quot;,&quot;t&quot;:&quot;Futuristic car A&quot;,'
            '&quot;pw&quot;:1200,&quot;ph&quot;:800}"></a>'
            '<a class="iusc" m="42"></a>'
            '<a class="iusc" m="{&quot;murl&quot;:&quot;https://i.pinimg.com/originals/dd/ee/ff.jpg&quot;,'
            '&quot;turl&quot;:&quot;https://tse1.example/th2.jpg&quot;,&quot;t&quot;:&quot;Futuristic car B&quot;,'
            '&quot;pw&quot;:900,&quot;ph&quot;:600}"></a>')
        parsed = parse_bing(fixture, 10)
        print("bing parser ->", len(parsed), "(expected 2)",
              parsed[0]["image"] if parsed else "")

        # --- offline: Pinterest HTML fallback parser ------------------------
        pin_fixture = (
            '{"images":{"orig":{"url":"https:\\/\\/i.pinimg.com\\/originals\\/11\\/22\\/33.jpg"}}}'
            '{"images":{"orig":{"url":"https:\\/\\/i.pinimg.com\\/736x\\/44\\/55\\/66.jpg"}}}'
            '{"images":{"orig":{"url":"https:\\/\\/i.pinimg.com\\/originals\\/11\\/22\\/33.jpg"}}}')
        pins = _pins_from_html(pin_fixture, 10)
        print("pin html parser ->", len(pins), "(expected 2)",
              pins[0]["image"] if pins else "")

        # --- offline: quality gates -----------------------------------------
        print("host pinimg ->", host_matches("i.pinimg.com", "pinterest.com"),
              "(expected True)")
        print("host junk ->", host_matches("www.emojiterra.com", "pinterest.com"),
              "(expected False)")
        junk = [{"title": "Stop Farting - Poop Emoji Meme"},
                {"title": "Fart Emoji Codes"}]
        good = [{"title": "Futuristic concept car render"},
                {"title": "Concept car design"}]
        print("decoy detected ->", not relevant(junk, "futuristic concept car"),
              "(expected True)")
        print("good accepted ->", relevant(good, "futuristic concept car"),
              "(expected True)")
        print("cyrillic skip ->", relevant(junk, "ежики милые"),
              "(expected True)")

        # --- offline: contact sheet from generated previews ----------------
        fake_results = [{"title": "t%d" % i, "image": "https://x/%d.jpg" % i,
                         "thumbnail": "", "w": 100, "h": 100} for i in range(3)]
        fake_thumbs = {}
        for i in range(3):
            img = QImage(120, 90, QImage.Format.Format_RGB32)
            img.fill(QColor(60 + i * 60, 90, 160))
            from PySide6.QtCore import QByteArray, QBuffer, QIODevice
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            img.save(buf, "PNG")
            buf.close()
            fake_thumbs[i] = bytes(ba)
        sheet = contact_sheet(fake_results, fake_thumbs)
        print("contact sheet ->", sheet.width(), "x", sheet.height(),
              "(expected %d x %d)" % (SHEET_COLS * SHEET_CELL, SHEET_CELL + SHEET_LABEL))

        # --- guards ---------------------------------------------------------
        bad = tools.call("board_web_search_images", {"query": ""})
        print("empty query ->", bad.get("ok"), "(expected False)", bad.get("error", ""))
        bad2 = tools.call("board_web_search_images",
                          {"query": "cars", "site": "http://pinterest.com"})
        print("bad site ->", bad2.get("ok"), "(expected False)", bad2.get("error", ""))

        # --- optional live searches (network!) ------------------------------
        if "--live" in sys.argv:
            live = tools.call("board_web_search_images",
                              {"query": "futuristic concept car",
                               "count": 6, "site": "pinterest.com"})
            print("live pinterest ->", live.get("ok"), live.get("backend"),
                  "count:", live.get("count"), live.get("error", ""))
            for row in (live.get("results") or [])[:6]:
                print("   #%d %s | %s" % (row["n"], row["host"], row["title"]))
            if live.get("ok"):
                import os
                from .sandbox import sandbox_root
                out = os.path.join(sandbox_root(win), "selftest_search.jpg")
                with open(out, "wb") as stream:
                    stream.write(base64.b64decode(live["_image"]["data"]))
                print("sheet saved ->", out)
            live2 = tools.call("board_web_search_images",
                               {"query": "hedgehog", "count": 4})
            print("live hedgehog ->", live2.get("ok"), live2.get("backend"),
                  "count:", live2.get("count"), live2.get("error", ""))
            for row in (live2.get("results") or [])[:4]:
                print("   #%d %s | %s" % (row["n"], row["host"], row["title"]))
        else:
            print("live search -> skipped (run with --live to try the network)")
        print("DONE - close the window to exit")

    QTimer.singleShot(800, run)
    sys.exit(app.exec())


if __name__ == "__main__":
    _selftest()