#!/usr/bin/env python3
"""
CASS extractor (robust, column-aware, de-dup, bold R/G, noise-filtered)

Outputs items like:
- id: "1.2.2"
  chapter: "1"
  type: "R"
  display: "CASS 1.2.2"
  text: "..."

Key points:
- Only accepts real anchors: lines that are EXACTLY "CASS <chapter>.<section>.<rule>[R|G|E|BG|C]?"
- If "R/G" is glued to the number (e.g., 1.2.2R), it is captured as type (not part of id).
- Reads type from same line OR next tiny bold line; normalises any non-R to "G".
- Harvests body to the RIGHT of the anchor line’s x1 (+margin) until the next anchor/section.
- Filters out Actionability table/headers/uppercase furniture lines to avoid garbage text.
"""

import argparse, pathlib, re, sys
from typing import Dict, List, Optional, Tuple
import pdfplumber, yaml

# -------------------- patterns --------------------
RULE_ID = r"(?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)\.(?P<rule>\d+(?:-[A-Z]|[A-Z])?)"
# NOTE: allow inline type immediately after rule (e.g., ...1.2.2R or ...1.2.2BG)
ANCHOR_RE = re.compile(rf"^\s*CASS\s+{RULE_ID}(?P<typesuf>R|G|E|BG|C)?\s*$")
SECTION_RE = re.compile(r"^\s*Section\s*:\s*CASS\s+\d+[A-Z]?\.\d+", re.I)
TYPE_ONLY_RE = re.compile(r"^\s*(R|G|E|BG|C)\s*$", re.I)

# “Furniture” / boilerplate we drop from bodies
DROP_LINE_RE = re.compile(
    r"^\s*(www\.handbook\.fca\.org\.uk|FCA\s+\d{4}/\d+|Page\s+\d+\s+of\s+\d+|CASS|CHAPTER)\s*$",
    re.I,
)
# lines that scream “actionability table” or front-matter we don’t want
DROP_HINTS = [
    "Actions for damages",
    "Section 138D",
    "Rights of Action",
    "For private person",
    "Removed?",
    "For other person",
]

def norm_type(t: Optional[str]) -> str:
    return "R" if (t or "").upper() == "R" else "G"

# -------------------- word→line helpers --------------------
def words_to_lines(words: List[dict], y_tol: float) -> List[dict]:
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
    return {
        "text": " ".join(w["text"] for w in ws).strip(),
        "x0": min(w["x0"] for w in ws),
        "x1": max(w["x1"] for w in ws),
        "top": min(w["top"] for w in ws),
        "bottom": max(w["bottom"] for w in ws),
        "doctop": min(w["doctop"] for w in ws),
        "fonts": [w.get("fontname","") for w in ws],
    }

def is_bold_line(ln: dict) -> bool:
    return any("Bold" in (f or "") for f in ln.get("fonts", []))

def is_uppercase_furniture(s: str) -> bool:
    # drop short UPPERCASE-only lines (headings)
    t = re.sub(r"[^A-Za-z]+", "", s)
    return bool(t) and t.isupper() and len(t) >= 3 and len(t) <= 30

# -------------------- anchor detection --------------------
def find_anchors(page, y_tol: float, type_gap: float) -> List[dict]:
    words = page.extract_words(use_text_flow=True, keep_blank_chars=False,
                               extra_attrs=["fontname","size"])
    lines = words_to_lines(words, y_tol)
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
            # solitary type on the next tiny left-line?
            if not t and (i+1) < len(lines):
                nxt = lines[i+1]
                if (nxt["doctop"] - ln["doctop"]) <= type_gap and TYPE_ONLY_RE.match(nxt["text"]) and is_bold_line(nxt):
                    t = TYPE_ONLY_RE.match(nxt["text"]).group(1); i += 1
            anchors.append({
                "kind":"rule", "id": rid, "type": norm_type(t),
                "doctop": ln["doctop"], "x1": ln["x1"], "page": page.page_number-1
            })
        i += 1
    return anchors

# -------------------- body harvesting --------------------
def harvest_bodies(pdf, anchors: List[dict], body_margin: float, y_tol: float, min_body_len: int) -> Dict[Tuple[str,str], dict]:
    # prepack all lines per page
    page_lines = [words_to_lines(p.extract_words(use_text_flow=True, keep_blank_chars=False,
                                                 extra_attrs=["fontname","size"]), y_tol)
                  for p in pdf.pages]
    out: Dict[Tuple[str,str], dict] = {}

    for idx, anc in enumerate(anchors):
        if anc["kind"] != "rule": continue
        start_y = anc["doctop"]
        end_y   = anchors[idx+1]["doctop"] if idx+1 < len(anchors) else float("inf")

        body: List[str] = []

        # collect on the SAME PAGE (right of anchor.x1 + margin)
        lines = page_lines[anc["page"]]
        for ln in lines:
            if not (start_y <= ln["doctop"] < end_y): continue
            if ln["x0"] <= (anc["x1"] + body_margin): continue
            if should_drop_body_line(ln["text"]): continue
            body.append(ln["text"])

        # cautiously extend into NEXT PAGE ONLY if that page has no new anchor above a small threshold
        if anc["page"] + 1 < len(pdf.pages):
            # is the next anchor on the next page and very near the top? then stop.
            next_on_next_page = [a for a in anchors[idx+1:idx+5] if a.get("page") == anc["page"]+1 and a["kind"]=="rule"]
            take_next = True
            if next_on_next_page and next_on_next_page[0]["doctop"] < (pdf.pages[anc["page"]+1].height * 0.20):
                take_next = False
            if take_next:
                for ln in page_lines[anc["page"]+1]:
                    if ln["x0"] <= (pdf.pages[anc["page"]+1].width * 0.42):  # left col approx; keep right only
                        continue
                    if should_drop_body_line(ln["text"]): continue
                    body.append(ln["text"])

        text = "\n".join(collapse_blanks(body)).strip()
        if len(text) < min_body_len:
            continue

        key = (anc["id"], anc["type"])
        prev = out.get(key)
        if not prev or len(text) > len(prev["text"]):
            chap = anc["id"].split(".")[0]
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
                "display": f"CASS {anc['id']}",
            }

    return out

def should_drop_body_line(s: str) -> bool:
    if not s.strip(): return True
    if DROP_LINE_RE.match(s): return True
    if any(h.lower() in s.lower() for h in DROP_HINTS): return True
    if is_uppercase_furniture(s): return True
    # drop lone dot/leader noise
    if re.fullmatch(r"[.\-–—\s]+", s): return True
    return False

def collapse_blanks(lines: List[str]) -> List[str]:
    out, last_blank = [], False
    for ln in lines:
        blank = (ln.strip() == "")
        if blank and last_blank: continue
        out.append(ln); last_blank = blank
    return out

# -------------------- sort (1, 1A, 3, 5, 6, 7, then rest) --------------------
CHAPTER_ORDER = ["1", "1A", "3", "5", "6", "7"]
def sort_key(r: dict):
    chap = r["chapter"]
    try:
        chap_idx = CHAPTER_ORDER.index(chap)
    except ValueError:
        chap_idx = 99
    a,b,c = r["id"].split(".")
    sec = int(b)
    rule_num = int("".join(ch for ch in c if ch.isdigit()) or 0)
    rule_suf = "".join(ch for ch in c if not ch.isdigit())
    t_rank = 0 if r["type"] == "R" else 1
    return (chap_idx, a, sec, rule_num, rule_suf, t_rank)

# -------------------- CLI --------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", help="Path(s) to CASS PDFs")
    ap.add_argument("--out", default="data/rules.yaml", help="Output YAML path")
    ap.add_argument("--y-tol", type=float, default=3.0)
    ap.add_argument("--type-gap", type=float, default=10.0)
    ap.add_argument("--body-margin", type=float, default=6.0)
    ap.add_argument("--min-body-len", type=int, default=40)
    args = ap.parse_args()

    all_entries: Dict[Tuple[str,str], dict] = {}

    for p in args.pdfs:
        path = pathlib.Path(p)
        if not path.exists():
            print(f"[warn] missing {path}", file=sys.stderr); continue
        with pdfplumber.open(str(path)) as pdf:
            anchors = []
            for page in pdf.pages:
                anchors.extend(find_anchors(page, args.y_tol, args.type_gap))
            anchors.sort(key=lambda a: a["doctop"])

            bodies = harvest_bodies(pdf, anchors, args.body_margin, args.y_tol, args.min_body_len)
            for k,v in bodies.items():
                cur = all_entries.get(k)
                if not cur or len(v["text"]) > len(cur["text"]):
                    all_entries[k] = v

    items = sorted(all_entries.values(), key=sort_key)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        yaml.safe_dump(items, f, sort_keys=False, allow_unicode=True)
    print(f"[ok] wrote {len(items)} rules -> {out}")

if __name__ == "__main__":
    main()
