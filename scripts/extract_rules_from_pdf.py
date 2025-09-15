#!/usr/bin/env python3
"""
CASS extractor (strict, R/G aware) – PDFs -> data/rules.yaml

Key behaviours
- Accept ONLY headings that START with "CASS " to avoid TOC/sidebars.
- Recognise headings like:
    "CASS 1.2.2", "CASS 1.2.2 R", "CASS 1.2.2R", "CASS 1.2.2 — Title",
    "CASS 1A.3.1G", "CASS 1A.3.1 G", "CASS 1.2.5A", "CASS 1A.3.1-A"
- Within each clause body, detect a solitary line "R", "G", "E", "BG", "C" and normalise:
    R -> "R"; everything else -> "G".
  (Many FCA PDFs float the type in the margin; text extraction often places it
  on its own line near the clause, not on the header line.)
- Section banners like "Section : CASS 1.2 ..." act as hard boundaries,
  but do not create rule entries.
- Strong de-duplication: unique per (id, type). If duplicates, keep the longer text
  and prefer entries that actually have a type.
"""

import argparse, pathlib, re, sys
from typing import Dict, List, Optional
import pdfplumber, yaml
import regex as rxx

# -------- cleanup helpers --------
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
        if PAGE_FOOTER_RE.match(ln):   # remove repeating footers
            continue
        if URL_RE.match(ln):           # drop inline link-dump lines
            continue
        if JUST_DOTS_RE.match(ln):     # dot/line fillers
            continue
        lines.append(ln)
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

# -------- pattern detection (STRICT: must start with "CASS ") --------
# Clause id: chapter.section.rule; rule may have letter/hyphen suffix (e.g., 5A, 1-A)
RULE_ID_RE = r"(?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)\.(?P<rule>\d+(?:[A-Z]|-[A-Z])?)"

# Heading with optional inline type and optional title
CLAUSE_RE = re.compile(
    rf"""^\s*CASS\s+{RULE_ID_RE}
        (?P<typesuf>[A-Z]{{1,3}})?                 # e.g., R, G, E, BG, C
        (?:\s+|(?:\s*[-–—:]\s*))?
        (?P<title>.*)?\s*$""",
    re.VERBOSE,
)

# Variant where type is separated by a space
CLAUSE_SPLITTYPE_RE = re.compile(
    rf"""^\s*CASS\s+{RULE_ID_RE}
        \s+(?P<typesuf>[A-Z]{{1,3}})
        (?:\s+|(?:\s*[-–—:]\s*))?
        (?P<title>.*)?\s*$""",
    re.VERBOSE,
)

# Section banner (boundary only)
SECTION_RE = re.compile(
    r"""^\s*Section\s*:\s*CASS\s+
        (?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)
        (?:\s*[-–—:]\s*.+)?\s*$""",
    re.VERBOSE | re.I,
)

# Solitary type marker on its own line
SOLO_TYPE_RE = re.compile(r"^\s*(R|G|E|BG|C)\s*$", re.I)

def norm_type(t: Optional[str]) -> Optional[str]:
    if not t: return None
    t = t.upper()
    return "R" if t == "R" else "G"   # everything non-R is Guidance for your use-case

def match_clause(line: str):
    m = CLAUSE_RE.match(line) or CLAUSE_SPLITTYPE_RE.match(line)
    if not m: return None
    gd = m.groupdict()
    rid = f"{gd['chapter']}.{gd['section']}.{gd['rule']}"
    typesuf = norm_type((gd.get("typesuf") or "").strip() or None)
    title = (gd.get("title") or "").strip() or None
    return {"id": rid, "chapter": gd["chapter"], "type": typesuf, "title": title}

def is_section_banner(line: str) -> bool:
    if CLAUSE_RE.match(line) or CLAUSE_SPLITTYPE_RE.match(line):
        return False
    return bool(SECTION_RE.match(line))

def harvest_type_from_body(lines: List[str]) -> Optional[str]:
    # look near start or end for a solitary type marker
    # (PDFs often drop "R"/"G" as its own line at margin)
    head = lines[:4]
    tail = lines[-4:]
    for seg in (head + tail):
        m = SOLO_TYPE_RE.match(seg or "")
        if m:
            return norm_type(m.group(1))
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
            buf = []
            return
        body_lines = [ln for ln in buf if not BLANKISH_RE.match(ln)]
        # Try to infer type from body if missing
        if not current.get("type"):
            inferred = harvest_type_from_body(body_lines)
            if inferred:
                current["type"] = inferred
        # Remove any standalone type lines from the body
        body_lines = strip_type_lines(body_lines)
        body = "\n".join(body_lines).strip()

        # Drop obvious TOC ghosts: no type AND very short body
        if not current.get("type") and len(body) < 80:
            current, buf = None, []
            return

        current["type"] = current.get("type") or "G"
        current["text"] = body
        current["display"] = f"CASS {current['id']}{current['type']}"
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
                "type": m["type"],      # may still be None (we'll infer)
                "title": m["title"],
                "summary": None,
                "risk_ids": [],
                "default_control_ids": [],
                "applicability_conditions": None,
            }
            buf = []
            continue

        # Section boundary ends current clause
        if is_section_banner(line):
            flush()
            continue

        # Ignore noise before first clause
        if not current:
            continue

        # Normal content
        if URL_RE.match(line) or PAGE_FOOTER_RE.match(line) or JUST_DOTS_RE.match(line):
            continue
        buf.append(line)

    flush()

    # ---- strong de-duplication: unique by (id, type) ----
    dedup: Dict[str, Dict] = {}
    for r in rules:
        key = f"{r['id']}|{r.get('type') or 'G'}"
        existing = dedup.get(key)
        if not existing:
            dedup[key] = r
            continue
        # Prefer entries with a type; if both typed, choose longer body
        has_type_new = bool(r.get("type"))
        has_type_old = bool(existing.get("type"))
        if (has_type_new and not has_type_old) or (len(r.get("text","")) > len(existing.get("text",""))):
            dedup[key] = r

    def sort_key(r):
        chap = r["chapter"]
        chap_num = int("".join(c for c in chap if c.isdigit()) or 0)
        chap_suf = "".join(c for c in chap if c.isalpha())
        a,b,c = r["id"].split(".")
        t_rank = 0 if (r.get("type")=="R") else 1
        # stable order: chapter → section → rule → type (R first)
        # rule may contain letters/hyphen; split numeric part first
        rule_num = int("".join(ch for ch in c if ch.isdigit()) or 0)
        rule_suf = "".join(ch for ch in c if not ch.isdigit())
        return (chap_num, chap_suf, int(b), rule_num, rule_suf, t_rank)

    out = sorted(dedup.values(), key=sort_key)
    # Ensure type is present and display is correct
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
        lines.extend(extract_lines(path))
        lines.append("")  # separator

    rules = parse_rules(lines)
    if not rules:
        print("[error] no rules detected. If PDFs are scanned, OCR them first.", file=sys.stderr)
        sys.exit(1)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        yaml.safe_dump(rules, f, sort_keys=False, allow_unicode=True)
    print(f"[ok] wrote {len(rules)} rules -> {out}")

if __name__ == "__main__":
    main()
