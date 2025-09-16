#!/usr/bin/env python3
"""
CASS extractor — token-proximity anchors, adjacent type (R/G) safe,
heading/footers filtered, first-sentence keep, paragraph reflow, de-dup.

What it does
------------
• Detect anchors ONLY in the left gutter:  CASS <chapter>.<section>.<rule> [R/G/E/BG/C?]
• If a type token (R/G) is adjacent, we capture it and start the body strictly to the
  RIGHT of the farthest x1 of the ID/Type cluster (+ body margin). This prevents R/G
  leaking into the body.
• Build body from LINES (not raw words), filter real headings (big+bold) and footer dates
  like “September 2025”, but KEEP the first sentence-like line even if bold (so we
  don’t lose the true first sentence).
• Reflow the right-column lines into paragraphs (single newlines collapse to spaces).
• De-duplicate by (id,type). Output is a YAML LIST of rules with:
  id, chapter, type (R/G), display ("CASS <id>"), text, and standard placeholders.

CLI tuning (defaults chosen for FCA CASS PDFs)
----------------------------------------------
--left-max-ratio 0.46  : anchors exist left of this x ratio (left gutter)
--right-min-ratio 0.42 : when spilling onto next pages, only keep lines to the right of this
--y-tol 3              : group tokens into lines, and detect "same line" bands
--type-dx 18           : max x gap for adjacent same-line type token
--type-dy 4            : max y drift for adjacent/next-line type token
--body-margin 14       : extra points to the right of ID/Type cluster before body begins
--heading-size-min 12  : lines with max font size >= this AND bold are treated as headings
--min-body-len 40      : drop tiny bodies (likely TOC/artefacts)

Requires: pdfplumber>=0.10, PyYAML>=6.0
"""

import argparse, pathlib, re, sys
from typing import Dict, List, Optional, Tuple
import pdfplumber, yaml
from statistics import median

# ------------ patterns ------------
ID_CORE = r"(?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)\.(?P<rule>\d+(?:-[A-Z]|[A-Z])?)"
CASS_RE = re.compile(r"^CASS$", re.I)
ID_TOKEN_RE = re.compile(rf"^{ID_CORE}(?P<trail>[A-Z]{{1,2}})?$")  # accepts glued 7.11.14R or 1.2.2BG
TYPE_ONLY_RE = re.compile(r"^(R|G|E|BG|C)$", re.I)
SECTION_RE = re.compile(r"^\s*Section\s*:\s*CASS\s+\d+[A-Z]?\.\d+", re.I)

# Furniture to drop from bodies
DROP_LINE_RE = re.compile(
    r"^\s*(www\.handbook\.fca\.org\.uk|FCA\s+\d{4}/\d+|Page\s+\d+\s+of\s+\d+)\s*$",
    re.I,
)
FOOTER_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+20\d{2}\b",
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

# ------------ word→line helpers ------------
def words_sorted(page):
    return sorted(
        page.extract_words(
            use_text_flow=True, keep_blank_chars=False,
            extra_attrs=["fontname","size"]
        ),
        key=lambda w: (w["doctop"], w["x0"])
    )

def words_to_lines(words: List[dict], y_tol: float) -> List[dict]:
    """
    Group tokens into visual lines keyed by doctop proximity.
    Each line carries text, x0/x1, doctop, fonts list, sizes list.
    """
    if not words:
        return []
    lines: List[dict] = []
    cur: List[dict] = []
    cur_top = None
    for w in sorted(words, key=lambda t: (t["doctop"], t["x0"])):
        top = w["top"]
        if cur_top is None or abs(top - cur_top) <= y_tol:
            cur.append(w)
            cur_top = top if cur_top is None else min(cur_top, top)
        else:
            lines.append(_pack_line(cur))
            cur = [w]
            cur_top = top
    if cur:
        lines.append(_pack_line(cur))
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
        "sizes": [float(w.get("size",0)) for w in ws],
    }

def line_is_bold(ln: dict) -> bool:
    return any("Bold" in (f or "") for f in ln.get("fonts", []))

def line_max_size(ln: dict) -> float:
    sizes = ln.get("sizes") or [0]
    return max(sizes) if sizes else 0.0

def line_med_size(ln: dict) -> float:
    sizes = ln.get("sizes") or [0]
    return float(median(sizes)) if sizes else 0.0

# ------------ heading & sentence heuristics ------------
def looks_like_heading(ln: dict, heading_size_min: float) -> bool:
    """
    True for real headings only:
      • big AND bold, or
      • starts with 'R ' or 'G ' followed by TitleCase, short and with no sentence punctuation.
    """
    t = (ln["text"] or "").strip()
    if not t:
        return False

    is_big  = line_max_size(ln) >= heading_size_min
    is_bold = line_is_bold(ln)

    # Pattern headings like "R Application", "G Purpose"
    if re.match(r"^[RG]\s+[A-Z][a-z]+(?:\b|:)", t) and len(t) <= 80 and not re.search(r"[.;]", t):
        return True

    # Only drop if both big AND bold (prevents losing first sentences that happen to be bold)
    if is_big and is_bold:
        # Uppercase short stubs (e.g. section banners)
        letters = re.sub(r"[^A-Za-z]+", "", t)
        if letters and letters.isupper() and 3 <= len(letters) <= 40:
            return True
        # Also treat short, no-punct title lines as headings
        if len(t) <= 80 and not re.search(r"[.;:]", t):
            return True

    return False

def looks_sentence_like(text: str) -> bool:
    """True if the line looks like running sentence text, not a header."""
    t = text.strip()
    if not t:
        return False
    # has lowercase and either punctuation or is reasonably long
    return bool(re.search(r"[a-z]", t)) and (bool(re.search(r"[.;:)]", t)) or len(t) > 60)

def should_drop_line_text(s: str) -> bool:
    if not s.strip(): return True
    if DROP_LINE_RE.match(s): return True
    if FOOTER_DATE_RE.search(s): return True
    if any(h.lower() in s.lower() for h in DROP_HINTS): return True
    # dot leaders etc.
    if re.fullmatch(r"[.\-–—\s]+", s): return True
    return False

# ------------ paragraph reflow ------------
LIST_START_RE = re.compile(r"""^(
    \(\d+\)|           # (1)
    \([a-z]\)|         # (a)
    \d+\.\s|           # 1. 
    \([ivxlcdm]+\)     # (i)
)$""", re.I | re.X)

def reflow_paragraphs_from_lines(lines: List[dict]) -> str:
    """
    Reflow a list of body LINES into paragraphs, preserving list breaks.
    """
    # clean and collapse blanks
    cleaned: List[str] = []
    for ln in lines:
        if should_drop_line_text(ln["text"]): 
            continue
        cleaned.append(ln["text"])

    tmp: List[str] = []
    last_blank = False
    for s in cleaned:
        if not s.strip():
            if not last_blank:
                tmp.append("")
            last_blank = True
        else:
            tmp.append(s.strip())
            last_blank = False

    # de-hyphenate at EOL (word-)
    def dehyphen(s: str) -> str:
        return re.sub(r"(\w)-\s*$", r"\1", s)

    buf: List[str] = []
    acc = ""

    def commit():
        nonlocal acc
        if acc.strip():
            buf.append(acc.strip())
        acc = ""

    for raw in tmp:
        s = raw
        if not s:
            commit(); 
            continue
        s = dehyphen(s)

        # new paragraph for list starters
        first_token = s.split()[0] if s.split() else ""
        if LIST_START_RE.match(first_token):
            commit()
            acc = s
            continue

        # if previous ended with ':' keep a break
        if acc.endswith(":"):
            commit()
            acc = s
            continue

        # default: join with space
        if not acc:
            acc = s
        else:
            acc = f"{acc} {s}"

    commit()
    return "\n\n".join(buf)

# ------------ anchor detection (adjacent type captured; start_x recorded) ------------
def detect_anchors(pdf, left_max_ratio: float, y_tol: float, type_dx: float, type_dy: float) -> List[dict]:
    anchors = []
    for pi, page in enumerate(pdf.pages):
        W = float(page.width)
        left_limit = W * left_max_ratio
        toks = words_sorted(page)
        i = 0
        while i < len(toks):
            t = toks[i]
            if t["x0"] >= left_limit:
                i += 1; continue

            if CASS_RE.match(t["text"]):
                # seek ID token on same band
                j = i + 1
                while j < len(toks) and abs(toks[j]["doctop"] - t["doctop"]) <= y_tol:
                    cand = toks[j]
                    if cand["x0"] >= left_limit:
                        break
                    m = ID_TOKEN_RE.match(cand["text"])
                    if m:
                        gd = m.groupdict()
                        rid = f"{gd['chapter']}.{gd['section']}.{gd['rule']}"
                        typ = None
                        start_x = cand["x1"]  # right edge of ID token by default

                        # same-line adjacent type?
                        k = j + 1
                        if k < len(toks):
                            nxt = toks[k]
                            same_band = abs(nxt["doctop"] - cand["doctop"]) <= type_dy
                            close_right = (0 <= (nxt["x0"] - cand["x1"]) <= type_dx)
                            if nxt["x0"] < left_limit and same_band and close_right and TYPE_ONLY_RE.match(nxt["text"]):
                                typ = nxt["text"].upper()
                                start_x = max(start_x, nxt["x1"])  # extend start barrier to right edge of R/G

                        # glued trail (…1.2.2R)
                        if not typ:
                            trail = (gd.get("trail") or "").upper()
                            if trail in ("R","G","E","BG","C"):
                                typ = trail
                                # start_x already includes glued trail (cand["x1"])

                        # tiny fallback: next very-close line still in left gutter
                        if not typ and (j + 1) < len(toks):
                            nxt = toks[j+1]
                            if nxt["x0"] < left_limit and 0 < (nxt["doctop"] - cand["doctop"]) <= type_dy and TYPE_ONLY_RE.match(nxt["text"]):
                                typ = nxt["text"].upper()
                                start_x = max(start_x, nxt["x1"])

                        anchors.append({
                            "kind":"rule",
                            "id": rid,
                            "type": norm_type(typ),
                            "page": pi,
                            "doctop": min(t["doctop"], cand["doctop"]),
                            "x1": start_x,      # far-right edge of ID/Type cluster
                        })
                        break
                    j += 1

            elif SECTION_RE.match(t["text"]) and t["x0"] < left_limit:
                anchors.append({"kind":"section","page":pi,"doctop":t["doctop"],"x1":t["x1"]})

            i += 1

    anchors.sort(key=lambda a: (a["page"], a["doctop"]))
    return anchors

# ------------ body harvesting using LINES (heading/date filters; keep first sentence) ------------
def harvest_bodies(pdf, anchors: List[dict], y_tol: float, body_margin: float, right_min_ratio: float,
                   heading_size_min: float, min_body_len: int) -> Dict[Tuple[str,str], dict]:
    out: Dict[Tuple[str,str], dict] = {}

    # Precompute lines per page
    page_lines = [words_to_lines(words_sorted(p), y_tol) for p in pdf.pages]
    widths = [float(p.width) for p in pdf.pages]

    for idx, anc in enumerate(anchors):
        if anc.get("kind") != "rule":
            continue

        start_page, start_y = anc["page"], anc["doctop"]
        if idx + 1 < len(anchors):
            end_page, end_y = anchors[idx+1]["page"], anchors[idx+1]["doctop"]
        else:
            end_page, end_y = start_page, float("inf")

        start_bar = anc["x1"] + body_margin  # do not cross ID/Type cluster + margin

        raw_lines: List[dict] = []
        first_kept = False  # ensure we keep a sentence-like first line even if bold

        # Start page: only lines to the right of start_bar
        for ln in page_lines[start_page]:
            if ln["doctop"] < start_y:
                continue
            if end_page == start_page and ln["doctop"] >= end_y:
                break
            if ln["x0"] <= start_bar:
                continue
            if should_drop_line_text(ln["text"]):
                continue

            # Treat as heading only if it truly looks like one; otherwise keep the first sentence-like line.
            if looks_like_heading(ln, heading_size_min):
                if not first_kept and looks_sentence_like(ln["text"]):
                    raw_lines.append(ln)
                    first_kept = True
                else:
                    continue
            else:
                raw_lines.append(ln)
                first_kept = True

        # Spill to intermediate pages (right half only)
        if end_page > start_page:
            for p in range(start_page+1, end_page):
                right_min = widths[p] * right_min_ratio
                for ln in page_lines[p]:
                    if ln["x0"] < right_min:
                        continue
                    if should_drop_line_text(ln["text"]) or looks_like_heading(ln, heading_size_min):
                        continue
                    raw_lines.append(ln)
            # End page up to end_y
            right_min = widths[end_page] * right_min_ratio
            for ln in page_lines[end_page]:
                if ln["doctop"] >= end_y:
                    break
                if ln["x0"] < right_min:
                    continue
                if should_drop_line_text(ln["text"]) or looks_like_heading(ln, heading_size_min):
                    continue
                raw_lines.append(ln)

        # Reflow into paragraphs
        text = reflow_paragraphs_from_lines(raw_lines).strip()
        if len(text) < min_body_len:
            continue

        key = (anc["id"], anc["type"])
        prev = out.get(key)
        if not prev or len(text) > len(prev["text"]):
            chapter = anc["id"].split(".")[0]
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

# ------------ sort order ------------
CHAPTER_ORDER = ["1", "1A", "3", "5", "6", "7"]
def sort_key(rec: dict):
    chap = rec["chapter"]
    chap_idx = CHAPTER_ORDER.index(chap) if chap in CHAPTER_ORDER else 99
    a,b,c = rec["id"].split(".")
    sec = int(b)
    rule_num = int("".join(ch for ch in c if ch.isdigit()) or 0)
    rule_suf = "".join(ch for ch in c if not ch.isdigit())
    t_rank = 0 if rec["type"] == "R" else 1
    return (chap_idx, a, sec, rule_num, rule_suf, t_rank)

# ------------ CLI ------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", help="CASS PDF(s)")
    ap.add_argument("--out", default="data/rules.yaml", help="Output YAML")
    # geometry / detection
    ap.add_argument("--left-max-ratio", type=float, default=0.46, help="x ratio for left gutter max (anchors live here)")
    ap.add_argument("--right-min-ratio", type=float, default=0.42, help="x ratio for right column on spill pages")
    ap.add_argument("--y-tol", type=float, default=3.0, help="y tolerance to group tokens into lines")
    ap.add_argument("--type-dx", type=float, default=18.0, help="max x gap for adjacent type on same line")
    ap.add_argument("--type-dy", type=float, default=4.0, help="max y drift for adjacent/next-line type")
    ap.add_argument("--body-margin", type=float, default=14.0, help="extra points to right of ID/Type cluster")
    ap.add_argument("--heading-size-min", type=float, default=12.0, help="min max font size to treat a line as heading")
    ap.add_argument("--min-body-len", type=int, default=40, help="drop tiny bodies (avoid TOC/artefacts)")
    args = ap.parse_args()

    all_entries: Dict[Tuple[str,str], dict] = {}

    for pth in args.pdfs:
        path = pathlib.Path(pth)
        if not path.exists():
            print(f"[warn] missing {path}", file=sys.stderr); 
            continue
        with pdfplumber.open(str(path)) as pdf:
            anchors = detect_anchors(pdf, args.left_max_ratio, args.y_tol, args.type_dx, args.type_dy)
            if not anchors:
                print(f"[warn] no anchors found in {path}", file=sys.stderr)
            bodies = harvest_bodies(pdf, anchors, args.y_tol, args.body_margin, args.right_min_ratio,
                                    args.heading_size_min, args.min_body_len)
            for k,v in bodies.items():
                cur = all_entries.get(k)
                if not cur or len(v["text"]) > len(cur["text"]):
                    all_entries[k] = v

    items = sorted(all_entries.values(), key=sort_key)
    outp = pathlib.Path(args.out); outp.parent.mkdir(parents=True, exist_ok=True)
    with outp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(items, f, sort_keys=False, allow_unicode=True)
    print(f"[ok] wrote", len(items), "rules ->", outp)

if __name__ == "__main__":
    main()
