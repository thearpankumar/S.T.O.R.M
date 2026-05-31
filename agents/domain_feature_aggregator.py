"""
D2: Domain Feature Aggregator

Aggregates all features from all subdomains within a domain,
applies semantic deduplication, and uses LLM to merge similar features
into domain-level canonical features.
"""

import logging
from collections import Counter
from typing import Any

from pydantic import BaseModel

from config.settings import settings
from db.domain_store import (
    get_all_features_for_domain,
    upsert_t2_domain_feature,
    delete_t2_domain_features,
)
from db.store import get_domain_id
from llm.bedrock import structured_call
from models.domain_features import DomainFeature, DomainFeatureResult

logger = logging.getLogger(__name__)


_STOPWORDS = {
    "and", "or", "the", "a", "an", "of", "for", "in", "with",
    "to", "from", "by", "on", "at", "management", "support",
    "&", "-", "/",
}

DEDUP_JACCARD_THRESHOLD: float = 0.5


class LLMFeatureMergeResult(BaseModel):
    features: list[dict[str, Any]]


FEATURE_MERGE_PROMPT = """\
You are a cybersecurity domain expert. Given the following features collected from 
various subdomains within the {domain} domain, consolidate them into {max_features} 
canonical domain-level features.

Raw features from subdomains:
{features_list}

Requirements:
1. Merge similar/duplicate features into single canonical features
2. Keep the most important and generalizable features
3. Each feature should be applicable across most tools in the domain
4. Feature names should be clear and concise

Return ONLY valid JSON:
{{
    "features": [
        {{"name": "Feature Name", "description": "Brief description", "importance": "high|medium|low"}},
        ...
    ]
}}

Provide exactly {max_features} features, ordered by importance (highest first).
"""


def _token_set(name: str) -> set[str]:
    return {
        w.strip("(),.")
        for w in name.lower().split()
        if w.strip("(),.") not in _STOPWORDS and len(w) > 1
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _deduplicate_features(features: list[dict]) -> list[dict]:
    kept: list[dict] = []
    kept_tokens: list[set[str]] = []

    for feature in features:
        name = feature.get("name", "")
        tokens = _token_set(name)
        
        duplicate = any(
            _jaccard(tokens, existing) >= DEDUP_JACCARD_THRESHOLD
            for existing in kept_tokens
        )
        
        if duplicate:
            logger.debug(f"  Dedup: dropped '{name}' (too similar to existing)")
        else:
            kept.append(feature)
            kept_tokens.append(tokens)

    dropped = len(features) - len(kept)
    if dropped:
        logger.info(f"Dedup removed {dropped} near-duplicate features ({len(kept)} kept)")
    
    return kept


async def aggregate_domain_features(
    domain_id: int,
    domain_name: str,
) -> DomainFeatureResult:
    logger.info(f"D2: Aggregating features for domain '{domain_name}'")
    
    all_features = await get_all_features_for_domain(domain_id)
    
    if not all_features:
        logger.warning(f"No features found for domain '{domain_name}'")
        return DomainFeatureResult(
            domain_id=domain_id,
            domain_name=domain_name,
            features=[],
        )
    
    feature_counter = Counter(f["name"] for f in all_features)
    
    logger.info(f"Found {len(all_features)} feature records ({len(feature_counter)} unique)")
    
    unique_features: dict[str, dict] = {}
    for feature in all_features:
        name = feature["name"]
        if name not in unique_features:
            unique_features[name] = {
                "name": name,
                "subdomains": [feature["subdomain_name"]],
                "count": feature_counter[name],
            }
        else:
            unique_features[name]["subdomains"].append(feature["subdomain_name"])
    
    features_list = list(unique_features.values())
    
    deduped_features = _deduplicate_features(features_list)
    
    features_text = "\n".join([
        f"- {f['name']} (appears in {f['count']} subdomains: {', '.join(f['subdomains'][:3])}...)"
        for f in deduped_features[:30]
    ])
    
    prompt = FEATURE_MERGE_PROMPT.format(
        domain=domain_name,
        features_list=features_text,
        max_features=settings.t2_max_features,
    )
    
    try:
        llm_result = await structured_call(
            prompt,
            LLMFeatureMergeResult,
            temperature=0.3
        )
        
        merged_features = []
        for i, f in enumerate(llm_result.features[:settings.t2_max_features]):
            source = unique_features.get(f["name"], {}).get("subdomains", [])
            merged_features.append({
                "name": f["name"],
                "description": f.get("description", ""),
                "importance": f.get("importance", "medium"),
                "source_subdomains": source,
            })
        
        logger.info(f"LLM merged into {len(merged_features)} domain features")
        
    except Exception as e:
        logger.warning(f"LLM merge failed: {e}, using frequency-based selection")
        
        sorted_features = sorted(
            deduped_features,
            key=lambda x: x["count"],
            reverse=True
        )
        
        merged_features = [
            {
                "name": f["name"],
                "source_subdomains": f["subdomains"],
            }
            for f in sorted_features[:settings.t2_max_features]
        ]
    
    await delete_t2_domain_features(domain_id)
    
    final_features = []
    for rank, feature in enumerate(merged_features, 1):
        feature_id = await upsert_t2_domain_feature(
            domain_id=domain_id,
            name=feature["name"],
            rank_order=rank,
            source_subdomains=feature.get("source_subdomains", []),
        )
        final_features.append(DomainFeature(
            name=feature["name"],
            rank_order=rank,
            source_subdomains=feature.get("source_subdomains", []),
        ))
    
    logger.info(f"D2 Complete: {len(final_features)} domain features for '{domain_name}'")
    
    return DomainFeatureResult(
        domain_id=domain_id,
        domain_name=domain_name,
        features=final_features,
    )
