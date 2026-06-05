"""
orchestrator/t5_graph.py - Technique 5 pipeline orchestrator.

Executes T5 Score Card pipeline:
  S1 (0-10%)  - Bootstrap: Verify T4 canonical tools exist
  S2 (10-50%) - Score Compute: Calculate D1-D5
  S3 (50-70%) - Aggregate: Compute composite, grade, rank, DB save
  S4 (70-90%) - LLM Insights: Generate strategic summaries
  S5 (90-100%)- Excel generation
"""

import asyncio
import logging
from typing import Any

from db.store import db
from db.t5_store import upsert_t5_run_status, reset_t5_data
from models.worker import WorkerEvent

logger = logging.getLogger(__name__)


def _step_to_event_type(step: str) -> str:
    if step == "done":
        return "completed"
    if step == "failed":
        return "failed"
    return "progress"


async def run_t5_pipeline(
    event_queue: asyncio.Queue[WorkerEvent] | None = None,
    reset_existing: bool = False,
) -> bool:
    """Execute the full T5 Score Card pipeline."""
    
    def emit_sync(step: str, message: str, progress_pct: float) -> None:
        if event_queue is None:
            return
        event = WorkerEvent(
            subdomain="t5_scorecard",
            event_type=_step_to_event_type(step),
            step=step,
            message=message,
            progress_pct=progress_pct,
        )
        try:
            event_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass
        except Exception as e:
            logger.debug(f"Failed to emit event: {e}")
            
    async def emit(step: str, message: str, progress_pct: float) -> None:
        logger.info(f"[T5:{step}] {message} ({int(progress_pct * 100)}%)")
        emit_sync(step, message, progress_pct)
        
    def make_progress_callback(step: str):
        async def callback(progress_pct: float, message: str) -> None:
            await emit(step, message, progress_pct)
        return callback
        
    logger.info("Starting T5 Score Card pipeline")
    await upsert_t5_run_status("running", total_tools=0, scored_tools=0)
    
    try:
        if reset_existing:
            await reset_t5_data()
            logger.info("T5 data reset")
            
        await emit("s1", "Bootstrapping from T4 canonical tools...", 0.05)
        
        # Verify T4 has been run
        row = await db.fetchone("SELECT COUNT(*) as c FROM t4_tools")
        total_t4 = row["c"] if row else 0
        if total_t4 == 0:
            logger.warning("T5 pipeline: No T4 tools found. Run T4 analysis first.")
            await upsert_t5_run_status("failed")
            await emit("failed", "No T4 canonical tools found. Run T4 analysis first.", 0.0)
            return False
            
        await emit("s1", f"Found {total_t4} tools to score.", 0.10)
        
        from agents.t5_scorer import run_s2_scoring, run_s4_insights
        
        # Runs S2 and S3 logic internally
        scored_count = await run_s2_scoring(on_progress=make_progress_callback("s2"))
        
        if scored_count == 0:
            await upsert_t5_run_status("failed")
            await emit("failed", "Scoring failed to produce results.", 0.0)
            return False
            
        await emit("s4", "Generating strategic LLM insights...", 0.70)
        
        insights_count = await run_s4_insights(on_progress=make_progress_callback("s4"))
        await emit("s4", f"Generated {insights_count} insights.", 0.90)
        
        await emit("s5", "Generating Score Card Excel workbook...", 0.92)
        try:
            from excel.t5_bridge import export_t5_workbook
            output_path = await export_t5_workbook()
            await emit("s5", f"Excel saved: {output_path}", 0.98)
        except Exception as e:
            logger.warning(f"T5 Excel generation failed: {e}")
            output_path = "output/technique5_scorecard.xlsx"
            
        await upsert_t5_run_status("done", total_tools=total_t4, scored_tools=scored_count)
        await emit("done", f"T5 Score Card complete - {scored_count} tools scored", 1.0)
        
        logger.info(f"T5 pipeline complete: {scored_count} tools, {output_path}")
        return True
        
    except Exception as exc:
        logger.error(f"T5 pipeline failed: {exc}", exc_info=True)
        await upsert_t5_run_status("failed")
        await emit("failed", str(exc), 0.0)
        return False
