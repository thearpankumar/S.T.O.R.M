"""
D3: Domain Subfeature Generator

Generates subfeatures for each domain-level feature.
These subfeatures are more specific evaluatable criteria
that can be assessed against tools.
"""

import logging
from typing import Any

from pydantic import BaseModel

from config.settings import settings
from db.domain_store import (
    get_t2_domain_features,
    upsert_t2_domain_subfeature,
    get_t2_domain_subfeatures,
    delete_t2_domain_subfeatures,
)
from llm.bedrock import structured_call
from models.domain_features import DomainSubFeature, DomainSubFeatureResult

logger = logging.getLogger(__name__)


class SubFeatureGenerationResult(BaseModel):
    subfeatures: list[str]


SUBFEATURE_PROMPT_TEMPLATE = """\
You are a cybersecurity product evaluator. For the given domain-level feature,
generate {max_sf} specific evaluatable subfeatures (criteria) that can be used 
to compare tools.

Domain: {domain}
Feature: {feature_name}

Context: This feature is used to compare tools across the {domain} domain.
Generate concrete, measurable sub-criteria.

Requirements:
1. Each subfeature should be a specific criterion that can be evaluated as:
   - Fully supported (✔) - Tool has native built-in capability
   - Partially supported (Partial) - Requires plugins, add-ons, or custom config
   - Not supported (✘) - Tool lacks this capability
2. Subfeatures should be clear and specific, not vague
3. Each should be independently evaluatable
4. Order by importance (most critical first)

Return ONLY a JSON array of subfeature names:
{{
    "subfeatures": [
        "Subfeature 1 name",
        "Subfeature 2 name",
        ...
    ]
}}

Provide exactly {max_sf} subfeatures.
"""


async def generate_domain_subfeatures(
    domain_id: int,
    domain_name: str,
    feature_id: int,
    feature_name: str,
) -> DomainSubFeatureResult:
    logger.info(f"D3: Generating subfeatures for feature '{feature_name}'")
    
    prompt = SUBFEATURE_PROMPT_TEMPLATE.format(
        domain=domain_name,
        feature_name=feature_name,
        max_sf=settings.t2_max_subfeatures_per_feature,
    )
    
    try:
        result = await structured_call(
            prompt,
            SubFeatureGenerationResult,
            temperature=0.4
        )
        
        subfeatures_raw = result.subfeatures[:settings.t2_max_subfeatures_per_feature]
        
    except Exception as e:
        logger.warning(f"Subfeature generation failed for '{feature_name}': {e}")
        subfeatures_raw = [
            f"{feature_name} Capability Level",
            f"{feature_name} Configuration Options",
            f"{feature_name} API Integration",
            f"{feature_name} Reporting & Analytics",
        ]
    
    subfeatures = []
    for rank, sf_name in enumerate(subfeatures_raw, 1):
        sf_id = await upsert_t2_domain_subfeature(
            domain_feature_id=feature_id,
            name=sf_name,
            rank_order=rank,
        )
        subfeatures.append(DomainSubFeature(
            feature_name=feature_name,
            name=sf_name,
            rank_order=rank,
        ))
    
    logger.info(f"D3: Generated {len(subfeatures)} subfeatures for '{feature_name}'")
    
    return DomainSubFeatureResult(
        domain_feature_id=feature_id,
        feature_name=feature_name,
        subfeatures=subfeatures,
    )


async def generate_all_domain_subfeatures(
    domain_id: int,
    domain_name: str,
) -> list[DomainSubFeature]:
    logger.info(f"D3: Generating all subfeatures for domain '{domain_name}'")
    
    await delete_t2_domain_subfeatures(domain_id)
    
    features = await get_t2_domain_features(domain_id)
    
    if not features:
        logger.warning(f"No features found for domain '{domain_name}'")
        return []
    
    all_subfeatures = []
    
    for feature in features:
        result = await generate_domain_subfeatures(
            domain_id=domain_id,
            domain_name=domain_name,
            feature_id=feature["id"],
            feature_name=feature["name"],
        )
        all_subfeatures.extend(result.subfeatures)
    
    logger.info(f"D3 Complete: {len(all_subfeatures)} total subfeatures for '{domain_name}'")
    
    return all_subfeatures
