from pydantic import BaseModel, field_validator


class Feature(BaseModel):
    name: str
    description: str = ""
    rank_order: int = 0

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v: str) -> str:
        return v.strip() if v else v


class FeatureDiscoveryResult(BaseModel):
    subdomain: str
    features: list[Feature]


class SubFeatureResult(BaseModel):
    feature: str
    subfeatures: list[str]

    @field_validator("subfeatures", mode="before")
    @classmethod
    def strip_subfeatures(cls, v: list[str]) -> list[str]:
        return [s.strip() if isinstance(s, str) else s for s in v]
