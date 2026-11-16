r"""Dump block geometry for problem pages to dev\diag_blocks.json."""
import json, os, sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from refsynapse.main_window import MainWindow
from refsynapse.ai.board_tools import BoardTools
from refsynapse.ai.sandbox import sandbox_root
from refsynapse.ai.board_pdf_translate_tools import _page_spans, _group_blocks
from refsynapse.ai.board_pdf_tools import _reader

PAGES = [7, 17, 19, 20]
app = QApplication(sys.argv)
win = MainWindow(); win.show()
tools = BoardTools(win)
sb = sandbox_root(win)

def run():
    src = os.path.join(sb, "manual_en.pdf")
    reader = _reader(src)
    out = {}
    for pg in PAGES:
        page = reader.pages[pg - 1]
        pw = float(page.mediabox.width)
        try:
            spans = _page_spans(page)
            blocks = _group_blocks(spans, pw)
        except Exception as ex:
            out[str(pg)] = {"error": str(ex)}
            continue
        out[str(pg)] = {"page_width": pw, "blocks": [
            {"id": b["id"], "x0": b["x0"], "x1": b["x1"],
             "x_room": b["x_room"], "size": b["size"],
             "text": b["text"][:80]} for b in blocks]}
        narrow = [b for b in blocks if b["x_room"] - b["x0"] < 60 and len(b["text"]) > 40]
        print("page %d: %d blocks, %d narrow (<60pt with >40 chars)" % (pg, len(blocks), len(narrow)))
        for b in narrow[:5]:
            print("  %s x0=%.0f x1=%.0f room=%.0f | %s" % (b["id"], b["x0"], b["x1"], b["x_room"], b["text"][:60]))
    dst = os.path.join("dev", "diag_blocks.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("saved:", dst)
    print("DONE - close the window to exit")

QTimer.singleShot(800, run)
sys.exit(app.exec())