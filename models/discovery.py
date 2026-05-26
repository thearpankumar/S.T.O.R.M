from pydantic import BaseModel, model_validator, field_validator
from models.tools import Tool


class SubdomainDiscoveryResult(BaseModel):
    domain: str
    subdomains: list[str]
    confidence_scores: list[float] = []

    @model_validator(mode="after")
    def validate_lengths(self) -> "SubdomainDiscoveryResult":
        if len(self.subdomains) != len(self.confidence_scores):
            self.confidence_scores = [1.0] * len(self.subdomains)
        return self

    @field_validator("subdomains", mode="before")
    @classmethod
    def dedupe_subdomains(cls, v: list[str]) -> list[str]:
        seen = set()
        result = []
        for item in v:
            normalized = item.lower().strip()
            if normalized not in seen:
                seen.add(normalized)
                result.append(item.strip())
        return result


class ToolDiscoveryResult(BaseModel):
    subdomain: str
    tools_enterprise: list[Tool]
    tools_opensource: list[Tool]
