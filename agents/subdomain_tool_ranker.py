"""
T2 D1: Subdomain Tool Ranker

Ranks tools within a single subdomain based on:
- Feature Coverage Score (50%): Avg support level across features
- Rank Distribution Score (30%): Ratio of ✔ vs ✘ in T1 matrix
- Market Presence Score (20%): LLM-assessed market leadership

Note: NO subdomain presence score (not applicable at subdomain level)
"""

import logging
from typing import Any

from pydantic import BaseModel

from config.settings import settings
from db.store import get_tools, get_features, get_subfeatures, get_matrix_cells
from db.subdomain_store import (
    upsert_t2_subdomain_tool,
    delete_t2_subdomain_tools,
    upsert_t2_subdomain_ranking,
)
from llm.bedrock import structured_call

logger = logging.getLogger(__name__)


class MarketPresenceResult(BaseModel):
    tool_scores: list[dict[str, Any]]


MARKET_PRESENCE_PROMPT = """\
You are a cybersecurity market analyst. Assess the market presence and leadership 
of these tools in the {subdomain} space.

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


async def _calculate_feature_coverage_score(
    tool_id: int,
    subdomain_id: int,
) -> float:
    matrix_cells = await get_matrix_cells(subdomain_id)
    
    support_values = {"✔": 1.0, "Partial": 0.5, "✘": 0.0}
    total_score = 0.0
    cell_count = 0
    
    for cell in matrix_cells:
        if cell["tool_id"] == tool_id:
            total_score += support_values.get(cell["support_level"], 0.0)
            cell_count += 1
    
    return (total_score / cell_count) if cell_count > 0 else 0.5


async def _calculate_rank_distribution_score(
    tool_id: int,
    subdomain_id: int,
) -> float:
    matrix_cells = await get_matrix_cells(subdomain_id)
    
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
    subdomain_name: str,
    tools: list[dict],
) -> dict[str, float]:
    if not settings.t2_enable_web_search_ranking:
        return {t["product_name"]: 0.5 for t in tools}
    
    tools_list = "\n".join([
        f"- {t['product_name']} ({t['vendor']}) - {t['tool_type']}"
        for t in tools[:30]
    ])
    
    prompt = MARKET_PRESENCE_PROMPT.format(
        subdomain=subdomain_name,
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
            pn = item.get("product_name")
            if pn:
                scores[pn] = item.get("market_score") or 0.5
        
        for tool in tools:
            if tool["product_name"] not in scores:
                scores[tool["product_name"]] = 0.5
        
        return scores
    except Exception as e:
        logger.warning(f"Market presence assessment failed: {e}, using defaults")
        return {t["product_name"]: 0.5 for t in tools}


async def rank_subdomain_tools(
    subdomain_id: int,
    subdomain_name: str,
) -> dict[str, Any]:
    logger.info(f"D1: Ranking tools for subdomain '{subdomain_name}'")
    
    tools = await get_tools(subdomain_id)
    
    if not tools:
        logger.warning(f"No tools found for subdomain '{subdomain_name}'")
        return {
            "subdomain_id": subdomain_id,
            "subdomain_name": subdomain_name,
            "tools_enterprise": [],
            "tools_opensource": [],
        }
    
    market_scores = await _assess_market_presence(subdomain_name, tools)
    
    ranked_tools: list[tuple[dict, float, dict]] = []
    
    for tool in tools:
        tool_id = tool["id"]
        product_name = tool["product_name"]
        
        coverage_score = await _calculate_feature_coverage_score(tool_id, subdomain_id)
        rank_dist_score = await _calculate_rank_distribution_score(tool_id, subdomain_id)
        market_score = market_scores.get(product_name, 0.5)
        
        weight_feature = 0.50
        weight_rank = 0.30
        weight_market = 0.20
        
        composite_score = (
            weight_feature * coverage_score +
            weight_rank * rank_dist_score +
            weight_market * market_score
        ) * 100
        
        scores = {
            "feature_coverage_score": round(coverage_score, 4),
            "market_presence_score": round(market_score, 4),
            "rank_distribution_score": round(rank_dist_score, 4),
        }
        
        ranked_tools.append((tool, composite_score, scores))
    
    ranked_tools.sort(key=lambda x: x[1], reverse=True)
    
    await delete_t2_subdomain_tools(subdomain_id)
    
    rankings = []
    for rank, (tool, score, scores) in enumerate(ranked_tools, 1):
        await upsert_t2_subdomain_tool(
            subdomain_id=subdomain_id,
            tool_id=tool["id"],
            vendor=tool["vendor"],
            product_name=tool["product_name"],
            tool_type=tool["tool_type"],
            rank_position=rank,
            composite_score=round(score, 2),
            **scores
        )
        rankings.append({
            "id": tool["id"],
            "vendor": tool["vendor"],
            "product_name": tool["product_name"],
            "tool_type": tool["tool_type"],
            "rank_position": rank,
            "composite_score": round(score, 2),
            **scores
        })
    
    enterprise_rankings = [r for r in rankings if r["tool_type"] == "enterprise"]
    opensource_rankings = [r for r in rankings if r["tool_type"] == "opensource"]
    
    logger.info(
        f"D1 Complete: Ranked {len(enterprise_rankings)} enterprise + "
        f"{len(opensource_rankings)} opensource tools for '{subdomain_name}'"
    )
    
    return {
        "subdomain_id": subdomain_id,
        "subdomain_name": subdomain_name,
        "tools_enterprise": enterprise_rankings,
        "tools_opensource": opensource_rankings,
    }
