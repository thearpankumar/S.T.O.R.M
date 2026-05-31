"""
D1: Domain Tool Aggregator & Ranker

Aggregates all tools from all subdomains within a domain,
calculates ranking scores, and selects top 15 enterprise + 5 open-source tools.

Ranking Formula (weights from settings):
    Composite Score = 
        0.40 × Subdomain Presence Score +
        0.20 × Feature Coverage Score +
        0.20 × Market Presence Score +
        0.20 × Rank Distribution Score
"""

import logging
from collections import Counter
from typing import Any

from pydantic import BaseModel

from config.settings import settings
from db.domain_store import (
    get_all_tools_for_domain,
    get_all_matrix_cells_for_domain,
    upsert_t2_domain_tool,
    delete_t2_domain_tools,
    upsert_t2_domain_ranking,
)
from db.store import get_domain_id
from llm.bedrock import structured_call
from models.domain_tools import DomainToolRanking, DomainToolAggregationResult

logger = logging.getLogger(__name__)


class MarketPresenceResult(BaseModel):
    tool_scores: list[dict[str, Any]]


MARKET_PRESENCE_PROMPT = """\
You are a cybersecurity market analyst. Assess the market presence and leadership 
of these tools in the {domain} space.

Tools to evaluate:
{tools_list}

For each tool, provide a market_presence_score from 0.0 to 1.0 based on:
- Market share and adoption in enterprises
- Brand recognition and reputation
- Industry awards and analyst recognition (Gartner, Forrester)
- Ecosystem integrations and partnerships

Return ONLY valid JSON:
{{
    "tool_scores": [
        {{"product_name": "Tool Name", "market_score": 0.85}},
        ...
    ]
}}
"""


def _calculate_subdomain_presence_score(tools: list[dict], product_name: str) -> tuple[int, float]:
    subdomains = set()
    for tool in tools:
        if tool["product_name"] == product_name:
            subdomains.add(tool["subdomain_name"])
    count = len(subdomains)
    total_subdomains = len(set(t["subdomain_name"] for t in tools))
    score = count / total_subdomains if total_subdomains > 0 else 0.0
    return count, score


def _calculate_feature_coverage_score(
    product_name: str,
    matrix_cells: list[dict],
    tools: list[dict]
) -> float:
    tool_id = None
    for tool in tools:
        if tool["product_name"] == product_name:
            tool_id = tool["id"]
            break
    
    if tool_id is None:
        return 0.0
    
    support_values = {"✔": 1.0, "Partial": 0.5, "✘": 0.0}
    total_score = 0.0
    cell_count = 0
    
    for cell in matrix_cells:
        if cell["tool_id"] == tool_id:
            total_score += support_values.get(cell["support_level"], 0.0)
            cell_count += 1
    
    return (total_score / cell_count) if cell_count > 0 else 0.5


def _calculate_rank_distribution_score(
    product_name: str,
    matrix_cells: list[dict],
    tools: list[dict]
) -> float:
    tool_id = None
    for tool in tools:
        if tool["product_name"] == product_name:
            tool_id = tool["id"]
            break
    
    if tool_id is None:
        return 0.0
    
    full_count = 0
    partial_count = 0
    none_count = 0
    
    for cell in matrix_cells:
        if cell["tool_id"] == tool_id:
            if cell["support_level"] == "✔":
                full_count += 1
            elif cell["support_level"] == "Partial":
                partial_count += 1
            else:
                none_count += 1
    
    total = full_count + partial_count + none_count
    if total == 0:
        return 0.5
    
    weighted_score = (full_count * 1.0 + partial_count * 0.5) / total
    return weighted_score


async def _assess_market_presence(
    domain_name: str,
    unique_tools: list[dict]
) -> dict[str, float]:
    if not settings.t2_enable_web_search_ranking:
        return {t["product_name"]: 0.5 for t in unique_tools}
    
    tools_list = "\n".join([
        f"- {t['product_name']} ({t['vendor']}) - {t['tool_type']}"
        for t in unique_tools[:30]
    ])
    
    prompt = MARKET_PRESENCE_PROMPT.format(
        domain=domain_name,
        tools_list=tools_list
    )
    
    try:
        result = await structured_call(
            prompt,
            MarketPresenceResult,
            temperature=0.3
        )
        
        scores = {}
        for item in result.tool_scores:
            scores[item["product_name"]] = item.get("market_score", 0.5)
        
        for tool in unique_tools:
            if tool["product_name"] not in scores:
                scores[tool["product_name"]] = 0.5
        
        return scores
    except Exception as e:
        logger.warning(f"Market presence assessment failed: {e}, using defaults")
        return {t["product_name"]: 0.5 for t in unique_tools}


async def aggregate_and_rank_tools(
    domain_name: str,
) -> DomainToolAggregationResult:
    logger.info(f"D1: Aggregating and ranking tools for domain '{domain_name}'")
    
    domain_id = await get_domain_id(domain_name)
    if not domain_id:
        raise ValueError(f"Domain '{domain_name}' not found in database")
    
    all_tools = await get_all_tools_for_domain(domain_id)
    matrix_cells = await get_all_matrix_cells_for_domain(domain_id)
    
    if not all_tools:
        logger.warning(f"No tools found for domain '{domain_name}'")
        return DomainToolAggregationResult(
            domain_id=domain_id,
            domain_name=domain_name,
            total_enterprise_tools=0,
            total_opensource_tools=0,
            tools_enterprise=[],
            tools_opensource=[],
        )
    
    tool_counter = Counter(t["product_name"] for t in all_tools)
    unique_tools_map: dict[str, dict] = {}
    for tool in all_tools:
        name = tool["product_name"]
        if name not in unique_tools_map:
            unique_tools_map[name] = tool
    
    unique_tools = list(unique_tools_map.values())
    
    logger.info(f"Found {len(unique_tools)} unique tools across {len(all_tools)} tool records")
    
    market_scores = await _assess_market_presence(domain_name, unique_tools)
    
    ranked_tools: list[tuple[dict, float, dict]] = []
    
    for tool in unique_tools:
        product_name = tool["product_name"]
        
        presence_count, presence_score = _calculate_subdomain_presence_score(
            all_tools, product_name
        )
        
        coverage_score = _calculate_feature_coverage_score(
            product_name, matrix_cells, all_tools
        )
        
        rank_dist_score = _calculate_rank_distribution_score(
            product_name, matrix_cells, all_tools
        )
        
        market_score = market_scores.get(product_name, 0.5)
        
        composite_score = (
            settings.t2_weight_subdomain_presence * presence_score +
            settings.t2_weight_feature_coverage * coverage_score +
            settings.t2_weight_market_presence * market_score +
            settings.t2_weight_rank_distribution * rank_dist_score
        ) * 100
        
        scores = {
            "subdomain_presence_count": presence_count,
            "subdomain_presence_score": round(presence_score, 4),
            "feature_coverage_score": round(coverage_score, 4),
            "market_presence_score": round(market_score, 4),
            "rank_distribution_score": round(rank_dist_score, 4),
        }
        
        ranked_tools.append((tool, composite_score, scores))
    
    ranked_tools.sort(key=lambda x: x[1], reverse=True)
    
    enterprise_tools = [(t, s, sc) for t, s, sc in ranked_tools if t["tool_type"] == "enterprise"]
    opensource_tools = [(t, s, sc) for t, s, sc in ranked_tools if t["tool_type"] == "opensource"]
    
    top_enterprise = enterprise_tools[:settings.t2_max_enterprise_tools]
    top_opensource = opensource_tools[:settings.t2_max_opensource_tools]
    
    await delete_t2_domain_tools(domain_id)
    
    enterprise_rankings = []
    for rank, (tool, score, scores) in enumerate(top_enterprise, 1):
        await upsert_t2_domain_tool(
            domain_id=domain_id,
            vendor=tool["vendor"],
            product_name=tool["product_name"],
            tool_type="enterprise",
            rank_position=rank,
            composite_score=round(score, 2),
            **scores
        )
        enterprise_rankings.append(DomainToolRanking(
            vendor=tool["vendor"],
            product_name=tool["product_name"],
            tool_type="enterprise",
            rank_position=rank,
            composite_score=round(score, 2),
            **scores
        ))
    
    oss_rankings = []
    for rank, (tool, score, scores) in enumerate(top_opensource, 1):
        await upsert_t2_domain_tool(
            domain_id=domain_id,
            vendor=tool["vendor"],
            product_name=tool["product_name"],
            tool_type="opensource",
            rank_position=rank,
            composite_score=round(score, 2),
            **scores
        )
        oss_rankings.append(DomainToolRanking(
            vendor=tool["vendor"],
            product_name=tool["product_name"],
            tool_type="opensource",
            rank_position=rank,
            composite_score=round(score, 2),
            **scores
        ))
    
    await upsert_t2_domain_ranking(
        domain_id=domain_id,
        status="running",
        total_enterprise_tools=len(enterprise_tools),
        total_opensource_tools=len(opensource_tools),
        selected_enterprise_tools=len(enterprise_rankings),
        selected_opensource_tools=len(oss_rankings),
    )
    
    logger.info(
        f"D1 Complete: Ranked {len(enterprise_rankings)} enterprise + "
        f"{len(oss_rankings)} opensource tools for '{domain_name}'"
    )
    
    return DomainToolAggregationResult(
        domain_id=domain_id,
        domain_name=domain_name,
        total_enterprise_tools=len(enterprise_tools),
        total_opensource_tools=len(opensource_tools),
        tools_enterprise=enterprise_rankings,
        tools_opensource=oss_rankings,
    )
