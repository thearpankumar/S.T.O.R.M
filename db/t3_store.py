"""
db/t3_store.py — Async DB access layer for Technique 3 (cross-domain tool classification).

Tables used:
  t3_tools              — deduplicated canonical tool registry
  t3_tool_subdomains    — M2M: tool <-> subdomain membership
  t3_classification_runs — single-row pipeline run tracker
"""

import json
import logging
from typing import Any

from db.store import db

logger = logging.getLogger(__name__)


# ── Canonical tool registry ────────────────────────────────────────────────────

async def upsert_t3_tool(
    vendor: str,
    product_name: str,
    tool_type: str,
    nist_functions: list[str] | None = None,
    nist_primary_function: str | None = None,
    _commit: bool = False,
) -> int:
    """Insert or update a canonical tool entry. Returns the t3_tools.id.
    
    Set _commit=True only when calling standalone; the bulk dedup stage
    defers commits to avoid 2000 individual disk flushes.
    """
    nist_json = json.dumps(nist_functions) if nist_functions else None
    await db.execute(
        """INSERT INTO t3_tools
               (vendor, product_name, tool_type, nist_functions, nist_primary_function, updated_at)
           VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(vendor, product_name) DO UPDATE SET
               tool_type             = excluded.tool_type,
               nist_functions        = COALESCE(excluded.nist_functions, nist_functions),
               nist_primary_function = COALESCE(excluded.nist_primary_function, nist_primary_function),
               updated_at            = CURRENT_TIMESTAMP""",
        (vendor, product_name, tool_type, nist_json, nist_primary_function),
    )
    if _commit:
        await db.commit()
    row = await db.fetchone(
        "SELECT id FROM t3_tools WHERE vendor = ? AND product_name = ?",
        (vendor, product_name),
    )
    return row["id"] if row else 0


async def update_t3_tool_nist(
    t3_tool_id: int,
    nist_functions: list[str],
    nist_primary_function: str,
    _commit: bool = False,
) -> None:
    """Update only the NIST fields on an existing t3_tools row.
    
    Set _commit=True only when calling standalone; bulk stage defers commits.
    """
    await db.execute(
        """UPDATE t3_tools
           SET nist_functions = ?, nist_primary_function = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (json.dumps(nist_functions), nist_primary_function, t3_tool_id),
    )
    if _commit:
        await db.commit()


async def bulk_update_t3_tool_nist(
    updates: list[tuple[int, list[str], str]],  # [(t3_tool_id, nist_functions, nist_primary), ...]
) -> None:
    """Update NIST fields for many tools in a single transaction."""
    conn = await db._get_conn()
    await conn.executemany(
        """UPDATE t3_tools
           SET nist_functions = ?, nist_primary_function = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        [(json.dumps(funcs), primary, tid) for tid, funcs, primary in updates],
    )
    await conn.commit()


async def update_t3_tool_coverage_counts(t3_tool_id: int) -> None:
    """Recompute and store domain_count + subdomain_count for a tool from the join table."""
    await db.execute(
        """UPDATE t3_tools SET
               subdomain_count = (
                   SELECT COUNT(*) FROM t3_tool_subdomains WHERE t3_tool_id = ?
               ),
               domain_count = (
                   SELECT COUNT(DISTINCT domain_id) FROM t3_tool_subdomains WHERE t3_tool_id = ?
               )
           WHERE id = ?""",
        (t3_tool_id, t3_tool_id, t3_tool_id),
    )
    await db.commit()


# ── Subdomain membership M2M ───────────────────────────────────────────────────

async def link_t3_tool_subdomain(
    t3_tool_id: int,
    subdomain_id: int,
    domain_id: int,
) -> None:
    """Create one membership link between a canonical tool and a subdomain."""
    await db.execute(
        """INSERT OR IGNORE INTO t3_tool_subdomains (t3_tool_id, subdomain_id, domain_id)
           VALUES (?, ?, ?)""",
        (t3_tool_id, subdomain_id, domain_id),
    )
    await db.commit()


async def link_t3_tool_subdomains_bulk(
    t3_tool_id: int,
    memberships: list[tuple[int, int]],  # [(subdomain_id, domain_id), ...]
    _commit: bool = False,
) -> None:
    """Bulk-insert subdomain membership links for one tool via executemany."""
    if not memberships:
        return
    conn = await db._get_conn()
    await conn.executemany(
        """INSERT OR IGNORE INTO t3_tool_subdomains (t3_tool_id, subdomain_id, domain_id)
           VALUES (?, ?, ?)""",
        [(t3_tool_id, sd_id, d_id) for sd_id, d_id in memberships],
    )
    if _commit:
        await conn.commit()


# ── Source data queries ────────────────────────────────────────────────────────

async def get_all_unique_tools_from_t1() -> list[dict[str, Any]]:
    """
    Fetch every unique (vendor, product_name) pair from the T1 tools table,
    collecting all subdomain and domain memberships per tool.

    Returns a list of dicts:
      {
        "vendor": str,
        "product_name": str,
        "tool_type": str,   # 'enterprise' | 'opensource'
        "memberships": [(subdomain_id, subdomain_name, domain_id, domain_name), ...]
      }
    """
    rows = await db.fetchall(
        """SELECT
               t.vendor,
               t.product_name,
               t.tool_type,
               sd.id   AS subdomain_id,
               sd.name AS subdomain_name,
               d.id    AS domain_id,
               d.name  AS domain_name
           FROM tools t
           JOIN subdomains sd ON sd.id = t.subdomain_id
           JOIN domains    d  ON d.id  = sd.domain_id
           WHERE sd.status = 'done'
           ORDER BY t.vendor, t.product_name, d.name, sd.name""",
    )

    # Group by (vendor, product_name)
    tool_map: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["vendor"], row["product_name"])
        if key not in tool_map:
            tool_map[key] = {
                "vendor":       row["vendor"],
                "product_name": row["product_name"],
                "tool_type":    row["tool_type"],
                "memberships":  [],
            }
        tool_map[key]["memberships"].append(
            (row["subdomain_id"], row["subdomain_name"], row["domain_id"], row["domain_name"])
        )

    return list(tool_map.values())


# ── Read queries for TUI + Excel ───────────────────────────────────────────────

async def get_t3_tools_with_coverage(
    filter_domain_id: int | None = None,
    filter_nist: str | None = None,
    filter_type: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return all classified T3 tools with their coverage counts and NIST data.
    Optionally filter by domain_id, NIST primary function, or tool_type.
    """
    # Build the WHERE clause dynamically
    conditions: list[str] = []
    params: list[Any] = []

    if filter_domain_id is not None:
        conditions.append(
            "EXISTS (SELECT 1 FROM t3_tool_subdomains tts "
            "WHERE tts.t3_tool_id = t.id AND tts.domain_id = ?)"
        )
        params.append(filter_domain_id)

    if filter_nist:
        # nist_primary_function exact match OR nist_functions JSON contains the value
        conditions.append(
            "(t.nist_primary_function = ? OR t.nist_functions LIKE ?)"
        )
        params.extend([filter_nist, f'%"{filter_nist}"%'])

    if filter_type:
        conditions.append("t.tool_type = ?")
        params.append(filter_type)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = await db.fetchall(
        f"""SELECT
               t.id,
               t.vendor,
               t.product_name,
               t.tool_type,
               t.nist_functions,
               t.nist_primary_function,
               t.domain_count,
               t.subdomain_count
           FROM t3_tools t
           {where_clause}
           ORDER BY t.domain_count DESC, t.subdomain_count DESC, t.vendor, t.product_name""",
        tuple(params),
    )
    return [dict(row) for row in rows]


async def get_t3_tool_memberships(t3_tool_id: int) -> list[dict[str, Any]]:
    """Return all subdomain + domain memberships for a single T3 tool."""
    rows = await db.fetchall(
        """SELECT
               tts.subdomain_id,
               sd.name  AS subdomain_name,
               tts.domain_id,
               d.name   AS domain_name
           FROM t3_tool_subdomains tts
           JOIN subdomains sd ON sd.id = tts.subdomain_id
           JOIN domains    d  ON d.id  = tts.domain_id
           WHERE tts.t3_tool_id = ?
           ORDER BY d.name, sd.name""",
        (t3_tool_id,),
    )
    return [dict(row) for row in rows]


async def get_t3_domain_list() -> list[dict[str, Any]]:
    """Return all domains that have at least one T3 tool, for filter dropdowns."""
    rows = await db.fetchall(
        """SELECT DISTINCT d.id, d.name
           FROM t3_tool_subdomains tts
           JOIN domains d ON d.id = tts.domain_id
           ORDER BY d.name""",
    )
    return [dict(row) for row in rows]


async def get_all_t3_tool_memberships() -> dict[int, list[dict[str, Any]]]:
    """Fetch ALL tool memberships in one query — eliminates N+1 for Excel export.
    
    Returns: {t3_tool_id: [{subdomain_id, subdomain_name, domain_id, domain_name}, ...]}
    """
    rows = await db.fetchall(
        """SELECT
               tts.t3_tool_id,
               tts.subdomain_id,
               sd.name  AS subdomain_name,
               tts.domain_id,
               d.name   AS domain_name
           FROM t3_tool_subdomains tts
           JOIN subdomains sd ON sd.id = tts.subdomain_id
           JOIN domains    d  ON d.id  = tts.domain_id
           ORDER BY tts.t3_tool_id, d.name, sd.name"""
    )
    result: dict[int, list[dict]] = {}
    for row in rows:
        tid = row["t3_tool_id"]
        if tid not in result:
            result[tid] = []
        result[tid].append({
            "subdomain_id":   row["subdomain_id"],
            "subdomain_name": row["subdomain_name"],
            "domain_id":      row["domain_id"],
            "domain_name":    row["domain_name"],
        })
    return result


async def get_t3_stats() -> dict[str, Any]:
    """Aggregate statistics for the T3 summary view."""
    total = await db.fetchone("SELECT COUNT(*) AS n FROM t3_tools")
    enterprise = await db.fetchone(
        "SELECT COUNT(*) AS n FROM t3_tools WHERE tool_type = 'enterprise'"
    )
    opensource = await db.fetchone(
        "SELECT COUNT(*) AS n FROM t3_tools WHERE tool_type = 'opensource'"
    )
    multi_domain = await db.fetchone(
        "SELECT COUNT(*) AS n FROM t3_tools WHERE domain_count > 1"
    )
    top_tool = await db.fetchone(
        "SELECT vendor, product_name, domain_count FROM t3_tools ORDER BY domain_count DESC LIMIT 1"
    )

    nist_counts: dict[str, int] = {}
    for func in ("ID", "PR", "DE", "RS", "RC", "GV"):
        row = await db.fetchone(
            "SELECT COUNT(*) AS n FROM t3_tools WHERE nist_functions LIKE ?",
            (f'%"{func}"%',),
        )
        nist_counts[func] = row["n"] if row else 0

    return {
        "total":        total["n"] if total else 0,
        "enterprise":   enterprise["n"] if enterprise else 0,
        "opensource":   opensource["n"] if opensource else 0,
        "multi_domain": multi_domain["n"] if multi_domain else 0,
        "top_tool":     dict(top_tool) if top_tool else None,
        "nist_counts":  nist_counts,
    }


# ── Classification run tracker ─────────────────────────────────────────────────

async def get_t3_run_status() -> dict[str, Any] | None:
    """Return the latest classification run record, or None if none exists."""
    row = await db.fetchone(
        "SELECT * FROM t3_classification_runs ORDER BY created_at DESC LIMIT 1"
    )
    return dict(row) if row else None


async def upsert_t3_run_status(
    status: str,
    total_tools: int = 0,
    classified_tools: int = 0,
) -> int:
    """
    Create or update the single classification run row.
    Returns the run id.
    """
    existing = await db.fetchone(
        "SELECT id FROM t3_classification_runs ORDER BY created_at DESC LIMIT 1"
    )
    if existing:
        run_id = existing["id"]
        started_sql = "started_at = CASE WHEN ? = 'running' THEN CURRENT_TIMESTAMP ELSE started_at END"
        completed_sql = "completed_at = CASE WHEN ? IN ('done','failed') THEN CURRENT_TIMESTAMP ELSE completed_at END"
        await db.execute(
            f"""UPDATE t3_classification_runs
                SET status = ?,
                    total_tools = ?,
                    classified_tools = ?,
                    {started_sql},
                    {completed_sql}
                WHERE id = ?""",
            (status, total_tools, classified_tools, status, status, run_id),
        )
    else:
        await db.execute(
            """INSERT INTO t3_classification_runs
                   (status, total_tools, classified_tools, started_at)
               VALUES (?, ?, ?, CASE WHEN ? = 'running' THEN CURRENT_TIMESTAMP ELSE NULL END)""",
            (status, total_tools, classified_tools, status),
        )
        row = await db.fetchone(
            "SELECT id FROM t3_classification_runs ORDER BY created_at DESC LIMIT 1"
        )
        run_id = row["id"] if row else 0
    await db.commit()
    return run_id


async def reset_t3_data() -> None:
    """
    Wipe all T3 classification data so the pipeline can be re-run from scratch.
    Deletes t3_tool_subdomains first (FK), then t3_tools, then resets run status.
    """
    await db.execute("DELETE FROM t3_tool_subdomains")
    await db.execute("DELETE FROM t3_tools")
    await db.execute("DELETE FROM t3_classification_runs")
    await db.commit()
    logger.info("T3 data reset — ready for fresh classification run")

async def update_t3_executive_summary(summary: str) -> None:
    """Save the LLM-generated executive summary to the most recent run."""
    existing = await db.fetchone(
        "SELECT id FROM t3_classification_runs ORDER BY created_at DESC LIMIT 1"
    )
    if existing:
        await db.execute(
            "UPDATE t3_classification_runs SET executive_summary = ? WHERE id = ?",
            (summary, existing["id"])
        )
        await db.commit()
