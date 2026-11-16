import os, sys, json, time
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from refsynapse.main_window import MainWindow
from refsynapse.ai.board_tools import BoardTools
from refsynapse.ai.sandbox import sandbox_root
from refsynapse.ai.board_pdf_translate_tools import _job_paths, _load_job

app = QApplication(sys.argv)
win = MainWindow()
win.show()
tools = BoardTools(win)
sb = sandbox_root(win)
print("tools:", len(tools.names()))

def run():
    src = os.path.join(sb, "manual_en.pdf")
    print("source:", src, "exists:", os.path.isfile(src))
    if not os.path.isfile(src):
        print("DONE - close the window to exit")
        return
    t0 = time.time()
    st = tools.call("board_pdf_translate_start",
                    {"path": src, "pages": "5,10,12", "restart": True})
    print("start ok:", st.get("ok"), "sec:", round(time.time() - t0, 1))
    if not st.get("ok"):
        print("ERROR:", st.get("error"))
        print("DONE - close the window to exit")
        return
    print("job:", st.get("job"), "progress:", st.get("progress"))
    print("warnings:", st.get("warnings"))
    folder, jf = _job_paths(sb, st["job"])
    job = _load_job(jf)
    rows = []
    for key in sorted(job["blocks"], key=int):
        blocks = job["blocks"][key]
        chars = sum(len(b["text"]) for b in blocks)
        print("page %s: %d blocks, %d chars" % (key, len(blocks), chars))
        for b in blocks:
            rows.append({"page": int(key), "id": b["id"],
                         "size": b["size"], "text": b["text"]})
    dst = os.path.join(sb, "translate_blocks.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print("blocks total:", len(rows), "->", dst)
    for r in rows[:10]:
        print("  p%d %s %.1fpt | %s" % (r["page"], r["id"], r["size"], r["text"][:70]))
    print("DONE - close the window to exit")

QTimer.singleShot(800, run)
sys.exit(app.exec())
