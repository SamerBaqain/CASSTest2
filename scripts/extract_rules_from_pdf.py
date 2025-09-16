#!/usr/bin/env python3
"""
CASS extractor (anchor-driven, column-aware, custom order + clean display)

- Finds anchors that look like: "CASS 1.2.2", optionally with "R"/"G" on the line
  or the next tiny line. Lettered chapters (e.g., 1A) are preserved.
- For each anchor, collects BODY text from lines whose x0 > (anchor.x1 + body_margin)
  until the next anchor/section banner.
- De-duplicates strictly by (id, type).
- 'type' is "R" for R; everything else -> "G".
- 'display' is **always** "CASS <id>" (NO R/G suffix).
- Sort order is: 1, 1A, 3, 5, 6, 7, then anything else.
"""

import argparse, pathlib, re, sys
from typing import Dict, List, Optional, Tuple
import pdfplumber, yaml

# ---------- Regexes ----------
RULE_ID_PART = r"(?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)\.(?P<rule>\d+(?:[A-Z]|-[A-Z])?)"
ANCHOR_RE = re.compile(rf"^\s*CASS\s+{RULE_ID_PART}(?:\s+(?P<typesuf>R|G|E|BG|C))?\s*$")
TYPE_ONLY_RE = re.compile(r"^\s*(R|G|E|BG|C)\s*$", re.I)
SECTION_RE = re.compile(r"^\s*Section\s*:\s*CASS\s+\d+[A-Z]?\.\d+", re.I)

# ---------- Normalisation ----------
def norm_type(t: Optional[str]) -> str:
    if not t:
        return "G"
    return "R" if t.upper() == "R" else "G"

# ---------- Line grouping ----------
def words_to_lines(words: List[dict], y_tol: float) -> List[dict]:
    if not words: return []
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
    if cur: lines.append(_pack_line(cur))
    lines.sort(key=lambda ln: ln["doctop"])
    return lines

def _pack_line(ws: List[dict]) -> dict:
    ws = sorted(ws, key=lambda w: w["x0"])
    return {
        "text": " ".join(w["text"] for w in ws).strip(),
        "x0": min(w["x0"] for w in ws),
        "x1": max(w["x1"] for w in ws),
        "top": min(w["top"] for w in ws),
        "bottom": max(w["bottom"] for w in ws),
        "doctop": min(w["doctop"] for w in ws),
        "words": ws,
    }

# ---------- Anchor detection on each page ----------
def find_anchors(page, y_tol: float, type_gap: float) -> List[dict]:
    lines = words_to_lines(page.extract_words(use_text_flow=True, keep_blank_chars=False), y_tol)
    anchors: List[dict] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        txt = ln["text"]
        if SECTION_RE.match(txt):
            anchors.append({"kind":"section", "doctop": ln["doctop"], "x1": ln["x1"], "page": page.page_number-1})
            i += 1; continue
        m = ANCHOR_RE.match(txt)
        if m:
            gd = m.groupdict()
            rid = f"{gd['chapter']}.{gd['section']}.{gd['rule']}"
            t = gd.get("typesuf")
            # If no inline type, see if the next tiny line is just a type marker
            if not t and (i+1) < len(lines):
                nxt = lines[i+1]
                if (nxt["doctop"] - ln["doctop"]) <= type_gap and TYPE_ONLY_RE.match(nxt["text"]):
                    t = TYPE_ONLY_RE.match(nxt["text"]).group(1)
                    i += 1  # consume that line
            anchors.append({
                "kind":"rule", "id": rid, "type": norm_type(t),
                "doctop": ln["doctop"], "x1": ln["x1"], "page": page.page_number-1
            })
        i += 1
    return anchors

# ---------- Body collection across pages ----------
def harvest(pdf, anchors: List[dict], body_margin: float, y_tol: float) -> Dict[Tuple[str,str], dict]:
    # Pre-pack ALL lines per page (once)
    page_lines = [words_to_lines(p.extract_words(use_text_flow=True, keep_blank_chars=False), y_tol) for p in pdf.pages]
    out: Dict[Tuple[str,str], dict] = {}

    for idx, anc in enumerate(anchors):
        if anc["kind"] != "rule":
            continue
        start_y = anc["doctop"]
        end_y = anchors[idx+1]["doctop"] if idx+1 < len(anchors) else float("inf")
        body: List[str] = []

        # accumulate text from start_y (inclusive) to end_y (exclusive), to the RIGHT of anchor.x1+margin
        for pi, lines in enumerate(page_lines):
            for ln in lines:
                if start_y <= ln["doctop"] < end_y and ln["x0"] > (anc["x1"] + body_margin):
                    body.append(ln["text"])

        text = "\n".join(_clean_body(body)).strip()
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
                "display": f"CASS {anc['id']}",   # << NO R/G suffix in display
            }
    return out

DROP_RE = re.compile(r"^\s*(www\.handbook\.fca\.org\.uk|FCA\s+\d{4}/\d+|Page\s+\d+\s+of\s+\d+)\s*$", re.I)
def _clean_body(lines: List[str]) -> List[str]:
    # Remove furniture + collapse blank lines
    tmp = []
    for ln in lines:
        if not ln.strip():
            tmp.append(""); continue
        if DROP_RE.match(ln): continue
        tmp.append(ln)
    out = []
    last_blank = False
    for ln in tmp:
        blank = (ln.strip() == "")
        if blank and last_blank: continue
        out.append(ln); last_blank = blank
    return out

# ---------- Sorting with your requested order ----------
CHAPTER_ORDER = ["1", "1A", "3", "5", "6", "7"]  # custom priority
def sort_key(rec: dict):
    chap = rec["chapter"]                     # keep letters
    # priority: custom order, else large index to push to end
    try:
        chap_idx = CHAPTER_ORDER.index(chap)
    except ValueError:
        # fallback: after the custom ones, natural by numeric+letter
        chap_idx = 99
    # within chapter, sort section and rule (numeric then suffix)
    a,b,c = rec["id"].split(".")
    sec = int(b)
    rule_num = int("".join(ch for ch in c if ch.isdigit()) or 0)
    rule_suf = "".join(ch for ch in c if not ch.isdigit())
    # R rows first within the same clause
    t_rank = 0 if rec["type"] == "R" else 1
    return (chap_idx, a, sec, rule_num, rule_suf, t_rank)

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", help="Path(s) to CASS PDFs")
    ap.add_argument("--out", default="data/rules.yaml", help="Output YAML path")
    ap.add_argument("--y-tol", type=float, default=3.0, help="Y tolerance for building lines")
    ap.add_argument("--type-gap", type=float, default=10.0, help="Max vertical gap to pick solitary R/G line")
    ap.add_argument("--body-margin", type=float, default=6.0, help="Points to the right of the anchor.x1 to treat as body")
    args = ap.parse_args()

    all_entries: Dict[Tuple[str,str], dict] = {}
    for p in args.pdfs:
        path = pathlib.Path(p)
        if not path.exists():
            print(f"[warn] missing {path}", file=sys.stderr); continue
        with pdfplumber.open(str(path)) as pdf:
            # find anchors across document
            anchors = []
            for page in pdf.pages:
                anchors += find_anchors(page, args.y_tol, args.type_gap)
            anchors.sort(key=lambda a: a["doctop"])
            bodies = harvest(pdf, anchors, args.body_margin, args.y_tol)
            # merge preferring longer bodies
            for k, v in bodies.items():
                cur = all_entries.get(k)
                if not cur or len(v["text"]) > len(cur["text"]):
                    all_entries[k] = v

    items = sorted(all_entries.values(), key=sort_key)

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(items, f, sort_keys=False, allow_unicode=True)
    print(f"[ok] wrote {len(items)} rules -> {out_path}")

if __name__ == "__main__":
    main()
