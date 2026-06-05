"""
db/t5_store.py - Database operations for Technique 5 Score Card.
"""

import json
import logging
from datetime import datetime
from typing import Any

from db.store import db

logger = logging.getLogger(__name__)


def _validate_tuple_length(tuples: list[tuple], expected_len: int, name: str) -> None:
    """Validate that all tuples have expected length."""
    for i, t in enumerate(tuples):
        if len(t) != expected_len:
            raise ValueError(f"{name} at index {i} has {len(t)} elements, expected {expected_len}")


async def upsert_t5_run_status(status: str, total_tools: int = 0, scored_tools: int = 0) -> None:
    """Update T5 pipeline run status."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    
    if status == "running":
        await db.execute(
            """INSERT INTO t5_score_runs (status, total_tools, scored_tools, started_at)
               VALUES (?, ?, ?, ?)""",
            ("running", total_tools, scored_tools, now)
        )
    else:
        await db.execute(
            """UPDATE t5_score_runs
               SET status = ?,
                   total_tools = MAX(total_tools, ?),
                   scored_tools = MAX(scored_tools, ?),
                   completed_at = CASE WHEN ? IN ('done', 'failed') THEN ? ELSE completed_at END
               WHERE id = (SELECT MAX(id) FROM t5_score_runs)""",
            (status, total_tools, scored_tools, status, now)
        )


async def get_t5_run_status() -> dict[str, Any]:
    """Get latest run status for T5 pipeline."""
    row = await db.fetchone(
        """SELECT * FROM t5_score_runs ORDER BY id DESC LIMIT 1"""
    )
    if not row:
        return {"status": "pending", "total_tools": 0, "scored_tools": 0}
    return dict(row)


async def bulk_upsert_t5_scores(scores: list[tuple]) -> None:
    """
    Bulk insert or update tool scores.
    Tuple order: t4_tool_id, vendor, product_name, primary_domain, tool_category, d1, d2, d3, d4, d5, composite, grade, domain_rank
    """
    if not scores:
        return
    
    _validate_tuple_length(scores, 13, "Score record")
    conn = await db._get_conn()
    await conn.executemany(
        """INSERT INTO t5_tool_scores (
               t4_tool_id, vendor, product_name, primary_domain, tool_category,
               d1_feature_coverage, d2_domain_breadth, d3_nist_alignment, 
               d4_market_maturity, d5_ranking_signal, 
               composite_score, grade, domain_rank
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(t4_tool_id) DO UPDATE SET
               vendor=excluded.vendor,
               product_name=excluded.product_name,
               primary_domain=excluded.primary_domain,
               tool_category=excluded.tool_category,
               d1_feature_coverage=excluded.d1_feature_coverage,
               d2_domain_breadth=excluded.d2_domain_breadth,
               d3_nist_alignment=excluded.d3_nist_alignment,
               d4_market_maturity=excluded.d4_market_maturity,
               d5_ranking_signal=excluded.d5_ranking_signal,
               composite_score=excluded.composite_score,
               grade=excluded.grade,
               domain_rank=excluded.domain_rank,
               updated_at=CURRENT_TIMESTAMP""",
        scores
    )
    await conn.commit()

async def update_t5_tool_insights(insights: list[tuple[int, str, str]]) -> None:
    """Update strategic insights for tools in bulk. (t4_tool_id, quadrant_position, insight)
    
    Commits after each row to release the SQLite write lock quickly, preventing
    'database is locked' errors when the UI auto-refresh runs concurrently.
    """
    if not insights:
        return
    
    _validate_tuple_length(insights, 3, "Insight record")
    conn = await db._get_conn()
    for t4_id, quadrant, insight in insights:
        await conn.execute(
            """UPDATE t5_tool_scores SET quadrant_position = ?, strategic_insight = ? WHERE t4_tool_id = ?""",
            (quadrant, insight, t4_id)
        )
        await conn.commit()  # Commit each row individually to release write lock


async def get_t5_scores(min_score: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
    """Get all tool scores above minimum score, sorted by rank."""
    query = """
        SELECT * FROM t5_tool_scores 
        WHERE composite_score >= ?
        ORDER BY primary_domain ASC, domain_rank ASC
    """
    params = [min_score]
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
        
    rows = await db.fetchall(query, params)
    return [dict(r) for r in rows]


async def get_t5_stats() -> dict[str, Any]:
    """Get aggregate statistics for the T5 Score Card."""
    # Base count and average
    base = await db.fetchone("""
        SELECT COUNT(*) as total, AVG(composite_score) as avg_composite,
               AVG(d1_feature_coverage) as avg_d1,
               AVG(d2_domain_breadth) as avg_d2,
               AVG(d3_nist_alignment) as avg_d3,
               AVG(d4_market_maturity) as avg_d4,
               AVG(d5_ranking_signal) as avg_d5
        FROM t5_tool_scores
    """)
    
    # Grade distribution
    grades = await db.fetchall("""
        SELECT grade, COUNT(*) as count 
        FROM t5_tool_scores 
        GROUP BY grade 
        ORDER BY grade
    """)
    
    # Grade sorting: A+, A, B+, B, C, D
    grade_order = {"A+": 0, "A": 1, "B+": 2, "B": 3, "C": 4, "D": 5}
    grade_counts = {r["grade"]: r["count"] for r in grades if r["grade"]}
    sorted_grades = {g: grade_counts.get(g, 0) for g in grade_order.keys()}
    
    return {
        "total": base["total"] if base else 0,
        "avg_composite": (base["avg_composite"] or 0.0) if base else 0.0,
        "dimension_averages": {
            "d1": (base["avg_d1"] or 0.0) if base else 0.0,
            "d2": (base["avg_d2"] or 0.0) if base else 0.0,
            "d3": (base["avg_d3"] or 0.0) if base else 0.0,
            "d4": (base["avg_d4"] or 0.0) if base else 0.0,
            "d5": (base["avg_d5"] or 0.0) if base else 0.0,
        },
        "grade_distribution": sorted_grades
    }


async def reset_t5_data() -> None:
    """Reset ALL T5 Score Card data — scores, ranks, insights, and run history."""
    await db.execute("DELETE FROM t5_tool_scores")
    await db.execute("DELETE FROM t5_score_runs")
    await db.commit()

