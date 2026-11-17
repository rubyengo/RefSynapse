"""Install accurate font metrics + table-aware grouping."""
import io, py_compile, shutil

PATH = r"refsynapse\ai\board_pdf_translate_tools.py"
BACKUP = r"agent\board_pdf_translate_tools.before_metrics.py"
M1 = "def _span_width(font_dict, size, text):"
M2 = "def _forms_have_text(page):"

NEW = r'''_METRICS = {}


def _font_metrics(font_dict):
    """letter -> advance (1/1000 em) for one font, plus a fallback advance.

    Simple fonts carry /Widths directly. Composite (Type0) fonts keep their
    widths in the descendant font, addressed by CID, and only the ToUnicode
    table links those CIDs back to letters. Without that link every line had
    to be guessed at half an em per character, which made paragraphs look a
    third wider than they are and glued neighbouring table cells together.
    """
    try:
        name = str(font_dict.get("/BaseFont", "")) or str(id(font_dict))
    except Exception:
        name = str(id(font_dict))
    if name in _METRICS:
        return _METRICS[name]
    table, values = {}, []
    try:
        widths = font_dict.get("/Widths")
        if widths:
            first = int(font_dict.get("/FirstChar", 0))
            for index, value in enumerate(widths):
                value = float(value)
                if value > 0:
                    table[chr(first + index)] = value
                    values.append(value)
        else:
            descendants = font_dict.get("/DescendantFonts")
            child = descendants[0].get_object() if descendants else None
            if child is not None:
                advances = {}
                raw = [x.get_object() if hasattr(x, "get_object") else x
                       for x in child.get("/W", [])]
                index = 0
                while index < len(raw):
                    if index + 1 < len(raw) and isinstance(raw[index + 1],
                                                           (list, tuple)):
                        for offset, value in enumerate(raw[index + 1]):
                            advances[int(raw[index]) + offset] = float(value)
                        index += 2
                    elif index + 2 < len(raw):
                        for cid in range(int(raw[index]),
                                         int(raw[index + 1]) + 1):
                            advances[cid] = float(raw[index + 2])
                        index += 3
                    else:
                        break
                values = [v for v in advances.values() if v > 0]
                try:
                    data = (font_dict["/ToUnicode"].get_object()
                            .get_data().decode("latin-1"))
                except Exception:
                    data = ""
                for chunk in re.findall(r"beginbfchar(.*?)endbfchar", data, re.S):
                    for src, dst in re.findall(
                            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", chunk):
                        cid = int(src, 16)
                        if cid not in advances or len(dst) % 4:
                            continue
                        try:
                            letter = "".join(chr(int(dst[k:k + 4], 16))
                                             for k in range(0, len(dst), 4))
                        except ValueError:
                            continue
                        if len(letter) == 1:
                            table[letter] = advances[cid]
                for chunk in re.findall(r"beginbfrange(.*?)endbfrange",
                                        data, re.S):
                    for lo, hi, dst in re.findall(
                            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>"
                            r"\s*<([0-9A-Fa-f]+)>", chunk):
                        first, last, base = int(lo, 16), int(hi, 16), int(dst, 16)
                        for cid in range(first, min(last, first + 255) + 1):
                            code = base + cid - first
                            if cid in advances and 0 <= code <= 0x10FFFF:
                                table[chr(code)] = advances[cid]
    except Exception:
        table, values = {}, []
    good = sorted(v for v in values if v > 0)
    default = good[len(good) // 2] if good else 500.0
    _METRICS[name] = (table, default)
    return _METRICS[name]


def _span_width(font_dict, size, text):
    """Width of a piece of text in points, from the font's own advances."""
    table, default = _font_metrics(font_dict)
    total = 0.0
    for ch in text:
        total += table.get(ch, default)
    return total / 1000.0 * size


def _page_spans(page):
    """Every drawn piece of text with position, real size and font name.

    Refuses a page whose text cannot be measured completely: such text would
    be removed from the working copy without being redrawn, i.e. lost.
    """
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
        whole = page.extract_text(visitor_text=visitor) or ""
    except Exception:
        return []
    if spans:
        wanted = len(re.sub(r"\s", "", whole))
        got = sum(len(re.sub(r"\s", "", s["text"])) for s in spans)
        if wanted and got < 0.85 * wanted:
            raise ToolError("too hard to measure (only %d%% of its text was "
                            "located)" % int(100.0 * got / wanted))
    return spans


def _style(font_name):
    """(bold, italic) - a heading must never melt into the paragraph below."""
    name = str(font_name).lower()
    bold = any(t in name for t in ("bold", "black", "heavy", "semibold"))
    italic = "italic" in name or "oblique" in name
    return bold, italic


def _group_blocks(spans, page_w=595.0):
    """Spans -> line segments -> blocks (paragraph / heading / table cell).

    Three rules keep the layout honest:
    * an x position where several lines begin is a table column, so a new
      cell starts there even when the gap to the previous cell is tiny;
    * a line only continues into the next one when it is a full prose line
      of the same weight, so headings and cells stay on their own;
    * 'x_room' is how far the wording may reach to the right - up to the next
      column, never past the widest original line, never off the page.
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

    starts = {}
    for line in lines:
        for x in {round(p["x"], 1) for p in line["parts"]}:
            starts[x] = starts.get(x, 0) + 1
    columns = {x for x, count in starts.items() if count >= 2}

    segments = []
    for line in lines:
        parts = sorted(line["parts"], key=lambda s: s["x"])
        current = [parts[0]]
        for span in parts[1:]:
            prev = current[-1]
            gap = span["x"] - (prev["x"] + prev["w"])
            if (gap > max(4.0, 0.9 * line["size"])
                    or (round(span["x"], 1) in columns and gap > -2.0)):
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
    if not rows:
        return []

    right = max(r["x1"] for r in rows)
    for row in rows:
        neighbours = [other["x0"] for other in rows
                      if other is not row
                      and abs(other["y"] - row["y"]) <= max(
                          1.5, 0.35 * row["size"])
                      and other["x0"] > row["x1"] - 0.5]
        limit = min(neighbours) - 2.0 if neighbours else page_w - 8.0
        row["room"] = max(row["x1"],
                          min(limit, row["x1"] + 2.0 * row["size"],
                              right + 2.0, page_w - 4.0))
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
                    and _style(row["font"]) == _style(block["font"])
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

with io.open(PATH, "r", encoding="utf-8-sig") as stream:
    src = stream.read()
if "_font_metrics" in src:
    print("already patched - nothing to do")
    py_compile.compile(PATH, doraise=True)
    print("COMPILE OK")
    raise SystemExit(0)
for marker in (M1, M2):
    n = src.count(marker)
    print("marker %-42s x%d" % (marker[:42], n))
    if n != 1:
        raise SystemExit("STOP: marker not unique, file NOT changed")
i1, i2 = src.index(M1), src.index(M2)
if i1 >= i2:
    raise SystemExit("STOP: markers out of order, file NOT changed")
shutil.copyfile(PATH, BACKUP)
new = src[:i1] + NEW + src[i2:]
with io.open(PATH, "w", encoding="utf-8", newline="\n") as stream:
    stream.write(new)
py_compile.compile(PATH, doraise=True)
print("backup ->", BACKUP)
print("lines: %d -> %d" % (len(src.split("\n")), len(new.split("\n"))))
print("COMPILE OK")