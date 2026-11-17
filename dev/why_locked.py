r"""Why are so many blocks locked? Read the job and report the offending codes.

Reads agent\translate\manual_en\job.json (already built), changes nothing,
opens no window. Run from the repo root:
    & ".\python\python.exe" -u dev\why_locked.py
"""
import collections
import json
import os
import re

JOB = os.path.join("agent", "translate", "manual_en", "job.json")
PAGES = (5, 9, 12, 17, 20)
LETTERS = re.compile(r"[A-Za-z\u0400-\u04FF]")


def safe(value):
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


def suspicious(text):
    out = []
    for ch in text:
        code = ord(ch)
        if (code < 32 or 0x80 <= code <= 0x9F
                or 0xE000 <= code <= 0xF8FF
                or code in (0xFFFD, 0x25A1, 0x25A0)):
            out.append(code)
    return out


def main():
    if not os.path.isfile(JOB):
        raise SystemExit("No job file: %s - run dev\\prep_manual.py first" % JOB)
    with open(JOB, encoding="utf-8") as stream:
        job = json.load(stream)
    print("job version:", job.get("version"))
    codes = collections.Counter()
    report = {}
    total_ready = total_locked_letters = 0
    for number in range(1, job["pages_total"] + 1):
        key = str(number)
        ready = job["blocks"].get(key, [])
        locked = job["locked"].get(key, [])
        rc = sum(len(b["text"]) for b in ready)
        lc = sum(len(b["text"]) for b in locked if LETTERS.search(b["text"]))
        total_ready += rc
        total_locked_letters += lc
        report[key] = {"mode": job["mode"].get(key),
                       "ready_chars": rc, "locked_with_letters": lc,
                       "locked": []}
        for block in locked:
            text = block["text"]
            bad = suspicious(text)
            if LETTERS.search(text):
                codes.update(bad)
                report[key]["locked"].append(
                    {"id": block["id"], "chars": len(text),
                     "codes": sorted(set(bad)), "text": text[:120]})
    print("offered chars:", total_ready)
    print("locked chars that DO contain letters:", total_locked_letters)
    print("most common suspicious codes:",
          [(hex(c), n) for c, n in codes.most_common(8)])
    for number in PAGES:
        info = report.get(str(number))
        if not info:
            continue
        print("--- page %d (%s): offered %d, locked-with-letters %d"
              % (number, info["mode"], info["ready_chars"],
                 info["locked_with_letters"]))
        for item in info["locked"][:4]:
            print("   %s chars=%d codes=%s"
                  % (item["id"], item["chars"], [hex(c) for c in item["codes"]]))
            print("      %s" % safe(item["text"][:90]))
    with open(os.path.join("dev", "locked_report.json"), "w",
              encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=1)
    print("report -> dev\\locked_report.json")


if __name__ == "__main__":
    main()