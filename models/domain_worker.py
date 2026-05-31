"""
Technique 2: Domain pipeline worker state model.
"""

from typing import Any
from pydantic import BaseModel, Field

from models.domain_tools import DomainToolRanking


class DomainWorkerState(BaseModel):
    domain_id: int
    domain_name: str
    current_step: str = "d1"
    tools_enterprise: list[DomainToolRanking] = Field(default_factory=list)
    tools_opensource: list[DomainToolRanking] = Field(default_factory=list)
    features: list[dict[str, Any]] = Field(default_factory=list)
    subfeatures: list[dict[str, Any]] = Field(default_factory=list)
    matrix_cells: list[dict[str, Any]] = Field(default_factory=list)
    total_enterprise_tools: int = 0
    total_opensource_tools: int = 0
    total_features: int = 0
