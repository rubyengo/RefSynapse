# dev/diag_ssd.py — диагностика: ссылки, текст на картинках, цвет текста.
import sys, os, glob
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pypdf import PdfReader
from pypdf.generic import ContentStream, ArrayObject, NameObject, DecodedStreamObject

IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

def mm(a, b):
    return (a[0]*b[0]+a[1]*b[2], a[0]*b[1]+a[1]*b[3],
            a[2]*b[0]+a[3]*b[2], a[2]*b[1]+a[3]*b[3],
            a[4]*b[0]+a[5]*b[2]+b[4], a[4]*b[1]+a[5]*b[3]+b[5])

def tr(m, x, y):
    return (x*m[0]+y*m[2]+m[4], x*m[1]+y*m[3]+m[5])

def pick():
    cands = glob.glob(os.path.join("agent", "MakeUseOf*Solid*State*Drives*.pdf"))
    src = [p for p in cands if "_ru" not in os.path.basename(p).lower()]
    res = [p for p in cands if "_ru" in os.path.basename(p).lower()]
    print("candidates:", [os.path.basename(p) for p in cands])
    if not src or not res:
        print("FILES NOT FOUND"); sys.exit(1)
    return sorted(src, key=os.path.getmtime)[-1], sorted(res, key=os.path.getmtime)[-1]

def stream_bytes(page):
    c = page.get("/Contents")
    if c is None: return b""
    c = c.get_object()
    if isinstance(c, ArrayObject):
        return b"\n".join(s.get_object().get_data() for s in c)
    return c.get_data()

def parse(data, pdf):
    s = DecodedStreamObject(); s.set_data(data)
    return ContentStream(s, pdf).operations

def resources_of(obj, parent):
    r = obj.get("/Resources")
    return r.get_object() if r is not None else parent

def walk(data, pdf, res, ctm, depth, images, runs, seen):
    if depth > 8: return
    try: ops = parse(data, pdf)
    except Exception as e:
        print("  parse error:", e); return
    stack = []
    fill = ("rgb", 0.0, 0.0, 0.0); space = "/DeviceGray"
    size = 10.0; tm = IDENT; tlm = IDENT; tl = 0.0; tz = 100.0; trmode = 0
    in_bt = False
    def ckey(vals):
        sp = str(space); v = [float(x) for x in vals]
        if sp.endswith("Gray") and len(v) >= 1: return ("rgb", v[0], v[0], v[0])
        if sp.endswith("RGB") and len(v) >= 3: return ("rgb", v[0], v[1], v[2])
        if sp.endswith("CMYK") and len(v) >= 4:
            c, m, y, k = v[:4]
            return ("rgb", 1-min(1, c*(1-k)+k), 1-min(1, m*(1-k)+k), 1-min(1, y*(1-k)+k))
        return ("special", sp, tuple(v))
    for operands, op in ops:
        txt = ""
        if op == b"q": stack.append((ctm, fill, space, size))
        elif op == b"Q":
            if stack: ctm, fill, space, size = stack.pop()
        elif op == b"cm": ctm = mm(tuple(float(x) for x in operands), ctm)
        elif op == b"cs": space = str(operands[0])
        elif op == b"g":
            v = float(operands[0]); fill = ("rgb", v, v, v)
        elif op == b"rg": fill = ("rgb",) + tuple(float(x) for x in operands[:3])
        elif op == b"k":
            c, m, y, k = [float(x) for x in operands[:4]]
            fill = ("rgb", 1-min(1, c*(1-k)+k), 1-min(1, m*(1-k)+k), 1-min(1, y*(1-k)+k))
        elif op in (b"sc", b"scn"):
            if operands and isinstance(operands[0], NameObject): fill = ("pattern", str(operands[0]))
            else: fill = ckey(operands)
        elif op == b"BT": in_bt = True; tm = IDENT; tlm = IDENT
        elif op == b"ET": in_bt = False
        elif op == b"Tf": size = float(operands[1])
        elif op == b"TL": tl = float(operands[0])
        elif op == b"Tz": tz = float(operands[0])
        elif op == b"Tr": trmode = int(operands[0])
        elif op == b"Tm": tm = tlm = tuple(float(x) for x in operands)
        elif op == b"Td":
            tlm = mm((1, 0, 0, 1, float(operands[0]), float(operands[1])), tlm); tm = tlm
        elif op == b"TD":
            tx, ty = float(operands[0]), float(operands[1]); tl = -ty
            tlm = mm((1, 0, 0, 1, tx, ty), tlm); tm = tlm
        elif op == b"T*":
            tlm = mm((1, 0, 0, 1, 0, -tl), tlm); tm = tlm
        elif op in (b"Tj", b"TJ", b"'", b'"'):
            if op in (b"'", b'"'):
                tlm = mm((1, 0, 0, 1, 0, -tl), tlm); tm = tlm
            if op == b"TJ":
                txt = "".join(str(x) for x in operands[0] if not isinstance(x, (int, float)))
            else:
                txt = str(operands[2 if op == b'"' else 0])
            if in_bt and trmode != 3 and txt.strip():
                m = mm(tm, ctm)
                runs.append((m[4], m[5], size*tz/100.0, fill, txt))
            tm = mm((1, 0, 0, 1, 0.5*size*max(1, len(txt)), 0), tm)
        elif op == b"Do":
            xo = (res or {}).get("/XObject")
            if xo is None: continue
            ref = xo.get_object().get(str(operands[0]))
            if ref is None: continue
            obj = ref.get_object(); st = str(obj.get("/Subtype"))
            if st == "/Image":
                pts = [tr(ctm, x, y) for x, y in ((0, 0), (1, 0), (0, 1), (1, 1))]
                xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                images.append((min(xs), min(ys), max(xs), max(ys)))
            elif st == "/Form":
                key = id(obj)
                if key in seen: continue
                seen.add(key)
                fm = obj.get("/Matrix")
                fmat = tuple(float(x) for x in fm) if fm else IDENT
                walk(obj.get_data(), pdf, resources_of(obj, res), mm(fmat, ctm),
                     depth+1, images, runs, seen)

def text_lines(page):
    lines = []
    def v(text, cm, tm, fd, fs):
        if not text.strip(): return
        m = mm(tuple(tm), tuple(cm)); size = float(fs or 10)
        w = 0.55*size*len(text)
        lines.append((text, (m[4], m[5]-0.3*size, m[4]+w, m[5]+size)))
    try: page.extract_text(visitor_text=v)
    except Exception as e: print("  visitor error:", e)
    return lines

def annots(page):
    a = page.get("/Annots")
    if a is None: return []
    out = []
    for ref in a.get_object():
        o = ref.get_object()
        rect = tuple(round(float(x), 1) for x in o.get("/Rect", (0, 0, 0, 0)))
        uri = None; dest = None
        act = o.get("/A")
        if act is not None:
            act = act.get_object()
            if str(act.get("/S")) == "/URI": uri = str(act.get("/URI"))
            else: dest = "A:" + str(act.get("/D"))[:40]
        if o.get("/Dest") is not None: dest = "D:" + str(o.get("/Dest"))[:40]
        out.append((str(o.get("/Subtype")), rect, uri, dest))
    return out

def near_black(c):
    return c[0] == "rgb" and max(c[1:]) < 0.05

def cdiff(a, b):
    if a[0] != b[0]: return True
    if a[0] != "rgb": return a != b
    return max(abs(x-y) for x, y in zip(a[1:], b[1:])) > 0.03

def main():
    src, res = pick()
    print("SRC:", src); print("RES:", res)
    ro, rr = PdfReader(src), PdfReader(res)
    print("pages:", len(ro.pages), len(rr.pages))
    tot_missing = tot_empty = tot_overlap = tot_color = 0
    for i, (po, pr) in enumerate(zip(ro.pages, rr.pages), 1):
        issues = []
        ao, ar = annots(po), annots(pr)
        keys_r = [(s, u, d) for s, r, u, d in ar]
        missing = []
        for s, r, u, d in ao:
            k = (s, u, d)
            if k in keys_r: keys_r.remove(k)
            else: missing.append((s, r, u, d))
        for s, r, u, d in missing:
            issues.append("ANNOTS MISSING %s rect=%s uri=%s dest=%s" % (s, r, u, d))
        lines = text_lines(pr)
        for s, r, u, d in ar:
            if s != "/Link": continue
            hit = any(b[0] < r[2]+1 and b[2] > r[0]-1 and b[1] < r[3]+1 and b[3] > r[1]-1
                      for t, b in lines)
            if not hit:
                issues.append("ANNOTS EMPTY RECT %s uri=%s dest=%s" % (r, u, d))
        img_o, run_o, img_r, run_r = [], [], [], []
        walk(stream_bytes(po), ro, resources_of(po, None), IDENT, 0, img_o, run_o, set())
        walk(stream_bytes(pr), rr, resources_of(pr, None), IDENT, 0, img_r, run_r, set())
        for t, b in lines:
            for r in img_r:
                ix = min(b[2], r[2]) - max(b[0], r[0])
                iy = min(b[3], r[3]) - max(b[1], r[1])
                if ix > 1 and iy > 1:
                    issues.append("TEXT ON IMAGE (%.0fx%.0f pt) %r" % (ix, iy, t[:60]))
        for x, y, sz, col, txt in run_o:
            best = None; bd = 1e9
            for x2, y2, sz2, col2, t2 in run_r:
                if abs(y2-y) > 4 or abs(x2-x) > 8: continue
                if not (0.4*sz <= sz2 <= 2.5*sz): continue
                d = (x2-x)**2 + (y2-y)**2
                if d < bd: bd = d; best = (col2, t2)
            if best is None:
                if not near_black(col):
                    issues.append("COLOR LOST? orig=%s at(%.0f,%.0f) %r" % (col, x, y, txt[:40]))
            elif cdiff(col, best[0]):
                issues.append("COLOR DIFF orig=%s new=%s at(%.0f,%.0f) %r" % (col, best[0], x, y, txt[:40]))
        nm = len(missing)
        ne = sum(1 for s in issues if s.startswith("ANNOTS EMPTY"))
        no = sum(1 for s in issues if s.startswith("TEXT ON IMAGE"))
        nc = sum(1 for s in issues if s.startswith("COLOR"))
        tot_missing += nm; tot_empty += ne; tot_overlap += no; tot_color += nc
        if issues:
            print("page %d: annots %d->%d, images %d" % (i, len(ao), len(ar), len(img_r)))
            for s in issues[:24]: print("   ", s)
        else:
            print("page %d: ok (annots %d, images %d)" % (i, len(ao), len(img_r)))
    print("SUMMARY missing_annots=%d empty_link_rects=%d text_on_image=%d color_issues=%d"
          % (tot_missing, tot_empty, tot_overlap, tot_color))

main()