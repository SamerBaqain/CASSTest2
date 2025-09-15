from pathlib import Path
from typing import List, Dict
import yaml
from .models import Rule, Risk, Control

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def load_rules() -> List[Rule]:
    path = DATA_DIR / "rules.yaml"
    data = load_yaml(path)
    items = data if isinstance(data, list) else data.get("rules", [])
    return [Rule(**it) for it in items]

def load_risks() -> Dict[str, Risk]:
    data = load_yaml(DATA_DIR / "risks.yaml")
    items = data.get("risks", [])
    return {r["id"]: Risk(**r) for r in items}

def load_controls() -> Dict[str, Control]:
    data = load_yaml(DATA_DIR / "controls.yaml")
    items = data.get("controls", [])
    return {c["id"]: Control(**c) for c in items}
