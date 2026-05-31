"""
Technique 2: Subdomain Pipeline Orchestrator

Coordinates the T2 pipeline at SUBDOMAIN level:
- D1: Tool ranking (within subdomain)
- Uses T1 data directly (features, subfeatures, matrix cells)

No need for D2-D4 aggregation - T1 already has all the data.
"""

import asyncio
import logging
from queue import Queue
import threading

from config.settings import settings
from config.domains import CYBERSECURITY_DOMAINS
from db.store import get_domain_id
from db.subdomain_store import (
    get_t2_subdomain_ranking,
    update_t2_subdomain_ranking_status,
    cleanup_t2_subdomain_data,
    seed_t2_subdomain_rankings,
    get_eligible_subdomains_for_domain,
)
from models.worker import WorkerEvent

logger = logging.getLogger(__name__)

_event_queue: Queue | None = None
_event_queue_lock = threading.Lock()


def create_event_queue() -> Queue:
    global _event_queue
    with _event_queue_lock:
        if _event_queue is None:
            _event_queue = Queue(maxsize=settings.event_queue_maxsize)
        return _event_queue


def emit_event(
    queue: Queue,
    subdomain: str,
    step: str,
    progress: float,
    message: str,
    event_type: str = "progress",
) -> None:
    event = WorkerEvent(
        subdomain=subdomain,
        event_type=event_type,
        step=step,
        progress_pct=progress,
        message=message,
    )
    try:
        queue.put_nowait(event)
    except Exception as e:
        logger.warning(f"Failed to emit event: {e}")


async def seed_t2_rankings() -> None:
    await seed_t2_subdomain_rankings()


async def run_subdomain_pipeline(
    subdomain_id: int,
    subdomain_name: str,
    event_queue: Queue | None = None,
) -> bool:
    logger.info(f"Starting T2 pipeline for subdomain '{subdomain_name}'")
    
    from db.store import db
    
    await db.execute(
        "INSERT OR IGNORE INTO t2_subdomain_rankings (subdomain_id, status) VALUES (?, 'pending')",
        (subdomain_id,)
    )
    await db.commit()
    
    result = await db.execute(
        "UPDATE t2_subdomain_rankings SET status = 'running', updated_at = CURRENT_TIMESTAMP "
        "WHERE subdomain_id = ? AND (status IS NULL OR status != 'running')",
        (subdomain_id,)
    )
    await db.commit()
    
    if result.rowcount == 0:
        logger.warning(f"Subdomain '{subdomain_name}' already running or no ranking row exists")
        return False
    
    def emit(step: str, progress: float, message: str, event_type: str = "progress") -> None:
        if event_queue:
            emit_event(event_queue, subdomain_name, step, progress, message, event_type)
    
    try:
        emit("d1", 0.0, "Starting tool ranking...", "started")
        
        from agents.subdomain_tool_ranker import rank_subdomain_tools
        result = await rank_subdomain_tools(subdomain_id, subdomain_name)
        
        if not result["tools_enterprise"] and not result["tools_opensource"]:
            logger.warning(f"No tools found for subdomain '{subdomain_name}'")
            await cleanup_t2_subdomain_data(subdomain_id)
            return False
        
        emit("d1", 1.0, f"Ranked {len(result['tools_enterprise'])} enterprise + {len(result['tools_opensource'])} OSS tools", "completed")
        
        await update_t2_subdomain_ranking_status(subdomain_id, "done")
        
        logger.info(f"T2 pipeline completed for '{subdomain_name}'")
        return True
        
    except Exception as e:
        logger.error(f"T2 Pipeline failed for '{subdomain_name}': {e}")
        
        await update_t2_subdomain_ranking_status(subdomain_id, "failed")
        
        try:
            await cleanup_t2_subdomain_data(subdomain_id)
            logger.info(f"Cleaned up partial T2 data for '{subdomain_name}'")
        except Exception as cleanup_err:
            logger.error(f"T2 cleanup also failed for '{subdomain_name}': {cleanup_err}")
        
        if event_queue:
            emit_event(event_queue, subdomain_name, "failed", 0.0, str(e), "failed")
        
        return False


async def reset_stale_t2_rankings() -> None:
    from db.store import db
    await db.execute(
        "UPDATE t2_subdomain_rankings SET status = 'pending' WHERE status = 'running'"
    )
    await db.commit()
