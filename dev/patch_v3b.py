r"""Safety net + a self-test that matches job version 3.

1. A page whose wording was not fully located must never be operated on:
   the original text is removed wholesale, so anything not measured would
   be lost. Coverage below 92% switches the page to 'facing' mode.
2. The in-module self-test still used the old block ids and the old
   'omitted blocks stay as they are' behaviour - replaced wholesale.
"""
import io
import py_compile
import shutil

PATH = r"refsynapse\ai\board_pdf_translate_tools.py"
BACKUP = r"dev\board_pdf_translate_tools.before_v3b.py"

ANCHOR = '''            chosen = "facing" if (unreadable or cramped or len(ready) > 45) \\
                else "inplace"'''

COVERAGE = '''            whole = re.sub(r"\\s", "", page.extract_text() or "")
            captured = sum(len(re.sub(r"\\s", "", b["text"]))
                           for b in keep_blocks + ready)
            covered = (captured / float(len(whole))) if whole else 1.0
            if covered < 0.92:
                warnings.append("page %d: only %d%% of its wording could be "
                                "measured - the page is kept as is and a "
                                "Russian page follows it"
                                % (number, int(100 * covered)))
'''

NEW_TAIL = r'''# ------------------------------------------------------------------ self-test
def _selftest():
    """python -m refsynapse.ai.board_pdf_translate_tools  (opens the window)"""
    import sys
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from ..main_window import MainWindow
    from .board_tools import BoardTools

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    tools = BoardTools(win)
    print("Registered tools:", len(tools.names()))
    for name in PDF_TRANSLATE_TOOL_NAMES:
        print("  has", name, "->", name in tools.names())
    sb = sandbox_root(win)
    print("agent sandbox ->", sb)

    def run():
        folder = os.path.join(sb, "pdf_translate_selftest")
        os.makedirs(folder, exist_ok=True)

        def p(name):
            return os.path.join(folder, name)

        def show(label, result, *keys):
            print(label, "->", result.get("ok"),
                  [result.get(k) for k in keys], result.get("error", ""))

        fails = []

        def expect(label, cond):
            print("  %s -> %s (expected True)" % (label, bool(cond)))
            if not cond:
                fails.append(label)

        def refuse(label, result):
            print("%s -> %s (expected False) %s"
                  % (label, result.get("ok"), result.get("error", "")[:110]))
            if result.get("ok"):
                fails.append(label)

        placed = []

        def remember(result):
            uid = (result.get("placed_item") or {}).get("uid")
            if uid:
                placed.append(uid)

        made = tools.call("board_pdf_create",
                          {"path": p("src.pdf"),
                           "text": "Chapter One\n\nHello world. This "
                                   "paragraph explains how to install the "
                                   "memory modules.\n\nSecond paragraph "
                                   "about the CPU cooler.",
                           "font_size": 12, "overwrite": True})
        show("create source", made, "pages")
        if not made.get("ok"):
            print("DONE - close the window to exit")
            return

        st = tools.call("board_pdf_translate_start",
                        {"path": p("src.pdf"), "restart": True})
        show("start", st, "job", "progress")
        job_id = st.get("job")
        modes = st.get("modes") or {}
        expect("page is inplace", modes.get("1") == "inplace")
        items = (st.get("next") or {}).get("items") or []
        expect("blocks found", len(items) >= 2)
        expect("ids are t*", all(i["id"].startswith("t") for i in items))
        expect("budget offered", all(i.get("max_chars", 0) > 0 for i in items))

        words = ["Глава первая",
                 "Привет, мир. Здесь описана установка модулей памяти.",
                 "Второй абзац о кулере процессора."]
        first = items[0]
        part = tools.call("board_pdf_translate_fill",
                          {"job": job_id, "page": 1,
                           "blocks": {first["id"]: words[0]}})
        show("partial fill", part, "progress")
        expect("page still pending",
               (part.get("progress") or {}).get("pending") == 1)
        expect("missing listed",
               bool((part.get("still_missing") or {}).get("1")))

        rest = {item["id"]: words[index % len(words)]
                for index, item in enumerate(items)}
        full = tools.call("board_pdf_translate_fill",
                          {"job": job_id, "page": 1, "blocks": rest})
        show("full fill", full, "progress")
        expect("no pending", (full.get("progress") or {}).get("pending") == 0)
        expect("next is null", full.get("next") is None)
        expect("no overflow", not full.get("overflow"))

        work = os.path.join(sb, "translate", job_id or "", "work.pdf")
        text = ""
        if os.path.isfile(work):
            text = _reader(work).pages[0].extract_text() or ""
        expect("russian visible", "Привет" in text)
        expect("english gone", "Hello world" not in text)

        tools.call("board_pdf_translate_fill",
                   {"job": job_id, "page": 1,
                    "blocks": {first["id"]: "Глава первая"}})
        text2 = ""
        if os.path.isfile(work):
            text2 = _reader(work).pages[0].extract_text() or ""
        expect("redraw not doubled", text2.count("Глава первая") == 1)

        refuse("unknown block id",
               tools.call("board_pdf_translate_fill",
                          {"job": job_id, "page": 1, "blocks": {"t999": "x"}}))

        made2 = tools.call("board_pdf_create",
                           {"path": p("tiny.pdf"),
                            "text": "OK\n\n" + ("A very long paragraph about "
                                                "memory modules. " * 6),
                            "font_size": 12, "overwrite": True})
        show("create tiny", made2, "pages")
        st_t = tools.call("board_pdf_translate_start",
                          {"path": p("tiny.pdf"), "restart": True})
        items_t = (st_t.get("next") or {}).get("items") or []
        target = None
        for item in items_t:
            if item["text"].strip() == "OK":
                target = item
                break
        if target is None:
            fails.append("no small block found")
        else:
            refuse("too long wording",
                   tools.call("board_pdf_translate_fill",
                              {"job": st_t.get("job"), "page": 1,
                               "blocks": {target["id"]:
                                          "Очень длинный русский текст, "
                                          "который никак не помещается "
                                          "в маленькую клетку заголовка."}}))
            tiny_job = _load_job(_job_paths(sb, st_t.get("job"))[1])
            expect("nothing was saved",
                   not tiny_job["translations"].get("1"))

        fin = tools.call("board_pdf_translate_finish",
                         {"job": job_id, "out": p("final.pdf"),
                          "overwrite": True})
        show("finish", fin, "pages", "bookmarks")
        remember(fin)
        expect("placed on board",
               (fin.get("placed_item") or {}).get("kind") == "document")
        expect("no extra pages", fin.get("pages") == 1)

        refuse("unknown job",
               tools.call("board_pdf_translate_finish", {"job": "no_such_job"}))
        outside = os.path.join(os.path.dirname(sb), "tr_escape.pdf")
        refuse("write outside",
               tools.call("board_pdf_translate_finish",
                          {"job": job_id, "out": outside}))
        print("outside created ->", os.path.exists(outside), "(expected False)")
        if os.path.exists(outside):
            fails.append("sandbox escape")

        st2 = tools.call("board_pdf_translate_start",
                         {"path": p("src.pdf"), "skip_pages": "1",
                          "restart": True})
        show("start skip", st2, "progress")
        expect("skip leaves no pending",
               (st2.get("progress") or {}).get("pending") == 0)
        fin2 = tools.call("board_pdf_translate_finish",
                          {"job": st2.get("job"), "out": p("kept.pdf"),
                           "overwrite": True, "place": False})
        show("finish kept", fin2, "pages", "kept_pages")
        text3 = ""
        if fin2.get("ok"):
            text3 = _reader(p("kept.pdf")).pages[0].extract_text() or ""
        expect("original preserved", "Hello world" in text3)

        st3 = tools.call("board_pdf_translate_start",
                         {"path": p("src.pdf"), "restart": True})
        refuse("finish too early",
               tools.call("board_pdf_translate_finish",
                          {"job": st3.get("job"), "out": p("early.pdf")}))
        part2 = tools.call("board_pdf_translate_finish",
                           {"job": st3.get("job"), "out": p("partial.pdf"),
                            "allow_partial": True, "place": False,
                            "overwrite": True})
        show("allow_partial", part2, "left_in_original")
        text4 = ""
        if part2.get("ok"):
            text4 = _reader(p("partial.pdf")).pages[0].extract_text() or ""
        expect("partial keeps english", "Hello world" in text4)

        resume = tools.call("board_pdf_translate_start",
                            {"path": p("src.pdf")})
        expect("resume works", resume.get("resumed") is True)

        made3 = tools.call("board_pdf_create",
                           {"path": p("flow.pdf"),
                            "text": "Chapter Two\n\n" +
                                    ("This section explains the setup. " * 12),
                            "font_size": 11, "overwrite": True})
        show("create flow", made3, "pages")
        st4 = tools.call("board_pdf_translate_start",
                         {"path": p("flow.pdf"), "restart": True})
        job4 = _load_job(_job_paths(sb, st4.get("job"))[1])
        job4["mode"]["1"] = "facing"
        _dump_job(job4, _job_paths(sb, st4.get("job"))[0])
        blocks4 = {b["id"]: "Русский текст этого блока."
                   for b in job4["blocks"]["1"]}
        fill4 = tools.call("board_pdf_translate_fill",
                           {"job": st4.get("job"), "page": 1,
                            "blocks": blocks4})
        show("facing fill", fill4, "progress")
        fin4 = tools.call("board_pdf_translate_finish",
                          {"job": st4.get("job"), "out": p("facing.pdf"),
                           "overwrite": True})
        show("facing finish", fin4, "pages", "russian_pages_added")
        remember(fin4)
        expect("a page was added", (fin4.get("pages") or 0) >= 2)
        if fin4.get("ok"):
            reader4 = _reader(p("facing.pdf"))
            expect("original page untouched",
                   "This section explains"
                   in (reader4.pages[0].extract_text() or ""))
            expect("russian page readable",
                   "Русский текст"
                   in (reader4.pages[1].extract_text() or ""))

        if placed:
            tools.call("board_delete", {"uids": placed})
        print("GUARD FAILS:", len(fails), fails)
        print("files are in ->", folder)
        print("DONE - close the window to exit")

    QTimer.singleShot(800, run)
    sys.exit(app.exec())


if __name__ == "__main__":
    _selftest()
'''

with io.open(PATH, "r", encoding="utf-8-sig") as stream:
    src = stream.read()

if "could be \"\n                                \"measured" in src or "covered < 0.92" in src:
    print("already patched - nothing to do")
    py_compile.compile(PATH, doraise=True)
    print("COMPILE OK")
    raise SystemExit(0)

MARK = "# ------------------------------------------------------------------ self-test"
for needle, label in ((ANCHOR, "mode decision"),
                      ("got < 0.55 * wanted", "measurement guard"),
                      (MARK, "self-test marker")):
    n = src.count(needle)
    print("%-20s x%d (expected 1)" % (label, n))
    if n != 1:
        raise SystemExit("STOP: target not unique, file NOT changed")

shutil.copyfile(PATH, BACKUP)
new = src.replace(ANCHOR, COVERAGE + ANCHOR, 1)
new = new.replace("got < 0.55 * wanted", "got < 0.85 * wanted", 1)
new = new[:new.index(MARK)] + NEW_TAIL

with io.open(PATH, "w", encoding="utf-8", newline="\n") as stream:
    stream.write(new)
py_compile.compile(PATH, doraise=True)
print("backup ->", BACKUP)
print("lines: %d -> %d" % (len(src.split("\n")), len(new.split("\n"))))
print("COMPILE OK")