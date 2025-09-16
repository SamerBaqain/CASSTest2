#!/usr/bin/env python3
"""
CASS extractor (PyMuPDF edition)

- Uses PyMuPDF blocks/lines/spans (stable layout)
- Finds left-gutter anchors: "CASS <chapter>.<section>.<rule> [R|G]"
- Harvests only right-column lines from each anchor to the next (with end slack)
- Strips any *leading* "CASS n.n.n R/G " prefix if fused to the first body line
- Keeps first real sentence (skips only tiny heading block)
- Ignores headers/footers/furniture
- Reflows lines (de-hyphen, bullets) into paragraphs
- De-dups by (id,type) and sorts by chapter order 1, 1A, 3, 5, 6, 7

Usage:
  python scripts/extract_rules_from_pdf.py data/source_pdfs/*.pdf --out docs/data/rules.yaml
"""

import argparse, pathlib, re, sys
from typing import Dict, List, Optional, Tuple
import fitz  # PyMuPDF
import yaml
from statistics import median

# ------------ Patterns ------------
ID_CORE = r"(?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)\.(?P<rule>\d+(?:-[A-Z]|[A-Z])?)"
ANCHOR_FUSED = re.compile(rf"^\s*CASS\s+{ID_CORE}(?:\s+(?P<typ>R|G|E|BG|C))?\s*$", re.I)
ANCHOR_PREFIX = re.compile(rf"^\s*CASS\s+{ID_CORE}(?:\s+(?P<typ>R|G|E|BG|C))?\s+", re.I)
TYPE_ONLY = re.compile(r"^(R|G|E|BG|C)$", re.I)
SECTION_HDR = re.compile(r"^\s*Section\s*:\s*CASS\s+\d+[A-Z]?\.\d+", re.I)

DROP_LINE = re.compile(
    r"^\s*(www\.handbook\.fca\.org\.uk|FCA\s+\d{4}/\d+|Page\s+\d+\s+of\s+\d+)\s*$", re.I
)
FOOTER_DATE = re.compile(
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

CHAPTER_ORDER = ["1", "1A", "3", "5", "6", "7"]


# ------------ Utils ------------
def norm_type(t: Optional[str]) -> str:
    return "R" if (t or "").upper() == "R" else "G"

def is_bold(fonts: List[str]) -> bool:
    return any("Bold" in (f or "") for f in fonts)

def lower_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    if not letters: return 0.0
    return sum(1 for c in letters if c.islower()) / len(letters)

def looks_sentence_like(s: str) -> bool:
    s = s.strip()
    if not s: return False
    if re.search(r"[.;:)]", s): return True
    return len(s) >= 50 and lower_ratio(s) >= 0.25

def looks_title_stub(s: str) -> bool:
    return bool(re.match(r"^[RG]\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*:?$", s.strip())) and not re.search(r"[.;)]", s)

def is_heading_line(text: str, fonts: List[str], sizes: List[float], body_size_guess: float, heading_size_min: float) -> bool:
    t = (text or "").strip()
    if not t: return False
    letters = re.sub(r"[^A-Za-z]+", "", t)
    if letters and letters.isupper() and len(letters) <= 40 and not re.search(r"[.;:)]", t):
        return True
    if looks_title_stub(t): return True
    big = (max(sizes) if sizes else 0.0) >= heading_size_min
    bold = is_bold(fonts)
    larger = (median(sizes) if sizes else 0.0) >= (body_size_guess + 1.2)
    if big and bold and larger and not re.search(r"[.;:)]", t): return True
    return False

def should_drop_text(s: str) -> bool:
    if not s or not s.strip(): return True
    if DROP_LINE.match(s): return True
    if FOOTER_DATE.search(s): return True
    if any(h.lower() in s.lower() for h in DROP_HINTS): return True
    if re.fullmatch(r"[.\-–—\s]+", s): return True
    return False

def strip_leading_anchor_prefix(s: str) -> str:
    m = ANCHOR_PREFIX.match(s)
    if m:
        return s[m.end():].lstrip()
    return s


# ------------ Page parsing ------------
def page_lines(page) -> List[dict]:
    """Return line dicts: text, x0,x1,y0,y1, fonts, sizes."""
    d = page.get_text("dict")
    lines = []
    for b in d.get("blocks", []):
        if b.get("type", 0) != 0:
            continue
        for ln in b.get("lines", []):
            spans = ln.get("spans", [])
            if not spans: continue
            text = "".join(s.get("text", "") for s in spans).strip()
            if not text: continue
            x0 = min(s["bbox"][0] for s in spans)
            y0 = min(s["bbox"][1] for s in spans)
            x1 = max(s["bbox"][2] for s in spans)
            y1 = max(s["bbox"][3] for s in spans)
            fonts = [s.get("font", "") for s in spans]
            sizes = [float(s.get("size", 0)) for s in spans]
            lines.append({
                "text": text, "x0": x0, "x1": x1, "y0": y0, "y1": y1,
                "fonts": fonts, "sizes": sizes
            })
    lines.sort(key=lambda t: (t["y0"], t["x0"]))
    return lines


def detect_anchors_on_page(lines: List[dict], page_width: float, left_max_ratio: float, type_dy: float) -> List[dict]:
    """Find 'CASS <id> [R|G]' anchors in the left gutter."""
    left_limit = page_width * left_max_ratio
    anchors = []

    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln["x0"] >= left_limit:
            i += 1; continue

        txt = ln["text"]
        m = ANCHOR_FUSED.match(txt)
        if m:
            gd = m.groupdict()
            rid = f"{gd['chapter']}.{gd['section']}.{gd['rule']}"
            typ = norm_type(gd.get("typ"))
            # if type missing, try next tiny left-gutter line immediately below
            if not gd.get("typ") and (i + 1) < len(lines):
                nxt = lines[i+1]
                if nxt["x0"] < left_limit and abs(nxt["y0"] - ln["y0"]) <= type_dy and TYPE_ONLY.match(nxt["text"]):
                    typ = norm_type(nxt["text"])
            anchors.append({
                "id": rid, "type": typ, "y": ln["y0"]
            })
            i += 1; continue

        # Handle "CASS" in one line and id in the next
        if re.fullmatch(r"^\s*CASS\s*$", txt, re.I):
            if (i + 1) < len(lines) and lines[i+1]["x0"] < left_limit:
                m2 = ANCHOR_FUSED.match(lines[i+1]["text"])
                if m2:
                    gd = m2.groupdict()
                    rid = f"{gd['chapter']}.{gd['section']}.{gd['rule']}"
                    typ = norm_type(gd.get("typ"))
                    # maybe the type is one more tiny token
                    if not gd.get("typ") and (i + 2) < len(lines):
                        nxt = lines[i+2]
                        if nxt["x0"] < left_limit and abs(nxt["y0"] - lines[i+1]["y0"]) <= type_dy and TYPE_ONLY.match(nxt["text"]):
                            typ = norm_type(nxt["text"])
                    anchors.append({
                        "id": rid, "type": typ, "y": lines[i+1]["y0"]
                    })
                    i += 2; continue
        i += 1

    anchors.sort(key=lambda a: a["y"])
    return anchors


def right_column_x0(lines: List[dict], page_width: float, right_min_ratio: float) -> float:
    """Detect right-column start via largest x0 gap; fallback to width * ratio."""
    xs = sorted(ln["x0"] for ln in lines if (ln["text"].strip()))
    if len(xs) >= 6:
        gaps = [(xs[i+1]-xs[i], i) for i in range(len(xs)-1)]
        gaps.sort(reverse=True)
        if gaps and gaps[0][0] >= 40.0:
            idx = gaps[0][1]
            right_group = xs[idx+1:]
            if right_group:
                return min(right_group)
    return page_width * right_min_ratio


# ------------ Body harvesting ------------
def heading_block_start(lines: List[dict], heading_size_min: float) -> int:
    """Skip at most a few heading-ish lines; keep first sentence-like line."""
    if not lines: return 0
    sample = lines[:min(12, len(lines))]
    body_size_guess = median([median(ln["sizes"]) if ln["sizes"] else 10.0 for ln in sample])
    i, stripped = 0, 0
    while i < len(lines) and stripped < 3:
        t = lines[i]["text"]
        if not t.strip() or should_drop_text(t):
            i += 1; stripped += 1; continue
        if looks_sentence_like(t): break
        if is_heading_line(t, lines[i]["fonts"], lines[i]["sizes"], body_size_guess, heading_size_min):
            i += 1; stripped += 1; continue
        break
    return min(i, len(lines)-1)


def harvest(pdf: fitz.Document,
            left_max_ratio: float, right_min_ratio: float,
            type_dy: float, y_tol: float, start_slack: float, end_slack: float,
            heading_size_min: float, min_body_len: int) -> Dict[Tuple[str,str], dict]:

    all_page_lines = [page_lines(p) for p in pdf]
    page_widths = [p.rect.width for p in pdf]
    # per-page column start
    right_x0 = [
        right_column_x0(all_page_lines[i], page_widths[i], right_min_ratio)
        for i in range(len(pdf))
    ]

    # collect anchors across document
    doc_anchors = []
    for pi, p in enumerate(pdf):
        anchors = detect_anchors_on_page(all_page_lines[pi], page_widths[pi], left_max_ratio, type_dy)
        for a in anchors:
            a2 = dict(a); a2["page"] = pi
            doc_anchors.append(a2)
    doc_anchors.sort(key=lambda a: (a["page"], a["y"]))

    out: Dict[Tuple[str,str], dict] = {}

    for idx, anc in enumerate(doc_anchors):
        # band end = next anchor
        if idx + 1 < len(doc_anchors):
            next_a = doc_anchors[idx+1]
            end_page, end_y = next_a["page"], next_a["y"]
        else:
            end_page, end_y = len(pdf)-1, float("inf")

        start_page, start_y = anc["page"], anc["y"]

        # collect body lines from start anchor to end anchor (with slack)
        collected: List[dict] = []

        # ---- start page ----
        lines = all_page_lines[start_page]
        col0 = right_x0[start_page]
        start_cut = start_y - (y_tol / 2.0)
        stop_cut  = (end_y + end_slack) if end_page == start_page else float("inf")

        start_candidates = []
        for ln in lines:
            if ln["y0"] < start_cut: continue
            if ln["y0"] >= stop_cut: break
            if (ln["x0"] < col0 - start_slack) and (ln["x1"] < col0 + 2):  # left gutter
                continue
            t = ln["text"]
            if should_drop_text(t) or SECTION_HDR.match(t): continue

            # if this line is essentially the next rule's first line, skip & stop for this page
            if end_page == start_page and abs(ln["y0"] - end_y) <= (y_tol * 1.25) and ANCHOR_PREFIX.match(t):
                break

            # strip a leading "CASS <id> R/G " if fused with this rule
            t2 = strip_leading_anchor_prefix(t)
            if not t2.strip(): continue
            ln2 = dict(ln); ln2["text"] = t2
            start_candidates.append(ln2)

        sidx = heading_block_start(start_candidates, heading_size_min)
        collected.extend(start_candidates[sidx:])

        # ---- full spill pages ----
        for p in range(start_page + 1, end_page):
            col = right_x0[p]
            for ln in all_page_lines[p]:
                if (ln["x0"] < col - 2) and (ln["x1"] < col + 2): continue
                t = ln["text"]
                if should_drop_text(t) or SECTION_HDR.match(t): continue
                ln2 = dict(ln); ln2["text"] = strip_leading_anchor_prefix(t)
                if not ln2["text"].strip(): continue
                collected.append(ln2)

        # ---- end page chunk ----
        if end_page >= start_page:
            col_end = right_x0[end_page]
            for ln in all_page_lines[end_page]:
                if ln["y0"] >= (end_y + end_slack): break
                if (ln["x0"] < col_end - 2) and (ln["x1"] < col_end + 2): continue
                t = ln["text"]
                if should_drop_text(t) or SECTION_HDR.match(t): continue
                # exclude the next rule's fused first line
                if abs(ln["y0"] - end_y) <= (y_tol * 1.25) and ANCHOR_PREFIX.match(t):
                    break
                ln2 = dict(ln); ln2["text"] = strip_leading_anchor_prefix(t)
                if not ln2["text"].strip(): continue
                collected.append(ln2)

        # reflow paragraphs
        text = reflow(collected).strip()
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


# ------------ Reflow ------------
LIST_START = re.compile(r"""^(
    \(\d+\) | \([a-z]\) | \d+\.\s | \([ivxlcdm]+\)
)$""", re.I | re.X)

def reflow(lines: List[dict]) -> str:
    cleaned = []
    for ln in lines:
        t = (ln["text"] or "").strip()
        if not t: continue
        cleaned.append(t)

    # collapse blanks
    tmp: List[str] = []
    last_blank = False
    for s in cleaned:
        if not s:
            if not last_blank: tmp.append("")
            last_blank = True
        else:
            tmp.append(s); last_blank = False

    def dehyphen(s: str) -> str:
        return re.sub(r"(\w)-\s*$", r"\1", s)

    out, acc = [], ""
    def push():
        nonlocal acc
        if acc.strip(): out.append(acc.strip())
        acc = ""

    for raw in tmp:
        s = raw
        if not s:
            push(); continue
        s = dehyphen(s)
        tok = s.split()[0] if s.split() else ""
        if LIST_START.match(tok):
            push(); acc = s; continue
        if acc.endswith(":"):
            push(); acc = s; continue
        acc = s if not acc else f"{acc} {s}"
    push()
    return "\n\n".join(out)


# ------------ Sorting ------------
def sort_key(rec: dict):
    chap = rec["chapter"]
    chap_idx = CHAPTER_ORDER.index(chap) if chap in CHAPTER_ORDER else 99
    a,b,c = rec["id"].split(".")
    sec = int(b)
    num = int("".join(ch for ch in c if ch.isdigit()) or 0)
    suf = "".join(ch for ch in c if not ch.isdigit())
    t_rank = 0 if rec["type"] == "R" else 1
    return (chap_idx, a, sec, num, suf, t_rank)


# ------------ CLI ------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", help="CASS PDF(s)")
    ap.add_argument("--out", default="docs/data/rules.yaml", help="Output YAML")
    ap.add_argument("--left-max-ratio", type=float, default=0.46)
    ap.add_argument("--right-min-ratio", type=float, default=0.42)
    ap.add_argument("--y-tol", type=float, default=3.0)
    ap.add_argument("--type-dy", type=float, default=5.0)
    ap.add_argument("--start-slack", type=float, default=8.0)
    ap.add_argument("--end-slack", type=float, default=24.0)
    ap.add_argument("--heading-size-min", type=float, default=12.0)
    ap.add_argument("--min-body-len", type=int, default=40)
    args = ap.parse_args()

    all_entries: Dict[Tuple[str,str], dict] = {}

    for pth in args.pdfs:
        path = pathlib.Path(pth)
        if not path.exists():
            print(f"[warn] missing {path}", file=sys.stderr)
            continue
        doc = fitz.open(str(path))
        try:
            bodies = harvest(
                doc,
                args.left_max_ratio, args.right_min_ratio,
                args.type_dy, args.y_tol, args.start_slack, args.end_slack,
                args.heading_size_min, args.min_body_len
            )
            for k, v in bodies.items():
                cur = all_entries.get(k)
                if not cur or len(v["text"]) > len(cur["text"]):
                    all_entries[k] = v
        finally:
            doc.close()

    items = sorted(all_entries.values(), key=sort_key)
    outp = pathlib.Path(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    with outp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(items, f, sort_keys=False, allow_unicode=True)
    print(f"[ok] wrote", len(items), "rules ->", outp)

if __name__ == "__main__":
    main()
