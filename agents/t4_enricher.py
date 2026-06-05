"""
agents/t4_enricher.py - Technique 4 license enrichment agent.

Uses efficient web search routing (Tavily -> BrightData fallback) via tools/router.
Batch LLM enrichment for license detection, URL, and description.
"""

import asyncio
import logging
import re
from typing import Any

from pydantic import BaseModel

from models.t4_tool import T4ToolEnrichment, T4BatchEnrichment, LICENSE_MODELS
from db.store import db
from db.t4_store import update_t4_tool_enrichment, get_stub_t4_tools
from llm.bedrock import structured_call
from tools.router import search
from config.settings import settings

logger = logging.getLogger(__name__)


def _sanitize_search_query(text: str) -> str:
    """Remove or escape special characters that could break search."""
    return re.sub(r'[^\w\s\-]', ' ', text).strip()


_LICENSE_PROMPT_LIST = "\n".join(f"   - '{lic}'" for lic in LICENSE_MODELS)

_ENRICHMENT_PROMPT = """\
You are a cybersecurity product analyst. For each tool below, research and provide:

1. license_model: One of:
{license_list}

2. url: Official product/landing page URL (if known)

3. description: One-sentence summary (max 25 words) of what the tool does

Tools to enrich:
{tool_list}

{web_context}

Return JSON:
{{
  "tools": [
    {{
      "vendor": "<exact vendor>",
      "product_name": "<exact product_name>",
      "license_model": "<license>",
      "url": "<url or null>",
      "description": "<summary>"
    }}
  ]
}}

Rules:
- Return exactly the same number of tools as provided, in the same order
- If unknown, use 'Unknown' for license_model
- Be concise in descriptions
"""


def _build_tool_list_text(tools: list[dict[str, Any]]) -> str:
    """Build tool list text for enrichment prompt."""
    lines = []
    for i, tool in enumerate(tools, 1):
        tool_type = tool.get("tool_type", "unknown")
        lines.append(
            f"{i}. {tool['vendor']} | {tool['product_name']} (type: {tool_type})"
        )
    return "\n".join(lines)


async def fetch_enrichment_search_context(tools: list[dict[str, Any]]) -> str:
    """
    Fetch web search context for tool enrichment.
    Uses router.search() which handles Tavily -> BrightData fallback.
    Returns aggregated search context string.
    """
    if not settings.t4_enable_web_enrichment or not settings.web_search_enabled:
        return ""
    
    search_queries = [
        f"{_sanitize_search_query(t['vendor'])} {_sanitize_search_query(t['product_name'])} license pricing official website"
        for t in tools[:5]
    ]
    
    search_context_parts = []
    
    for query in search_queries:
        try:
            results, source = await search(query)
            if results:
                top_result = results[0]
                title = top_result.get("title", "")
                snippet = top_result.get("content", top_result.get("snippet", ""))[:500]
                url = top_result.get("url", "")
                search_context_parts.append(
                    f"Web Search: {query}\n"
                    f"  Title: {title}\n"
                    f"  Snippet: {snippet}\n"
                    f"  Source: {url}\n"
                )
        except Exception as e:
            logger.warning(f"Search failed for '{query}': {e}")
            continue
    
    if search_context_parts:
        return "---\nWeb Search Results:\n---\n" + "\n".join(search_context_parts)
    return ""


async def enrich_tool_batch(
    tools: list[dict[str, Any]],
    semaphore: asyncio.Semaphore,
) -> list[T4ToolEnrichment]:
    """
    Enrich a batch of tools with license info.
    
    Strategy:
    1. If web_search_enabled: Search top tools, aggregate results
    2. Call LLM with tool names + web context
    3. Parse and return structured enrichment
    """
    tool_list_text = _build_tool_list_text(tools)
    
    web_context = ""
    if settings.web_search_enabled:
        web_context = await fetch_enrichment_search_context(tools)
    
    prompt = _ENRICHMENT_PROMPT.format(
        license_list=_LICENSE_PROMPT_LIST,
        tool_list=tool_list_text,
        web_context=web_context
    )
    
    async with semaphore:
        try:
            result = await structured_call(
                prompt,
                T4BatchEnrichment,
                temperature=0.2
            )
            
            if len(result.tools) != len(tools):
                logger.error(
                    f"T4 enrichment batch size mismatch: sent {len(tools)}, "
                    f"got {len(result.tools)}"
                )
                if len(result.tools) < len(tools):
                    logger.warning(f"Partial results - only {len(result.tools)} tools enriched")
                    padded = list(result.tools)
                    for i in range(len(padded), len(tools)):
                        tool_data = tools[i]
                        padded.append(T4ToolEnrichment(
                            vendor=tool_data.get("vendor", "Unknown"),
                            product_name=tool_data.get("product_name", "Unknown"),
                            license_model="Unknown",
                            url=None,
                            description=f"Tool by {tool_data.get('vendor', 'Unknown')}"
                        ))
                    return padded
                return result.tools[:len(tools)]
            
            return result.tools
            
        except Exception as e:
            logger.warning(f"T4 enrichment batch failed: {e} - using fallback")
            fallback = []
            for tool in tools:
                tool_type = tool.get("tool_type", "unknown")
                if tool_type == "enterprise":
                    license_model = "Commercial"
                elif tool_type == "opensource":
                    license_model = "GPL-3.0"
                else:
                    license_model = "Unknown"
                
                fallback.append(T4ToolEnrichment(
                    vendor=tool.get("vendor", "Unknown"),
                    product_name=tool.get("product_name", "Unknown"),
                    license_model=license_model,
                    url=None,
                    description=f"Tool by {tool.get('vendor', 'Unknown')}"
                ))
            return fallback


async def run_s2_enrichment(on_progress: Any = None) -> int:
    """
    Run S2 enrichment as batched LLM calls.
    
    Batches tools (50 per batch), searches web, enriches via LLM.
    Returns number of tools enriched.
    """
    from db.t4_store import bulk_update_t4_tool_enrichment
    
    logger.info("T4 S2: Starting license enrichment")
    
    tools = await get_stub_t4_tools()
    
    if not tools:
        logger.info("T4 S2: No tools pending enrichment")
        return 0
    
    logger.info(f"T4 S2: {len(tools)} tools to enrich")
    
    batch_size = getattr(settings, "t4_llm_batch_size", 50)
    batches = [tools[i:i + batch_size] for i in range(0, len(tools), batch_size)]
    semaphore = asyncio.Semaphore(settings.llm_concurrency)
    
    enriched = 0
    
    for batch_idx, batch in enumerate(batches):
        try:
            results = await enrich_tool_batch(batch, semaphore)
            
            batch_updates: list[tuple] = []
            for tool, enrichment in zip(batch, results):
                batch_updates.append((
                    tool["id"],
                    enrichment.license_model,
                    enrichment.url,
                    enrichment.description,
                ))
                enriched += 1
            
            if batch_updates:
                await bulk_update_t4_tool_enrichment(batch_updates)
            
            if on_progress:
                pct = 0.10 + (enriched / len(tools)) * 0.25
                await on_progress(pct, f"Enriched {enriched}/{len(tools)} tools")
            
            logger.info(f"T4 S2: Batch {batch_idx + 1}/{len(batches)} - {enriched}/{len(tools)} enriched")
            
        except Exception as batch_err:
            logger.error(f"T4 S2 batch {batch_idx} failed: {batch_err}")
            continue
    
    logger.info(f"T4 S2 complete: {enriched} tools enriched")
    return enriched
