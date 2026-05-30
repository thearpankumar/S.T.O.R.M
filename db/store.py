import aiosqlite
import logging
import threading
from pathlib import Path
from typing import Any
from config.settings import settings
from db.schema import SCHEMA_SQL

logger = logging.getLogger(__name__)



class Database:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or settings.db_path
        self._local = threading.local()

    async def _get_conn(self) -> aiosqlite.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(self.db_path)
            conn.row_factory = aiosqlite.Row
            self._local.conn = conn
        return conn

    async def connect(self) -> None:
        conn = await self._get_conn()
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()

    async def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn:
            await conn.close()
            self._local.conn = None

    async def _executescript(self, script: str) -> None:
        conn = await self._get_conn()
        await conn.executescript(script)
        await conn.commit()

    async def execute(self, query: str, params: tuple = ()) -> Any:
        conn = await self._get_conn()
        return await conn.execute(query, params)

    async def fetchone(self, query: str, params: tuple = ()) -> aiosqlite.Row | None:
        conn = await self._get_conn()
        async with await conn.execute(query, params) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, query: str, params: tuple = ()) -> list[aiosqlite.Row]:
        conn = await self._get_conn()
        async with await conn.execute(query, params) as cursor:
            return await cursor.fetchall()

    async def commit(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn:
            await conn.commit()


db = Database()


async def init_db() -> None:
    await db.connect()
    await _seed_domains()
    await _reset_stale_statuses()


async def shutdown_db() -> None:
    await db.close()


async def _reset_stale_statuses() -> None:
    await db.execute(
        "UPDATE subdomains SET status = 'pending' WHERE status = 'running'"
    )
    await db.commit()


async def _seed_domains() -> None:
    from config.domains import CYBERSECURITY_DOMAINS

    for domain in CYBERSECURITY_DOMAINS:
        await db.execute(
            "INSERT OR IGNORE INTO domains (name) VALUES (?)",
            (domain,)
        )
    await db.commit()


async def get_domain_id(domain_name: str) -> int | None:
    row = await db.fetchone(
        "SELECT id FROM domains WHERE name = ?",
        (domain_name,)
    )
    return row["id"] if row else None


async def get_subdomains(domain_id: int, status: str | None = None) -> list[dict]:
    if status:
        rows = await db.fetchall(
            "SELECT * FROM subdomains WHERE domain_id = ? AND status = ?",
            (domain_id, status)
        )
    else:
        rows = await db.fetchall(
            "SELECT * FROM subdomains WHERE domain_id = ?",
            (domain_id,)
        )
    return [dict(row) for row in rows]


async def upsert_subdomain(domain_id: int, name: str, confidence_score: float = 1.0) -> int:
    await db.execute(
        """INSERT INTO subdomains (domain_id, name, confidence_score, status)
           VALUES (?, ?, ?, 'pending')
           ON CONFLICT(domain_id, name) DO UPDATE SET confidence_score = ?""",
        (domain_id, name, confidence_score, confidence_score)
    )
    await db.commit()
    row = await db.fetchone(
        "SELECT id FROM subdomains WHERE domain_id = ? AND name = ?",
        (domain_id, name)
    )
    return row["id"] if row else 0


async def update_subdomain_status(subdomain_id: int, status: str) -> None:
    await db.execute(
        "UPDATE subdomains SET status = ? WHERE id = ?",
        (status, subdomain_id)
    )
    await db.commit()


async def save_worker_state(subdomain_id: int, state_json: str, current_step: str) -> None:
    await db.execute(
        """INSERT INTO worker_state (subdomain_id, state_json, current_step, updated_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(subdomain_id) DO UPDATE SET 
             state_json = excluded.state_json,
             current_step = excluded.current_step,
             updated_at = CURRENT_TIMESTAMP""",
        (subdomain_id, state_json, current_step)
    )
    await db.commit()


async def load_worker_state(subdomain_id: int) -> tuple[str, str] | None:
    row = await db.fetchone(
        "SELECT state_json, current_step FROM worker_state WHERE subdomain_id = ?",
        (subdomain_id,)
    )
    if row:
        return row["state_json"], row["current_step"]
    return None


async def upsert_tool(subdomain_id: int, vendor: str, product_name: str, tool_type: str) -> int:
    await db.execute(
        """INSERT INTO tools (subdomain_id, vendor, product_name, tool_type)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(subdomain_id, product_name) DO UPDATE SET vendor = ?""",
        (subdomain_id, vendor, product_name, tool_type, vendor)
    )
    await db.commit()
    row = await db.fetchone(
        "SELECT id FROM tools WHERE subdomain_id = ? AND product_name = ?",
        (subdomain_id, product_name)
    )
    return row["id"] if row else 0


async def get_tools(subdomain_id: int) -> list[dict]:
    rows = await db.fetchall(
        "SELECT * FROM tools WHERE subdomain_id = ? ORDER BY tool_type, product_name",
        (subdomain_id,)
    )
    return [dict(row) for row in rows]


async def upsert_feature(subdomain_id: int, name: str, rank_order: int) -> int:
    await db.execute(
        """INSERT INTO features (subdomain_id, name, rank_order)
           VALUES (?, ?, ?)
           ON CONFLICT(subdomain_id, name) DO UPDATE SET rank_order = ?""",
        (subdomain_id, name, rank_order, rank_order)
    )
    await db.commit()
    row = await db.fetchone(
        "SELECT id FROM features WHERE subdomain_id = ? AND name = ?",
        (subdomain_id, name)
    )
    return row["id"] if row else 0


async def get_features(subdomain_id: int) -> list[dict]:
    rows = await db.fetchall(
        "SELECT * FROM features WHERE subdomain_id = ? ORDER BY rank_order",
        (subdomain_id,)
    )
    return [dict(row) for row in rows]


async def upsert_subfeature(feature_id: int, name: str, rank_order: int) -> int:
    await db.execute(
        """INSERT INTO subfeatures (feature_id, name, rank_order)
           VALUES (?, ?, ?)
           ON CONFLICT(feature_id, name) DO UPDATE SET rank_order = ?""",
        (feature_id, name, rank_order, rank_order)
    )
    await db.commit()
    row = await db.fetchone(
        "SELECT id FROM subfeatures WHERE feature_id = ? AND name = ?",
        (feature_id, name)
    )
    return row["id"] if row else 0


async def get_subfeatures(feature_id: int) -> list[dict]:
    rows = await db.fetchall(
        "SELECT * FROM subfeatures WHERE feature_id = ? ORDER BY rank_order",
        (feature_id,)
    )
    return [dict(row) for row in rows]


async def upsert_matrix_cell(subdomain_id: int, subfeature_id: int, tool_id: int, support_level: str) -> None:
    await db.execute(
        """INSERT INTO matrix_cells (subdomain_id, subfeature_id, tool_id, support_level)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(subdomain_id, subfeature_id, tool_id) DO UPDATE SET support_level = ?""",
        (subdomain_id, subfeature_id, tool_id, support_level, support_level)
    )
    await db.commit()


async def get_matrix_cells(subdomain_id: int) -> list[dict]:
    rows = await db.fetchall(
        "SELECT * FROM matrix_cells WHERE subdomain_id = ?",
        (subdomain_id,)
    )
    return [dict(row) for row in rows]


async def cleanup_subdomain_data(subdomain_id: int) -> None:
    """
    Delete ALL partial data written for a subdomain (matrix_cells, subfeatures,
    features, tools, worker_state) and reset its status to 'pending' so it can
    be safely re-queued without leaving orphaned rows.

    Deletion order respects FK constraints:
      matrix_cells → subfeatures (via feature_id) → features → tools → worker_state
    """
    # 1. matrix cells
    await db.execute(
        "DELETE FROM matrix_cells WHERE subdomain_id = ?",
        (subdomain_id,)
    )

    # 2. subfeatures (must go via feature ids because subfeatures FK → features)
    feature_rows = await db.fetchall(
        "SELECT id FROM features WHERE subdomain_id = ?",
        (subdomain_id,)
    )
    for row in feature_rows:
        await db.execute(
            "DELETE FROM subfeatures WHERE feature_id = ?",
            (row["id"],)
        )

    # 3. features
    await db.execute(
        "DELETE FROM features WHERE subdomain_id = ?",
        (subdomain_id,)
    )

    # 4. tools
    await db.execute(
        "DELETE FROM tools WHERE subdomain_id = ?",
        (subdomain_id,)
    )

    # 5. worker state checkpoint
    await db.execute(
        "DELETE FROM worker_state WHERE subdomain_id = ?",
        (subdomain_id,)
    )

    # 6. reset status → pending so it can be retried cleanly
    await db.execute(
        "UPDATE subdomains SET status = 'pending' WHERE id = ?",
        (subdomain_id,)
    )

    await db.commit()
    logger.info(f"Cleaned up partial data for subdomain_id={subdomain_id} → reset to pending")
