import io, py_compile
T = r"refsynapse\ai\board_pdf_translate_tools.py"
E = r"refsynapse\ai\board_pdf_edit_tools.py"

def check(path):
    try:
        py_compile.compile(path, doraise=True)
        return True
    except py_compile.PyCompileError as ex:
        print("COMPILE PROBLEM:", str(ex).splitlines()[-1][:200])
        return False

ok = check(T)
if not ok:
    with io.open(T, "r", encoding="utf-8-sig") as f:
        lines = f.read().split("\n")
    fixed = 0
    for i, line in enumerate(lines):
        if line.strip() == '["job"]),':
            prev = lines[i - 1].rstrip()
            if prev.endswith("}),"):
                lines[i - 1] = prev[:-2] + ","
                lines[i] = '                 ["job"]),'
                fixed += 1
    print("bracket sites fixed:", fixed)
    if fixed:
        with io.open(T, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines))
        ok = check(T)
    if not ok:
        raise SystemExit("STOP: translate module does not compile, hook NOT added")
print("translate COMPILE OK")

with io.open(E, "r", encoding="utf-8-sig") as f:
    src = f.read()
if "register_pdf_translate_tools" in src:
    print("hook already present")
else:
    marker = "# ------------------------------------------------------------------ self-test"
    if src.count(marker) != 1:
        raise SystemExit("STOP: marker found %d times, expected 1" % src.count(marker))
    hook = (
        "    # Translation (batch 3) lives in board_pdf_translate_tools.py.\n"
        "    try:\n"
        "        from .board_pdf_translate_tools import register_pdf_translate_tools\n"
        "    except ImportError:\n"
        "        register_pdf_translate_tools = None\n"
        "    if register_pdf_translate_tools is not None:\n"
        "        register_pdf_translate_tools(tools)\n"
        "\n"
    )
    src = src.replace(marker, hook + "\n" + marker, 1)
    with io.open(E, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)
    print("hook added")
if not check(E):
    raise SystemExit("STOP: edit module does not compile after the hook")
print("edit COMPILE OK")
print("ALL OK - ready for the live selftest")
