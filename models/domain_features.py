"""
Technique 2: Domain-level feature models.
"""

from pydantic import BaseModel


class DomainFeature(BaseModel):
    name: str
    rank_order: int
    source_subdomains: list[str] = []


class DomainSubFeature(BaseModel):
    feature_name: str
    name: str
    rank_order: int


class DomainFeatureResult(BaseModel):
    domain_id: int
    domain_name: str
    features: list[DomainFeature]


class DomainSubFeatureResult(BaseModel):
    domain_feature_id: int
    feature_name: str
    subfeatures: list[DomainSubFeature]
