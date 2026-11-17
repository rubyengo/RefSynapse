# dev/dump_engine.py — выгрузка ключевых функций движка + проверка внутренних ссылок.
import sys, io
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pypdf import PdfReader

SRC = r"refsynapse\ai\board_pdf_translate_tools.py"
ls = io.open(SRC, encoding="utf-8").read().splitlines()
ranges = [(297, 325), (370, 496), (595, 708), (792, 848), (1043, 1084), (1133, 1437)]
out = []
for a, b in ranges:
    out.append("----- %d-%d -----" % (a, b))
    for i in range(a - 1, min(b, len(ls))):
        out.append("%d %s" % (i + 1, ls[i]))
io.open(r"dev\engine_dump1.txt", "w", encoding="utf-8").write("\n".join(out))
print("dump lines:", len(out), "-> dev\\engine_dump1.txt")

res = r"agent\MakeUseOf.com_-_Solid_State_Drives_ru.pdf"
r = PdfReader(res)
ids = set()
for p in r.pages:
    ir = p.indirect_reference
    if ir is not None:
        ids.add(ir.idnum)
tot = bad = 0
for i, p in enumerate(r.pages, 1):
    a = p.get("/Annots")
    if a is None:
        continue
    for ref in a.get_object():
        o = ref.get_object()
        d = o.get("/Dest")
        act = o.get("/A")
        if d is None and act is not None:
            act = act.get_object()
            d = act.get("/D")
        if d is None:
            continue
        if hasattr(d, "get_object"):
            d = d.get_object()
        if not isinstance(d, list) or not d:
            continue
        tot += 1
        t = d[0]
        ok = hasattr(t, "idnum") and t.idnum in ids
        if not ok:
            bad += 1
            print("page %d BROKEN internal dest -> idnum %s" % (i, getattr(t, "idnum", t)))
print("internal dests: %d, broken: %d" % (tot, bad))