from pydantic import BaseModel, field_validator
from typing import Literal


class Tool(BaseModel):
    vendor: str
    product_name: str
    tool_type: Literal["enterprise", "opensource"]
    url: str | None = None

    @field_validator("product_name", "vendor", mode="before")
    @classmethod
    def strip_strings(cls, v: str) -> str:
        return v.strip() if v else v


class SearchResult(BaseModel):
    url: str
    title: str
    snippet: str
    source: Literal["tavily", "brightdata", "web_fetch"]


class FetchResult(BaseModel):
    url: str
    content: str
    source: str
