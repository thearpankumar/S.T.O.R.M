import asyncio
import logging
from typing import Any
from pydantic import BaseModel

from models.discovery import SubdomainDiscoveryResult
from models.web_search import WebSearchResult
from agents.consensus import three_way_consensus
from db.store import get_domain_id, upsert_subdomain, get_subdomains
from config.settings import settings
from tools.router import search

logger = logging.getLogger(__name__)


SEARCH_PROMPT_TEMPLATE = """You are a cybersecurity domain expert analyzing web search results.

Domain: {domain}

Web Search Results:
{search_context}

Based on the web search results above, identify exactly 10-12 important subdomains within "{domain}".

Requirements:
- Extract subdomains mentioned or implied in the search results
- Each subdomain should be a specific area within {domain}
- Use clear, professional naming (e.g., "Network Segmentation", "Vulnerability Management")
- Avoid overlapping or redundant subdomains
- Focus on enterprise-relevant subdomains that appear in real industry discussions
- If search results don't provide enough subdomains, supplement with your knowledge

Return a valid JSON object with the following structure:
{{
    "domain": "{domain}",
    "subdomains": ["subdomain1", "subdomain2", ...],
    "confidence_scores": [1.0, 1.0, ...]
}}
"""

FALLBACK_PROMPT_TEMPLATE = """You are a cybersecurity domain expert. Given the cybersecurity domain below, list exactly 10-12 important subdomains within it.

Domain: {domain}

Requirements:
- List exactly 10-12 distinct subdomains
- Each subdomain should be a specific area within {domain}
- Use clear, professional naming (e.g., "Network Segmentation", "Vulnerability Management")
- Avoid overlapping or redundant subdomains
- Focus on enterprise-relevant subdomains

Return a valid JSON object with the following structure:
{{
    "domain": "{domain}",
    "subdomains": ["subdomain1", "subdomain2", ...],
    "confidence_scores": [1.0, 1.0, ...]
}}
"""


async def fetch_search_context(domain: str) -> tuple[list[WebSearchResult], str]:
    query = f"cybersecurity {domain} subdomains enterprise areas categories"
    logger.info(f"Searching for: {query}")
    
    try:
        results, source = await search(query)
        
        if not results:
            logger.warning(f"No search results for '{domain}'")
            return [], ""
        
        search_results = [
            WebSearchResult(
                query=query,
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content", r.get("snippet", "")),
                source=source
            )
            for r in results[:10]
        ]
        
        context_parts = []
        for i, r in enumerate(search_results, 1):
            context_parts.append(f"{i}. {r.title}\n   {r.snippet}\n   Source: {r.url}")
        
        search_context = "\n".join(context_parts)
        logger.info(f"Got {len(search_results)} search results from {source}")
        
        return search_results, search_context
        
    except Exception as e:
        logger.error(f"Web search failed: {e}")
        return [], ""


async def discover_subdomains(domain: str, force_redo: bool = False) -> list[dict[str, Any]]:
    domain_id = await get_domain_id(domain)
    if not domain_id:
        logger.error(f"Domain '{domain}' not found in database")
        return []
    
    existing = await get_subdomains(domain_id)
    if existing and not force_redo:
        logger.info(f"Domain '{domain}' already has {len(existing)} subdomains discovered")
        return existing
    
    logger.info(f"Discovering subdomains for '{domain}'")
    
    search_results = []
    search_context = ""
    
    if settings.web_search_enabled:
        search_results, search_context = await fetch_search_context(domain)
    
    if search_context:
        prompt = SEARCH_PROMPT_TEMPLATE.format(domain=domain, search_context=search_context)
        logger.info("Using web search context for subdomain discovery")
    else:
        prompt = FALLBACK_PROMPT_TEMPLATE.format(domain=domain)
        logger.info("Using fallback LLM-only subdomain discovery")
    
    subdomain_names = await three_way_consensus(
        prompt=prompt,
        response_model=SubdomainDiscoveryResult,
        extract_list_fn=lambda r: r.subdomains,
        threshold=2
    )
    
    results = []
    for i, name in enumerate(subdomain_names[:12]):
        await asyncio.sleep(0)
        subdomain_id = await upsert_subdomain(domain_id, name, confidence_score=1.0)
        results.append({
            "id": subdomain_id,
            "domain_id": domain_id,
            "name": name,
            "confidence_score": 1.0,
            "status": "pending"
        })
    
    logger.info(f"Discovered {len(results)} subdomains for '{domain}'")
    return results


class DiscoveryResult(BaseModel):
    subdomains: list[dict[str, Any]]


async def run_discovery(domain: str, force_redo: bool = False) -> DiscoveryResult:
    subdomains = await discover_subdomains(domain, force_redo)
    return DiscoveryResult(subdomains=subdomains)
