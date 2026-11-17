# dev/diag_v9.py - confirm: payload bloat, model misassignment, inline images.
import io, json, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pypdf import PdfReader
from pypdf.generic import ContentStream

SRC = r"refsynapse\ai\board_pdf_translate_tools.py"
ls = io.open(SRC, encoding="utf-8").read().splitlines()
print("----- engine 174-252 (_next_batch / _payload) -----")
for i in range(173, 252):
    print("%d %s" % (i + 1, ls[i]))
raw = io.open(r"agent\translate\test1\job.json", encoding="utf-8").read()
job = json.loads(raw)
print("job.json chars:", len(raw))
print("block fields:", sorted(job["blocks"]["5"][0].keys()))
print("avoid of p11 first block:", job["blocks"]["11"][0].get("avoid"))
for key in ["5", "7", "10", "12"]:
    print("=" * 20, "PAGE", key)
    tr = job["translations"].get(key, {})
    for b in job["blocks"].get(key, []):
        print("%s | ORIG %s | RU %s" % (
            b["id"], b["text"].replace("\n", " ")[:44],
            tr.get(b["id"], "<none>").replace("\n", " ")[:44]))
r = PdfReader(r"agent\translate\test1\source.pdf")
for n in (9, 11):
    pg = r.pages[n - 1]
    cs = ContentStream(pg.get_contents(), r)
    inline = sum(1 for _, op in cs.operations if op == b"INLINE IMAGE")
    xo = (pg.get("/Resources") or {}).get("/XObject") or {}
    kinds = {}
    for name, ref in xo.items():
        st = str(ref.get_object().get("/Subtype"))
        kinds[st] = kinds.get(st, 0) + 1
    print("page", n, "inline images:", inline, "xobjects:", kinds)