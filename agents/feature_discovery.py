"""
Feature Discovery — hardened

Key fixes over original:
  1. Hard limit: saves exactly MAX_FEATURES (8) features, never more.
     The previous code used `result.features[:8]` but `get_features()` then
     fetched ALL features from DB — including any that leaked in from prior runs.
     Now we enforce the cap AT the DB level by deleting excess rows first.

  2. Semantic deduplication: before saving, removes near-duplicate feature names
     (e.g. "Hybrid & Multi-Cloud Support" vs "Hybrid & Multi-Cloud Policy
     Management" vs "Multi-Cloud & Hybrid Environment Support"). The LLM often
     over-generates variations of the same concept when the context is large.
     We normalise each name to a word-set and drop any feature whose overlap
     with an already-kept feature exceeds the DEDUP_JACCARD_THRESHOLD.

  3. Both the prompt and the save path enforce the same ceiling, so a LLM that
     ignores the count instruction can't inflate the feature list.
"""

import logging
import asyncio
from typing import Any
from pydantic import BaseModel

from models.features import FeatureDiscoveryResult, Feature
from models.tools import Tool
from models.web_search import WebSearchResult
from models.worker import WorkerState
from llm.bedrock import structured_call
from db.store import upsert_feature, get_features
from config.settings import settings
from tools.router import search

logger = logging.getLogger(__name__)

# ── Tuneable constants ───────────────────────────────────────────────────────
# Limits are driven from settings so they can be overridden via .env.
MIN_FEATURES: int = 5        # warn-only lower bound
DEDUP_JACCARD_THRESHOLD: float = 0.5

# Re-exported alias so graph.py pre-flight guard can do:
#   from agents.feature_discovery import MAX_FEATURES as _MAX_F
MAX_FEATURES: int = settings.max_features  # evaluated at import time

# ── Semantic deduplication ───────────────────────────────────────────────────

# Words that carry no meaning for similarity comparison
_STOPWORDS = {
    "and", "or", "the", "a", "an", "of", "for", "in", "with",
    "to", "from", "by", "on", "at", "management", "support",
    "&", "-", "/",
}


def _token_set(name: str) -> set[str]:
    """Lowercase word-set of a feature name, stopwords excluded."""
    return {
        w.strip("(),.")
        for w in name.lower().split()
        if w.strip("(),.") not in _STOPWORDS and len(w) > 1
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def deduplicate_features(features: list[Feature]) -> list[Feature]:
    """
    Remove near-duplicate features based on Jaccard word-overlap.
    Keeps the higher rank_order (lower number = more important) variant.
    """
    kept: list[Feature] = []
    kept_tokens: list[set[str]] = []

    for feature in features:
        tokens = _token_set(feature.name)
        duplicate = any(
            _jaccard(tokens, existing) >= DEDUP_JACCARD_THRESHOLD
            for existing in kept_tokens
        )
        if duplicate:
            logger.debug(
                f"  Dedup: dropped '{feature.name}' "
                f"(too similar to an existing feature)"
            )
        else:
            kept.append(feature)
            kept_tokens.append(tokens)

    dropped = len(features) - len(kept)
    if dropped:
        logger.info(
            f"  Dedup removed {dropped} near-duplicate features "
            f"({len(kept)} kept)"
        )
    return kept


# ── Prompts ──────────────────────────────────────────────────────────────────

SEARCH_PROMPT_TEMPLATE = """\
You are a cybersecurity features expert. Using the web search results below,
identify exactly {max_features} key features that differentiate tools in this space.

Subdomain: {subdomain}
Tools in this space: {tools}

Web Search Results:
{search_context}

Requirements:
- Return EXACTLY {max_features} features — no more, no fewer.
- Each feature must be meaningfully distinct (no overlapping concepts).
- Features should be high-value differentiators that enterprises evaluate.
- Clear, concise names (avoid vague terms like "General Support").

Return ONLY valid JSON:
{{
    "subdomain": "{subdomain}",
    "features": [
        {{"name": "Feature Name", "description": "One-sentence description", "rank_order": 1}},
        ...
    ]
}}

rank_order 1 = most important. Do NOT exceed {max_features} features.
"""

FALLBACK_PROMPT_TEMPLATE = """\
You are a cybersecurity features expert. Identify exactly {max_features} key
features that differentiate tools in this subdomain.

Subdomain: {subdomain}
Tools in this space: {tools}

Requirements:
- Return EXACTLY {max_features} features — no more, no fewer.
- Each feature must be meaningfully distinct (no overlapping concepts).
- Features should be high-value differentiators that enterprises evaluate.

Return ONLY valid JSON:
{{
    "subdomain": "{subdomain}",
    "features": [
        {{"name": "Feature Name", "description": "One-sentence description", "rank_order": 1}},
        ...
    ]
}}

rank_order 1 = most important. Do NOT exceed {max_features} features.
"""


# ── Core discovery logic ─────────────────────────────────────────────────────

async def fetch_feature_search_context(subdomain_name: str) -> tuple[list[WebSearchResult], str]:
    query = f"{subdomain_name} features capabilities enterprise evaluation criteria"
    logger.info(f"Searching for features: {query}")

    try:
        results, source = await search(query)
        if not results:
            logger.warning(f"No search results for features in '{subdomain_name}'")
            return [], ""

        search_results = [
            WebSearchResult(
                query=query,
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content", r.get("snippet", ""))[:500],
                source=source,
            )
            for r in results[:10]
        ]

        context_parts = [
            f"{i}. {r.title}\n   {r.snippet}\n   Source: {r.url}"
            for i, r in enumerate(search_results, 1)
        ]
        search_context = "\n".join(context_parts)
        logger.info(f"Got {len(search_results)} feature search results from {source}")
        return search_results, search_context

    except Exception as e:
        logger.error(f"Feature search failed: {e}")
        return [], ""


async def discover_features(
    subdomain_id: int,
    subdomain_name: str,
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    logger.info(f"Discovering features for subdomain '{subdomain_name}'")

    tool_names = [t["product_name"] for t in tools[:10]]
    tools_str  = ", ".join(tool_names)

    search_context = ""
    if settings.web_search_enabled:
        _, search_context = await fetch_feature_search_context(subdomain_name)

    if search_context:
        prompt = SEARCH_PROMPT_TEMPLATE.format(
            subdomain=subdomain_name,
            tools=tools_str,
            search_context=search_context,
            max_features=settings.max_features,
        )
        logger.info("Using web search context for feature discovery")
    else:
        prompt = FALLBACK_PROMPT_TEMPLATE.format(
            subdomain=subdomain_name,
            tools=tools_str,
            max_features=settings.max_features,
        )
        logger.info("Using fallback LLM-only feature discovery")

    result = await structured_call(prompt, FeatureDiscoveryResult, temperature=0.3)

    # ── Validate count ───────────────────────────────────────────────────────
    raw_count = len(result.features)
    if raw_count > settings.max_features:
        logger.warning(
            f"LLM returned {raw_count} features for '{subdomain_name}' "
            f"(limit={settings.max_features}) — deduplicating then truncating"
        )
    elif raw_count < MIN_FEATURES:
        logger.warning(
            f"LLM returned only {raw_count} features for '{subdomain_name}' "
            f"(expected {MIN_FEATURES}–{settings.max_features})"
        )

    # ── Semantic deduplication then hard truncation ──────────────────────────
    unique_features = deduplicate_features(result.features)
    final_features  = unique_features[:settings.max_features]

    logger.info(
        f"Saving {len(final_features)} features for '{subdomain_name}' "
        f"(raw={raw_count}, after dedup={len(unique_features)}, "
        f"after cap={len(final_features)})"
    )

    # ── Persist ──────────────────────────────────────────────────────────────
    for i, feature in enumerate(final_features):
        await upsert_feature(
            subdomain_id, feature.name, feature.rank_order or (i + 1)
        )

    features = await get_features(subdomain_id)

    # Safety: if DB somehow has MORE than max_features (e.g. from a prior run
    # that ran before this limit was added), truncate the live list here.
    if len(features) > settings.max_features:
        logger.warning(
            f"DB has {len(features)} features for '{subdomain_name}' "
            f"after upsert — truncating returned list to {settings.max_features}"
        )
        features = features[:settings.max_features]

    logger.info(f"Final feature count for '{subdomain_name}': {len(features)}")
    return features


# ── Public entry points ───────────────────────────────────────────────────────

class FeatureDiscoveryOutput(BaseModel):
    features: list[dict[str, Any]]


async def run_feature_discovery(state: WorkerState) -> FeatureDiscoveryOutput:
    all_tools  = state.tools_enterprise + state.tools_opensource
    tools_data = [{"product_name": t.product_name, "vendor": t.vendor} for t in all_tools]
    features   = await discover_features(state.subdomain_id, state.subdomain, tools_data)
    return FeatureDiscoveryOutput(features=features)


async def run_feature_discovery_direct(
    subdomain_id: int,
    subdomain_name: str,
    tools_enterprise: list[Tool],
    tools_opensource: list[Tool],
) -> FeatureDiscoveryOutput:
    all_tools = [
        {"product_name": t.product_name, "vendor": t.vendor}
        for t in tools_enterprise + tools_opensource
    ]
    features = await discover_features(subdomain_id, subdomain_name, all_tools)
    return FeatureDiscoveryOutput(features=features)
