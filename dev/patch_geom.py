"""Replace three geometry functions in board_pdf_translate_tools.py.

Why: on the real manual the block rectangles were wrong. Subset fonts list
hundreds of zero widths, so the old average made every line look ~3x too
short; and two side-by-side tables were glued into one block. This installs
real glyph widths, column-aware splitting and per-cell room to the right.
Aborts without touching the file if any marker is missing or duplicated.
"""
import io, os, py_compile, shutil

PATH = r"refsynapse\ai\board_pdf_translate_tools.py"
BACKUP = r"agent\board_pdf_translate_tools.before_geom.py"

M1 = "def _page_spans(page):"
M2 = "def _group_blocks(spans):"
M3 = "def _forms_have_text(page):"
M4 = "def _fit_block(block, text, font, min_scale):"
M5 = "# ----------------------------------------------------------------- rendering"

NEW_WIDTH_AND_SPANS = r'''def _span_width(font_dict, size, text):
    """Width of a piece of text in points, from the font's own glyph widths.

    The average of /Widths is useless for a subset font: it lists hundreds of
    zero entries for glyphs the font does not carry, which makes every line
    look three times shorter than it is. Zeros are ignored and each character
    is looked up by its own code instead.
    """
    total = None
    try:
        widths = font_dict.get("/Widths") if font_dict else None
        if widths:
            values = [float(v) for v in widths]
            good = sorted(v for v in values if v > 0)
            default = good[len(good) // 2] if good else 500.0
            first = int(font_dict.get("/FirstChar", 0))
            total = 0.0
            for ch in text:
                index = ord(ch) - first
                value = values[index] if 0 <= index < len(values) else 0.0
                total += value if value > 0 else default
    except Exception:
        total = None
    if total is None:
        total = 500.0 * len(text)
    return total / 1000.0 * size


def _page_spans(page):
    """Every drawn piece of text with position, real size and font name."""
    spans = []

    def visitor(text, cm, tm, font_dict, font_size):
        if not text or not text.strip():
            return
        try:
            a, b, c, d, e, f = [float(v) for v in cm]
            g, h, i, j, k, l = [float(v) for v in tm]
        except Exception:
            return
        scale_y = (g * b + h * d) or (i * b + j * d) or 1.0
        size = abs(float(font_size) * scale_y) or abs(float(font_size)) or 9.0
        if size < 2.0:
            return
        x = k * a + l * c + e
        y = k * b + l * d + f
        name = ""
        try:
            name = str((font_dict or {}).get("/BaseFont", ""))
        except Exception:
            pass
        clean = text.replace("\n", " ").replace("\r", " ")
        spans.append({"text": clean,
                      "x": float(x), "y": float(y), "size": float(size),
                      "w": _span_width(font_dict, float(size), clean),
                      "font": name})

    try:
        page.extract_text(visitor_text=visitor)
    except Exception:
        return []
    return spans


'''

NEW_GROUP = r'''def _group_blocks(spans, page_w=595.0):
    """Spans -> line segments -> blocks (paragraph / heading / table cell).

    Two rules keep tables and headings intact:
    * a horizontal gap wider than a word space starts a NEW cell, so two
      side-by-side tables never end up in one block;
    * a line only continues into the next one when it is a full prose line
      (15 em wide or more), so a heading or a short cell stays on its own.
    'x_room' is how far the text may reach to the right: up to the next cell
    on the same line, never further than two em past the original wording.
    """
    if len(spans) > MAX_SPANS_PER_PAGE:
        raise ToolError("too dense (%d text pieces)" % len(spans))
    ordered = sorted(spans, key=lambda s: (-s["y"], s["x"]))
    lines = []
    for span in ordered:
        for line in lines:
            if abs(line["y"] - span["y"]) <= max(
                    1.5, 0.35 * max(line["size"], span["size"])):
                line["parts"].append(span)
                break
        else:
            lines.append({"y": span["y"], "size": span["size"],
                          "parts": [span]})

    segments = []
    for line in lines:
        parts = sorted(line["parts"], key=lambda s: s["x"])
        current = [parts[0]]
        for span in parts[1:]:
            prev = current[-1]
            gap = span["x"] - (prev["x"] + prev["w"])
            if gap > max(4.0, 0.9 * line["size"]):
                segments.append(current)
                current = [span]
            else:
                current.append(span)
        segments.append(current)

    rows = []
    for parts in segments:
        main = max(parts, key=lambda s: s["size"])
        text = parts[0]["text"]
        for prev, span in zip(parts, parts[1:]):
            gap = span["x"] - (prev["x"] + prev["w"])
            sep = " " if gap > 0.25 * main["size"] else ""
            if sep and text.endswith(" "):
                sep = ""
            text += sep + span["text"]
        rows.append({"x0": min(s["x"] for s in parts),
                     "x1": max(s["x"] + s["w"] for s in parts),
                     "y": main["y"], "size": main["size"],
                     "text": re.sub(r"\s+", " ", text).strip(),
                     "font": main["font"]})
    rows = [r for r in rows if r["text"]]

    for row in rows:
        neighbours = [other["x0"] for other in rows
                      if other is not row
                      and abs(other["y"] - row["y"]) <= max(
                          1.5, 0.35 * row["size"])
                      and other["x0"] > row["x1"] - 0.5]
        limit = min(neighbours) - 2.0 if neighbours else page_w - 12.0
        row["room"] = max(row["x1"],
                          min(limit, row["x1"] + 2.0 * row["size"]))
    rows.sort(key=lambda r: (-r["y"], r["x0"]))

    blocks = []
    for row in rows:
        joined = False
        if blocks:
            block = blocks[-1]
            last = block["lines"][-1]
            step = last["y"] - row["y"]
            size = block["size"]
            bullet = row["text"].lstrip().startswith(BULLET_STARTS)
            same_x = abs(row["x0"] - block["x0"]) <= 2.5
            indent = 0 < row["x0"] - block["x0"] <= 2.0 * row["size"]
            prose = (last["x1"] - last["x0"]) >= 15.0 * last["size"]
            if (not bullet
                    and prose
                    and (same_x or indent)
                    and row["x0"] < block["x1"] + 2.0
                    and 0.55 * size <= step <= 1.5 * size
                    and 0.8 <= row["size"] / size <= 1.25):
                block["lines"].append(row)
                block["x1"] = max(block["x1"], row["x1"])
                joined = True
        if not joined:
            blocks.append({"x0": row["x0"], "x1": row["x1"],
                           "size": row["size"], "font": row["font"],
                           "lines": [row]})

    result = []
    for number, block in enumerate(blocks, 1):
        lines = block["lines"]
        size = block["size"]
        steps = [a["y"] - b["y"] for a, b in zip(lines, lines[1:])]
        if steps:
            steps.sort()
            leading = steps[len(steps) // 2] / size
            leading = min(1.45, max(1.05, leading))
        else:
            leading = 1.2
        result.append({"id": "b%d" % number,
                       "x0": round(block["x0"], 2),
                       "x1": round(block["x1"], 2),
                       "x_room": round(max(l["room"] for l in lines), 2),
                       "first_y": round(lines[0]["y"], 2),
                       "y_bot": round(lines[-1]["y"] - 0.30 * size, 2),
                       "size": round(size, 2),
                       "leading": round(leading, 3),
                       "font": block["font"],
                       "text": "\n".join(l["text"] for l in lines)})
    if len(result) > MAX_BLOCKS_PER_PAGE:
        raise ToolError("too many text blocks (%d)" % len(result))
    return result


'''

NEW_FIT = r'''def _fit_block(block, text, font, min_scale):
    """Word wrap into the original rectangle; shrink the font if needed."""
    right = float(block.get("x_room", block["x1"]))
    width = max(right - block["x0"] + 2.0, 8.0)
    size0 = block["size"]
    floor = max(5.0, size0 * min_scale)
    size = size0
    while True:
        lines = _wrap(text, font, size, width)
        step = size * block["leading"]
        bottom = block["first_y"] - (len(lines) - 1) * step - 0.30 * size
        overflow = bottom < block["y_bot"] - 1.5
        if not overflow or size <= floor + 1e-6:
            return lines, size, overflow
        size = max(floor, size - 0.25)


'''

with io.open(PATH, "r", encoding="utf-8-sig") as stream:
    src = stream.read()

if "_span_width" in src:
    print("already patched - nothing to do")
    py_compile.compile(PATH, doraise=True)
    print("COMPILE OK")
    raise SystemExit(0)

spots = []
for marker in (M1, M2, M3, M4, M5):
    n = src.count(marker)
    print("marker %-46s x%d" % (marker[:46], n))
    if n != 1:
        raise SystemExit("STOP: marker not unique, file NOT changed")
    spots.append(src.index(marker))
if spots != sorted(spots):
    raise SystemExit("STOP: markers out of order, file NOT changed")
i1, i2, i3, i4, i5 = spots

calls = [k for k, line in enumerate(src.split("\n"))
         if line.strip() == "found = _group_blocks(spans)"]
print("call sites:", len(calls), "(expected 1)")
if len(calls) != 1:
    raise SystemExit("STOP: call site not unique, file NOT changed")

shutil.copyfile(PATH, BACKUP)
new = src[:i1] + NEW_WIDTH_AND_SPANS + NEW_GROUP + src[i3:i4] + NEW_FIT + src[i5:]
lines = new.split("\n")
k = [i for i, line in enumerate(lines)
     if line.strip() == "found = _group_blocks(spans)"][0]
pad = lines[k][:len(lines[k]) - len(lines[k].lstrip())]
lines[k] = pad + "found = _group_blocks(spans, float(page.mediabox.width))"
new = "\n".join(lines)

with io.open(PATH, "w", encoding="utf-8", newline="\n") as stream:
    stream.write(new)
py_compile.compile(PATH, doraise=True)
print("backup ->", BACKUP)
print("lines: %d -> %d" % (len(src.split("\n")), len(new.split("\n"))))
print("COMPILE OK")