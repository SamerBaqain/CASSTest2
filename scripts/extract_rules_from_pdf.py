#!/usr/bin/env python3
"""
CASS extractor with strong de-duplication:
- Header patterns: "CASS 1A.3.1G", "1A.3.1 G", "1.2.1 R — Title"
- Normalises type: R stays R; G / E / BG -> G (Guidance). Others -> G.
- De-duplicates strictly by (id, type), keeping the longest text.
Result fields:
  id: "1.1.1"         (numeric only)
  type: "R"|"G"       (normalised)
  display: "CASS 1.1.1R"  (for UI)
"""

import argparse, pathlib, re, sys
from typing import Dict, List, Optional
import pdfplumber, yaml
import regex as rxx

# ---- noise cleanup ----
PAGE_FOOTER_RE = re.compile(r"^\s*(Page\s+\d+\s+of\s+\d+|FCA\s+\d{4}/\d+|www\.handbook\.fca\.org\.uk.*)$", re.I)
BLANKISH_RE = re.compile(r"^\s*$")
HYPHEN_LINEBREAK_RE = rxx.compile(r"(\p{Letter})-\n(\p{Letter})", rxx.UNICODE)
TYPE_LINE_RE = re.compile(r"^\s*([A-Z]{1,3})\s*$")  # solitary type line

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

# ---- header detection ----
CLAUSE_RE = re.compile(
    r"""^\s*(?:CASS\s*)?
        (?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)\.(?P<rule>\d+)
        (?P<typesuf>[A-Z]{1,3})?
        (?:\s+|(?:\s*[-–—:]\s*))?
        (?P<title>.*)?\s*$""",
    re.VERBOSE,
)
CLAUSE_SPLITTYPE_RE = re.compile(
    r"""^\s*(?:CASS\s*)?
        (?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)\.(?P<rule>\d+)
        \s+(?P<typesuf>[A-Z]{1,3})
        (?:\s+|(?:\s*[-–—:]\s*))?
        (?P<title>.*)?\s*$""",
    re.VERBOSE,
)
SECTION_RE = re.compile(
    r"""^\s*(?:CASS\s*)?
        (?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)
        (?:\s*[-–—:]\s*.+)?\s*$""",
    re.VERBOSE,
)

def norm_type(t: Optional[str]) -> Optional[str]:
    if not t: return None
    t = t.upper()
    if t == "R": return "R"
    # Treat all non-R (G, E, BG, etc.) as Guidance for your use-case
    return "G"

def match_clause(line: str):
    m = CLAUSE_RE.match(line) or CLAUSE_SPLITTYPE_RE.match(line)
    if not m: return None
    gd = m.groupdict()
    rid = f"{gd['chapter']}.{gd['section']}.{gd['rule']}"
    typesuf = norm_type((gd.get("typesuf") or "").strip() or None)
    title = (gd.get("title") or "").strip() or None
    return {"id": rid, "chapter": gd["chapter"], "type": typesuf, "title": title}

def is_section_banner(line: str) -> bool:
    if CLAUSE_RE.match(line) or CLAUSE_SPLITTYPE_RE.match(line): return False
    return bool(SECTION_RE.match(line))

def parse_rules(all_lines: List[str]) -> List[Dict]:
    rules: List[Dict] = []
    current: Optional[Dict] = None
    buf: List[str] = []
    just_opened = False

    def flush():
        nonlocal current, buf
        if current:
            body = "\n".join(buf).strip()
            current["text"] = body
            # display = numeric id + type (R/G) if present
            suff = current["type"] or ""
            current["display"] = f"CASS {current['id']}{suff}"
            rules.append(current)
        current, buf = None, []

    for raw in all_lines:
        line = raw.rstrip()

        m = match_clause(line)
        if m:
            flush()
            current = {
                "id": m["id"],
                "chapter": m["chapter"],
                "type": m["type"],          # may be None now
                "title": m["title"],
                "summary": None,
                "risk_ids": [],
                "default_control_ids": [],
                "applicability_conditions": None,
            }
            buf = []
            just_opened = True
            continue

        if is_section_banner(line):
            flush()
            just_opened = False
            continue

        # Sometimes type is on its own line immediately after header
        if just_opened:
            mt = TYPE_LINE_RE.match(line)
            if mt:
                current["type"] = norm_type(mt.group(1))
                just_opened = False
                continue
            just_opened = False

        if BLANKISH_RE.match(line):
            if buf and buf[-1] != "": buf.append("")
            continue

        if current is None: continue
        buf.append(line)

    flush()

    # ---- strong de-duplication: unique by (id, type) ----
    dedup: Dict[str, Dict] = {}
    for r in rules:
        key = f"{r['id']}|{r.get('type') or 'G'}"  # default missing types to Guidance
        # Prefer an entry that has a type; prefer longer text if both typed
        has_type_existing = bool(dedup.get(key, {}).get("type"))
        has_type_new = bool(r.get("type"))
        if key not in dedup:
            dedup[key] = r
        else:
            if (has_type_new and not has_type_existing) or \
               (len(r.get("text","")) > len(dedup[key].get("text",""))):
                dedup[key] = r

    def sort_key(r):
        chap = r["chapter"]
        chap_num = int("".join(c for c in chap if c.isdigit()) or 0)
        chap_suf = "".join(c for c in chap if c.isalpha())
        a,b,c = r["id"].split(".")
        t = r.get("type") or "G"
        t_rank = 0 if t=="R" else 1
        return (chap_num, chap_suf, int(b), int(c), t_rank)

    out = sorted(dedup.values(), key=sort_key)

    # Ensure every entry has a normalised type; default to G if missing
    for r in out:
        r["type"] = r.get("type") or "G"
        r["display"] = f"CASS {r['id']}{r['type']}"
    return out

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
        lines.extend(extract_lines(path)); lines.append("")

    rules = parse_rules(lines)
    if not rules:
        print("[error] no rules detected. If PDFs are scanned, OCR them first.", file=sys.stderr); sys.exit(1)

    out = pathlib.Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        yaml.safe_dump(rules, f, sort_keys=False, allow_unicode=True)
    print(f"[ok] wrote {len(rules)} rules -> {out}")

if __name__ == "__main__":
    main()
