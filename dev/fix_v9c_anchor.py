# dev/fix_v9c_anchor.py - rewrite anchor 12 of make_v9c.py (description hint).
import io, py_compile, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BUILDER = r"dev\make_v9c.py"
ENGINE = r"refsynapse\ai\board_pdf_translate_tools.py"
src = io.open(BUILDER, encoding="utf-8").read()

START = "R.append((\n'\"never invent ids."
END = "keep their \"'))\n"
if START not in src:
    print("ENTRY NOT FOUND - builder untouched (already fixed?)")
    sys.exit(1)
start = src.index(START)
end = src.index(END, start) + len(END)

NEW = (
    "R.append((\n"
    '    "never invent ids. Colour accents:",\n'
    '    "never invent ids. Every id gets the translation of ITS OWN text - "\n'
    '    "never summarise, merge, split or shift blocks; wording that belongs "\n'
    '    "to another block is refused and listed in \'misaligned\'. "\n'
    '    "Colour accents:"))\n'
)
src = src[:start] + NEW + src[end:]
io.open(BUILDER, "w", encoding="utf-8", newline="\n").write(src)
py_compile.compile(BUILDER, doraise=True)
print("builder patched: anchor 12 rewritten")
print("BUILDER SYNTAX OK")
engine = io.open(ENGINE, encoding="utf-8").read()
print("engine anchor count:", engine.count("never invent ids. Colour accents:"))
print("engine lines:", engine.count("\n") + 1)