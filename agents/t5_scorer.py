"""
agents/t5_scorer.py - Technique 5 Score Card generation engine.

Computes multi-dimensional scores for each tool based on data from T1, T2, T3, and T4.
Optionally generates 1-sentence strategic insights using LLMs.
"""

import asyncio
import json
import logging
from typing import Any, Callable, Awaitable

from pydantic import BaseModel

from config.settings import settings
from config.domains import CYBERSECURITY_DOMAINS
from db.store import db
from db.t5_store import bulk_upsert_t5_scores, update_t5_tool_insights
from llm.bedrock import structured_call

logger = logging.getLogger(__name__)


TOTAL_DOMAINS = len(CYBERSECURITY_DOMAINS)


# ── Scoring Configuration ──

LICENSE_SCORES = {
    "Commercial": 90, 
    "Freemium": 75, 
    "Proprietary": 80,
    "Apache-2.0": 85, 
    "MIT": 80, 
    "GPL-3.0": 70, 
    "GPL-2.0": 65,
    "LGPL": 65, 
    "BSD": 80, 
    "MPL-2.0": 70, 
    "Unknown": 30
}

# ── Pydantic models for LLM Insights ──

class T5ToolInsight(BaseModel):
    t4_tool_id: int
    quadrant_position: str
    insight: str

class T5BatchInsights(BaseModel):
    insights: list[T5ToolInsight]


# ── Scoring Logic ──

def get_grade(score: float) -> str:
    if score >= 90: return "A+"
    if score >= 80: return "A"
    if score >= 70: return "B+"
    if score >= 60: return "B"
    if score >= 50: return "C"
    return "D"


async def gather_scoring_data() -> list[dict[str, Any]]:
    """Gather all necessary data from T4, T3, and T2 for scoring."""
    
    # Use CTE to prevent O(N*M) correlated subqueries across large tool sets
    query = """
        WITH SubdomainCounts AS (
            SELECT subdomain_id, COUNT(*) as total_tools
            FROM t2_subdomain_tools
            GROUP BY subdomain_id
        ),
        ToolAvgRanks AS (
            SELECT 
                t4ts.t4_tool_id,
                AVG(CAST(t2st.rank_position AS FLOAT) / sc.total_tools) as avg_rank_percentile
            FROM t4_tool_subdomains t4ts
            JOIN t2_subdomain_tools t2st ON t4ts.t1_tool_id = t2st.tool_id
            JOIN SubdomainCounts sc ON t2st.subdomain_id = sc.subdomain_id
            GROUP BY t4ts.t4_tool_id
        ),
        PrimaryDomain AS (
            SELECT t4d.t4_tool_id, d.name as primary_domain
            FROM t4_tool_domains t4d
            LEFT JOIN domains d ON t4d.primary_domain_id = d.id
        )
        SELECT 
            t4.id AS t4_tool_id,
            t4.vendor,
            t4.product_name,
            t4.tool_type,
            t4.license_model,
            t4f.support_rate,
            t4d.domain_count,
            t3.nist_functions,
            tar.avg_rank_percentile,
            pd.primary_domain
        FROM t4_tools t4
        LEFT JOIN t4_tool_features t4f ON t4.id = t4f.t4_tool_id
        LEFT JOIN t4_tool_domains t4d ON t4.id = t4d.t4_tool_id
        LEFT JOIN t3_tools t3 ON LOWER(t4.vendor) = LOWER(t3.vendor) AND LOWER(t4.product_name) = LOWER(t3.product_name)
        LEFT JOIN ToolAvgRanks tar ON t4.id = tar.t4_tool_id
        LEFT JOIN PrimaryDomain pd ON t4.id = pd.t4_tool_id
    """
    
    rows = await db.fetchall(query)
    return [dict(r) for r in rows]


def compute_scores(tools: list[dict[str, Any]]) -> list[tuple]:
    """
    Compute dimension scores and composite for a list of tools.
    Returns list of tuples ready for bulk_upsert_t5_scores.
    """
    results = []
    
    w_d1 = settings.t5_weight_feature_coverage
    w_d2 = settings.t5_weight_domain_breadth
    w_d3 = settings.t5_weight_nist_alignment
    w_d4 = settings.t5_weight_market_maturity
    w_d5 = settings.t5_weight_ranking_signal
    
    for t in tools:
        t4_id = t["t4_tool_id"]
        vendor = t["vendor"]
        product_name = t["product_name"]
        
        domain_count = t["domain_count"] or 0
        primary_domain = t["primary_domain"] or "Unknown"
        tool_category = "Platform" if domain_count >= 3 else "Point Solution"
        
        # D1: Feature Coverage (0-100)
        support_rate = t["support_rate"] or 0.0
        d1 = support_rate * 100.0
        
        # D2: Domain Breadth (0-100)
        d2 = min(100.0, (domain_count / TOTAL_DOMAINS) * 100.0)
        
        # D3: NIST Alignment (0-100)
        has_t3 = t["nist_functions"] is not None
        if has_t3:
            try:
                funcs = json.loads(t["nist_functions"])
                d3 = min(100.0, (len(funcs) / 6.0) * 100.0)
            except json.JSONDecodeError as e:
                logger.warning(f"Invalid NIST functions JSON for tool {t.get('t4_tool_id')}: {e}")
                d3 = 0.0
        else:
            d3 = 0.0
            
        # D4: Market Maturity (0-100)
        base_license = LICENSE_SCORES.get(t["license_model"] or "Unknown", 30)
        type_bonus = 10 if (t["tool_type"] or "").lower() == "enterprise" else 0
        d4 = min(100.0, float(base_license + type_bonus))
        
        # D5: Ranking Signal (0-100)
        avg_rank_pct = t["avg_rank_percentile"]
        has_t2 = avg_rank_pct is not None
        if has_t2:
            # lower rank percentile is better (e.g., 0.1 = top 10%).
            d5 = max(0.0, (1.0 - avg_rank_pct) * 100.0)
        else:
            d5 = 0.0
            
        # Composite weight calculation

        adj_w_d1 = w_d1
        adj_w_d2 = w_d2
        
        if tool_category == "Point Solution":
            # Redistribute D2 weight into D1 (don't penalize specialization)
            adj_w_d1 += adj_w_d2
            adj_w_d2 = 0.0
        
        # Redistribute weights for unavailable data dimensions
        # Calculate missing weight AFTER the Point Solution redistribution
        actual_w_d3 = w_d3 if has_t3 else 0.0
        actual_w_d5 = w_d5 if has_t2 else 0.0
        missing_weight = (w_d3 if not has_t3 else 0.0) + (w_d5 if not has_t2 else 0.0)
        
        if missing_weight > 0:
            if adj_w_d2 > 0:
                # Platform: split missing weight evenly between D1 and D2
                adj_w_d1 += (missing_weight / 2.0)
                adj_w_d2 += (missing_weight / 2.0)
            else:
                # Point Solution: all missing weight goes to D1
                adj_w_d1 += missing_weight
        
        # Normalize so weights always sum to exactly 1.0
        total_w = adj_w_d1 + adj_w_d2 + actual_w_d3 + w_d4 + actual_w_d5
        if total_w > 0:
            adj_w_d1 /= total_w
            adj_w_d2 /= total_w
            actual_w_d3 /= total_w
            w_d4_norm = w_d4 / total_w
            actual_w_d5 /= total_w
        else:
            w_d4_norm = w_d4
        
        composite = (
            (d1 * adj_w_d1) +
            (d2 * adj_w_d2) +
            (d3 * actual_w_d3) +
            (d4 * w_d4_norm) +
            (d5 * actual_w_d5)
        )
        
        grade = get_grade(composite)
        
        results.append({
            "t4_tool_id": t4_id,
            "vendor": vendor,
            "product": product_name,
            "primary_domain": primary_domain,
            "tool_category": tool_category,
            "d1": d1,
            "d2": d2,
            "d3": d3,
            "d4": d4,
            "d5": d5,
            "composite": composite,
            "grade": grade
        })
        
    # Group by primary domain for localized ranking
    from collections import defaultdict
    domain_groups = defaultdict(list)
    for r in results:
        domain_groups[r["primary_domain"]].append(r)
    
    tuples = []
    for dom, dom_tools in domain_groups.items():
        # Sort by composite (descending), then vendor, then product for deterministic tie-breaking
        dom_tools.sort(key=lambda x: (-x["composite"], x["vendor"], x["product"]))
        for i, r in enumerate(dom_tools, 1):
            tuples.append((
                r["t4_tool_id"], r["vendor"], r["product"],
                r["primary_domain"], r["tool_category"],
                r["d1"], r["d2"], r["d3"], r["d4"], r["d5"],
                r["composite"], r["grade"], i  # i is domain_rank
            ))
            
    return tuples


async def run_s2_scoring(
    on_progress: Callable[[float, str], Awaitable[None]] | None = None
) -> int:
    """Run S2 and S3: Gather data, compute scores, rank, and bulk insert."""
    logger.info("T5 S2: Gathering data and computing scores...")
    
    tools = await gather_scoring_data()
    if not tools:
        logger.warning("No canonical T4 tools found for scoring.")
        return 0
        
    if on_progress:
        await on_progress(0.20, f"Gathered data for {len(tools)} tools")
        
    score_tuples = compute_scores(tools)
    
    if on_progress:
        await on_progress(0.50, f"Computed scores and ranks")
        
    await bulk_upsert_t5_scores(score_tuples)
    
    if on_progress:
        await on_progress(0.70, f"Saved scores to database")
        
    logger.info(f"T5 S2/S3 Complete: Scored {len(score_tuples)} tools.")
    return len(score_tuples)


# ── LLM Insights (Optional Stage) ──

_INSIGHT_PROMPT = """\
You are a strategic cybersecurity analyst evaluating tools within their respective domains.
Review the following tool scores across 5 dimensions:
D1: Feature Coverage | D2: Domain Breadth | D3: NIST Alignment | D4: Market Maturity | D5: Ranking Signal

For each tool, determine its placement in a Gartner-style Magic Quadrant:
- "Leader" (High maturity, high coverage)
- "Visionary" (Good coverage, newer/niche maturity)
- "Challenger" (High maturity, lower coverage)
- "Niche Player" (Point solutions, specialized)

Then, provide ONE short sentence (max 20 words) of strategic insight explaining why it is placed there or its overall positioning within its domain.

Tools:
{tools_text}

Return JSON matching exactly this structure:
{{
  "insights": [
    {{
      "t4_tool_id": 123,
      "quadrant_position": "Leader",
      "insight": "..."
    }}
  ]
}}
"""

async def generate_insight_batch(batch: list[dict], semaphore: asyncio.Semaphore) -> list[tuple[int, str, str]]:
    lines = []
    for t in batch:
        lines.append(
            f"ID: {t['t4_tool_id']} | {t['vendor']} {t['product_name']} "
            f"| Domain: {t['primary_domain']} | Category: {t['tool_category']} "
            f"| Composite: {t['composite_score']:.1f} ({t['grade']}) "
            f"| D1:{t['d1_feature_coverage']:.0f} D2:{t['d2_domain_breadth']:.0f} "
            f"D3:{t['d3_nist_alignment']:.0f} D4:{t['d4_market_maturity']:.0f} D5:{t['d5_ranking_signal']:.0f}"
        )
    tools_text = "\n".join(lines)
    prompt = _INSIGHT_PROMPT.format(tools_text=tools_text)
    
    async with semaphore:
        try:
            result = await structured_call(prompt, T5BatchInsights, temperature=0.3)
            return [(r.t4_tool_id, r.quadrant_position, r.insight) for r in result.insights]
        except Exception as e:
            logger.warning(f"Failed to generate insights for batch: {e}")
            return []


async def run_s4_insights(
    on_progress: Callable[[float, str], Awaitable[None]] | None = None
) -> int:
    """Run optional S4 LLM insights generation."""
    if not settings.t5_enable_llm_insights:
        logger.info("T5 S4: LLM insights disabled in settings. Skipping.")
        return 0
        
    logger.info("T5 S4: Generating strategic insights...")
    
    # Get Top N tools per domain to generate insights for
    limit = settings.t5_max_llm_insights
    rows = await db.fetchall(
        """
        SELECT * FROM t5_tool_scores 
        WHERE domain_rank <= 10 
        ORDER BY primary_domain ASC, domain_rank ASC 
        LIMIT ?
        """,
        (limit,)
    )
    if not rows:
        return 0
        
    tools = [dict(r) for r in rows]
    batch_size = settings.t5_llm_batch_size
    batches = [tools[i:i + batch_size] for i in range(0, len(tools), batch_size)]
    semaphore = asyncio.Semaphore(settings.llm_concurrency)
    
    total_insights = 0
    tasks = [generate_insight_batch(batch, semaphore) for batch in batches]
    
    # Process as they complete to give real-time UI updates
    completed = 0
    for task in asyncio.as_completed(tasks):
        try:
            result = await task
            if result:
                await update_t5_tool_insights(result)
                total_insights += len(result)
        except Exception as e:
            logger.warning(f"Insight batch failed: {e}")
            
        completed += 1
        if on_progress:
            pct = 0.70 + (completed / len(batches)) * 0.20
            await on_progress(pct, f"Generated {total_insights} insights")
            
    logger.info(f"T5 S4 Complete: Generated {total_insights} insights.")
    return total_insights
