"""
Technique 2: Domain-level store operations.

Provides CRUD operations for domain-level tool rankings, features, and matrix cells.
Uses the same Database instance as Technique 1 for connection pooling.
"""

import json
import logging
from typing import Any

from db.store import db
from config.settings import settings

logger = logging.getLogger(__name__)


async def get_t2_domain_ranking(domain_id: int) -> dict | None:
    row = await db.fetchone(
        "SELECT * FROM t2_domain_rankings WHERE domain_id = ?",
        (domain_id,)
    )
    return dict(row) if row else None


async def get_all_t2_domain_rankings() -> list[dict]:
    rows = await db.fetchall(
        "SELECT d.name as domain_name, dr.* FROM t2_domain_rankings dr "
        "JOIN domains d ON dr.domain_id = d.id ORDER BY d.name"
    )
    return [dict(row) for row in rows]


async def upsert_t2_domain_ranking(
    domain_id: int,
    status: str = "pending",
    total_enterprise_tools: int = 0,
    total_opensource_tools: int = 0,
    selected_enterprise_tools: int = 0,
    selected_opensource_tools: int = 0,
    total_features: int = 0,
) -> None:
    await db.execute(
        """INSERT INTO t2_domain_rankings 
           (domain_id, status, total_enterprise_tools, total_opensource_tools,
            selected_enterprise_tools, selected_opensource_tools, total_features, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(domain_id) DO UPDATE SET
             status = excluded.status,
             total_enterprise_tools = excluded.total_enterprise_tools,
             total_opensource_tools = excluded.total_opensource_tools,
             selected_enterprise_tools = excluded.selected_enterprise_tools,
             selected_opensource_tools = excluded.selected_opensource_tools,
             total_features = excluded.total_features,
             updated_at = CURRENT_TIMESTAMP""",
        (domain_id, status, total_enterprise_tools, total_opensource_tools,
         selected_enterprise_tools, selected_opensource_tools, total_features)
    )
    await db.commit()


async def update_t2_domain_ranking_status(domain_id: int, status: str) -> None:
    await db.execute(
        "UPDATE t2_domain_rankings SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE domain_id = ?",
        (status, domain_id)
    )
    await db.commit()


async def upsert_t2_domain_tool(
    domain_id: int,
    vendor: str,
    product_name: str,
    tool_type: str,
    rank_position: int,
    composite_score: float,
    subdomain_presence_count: int = 0,
    subdomain_presence_score: float = 0.0,
    feature_coverage_score: float = 0.0,
    market_presence_score: float = 0.0,
    rank_distribution_score: float = 0.0,
) -> int:
    await db.execute(
        """INSERT INTO t2_domain_tools 
           (domain_id, vendor, product_name, tool_type, rank_position, composite_score,
            subdomain_presence_count, subdomain_presence_score, feature_coverage_score,
            market_presence_score, rank_distribution_score)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(domain_id, product_name) DO UPDATE SET
             vendor = excluded.vendor,
             tool_type = excluded.tool_type,
             rank_position = excluded.rank_position,
             composite_score = excluded.composite_score,
             subdomain_presence_count = excluded.subdomain_presence_count,
             subdomain_presence_score = excluded.subdomain_presence_score,
             feature_coverage_score = excluded.feature_coverage_score,
             market_presence_score = excluded.market_presence_score,
             rank_distribution_score = excluded.rank_distribution_score""",
        (domain_id, vendor, product_name, tool_type, rank_position, composite_score,
         subdomain_presence_count, subdomain_presence_score, feature_coverage_score,
         market_presence_score, rank_distribution_score)
    )
    await db.commit()
    row = await db.fetchone(
        "SELECT id FROM t2_domain_tools WHERE domain_id = ? AND product_name = ?",
        (domain_id, product_name)
    )
    return row["id"] if row else 0


async def get_t2_domain_tools(domain_id: int) -> list[dict]:
    rows = await db.fetchall(
        "SELECT * FROM t2_domain_tools WHERE domain_id = ? ORDER BY tool_type, rank_position",
        (domain_id,)
    )
    return [dict(row) for row in rows]


async def get_t2_domain_tools_by_type(domain_id: int, tool_type: str) -> list[dict]:
    rows = await db.fetchall(
        "SELECT * FROM t2_domain_tools WHERE domain_id = ? AND tool_type = ? ORDER BY rank_position",
        (domain_id, tool_type)
    )
    return [dict(row) for row in rows]


async def delete_t2_domain_tools(domain_id: int) -> None:
    await db.execute("DELETE FROM t2_domain_tools WHERE domain_id = ?", (domain_id,))
    await db.commit()


async def upsert_t2_domain_feature(
    domain_id: int,
    name: str,
    rank_order: int,
    source_subdomains: list[str] | None = None,
) -> int:
    source_json = json.dumps(source_subdomains) if source_subdomains else "[]"
    await db.execute(
        """INSERT INTO t2_domain_features (domain_id, name, rank_order, source_subdomains)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(domain_id, name) DO UPDATE SET
             rank_order = excluded.rank_order,
             source_subdomains = excluded.source_subdomains""",
        (domain_id, name, rank_order, source_json)
    )
    await db.commit()
    row = await db.fetchone(
        "SELECT id FROM t2_domain_features WHERE domain_id = ? AND name = ?",
        (domain_id, name)
    )
    return row["id"] if row else 0


async def get_t2_domain_features(domain_id: int) -> list[dict]:
    rows = await db.fetchall(
        "SELECT * FROM t2_domain_features WHERE domain_id = ? ORDER BY rank_order",
        (domain_id,)
    )
    return [dict(row) for row in rows]


async def delete_t2_domain_features(domain_id: int) -> None:
    await db.execute("DELETE FROM t2_domain_features WHERE domain_id = ?", (domain_id,))
    await db.commit()


async def upsert_t2_domain_subfeature(
    domain_feature_id: int,
    name: str,
    rank_order: int,
) -> int:
    await db.execute(
        """INSERT INTO t2_domain_subfeatures (domain_feature_id, name, rank_order)
           VALUES (?, ?, ?)
           ON CONFLICT(domain_feature_id, name) DO UPDATE SET rank_order = excluded.rank_order""",
        (domain_feature_id, name, rank_order)
    )
    await db.commit()
    row = await db.fetchone(
        "SELECT id FROM t2_domain_subfeatures WHERE domain_feature_id = ? AND name = ?",
        (domain_feature_id, name)
    )
    return row["id"] if row else 0


async def get_t2_domain_subfeatures(domain_feature_id: int) -> list[dict]:
    rows = await db.fetchall(
        "SELECT * FROM t2_domain_subfeatures WHERE domain_feature_id = ? ORDER BY rank_order",
        (domain_feature_id,)
    )
    return [dict(row) for row in rows]


async def get_t2_domain_subfeatures_by_domain(domain_id: int) -> list[dict]:
    rows = await db.fetchall(
        """SELECT sf.*, f.domain_id, f.name as feature_name
           FROM t2_domain_subfeatures sf
           JOIN t2_domain_features f ON sf.domain_feature_id = f.id
           WHERE f.domain_id = ?
           ORDER BY f.rank_order, sf.rank_order""",
        (domain_id,)
    )
    return [dict(row) for row in rows]


async def delete_t2_domain_subfeatures(domain_id: int) -> None:
    await db.execute(
        """DELETE FROM t2_domain_subfeatures 
           WHERE domain_feature_id IN (
               SELECT id FROM t2_domain_features WHERE domain_id = ?
           )""",
        (domain_id,)
    )
    await db.commit()


async def upsert_t2_domain_matrix_cell(
    domain_id: int,
    domain_subfeature_id: int,
    domain_tool_id: int,
    support_level: str,
) -> None:
    await db.execute(
        """INSERT INTO t2_domain_matrix_cells 
           (domain_id, domain_subfeature_id, domain_tool_id, support_level)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(domain_id, domain_subfeature_id, domain_tool_id) 
           DO UPDATE SET support_level = excluded.support_level""",
        (domain_id, domain_subfeature_id, domain_tool_id, support_level)
    )
    await db.commit()


async def get_t2_domain_matrix_cells(domain_id: int) -> list[dict]:
    rows = await db.fetchall(
        "SELECT * FROM t2_domain_matrix_cells WHERE domain_id = ?",
        (domain_id,)
    )
    return [dict(row) for row in rows]


async def delete_t2_domain_matrix_cells(domain_id: int) -> None:
    await db.execute("DELETE FROM t2_domain_matrix_cells WHERE domain_id = ?", (domain_id,))
    await db.commit()


async def save_t2_worker_state(domain_id: int, state_json: str, current_step: str) -> None:
    await db.execute(
        """INSERT INTO t2_domain_worker_state (domain_id, state_json, current_step, updated_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(domain_id) DO UPDATE SET
             state_json = excluded.state_json,
             current_step = excluded.current_step,
             updated_at = CURRENT_TIMESTAMP""",
        (domain_id, state_json, current_step)
    )
    await db.commit()


async def load_t2_worker_state(domain_id: int) -> tuple[str, str] | None:
    row = await db.fetchone(
        "SELECT state_json, current_step FROM t2_domain_worker_state WHERE domain_id = ?",
        (domain_id,)
    )
    if row:
        return row["state_json"], row["current_step"]
    return None


async def delete_t2_worker_state(domain_id: int) -> None:
    await db.execute("DELETE FROM t2_domain_worker_state WHERE domain_id = ?", (domain_id,))
    await db.commit()


async def cleanup_t2_domain_data(domain_id: int) -> None:
    await delete_t2_domain_matrix_cells(domain_id)
    await delete_t2_domain_subfeatures(domain_id)
    await delete_t2_domain_features(domain_id)
    await delete_t2_domain_tools(domain_id)
    await delete_t2_worker_state(domain_id)
    await db.execute(
        "UPDATE t2_domain_rankings SET status = 'pending' WHERE domain_id = ?",
        (domain_id,)
    )
    await db.commit()
    logger.info(f"Cleaned up T2 data for domain_id={domain_id}")


async def get_all_tools_for_domain(domain_id: int) -> list[dict]:
    rows = await db.fetchall(
        """SELECT t.*, sd.name as subdomain_name, sd.id as subdomain_id
           FROM tools t
           JOIN subdomains sd ON t.subdomain_id = sd.id
           WHERE sd.domain_id = ?
           ORDER BY t.tool_type, t.product_name""",
        (domain_id,)
    )
    return [dict(row) for row in rows]


async def get_all_features_for_domain(domain_id: int) -> list[dict]:
    rows = await db.fetchall(
        """SELECT f.*, sd.name as subdomain_name, sd.id as subdomain_id
           FROM features f
           JOIN subdomains sd ON f.subdomain_id = sd.id
           WHERE sd.domain_id = ?
           ORDER BY sd.name, f.rank_order""",
        (domain_id,)
    )
    return [dict(row) for row in rows]


async def get_all_matrix_cells_for_domain(domain_id: int) -> list[dict]:
    rows = await db.fetchall(
        """SELECT mc.*, t.product_name, t.tool_type, sd.name as subdomain_name
           FROM matrix_cells mc
           JOIN subdomains sd ON mc.subdomain_id = sd.id
           JOIN tools t ON mc.tool_id = t.id
           WHERE sd.domain_id = ?""",
        (domain_id,)
    )
    return [dict(row) for row in rows]
