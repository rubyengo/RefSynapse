import ast
import os
import re

path = os.path.join("refsynapse", "ai", "board_tools.py")
with open(path, encoding="utf-8-sig") as stream:
    src = stream.read()

try:
    tree = ast.parse(src)
except SyntaxError as ex:
    print("SYNTAX ERROR line %s: %s" % (ex.lineno, ex.msg))
    raise SystemExit(1)

names = re.findall(r'self\._add\(\s*"([a-z0-9_]+)"', src)
cls = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BoardTools"]
methods = {n.name for n in cls[0].body if isinstance(n, ast.FunctionDef)} if cls else set()
hooks = ["register_crop_tools(self)", "register_extra_tools(self)", "register_pdf_tools(self)"]
counts = [src.count(h) for h in hooks]
lines = len(src.splitlines())

good = (lines >= 700 and len(names) == 22 and len(methods) >= 41
        and counts == [1, 1, 1]
        and 'if __name__ == "__main__":' in src
        and "class BoardBridge(QObject):" in src)

print("syntax OK")
print("lines", lines, "| base tools", len(names), "| methods", len(methods))
print("hooks crop/extra/pdf", counts)
print("STATIC CHECK:", "PASS" if good else "FAIL")
