#!/usr/bin/env python3
"""
CASS extractor (strict, type-safe, lettered chapters supported)

Fixes:
- Distinguish 1.* vs 1A.* chapters (kept in id).
- Strip stray "R " / "G " that sometimes leak into titles.
- Unique per (id, type) with longest body kept.
- Treat everything non-R (G/E/BG/C…) as Guidance "G".
- Ignore TOC/footers and use section banners as hard boundaries.
"""

import argparse, pathlib, re, sys
from typing import Dict, List, Optional
import pdfplumber, yaml
import regex as rxx

# ---------- noise cleanup ----------
PAGE_FOOTER_RE = re.compile(r"^\s*(Page\s+\d+\s+of\s+\d+|www\.handbook\.fca\.org\.uk.*|FCA\s+\d{4}/\d+)$", re.I)
JUST_DOTS_RE = re.compile(r"^\s*[.\-–—]+\s*$")
BLANKISH_RE = re.compile(r"^\s*$")
URL_RE = re.compile(r"^\s*https?://", re.I)
HYPHEN_LINEBREAK_RE = rxx.compile(r"(\p{Letter})-\n(\p{Letter})", rxx.UNICODE)

def clean_page_text(raw: str) -> str:
    txt = (raw or "").replace("\r\n","\n").replace("\r","\n")
    txt = HYPHEN_LINEBREAK_RE.sub(r"\1\2", txt)
    lines = []
    for ln in txt.split("\n"):
        if PAGE_FOOTER_RE.match(ln): continue
        if URL_RE.match(ln): continue
        if JUST_DOTS_RE.match(ln): continue
        lines.append(ln)
    return "\n".join(lines)

def extract_lines(pdf_path: pathlib.Path) -> List[str]:
    lines: List[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for p in pdf.pages:
            t = p.extract_text() or ""
            if not t.strip(): continue
            lines.extend(clean_page_text(t).split("\n"))
    return lines

# ---------- header detection (REQUIRES "CASS " prefix) ----------
# chapter: digits + optional single letter (e.g., 1A, 7A)
# rule may have letter/hyphen suffix (e.g., 5A, 1-A)
RULE_ID_RE = r"(?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)\.(?P<rule>\d+(?:[A-Z]|-[A-Z])?)"

CLAUSE_RE = re.compile(
    rf"""^\s*CASS\s+{RULE_ID_RE}
        (?P<typesuf>[A-Z]{{1,3}})?
        (?:\s+|(?:\s*[-–—:]\s*))?
        (?P<title>.*)?\s*$""",
    re.VERBOSE,
)

CLAUSE_SPLITTYPE_RE = re.compile(
    rf"""^\s*CASS\s+{RULE_ID_RE}
        \s+(?P<typesuf>[A-Z]{{1,3}})
        (?:\s+|(?:\s*[-–—:]\s*))?
        (?P<title>.*)?\s*$""",
    re.VERBOSE,
)

SECTION_RE = re.compile(
    r"""^\s*Section\s*:\s*CASS\s+
        (?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)
        (?:\s*[-–—:]\s*.+)?\s*$""",
    re.VERBOSE | re.I,
)

SOLO_TYPE_RE = re.compile(r"^\s*(R|G|E|BG|C)\s*$", re.I)

def norm_type(t: Optional[str]) -> Optional[str]:
    if not t: return None
    t = t.upper()
    return "R" if t == "R" else "G"

def clean_title(title: Optional[str]) -> Optional[str]:
    if not title: return None
    t = title.strip()
    # remove any leading dash/colon
    t = re.sub(r"^\s*[-–—:]\s*", "", t)
    # if a type slipped into title ("R Application" / "G Purpose"), drop it
    t = re.sub(r"^(?:R|G|E|BG|C)\s+", "", t)
    # compress whitespace
    t = re.sub(r"\s+", " ", t).strip()
    return t or None

def match_clause(line: str):
    m = CLAUSE_RE.match(line) or CLAUSE_SPLITTYPE_RE.match(line)
    if not m: return None
    gd = m.groupdict()
    rid = f"{gd['chapter']}.{gd['section']}.{gd['rule']}"   # KEEP chapter letters (e.g., 1A)
    typesuf = norm_type((gd.get("typesuf") or "").strip() or None)
    title = clean_title(gd.get("title"))
    return {"id": rid, "chapter": gd["chapter"], "type": typesuf, "title": title}

def is_section_banner(line: str) -> bool:
    if CLAUSE_RE.match(line) or CLAUSE_SPLITTYPE_RE.match(line): return False
    return bool(SECTION_RE.match(line))

def harvest_type_from_body(lines: List[str]) -> Optional[str]:
    head = lines[:4]; tail = lines[-4:]
    for seg in (head + tail):
        m = SOLO_TYPE_RE.match(seg or "")
        if m: return norm_type(m.group(1))
    return None

def strip_type_lines(lines: List[str]) -> List[str]:
    return [ln for ln in lines if not SOLO_TYPE_RE.match(ln or "")]

def parse_rules(all_lines: List[str]) -> List[Dict]:
    rules: List[Dict] = []
    current: Optional[Dict] = None
    buf: List[str] = []

    def flush():
        nonlocal current, buf, rules
        if not current:
            buf = []; return
        # body prep
        body_lines = [ln for ln in buf if not BLANKISH_RE.match(ln)]
        if not current.get("type"):
            inferred = harvest_type_from_body(body_lines)
            if inferred: current["type"] = inferred
        body_lines = strip_type_lines(body_lines)
        body = "\n".join(body_lines).strip()

        # drop TOC ghosts (no type + tiny body)
        if not current.get("type") and len(body) < 80:
            current, buf = None, []; return

        current["type"] = current.get("type") or "G"
        current["text"] = body
        current["display"] = f"CASS {current['id']}{current['type']}"
        rules.append(current)
        current, buf = None, []

    for raw in all_lines:
        line = raw.rstrip()

        m = match_clause(line)
        if m:
            flush()
            current = {
                "id": m["id"],
                "chapter": m["chapter"],  # includes 'A' when present
                "type": m["type"],
                "title": m["title"],
                "summary": None,
                "risk_ids": [],
                "default_control_ids": [],
                "applicability_conditions": None,
            }
            buf = []
            continue

        if is_section_banner(line):
            flush(); continue

        if not current: continue
        if URL_RE.match(line) or PAGE_FOOTER_RE.match(line) or JUST_DOTS_RE.match(line): continue
        buf.append(line)

    flush()

    # ---- unique by (id, type) ----
    dedup: Dict[str, Dict] = {}
    for r in rules:
        key = f"{r['id']}|{r.get('type') or 'G'}"
        exist = dedup.get(key)
        if not exist:
            dedup[key] = r; continue
        # prefer typed, else longer body
        if (r.get("type") and not exist.get("type")) or (len(r.get("text","")) > len(exist.get("text",""))):
            dedup[key] = r

    def sort_key(r):
        # sort but DO NOT lose lettered chapters
        chap = r["chapter"]                        # e.g., "1A" or "1"
        chap_num = int("".join(c for c in chap if c.isdigit()) or 0)
        chap_suf = "".join(c for c in chap if c.isalpha())     # "A" or ""
        a,b,c = r["id"].split(".")
        # rule may have letters/hyphen suffix (e.g., 5A or 1-A)
        rule_num = int("".join(ch for ch in c if ch.isdigit()) or 0)
        rule_suf = "".join(ch for ch in c if not ch.isdigit())
        t_rank = 0 if (r.get("type") == "R") else 1
        return (chap_num, chap_suf, int(b), rule_num, rule_suf, t_rank)

    out = sorted(dedup.values(), key=sort_key)

    # final normalisation
    for r in out:
        r["type"] = r.get("type") or "G"
        r["title"] = clean_title(r.get("title"))
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
