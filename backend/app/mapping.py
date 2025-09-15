from typing import List, Dict, Tuple
from .models import Rule, Control

from .engine import eval_cond

def applicable_rules(rules: List[Rule], firm: dict) -> List[Rule]:
    out: List[Rule] = []
    for r in rules:
        if r.applicability_conditions is None:
            out.append(r)
        elif eval_cond(r.applicability_conditions, firm):
            out.append(r)
    return out

def collect_risks(rules: List[Rule]) -> List[str]:
    s = []
    for r in rules:
        s.extend(r.risk_ids or [])
    return sorted(set(s))

def suggest_controls(rules: List[Rule], controls: Dict[str, Control], risks: List[str]) -> List[str]:
    ids = set()
    for r in rules:
        ids.update(r.default_control_ids or [])
    for cid, c in controls.items():
        if any(r in c.mitigates_risk_ids for r in risks):
            ids.add(cid)
    return sorted(ids)

def build_matrix(risk_ids: List[str], control_objs: Dict[str, Control], user_controls: List[Dict]) -> Tuple[List[List[int]], List[str], List[str]]:
    control_ids = list(control_objs.keys()) + [uc.get("id") or uc.get("name") for uc in user_controls]
    mitigations: Dict[str, List[str]] = {cid: control_objs[cid].mitigates_risk_ids for cid in control_objs}
    for uc in user_controls:
        ucid = uc.get("id") or uc.get("name")
        mitigations[ucid] = uc.get("mitigates_risk_ids", [])
    matrix = []
    gaps = []
    for rid in risk_ids:
        row = []
        coverage = 0
        for cid in control_ids:
            val = 1 if rid in mitigations.get(cid, []) else 0
            coverage += val
            row.append(val)
        if coverage == 0:
            gaps.append(rid)
        matrix.append(row)
    return matrix, gaps, control_ids
