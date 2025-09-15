#!/usr/bin/env python3
"""
Robust CASS extractor: PDFs -> data/rules.yaml

Recognises clause headers such as:
  CASS 1A.3.1G
  1A.3.1 G
  1.2.1 R — Title text
Also recognises section banners like:
  CASS 7A.2 — [section title]
and uses them as boundaries (but they don't create rule entries).
"""

import argparse, pathlib, re, sys
from typing import Dict, List, Optional
import pdfplumber, yaml
import regex as rxx

# Remove common footer/header noise
PAGE_FOOTER_RE = re.compile(r"^\s*(Page\s+\d+\s+of\s+\d+|FCA\s+\d{4}/\d+|www\.handbook\.fca\.org\.uk.*)$", re.I)
BLANKISH_RE = re.compile(r"^\s*$")
HYPHEN_LINEBREAK_RE = rxx.compile(r"(\p{Letter})-\n(\p{Letter})", rxx.UNICODE)

def clean_page_text(raw: str) -> str:
    txt = (raw or "").replace("\r\n","\n").replace("\r","\n")
    txt = HYPHEN_LINEBREAK_RE.sub(r"\1\2", txt)
    lines = [ln for ln in txt.split("\n") if not PAGE_FOOTER_RE.match(ln)]
    return "\n".join(lines)

def extract_lines(pdf_path: pathlib.Path) -> List[str]:
    lines: List[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for p in pdf.pages:
            t = p.extract_text() or ""
            if not t.strip():
                continue
            lines.extend(clean_page_text(t).split("\n"))
    return lines

# -------- Header detection --------
# 1) Full clause header with optional inline type suffix and optional title
CLAUSE_RE = re.compile(
    r"""^\s*
    (?:CASS\s*)?
    (?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)\.(?P<rule>\d+)
    (?P<typesuf>[A-Z]{1,3})?                # e.g., G, R, E, BG
    (?:\s+|(?:\s*[-–—:]\s*))?
    (?P<title>.*)?                          # optional title
    \s*$""",
    re.VERBOSE,
)

# 2) Variant where type is separated by a space (e.g., "1.2.1 R  Title")
CLAUSE_SPLITTYPE_RE = re.compile(
    r"""^\s*
    (?:CASS\s*)?
    (?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)\.(?P<rule>\d+)
    \s+(?P<typesuf>[RGE]|BG)\b
    (?:\s+|(?:\s*[-–—:]\s*))?
    (?P<title>.*)?
    \s*$""",
    re.VERBOSE,
)

# 3) Section banner like "CASS 1A.3 — ..." (use as boundary; no rule created)
SECTION_RE = re.compile(
    r"""^\s*
    (?:CASS\s*)?
    (?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)
    (?:\s*[-–—:]\s*.+)?
    \s*$""",
    re.VERBOSE,
)

def match_clause(line: str):
    m = CLAUSE_RE.match(line) or CLAUSE_SPLITTYPE_RE.match(line)
    if not m:
        return None
    gd = m.groupdict()
    rid = f"{gd['chapter']}.{gd['section']}.{gd['rule']}"
    typesuf = (gd.get("typesuf") or "").strip() or None
    title = (gd.get("title") or "").strip() or None
    return {"id": rid, "chapter": gd["chapter"], "type": typesuf, "title": title}

def is_section_banner(line: str) -> bool:
    # Ensure it's not a clause masquerading as a section (i.e., has .rule)
    if CLAUSE_RE.match(line) or CLAUSE_SPLITTYPE_RE.match(line):
        return False
    return bool(SECTION_RE.match(line))

def parse_rules(all_lines: List[str]) -> List[Dict]:
    rules: List[Dict] = []
    current: Optional[Dict] = None
    buf: List[str] = []

    def flush():
        nonlocal current, buf, rules
        if current:
            body = "\n".join(buf).strip()
            current["text"] = body
            current["display"] = f"CASS {current['id']}"
            rules.append(current)
        current, buf = None, []

    for raw in all_lines:
        line = raw.rstrip()

        # New clause header?
        m = match_clause(line)
        if m:
            flush()
            current = {
                "id": m["id"],
                "chapter": m["chapter"],
                "type": m["type"],          # R / G / E / BG / None
                "title": m["title"],
                "summary": None,
                "risk_ids": [],
                "default_control_ids": [],
                "applicability_conditions": None,
            }
            buf = []
            continue

        # Section banner acts as a boundary (end the previous clause)
        if is_section_banner(line):
            flush()
            continue

        # Normal content
        if BLANKISH_RE.match(line):
            if buf and buf[-1] != "":
                buf.append("")
            continue
        if current is None:
            # ignore preamble lines before first clause
            continue
        buf.append(line)

    flush()

    # De-duplicate by (id,type,title) keeping the longer text
    dedup: Dict[str, Dict] = {}
    for r in rules:
        key = f"{r['id']}|{r.get('type') or ''}|{r.get('title') or ''}"
        if key not in dedup or len(r.get("text","")) > len(dedup[key].get("text","")):
            dedup[key] = r

    def sort_key(r):
        chap = r["chapter"]
        chap_num = int("".join(c for c in chap if c.isdigit()) or 0)
        chap_suf = "".join(c for c in chap if c.isalpha())
        a,b,c = r["id"].split(".")
        return (chap_num, chap_suf, int(b), int(c))
    return sorted(dedup.values(), key=sort_key)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", help="Path(s) to CASS PDFs")
    ap.add_argument("--out", default="data/rules.yaml", help="Output YAML path")
    args = ap.parse_args()

    lines: List[str] = []
    for p in args.pdfs:
        path = pathlib.Path(p)
        if not path.exists():
            print(f"[warn] missing {path}", file=sys.stderr); continue
        print(f"[info] reading {path}", file=sys.stderr)
        lines.extend(extract_lines(path))
        lines.append("")  # separator

    rules = parse_rules(lines)
    if not rules:
        print("[error] no rules detected; if PDFs are scanned, OCR them first.", file=sys.stderr)
        sys.exit(1)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        yaml.safe_dump(rules, f, sort_keys=False, allow_unicode=True)
    print(f"[ok] wrote {len(rules)} rules -> {out}")

if __name__ == "__main__":
    main()
