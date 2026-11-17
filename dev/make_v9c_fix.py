# dev/make_v9c_fix.py - honest filler in the tiny guard, measure without marks.
import io, py_compile, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SRC = r"refsynapse\ai\board_pdf_translate_tools.py"
text = io.open(SRC, encoding="utf-8").read()
before = text.count("\n") + 1
R = []

R.append((
'                      for i in items_t if i["id"] != target["id"]}',
'                      for i in items_t if i["id"] != target["id"]}\n'
'            others = {k: (v + " ") * 6 for k, v in others.items()}'))

R.append((
'''    src = str(block.get("text", ""))
    a, b = _plain_len(src), _plain_len(value)
''',
'''    src = str(block.get("text", ""))
    value = _strip_marks(value)[0]
    a, b = _plain_len(src), _plain_len(value)
'''))

for i, (old, new) in enumerate(R, 1):
    n = text.count(old)
    if n != 1:
        print("ANCHOR %d FOUND %d TIMES - ABORT, file untouched" % (i, n))
        sys.exit(1)
    text = text.replace(old, new)
io.open(SRC, "w", encoding="utf-8", newline="\n").write(text)
py_compile.compile(SRC, doraise=True)
print("SYNTAX OK")
print("lines: %d -> %d" % (before, text.count("\n") + 1))
print("replacements applied:", len(R))