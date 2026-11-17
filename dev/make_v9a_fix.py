# dev/make_v9a_fix.py - fix my own v9a flaws: relink by position, photo geometry.
import io, py_compile, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SRC = r"refsynapse\ai\board_pdf_translate_tools.py"
text = io.open(SRC, encoding="utf-8").read()
before = text.count("\n") + 1
R = []

R.append((
'''def _relink_annots(src, out, mapping):
    """Point every internal jump at the LIVE page object of the result."""
    src_ids = _page_ids(src)
    fixed = 0
    for page in out.pages:
        for obj in _annots_of(page):
            d = _dest_array(obj)
            if d is None:
                continue
            head = d[0]
            if not isinstance(head, IndirectObject):
                continue
            orig = src_ids.get(head.idnum)
            if orig is None:
                continue
            live = mapping.get(orig, orig)
            if live >= len(out.pages):
                continue
            ref = out.pages[live].indirect_reference
            if ref is not None and head.idnum != ref.idnum:
                d[0] = ref
                fixed += 1
    return fixed
''',
'''def _relink_annots(src, out, mapping):
    """Point every internal jump at the LIVE page object of the result.
    Object numbers differ between files, so match annotations by their
    ORDER on the matching source page, read the intended target there,
    and aim it at the result page via the assembly mapping."""
    src_ids = _page_ids(src)
    inv = {v: k for k, v in mapping.items()}
    fixed = 0
    for oi, page in enumerate(out.pages):
        si = inv.get(oi)
        if si is None or si >= len(src.pages):
            continue
        s_ann = _annots_of(src.pages[si])
        o_ann = _annots_of(page)
        for k, obj in enumerate(o_ann):
            if k >= len(s_ann):
                break
            d = _dest_array(obj)
            sd = _dest_array(s_ann[k])
            if d is None or sd is None:
                continue
            head = sd[0]
            if not isinstance(head, IndirectObject):
                continue
            t = src_ids.get(head.idnum)
            if t is None:
                continue
            live = mapping.get(t, t)
            if live >= len(out.pages):
                continue
            ref = out.pages[live].indirect_reference
            if ref is not None:
                d[0] = ref
                fixed += 1
    return fixed
'''))

R.append((
'(120, 0, 0, 90, 150, 250)',
'(100, 0, 0, 90, 190, 250)'))

R.append((
'if 249 < s["y"] < 341 and s["x"] + s["w"] > 149]',
'if 249 < s["y"] < 341 and s["x"] + s["w"] > 189]'))

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