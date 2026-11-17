import io, py_compile
path = r"refsynapse\ai\board_pdf_translate_tools.py"
with io.open(path, "r", encoding="utf-8-sig") as f:
    lines = f.read().split("\n")
fixed = 0
for i, line in enumerate(lines):
    if line.strip() == '["job"]),':
        prev = lines[i - 1].rstrip()
        if not prev.endswith("}),"):
            raise SystemExit("UNEXPECTED previous line: %r" % prev)
        lines[i - 1] = prev[:-2] + ","
        lines[i] = '                 ["job"]),'
        fixed += 1
print("sites fixed:", fixed, "(expected 2)")
if fixed != 2:
    raise SystemExit("STOP: expected exactly 2 broken sites, file NOT changed")
with io.open(path, "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join(lines))
py_compile.compile(path, doraise=True)
print("COMPILE OK")
