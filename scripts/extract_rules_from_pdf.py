#!/usr/bin/env python3
"""
CASS extractor (token-proximity anchors, column-aware, robust filters)

Outputs entries like:
- id: "1.2.2"
  chapter: "1"
  type: "R"
  display: "CASS 1.2.2"
  text: "... full body ..."

Tunable CLI flags (defaults chosen for CASS handbook PDFs):
  --left-max-ratio 0.45   # anything left of 45% page width is "left gutter" (anchors live here)
  --y-tol 3               # y tolerance for grouping nearby words to a token band
  --type-gap 12           # max doctop gap to treat the next token as a standalone R/G
  --body-margin 8         # body must start at x0 > anchor.x1 + margin (points)
  --min-body-len 40       # drop tiny bodies (likely TOC/artefacts)
"""

import argparse, pathlib, re, sys
from typing import Dict, List, Optional, Tuple
import pdfplumber, yaml

# -------- patterns --------
ID_CORE = r"(?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)\.(?P<rule>\d+(?:-[A-Z]|[A-Z])?)"
CASS_WORD_RE = re.compile(r"^CASS$", re.I)
ID_TOKEN_RE  = re.compile(rf"^{ID_CORE}(?P<trail>[A-Z]{{1,2}})?$")  # may be glued like 7.11.14R or 7.11.14BG
TYPE_ONLY_RE = re.compile(r"^(R|G|E|BG|C)$", re.I)
SECTION_RE   = re.compile(r"^\s*Section\s*:\s*CASS\s+\d+[A-Z]?\.\d+", re.I)

DROP_LINE_RE = re.compile(
    r"^\s*(www\.handbook\.fca\.org\.uk|FCA\s+\d{4}/\d+|Page\s+\d+\s+of\s+\d+)\s*$",
    re.I,
)
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

# -------- utilities --------
def words_sorted(page):
    # include font info so we can tell bold R/G
    return sorted(
        page.extract_words(use_text_flow=True, keep_blank_chars=False,
                           extra_attrs=["fontname","size"]),
        key=lambda w: (w["doctop"], w["x0"])
    )

def is_bold(word: dict) -> bool:
    return "Bold" in (word.get("fontname") or "")

def is_uppercase_furniture(s: str) -> bool:
    letters = re.sub(r"[^A-Za-z]+", "", s)
    return bool(letters) and letters.isupper() and 3 <= len(letters) <= 30

def should_drop_line(s: str) -> bool:
    if not s.strip(): return True
    if DROP_LINE_RE.match(s): return True
    if any(h.lower() in s.lower() for h in DROP_HINTS): return True
    if is_uppercase_furniture(s): return True
    if re.fullmatch(r"[.\-–—\s]+", s): return True
    return False

def collapse_blanks(lines: List[str]) -> List[str]:
    out, last_blank = [], False
    for ln in lines:
        blank = (ln.strip() == "")
        if blank and last_blank: continue
        out.append(ln); last_blank = blank
    return out

# -------- anchor detection (token proximity in left gutter) --------
def detect_anchors(pdf, left_max_ratio: float, y_tol: float, type_gap: float) -> List[dict]:
    anchors = []
    for pi, page in enumerate(pdf.pages):
        W = float(page.width)
        left_limit = W * left_max_ratio
        words = words_sorted(page)

        i = 0
        while i < len(words):
            w = words[i]
            if w["x0"] >= left_limit:
                i += 1; continue  # only look for anchors in left gutter

            if CASS_WORD_RE.match(w["text"]):
                # look ahead for an ID token on (roughly) same y band
                j = i + 1
                while j < len(words) and words[j]["doctop"] - w["doctop"] <= y_tol*1.5:
                    cand = words[j]
                    if cand["x0"] >= left_limit:  # still in left gutter
                        break
                    m = ID_TOKEN_RE.match(cand["text"])
                    if m:
                        gd = m.groupdict()
                        rid = f"{gd['chapter']}.{gd['section']}.{gd['rule']}"
                        typ = None
                        trail = (gd.get("trail") or "").upper()  # glued R/G/BG?
                        if trail in ("R","G","E","BG","C"):
                            typ = trail
                            # fix id if trail was actually type (do not clip legitimate letter suffix like ...1A)
                            # We only clip if the last char(s) are R/G/E/BG/C and previous char is a digit.
                            # Already ensured by regex: trail is separate capture.
                        # if still no type, check the next token nearby as type-only
                        k = j + 1
                        if not typ and k < len(words):
                            nxt = words[k]
                            if nxt["doctop"] - cand["doctop"] <= type_gap and nxt["x0"] < left_limit and TYPE_ONLY_RE.match(nxt["text"]):
                                # prefer bold, but accept any if font info missing
                                if is_bold(nxt) or not nxt.get("fontname"):
                                    typ = nxt["text"].upper()
                                    j = k  # consume
                        anchors.append({
                            "kind":"rule",
                            "id": rid,
                            "type": norm_type(typ),
                            "page": pi,
                            "doctop": min(w["doctop"], cand["doctop"]),
                            "x1": max(w["x1"], cand["x1"]),
                        })
                        break
                    j += 1
            else:
                # section banner boundary (rare—keep as a boundary)
                if SECTION_RE.match(w["text"]) and w["x0"] < left_limit:
                    anchors.append({"kind":"section","page":pi,"doctop":w["doctop"],"x1":w["x1"]})
            i += 1

    anchors.sort(key=lambda a: (a["page"], a["doctop"]))
    return anchors

# -------- body harvesting --------
def harvest_bodies(pdf, anchors: List[dict], body_margin: float, min_body_len: int) -> Dict[Tuple[str,str], dict]:
    out: Dict[Tuple[str,str], dict] = {}

    # Pre-load words per page (right column collection later)
    page_words = [words_sorted(p) for p in pdf.pages]
    page_widths = [float(p.width) for p in pdf.pages]
    page_heights = [float(p.height) for p in pdf.pages]

    for idx, anc in enumerate(anchors):
        if anc.get("kind") != "rule": continue

        start_page, start_y = anc["page"], anc["doctop"]
        end_y = float("inf"); end_page = start_page
        # find next boundary (rule/section)
        for k in range(idx+1, len(anchors)):
            end_page = anchors[k]["page"]; end_y = anchors[k]["doctop"]
            break

        body_lines: List[str] = []

        # Same page: take words to the right of anchor.x1 + margin, from start_y to end (or page end if next boundary on later page)
        words = page_words[start_page]
        for w in words:
            if w["doctop"] < start_y: continue
            # if next boundary on same page, stop before it
            if end_page == start_page and w["doctop"] >= end_y: break
            if w["x0"] <= (anc["x1"] + body_margin): continue
            if should_drop_line(w["text"]): continue
            body_lines.append(w["text"])

        # If boundary is on a later page, we may need to spill onto intermediate page(s)
        if end_page > start_page:
            # collect from pages (start_page+1) up to end_page-1: keep only right half
            for pi in range(start_page+1, end_page):
                right_min = page_widths[pi] * 0.42
                for w in page_words[pi]:
                    if w["x0"] < right_min: continue
                    if should_drop_line(w["text"]): continue
                    body_lines.append(w["text"])
            # on the end_page, include words before end_y, right half only
            right_min_end = page_widths[end_page] * 0.42
            for w in page_words[end_page]:
                if w["doctop"] >= end_y: break
                if w["x0"] < right_min_end: continue
                if should_drop_line(w["text"]): continue
                body_lines.append(w["text"])

        text = "\n".join(collapse_blanks(body_lines)).strip()
        if len(text) < min_body_len:
            continue

        key = (anc["id"], anc["type"])
        prev = out.get(key)
        if not prev or len(text) > len(prev["text"]):
            chapter = anc["id"].split(".")[0]  # keeps letter (e.g., "1A")
            out[key] = {
                "id": anc["id"],
                "chapter": chapter,
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

# -------- sorting (1, 1A, 3, 5, 6, 7, then rest) --------
CHAPTER_ORDER = ["1", "1A", "3", "5", "6", "7"]
def sort_key(rec: dict):
    chap = rec["chapter"]
    try:
        chap_idx = CHAPTER_ORDER.index(chap)
    except ValueError:
        chap_idx = 99
    a,b,c = rec["id"].split(".")
    sec = int(b)
    rule_num = int("".join(ch for ch in c if ch.isdigit()) or 0)
    rule_suf = "".join(ch for ch in c if not ch.isdigit())
    t_rank = 0 if rec["type"] == "R" else 1
    return (chap_idx, a, sec, rule_num, rule_suf, t_rank)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", help="CASS PDF(s)")
    ap.add_argument("--out", default="data/rules.yaml", help="Output YAML")
    ap.add_argument("--left-max-ratio", type=float, default=0.45)
    ap.add_argument("--y-tol", type=float, default=3.0)
    ap.add_argument("--type-gap", type=float, default=12.0)
    ap.add_argument("--body-margin", type=float, default=8.0)
    ap.add_argument("--min-body-len", type=int, default=40)
    args = ap.parse_args()

    all_entries: Dict[Tuple[str,str], dict] = {}
    for pth in args.pdfs:
        path = pathlib.Path(pth)
        if not path.exists():
            print(f"[warn] missing {path}", file=sys.stderr); continue

        with pdfplumber.open(str(path)) as pdf:
            anchors = detect_anchors(pdf, args.left_max_ratio, args.y_tol, args.type_gap)
            if not anchors:
                print(f"[warn] no anchors found in {path}", file=sys.stderr)
            bodies = harvest_bodies(pdf, anchors, args.body_margin, args.min_body_len)
            for k,v in bodies.items():
                cur = all_entries.get(k)
                if not cur or len(v["text"]) > len(cur["text"]):
                    all_entries[k] = v

    items = sorted(all_entries.values(), key=sort_key)
    outp = pathlib.Path(args.out); outp.parent.mkdir(parents=True, exist_ok=True)
    with outp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(items, f, sort_keys=False, allow_unicode=True)
    print(f"[ok] wrote {len(items)} rules -> {outp}")

if __name__ == "__main__":
    main()
