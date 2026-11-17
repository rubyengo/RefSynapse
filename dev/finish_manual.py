r"""Deterministically finish the manual translation job (no model calls).

Pending pages are switched to 'facing' mode (original page + a Russian page
after it), so there is no per-box fit limit and no overlap. Prose is taken
from the embedded dictionary; anything not matched (pin names, URLs, company
address, screenshot labels) keeps its original wording and is reported.
"""
import os
import re
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from refsynapse.main_window import MainWindow
from refsynapse.ai.board_tools import BoardTools
from refsynapse.ai.sandbox import sandbox_root
from refsynapse.ai.board_pdf_tools import _reader
from refsynapse.ai.board_pdf_translate_tools import (
    _job_paths, _load_job, _dump_job, _missing)

EXACT = {"yes": "Да", "no": "Нет"}
MAP = [
    ("startup screen:", "Экран запуска:"),
    ("the following startup logo screen", "При загрузке компьютера появится следующий экран с логотипом."),
    ("function keys", "Функциональные клавиши:"),
    ("press the <delete> key", "Нажмите <Delete>, чтобы войти в BIOS Setup или открыть Q-Flash в BIOS Setup."),
    ("boot menu allows you", "Boot Menu позволяет выбрать первое загрузочное устройство без входа в BIOS Setup. Стрелками выберите устройство и нажмите <Enter>."),
    ("the system will boot from the device immediately", "Система сразу загрузится с этого устройства."),
    ("note: the setting in boot menu", "Настройка Boot Menu действует один раз; после перезагрузки порядок загрузки берётся из BIOS Setup."),
    ("press the <end> key", "Нажмите <End>, чтобы сразу открыть Q-Flash без входа в BIOS Setup."),
    ("chapter 4 installing the operating system", "Глава 4 Установка ОС и драйверов"),
    ("4-1 operating system installation", "4-1 Установка операционной системы"),
    ("with the correct bios settings", "При правильных настройках BIOS можно устанавливать операционную систему."),
    ("as some operating systems already include raid driver", "Некоторые ОС уже содержат драйвер RAID, поэтому отдельный драйвер при установке Windows не нужен. После установки ОС рекомендуется поставить все драйверы через GIGABYTE APP Center. Если ОС требует дополнительный драйвер RAID при установке, выполните шаги ниже."),
    ("step 1: go to gigabyte's website", "Шаг 1: на сайте GIGABYTE откройте страницу модели платы, скачайте AMD RAID Preinstall Driver (Support\\Download\\SATA RAID/AHCI), распакуйте и скопируйте на USB-флешку."),
    ("step 2: boot from the windows setup disc", "Шаг 2: загрузитесь с установочного диска Windows и выполните обычную установку. Когда появится запрос драйвера, выберите Browse."),
    ("step 3: insert the usb thumb drive", "Шаг 3: вставьте USB-флешку, укажите папку драйвера. Сначала выберите AMD-RAID Bottom Device и нажмите Next, затем AMD-RAID Controller и Next. Продолжите установку ОС."),
    ("4-2 drivers installation", "4-2 Установка драйверов"),
    ("after you install the operating system, a dialog box", "После установки ОС в правом нижнем углу появится окно с предложением скачать драйверы и приложения GIGABYTE через APP Center. Нажмите Install. (В BIOS Setup: Settings\\IO Ports\\APP Center Download& Install Configuration\\APP Center Download& Install = Enabled.)"),
    ("before the installation, make sure the system is connected", "Перед установкой убедитесь, что система подключена к Интернету."),
    ("when the end user license agreement dialog box appears", "В окне лицензионного соглашения нажмите <Accept>, чтобы установить APP Center. На экране APP Center выберите драйверы и приложения и нажмите Install."),
    ("please visit gigabyte's website for more software information", "Подробнее о ПО — на сайте GIGABYTE:"),
    ("please visit gigabyte's website for more troubleshooting information", "Подробнее о решении проблем — на сайте GIGABYTE:"),
    ("chapter 5 appendix", "Глава 5 Приложение"),
    ("5-1 configuring a raid set", "5-1 Настройка RAID"),
    ("raid levels", "Уровни RAID"),
    ("minimum number of hard drives", "Минимум дисков"),
    ("array capacity", "Объём массива"),
    ("fault tolerance", "Отказоустойчивость"),
    ("number of hard drives* size of the smallest drive", "Число дисков * объём наименьшего"),
    ("(number of hard drives/2)* size of the smallest drive", "(Число дисков/2) * объём наименьшего"),
    ("size of the smallest drive", "Объём наименьшего диска"),
    ("before you begin, please prepare the following items", "Перед началом подготовьте:"),
    ("this motherboard supports raid 0, raid 1, and raid 10", "Плата поддерживает RAID 0, 1 и 10. Подготовьте нужное число дисков по таблице выше."),
    ("sata hard drives or ssds", "SATA-диски или SSD. Для оптимальной работы используйте два диска одинаковой модели и объёма."),
    ("windows setup disc", "Установочный диск Windows."),
    ("an internet connected computer", "Компьютер с доступом в Интернет."),
    ("a usb thumb drive", "USB-флешка."),
    ("an m.2 pcie ssd cannot be used", "M.2 PCIe SSD нельзя объединить в RAID с SATA-диском."),
    ("please visit gigabyte's website for details on configuring a raid array", "Подробнее о настройке RAID — на сайте GIGABYTE:"),
    ("contact us", "Свяжитесь с нами"),
    ("to submit a technical or non-technical", "Для технических и нетехнических вопросов (продажи/маркетинг):"),
    ("gigabyte esupport", "GIGABYTE eSupport"),
]


def norm(t):
    return re.sub(r"\s+", " ", t).strip().lower()


def ru_for(text):
    n = norm(text)
    if n in EXACT:
        return EXACT[n], True
    for prefix, ru in MAP:
        if n.startswith(prefix):
            return ru, True
    return text, False


app = QApplication(sys.argv)
win = MainWindow()
win.show()
tools = BoardTools(win)
sb = sandbox_root(win)


def run():
    folder, jf = _job_paths(sb, "manual_en")
    job = _load_job(jf)
    pending = sorted(int(k) for k, v in job["status"].items() if v == "pending")
    print("pending pages:", pending)
    fallback = []
    for number in pending:
        key = str(number)
        job["mode"][key] = "facing"
        _dump_job(job, folder)
        missing = _missing(job, key)
        blocks = {}
        for b in job["blocks"].get(key, []):
            if b["id"] in missing:
                ru, hit = ru_for(b["text"])
                blocks[b["id"]] = ru
                if not hit:
                    fallback.append((number, b["id"], b["text"][:40]))
        r = tools.call("board_pdf_translate_fill",
                       {"job": "manual_en", "page": number, "blocks": blocks})
        print("page %d facing fill -> %s %s" % (number, r.get("ok"), r.get("error", "")))
        if not r.get("ok"):
            print("DONE - close the window to exit")
            return
        job = _load_job(jf)
    fin = tools.call("board_pdf_translate_finish",
                     {"job": "manual_en", "out": os.path.join(sb, "manual_en_ru.pdf"),
                      "overwrite": True})
    print("finish ->", fin.get("ok"), fin.get("error", ""))
    if fin.get("ok"):
        print("pages:", fin.get("pages"), "| bookmarks:", fin.get("bookmarks"))
        print("russian pages added:", fin.get("russian_pages_added"))
        print("overflow:", fin.get("overflow"))
        res = _reader(os.path.join(sb, "manual_en_ru.pdf"))
        cyr = re.compile("[Ѐ-ӿ]")
        with_ru = sum(1 for p in res.pages if len(cyr.findall(p.extract_text() or "")) > 30)
        print("pages with Russian:", with_ru, "of", len(res.pages))
    print("kept original (labels/URLs):", len(fallback))
    for f in fallback[:20]:
        print("   p%d %s | %s" % f)
    print("DONE - close the window to exit")


QTimer.singleShot(800, run)
sys.exit(app.exec())