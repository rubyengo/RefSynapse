import ast, io

p = r"refsynapse\ai\board_pdf_translate_tools.py"
s = io.open(p, encoding="utf-8").read()
old = (
    '        if nb:\n'
    '            tools.call("board_pdf_translate_fill",\n'
    '                       {"job": nid, "page": 1,\n'
    '                        "blocks": {nb["id"]: long_ru}})\n'
)
new = (
    '        ob = next((b for b in jn["blocks"].get("1", [])\n'
    '                   if b["text"].strip() == "Next line."), None)\n'
    '        if nb and ob:\n'
    '            tools.call("board_pdf_translate_fill",\n'
    '                       {"job": nid, "page": 1,\n'
    '                        "blocks": {nb["id"]: long_ru,\n'
    '                                   ob["id"]: "Next line."},\n'
    '                        "on_overflow": "facing"})\n'
)
n = s.count(old)
assert n == 1, "pattern found %d times - stop, file not touched" % n
s = s.replace(old, new)
ast.parse(s)
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("PATCHED", n, "SYNTAX OK", len(s.splitlines()))