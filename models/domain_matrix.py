"""
Technique 2: Domain-level matrix models.
"""

from pydantic import BaseModel


class DomainMatrixCell(BaseModel):
    subfeature_name: str
    feature_name: str
    tool_support: dict[str, str]


class DomainMatrixBatch(BaseModel):
    domain_id: int
    domain_name: str
    tools_enterprise: list[str]
    tools_opensource: list[str]
    rows: list[DomainMatrixCell]


class DomainMatrixCellInput(BaseModel):
    domain_subfeature_id: int
    subfeature_name: str
    feature_name: str
    tool_name: str
    tool_id: int
    support_level: str
    confidence: float = 1.0
