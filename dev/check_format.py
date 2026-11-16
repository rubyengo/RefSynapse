import ast, os, re

FILES = [os.path.join("refsynapse", "ai", "board_pdf_tools.py"),
         os.path.join("refsynapse", "ai", "board_pdf_edit_tools.py")]
SLOT = re.compile(r"%[-+ #0]*[\d.*]*[sdifgexXorc%]")
bad = checked = 0
for path in FILES:
    with open(path, encoding="utf-8-sig") as stream:
        source = stream.read()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mod):
            continue
        left = node.left
        if not (isinstance(left, ast.Constant) and isinstance(left.value, str)):
            continue
        checked += 1
        need = len([s for s in SLOT.findall(left.value) if s != "%%"])
        have = len(node.right.elts) if isinstance(node.right, ast.Tuple) else 1
        if need != have:
            bad += 1
            print("MISMATCH %s line %d: needs %d, given %d"
                  % (os.path.basename(path), node.lineno, need, have))
print("format strings checked ->", checked)
print("mismatches ->", bad)
print("FORMAT CHECK:", "PASS" if bad == 0 else "FAIL")
