"""
Matrix Population — fully scalable

Architecture:
  Feature loop  (parallel, semaphore-capped)
    └─ Sub-feature mini-batch loop  (serial within each feature, parallel across features)
         └─ One LLM call per mini-batch of ≤ max_sf_batch_size sub-features

Why mini-batching beats chunking by feature alone:
  If a feature has 12 sub-features and there are 15 tools, one feature-chunk
  call would need 180 cells in its JSON — that can still overflow 8192 tokens
  when tool names are long. By further splitting the sub-features into mini-
  batches of max_sf_batch_size (default 8), every single LLM call is bounded to:
      max_sf_batch_size × total_tools × ~40 chars ≈ 8 × 15 × 40 = 4800 chars
  which safely fits in a 4096-token response even with verbose tool names.

The results from all mini-batches for a feature are merged before DB writes,
so the DB schema and Excel exporter are unchanged.
"""

import asyncio
import logging
from typing import Any, Callable, Coroutine
from pydantic import BaseModel

from models.matrix import MatrixBatch, ToolSupportRow
from llm.bedrock import structured_call
from db.store import get_tools, get_features, get_subfeatures, upsert_matrix_cell
from config.settings import settings

logger = logging.getLogger(__name__)


# ── Prompt ───────────────────────────────────────────────────────────────────

MINI_BATCH_PROMPT = """\
You are a cybersecurity tools expert. Evaluate each tool's support for the
sub-features listed below.

Subdomain : {subdomain}
Feature   : {feature}
Batch     : sub-features {batch_start}–{batch_end} of {total_sfs}

Enterprise Tools : {enterprise_tools}
Open-Source Tools: {opensource_tools}

Sub-features in THIS batch only:
{subfeatures_text}

Support levels:
  "✔"       = Fully supported natively
  "✘"       = Not supported
  "Partial" = Limited / add-on / custom config required

Rules:
- Be accurate and conservative. Uncertain → "Partial".
- Every tool MUST appear for EVERY sub-feature in this batch.
- Return ONLY valid JSON — no prose, no markdown fences.

JSON structure (rows = one per sub-feature in this batch):
{{
    "subdomain": "{subdomain}",
    "tools_enterprise": {enterprise_tools_json},
    "tools_opensource": {opensource_tools_json},
    "rows": [
        {{
            "subdomain": "{subdomain}",
            "feature": "{feature}",
            "sub_feature": "<exact sub-feature name>",
            "tool_support": {{
                "<tool name>": "✔",
                "<another>":   "✘"
            }}
        }}
    ]
}}
"""


def _make_fallback_rows(
    subdomain: str,
    feature_name: str,
    subfeatures: list[dict],
    tool_names: list[str],
) -> list[ToolSupportRow]:
    return [
        ToolSupportRow(
            subdomain=subdomain,
            feature=feature_name,
            sub_feature=sf["name"],
            tool_support={n: "✘" for n in tool_names},
        )
        for sf in subfeatures
    ]


def _chunk(lst: list, size: int) -> list[list]:
    """Split a list into chunks of at most `size` items."""
    return [lst[i : i + size] for i in range(0, len(lst), size)]


async def _call_mini_batch(
    subdomain_name: str,
    feature_name: str,
    sf_batch: list[dict],
    enterprise_tools: list[dict],
    opensource_tools: list[dict],
    llm_sem: asyncio.Semaphore,
    batch_start: int,
    total_sfs: int,
) -> list[ToolSupportRow]:
    """
    Single LLM call covering one mini-batch of sub-features.
    Returns rows; never raises — falls back to ✘ on any failure.
    """
    enterprise_names = [t["product_name"] for t in enterprise_tools]
    opensource_names = [t["product_name"] for t in opensource_tools]
    all_tool_names   = enterprise_names + opensource_names
    subfeatures_text = "\n".join(f"  - {sf['name']}" for sf in sf_batch)
    batch_end        = batch_start + len(sf_batch) - 1

    prompt = MINI_BATCH_PROMPT.format(
        subdomain=subdomain_name,
        feature=feature_name,
        batch_start=batch_start,
        batch_end=batch_end,
        total_sfs=total_sfs,
        enterprise_tools=", ".join(enterprise_names),
        opensource_tools=", ".join(opensource_names),
        subfeatures_text=subfeatures_text,
        enterprise_tools_json=str(enterprise_names),
        opensource_tools_json=str(opensource_names),
    )

    # cells = batch_size × tools; generous token budget per cell
    num_cells  = len(sf_batch) * len(all_tool_names)
    max_tokens = max(2048, min(8192, num_cells * 45 + 512))

    try:
        async with llm_sem:
            result = await structured_call(
                prompt, MatrixBatch, temperature=0.2, max_tokens=max_tokens
            )
        return result.rows
    except Exception as e:
        logger.warning(
            f"  Mini-batch failed for '{feature_name}' "
            f"sfs {batch_start}–{batch_end}: {e} — filling with ✘"
        )
        return _make_fallback_rows(
            subdomain_name, feature_name, sf_batch, all_tool_names
        )


async def _process_feature(
    subdomain_id: int,
    subdomain_name: str,
    feature: dict,
    subfeatures: list[dict],
    tools: list[dict],
    enterprise_tools: list[dict],
    opensource_tools: list[dict],
    llm_sem: asyncio.Semaphore,
    progress_cb: Callable[[str, int, int], Coroutine] | None,
    feature_idx: int,
    total_features: int,
) -> list[ToolSupportRow]:
    """
    Process ONE feature: split its sub-features into mini-batches,
    call each mini-batch serially (they share the same context), persist
    results immediately, then fire the progress callback.
    """
    batch_size = settings.max_sf_batch_size
    sf_batches = _chunk(subfeatures, batch_size)

    logger.info(
        f"  Feature [{feature_idx+1}/{total_features}] '{feature['name']}' "
        f"— {len(subfeatures)} sfs in {len(sf_batches)} mini-batch(es) "
        f"× {len(enterprise_tools)+len(opensource_tools)} tools"
    )

    all_rows: list[ToolSupportRow] = []
    sf_offset = 1
    for batch in sf_batches:
        rows = await _call_mini_batch(
            subdomain_name=subdomain_name,
            feature_name=feature["name"],
            sf_batch=batch,
            enterprise_tools=enterprise_tools,
            opensource_tools=opensource_tools,
            llm_sem=llm_sem,
            batch_start=sf_offset,
            total_sfs=len(subfeatures),
        )
        all_rows.extend(rows)
        sf_offset += len(batch)

    # ── Persist all rows for this feature to DB immediately ──────────────────
    persisted = 0
    all_tool_names = (
        [t["product_name"] for t in enterprise_tools]
        + [t["product_name"] for t in opensource_tools]
    )
    for row in all_rows:
        sf_rec = next(
            (sf for sf in subfeatures if sf["name"] == row.sub_feature), None
        )
        if not sf_rec:
            logger.debug(f"  Sub-feature not matched: '{row.sub_feature}'")
            continue
        for tool_name, support_level in row.tool_support.items():
            tool_rec = next(
                (t for t in tools if t["product_name"] == tool_name), None
            )
            if not tool_rec:
                continue
            await upsert_matrix_cell(
                subdomain_id, sf_rec["id"], tool_rec["id"], support_level
            )
            persisted += 1

    logger.info(
        f"  ✓ '{feature['name']}': {persisted} cells saved "
        f"({len(sf_batches)} batch(es))"
    )

    if progress_cb:
        await progress_cb(feature["name"], feature_idx + 1, total_features)

    return all_rows


async def populate_matrix(
    subdomain_id: int,
    subdomain_name: str,
    progress_cb: Callable[[str, int, int], Coroutine] | None = None,
) -> MatrixBatch:
    logger.info(f"Populating matrix for '{subdomain_name}'")

    tools            = await get_tools(subdomain_id)
    enterprise_tools = [t for t in tools if t["tool_type"] == "enterprise"]
    opensource_tools = [t for t in tools if t["tool_type"] == "opensource"]
    enterprise_names = [t["product_name"] for t in enterprise_tools]
    opensource_names = [t["product_name"] for t in opensource_tools]

    features = await get_features(subdomain_id)
    subfeatures_map: dict[int, list[dict]] = {}
    for f in features:
        sfs = await get_subfeatures(f["id"])
        subfeatures_map[f["id"]] = sfs

    total_tools = len(enterprise_tools) + len(opensource_tools)
    total_sfs   = sum(len(v) for v in subfeatures_map.values())
    total_cells = total_tools * total_sfs

    logger.info(
        f"Matrix dimensions: {len(features)} features, "
        f"{total_sfs} sub-features, {total_tools} tools "
        f"= {total_cells} cells"
    )

    # Semaphore caps concurrent LLM mini-batch calls globally
    llm_sem = asyncio.Semaphore(settings.llm_concurrency)

    valid_features = [f for f in features if subfeatures_map.get(f["id"])]
    skipped = len(features) - len(valid_features)
    if skipped:
        logger.warning(f"{skipped} features had no sub-features and were skipped")

    total = len(valid_features)

    # ALL features run concurrently; within each feature the mini-batches are
    # serial to preserve context coherence. The semaphore prevents global
    # Bedrock overload.
    chunk_results: list[list[ToolSupportRow]] = await asyncio.gather(
        *[
            _process_feature(
                subdomain_id=subdomain_id,
                subdomain_name=subdomain_name,
                feature=feature,
                subfeatures=subfeatures_map[feature["id"]],
                tools=tools,
                enterprise_tools=enterprise_tools,
                opensource_tools=opensource_tools,
                llm_sem=llm_sem,
                progress_cb=progress_cb,
                feature_idx=idx,
                total_features=total,
            )
            for idx, feature in enumerate(valid_features)
        ]
    )

    all_rows = [row for chunk in chunk_results for row in chunk]

    logger.info(
        f"Matrix population complete for '{subdomain_name}': "
        f"{len(all_rows)} rows across {total} features"
    )

    return MatrixBatch(
        subdomain=subdomain_name,
        tools_enterprise=enterprise_names,
        tools_opensource=opensource_names,
        rows=all_rows,
    )


class MatrixPopulationOutput(BaseModel):
    batch: MatrixBatch


async def run_matrix_population(
    subdomain_id: int,
    subdomain_name: str,
    progress_cb: Callable[[str, int, int], Coroutine] | None = None,
) -> MatrixPopulationOutput:
    batch = await populate_matrix(subdomain_id, subdomain_name, progress_cb=progress_cb)
    return MatrixPopulationOutput(batch=batch)
