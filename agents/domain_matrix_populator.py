"""
D4: Domain Matrix Populator

Populates the tool-support matrix at the domain level.
Evaluates each tool against each subfeature and assigns support levels.

Uses data from Technique 1 when available (inheritance),
otherwise makes new LLM assessments.
"""

import logging
from typing import Any

from pydantic import BaseModel

from config.settings import settings
from db.domain_store import (
    get_t2_domain_tools,
    get_t2_domain_subfeatures_by_domain,
    get_all_matrix_cells_for_domain,
    upsert_t2_domain_matrix_cell,
    delete_t2_domain_matrix_cells,
)
from db.store import get_domain_id
from llm.bedrock import structured_call
from models.domain_matrix import DomainMatrixBatch, DomainMatrixCell

logger = logging.getLogger(__name__)


class MatrixAssessmentResult(BaseModel):
    support_levels: dict[str, str]


MATRIX_BATCH_PROMPT = """\
You are a cybersecurity product evaluator. Assess the support level for each 
subfeature across the given tools.

Domain: {domain}
Feature: {feature_name}
Subfeature: {subfeature_name}

Tools to evaluate:
{tools_list}

For each tool, determine if it supports the subfeature:
- "✔" = Fully supported natively (built-in, no extra configuration)
- "Partial" = Partially supported (requires plugins, add-ons, or custom config)
- "✘" = Not supported (tool lacks this capability)

Return ONLY valid JSON:
{{
    "support_levels": {{
        "Tool 1 Name": "✔",
        "Tool 2 Name": "Partial",
        "Tool 3 Name": "✘",
        ...
    }}
}}

Evaluate ALL tools listed. Be realistic - most mature enterprise tools support 
common features natively.
"""


def _find_inherited_support(
    tool_name: str,
    subfeature_name: str,
    feature_name: str,
    t1_matrix_cells: list[dict],
) -> str | None:
    for cell in t1_matrix_cells:
        if cell["product_name"] == tool_name:
            if subfeature_name.lower() in cell.get("subfeature_name", "").lower():
                return cell["support_level"]
    return None


async def _assess_batch(
    domain_name: str,
    feature_name: str,
    subfeature_name: str,
    tools: list[dict],
    t1_matrix_cells: list[dict],
) -> dict[str, str]:
    results = {}
    needs_assessment = []
    
    for tool in tools:
        inherited = _find_inherited_support(
            tool["product_name"],
            subfeature_name,
            feature_name,
            t1_matrix_cells,
        )
        
        if inherited:
            results[tool["product_name"]] = inherited
        else:
            needs_assessment.append(tool)
    
    if not needs_assessment:
        return results
    
    tools_list = "\n".join([
        f"- {t['product_name']} ({t['vendor']}) - {t['tool_type']}"
        for t in needs_assessment
    ])
    
    prompt = MATRIX_BATCH_PROMPT.format(
        domain=domain_name,
        feature_name=feature_name,
        subfeature_name=subfeature_name,
        tools_list=tools_list,
    )
    
    try:
        llm_result = await structured_call(
            prompt,
            MatrixAssessmentResult,
            temperature=0.3
        )
        
        for tool_name, support in llm_result.support_levels.items():
            if support in ("✔", "Partial", "✘"):
                results[tool_name] = support
            else:
                logger.warning(f"Invalid support level '{support}' for {tool_name}, defaulting to Partial")
                results[tool_name] = "Partial"
        
    except Exception as e:
        logger.warning(f"Matrix assessment failed for '{subfeature_name}': {e}")
        for tool in needs_assessment:
            results[tool["product_name"]] = "Partial"
    
    return results


async def populate_domain_matrix(
    domain_id: int,
    domain_name: str,
) -> DomainMatrixBatch:
    logger.info(f"D4: Populating matrix for domain '{domain_name}'")
    
    tools = await get_t2_domain_tools(domain_id)
    subfeatures = await get_t2_domain_subfeatures_by_domain(domain_id)
    t1_cells = await get_all_matrix_cells_for_domain(domain_id)
    
    if not tools:
        logger.warning(f"No tools found for domain '{domain_name}'")
        return DomainMatrixBatch(
            domain_id=domain_id,
            domain_name=domain_name,
            tools_enterprise=[],
            tools_opensource=[],
            rows=[],
        )
    
    if not subfeatures:
        logger.warning(f"No subfeatures found for domain '{domain_name}'")
        return DomainMatrixBatch(
            domain_id=domain_id,
            domain_name=domain_name,
            tools_enterprise=[],
            tools_opensource=[],
            rows=[],
        )
    
    await delete_t2_domain_matrix_cells(domain_id)
    
    tools_enterprise = [t for t in tools if t["tool_type"] == "enterprise"]
    tools_opensource = [t for t in tools if t["tool_type"] == "opensource"]
    
    all_tool_names = [t["product_name"] for t in tools_enterprise] + [t["product_name"] for t in tools_opensource]
    
    rows: list[DomainMatrixCell] = []
    total_assessments = len(subfeatures) * len(tools)
    completed = 0
    
    sf_by_feature: dict[str, list[dict]] = {}
    for sf in subfeatures:
        feat = sf["feature_name"]
        sf_by_feature.setdefault(feat, []).append(sf)
    
    for feature_name, feature_subfeatures in sf_by_feature.items():
        for sf in feature_subfeatures:
            sf_name = sf["name"]
            sf_id = sf["id"]
            
            tool_support: dict[str, str] = {}
            
            batch_size = settings.t2_max_sf_batch_size
            
            for i in range(0, len(tools), batch_size):
                batch_tools = tools[i:i + batch_size]
                
                support_results = await _assess_batch(
                    domain_name=domain_name,
                    feature_name=feature_name,
                    subfeature_name=sf_name,
                    tools=batch_tools,
                    t1_matrix_cells=t1_cells,
                )
                
                for tool_name, support in support_results.items():
                    tool_support[tool_name] = support
                    
                    tool_id = next(
                        (t["id"] for t in tools if t["product_name"] == tool_name),
                        None
                    )
                    
                    if tool_id:
                        await upsert_t2_domain_matrix_cell(
                            domain_id=domain_id,
                            domain_subfeature_id=sf_id,
                            domain_tool_id=tool_id,
                            support_level=support,
                        )
                
                completed += len(batch_tools)
            
            rows.append(DomainMatrixCell(
                subfeature_name=sf_name,
                feature_name=feature_name,
                tool_support=tool_support,
            ))
            
            logger.debug(f"D4: {completed}/{total_assessments} assessments complete")
    
    logger.info(f"D4 Complete: {len(rows)} subfeatures × {len(all_tool_names)} tools = {total_assessments} cells")
    
    return DomainMatrixBatch(
        domain_id=domain_id,
        domain_name=domain_name,
        tools_enterprise=[t["product_name"] for t in tools_enterprise],
        tools_opensource=[t["product_name"] for t in tools_opensource],
        rows=rows,
    )
