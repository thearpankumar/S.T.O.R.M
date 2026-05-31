"""
Technique 2: Subdomain-level store operations.

Provides CRUD operations for subdomain-level tool rankings.
"""

import json
import logging
from typing import Any

from db.store import db
from config.settings import settings

logger = logging.getLogger(__name__)


async def get_t2_subdomain_ranking(subdomain_id: int) -> dict | None:
    row = await db.fetchone(
        "SELECT * FROM t2_subdomain_rankings WHERE subdomain_id = ?",
        (subdomain_id,)
    )
    return dict(row) if row else None


async def get_all_t2_subdomain_rankings() -> list[dict]:
    rows = await db.fetchall(
        "SELECT sr.*, sd.name as subdomain_name, sd.domain_id, d.name as domain_name "
        "FROM t2_subdomain_rankings sr "
        "JOIN subdomains sd ON sr.subdomain_id = sd.id "
        "JOIN domains d ON sd.domain_id = d.id "
        "ORDER BY d.name, sd.name"
    )
    return [dict(row) for row in rows]


async def get_t2_rankings_for_domain(domain_id: int) -> list[dict]:
    rows = await db.fetchall(
        "SELECT sr.*, sd.name as subdomain_name "
        "FROM t2_subdomain_rankings sr "
        "JOIN subdomains sd ON sr.subdomain_id = sd.id "
        "WHERE sd.domain_id = ? "
        "ORDER BY sd.name",
        (domain_id,)
    )
    return [dict(row) for row in rows]


async def upsert_t2_subdomain_ranking(
    subdomain_id: int,
    status: str = "pending",
    ranked_enterprise_tools: int = 0,
    ranked_opensource_tools: int = 0,
) -> None:
    await db.execute(
        """INSERT INTO t2_subdomain_rankings 
           (subdomain_id, status, ranked_enterprise_tools, ranked_opensource_tools, updated_at)
           VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(subdomain_id) DO UPDATE SET
             status = excluded.status,
             ranked_enterprise_tools = excluded.ranked_enterprise_tools,
             ranked_opensource_tools = excluded.ranked_opensource_tools,
             updated_at = CURRENT_TIMESTAMP""",
        (subdomain_id, status, ranked_enterprise_tools, ranked_opensource_tools)
    )
    await db.commit()


async def update_t2_subdomain_ranking_status(subdomain_id: int, status: str) -> None:
    await db.execute(
        "UPDATE t2_subdomain_rankings SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE subdomain_id = ?",
        (status, subdomain_id)
    )
    await db.commit()


async def upsert_t2_subdomain_tool(
    subdomain_id: int,
    tool_id: int,
    vendor: str,
    product_name: str,
    tool_type: str,
    rank_position: int,
    composite_score: float,
    feature_coverage_score: float = 0.0,
    market_presence_score: float = 0.0,
    rank_distribution_score: float = 0.0,
) -> int:
    await db.execute(
        """INSERT INTO t2_subdomain_tools 
           (subdomain_id, tool_id, vendor, product_name, tool_type, rank_position, composite_score,
            feature_coverage_score, market_presence_score, rank_distribution_score)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(subdomain_id, product_name) DO UPDATE SET
             tool_id = excluded.tool_id,
             vendor = excluded.vendor,
             tool_type = excluded.tool_type,
             rank_position = excluded.rank_position,
             composite_score = excluded.composite_score,
             feature_coverage_score = excluded.feature_coverage_score,
             market_presence_score = excluded.market_presence_score,
             rank_distribution_score = excluded.rank_distribution_score""",
        (subdomain_id, tool_id, vendor, product_name, tool_type, rank_position, composite_score,
         feature_coverage_score, market_presence_score, rank_distribution_score)
    )
    await db.commit()
    row = await db.fetchone(
        "SELECT id FROM t2_subdomain_tools WHERE subdomain_id = ? AND product_name = ?",
        (subdomain_id, product_name)
    )
    return row["id"] if row else 0


async def get_t2_subdomain_tools(subdomain_id: int) -> list[dict]:
    rows = await db.fetchall(
        "SELECT * FROM t2_subdomain_tools WHERE subdomain_id = ? ORDER BY tool_type, rank_position",
        (subdomain_id,)
    )
    return [dict(row) for row in rows]


async def delete_t2_subdomain_tools(subdomain_id: int) -> None:
    await db.execute("DELETE FROM t2_subdomain_tools WHERE subdomain_id = ?", (subdomain_id,))
    await db.commit()


async def get_eligible_subdomains() -> list[dict]:
    rows = await db.fetchall(
        """SELECT sd.*, d.name as domain_name,
                  (SELECT COUNT(*) FROM tools t WHERE t.subdomain_id = sd.id) as tool_count,
                  (SELECT COUNT(*) FROM features f WHERE f.subdomain_id = sd.id) as feature_count
           FROM subdomains sd
           JOIN domains d ON sd.domain_id = d.id
           WHERE sd.status = 'done' 
           AND EXISTS (SELECT 1 FROM tools t WHERE t.subdomain_id = sd.id)
           ORDER BY d.name, sd.name"""
    )
    return [dict(row) for row in rows]


async def get_eligible_subdomains_for_domain(domain_id: int) -> list[dict]:
    rows = await db.fetchall(
        """SELECT sd.*, d.name as domain_name,
                  (SELECT COUNT(*) FROM tools t WHERE t.subdomain_id = sd.id) as tool_count,
                  (SELECT COUNT(*) FROM features f WHERE f.subdomain_id = sd.id) as feature_count
           FROM subdomains sd
           JOIN domains d ON sd.domain_id = d.id
           WHERE sd.domain_id = ? 
           AND sd.status = 'done'
           AND EXISTS (SELECT 1 FROM tools t WHERE t.subdomain_id = sd.id)
           ORDER BY sd.name""",
        (domain_id,)
    )
    return [dict(row) for row in rows]


async def save_t2_subdomain_worker_state(subdomain_id: int, state_json: str, current_step: str) -> None:
    await db.execute(
        """INSERT INTO t2_subdomain_worker_state (subdomain_id, state_json, current_step, updated_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(subdomain_id) DO UPDATE SET
             state_json = excluded.state_json,
             current_step = excluded.current_step,
             updated_at = CURRENT_TIMESTAMP""",
        (subdomain_id, state_json, current_step)
    )
    await db.commit()


async def load_t2_subdomain_worker_state(subdomain_id: int) -> tuple[str, str] | None:
    row = await db.fetchone(
        "SELECT state_json, current_step FROM t2_subdomain_worker_state WHERE subdomain_id = ?",
        (subdomain_id,)
    )
    if row:
        return row["state_json"], row["current_step"]
    return None


async def delete_t2_subdomain_worker_state(subdomain_id: int) -> None:
    await db.execute("DELETE FROM t2_subdomain_worker_state WHERE subdomain_id = ?", (subdomain_id,))
    await db.commit()


async def cleanup_t2_subdomain_data(subdomain_id: int) -> None:
    await delete_t2_subdomain_tools(subdomain_id)
    await delete_t2_subdomain_worker_state(subdomain_id)
    await db.execute(
        "UPDATE t2_subdomain_rankings SET status = 'pending' WHERE subdomain_id = ?",
        (subdomain_id,)
    )
    await db.commit()
    logger.info(f"Cleaned up T2 data for subdomain_id={subdomain_id}")


async def seed_t2_subdomain_rankings() -> None:
    eligible = await get_eligible_subdomains()
    for sd in eligible:
        await db.execute(
            "INSERT OR IGNORE INTO t2_subdomain_rankings (subdomain_id, status) VALUES (?, 'pending')",
            (sd["id"],)
        )
    await db.commit()
    logger.info(f"Seeded T2 rankings for {len(eligible)} eligible subdomains")
