import pdfplumber, re, argparse, pathlib, yaml, sys

RULE_ID_RE = re.compile(r"CASS\s+(\d+[A-Z]?)\.(\d+)\.(\d+[A-Z]?)")
TYPE_RE = re.compile(r"^(R|G|E|BG|C)$")
DROP_LINES_RE = re.compile(r"^\s*(www\.handbook\.fca\.org\.uk|FCA\s+\d{4}/\d+|Page\s+\d+\s+of\s+\d+)\s*$", re.I)

def group_words_by_line(words, y_tol=3.0):
    ''' Group words into lines using y position '''
    lines = []
    words = sorted(words, key=lambda w: (w['top'], w['x0']))
    cur_line = []
    cur_top = None
    for w in words:
        top = w['top']
        if cur_top is None or abs(top - cur_top) <= y_tol:
            cur_line.append(w)
            cur_top = top if cur_top is None else min(cur_top, top)
        else:
            lines.append(cur_line)
            cur_line = [w]
            cur_top = top
    if cur_line:
        lines.append(cur_line)
    return lines

def extract_rules_from_page(page):
    rules = []
    words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=True, use_text_flow=True)
    lines = group_words_by_line(words)
    for line in lines:
        if not line or len(line) < 3:  # Must have at least Rule, Type, Body
            continue
        # Find index of the 'CASS' rule anchor
        for i, w in enumerate(line):
            m = RULE_ID_RE.match(w['text'])
            if m:
                rule_number = m.group(0).replace("  ", " ").strip()
                # Next word should be R/G/E etc
                if i+1 < len(line) and TYPE_RE.match(line[i+1]['text']):
                    rule_type = line[i+1]['text']
                    # Everything after that is the rule text
                    body_words = [w['text'] for w in line[i+2:]]
                    rule_body = " ".join(body_words).strip()
                    if rule_body and not DROP_LINES_RE.match(rule_body):
                        rules.append({
                            "id": rule_number.replace("CASS ", ""),
                            "type": rule_type,
                            "text": rule_body,
                        })
                break
    return rules

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--out", default="rules.yaml")
    args = ap.parse_args()

    all_rules = []
    for fname in args.pdfs:
        with pdfplumber.open(fname) as pdf:
            for page in pdf.pages:
                rules = extract_rules_from_page(page)
                all_rules.extend(rules)

    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        yaml.safe_dump(all_rules, f, allow_unicode=True, sort_keys=False)
    print(f"Wrote {len(all_rules)} rules to {args.out}")

if __name__ == "__main__":
    main()
