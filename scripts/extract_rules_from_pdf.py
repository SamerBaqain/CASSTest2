#!/usr/bin/env python3
"""
CASS extractor (column-aware):
- Uses PDF coordinates (pdfplumber.extract_words) to treat the page as two columns.
- LEFT column: "CASS <chapter>.<section>.<rule>" + nearby "R/G" marker
- RIGHT column: rule body text until the next LEFT anchor (or section banner)
- Output: unique entries per (id, type) with clean text.

If some PDFs are scanned (images), this will produce 0 rules; OCR them first.
"""

import argparse, pathlib, re, sys
from typing import Dict, List, Optional, Tuple
import pdfplumber, yaml

# ---------- Tunables (can be overridden via CLI) ----------
LEFT_COL_RATIO_DEFAULT  = 0.33   # everything with x0 < page_width * ratio is LEFT column
RIGHT_COL_RATIO_DEFAULT = 0.36   # everything with x0 >= page_width * ratio is RIGHT column
LINE_Y_TOL_DEFAULT      = 3.0    # vertical tolerance to group words into a single line
TYPE_JOIN_GAP           = 10.0   # max vertical gap (points) to treat next line as the 'R/G' for an anchor

# ---------- Regexes ----------
RULE_ID_PART = r"(?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)\.(?P<rule>\d+(?:[A-Z]|-[A-Z])?)"
ANCHOR_RE = re.compile(rf"^\s*CASS\s+{RULE_ID_PART}\s*(?P<typesuf>(?:R|G|E|BG|C))?\s*$")
TYPE_ONLY_RE = re.compile(r"^\s*(R|G|E|BG|C)\s*$", re.I)
SECTION_RE = re.compile(r"^\s*Section\s*:\s*CASS\s+\d+[A-Z]?\.\d+", re.I)

def norm_type(t: Optional[str]) -> Optional[str]:
    if not t: return None
    t = t.upper()
    return "R" if t == "R" else "G"

# ---------- Helpers ----------
def words_to_lines(words: List[dict], y_tol: float) -> List[dict]:
    """Group words into lines by y (top) with given tolerance; return lines sorted by doctop, then x."""
    if not words: return []
    # sort by doctop then x0
    words = sorted(words, key=lambda w: (w["doctop"], w["x0"]))
    lines: List[dict] = []
    cur: List[dict] = []
    cur_top = None
    for w in words:
        top = w["top"]
        if cur_top is None or abs(top - cur_top) <= y_tol:
            cur.append(w)
            cur_top = top if cur_top is None else min(cur_top, top)
        else:
            lines.append(_pack_line(cur))
            cur = [w]; cur_top = top
    if cur:
        lines.append(_pack_line(cur))
    # Sort by doctop (asc)
    lines.sort(key=lambda ln: ln["doctop"])
    return lines

def _pack_line(ws: List[dict]) -> dict:
    ws = sorted(ws, key=lambda w: w["x0"])
    text = " ".join(w["text"] for w in ws).strip()
    return {
        "text": text,
        "x0": min(w["x0"] for w in ws),
        "x1": max(w["x1"] for w in ws),
        "top": min(w["top"] for w in ws),
        "bottom": max(w["bottom"] for w in ws),
        "doctop": min(w["doctop"] for w in ws),
        "words": ws,
    }

def left_right_words(page, left_ratio: float, right_ratio: float) -> Tuple[List[dict], List[dict], float, float]:
    W = float(page.width)
    left_max_x  = W * left_ratio
    right_min_x = W * right_ratio
    words = page.extract_words(x_tolerance=1.0, y_tolerance=1.0, keep_blank_chars=False, use_text_flow=True)
    left_words  = [w for w in words if w["x0"] < left_max_x]
    right_words = [w for w in words if w["x0"] >= right_min_x]
    return left_words, right_words, left_max_x, right_min_x

# ---------- Core ----------
def extract_anchors(pdf, left_ratio: float, y_tol: float) -> List[dict]:
    """Return list of anchors: [{id, type, doctop, page_index}] plus 'section' boundaries (type=None, id=None)."""
    anchors: List[dict] = []
    for pi, page in enumerate(pdf.pages):
        left_words, _right_words, *_ = left_right_words(page, left_ratio, RIGHT_COL_RATIO_DEFAULT)
        lines = words_to_lines(left_words, y_tol)
        i = 0
        while i < len(lines):
            ln = lines[i]
            txt = ln["text"]
            # Section banner as a boundary
            if SECTION_RE.match(txt):
                anchors.append({"kind":"section", "doctop": ln["doctop"], "page": pi})
                i += 1
                continue
            m = ANCHOR_RE.match(txt)
            if m:
                gd = m.groupdict()
                rid = f"{gd['chapter']}.{gd['section']}.{gd['rule']}"
                typ = norm_type(gd.get("typesuf"))
                # If no inline type, check the next small line for a solitary R/G/etc.
                if not typ and (i + 1) < len(lines):
                    nxt = lines[i+1]
                    if (nxt["doctop"] - ln["doctop"]) <= TYPE_JOIN_GAP and TYPE_ONLY_RE.match(nxt["text"]):
                        typ = norm_type(TYPE_ONLY_RE.match(nxt["text"]).group(1))
                        i += 1  # consume the type line
                anchors.append({"kind":"rule", "id": rid, "type": typ or "G", "doctop": ln["doctop"], "page": pi})
            i += 1
    # sort by doctop
    anchors.sort(key=lambda a: a["doctop"])
    return anchors

def harvest_bodies(pdf, anchors: List[dict], right_ratio: float, y_tol: float) -> Dict[Tuple[str,str], dict]:
    """Collect right-column text between consecutive anchors; return dict keyed by (id,type)."""
    # Pre-extract right-side lines for all pages
    page_right_lines: List[List[dict]] = []
    for page in pdf.pages:
        _left, right_words, *_ = left_right_words(page, LEFT_COL_RATIO_DEFAULT, right_ratio)
        page_right_lines.append(words_to_lines(right_words, y_tol))

    results: Dict[Tuple[str,str], dict] = {}
    # Iterate over anchors; for each real rule anchor, gather right-column lines until next anchor
    for idx, anc in enumerate(anchors):
        if anc.get("kind") != "rule":
            continue
        y0 = anc["doctop"]
        # find next boundary
        y1 = anchors[idx+1]["doctop"] if idx+1 < len(anchors) else float("inf")
        # collect right lines with doctop in [y0, y1)
        body_lines: List[str] = []
        for pi, lines in enumerate(page_right_lines):
            for ln in lines:
                if y0 <= ln["doctop"] < y1:
                    body_lines.append(ln["text"])
        text = "\n".join(_clean_body(body_lines)).strip()
        key = (anc["id"], anc["type"])
        # keep the longer body if duplicate key reappears
        prev = results.get(key)
        if not prev or len(text) > len(prev["text"]):
            chapter = anc["id"].split(".")[0]  # keeps letter (e.g., "1A")
            results[key] = {
                "id": anc["id"],
                "chapter": chapter,
                "type": anc["type"],
                "title": None,
                "summary": None,
                "risk_ids": [],
                "default_control_ids": [],
                "applicability_conditions": None,
                "text": text,
                "display": f"CASS {anc['id']}{anc['type']}"
            }
    return results

DROP_LINES_RE = re.compile(r"^\s*(www\.handbook\.fca\.org\.uk|FCA\s+\d{4}/\d+|Page\s+\d+\s+of\s+\d+)\s*$", re.I)
def _clean_body(lines: List[str]) -> List[str]:
    out = []
    for ln in lines:
        if not ln.strip(): 
            # collapse excessive blanks later by joining
            out.append("")
            continue
        if DROP_LINES_RE.match(ln): 
            continue
        out.append(ln)
    # collapse multiple blank lines
    cleaned: List[str] = []
    last_blank = False
    for ln in out:
        blank = (ln.strip()=="")
        if blank and last_blank:
            continue
        cleaned.append(ln)
        last_blank = blank
    return cleaned

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", help="Path(s) to CASS PDFs")
    ap.add_argument("--out", default="data/rules.yaml", help="Output YAML path")
    ap.add_argument("--left-col", type=float, default=LEFT_COL_RATIO_DEFAULT, help="Left column x-ratio (default 0.33)")
    ap.add_argument("--right-col", type=float, default=RIGHT_COL_RATIO_DEFAULT, help="Right column x-ratio (default 0.36)")
    ap.add_argument("--line-y-tol", type=float, default=LINE_Y_TOL_DEFAULT, help="Y tolerance for line grouping")
    args = ap.parse_args()

    all_entries: Dict[Tuple[str,str], dict] = {}

    for p in args.pdfs:
        path = pathlib.Path(p)
        if not path.exists():
            print(f"[warn] missing {path}", file=sys.stderr); continue
        print(f"[info] reading {path}", file=sys.stderr)
        with pdfplumber.open(str(path)) as pdf:
            anchors = extract_anchors(pdf, args.left_col, args.line_y_tol)
            if not anchors:
                print(f"[warn] no anchors detected in {path}", file=sys.stderr)
            bodies = harvest_bodies(pdf, anchors, args.right_col, args.line_y_tol)
            for k,v in bodies.items():
                cur = all_entries.get(k)
                if not cur or len(v["text"]) > len(cur["text"]):
                    all_entries[k] = v

    items = list(all_entries.values())
    # stable sort: chapter (numeric then letter), section, rule (numeric then suffix), type (R before G)
    def sort_key(r):
        chapter = r["chapter"]
        chap_num = int("".join(c for c in chapter if c.isdigit()) or 0)
        chap_suf = "".join(c for c in chapter if c.isalpha())
        a,b,c = r["id"].split(".")
        rule_num = int("".join(ch for ch in c if ch.isdigit()) or 0)
        rule_suf = "".join(ch for ch in c if not ch.isdigit())
        t_rank = 0 if r["type"]=="R" else 1
        return (chap_num, chap_suf, int(b), rule_num, rule_suf, t_rank)
    items.sort(key=sort_key)

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(items, f, sort_keys=False, allow_unicode=True)
    print(f"[ok] wrote {len(items)} rules -> {out_path}")

if __name__ == "__main__":
    main()
