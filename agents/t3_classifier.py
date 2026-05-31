"""
agents/t3_classifier.py — Technique 3 cross-domain tool classification agent.

Two-stage pipeline:
  Stage 1 (Dedup)  — SQL-only. Reads T1 tools, deduplicates by (vendor, product_name),
                     builds t3_tools + t3_tool_subdomains. No LLM.
  Stage 2 (NIST)   — LLM-batched. Classifies each tool against NIST CSF 2.0 functions.
                     Uses rule-based seed fallback when t3_enable_nist_llm=False.
"""

import asyncio
import json
import logging
from typing import Any

from pydantic import BaseModel

from config.settings import settings
from db.t3_store import (
    get_all_unique_tools_from_t1,
    link_t3_tool_subdomains_bulk,
    update_t3_tool_nist,
    bulk_update_t3_tool_nist,
    upsert_t3_tool,
)
from db.store import db as _db
from llm.bedrock import structured_call

logger = logging.getLogger(__name__)


# ── NIST CSF 2.0 constants ─────────────────────────────────────────────────────

NIST_FUNCTIONS = ("ID", "PR", "DE", "RS", "RC", "GV")

NIST_LABELS = {
    "ID": "Identify",
    "PR": "Protect",
    "DE": "Detect",
    "RS": "Respond",
    "RC": "Recover",
    "GV": "Govern",
}

# Rule-based domain → NIST function seed.
# Tools are assigned these functions based on their domain memberships.
# The LLM may override or extend these.
_DOMAIN_NIST_MAP: dict[str, list[str]] = {
    "Network Security":                  ["ID", "PR", "DE"],
    "Application Security":              ["ID", "PR", "DE"],
    "Cloud Security":                    ["ID", "PR", "DE", "GV"],
    "Endpoint Security":                 ["PR", "DE", "RS"],
    "DevSecOps":                         ["ID", "PR", "GV"],
    "Identity & Access Management":      ["PR", "GV"],
    "Governance, Risk & Compliance (GRC)": ["ID", "GV"],
    "Security Operations (SOC)":         ["DE", "RS"],
    "Threat Intelligence":               ["ID", "DE"],
    "Malware Analysis":                  ["DE", "RS"],
    "Incident Response":                 ["RS", "RC"],
    "OT/ICS Security":                   ["ID", "PR", "DE", "RS"],
    "Mobile Security":                   ["PR", "DE"],
    "AI Security":                       ["ID", "PR", "GV"],
    "Cryptography":                      ["PR"],
    "Information Security (InfoSec)":    ["ID", "PR", "GV"],
    "Cyber Defense / Defensive Security": ["PR", "DE", "RS"],
    "Digital Forensics / Cyber Forensics": ["DE", "RS"],
    "Offensive Security / Ethical Hacking": ["ID", "DE"],
}

# Priority order for picking the "primary" function from a set
_NIST_PRIORITY = ["DE", "RS", "PR", "ID", "RC", "GV"]


# ── Pydantic output model for LLM calls ───────────────────────────────────────

class NistToolClassification(BaseModel):
    product_name: str
    vendor: str
    nist_functions: list[str]   # subset of NIST_FUNCTIONS
    nist_primary: str           # single value from NIST_FUNCTIONS


class NistBatchResult(BaseModel):
    tools: list[NistToolClassification]


# ── Rule-based NIST seed (no LLM) ─────────────────────────────────────────────

def _rule_based_nist(domain_names: list[str]) -> tuple[list[str], str]:
    """
    Given a list of domain names a tool appears in, compute NIST functions
    via the hardcoded domain→NIST map.  Returns (functions_list, primary).
    """
    func_set: set[str] = set()
    unmatched_domains: list[str] = []
    
    for domain in domain_names:
        funcs = _DOMAIN_NIST_MAP.get(domain, [])
        if funcs:
            func_set.update(funcs)
        else:
            unmatched_domains.append(domain)
    
    if unmatched_domains:
        logger.warning(f"No NIST mapping for domains: {unmatched_domains}")

    if not func_set:
        func_set = {"ID"}

    # Sort by canonical NIST order
    sorted_funcs = [f for f in NIST_FUNCTIONS if f in func_set]

    # Pick primary by priority
    primary = next((f for f in _NIST_PRIORITY if f in func_set), sorted_funcs[0])
    return sorted_funcs, primary


# ── LLM-based NIST classification ─────────────────────────────────────────────

_NIST_PROMPT = """\
You are a cybersecurity classification expert using NIST Cybersecurity Framework 2.0.

Classify each tool below by assigning NIST CSF 2.0 functions it supports:
  ID = Identify  |  PR = Protect  |  DE = Detect
  RS = Respond   |  RC = Recover  |  GV = Govern

For each tool, consider its known domain/subdomain context provided.
Assign ALL applicable functions (a tool may support 1-6).
Choose ONE primary function that best represents its core purpose.

Tools to classify:
{tool_list}

Return valid JSON with this exact structure:
{{
  "tools": [
    {{
      "product_name": "<exact product_name>",
      "vendor": "<exact vendor>",
      "nist_functions": ["ID", "PR"],
      "nist_primary": "PR"
    }},
    ...
  ]
}}

Rules:
- nist_functions must only contain values from: ID, PR, DE, RS, RC, GV
- nist_primary must be one value from nist_functions
- Return exactly the same number of tools as provided, in the same order
"""


def _build_tool_list_text(tools: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, tool in enumerate(tools, 1):
        domain_names = list({m[3] for m in tool.get("memberships", [])})
        subdomain_names = [m[1] for m in tool.get("memberships", [])][:6]  # cap for prompt length
        context = f"Domains: {', '.join(domain_names[:4])}"
        if subdomain_names:
            context += f" | Subdomains: {', '.join(subdomain_names[:4])}"
        lines.append(
            f"{i}. {tool['product_name']} (vendor: {tool['vendor']}, type: {tool['tool_type']})\n"
            f"   Context — {context}"
        )
    return "\n".join(lines)


async def _classify_batch_llm(
    batch: list[dict[str, Any]],
    semaphore: asyncio.Semaphore,
) -> list[NistToolClassification]:
    """Call LLM to classify a batch of tools. Falls back to rule-based on error."""
    tool_list_text = _build_tool_list_text(batch)
    prompt = _NIST_PROMPT.format(tool_list=tool_list_text)

    async with semaphore:
        try:
            result = await structured_call(prompt, NistBatchResult, temperature=0.2)
            # Validate we got back the right count
            if len(result.tools) != len(batch):
                logger.warning(
                    f"NIST batch size mismatch: sent {len(batch)}, got {len(result.tools)} — using rule-based fallback"
                )
                raise ValueError("batch size mismatch")

            # Sanitize: ensure all returned functions are valid
            cleaned: list[NistToolClassification] = []
            for item in result.tools:
                valid_funcs = [f for f in item.nist_functions if f in NIST_FUNCTIONS]
                if not valid_funcs:
                    valid_funcs = ["ID"]
                primary = item.nist_primary if item.nist_primary in valid_funcs else valid_funcs[0]
                cleaned.append(NistToolClassification(
                    product_name=item.product_name,
                    vendor=item.vendor,
                    nist_functions=valid_funcs,
                    nist_primary=primary,
                ))
            return cleaned

        except Exception as e:
            logger.warning(f"LLM NIST classification batch failed ({e}), using rule-based fallback")
            # Fall through to rule-based for this batch
            fallback: list[NistToolClassification] = []
            for tool in batch:
                domain_names = list({m[3] for m in tool.get("memberships", [])})
                funcs, primary = _rule_based_nist(domain_names)
                fallback.append(NistToolClassification(
                    product_name=tool["product_name"],
                    vendor=tool["vendor"],
                    nist_functions=funcs,
                    nist_primary=primary,
                ))
            return fallback


# ── Stage 1: Deduplication ─────────────────────────────────────────────────────

_DEDUP_BATCH_COMMIT = 200  # commit every N tools to balance memory vs. disk flushes


async def run_dedup_stage(
    tools: list[dict[str, Any]],
    on_progress: Any = None,
) -> dict[tuple[str, str], int]:
    """
    Upsert all unique tools from T1 into t3_tools and link their subdomain memberships.
    Uses batched transactions (commit every 200 tools) instead of per-row commits.
    Returns a dict mapping (vendor, product_name) -> t3_tool_id.
    """
    logger.info(f"T3 Stage 1: deduplicating {len(tools)} unique tools from T1")
    tool_id_map: dict[tuple[str, str], int] = {}
    conn = await _db._get_conn()

    for i, tool in enumerate(tools):
        # Upsert tool row (no individual commit)
        await upsert_t3_tool(
            vendor=tool["vendor"],
            product_name=tool["product_name"],
            tool_type=tool["tool_type"],
            _commit=False,
        )

        # Commit in batches to avoid holding a massive in-memory transaction
        if (i + 1) % _DEDUP_BATCH_COMMIT == 0:
            await conn.commit()

    # Final commit for remaining rows
    await conn.commit()

    # Fetch all inserted IDs in one query
    rows = await _db.fetchall(
        "SELECT id, vendor, product_name FROM t3_tools"
    )
    id_lookup = {(r["vendor"], r["product_name"]): r["id"] for r in rows}

    # Bulk-insert all memberships (one executemany per tool, single transaction)
    for tool in tools:
        t3_id = id_lookup.get((tool["vendor"], tool["product_name"]))
        if t3_id:
            tool_id_map[(tool["vendor"], tool["product_name"])] = t3_id
            memberships = [(sd_id, d_id) for sd_id, _, d_id, _ in tool["memberships"]]
            await link_t3_tool_subdomains_bulk(t3_id, memberships, _commit=False)

    await conn.commit()  # commit all memberships at once

    # Recompute domain_count + subdomain_count for all tools in one SQL statement
    await _db.execute(
        """UPDATE t3_tools SET
               subdomain_count = (
                   SELECT COUNT(*) FROM t3_tool_subdomains WHERE t3_tool_id = t3_tools.id
               ),
               domain_count = (
                   SELECT COUNT(DISTINCT domain_id) FROM t3_tool_subdomains WHERE t3_tool_id = t3_tools.id
               )"""
    )
    await conn.commit()

    if on_progress:
        # Stage 1 takes 30% of total progress (deduplication)
        await on_progress(0.30, f"Deduplication done — {len(tool_id_map)} unique tools")

    logger.info(f"T3 Stage 1 complete: {len(tool_id_map)} canonical tools written")
    return tool_id_map


# ── Stage 2: NIST classification ──────────────────────────────────────────────

async def run_nist_stage(
    tools: list[dict[str, Any]],
    tool_id_map: dict[tuple[str, str], int],
    on_progress: Any = None,
) -> None:
    """
    Classify all tools with NIST CSF 2.0 functions.
    Uses LLM in mini-batches (t3_nist_batch_size) if t3_enable_nist_llm=True,
    otherwise falls back to pure rule-based mapping.
    """
    total = len(tools)
    logger.info(f"T3 Stage 2: classifying {total} tools for NIST CSF 2.0")

    if not settings.t3_enable_nist_llm:
        # Pure rule-based — no LLM
        for i, tool in enumerate(tools):
            domain_names = list({m[3] for m in tool.get("memberships", [])})
            funcs, primary = _rule_based_nist(domain_names)
            t3_id = tool_id_map.get((tool["vendor"], tool["product_name"]))
            if t3_id:
                await update_t3_tool_nist(t3_id, funcs, primary)
            if on_progress and i % 50 == 0:
                # NIST classification takes 65% of progress (30-95%), first 30% was Stage 1
                pct = 0.30 + (i + 1) / max(total, 1) * 0.65
                await on_progress(pct, f"Rule-based NIST classification... ({i+1}/{total})")
        logger.info("T3 Stage 2 complete (rule-based)")
        return

    # LLM batched classification
    batch_size = settings.t3_nist_batch_size
    batches = [tools[i:i + batch_size] for i in range(0, len(tools), batch_size)]
    semaphore = asyncio.Semaphore(settings.llm_concurrency)

    classified = 0
    for batch_idx, batch in enumerate(batches):
        results = await _classify_batch_llm(batch, semaphore)

        # Collect all NIST updates for this batch and write in one transaction
        nist_updates: list[tuple[int, list[str], str]] = []
        for tool, classification in zip(batch, results):
            t3_id = tool_id_map.get((tool["vendor"], tool["product_name"]))
            if t3_id:
                nist_updates.append((t3_id, classification.nist_functions, classification.nist_primary))

        if nist_updates:
            await bulk_update_t3_tool_nist(nist_updates)

        classified += len(batch)

        if on_progress:
            pct = 0.30 + classified / max(total, 1) * 0.65
            await on_progress(pct, f"NIST classification... ({classified}/{total} tools)")

    logger.info(f"T3 Stage 2 complete (LLM): {classified} tools classified")


# ── Public entry point ─────────────────────────────────────────────────────────

async def run_t3_classification(
    on_progress: Any = None,
    reset_existing: bool = False,
) -> int:
    """
    Full T3 classification pipeline.

    Args:
        on_progress: async callable(pct: float, message: str) for live progress
        reset_existing: if True, wipe existing T3 data before running

    Returns:
        Number of unique tools classified.
    """
    if reset_existing:
        from db.t3_store import reset_t3_data
        await reset_t3_data()
        logger.info("T3 data reset before re-classification")

    if on_progress:
        await on_progress(0.05, "Loading tools from T1 database...")

    # Gather unique tools from completed T1 subdomains
    tools = await get_all_unique_tools_from_t1()

    if not tools:
        logger.warning("T3 classification: no T1 tools found (run T1 pipelines first)")
        return 0

    logger.info(f"T3 classification starting: {len(tools)} unique tools across all subdomains")

    if on_progress:
        await on_progress(0.10, f"Found {len(tools)} unique tools — starting deduplication...")

    # Stage 1
    tool_id_map = await run_dedup_stage(tools, on_progress=on_progress)

    if on_progress:
        await on_progress(0.30, f"Deduplication done — starting NIST classification...")

    # Stage 2
    await run_nist_stage(tools, tool_id_map, on_progress=on_progress)

    if on_progress:
        await on_progress(1.0, f"Complete — {len(tool_id_map)} tools classified")

    return len(tool_id_map)

async def generate_t3_executive_summary(stats: dict) -> str:
    """Generate an LLM-based strategic executive summary from T3 pipeline stats."""
    from llm.bedrock import simple_call
    prompt = f"""
You are an expert Cybersecurity Analyst. Analyze the following tool classification statistics and provide a concise, 2-3 paragraph strategic executive summary. Identify coverage gaps, strengths, and dominant vendors.

Data:
- Total Tools: {stats.get('total')}
- Multi-Domain Platforms: {stats.get('multi_domain')}
- Open Source Tools: {stats.get('opensource')}
- NIST Function Distribution: {stats.get('nist_counts')}
- Top Vendor: {stats.get('top_tool', {}).get('vendor')} with {stats.get('top_tool', {}).get('domain_count')} domains covered.

Keep the tone professional, objective, and boardroom-ready. Avoid using bolding or complex markdown, just plain text paragraphs.
"""
    summary = await simple_call(prompt=prompt, temperature=0.5, max_tokens=1000)
    return summary.strip()

