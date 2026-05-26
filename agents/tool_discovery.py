import logging
import asyncio
from typing import Any
from pydantic import BaseModel

from models.discovery import ToolDiscoveryResult
from models.tools import Tool
from models.web_search import WebSearchResult
from llm.bedrock import structured_call
from db.store import upsert_tool, get_tools
from config.settings import settings
from tools.router import search

logger = logging.getLogger(__name__)


SEARCH_PROMPT_TEMPLATE = """You are a cybersecurity tools expert. Analyze the web search results to identify real tools used in enterprise environments.

Subdomain: {subdomain}

Web Search Results:
{search_context}

Based on the search results above, identify tools for this subdomain.

Requirements for Enterprise Tools ({max_ent} tools):
- Commercial/enterprise-grade solutions mentioned in search results
- Widely adopted in enterprise environments
- Established vendors with enterprise support
- Include market leaders and notable solutions from the search results

Requirements for Open-Source Tools ({max_oss} tools):
- Actively maintained open-source projects found in search results
- Community or enterprise adoption
- Available for self-hosting
- Notable in security community

Return a valid JSON object with this exact structure:
{{
    "subdomain": "{subdomain}",
    "tools_enterprise": [
        {{"vendor": "Vendor Name", "product_name": "Product Name", "tool_type": "enterprise"}},
        ...
    ],
    "tools_opensource": [
        {{"vendor": "Organization", "product_name": "Project Name", "tool_type": "opensource"}},
        ...
    ]
}}

Provide exactly {max_ent} enterprise tools and {max_oss} open-source tools. Use real tools from the search results when available.
"""


async def fetch_tool_search_context(subdomain_name: str) -> tuple[list[WebSearchResult], str]:
    query = f"{subdomain_name} tools enterprise solutions cybersecurity"
    logger.info(f"Searching for tools: {query}")
    
    try:
        results, source = await search(query)
        
        if not results:
            logger.warning(f"No search results for tools in '{subdomain_name}'")
            return [], ""
        
        search_results = [
            WebSearchResult(
                query=query,
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content", r.get("snippet", ""))[:500],
                source=source
            )
            for r in results[:10]
        ]
        
        context_parts = []
        for i, r in enumerate(search_results, 1):
            context_parts.append(f"{i}. {r.title}\n   {r.snippet}\n   Source: {r.url}")
        
        search_context = "\n".join(context_parts)
        logger.info(f"Got {len(search_results)} tool search results from {source}")
        
        return search_results, search_context
        
    except Exception as e:
        logger.error(f"Tool search failed: {e}")
        return [], ""


async def discover_tools(subdomain_id: int, subdomain_name: str) -> dict[str, Any]:
    logger.info(f"Discovering tools for subdomain '{subdomain_name}'")
    
    search_results = []
    search_context = ""
    
    if settings.web_search_enabled:
        search_results, search_context = await fetch_tool_search_context(subdomain_name)
    
    if search_context:
        prompt = SEARCH_PROMPT_TEMPLATE.format(
            subdomain=subdomain_name,
            search_context=search_context,
            max_ent=settings.max_enterprise_tools,
            max_oss=settings.max_opensource_tools,
        )
        logger.info("Using web search context for tool discovery")
    else:
        fallback_prompt = f"""You are a cybersecurity tools expert. For the given subdomain, identify the top tools used in enterprise environments.

Subdomain: {subdomain_name}

Requirements for Enterprise Tools ({settings.max_enterprise_tools} tools):
- Commercial/enterprise-grade solutions
- Widely adopted in enterprise environments

Requirements for Open-Source Tools ({settings.max_opensource_tools} tools):
- Actively maintained open-source projects
- Community or enterprise adoption

Return a valid JSON object with this exact structure:
{{
    "subdomain": "{subdomain_name}",
    "tools_enterprise": [{{"vendor": "Name", "product_name": "Name", "tool_type": "enterprise"}}],
    "tools_opensource": [{{"vendor": "Name", "product_name": "Name", "tool_type": "opensource"}}]
}}

Provide exactly {settings.max_enterprise_tools} enterprise tools and {settings.max_opensource_tools} open-source tools."""
        prompt = fallback_prompt
        logger.info("Using fallback LLM-only tool discovery")
    
    result = await structured_call(prompt, ToolDiscoveryResult, temperature=0.4)

    raw_ent = len(result.tools_enterprise)
    raw_oss = len(result.tools_opensource)
    if raw_ent > settings.max_enterprise_tools or raw_oss > settings.max_opensource_tools:
        logger.warning(
            f"Tool discovery returned {raw_ent} enterprise + {raw_oss} opensource "
            f"for '{subdomain_name}' — capping to "
            f"{settings.max_enterprise_tools}+{settings.max_opensource_tools}"
        )

    for tool in result.tools_enterprise[:settings.max_enterprise_tools]:
        await upsert_tool(subdomain_id, tool.vendor, tool.product_name, "enterprise")

    for tool in result.tools_opensource[:settings.max_opensource_tools]:
        await upsert_tool(subdomain_id, tool.vendor, tool.product_name, "opensource")

    tools = await get_tools(subdomain_id)

    # Safety: if prior runs left more tools than the current limit, truncate live list
    enterprise_saved = [t for t in tools if t["tool_type"] == "enterprise"][:settings.max_enterprise_tools]
    opensource_saved = [t for t in tools if t["tool_type"] == "opensource"][:settings.max_opensource_tools]
    enterprise_count = len(enterprise_saved)
    opensource_count = len(opensource_saved)

    logger.info(f"Discovered {enterprise_count} enterprise + {opensource_count} opensource tools")

    return {
        "enterprise": enterprise_saved,
        "opensource": opensource_saved,
    }


class ToolDiscoveryOutput(BaseModel):
    tools_enterprise: list[dict[str, Any]]
    tools_opensource: list[dict[str, Any]]


async def run_tool_discovery(subdomain_id: int, subdomain_name: str) -> ToolDiscoveryOutput:
    result = await discover_tools(subdomain_id, subdomain_name)
    return ToolDiscoveryOutput(
        tools_enterprise=result["enterprise"],
        tools_opensource=result["opensource"]
    )
