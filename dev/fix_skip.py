import io, py_compile
path = r"refsynapse\ai\board_pdf_translate_tools.py"
with io.open(path, "r", encoding="utf-8-sig") as f:
    src = f.read()
old = "            if number in skip:"
new = "            if index in skip:"
n = src.count(old)
print("sites found:", n, "(expected 1)")
if n != 1:
    raise SystemExit("STOP: file differs, nothing changed")
src = src.replace(old, new, 1)
with io.open(path, "w", encoding="utf-8", newline="\n") as f:
    f.write(src)
py_compile.compile(path, doraise=True)
print("COMPILE OK")
