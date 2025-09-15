from typing import Dict, Any

def _resolve(path: str, env: Dict[str, Any]):
    cur: Any = env
    for p in path.split("."):
        cur = cur.get(p) if isinstance(cur, dict) else getattr(cur, p, None)
        if cur is None:
            return None
    return cur

def eval_expr(expr: str, firm: Dict[str, Any]) -> bool:
    left, _, right = expr.partition("==")
    left = left.strip()
    right = right.strip().strip('"\'')
    expected = {"true": True, "false": False}.get(right.lower(), right)
    if not left.startswith("firm."):
        return False
    val = _resolve(left[len("firm."):], {"firm": firm, **firm})
    return val == expected

def eval_cond(cond, firm: Dict[str, Any]) -> bool:
    if cond is None:
        return True
    if isinstance(cond, dict):
        if "all" in cond:
            return all(eval_cond(c, firm) for c in cond["all"])
        if "any" in cond:
            return any(eval_cond(c, firm) for c in cond["any"])
        if "not" in cond:
            inner = cond["not"]
            return not eval_cond(inner, firm)
    return eval_expr(str(cond), firm)
