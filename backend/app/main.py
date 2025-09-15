from fastapi import FastAPI
from typing import Dict
from .loaders import load_rules, load_risks, load_controls
from .mapping import applicable_rules, collect_risks, suggest_controls, build_matrix
from .models import Control

app = FastAPI(title="CASS Mapper API", version="0.1.0")

RULES = []
RISKS = {}
CONTROLS: Dict[str, Control] = {}

@app.on_event("startup")
def startup():
    global RULES, RISKS, CONTROLS
    RULES = load_rules()
    RISKS = load_risks()
    CONTROLS = load_controls()

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/questionnaire")
def questionnaire():
    from pathlib import Path
    import yaml
    q = yaml.safe_load((Path(__file__).resolve().parents[2] / "data" / "questionnaire.yaml").read_text(encoding="utf-8"))
    return q

@app.post("/profile")
def profile(firm: Dict):
    return {"firm": firm}

@app.post("/rules/applicable")
def rules_applicable(firm: Dict):
    rules = applicable_rules(RULES, firm)
    return {"rules": [r.model_dump() for r in rules]}

@app.post("/risks")
def risks_for_firm(firm: Dict):
    rules = applicable_rules(RULES, firm)
    rids = collect_risks(rules)
    return {"risks": [RISKS[r].model_dump() for r in rids if r in RISKS]}

@app.post("/controls/suggested")
def controls_for_firm(firm: Dict):
    rules = applicable_rules(RULES, firm)
    rids = collect_risks(rules)
    cids = suggest_controls(rules, CONTROLS, rids)
    return {"controls": [CONTROLS[c].model_dump() for c in cids if c in CONTROLS]}

@app.post("/controls/map")
def map_controls(body: Dict):
    risk_ids = body.get("risks", [])
    user_controls = body.get("user_controls", [])
    matrix, gaps, control_ids = build_matrix(risk_ids, CONTROLS, user_controls)
    return {"risks": risk_ids, "controls": control_ids, "matrix": matrix, "gaps": gaps}
