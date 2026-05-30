import asyncio
import logging
from models.worker import WorkerState, WorkerEvent
from models.tools import Tool
from models.features import Feature
from config.settings import settings
from db.store import (
    save_worker_state,
    load_worker_state,
    update_subdomain_status,
    cleanup_subdomain_data,
    get_tools,
    get_features,
    get_subfeatures,
    db as _db,
)

from agents.tool_discovery import run_tool_discovery
from agents.feature_discovery import run_feature_discovery_direct
from agents.subfeature_discovery import run_subfeature_discovery
from agents.matrix_population import run_matrix_population
from excel.bridge import create_workbook_from_db

logger = logging.getLogger(__name__)

_semaphore: asyncio.Semaphore | None = None


def get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.max_workers)
    return _semaphore


def create_event_queue() -> asyncio.Queue[WorkerEvent]:
    return asyncio.Queue(maxsize=settings.event_queue_maxsize)


async def run_worker_pipeline(
    domain: str,
    subdomain_id: int,
    subdomain_name: str,
    event_queue: asyncio.Queue[WorkerEvent],
) -> WorkerState:
    state = WorkerState(
        domain=domain,
        subdomain=subdomain_name,
        subdomain_id=subdomain_id,
    )
    
    sem = get_semaphore()
    
    async with sem:
        await event_queue.put(WorkerEvent(
            subdomain=subdomain_name,
            event_type="started",
            step="m2",
            message="Starting worker pipeline",
            progress_pct=0.0
        ))
        
        await update_subdomain_status(subdomain_id, "running")
        
        try:
            state.current_step = "m2"
            await event_queue.put(WorkerEvent(
                subdomain=subdomain_name,
                event_type="progress",
                step="m2",
                message="Discovering tools",
                progress_pct=0.2
            ))
            
            tool_result = await run_tool_discovery(subdomain_id, subdomain_name)
            state.tools_enterprise = [
                Tool(vendor=t["vendor"], product_name=t["product_name"], tool_type="enterprise")
                for t in tool_result.tools_enterprise
            ]
            state.tools_opensource = [
                Tool(vendor=t["vendor"], product_name=t["product_name"], tool_type="opensource")
                for t in tool_result.tools_opensource
            ]
            await save_worker_state(subdomain_id, state.to_checkpoint(), "m2")
            
            state.current_step = "m3"
            await event_queue.put(WorkerEvent(
                subdomain=subdomain_name,
                event_type="progress",
                step="m3",
                message="Discovering features",
                progress_pct=0.4
            ))
            
            feature_result = await run_feature_discovery_direct(
                subdomain_id, subdomain_name, state.tools_enterprise, state.tools_opensource
            )
            state.features = [
                Feature(name=f["name"], description=f.get("description", ""), rank_order=f.get("rank_order", 0))
                for f in feature_result.features
            ]
            await save_worker_state(subdomain_id, state.to_checkpoint(), "m3")
            
            state.current_step = "m4"
            total_features = len(state.features)
            m4_done = [0]  # mutable counter for closure

            async def m4_progress(feature_name: str) -> None:
                m4_done[0] += 1
                pct = 0.4 + 0.2 * (m4_done[0] / max(total_features, 1))
                await event_queue.put(WorkerEvent(
                    subdomain=subdomain_name,
                    event_type="progress",
                    step="m4",
                    message=f"Subfeatures: {feature_name} ({m4_done[0]}/{total_features})",
                    progress_pct=round(pct, 2),
                ))

            await event_queue.put(WorkerEvent(
                subdomain=subdomain_name,
                event_type="progress",
                step="m4",
                message="Discovering subfeatures",
                progress_pct=0.4
            ))

            subfeature_result = await run_subfeature_discovery(
                subdomain_id, subdomain_name, progress_cb=m4_progress
            )
            state.sub_features = subfeature_result.sub_features
            await save_worker_state(subdomain_id, state.to_checkpoint(), "m4")

            # ── Pre-flight matrix size guard ──────────────────────────────────
            # If the matrix would produce too many cells (tools x subfeatures)
            # to fit in per-feature LLM chunks, trim excess features from the DB
            # NOW before they reach matrix population.
            MAX_MATRIX_CELLS = 4_000
            from agents.feature_discovery import MAX_FEATURES as _MAX_F
            _tools_now    = await get_tools(subdomain_id)
            _features_now = await get_features(subdomain_id)

            # Cannot await inside sum() generator — collect counts with a loop
            _total_sfs = 0
            for _f in _features_now:
                _sfs = await get_subfeatures(_f["id"])
                _total_sfs += len(_sfs)

            _projected = len(_tools_now) * _total_sfs

            if _projected > MAX_MATRIX_CELLS:
                _safe   = _features_now[:_MAX_F]
                _excess = _features_now[_MAX_F:]
                logger.warning(
                    f"Pre-flight guard: '{subdomain_name}' has {_projected} projected "
                    f"cells ({len(_features_now)}f x {_total_sfs}sf x {len(_tools_now)}t). "
                    f"Trimming {len(_excess)} excess features."
                )
                for _f in _excess:
                    for _sf in await get_subfeatures(_f["id"]):
                        await _db.execute(
                            "DELETE FROM subfeatures WHERE id = ?", (_sf["id"],)
                        )
                    await _db.execute(
                        "DELETE FROM features WHERE id = ?", (_f["id"],)
                    )
                await _db.commit()

                # Recount after trim — again, must use a loop not sum(await ...)
                _total_sfs_after = 0
                for _f in _safe:
                    _sfs = await get_subfeatures(_f["id"])
                    _total_sfs_after += len(_sfs)

                logger.info(
                    f"After trim: {len(_safe)} features, {_total_sfs_after} sfs, "
                    f"{len(_tools_now) * _total_sfs_after} projected cells"
                )
            else:
                logger.info(
                    f"Pre-flight OK for '{subdomain_name}': {_projected} projected "
                    f"cells ({len(_features_now)}f x {_total_sfs}sf x {len(_tools_now)}t)"
                )
            # ── End pre-flight guard ──────────────────────────────────────────


            state.current_step = "m5"
            total_m5 = len(state.features)
            m5_done = [0]

            async def m5_progress(feature_name: str, done: int, total: int) -> None:
                m5_done[0] = done
                pct = 0.6 + 0.35 * (done / max(total, 1))
                await event_queue.put(WorkerEvent(
                    subdomain=subdomain_name,
                    event_type="progress",
                    step="m5",
                    message=f"Matrix: {feature_name} ({done}/{total})",
                    progress_pct=round(pct, 2),
                ))

            await event_queue.put(WorkerEvent(
                subdomain=subdomain_name,
                event_type="progress",
                step="m5",
                message="Populating matrix",
                progress_pct=0.6
            ))

            await run_matrix_population(
                subdomain_id, subdomain_name, progress_cb=m5_progress
            )
            await save_worker_state(subdomain_id, state.to_checkpoint(), "m5")
            
            await create_workbook_from_db(subdomain_id, subdomain_name)
            
            await update_subdomain_status(subdomain_id, "done")
            
            await event_queue.put(WorkerEvent(
                subdomain=subdomain_name,
                event_type="completed",
                step="m5",
                message="Pipeline completed successfully",
                progress_pct=1.0
            ))
            
            logger.info(f"Completed pipeline for subdomain '{subdomain_name}'")
            
        except Exception as e:
            logger.error(f"Pipeline failed for subdomain '{subdomain_name}': {e}")
            # Clean up ALL partial data so the DB stays consistent and the
            # subdomain is reset to 'pending' for a clean retry.
            try:
                await cleanup_subdomain_data(subdomain_id)
            except Exception as cleanup_err:
                logger.error(f"Cleanup also failed for '{subdomain_name}': {cleanup_err}")
                await update_subdomain_status(subdomain_id, "failed")
            await event_queue.put(WorkerEvent(
                subdomain=subdomain_name,
                event_type="failed",
                step=state.current_step,
                message=f"Pipeline failed: {str(e)}",
                progress_pct=0.0
            ))
            raise
    
    return state


async def resume_worker_pipeline(
    subdomain_id: int,
    subdomain_name: str,
    event_queue: asyncio.Queue[WorkerEvent],
) -> WorkerState:
    checkpoint = await load_worker_state(subdomain_id)
    
    if not checkpoint:
        raise ValueError(f"No checkpoint found for subdomain {subdomain_id}")
    
    state_json, last_step = checkpoint
    state = WorkerState.from_checkpoint(state_json)
    
    logger.info(f"Resuming subdomain '{subdomain_name}' from step {last_step}")
    
    sem = get_semaphore()
    
    async with sem:
        await event_queue.put(WorkerEvent(
            subdomain=subdomain_name,
            event_type="started",
            step=last_step,
            message=f"Resuming from {last_step}",
            progress_pct=0.0
        ))
        
        await update_subdomain_status(subdomain_id, "running")
        
        try:
            if last_step == "m2":
                tools = await get_tools(subdomain_id)
                state.tools_enterprise = [
                    Tool(vendor=t["vendor"], product_name=t["product_name"], tool_type="enterprise")
                    for t in tools if t["tool_type"] == "enterprise"
                ]
                state.tools_opensource = [
                    Tool(vendor=t["vendor"], product_name=t["product_name"], tool_type="opensource")
                    for t in tools if t["tool_type"] == "opensource"
                ]
                last_step = "m3"
            
            if last_step == "m3":
                features = await get_features(subdomain_id)
                state.features = [
                    Feature(name=f["name"], description="", rank_order=f.get("rank_order", 0))
                    for f in features
                ]
                last_step = "m4"
            
            if last_step == "m4":
                features = await get_features(subdomain_id)
                for f in features:
                    subfeatures = await get_subfeatures(f["id"])
                    state.sub_features[f["name"]] = [sf["name"] for sf in subfeatures]
                last_step = "m5"
            
            state.current_step = "m5"
            await event_queue.put(WorkerEvent(
                subdomain=subdomain_name,
                event_type="progress",
                step="m5",
                message="Populating matrix (resumed)",
                progress_pct=0.8
            ))
            
            await run_matrix_population(
                subdomain_id, subdomain_name,
                progress_cb=None,  # resume: simple run without fine-grained events
            )
            await create_workbook_from_db(subdomain_id, subdomain_name)
            
            await update_subdomain_status(subdomain_id, "done")
            
            await event_queue.put(WorkerEvent(
                subdomain=subdomain_name,
                event_type="completed",
                step="m5",
                message="Pipeline completed (resumed)",
                progress_pct=1.0
            ))
            
        except Exception as e:
            logger.error(f"Resume failed for subdomain '{subdomain_name}': {e}")
            # Same atomic cleanup on resume failure
            try:
                await cleanup_subdomain_data(subdomain_id)
            except Exception as cleanup_err:
                logger.error(f"Cleanup also failed for '{subdomain_name}': {cleanup_err}")
                await update_subdomain_status(subdomain_id, "failed")
            await event_queue.put(WorkerEvent(
                subdomain=subdomain_name,
                event_type="failed",
                step=state.current_step,
                message=f"Resume failed: {str(e)}",
                progress_pct=0.0
            ))
            raise
    
    return state


async def run_multiple_workers(
    tasks: list[tuple[str, int, str]],
    event_queue: asyncio.Queue[WorkerEvent],
) -> list[WorkerState | Exception]:
    coros = [
        run_worker_pipeline(domain, subdomain_id, subdomain_name, event_queue)
        for domain, subdomain_id, subdomain_name in tasks
    ]
    return await asyncio.gather(*coros, return_exceptions=True)
