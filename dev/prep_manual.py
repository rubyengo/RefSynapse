r"""Build the manual translation job and report the plan for every page.

No model calls. Run from the repo root:
    & ".\python\python.exe" -u dev\prep_manual.py
"""
import json
import os
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from refsynapse.main_window import MainWindow
from refsynapse.ai.board_tools import BoardTools
from refsynapse.ai.sandbox import sandbox_root
from refsynapse.ai.board_pdf_tools import _reader
from refsynapse.ai.board_pdf_translate_tools import _job_paths, _load_job


def s(value):
    """ASCII-safe printing: the console here is cp1251 and crashes on stray codes."""
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


app = QApplication(sys.argv)
win = MainWindow()
win.show()
tools = BoardTools(win)
sb = sandbox_root(win)
print("tools:", len(tools.names()))


def run():
    src = os.path.join(sb, "manual_en.pdf")
    print("source exists:", os.path.isfile(src))
    if not os.path.isfile(src):
        print("DONE - close the window to exit")
        return
    st = tools.call("board_pdf_translate_start",
                    {"path": src, "restart": True, "skip_pages": "28"})
    print("start ok:", st.get("ok"), s(st.get("error", "")))
    if not st.get("ok"):
        print("DONE - close the window to exit")
        return
    print("progress:", st.get("progress"))
    job = _load_job(_job_paths(sb, st["job"])[1])
    print("job version:", job.get("version"))
    reader = _reader(src)
    total_items, kept_with_text = 0, []
    for number in range(1, job["pages_total"] + 1):
        key = str(number)
        mode = job["mode"].get(key, "-")
        blocks = job["blocks"].get(key, [])
        locked = job["locked"].get(key, [])
        chars = sum(len(b["text"]) for b in blocks)
        caps = [b.get("max_chars", 0) for b in blocks]
        total_items += len(blocks)
        if mode in ("inplace", "facing"):
            print("p%2d %-7s blocks=%3d locked=%3d chars=%5d min_cap=%s"
                  % (number, mode, len(blocks), len(locked), chars,
                     min(caps) if caps else "-"))
        else:
            try:
                source_chars = len(reader.pages[number - 1].extract_text() or "")
            except Exception:
                source_chars = -1
            print("p%2d %-7s source_chars=%5d" % (number, mode, source_chars))
            if source_chars > 200 and number != 28:
                kept_with_text.append(number)
    print("blocks to translate:", total_items)
    print("KEPT WITH TEXT:", kept_with_text)
    for note in job.get("warnings", []):
        print("warning:", s(note))
    report = {"modes": job["mode"],
              "counts": {k: len(v) for k, v in job["blocks"].items()},
              "kept_with_text": kept_with_text,
              "warnings": job.get("warnings", [])}
    with open(os.path.join("dev", "prep_report.json"), "w",
              encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=1)
    print("report -> dev\\prep_report.json")
    print("DONE - close the window to exit")


QTimer.singleShot(800, run)
sys.exit(app.exec())