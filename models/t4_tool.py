"""
models/t4_tool.py - Pydantic models for Technique 4 tool analysis.
"""

from typing import Optional
from pydantic import BaseModel, field_validator


LICENSE_MODELS = [
    "Commercial",
    "GPL-3.0",
    "GPL-2.0", 
    "LGPL",
    "MIT",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "Apache-2.0",
    "MPL-2.0",
    "Freemium",
    "Proprietary",
    "Unknown",
]


def _normalize_license(v: str) -> str:
    """Normalize license model string."""
    if not v:
        return "Unknown"
    v = v.strip()
    license_map = {
        "commercial": "Commercial",
        "proprietary": "Proprietary",
        "gpl": "GPL-3.0",
        "gpl-3.0": "GPL-3.0",
        "gpl-2.0": "GPL-2.0",
        "lgpl": "LGPL",
        "mit": "MIT",
        "bsd": "BSD-3-Clause",
        "bsd-2-clause": "BSD-2-Clause",
        "bsd-3-clause": "BSD-3-Clause",
        "apache": "Apache-2.0",
        "apache-2.0": "Apache-2.0",
        "mpl": "MPL-2.0",
        "mpl-2.0": "MPL-2.0",
        "mozilla": "MPL-2.0",
        "freemium": "Freemium",
        "unknown": "Unknown",
    }
    return license_map.get(v.lower(), v if v in LICENSE_MODELS else "Unknown")


class T4ToolEnrichment(BaseModel):
    """Single tool enrichment result from LLM."""
    vendor: str
    product_name: str
    license_model: str
    url: Optional[str] = None
    description: str
    
    @field_validator("license_model", mode="before")
    @classmethod
    def normalize_license(cls, v: str) -> str:
        return _normalize_license(v)


class T4BatchEnrichment(BaseModel):
    """Batch enrichment result from LLM."""
    tools: list[T4ToolEnrichment]


class T4ToolSummary(BaseModel):
    """Summary view of a T4 tool."""
    id: int
    vendor: str
    product_name: str
    tool_type: str
    license_model: Optional[str] = None
    domain_count: int = 0
    subdomain_count: int = 0
    total_subfeatures: int = 0
    supported_subfeatures: int = 0
    support_rate: float = 0.0
    
    @field_validator("support_rate", mode="before")
    @classmethod
    def round_rate(cls, v: float) -> float:
        return round(float(v), 3) if v else 0.0


class T4ToolDetail(BaseModel):
    """Detailed view of a T4 tool with subdomain breakdown."""
    id: int
    vendor: str
    product_name: str
    tool_type: str
    license_model: Optional[str] = None
    url: Optional[str] = None
    description: Optional[str] = None
    domain_count: int = 0
    subdomain_count: int = 0
    domain_list: list[str] = []
    total_subfeatures: int = 0
    supported_subfeatures: int = 0
    partial_subfeatures: int = 0
    unsupported_subfeatures: int = 0
    support_rate: float = 0.0
    subdomain_breakdown: list[dict] = []


class T4Stats(BaseModel):
    """T4 aggregate statistics."""
    total: int = 0
    enterprise: int = 0
    opensource: int = 0
    freemium: int = 0
    multi_domain: int = 0
    top_tool: Optional[dict] = None
    license_counts: dict[str, int] = {}
    avg_support_rate: float = 0.0
