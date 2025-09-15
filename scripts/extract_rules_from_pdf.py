#!/usr/bin/env python3
"""
Extract CASS rules/clauses from one or more PDFs into data/rules.yaml.

- Recognises headings such as:
  "CASS 1.2.1 ..." or "1.2.1 ..." (R/G/E marker may not be on the same line)
- Stores: id (e.g., "1.2.1"), chapter ("1"), title (if present), type (R/G/E if detected),
  and the rule text until the next heading.

Tip for scanned PDFs: OCR first (outside GitHub) with ocrmypdf, then upload the OCR'd PDFs.
"""
import argparse, pathlib, re, sys
from typing import Dict, List, Optional
import pdfplumber, yaml
import regex as rxx

HEAD_PATTERNS = [
    re.compile(r"^\s*(?:CASS\s*)?(?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)\.(?P<rule>\d+)(?:\s*[-–—:]\s*(?P<title>.+))?\s*$"),
    re.compile(r"^\s*(?:CASS\s*)?(?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)\s*[-–—:]\s*(?P<title>.+?)\s*$")
]
PAGE_FOOTER_RE = re.compile(r"^\s*(Page\s+\d+\s+of\s+\d+|FCA\s+\d{4}/\d+|www\.handbook\.fca\.org\.uk.*)$", re.I)
BLANKISH_RE = re.compile(r"^\s*$")
HYPHEN_LINEBREAK_RE = rxx.compile(r"(\p{Letter})-\n(\p{Letter})", rxx.UNICODE)
TYPE_LINE_RE = re.compile(r"^\s*([RGE])\s*$")  # a solitary R/G/E on its own line

def clean_page_text(raw: str) -> str:
    txt = (raw or "").replace("\r\n","\n").replace("\r","\n")
    txt = HYPHEN_LINEBREAK_RE.sub(r"\1\2", txt)
    lines = [ln for ln in txt.split("\n") if not PAGE_FOOTER_RE.match(ln)]
    return "\n".join(lines)

def extract_lines(pdf_path: pathlib.Path) -> List[str]:
    lines: List[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if not t.strip():
                continue
            lines.extend(clean_page_text(t).split("\n"))
    return lines

def parse_rules(lines: List[str]) -> List[Dict]:
    rules: List[Dict] = []
    current: Optional[Dict] = None
    buf: List[str] = []
    last_was_header = False

    def flush():
        nonlocal current, buf
        if current:
            body = "\n".join(buf).strip()
            current["text"] = body
            current["display"] = f"CASS {current['id']}"
            rules.append(current)
        current, buf = None, []

    for raw in lines:
        line = raw.rstrip()

        header = None
        for pat in HEAD_PATTERNS:
            m = pat.match(line)
            if m and m.groupdict().get("rule"):
                gd = m.groupdict()
                header = {
                    "id": f"{gd['chapter']}.{gd['section']}.{gd['rule']}",
                    "chapter": gd["chapter"],
                    "title": (gd.get("title") or "").strip() or None,
                }
                break
            elif m and not m.groupdict().get("rule"):
                flush()
                last_was_header = False
                header = None
                break

        if header:
            flush()
            current = {
                "id": header["id"],
                "chapter": header["chapter"],
                "type": None,
                "title": header["title"],
                "summary": None,
                "risk_ids": [],
                "default_control_ids": [],
                "applicability_conditions": None,
            }
            buf = []
            last_was_header = True
            continue

        if last_was_header and TYPE_LINE_RE.match(line):
            current["type"] = TYPE_LINE_RE.match(line).group(1)
            last_was_header = False
            continue

        last_was_header = False

        if BLANKISH_RE.match(line):
            if buf and buf[-1] != "":
                buf.append("")
            continue

        if current is None:
            continue
        buf.append(line)

    flush()

    dedup: Dict[str, Dict] = {}
    for r in rules:
        key = f"{r['id']}|{r.get('type') or ''}"
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

    all_lines: List[str] = []
    for p in args.pdfs:
        path = pathlib.Path(p)
        if not path.exists():
            print(f"[warn] missing {path}", file=sys.stderr); continue
        print(f"[info] reading {path}", file=sys.stderr)
        all_lines.extend(extract_lines(path))
        all_lines.append("")

    rules = parse_rules(all_lines)
    if not rules:
        print("[error] no rules detected; try OCR if scanned", file=sys.stderr); sys.exit(1)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        yaml.safe_dump(rules, f, sort_keys=False, allow_unicode=True)
    print(f"[ok] wrote {len(rules)} rules -> {out}")

if __name__ == "__main__":
    main()
