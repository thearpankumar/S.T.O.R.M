"""
orchestrator/t3_graph.py — Technique 3 pipeline orchestrator.

Mirrors the pattern of subdomain_graph.py.
Emits WorkerEvent progress messages into an optional asyncio Queue
so the TUI can display live progress in the Active Pipelines tab.
"""

import asyncio
import logging
from asyncio import Queue
from typing import Any

from models.worker import WorkerEvent

logger = logging.getLogger(__name__)


def _step_to_event_type(step: str) -> str:
    """Map a step name to a WorkerEvent event_type literal."""
    if step == "done":
        return "completed"
    if step == "failed":
        return "failed"
    if step == "s1" and False:   # placeholder — s1 start handled below
        return "started"
    return "progress"


async def run_t3_pipeline(
    event_queue=None,
    reset_existing: bool = False,
) -> bool:
    """
    Execute the full T3 classification pipeline.

    Stages:
      s1 (0%–30%)  — SQL deduplication of T1 tools
      s2 (30%–95%) — LLM NIST classification (batched)
      done (100%)  — complete

    Returns True on success, False if no tools were found or an error occurred.
    """
    from db.t3_store import upsert_t3_run_status
    from agents.t3_classifier import run_t3_classification

    def emit_sync(step: str, message: str, progress_pct: float) -> None:
        """Put a WorkerEvent into the queue (sync, non-blocking)."""
        if event_queue is None:
            return
        from models.worker import WorkerEvent
        if step in ("done", "completed"):
            etype = "completed"
        elif step == "failed":
            etype = "failed"
        elif progress_pct == 0.0:
            etype = "started"
        else:
            etype = "progress"
        event = WorkerEvent(
            subdomain="t3_classification",
            event_type=etype,
            step=step,
            message=message,
            progress_pct=progress_pct,
        )
        try:
            event_queue.put_nowait(event)
        except Exception:
            pass  # queue full — drop silently

    async def emit(step: str, message: str, progress_pct: float) -> None:
        logger.info(f"[T3:{step}] {message} ({int(progress_pct * 100)}%)")
        emit_sync(step, message, progress_pct)

    logger.info("Starting T3 pipeline")
    await upsert_t3_run_status("running", total_tools=0, classified_tools=0)

    try:
        await emit("s1", "Loading and deduplicating T1 tools...", 0.05)

        classified = await run_t3_classification(
            on_progress=lambda pct, msg: emit("s2" if pct >= 0.30 else "s1", msg, pct),
            reset_existing=reset_existing,
        )

        if classified == 0:
            logger.warning("T3 pipeline: no tools classified — T1 pipelines may not be complete")
            await upsert_t3_run_status("failed", total_tools=0, classified_tools=0)
            await emit("failed", "No T1 tools found. Run T1 pipelines first.", 0.0)
            return False

        await emit("s3", "Generating executive summary...", 0.95)
        try:
            from db.t3_store import get_t3_stats, update_t3_executive_summary
            from agents.t3_classifier import generate_t3_executive_summary
            stats = await get_t3_stats()
            summary = await generate_t3_executive_summary(stats)
            await update_t3_executive_summary(summary)
        except Exception as e:
            logger.error(f"Failed to generate T3 executive summary: {e}")

        await upsert_t3_run_status("done", total_tools=classified, classified_tools=classified)
        await emit("done", f"Classification complete — {classified} unique tools", 1.0)
        logger.info(f"T3 pipeline complete: {classified} tools classified")
        return True

    except Exception as exc:
        logger.error(f"T3 pipeline failed: {exc}", exc_info=True)
        await upsert_t3_run_status("failed")
        await emit("failed", str(exc), 0.0)
        return False
