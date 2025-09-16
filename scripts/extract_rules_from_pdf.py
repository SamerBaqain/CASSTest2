#!/usr/bin/env python3
"""
CASS extractor (adjacent-type, column-aware, paragraph reflow)

- Detect anchors only in the left gutter: "CASS <chapter>.<section>.<rule>"
- Classify type (R/G) by the token immediately to the right of the ID on the SAME LINE
  (bold single-token "R" / "G" / "E" / "BG" / "C"), with a very small fallback to the next line.
- Harvest body strictly to the RIGHT of the anchor.x1 (+margin) until next anchor/section.
- Reflow the right-column lines into paragraphs (no "one line per PDF line").
- De-duplicate by (id, type). Display is always "CASS <id>" (no R/G).
"""

import argparse, pathlib, re, sys
from typing import Dict, List, Optional, Tuple
import pdfplumber, yaml

# ----------- patterns -----------
ID_CORE = r"(?P<chapter>\d+[A-Z]?)\.(?P<section>\d+)\.(?P<rule>\d+(?:-[A-Z]|[A-Z])?)"
CASS_RE = re.compile(r"^CASS$", re.I)
ID_TOKEN_RE  = re.compile(rf"^{ID_CORE}(?P<trail>[A-Z]{{1,2}})?$")  # allows glued 7.11.14R / 1.2.2BG
TYPE_ONLY_RE = re.compile(r"^(R|G|E|BG|C)$", re.I)
SECTION_RE   = re.compile(r"^\s*Section\s*:\s*CASS\s+\d+[A-Z]?\.\d+", re.I)

# Furniture/noise we always drop from bodies
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

# ----------- utilities -----------
def words_sorted(page):
    return sorted(
        page.extract_words(use_text_flow=True, keep_blank_chars=False,
                           extra_attrs=["fontname","size"]),
        key=lambda w: (w["doctop"], w["x0"])
    )

def is_bold_token(w: dict) -> bool:
    return "Bold" in (w.get("fontname") or "")

def should_drop_line(s: str) -> bool:
    if not s.strip(): return True
    if DROP_LINE_RE.match(s): return True
    if any(h.lower() in s.lower() for h in DROP_HINTS): return True
    # Uppercase furniture (short headings)
    letters = re.sub(r"[^A-Za-z]+", "", s)
    if letters and letters.isupper() and 3 <= len(letters) <= 30:
        return True
    # dot/leader junk
    if re.fullmatch(r"[.\-–—\s]+", s): return True
    return False

def collapse_blanks(lines: List[str]) -> List[str]:
    out, last_blank = [], False
    for ln in lines:
        blank = (ln.strip() == "")
        if blank and last_blank: continue
        out.append(ln); last_blank = blank
    return out

# ----------- paragraph reflow -----------
LIST_START_RE = re.compile(r"""^(
    \(\d+\) |       # (1)
    \([a-z]\) |     # (a)
    \d+\.\s |       # 1. 
    \([ivxlcdm]+\)  # (i)
)$""", re.I | re.X)

def reflow_paragraphs(lines: List[str]) -> str:
    """
    Turn a list of 'visual lines' into readable paragraphs:
      - join lines with spaces,
      - keep breaks after ':' and before list markers ((1), (a), 1., (i), ...),
      - repair hyphenated wraps: 'trans-\nfer' -> 'transfer'
    """
    # 1) strip furniture and blank-collapse first
    lines = [ln for ln in lines if not should_drop_line(ln)]
    lines = collapse_blanks(lines)

    # 2) de-hyphenate: word-<linebreak>wrap -> wordwrap
    # (We can't see raw newlines here, so fix trailing hyphens at EOL)
    def dehyphen(s: str) -> str:
        return re.sub(r"(\w)-\s*$", r"\1", s)

    buf: List[str] = []
    acc = ""

    def commit():
        nonlocal acc
        if acc.strip():
            buf.append(acc.strip())
        acc = ""

    for i, raw in enumerate(lines):
        s = raw.strip()
        if not s:
            commit(); continue

        s = dehyphen(s)

        # start a new paragraph for list items, unless accumulator is empty
        if LIST_START_RE.match(s.split()[0]):
            commit()
            acc = s
            continue

        # if previous ended with ':' keep a break (new paragraph)
        if acc.endswith(":"):
            commit()
            acc = s
            continue

        # default: join with a space
        if not acc:
            acc = s
        else:
            # if previous ended with hyphen (already trimmed), just space-join
            acc = f"{acc} {s}"

    commit()

    # 3) merge paragraphs separated only by short blanks into a single block text
    return "\n\n".join(buf)

# ----------- anchor detection (adjacent-type, left gutter) -----------
def detect_anchors(pdf, left_max_ratio: float, y_tol: float, type_dx: float, type_dy: float) -> List[dict]:
    """
    Find anchors by token proximity:
      [CASS] [<id>] [R/G?]  (all in left gutter; type must be immediately adjacent in x with tiny y drift)
    """
    anchors: List[dict] = []

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
                # scan to find an ID token on roughly same band
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

                        # 1) same-line immediate neighbor to the right?
                        k = j + 1
                        if k < len(toks):
                            nxt = toks[k]
                            same_band = abs(nxt["doctop"] - cand["doctop"]) <= type_dy
                            close_right = (0 <= (nxt["x0"] - cand["x1"]) <= type_dx)
                            if nxt["x0"] < left_limit and same_band and close_right and TYPE_ONLY_RE.match(nxt["text"]):
                                typ = nxt["text"].upper()

                        # 2) If ID token had glued trail (e.g., 1.2.2R), use that (only R/G/E/BG/C)
                        if not typ:
                            trail = (gd.get("trail") or "").upper()
                            if trail in ("R","G","E","BG","C"):
                                typ = trail

                        # 3) Tiny fallback: next very-close line under the ID, still in left gutter
                        if not typ and (j + 1) < len(toks):
                            nxt = toks[j+1]
                            if nxt["x0"] < left_limit and 0 < (nxt["doctop"] - cand["doctop"]) <= type_dy and TYPE_ONLY_RE.match(nxt["text"]):
                                typ = nxt["text"].upper()

                        anchors.append({
                            "kind":"rule",
                            "id": rid,
                            "type": norm_type(typ),
                            "page": pi,
                            "doctop": min(t["doctop"], cand["doctop"]),
                            "x1": max(t["x1"], cand["x1"]),
                        })
                        break
                    j += 1
                # section banner? (rare)
            elif SECTION_RE.match(t["text"]) and t["x0"] < left_limit:
                anchors.append({"kind":"section","page":pi,"doctop":t["doctop"],"x1":t["x1"]})

            i += 1

    anchors.sort(key=lambda a: (a["page"], a["doctop"]))
    return anchors

# ----------- body harvesting (right of anchor, then paragraphs) -----------
def harvest_bodies(pdf, anchors: List[dict], body_margin: float, right_min_ratio: float, min_body_len: int) -> Dict[Tuple[str,str], dict]:
    out: Dict[Tuple[str,str], dict] = {}
    page_words = [words_sorted(p) for p in pdf.pages]
    widths = [float(p.width) for p in pdf.pages]

    for idx, anc in enumerate(anchors):
        if anc.get("kind") != "rule": continue

        start_page, start_y = anc["page"], anc["doctop"]
        # next boundary (rule or section)
        if idx + 1 < len(anchors):
            end_page, end_y = anchors[idx+1]["page"], anchors[idx+1]["doctop"]
        else:
            end_page, end_y = start_page, float("inf")

        # gather text on start page (to the right of anchor)
        raw_lines: List[str] = []
        for w in page_words[start_page]:
            if w["doctop"] < start_y: continue
            if end_page == start_page and w["doctop"] >= end_y: break
            if w["x0"] <= (anc["x1"] + body_margin): continue
            raw_lines.append(w["text"])

        # spill to intermediate pages (right half only) if needed
        if end_page > start_page:
            for p in range(start_page+1, end_page):
                right_min = widths[p] * right_min_ratio
                for w in page_words[p]:
                    if w["x0"] < right_min: continue
                    raw_lines.append(w["text"])
            # end page up to end_y
            right_min = widths[end_page] * right_min_ratio
            for w in page_words[end_page]:
                if w["doctop"] >= end_y: break
                if w["x0"] < right_min: continue
                raw_lines.append(w["text"])

        # reflow into paragraphs
        text = reflow_paragraphs(raw_lines).strip()
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

# ----------- sort order (1, 1A, 3, 5, 6, 7, then rest) -----------
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

# ----------- CLI -----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", help="CASS PDF(s)")
    ap.add_argument("--out", default="data/rules.yaml", help="Output YAML")
    # detection / geometry
    ap.add_argument("--left-max-ratio", type=float, default=0.45, help="Left gutter max x ratio (anchors live here)")
    ap.add_argument("--right-min-ratio", type=float, default=0.42, help="Right column min x ratio for spill pages")
    ap.add_argument("--y-tol", type=float, default=3.0, help="Same-line y tolerance for CASS+ID")
    ap.add_argument("--type-dx", type=float, default=18.0, help="Max x gap to treat token as adjacent type (same line)")
    ap.add_argument("--type-dy", type=float, default=4.0, help="Max y drift for same-line type; also tiny fallback to next line")
    ap.add_argument("--body-margin", type=float, default=10.0, help="Points to the right of anchor.x1 to start body")
    ap.add_argument("--min-body-len", type=int, default=40, help="Drop bodies shorter than this many chars")
    args = ap.parse_args()

    all_entries: Dict[Tuple[str,str], dict] = {}

    for pth in args.pdfs:
        path = pathlib.Path(pth)
        if not path.exists():
            print(f"[warn] missing {path}", file=sys.stderr); continue
        with pdfplumber.open(str(path)) as pdf:
            anchors = detect_anchors(pdf, args.left_max_ratio, args.y_tol, args.type_dx, args.type_dy)
            if not anchors:
                print(f"[warn] no anchors found in {path}", file=sys.stderr)
            bodies = harvest_bodies(pdf, anchors, args.body_margin, args.right_min_ratio, args.min_body_len)
            for k, v in bodies.items():
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
