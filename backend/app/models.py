from typing import List, Optional, Dict
from pydantic import BaseModel

class Rule(BaseModel):
    id: str
    chapter: str
    type: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    text: Optional[str] = None
    display: Optional[str] = None
    risk_ids: List[str] = []
    default_control_ids: List[str] = []
    applicability_conditions: Optional[Dict] = None

class Risk(BaseModel):
    id: str
    name: str
    description: str
    categories: List[str] = []
    related_rule_ids: List[str] = []

class Control(BaseModel):
    id: str
    name: str
    objective: str
    mitigates_risk_ids: List[str]
    type: str
    owner_role: Optional[str] = None

class FirmProfile(BaseModel):
    uk_establishment: Optional[bool] = None
    holds_client_money: Optional[bool] = None
    holds_custody_assets: Optional[bool] = None
    debt_mgmt: Optional[bool] = None
    uses_e_channels: Optional[bool] = None
