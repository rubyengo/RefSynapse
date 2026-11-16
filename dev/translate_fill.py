r"""Fill pages 5, 10, 12 of the manual with Russian and assemble a preview.

Run from the repo root:  & ".\python\python.exe" -u agent\translate_fill.py
Resumes the job created by translate_probe.py, feeds the translations,
finishes to agent\manual_ru_preview.pdf and verifies the result.
"""
import os, sys, collections
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from refsynapse.main_window import MainWindow
from refsynapse.ai.board_tools import BoardTools
from refsynapse.ai.sandbox import sandbox_root
from refsynapse.ai.board_pdf_tools import _reader
from refsynapse.ai.board_pdf_translate_tools import _job_paths, _load_job
from pypdf.generic import ContentStream

TRANS = {
 "5": {
  "b1": "Глава 2. Установка оборудования",
  "b2": "2-1 Меры предосторожности",
  "b3": "Материнская плата содержит множество хрупких электронных схем и компонентов, которые могут быть повреждены электростатическим разрядом (ESD). Перед установкой внимательно прочитайте руководство пользователя и соблюдайте следующие правила:",
  "b5": "Перед установкой убедитесь, что корпус подходит для этой материнской платы.",
  "b7": "Перед установкой не удаляйте и не повреждайте наклейку с серийным номером (S/N) платы и гарантийную наклейку продавца. Они необходимы для подтверждения гарантии.",
  "b9": "Перед установкой или снятием платы и других компонентов всегда отключайте питание, вынув шнур из розетки.",
  "b11": "Подключая компоненты к внутренним разъёмам платы, убедитесь, что соединения плотные и надёжные.",
  "b13": "Работая с платой, не касайтесь металлических выводов и контактов разъёмов.",
  "b15": "При работе с платой, ЦП и памятью желательно носить антистатический браслет (ESD). Если его нет, держите руки сухими и сначала коснитесь металлического предмета, чтобы снять статический заряд.",
  "b17": "Перед установкой держите плату на антистатическом коврике или в антистатическом пакете.",
  "b19": "Перед подключением или отключением кабеля питания платы убедитесь, что блок питания выключен.",
  "b21": "Перед включением убедитесь, что напряжение блока питания соответствует местной электросети.",
  "b23": "Перед использованием проверьте, что все кабели и разъёмы питания компонентов подключены.",
  "b25": "Во избежание повреждения платы не допускайте контакта винтов с её схемой и компонентами.",
  "b27": "Убедитесь, что на плате и внутри корпуса не осталось лишних винтов и металлических деталей.",
  "b29": "Не ставьте компьютер на неровную поверхность.",
  "b31": "Не размещайте компьютер в жарком или влажном месте.",
  "b33": "Включение питания во время установки может повредить компоненты и нанести вред пользователю.",
  "b35": "Если вы не уверены в каком-либо шаге установки или возникла проблема с использованием продукта, обратитесь к квалифицированному специалисту.",
  "b37": "При использовании адаптера, удлинителя или сетевого фильтра соблюдайте инструкции по их подключению и заземлению."
 },
 "10": {
  "b1": "2-4 Установка памяти",
  "b2": "Перед установкой памяти ознакомьтесь со следующими рекомендациями:",
  "b4": "Убедитесь, что плата поддерживает эту память. Рекомендуется использовать модули одинакового объёма, производителя, скорости и чипов. (Актуальный список поддерживаемых скоростей и модулей — на сайте GIGABYTE.)",
  "b6": "Перед установкой памяти всегда выключайте компьютер и отключайте шнур питания от розетки, чтобы не повредить оборудование.",
  "b8": "Модули памяти имеют защиту от неправильной установки: модуль вставляется только одной стороной. Если модуль не вставляется, поверните его.",
  "b10": "При установке модулей памяти сначала заполняйте слот DDR4_B1.",
  "b11": "Двухканальный режим памяти",
  "b12": "На плате два слота памяти с поддержкой двухканального режима. После установки памяти BIOS автоматически определит её параметры и объём. Двухканальный режим удваивает пропускную способность памяти.\nДва слота DDR4 разделены на два канала, в каждом по одному слоту:",
  "b13": "▸",
  "b14": "Канал A: DDR4_A1",
  "b15": "▸",
  "b16": "Канал B: DDR4_B1",
  "b17": "Из-за ограничений ЦП перед установкой памяти в двухканальном режиме прочтите рекомендации ниже.\n1.",
  "b18": "Двухканальный режим невозможен с одним модулем памяти.",
  "b20": "Для двухканального режима с двумя модулями рекомендуется использовать память одинакового объёма, производителя, скорости и чипов."
 },
 "12": {
  "b1": "2-6 Разъёмы задней панели",
  "b2": "Порт USB 2.0/1.1",
  "b3": "Порт поддерживает спецификацию USB 2.0/1.1. Подключайте к нему USB-устройства.",
  "b4": "Порт PS/2 (клавиатура/мышь)",
  "b5": "Подключайте к этому порту мышь или клавиатуру PS/2.",
  "b6": "Порт D-Sub (прим. 1, 2)",
  "b7": "Порт D-Sub — 15-контактный разъём, максимальное разрешение 1920x1200@60 Гц (фактические разрешения зависят от монитора). Подключайте к нему монитор с входом D-Sub.",
  "b8": "Порт HDMI (прим. 1)",
  "b9": "Порт HDMI поддерживает HDCP, Dolby TrueHD и DTS HD Master Audio, а также вывод LPCM до 192 кГц/24 бит (7.1).",
  "b10": "Подключайте к порту монитор с HDMI. Максимальное разрешение — 4096x2160@60 Гц; фактические разрешения зависят от монитора.",
  "b11": "После подключения HDMI-устройства выберите HDMI устройством воспроизведения звука по умолчанию. (Название пункта зависит от операционной системы.)",
  "b12": "Порт USB 3.2 Gen 1",
  "b13": "Порт поддерживает спецификацию USB 3.2 Gen 1 и совместим с USB 2.0. Подключайте к нему USB-устройства.",
  "b14": "Порт RJ-45 (LAN)",
  "b15": "Гигабитный сетевой порт обеспечивает подключение к Интернету со скоростью до 1 Гбит/с. Ниже описаны состояния его индикаторов.",
  "b16": "Скорость",
  "b17": "Активность",
  "b18": "Скорость:",
  "b19": "Активность:",
  "b20": "Состояние",
  "b21": "Описание",
  "b22": "Состояние",
  "b23": "Описание",
  "b24": "Оранжевый",
  "b25": "Скорость 1 Гбит/с",
  "b26": "Мигает",
  "b27": "Идёт передача или приём данных",
  "b28": "Зелёный",
  "b29": "Скорость 100 Мбит/с",
  "b30": "Выкл.",
  "b31": "Передачи и приёма данных нет",
  "b32": "Выкл.",
  "b33": "Скорость 10 Мбит/с",
  "b34": "Сетевой порт",
  "b35": "(прим. 1) Только для процессоров AMD Ryzen™ 5000 G-Series/4000 G-Series/3000 G-Series.\n(прим. 2) Только для A520M K V2.",
  "b37": "Отсоединяя кабель от разъёма задней панели, сначала отключите его от устройства, затем от платы.",
  "b39": "Вынимайте кабель из разъёма строго прямо, не раскачивая его из стороны в сторону, чтобы избежать замыкания в разъёме."
 }
}

app = QApplication(sys.argv)
win = MainWindow()
win.show()
tools = BoardTools(win)
sb = sandbox_root(win)
print("tools:", len(tools.names()))

def run():
    src = os.path.join(sb, "manual_en.pdf")
    st = tools.call("board_pdf_translate_start", {"path": src, "pages": "5,10,12"})
    print("resume ok:", st.get("ok"), "| resumed:", st.get("resumed"))
    if not st.get("ok"):
        print("ERROR:", st.get("error"))
        print("DONE - close the window to exit")
        return
    job_id = st["job"]
    folder, jf = _job_paths(sb, job_id)
    job = _load_job(jf)
    for pg, blocks in TRANS.items():
        valid = {b["id"] for b in job["blocks"].get(str(pg), [])}
        unknown = [k for k in blocks if k not in valid]
        if unknown:
            print("BLOCK IDS CHANGED on page", pg, unknown,
                  "- re-run agent\\translate_probe.py first")
            print("DONE - close the window to exit")
            return
    pages = [{"page": pg, "blocks": blocks} for pg, blocks in sorted(TRANS.items())]
    fi = tools.call("board_pdf_translate_fill", {"job": job_id, "pages": pages})
    print("fill ok:", fi.get("ok"), "| progress:", fi.get("progress"))
    if not fi.get("ok"):
        print("ERROR:", fi.get("error"))
        print("DONE - close the window to exit")
        return
    print("overflow:", fi.get("overflow"))
    out = os.path.join(sb, "manual_ru_preview.pdf")
    fin = tools.call("board_pdf_translate_finish",
                     {"job": job_id, "out": out, "overwrite": True, "place": True})
    print("finish ok:", fin.get("ok"), "| pages:", fin.get("pages"),
          "| bookmarks:", fin.get("bookmarks"))
    if not fin.get("ok"):
        print("ERROR:", fin.get("error"))
        print("DONE - close the window to exit")
        return
    print("saved:", fin.get("path"))
    print("placed on board:", (fin.get("placed_item") or {}).get("uid"))
    print("warnings:", fin.get("warnings"))
    print("overflow in result:", fin.get("overflow"))

    src_r = _reader(src)
    res_r = _reader(out)
    marks = {5: ("Меры предосторожности", "Installation Precautions"),
             10: ("Установка памяти", "Installing the Memory"),
             12: ("Разъёмы задней панели", "Back Panel Connectors")}
    fails = []
    for pg, (ru, en) in marks.items():
        text = res_r.pages[pg - 1].extract_text() or ""
        ru_ok, en_gone = ru in text, en not in text
        print("page %d: russian=%s english_gone=%s chars=%d"
              % (pg, ru_ok, en_gone, len(text)))
        if not (ru_ok and en_gone):
            fails.append("text p%d" % pg)
        a = collections.Counter(op for _, op in
                                ContentStream(src_r.pages[pg-1].get_contents(), src_r).operations)
        b = collections.Counter(op for _, op in
                                ContentStream(res_r.pages[pg-1].get_contents(), res_r).operations)
        kept = all(a[k] == b[k] for k in (b"m", b"l", b"c", b"re", b"Do"))
        print("page %d: drawings kept=%s" % (pg, kept))
        if not kept:
            fails.append("drawings p%d" % pg)
    print("FAILS:", len(fails), fails)
    print("Open agent\\manual_ru_preview.pdf and compare pages 5, 10, 12 with the original.")
    print("DONE - close the window to exit")

QTimer.singleShot(800, run)
sys.exit(app.exec())