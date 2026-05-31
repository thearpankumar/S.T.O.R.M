"""
Technique 2: Domain Pipeline Orchestrator

Coordinates the D1-D5 pipeline stages for domain-level tool ranking:
- D1: Tool aggregation
- D2: Feature aggregation
- D3: Subfeature generation
- D4: Matrix population
- D5: Excel export
"""

import asyncio
import logging
from queue import Queue

from config.settings import settings
from config.domains import CYBERSECURITY_DOMAINS
from db.store import get_domain_id
from db.domain_store import (
    update_t2_domain_ranking_status,
    get_t2_domain_ranking,
    cleanup_t2_domain_data,
)
from models.worker import WorkerEvent

logger = logging.getLogger(__name__)

_event_queue: Queue | None = None


def create_event_queue() -> Queue:
    global _event_queue
    if _event_queue is None:
        _event_queue = Queue(maxsize=settings.event_queue_maxsize)
    return _event_queue


def emit_event(
    queue: Queue,
    domain: str,
    step: str,
    progress: float,
    message: str,
    event_type: str = "progress",
) -> None:
    event = WorkerEvent(
        subdomain=domain,
        event_type=event_type,
        step=step,
        progress_pct=progress,
        message=message,
    )
    try:
        queue.put_nowait(event)
    except Exception as e:
        logger.warning(f"Failed to emit event: {e}")


async def run_domain_pipeline(
    domain_name: str,
    event_queue: Queue | None = None,
) -> bool:
    logger.info(f"Starting Technique 2 pipeline for domain '{domain_name}'")
    
    domain_id = await get_domain_id(domain_name)
    if not domain_id:
        logger.error(f"Domain '{domain_name}' not found")
        return False
    
    from db.store import db
    result = await db.execute(
        "UPDATE t2_domain_rankings SET status = 'running', updated_at = CURRENT_TIMESTAMP WHERE domain_id = ? AND (status IS NULL OR status != 'running')",
        (domain_id,)
    )
    await db.commit()
    
    if result.rowcount == 0:
        logger.warning(f"Domain '{domain_name}' already running or no ranking row exists")
        return False
    
    def emit(step: str, progress: float, message: str, event_type: str = "progress") -> None:
        if event_queue:
            emit_event(event_queue, domain_name, step, progress, message, event_type)
    
    try:
        emit("d1", 0.0, "Starting tool aggregation...", "started")
        
        from agents.domain_tool_aggregator import aggregate_and_rank_tools
        tool_result = await aggregate_and_rank_tools(domain_name)
        
        emit("d1", 0.20, f"Ranked {len(tool_result.tools_enterprise)} enterprise + {len(tool_result.tools_opensource)} OSS tools")
        
        if not tool_result.tools_enterprise and not tool_result.tools_opensource:
            logger.warning(f"No tools found for domain '{domain_name}'")
            await cleanup_t2_domain_data(domain_id)
            return False
        
        emit("d2", 0.25, "Aggregating domain features...")
        
        from agents.domain_feature_aggregator import aggregate_domain_features
        feature_result = await aggregate_domain_features(domain_id, domain_name)
        
        emit("d2", 0.35, f"Aggregated {len(feature_result.features)} domain features")
        
        if not feature_result.features:
            logger.warning(f"No features found for domain '{domain_name}'")
            await cleanup_t2_domain_data(domain_id)
            return False
        
        emit("d3", 0.40, "Generating subfeatures...")
        
        from agents.domain_subfeature_gen import generate_all_domain_subfeatures
        subfeatures = await generate_all_domain_subfeatures(domain_id, domain_name)
        
        emit("d3", 0.55, f"Generated {len(subfeatures)} subfeatures")
        
        if not subfeatures:
            logger.warning(f"No subfeatures generated for domain '{domain_name}'")
            await cleanup_t2_domain_data(domain_id)
            return False
        
        emit("d4", 0.60, "Populating feature matrix...")
        
        from agents.domain_matrix_populator import populate_domain_matrix
        matrix_result = await populate_domain_matrix(domain_id, domain_name)
        
        emit("d4", 0.85, f"Populated {len(matrix_result.rows)} matrix rows")
        
        emit("d5", 0.90, "Exporting to Excel...")
        
        from excel.domain_bridge import export_domain_ranking_excel
        output_path = await export_domain_ranking_excel(domain_id)
        
        emit("d5", 1.0, f"Exported to {output_path}", "completed")
        
        await update_t2_domain_ranking_status(domain_id, "done")
        
        logger.info(f"Technique 2 pipeline completed for '{domain_name}'")
        return True
        
    except Exception as e:
        logger.error(f"Pipeline failed for '{domain_name}': {e}")
        
        await update_t2_domain_ranking_status(domain_id, "failed")
        
        try:
            await cleanup_t2_domain_data(domain_id)
            logger.info(f"Cleaned up partial T2 data for '{domain_name}'")
        except Exception as cleanup_err:
            logger.error(f"T2 cleanup also failed for '{domain_name}': {cleanup_err}")
        
        if event_queue:
            emit_event(event_queue, domain_name, "failed", 0.0, str(e), "failed")
        
        return False


async def run_all_domain_pipelines(
    event_queue: Queue | None = None,
    domain_filter: list[str] | None = None,
) -> dict[str, bool]:
    results = {}
    
    domains = domain_filter or CYBERSECURITY_DOMAINS
    
    for domain in domains:
        result = await run_domain_pipeline(domain, event_queue)
        results[domain] = result
    
    return results
