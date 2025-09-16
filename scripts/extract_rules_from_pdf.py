#!/usr/bin/env python3
"""
CASS extractor — per-page column detection (largest x0 gap), type-safe anchors,
strip leading anchor prefix from fused lines, keep first real sentence,
ignore headings/footers, robust page boundaries, paragraph reflow, de-dup, stable sort.

Run:
  python scripts/extract_rules_from_pdf.py \
    --left-max-ratio 0.46 --right-min-ratio 0.42 \
    --y-tol 3 --type-dx 18 --type-dy 4 \
    --body-margin 14 --start-slack 8 --heading-size-min 12 --min-body-len 40 \
    data/source_pdfs/*.pdf --out data/rules.yaml
"""

import argparse, pathlib, re, sys
from typing import Dict, List, Optional, Tuple
import pdfplumber, yaml
from statistics import median

# ------------------ patterns ------------------
ID_CORE = r"(?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)\.(?P<rule>\d+(?:-[A-Z]|[A-Z])?)"
CASS_RE = re.compile(r"^CASS$", re.I)
ID_TOKEN_RE = re.compile(rf"^{ID_CORE}(?P<trail>[A-Z]{{1,2}})?$")
TYPE_ONLY_RE = re.compile(r"^(R|G|E|BG|C)$", re.I)
SECTION_RE = re.compile(r"^\s*Section\s*:\s*CASS\s+\d+[A-Z]?\.\d+", re.I)

# Full-line anchor: drop entirely when it's a line by itself
ANCHOR_LINE_RE = re.compile(
    r"^\s*CASS\s+\d+[A-Z]?\.\d+\.\d+(?:-[A-Z]|[A-Z])?(?:\s+(?:R|G|E|BG|C))?\s*$",
    re.I,
)

# Leading anchor prefix (fused with body line on same baseline)
ANCHOR_PREFIX_RE = re.compile(
    r"^\s*CASS\s+\d+[A-Z]?\.\d+\.\d+(?:-[A-Z]|[A-Z])?(?:\s+(?:R|G|E|BG|C))?\s+",
    re.I,
)

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

def lower_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    if not letters: return 0.0
    return sum(1 for c in letters if c.islower()) / len(letters)

# ------------------ word → line ------------------
def words_sorted(page):
    return sorted(
        page.extract_words(
            use_text_flow=True, keep_blank_chars=False,
            extra_attrs=["fontname","size"]
        ),
        key=lambda w: (w["doctop"], w["x0"])
    )

def words_to_lines(words: List[dict], y_tol: float) -> List[dict]:
    if not words: return []
    lines, cur, cur_top = [], [], None
    for w in sorted(words, key=lambda t: (t["doctop"], t["x0"])):
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

# ------------------ heuristics ------------------
def looks_sentence_like(text: str) -> bool:
    t = text.strip()
    if not t: return False
    if re.search(r"[.;:)]", t): return True
    if len(t) >= 50 and lower_ratio(t) >= 0.25: return True
    return False

def looks_title_stub(text: str) -> bool:
    return bool(re.match(r"^[RG]\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*:?$", text.strip())) and not re.search(r"[.;)]", text)

def is_heading_line(ln: dict, body_size_guess: float, heading_size_min: float) -> bool:
    t = (ln["text"] or "").strip()
    if not t: return False
    letters = re.sub(r"[^A-Za-z]+", "", t)
    if letters and letters.isupper() and len(letters) <= 40 and not re.search(r"[.;:)]", t):
        return True
    if looks_title_stub(t): return True
    big = line_max_size(ln) >= heading_size_min
    bold = line_is_bold(ln)
    larger = line_med_size(ln) >= (body_size_guess + 1.2)
    if big and bold and larger and not re.search(r"[.;:)]", t): return True
    return False

def should_drop_line_text(s: str) -> bool:
    if not s.strip(): return True
    if ANCHOR_LINE_RE.match(s): return True          # full-line anchor -> drop
    if DROP_LINE_RE.match(s): return True
    if FOOTER_DATE_RE.search(s): return True
    if any(h.lower() in s.lower() for h in DROP_HINTS): return True
    if re.fullmatch(r"[.\-–—\s]+", s): return True
    return False

def strip_leading_anchor_prefix(s: str) -> str:
    """Remove a *leading* 'CASS x.x.x R/G' prefix if present; keep mid-text references."""
    return ANCHOR_PREFIX_RE.sub("", s).strip()

# ------------------ paragraph reflow ------------------
LIST_START_RE = re.compile(r"""^(
    \(\d+\)| \([a-z]\)| \d+\.\s| \([ivxlcdm]+\)
)$""", re.I | re.X)

def reflow_paragraphs_from_lines(lines: List[dict]) -> str:
    # sanitize and drop furniture
    cleaned = []
    for ln in lines:
        t = ln["text"]
        if should_drop_line_text(t):  # includes full-line anchors & furniture
            continue
        # also remove a *leading* anchor prefix if fused with body
        t2 = strip_leading_anchor_prefix(t)
        if not t2:
            continue
        cleaned.append(t2)

    # collapse duplicate blank lines
    tmp, last_blank = [], False
    for s in cleaned:
        if not s.strip():
            if not last_blank: tmp.append("")
            last_blank = True
        else:
            tmp.append(s.strip()); last_blank = False

    def dehyphen(s: str) -> str:
        return re.sub(r"(\w)-\s*$", r"\1", s)

    buf, acc = [], ""
    def commit():
        nonlocal acc
        if acc.strip(): buf.append(acc.strip())
        acc = ""

    for raw in tmp:
        s = raw
        if not s: commit(); continue
        s = dehyphen(s)
        first_token = s.split()[0] if s.split() else ""
        if LIST_START_RE.match(first_token): commit(); acc = s; continue
        if acc.endswith(":"): commit(); acc = s; continue
        acc = s if not acc else f"{acc} {s}"
    commit()
    return "\n\n".join(buf)

# ------------------ anchors ------------------
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
                j = i + 1
                while j < len(toks) and abs(toks[j]["doctop"] - t["doctop"]) <= y_tol:
                    cand = toks[j]
                    if cand["x0"] >= left_limit: break
                    m = ID_TOKEN_RE.match(cand["text"])
                    if m:
                        gd = m.groupdict()
                        rid = f"{gd['chapter']}.{gd['section']}.{gd['rule']}"
                        typ = None
                        start_x = cand["x1"]
                        # same-line type
                        k = j + 1
                        if k < len(toks):
                            nxt = toks[k]
                            same_band = abs(nxt["doctop"] - cand["doctop"]) <= type_dy
                            close_right = (0 <= (nxt["x0"] - cand["x1"]) <= type_dx)
                            if nxt["x0"] < left_limit and same_band and close_right and TYPE_ONLY_RE.match(nxt["text"]):
                                typ = nxt["text"].upper(); start_x = max(start_x, nxt["x1"])
                        # glued trail
                        if not typ:
                            trail = (gd.get("trail") or "").upper()
                            if trail in ("R","G","E","BG","C"): typ = trail
                        # next-line type
                        if not typ and (j + 1) < len(toks):
                            nxt = toks[j+1]
                            if nxt["x0"] < left_limit and 0 < (nxt["doctop"] - cand["doctop"]) <= type_dy and TYPE_ONLY_RE.match(nxt["text"]):
                                typ = nxt["text"].upper(); start_x = max(start_x, nxt["x1"])
                        anchors.append({
                            "kind":"rule","id":rid,"type":norm_type(typ),
                            "page":pi,"doctop":min(t["doctop"], cand["doctop"]),
                            "x1":start_x
                        })
                        break
                    j += 1
            elif SECTION_RE.match(t["text"]) and t["x0"] < left_limit:
                anchors.append({"kind":"section","page":pi,"doctop":t["doctop"],"x1":t["x1"]})
            i += 1
    anchors.sort(key=lambda a:(a["page"],a["doctop"]))
    return anchors

# ------------------ per-page right-column x0 ------------------
def estimate_right_col_x0(lines: List[dict], min_big_gap: float = 40.0) -> Optional[float]:
    xs = sorted(ln["x0"] for ln in lines if (ln.get("text") or "").strip())
    if len(xs) < 5: return None
    gaps = sorted([(xs[i+1]-xs[i], i) for i in range(len(xs)-1)], reverse=True)
    gap, idx = gaps[0]
    if gap >= min_big_gap:
        right_group = xs[idx+1:]
        return min(right_group) if right_group else None
    cut = int(len(xs)*0.6)
    right_group = xs[cut:]
    return min(right_group) if right_group else None

def page_right_x0s(pdf, page_lines: List[List[dict]], right_min_ratio: float) -> List[float]:
    xs = []
    for p, page in enumerate(pdf.pages):
        W = float(page.width)
        fallback = W * right_min_ratio
        candidates = [ln for ln in page_lines[p] if not should_drop_line_text(ln["text"])]
        x0 = estimate_right_col_x0(candidates)
        xs.append(x0 if x0 is not None else fallback)
    return xs

# ------------------ body start ------------------
def looks_like_heading_block_start(lines: List[dict], heading_size_min: float) -> int:
    if not lines: return 0
    sample = lines[:min(12, len(lines))]
    body_size_guess = median([line_med_size(ln) for ln in sample if (ln.get("text") or "").strip()] or [10.0])
    i, stripped = 0, 0
    while i < len(lines) and stripped < 3:
        ln = lines[i]; t = (ln.get("text") or "").strip()
        if not t or should_drop_line_text(t): i += 1; stripped += 1; continue
        if looks_sentence_like(t): break
        if is_heading_line(ln, body_size_guess, heading_size_min): i += 1; stripped += 1; continue
        break
    return min(i, len(lines)-1)

# ------------------ harvesting ------------------
def harvest_bodies(pdf, anchors: List[dict], y_tol: float, body_margin: float, right_min_ratio: float,
                   heading_size_min: float, min_body_len: int, start_slack: float) -> Dict[Tuple[str,str], dict]:
    out: Dict[Tu]()
