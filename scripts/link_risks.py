#!/usr/bin/env python3
import sys, yaml, re
from pathlib import Path

def load_yaml(p): 
    return yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {}

def main(rules_path="data/rules.yaml", links_path="data/rule_risk_links.yaml", out_path=None):
    out_path = out_path or rules_path
    rules = load_yaml(rules_path)
    links = load_yaml(links_path).get("rules", [])
    # Precompile patterns by translating "1.5." to regex ^1\.5\.
    compiled = [(re.compile(rf"^{re.escape(l['match'])}"), l["risk_ids"]) for l in links]

    # If rules.yaml is a dict with "rules" key, accept it, else list
    if isinstance(rules, dict) and "rules" in rules:
        items = rules["rules"]
    else:
        items = rules

    # Apply best (longest) match
    for r in items:
        rid = r.get("id","")
        matches = [(len(m.pattern), risks) for (m, risks) in compiled if m.search(rid)]
        if matches:
            _, risks = sorted(matches, key=lambda x: x[0], reverse=True)[0]
            current = set(r.get("risk_ids") or [])
            r["risk_ids"] = sorted(set(risks) | current)

    # Write back
    data_to_write = {"rules": items} if isinstance(rules, dict) and "rules" in rules else items
    Path(out_path).write_text(yaml.safe_dump(data_to_write, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"[ok] linked risks into {out_path}")

if __name__ == "__main__":
    main(*sys.argv[1:])
