import os
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from pypdf import PdfReader, PdfWriter

font = r"C:\Windows\Fonts\segoeui.ttf"
print("font exists ->", os.path.isfile(font))
pdfmetrics.registerFont(TTFont("Segoe", font))

out = os.path.join("agent", "smoke.pdf")
c = canvas.Canvas(out)
c.setFont("Segoe", 24)
c.drawString(72, 720, "Привет, PDF! Ёжики и снег.")
c.showPage()
c.save()

r = PdfReader(out)
txt = r.pages[0].extract_text() or ""
print("pages ->", len(r.pages))
print("cyrillic ok ->", "Привет" in txt)

w = PdfWriter()
w.append(out)
w.append(out)
merged = os.path.join("agent", "smoke_merged.pdf")
with open(merged, "wb") as f:
    w.write(f)
print("merged pages ->", len(PdfReader(merged).pages))
print("SMOKE OK")
