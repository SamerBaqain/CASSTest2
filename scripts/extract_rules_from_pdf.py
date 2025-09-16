#!/usr/bin/env python3
"""
CASS extractor for two-column PDFs (anchor-driven, bold R/G aware)

What it does
------------
- Finds only real clause anchors that look like: "CASS 1.2.2" (i.e., chapter.section.rule)
- Reads the bold "R" or "G" immediately next to / below the anchor in the left column
  (falls back if font info isn't available) → type = "R" or "G"
- Harvests the clause BODY from the right column (text with x0 > anchor.x1 + margin)
  until the next anchor/section banner
- De-duplicates strictly by (id, type), keeping the longest body
- Outputs YAML list your site already consumes:
    id, chapter, type, display ("CASS <id>"), text, and placeholders (summary, risk_ids, ...)

Tuning knobs (CLI flags)
------------------------
--y-tol        : vertical tolerance to group words into lines (default 3.0)
--type-gap     : max vertical gap to treat the next short line as the solitary R/G (default 12)
--body-margin  : extra points to the right of the anchor's x1 to start BODY (default 6)
--left-max-ratio: anything with x0 < page.width * ratio is considered "left column" (default 0.40)
--min-body-len : drop entries with very tiny body (helps avoid TOC ghosts) (default 20 chars)

Requirements
------------
pdfplumber>=0.10
PyYAML>=6.0
"""

import argparse, pathlib, re, sys
from typing import Dict, List, Optional, Tuple
import pdfplumber, yaml

# ---------- Regexes ----------
RULE_ID_PART = r"(?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)\.(?P<rule>\d+(?:[A-Z]|-[A-Z])?)"
ANCHOR_RE = re.compile(rf"^\s*CASS\s+{RULE_ID_PART}\s*$")
SECTION_RE = re.compile(r"^\s*Section\s*:\s*CASS\s+\d+[A-Z]?\.\d+", re.I)
TYPE_ONLY_RE = re.compile(r"^\s*(R|G|E|BG|C)\s*$", re.I)  # anything not R → G later
DROP_LINE_RE = re.compile(r"^\s*(www\.handbook\.fca\.org\.uk|FCA\s+\d{4}/\d+|Page\s+\d+\s+of\s+\d+)\s*$", re.I)

def norm_type(t: Optional[str]) -> str:
    if not t: return "G"
    return "R" if t.upper() == "R" else "G"

# ---------- Word → line utilities ----------
def words_to_lines(words: List[dict], y_tol: float) -> List[dict]:
    """Group words into lines by doctop with y tolerance, keep x0/x1/font info."""
    if not words: return []
    words = sorted(words, key=lambda w: (w["doctop"], w["x0"]))
    lines, cur, cur_top = [], [], None
    for w in words:
        top = w["top"]
        if cur_top is None or abs(top - cur_top) <= y_tol:
            cur.append(w); cur_top = top if cur_top is None else min(cur_top, top)
        else:
            lines.append(_pack_line(cur)); cur = [w]; cur_top = top
    if cur: lines.append(_pack_line(cur))
    lines.sort(key=lambda ln: ln["doctop"])
    return lines

def _pack_line(ws: List[dict]) -> dict:
    ws = sorted(ws, key=lambda w: w["x0"])
    text = " ".join(w["text"] for w in ws).strip()
    fonts = [w.get("fontname","") for w in ws]
    sizes = [w.get("size",0) for w in ws]
    return {
        "text": text,
        "x0": min(w["x0"] for w in ws),
        "x1": max(w["x1"] for w in ws),
        "top": min(w["top"] for w in ws),
        "bottom": max(w["bottom"] for w in ws),
        "doctop": min(w["doctop"] for w in ws),
        "fonts": fonts, "sizes": sizes, "words": ws,
    }

def is_bold_line(ln: dict) -> bool:
    # crude but effective: any fontname containing 'Bold'
    return any("Bold" in (f or "") for f in ln.get("fonts", []))

# ---------- Anchor detection (left column only) ----------
def find_anchors(page, y_tol: float, type_gap: float, left_max_ratio: float) -> List[dict]:
    # grab word-level with font info
    words = page.extract_words(use_text_flow=True, keep_blank_chars=False,
                               extra_attrs=["fontname","size"])
    lines = words_to_lines(words, y_tol)
    left_limit = page.width * left_max_ratio
    anchors: List[dict] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln["x0"] >= left_limit:  # only left column lines can be anchors
            i += 1; continue
        txt = ln["text"]
        if SECTION_RE.match(txt):
            anchors.append({"kind":"section", "doctop": ln["doctop"], "x1": ln["x1"], "page": page.page_number-1})
            i += 1; continue
        m = ANCHOR_RE.match(txt)
        if m:
            gd = m.groupdict()
            rid = f"{gd['chapter']}.{gd['section']}.{gd['rule']}"
            # find type on the same line (e.g., "...  R") or on the next tiny line as a solitary bold "R/G"
            t = None
            # If the line has a trailing R/G token as a separate word, pdfplumber usually splits it.
            # We check next line if it's very close and looks like a type-only line.
            if (i+1) < len(lines):
                nxt = lines[i+1]
                # must be near AND in the left column
                if (nxt["doctop"] - ln["doctop"]) <= type_gap and nxt["x0"] < left_limit and TYPE_ONLY_RE.match(nxt["text"]):
                    # prefer bold detection, but accept any if font not exposed
                    if is_bold_line(nxt) or not nxt.get("fonts"):
                        t = TYPE_ONLY_RE.match(nxt["text"]).group(1)
                        i += 1  # consume type line
            # If still None, try to see if the current line ends with a R/G token (rare)
            if t is None:
                tail = txt.split()[-1] if txt.split() else ""
                if TYPE_ONLY_RE.fullmatch(tail):
                    t = tail
            anchors.append({
                "kind":"rule", "id": rid, "type": norm_type(t),
                "doctop": ln["doctop"], "x1": ln["x1"], "page": page.page_number-1
            })
        i += 1
    return anchors

# ---------- BODY collection (right of anchor → until next anchor/section) ----------
def harvest_bodies(pdf, anchors: List[dict], body_margin: float, y_tol: float, left_max_ratio: float, min_body_len: int) -> Dict[Tuple[str,str], dict]:
    # Pre-pack lines for each page
    page_lines = []
    for p in pdf.pages:
        words = p.extract_words(use_text_flow=True, keep_blank_chars=False,
                                extra_attrs=["fontname","size"])
        page_lines.append(words_to_lines(words, y_tol))

    out: Dict[Tuple[str,str], dict] = {}

    for idx, anc in enumerate(anchors):
        if anc["kind"] != "rule": continue

        start_y = anc["doctop"]
        end_y   = anchors[idx+1]["doctop"] if idx+1 < len(anchors) else float("inf")
        body: List[str] = []

        for pi, lines in enumerate(page_lines):
            for ln in lines:
                # reject obvious furniture / left-column noise
                if DROP_LINE_RE.match(ln["text"]): 
                    continue
                # keep only lines between anchors
                if not (start_y <= ln["doctop"] < end_y):
                    continue
                # if it's on the anchor's page: require ln.x0 > anchor.x1 + margin
                if pi == anc["page"]:
                    if ln["x0"] <= (anc["x1"] + body_margin): 
                        continue
                else:
                    # for other pages, "right column" ≈ not in the left column
                    left_limit = pdf.pages[pi].width * left_max_ratio
                    if ln["x0"] < left_limit:
                        continue
                # drop solitary type markers that sometimes get mirrored into body
                if TYPE_ONLY_RE.match(ln["text"]) and is_bold_line(ln):
                    continue
                body.append(ln["text"])

        text = "\n".join(_collapse_blanks(body)).strip()
        if len(text) < min_body_len:
            # too small → very likely TOC/artefact
            continue

        key = (anc["id"], anc["type"])
        prev = out.get(key)
        if not prev or len(text) > len(prev["text"]):
            chap = anc["id"].split(".")[0]  # preserves letters (e.g., "1A")
            out[key] = {
                "id": anc["id"],
                "chapter": chap,
                "type": anc["type"],
                "title": None,
                "summary": None,
                "risk_ids": [],
                "default_control_ids": [],
                "applicability_conditions": None,
                "text": text,
                "display": f"CASS {anc['id']}",  # no R/G suffix
            }

    return out

def _collapse_blanks(lines: List[str]) -> List[str]:
    out, last_blank = [], False
    for ln in lines:
        blank = (ln.strip() == "")
        if blank and last_blank: 
            continue
        out.append(ln); last_blank = blank
    return out

# ---------- Sorting with the order you want ----------
CHAPTER_ORDER = ["1", "1A", "3", "5", "6", "7"]
def sort_key(rec: dict):
    chap = rec["chapter"]
    try:
        chap_idx = CHAPTER_ORDER.index(chap)
    except ValueError:
        chap_idx = 99  # anything else after the custom list
    a,b,c = rec["id"].split(".")
    sec = int(b)
    rule_num = int("".join(ch for ch in c if ch.isdigit()) or 0)
    rule_suf = "".join(ch for ch in c if not ch.isdigit())
    t_rank = 0 if rec["type"] == "R" else 1
    return (chap_idx, a, sec, rule_num, rule_suf, t_rank)

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", help="Path(s) to CASS PDFs")
    ap.add_argument("--out", default="data/rules.yaml", help="Output YAML path")
    ap.add_argument("--y-tol", type=float, default=3.0)
    ap.add_argument("--type-gap", type=float, default=12.0)
    ap.add_argument("--body-margin", type=float, default=6.0)
    ap.add_argument("--left-max-ratio", type=float, default=0.40)
    ap.add_argument("--min-body-len", type=int, default=20)
    args = ap.parse_args()

    all_entries: Dict[Tuple[str,str], dict] = {}

    for pth in args.pdfs:
        path = pathlib.Path(pth)
        if not path.exists():
            print(f"[warn] missing {path}", file=sys.stderr); 
            continue

        with pdfplumber.open(str(path)) as pdf:
            # 1) collect anchors across pages
            anchors = []
            for page in pdf.pages:
                anchors += find_anchors(page, args.y_tol, args.type_gap, args.left_max_ratio)
            anchors.sort(key=lambda a: a["doctop"])

            # 2) harvest bodies
            bodies = harvest_bodies(pdf, anchors, args.body_margin, args.y_tol, args.left_max_ratio, args.min_body_len)

            # 3) merge into global set, preferring longer text on collisions
            for k, v in bodies.items():
                cur = all_entries.get(k)
                if not cur or len(v["text"]) > len(cur["text"]):
                    all_entries[k] = v

    # final list + sort
    items = sorted(all_entries.values(), key=sort_key)

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(items, f, sort_keys=False, allow_unicode=True)

    print(f"[ok] wrote {len(items)} rules -> {out_path}")

if __name__ == "__main__":
    main()
