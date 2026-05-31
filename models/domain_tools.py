"""
Technique 2: Domain-level tool models.
"""

from pydantic import BaseModel


class DomainToolRanking(BaseModel):
    vendor: str
    product_name: str
    tool_type: str
    rank_position: int
    composite_score: float
    subdomain_presence_count: int = 0
    subdomain_presence_score: float = 0.0
    feature_coverage_score: float = 0.0
    market_presence_score: float = 0.0
    rank_distribution_score: float = 0.0


class DomainToolAggregationResult(BaseModel):
    domain_id: int
    domain_name: str
    total_enterprise_tools: int
    total_opensource_tools: int
    tools_enterprise: list[DomainToolRanking]
    tools_opensource: list[DomainToolRanking]
