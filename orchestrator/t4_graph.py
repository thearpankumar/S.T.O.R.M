"""
orchestrator/t4_graph.py - Technique 4 pipeline orchestrator.

Executes T4 analysis pipeline:
  S1 (0-5%)   - Bootstrap: deduplicate T1 tools, link subdomains
  S2 (5-35%)  - Web enrichment: license + URL + description (LLM)
  S3 (35-60%) - Feature aggregation: calculate support metrics
  S4 (60-80%) - Domain mapping: aggregate domain coverage
  S5 (80-100%)- Excel generation
"""

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any

from db.store import db
from db.t4_store import (
    upsert_t4_tool,
    link_t4_tool_subdomains_bulk,
    link_t4_tool_subdomains_bulk_batch,
    upsert_t4_run_status,
    update_t4_tool_features,
    update_t4_tool_subdomain_features,
    update_t4_tool_domains,
    get_enrichment_progress,
)
from models.worker import WorkerEvent

logger = logging.getLogger(__name__)


def _step_to_event_type(step: str) -> str:
    """Map a step name to WorkerEvent event_type."""
    if step == "done":
        return "completed"
    if step == "failed":
        return "failed"
    return "progress"


async def run_s1_bootstrap(on_progress: Any = None) -> int:
    """
    Stage S1: Bootstrap T4 from existing T1 data.
    - Deduplicate T1 tools by (vendor, product_name)
    - Create stubs in t4_tools using bulk insert
    - Link all subdomain memberships using bulk insert
    Returns number of unique canonical tools.
    """
    from db.t4_store import bulk_upsert_t4_tools
    
    logger.info("T4 S1: Starting bootstrap from T1 tools")
    
    rows = await db.fetchall(
        """SELECT 
               t.id AS t1_tool_id,
               t.vendor,
               t.product_name,
               t.tool_type,
               t.subdomain_id,
               sd.domain_id,
               d.name AS domain_name,
               sd.name AS subdomain_name
           FROM tools t
           JOIN subdomains sd ON sd.id = t.subdomain_id
           JOIN domains d ON d.id = sd.domain_id
           WHERE sd.status = 'done'
           ORDER BY t.vendor, t.product_name"""
    )
    
    if not rows:
        logger.warning("T4 S1: No T1 tools found (complete T1 pipelines first)")
        return 0
    
    canonical_map: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        key = (row["vendor"], row["product_name"])
        canonical_map[key].append(dict(row))
    
    logger.info(f"T4 S1: Found {len(rows)} T1 tool entries -> {len(canonical_map)} unique tools")
    
    tool_inserts: list[tuple] = []
    for (vendor, product_name), appearances in canonical_map.items():
        tool_type = next(
            (a["tool_type"] for a in appearances if a.get("tool_type")),
            "unknown"
        )
        tool_inserts.append((vendor, product_name, tool_type))
    
    if tool_inserts:
        await bulk_upsert_t4_tools(tool_inserts)
    
    tool_rows = await db.fetchall(
        "SELECT id, vendor, product_name FROM t4_tools"
    )
    id_lookup = {(r["vendor"], r["product_name"]): r["id"] for r in tool_rows}
    
    subdomain_links: list[tuple] = []
    for (vendor, product_name), appearances in canonical_map.items():
        t4_id = id_lookup.get((vendor, product_name))
        if t4_id is None:
            logger.warning(f"T4 tool ID not found for {vendor} {product_name}")
            continue
        
        for a in appearances:
            subdomain_links.append((t4_id, a["subdomain_id"], a["domain_id"], a["t1_tool_id"]))
    
    if subdomain_links:
        await link_t4_tool_subdomains_bulk_batch(subdomain_links)
    
    if on_progress:
        await on_progress(0.05, f"Bootstrapped {len(canonical_map)} canonical tools from T1")
    
    logger.info(f"T4 S1 complete: {len(canonical_map)} canonical tools, {len(rows)} memberships")
    return len(canonical_map)


async def run_s3_feature_aggregation(on_progress: Any = None) -> int:
    """
    Stage S3: Aggregate feature support per canonical tool.
    - JOIN matrix_cells with t4_tool_subdomains
    - Calculate total, supported, partial, unsupported counts
    - Populate t4_tool_features and t4_tool_subdomain_features using bulk operations
    Returns number of tools with feature data.
    """
    from db.t4_store import (
        bulk_update_t4_tool_subdomain_features,
        bulk_update_t4_tool_features,
    )
    
    logger.info("T4 S3: Starting feature aggregation from matrix_cells")
    
    rows = await db.fetchall(
        """SELECT 
               tts.t4_tool_id,
               tts.subdomain_id,
               tts.domain_id,
               COUNT(*) AS total_subfeatures,
               SUM(CASE WHEN mc.support_level = '✔' THEN 1 ELSE 0 END) AS supported_subfeatures,
               SUM(CASE WHEN mc.support_level = 'Partial' THEN 1 ELSE 0 END) AS partial_subfeatures,
               SUM(CASE WHEN mc.support_level = '✘' THEN 1 ELSE 0 END) AS unsupported_subfeatures
           FROM t4_tool_subdomains tts
           JOIN tools t ON t.id = tts.t1_tool_id
           JOIN matrix_cells mc ON mc.tool_id = t.id
           GROUP BY tts.t4_tool_id, tts.subdomain_id"""
    )
    
    if not rows:
        logger.warning("T4 S3: No matrix_cells data found (T1 subdomains may not be complete)")
        return 0
    
    tool_aggregates: dict[int, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "supported": 0, "partial": 0, "unsupported": 0}
    )
    
    subdomain_updates: list[tuple] = []
    
    for row in rows:
        t4_tool_id = row["t4_tool_id"]
        subdomain_id = row["subdomain_id"]
        domain_id = row["domain_id"]
        total = row["total_subfeatures"]
        supported = row["supported_subfeatures"]
        partial = row["partial_subfeatures"]
        unsupported = row["unsupported_subfeatures"]
        
        tool_aggregates[t4_tool_id]["total"] += total
        tool_aggregates[t4_tool_id]["supported"] += supported
        tool_aggregates[t4_tool_id]["partial"] += partial
        tool_aggregates[t4_tool_id]["unsupported"] += unsupported
        
        if total > 0:
            # Partial counts as 0.5 credit — a tool that 'partially' supports a subfeature
            # is not zero, so weighting it gives a more accurate coverage picture.
            support_pct = round(((supported + 0.5 * partial) / total * 100.0), 1)
        else:
            support_pct = 0.0
        support_level = "High" if support_pct >= 70.0 else ("Medium" if support_pct >= 40.0 else "Low")
        
        subdomain_updates.append((
            t4_tool_id, subdomain_id, domain_id, total,
            supported, partial, support_pct, support_level
        ))
    
    feature_updates: list[tuple] = []
    for t4_tool_id, agg in tool_aggregates.items():
        total = agg["total"]
        supported = agg["supported"]
        partial = agg["partial"]
        unsupported = agg["unsupported"]
        # Partial counts as 0.5 credit (matches per-subdomain formula above)
        support_rate = ((supported + 0.5 * partial) / total) if total > 0 else 0.0
        
        feature_updates.append((
            t4_tool_id, total, supported, partial, unsupported, round(support_rate, 3)
        ))
    
    if subdomain_updates:
        await bulk_update_t4_tool_subdomain_features(subdomain_updates)
    
    if feature_updates:
        await bulk_update_t4_tool_features(feature_updates)
    
    if on_progress:
        await on_progress(0.60, f"Aggregated features for {len(tool_aggregates)} tools")
    
    logger.info(f"T4 S3 complete: {len(tool_aggregates)} tools with feature data, {len(subdomain_updates)} subdomain records")
    return len(tool_aggregates)


async def run_s4_domain_mapping(on_progress: Any = None) -> int:
    """
    Stage S4: Aggregate domain/subdomain counts per tool.
    - Identify primary domain (most appearances)
    - Populate t4_tool_domains with counts and domain list using bulk operations
    Returns number of tools mapped.
    """
    from db.t4_store import bulk_update_t4_tool_domains
    
    logger.info("T4 S4: Starting domain mapping")
    
    rows = await db.fetchall(
        """SELECT 
               tts.t4_tool_id,
               tts.domain_id,
               tts.subdomain_id,
               d.name AS domain_name
           FROM t4_tool_subdomains tts
           JOIN domains d ON d.id = tts.domain_id"""
    )
    
    if not rows:
        logger.warning("T4 S4: No domain memberships found")
        return 0
    
    tool_domains: dict[int, dict] = defaultdict(
        lambda: {"domains": {}, "subdomain_ids": set(), "domain_ids": set(), "primary_domain_id": None, "primary_subdomain_count": 0}
    )
    
    domain_subdomain_counts: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    
    for row in rows:
        t4_tool_id = row["t4_tool_id"]
        domain_id = row["domain_id"]
        subdomain_id = row["subdomain_id"]
        domain_name = row["domain_name"]
        
        tool_domains[t4_tool_id]["domains"][domain_name] = domain_id
        tool_domains[t4_tool_id]["subdomain_ids"].add(subdomain_id)
        tool_domains[t4_tool_id]["domain_ids"].add(domain_id)
        domain_subdomain_counts[t4_tool_id][domain_id] += 1
    
    for t4_tool_id, counts in domain_subdomain_counts.items():
        primary_domain = max(counts.items(), key=lambda x: x[1])
        tool_domains[t4_tool_id]["primary_domain_id"] = primary_domain[0]
        tool_domains[t4_tool_id]["primary_subdomain_count"] = primary_domain[1]
    
    domain_updates: list[tuple] = []
    for t4_tool_id, data in tool_domains.items():
        domain_count = len(data["domains"])
        subdomain_count = len(data["subdomain_ids"])
        primary_domain_id = data["primary_domain_id"]
        domain_list = sorted(data["domains"].keys())
        domain_list_json = json.dumps(domain_list)
        
        domain_updates.append((
            t4_tool_id, primary_domain_id, domain_count, subdomain_count, domain_list_json
        ))
    
    if domain_updates:
        await bulk_update_t4_tool_domains(domain_updates)
    
    if on_progress:
        await on_progress(0.80, f"Mapped {len(tool_domains)} tools to domains")
    
    logger.info(f"T4 S4 complete: {len(tool_domains)} tools domain-mapped")
    return len(tool_domains)


async def run_t4_pipeline(
    event_queue: asyncio.Queue[WorkerEvent] | None = None,
    reset_existing: bool = False,
    skip_enrichment: bool = False,
) -> bool:
    """
    Execute the full T4 analysis pipeline.
    
    Stages:
      S1 (0-5%)   — Bootstrap from T1
      S2 (5-35%)  — Web enrichment (license detection)
      S3 (35-60%) — Feature aggregation
      S4 (60-80%) — Domain mapping
      S5 (80-100%)— Excel generation
    
    Returns True on success.
    """
    def emit_sync(step: str, message: str, progress_pct: float) -> None:
        if event_queue is None:
            return
        event = WorkerEvent(
            subdomain="t4_analysis",
            event_type=_step_to_event_type(step),
            step=step,
            message=message,
            progress_pct=progress_pct,
        )
        try:
            event_queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Event queue full - progress event dropped")
        except Exception as e:
            logger.debug(f"Failed to emit event: {e}")
    
    async def emit(step: str, message: str, progress_pct: float) -> None:
        logger.info(f"[T4:{step}] {message} ({int(progress_pct * 100)}%)")
        emit_sync(step, message, progress_pct)
    
    def make_progress_callback(step: str):
        async def callback(progress_pct: float, message: str) -> None:
            await emit(step, message, progress_pct)
        return callback
    
    logger.info("Starting T4 analysis pipeline")
    await upsert_t4_run_status("running", total_tools=0, processed_tools=0)
    
    try:
        if reset_existing:
            from db.t4_store import reset_t4_data
            await reset_t4_data()
            logger.info("T4 data reset")
        
        await emit("s1", "Bootstrapping from T1 tools...", 0.05)
        canonical_count = await run_s1_bootstrap(on_progress=make_progress_callback("s1"))
        
        if canonical_count == 0:
            logger.warning("T4 pipeline: No tools found - complete T1 pipelines first")
            await upsert_t4_run_status("failed", total_tools=0, processed_tools=0)
            await emit("failed", "No T1 tools found. Run T1 pipelines first.", 0.0)
            return False
        
        await emit("s1", f"Found {canonical_count} canonical tools", 0.05)
        
        progress = await get_enrichment_progress()
        pending_enrichment = progress.get("pending", 0)
        
        enriched_count = 0
        if not skip_enrichment and pending_enrichment > 0:
            await emit("s2", f"Enriching {pending_enrichment} tools...", 0.10)
            from agents.t4_enricher import run_s2_enrichment
            enriched_count = await run_s2_enrichment(on_progress=make_progress_callback("s2"))
            await emit("s2", f"Enriched {enriched_count} tools with license info", 0.35)
        else:
            # Already enriched — count tools that have license_model set
            progress_snap = await get_enrichment_progress()
            enriched_count = progress_snap.get("enriched", 0)
            await emit("s2", "Skipping enrichment (already complete or disabled)", 0.35)
        
        await emit("s3", "Aggregating feature support...", 0.40)
        feature_count = await run_s3_feature_aggregation(on_progress=make_progress_callback("s3"))
        
        await emit("s4", "Mapping domain coverage...", 0.65)
        domain_count = await run_s4_domain_mapping(on_progress=make_progress_callback("s4"))
        
        await emit("s5", "Generating Excel workbook...", 0.85)
        try:
            from excel.t4_bridge import export_t4_workbook
            output_path = await export_t4_workbook()
            await emit("s5", f"Excel saved: {output_path}", 0.95)
        except Exception as e:
            logger.warning(f"T4 Excel generation skipped: {e}")
            output_path = "output/technique4_tool_analysis.xlsx"
        
        await upsert_t4_run_status("done", total_tools=canonical_count, processed_tools=domain_count, enriched_tools=enriched_count)
        await emit("done", f"T4 analysis complete - {canonical_count} tools analyzed", 1.0)
        
        logger.info(f"T4 pipeline complete: {canonical_count} tools, {output_path}")
        return True
        
    except Exception as exc:
        logger.error(f"T4 pipeline failed: {exc}", exc_info=True)
        await upsert_t4_run_status("failed")
        await emit("failed", str(exc), 0.0)
        return False
