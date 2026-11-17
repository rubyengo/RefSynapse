r"""Quick check of agent\manual_en_ru.pdf: pages, bookmarks, Russian per page."""
import os
import re
from pypdf import PdfReader

PATH = os.path.join("agent", "manual_en_ru.pdf")
if not os.path.isfile(PATH):
    raise SystemExit("No result yet: %s" % PATH)
reader = PdfReader(PATH)
cyr = re.compile("[\u0410-\u044f\u0401\u0451]")
rows = []
for number, page in enumerate(reader.pages, 1):
    try:
        text = page.extract_text() or ""
    except Exception:
        text = ""
    rows.append((number, len(cyr.findall(text))))
print("pages:", len(reader.pages))
print("bookmarks:", len(reader.outline))
print("pages with Russian:", sum(1 for _, n in rows if n > 30))
print("cyrillic chars per page:", rows)