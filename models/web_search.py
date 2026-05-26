from pydantic import BaseModel, Field
from typing import Literal


class WebSearchResult(BaseModel):
    query: str
    title: str
    url: str
    snippet: str
    source: Literal["tavily", "brightdata"]


class WebSearchResponse(BaseModel):
    results: list[WebSearchResult]
    source: str
    total: int


class SubdomainSearchContext(BaseModel):
    domain: str
    search_results: list[WebSearchResult]
    search_summary: str = Field(default="")
