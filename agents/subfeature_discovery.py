"""
Subfeature Discovery — optimized

Key improvements over original:
  1. ONE web-search call per subdomain (shared across all features) instead of
     one call per feature — cuts Tavily quota usage by N×.
  2. asyncio.Semaphore gates concurrent LLM calls to settings.llm_concurrency,
     preventing Bedrock throttling when many features are processed in parallel.
  3. asyncio.gather still runs all features concurrently (fast), but the
     semaphore ensures at most `llm_concurrency` LLM calls are in-flight at once.
  4. Supports an optional progress_cb so graph.py can emit per-feature events.
"""

import asyncio
import logging
from typing import Any, Callable, Coroutine
from pydantic import BaseModel

from models.features import SubFeatureResult
from models.web_search import WebSearchResult
from llm.bedrock import structured_call
from db.store import upsert_subfeature, get_subfeatures, get_features
from config.settings import settings
from tools.router import search

logger = logging.getLogger(__name__)


# ── Shared web-search context cache (keyed by subdomain, lives per-process) ─
_search_context_cache: dict[str, str] = {}


async def fetch_subdomain_search_context(subdomain_name: str) -> str:
    """
    Fetch a broad web-search context for the whole subdomain and cache it.
    All features in the same subdomain share this context — avoids N redundant
    Tavily calls (one per feature) that the original code made.
    """
    if subdomain_name in _search_context_cache:
        logger.debug(f"Search context cache hit for '{subdomain_name}'")
        return _search_context_cache[subdomain_name]

    query = f"{subdomain_name} cybersecurity features capabilities sub-features evaluation"
    logger.info(f"Fetching shared search context for '{subdomain_name}': {query}")

    try:
        results, source = await search(query)
        if not results:
            logger.warning(f"No search results for '{subdomain_name}'")
            _search_context_cache[subdomain_name] = ""
            return ""

        context_parts = []
        for i, r in enumerate(results[:10], 1):
            snippet = r.get("content", r.get("snippet", ""))[:500]
            context_parts.append(
                f"{i}. {r.get('title', '')}\n   {snippet}\n   Source: {r.get('url', '')}"
            )

        context = "\n".join(context_parts)
        _search_context_cache[subdomain_name] = context
        logger.info(
            f"Cached search context for '{subdomain_name}' "
            f"({len(results[:10])} results from {source})"
        )
        return context

    except Exception as e:
        logger.error(f"Search failed for '{subdomain_name}': {e}")
        _search_context_cache[subdomain_name] = ""
        return ""


PROMPT_WITH_CONTEXT = """\
You are a cybersecurity sub-feature expert. Using the web search results below,
identify 4-6 specific sub-features for the given feature.

Subdomain : {subdomain}
Feature   : {feature}

Web Search Context:
{search_context}

Requirements:
- Return EXACTLY {max_subfeatures} distinct, fine-grained sub-features that enterprises evaluate
- Clear, concise names drawn from actual product documentation
- Specific enough to determine per-tool support (✔ / ✘ / Partial)
- No overlapping concepts — each must be meaningfully different

Return ONLY valid JSON:
{{
    "feature": "{feature}",
    "subfeatures": ["Sub-feature 1", "Sub-feature 2", ...]
}}

Provide EXACTLY {max_subfeatures} subfeatures — no more, no fewer.
"""

PROMPT_FALLBACK = """\
You are a cybersecurity sub-feature expert. Identify 4-6 specific sub-features
for the given feature within this subdomain.

Subdomain : {subdomain}
Feature   : {feature}

Requirements:
- Return EXACTLY {max_subfeatures} distinct, fine-grained sub-features that enterprises evaluate
- Clear, concise names
- Specific enough to determine per-tool support (✔ / ✘ / Partial)
- No overlapping concepts — each must be meaningfully different

Return ONLY valid JSON:
{{
    "feature": "{feature}",
    "subfeatures": ["Sub-feature 1", "Sub-feature 2", ...]
}}

Provide EXACTLY {max_subfeatures} subfeatures — no more, no fewer.
"""


async def discover_subfeatures(
    subdomain_id: int,
    subdomain_name: str,
    features: list[dict[str, Any]],
    progress_cb: Callable[[str], Coroutine] | None = None,
) -> dict[str, list[str]]:
    """
    Discover sub-features for every feature concurrently.

    progress_cb(feature_name) is awaited after each feature completes so that
    the orchestrator can emit fine-grained progress events to the TUI.
    """
    logger.info(
        f"Discovering subfeatures for {len(features)} features in '{subdomain_name}'"
    )

    # Fetch shared search context ONCE for the whole subdomain
    search_context = ""
    if settings.web_search_enabled:
        search_context = await fetch_subdomain_search_context(subdomain_name)

    # Semaphore caps concurrent LLM calls
    llm_sem = asyncio.Semaphore(settings.llm_concurrency)

    result: dict[str, list[str]] = {}

    async def process_feature(feature: dict[str, Any]) -> tuple[int, list[str]]:
        if search_context:
            prompt = PROMPT_WITH_CONTEXT.format(
                subdomain=subdomain_name,
                feature=feature["name"],
                search_context=search_context,
                max_subfeatures=settings.max_subfeatures_per_feature,
            )
        else:
            prompt = PROMPT_FALLBACK.format(
                subdomain=subdomain_name,
                feature=feature["name"],
                max_subfeatures=settings.max_subfeatures_per_feature,
            )

        async with llm_sem:
            logger.info(f"  LLM: subfeatures for '{feature['name']}'")
            sf_result = await structured_call(
                prompt, SubFeatureResult, temperature=0.3, max_tokens=1024
            )

        feature_id = feature["id"]
        raw_sf_count = len(sf_result.subfeatures)
        if raw_sf_count > settings.max_subfeatures_per_feature:
            logger.warning(
                f"  LLM returned {raw_sf_count} subfeatures for "
                f"'{feature['name']}' — capping to {settings.max_subfeatures_per_feature}"
            )
        for i, sf_name in enumerate(sf_result.subfeatures[:settings.max_subfeatures_per_feature]):
            await upsert_subfeature(feature_id, sf_name.strip(), i + 1)

        saved = await get_subfeatures(feature_id)
        names = [sf["name"] for sf in saved]

        if progress_cb:
            await progress_cb(feature["name"])

        logger.info(
            f"  ✓ '{feature['name']}' → {len(names)} subfeatures"
        )
        return feature_id, names

    # Run ALL features concurrently (semaphore keeps LLM calls bounded)
    pairs = await asyncio.gather(
        *[process_feature(f) for f in features], return_exceptions=True
    )

    for item in pairs:
        if isinstance(item, Exception):
            logger.error(f"Subfeature discovery chunk failed: {item}")
            continue
        feature_id, names = item
        feature_name = next(
            (f["name"] for f in features if f["id"] == feature_id), ""
        )
        result[feature_name] = names

    total = sum(len(v) for v in result.values())
    logger.info(
        f"Subfeature discovery done: {total} sub-features "
        f"across {len(result)} features"
    )
    return result


class SubfeatureDiscoveryOutput(BaseModel):
    sub_features: dict[str, list[str]]


async def run_subfeature_discovery(
    subdomain_id: int,
    subdomain_name: str,
    progress_cb: Callable[[str], Coroutine] | None = None,
) -> SubfeatureDiscoveryOutput:
    features = await get_features(subdomain_id)

    if not features:
        logger.warning(f"No features found for subdomain {subdomain_id}")
        return SubfeatureDiscoveryOutput(sub_features={})

    sub_features = await discover_subfeatures(
        subdomain_id, subdomain_name, features, progress_cb=progress_cb
    )
    return SubfeatureDiscoveryOutput(sub_features=sub_features)
