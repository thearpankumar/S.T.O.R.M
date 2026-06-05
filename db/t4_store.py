"""
db/t4_store.py - Async DB access layer for Technique 4 (Tool-Level Cross-Domain Analysis).
"""

import json
import logging
from typing import Any

from db.store import db

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Canonical Tool Registry (T4)
# ═══════════════════════════════════════════════════════════════════════════

async def upsert_t4_tool(
    vendor: str,
    product_name: str,
    tool_type: str = "unknown",
    license_model: str | None = None,
    url: str | None = None,
    description: str | None = None,
    source_type: str = "t1",
    _commit: bool = True,
) -> int:
    """Insert or update a canonical T4 tool entry. Returns the t4_tools.id."""
    await db.execute(
        """INSERT INTO t4_tools
               (vendor, product_name, tool_type, license_model, url, description, source_type, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(vendor, product_name) DO UPDATE SET
               tool_type    = COALESCE(excluded.tool_type, tool_type),
               license_model= COALESCE(excluded.license_model, license_model),
               url          = COALESCE(excluded.url, url),
               description  = COALESCE(excluded.description, description),
               updated_at   = CURRENT_TIMESTAMP""",
        (vendor, product_name, tool_type, license_model, url, description, source_type),
    )
    if _commit:
        await db.commit()
    row = await db.fetchone(
        "SELECT id FROM t4_tools WHERE vendor = ? AND product_name = ?",
        (vendor, product_name),
    )
    return row["id"] if row else 0


async def update_t4_tool_enrichment(
    t4_tool_id: int,
    license_model: str,
    url: str | None = None,
    description: str | None = None,
    _commit: bool = True,
) -> None:
    """Update only the enrichment fields on a T4 tool."""
    await db.execute(
        """UPDATE t4_tools
           SET license_model = ?, url = ?, description = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (license_model, url, description, t4_tool_id),
    )
    if _commit:
        await db.commit()


async def get_stub_t4_tools() -> list[dict[str, Any]]:
    """Fetch all T4 tools that have not yet been enriched (license_model IS NULL)."""
    rows = await db.fetchall(
        """SELECT id, vendor, product_name, tool_type, source_type
           FROM t4_tools
           WHERE license_model IS NULL
           ORDER BY vendor, product_name"""
    )
    return [dict(row) for row in rows]


# ═══════════════════════════════════════════════════════════════════════════
# Tool → Subdomain Membership
# ═══════════════════════════════════════════════════════════════════════════

async def link_t4_tool_subdomain(
    t4_tool_id: int,
    subdomain_id: int,
    domain_id: int,
    t1_tool_id: int,
    _commit: bool = True,
) -> None:
    """Create one membership link."""
    await db.execute(
        """INSERT OR IGNORE INTO t4_tool_subdomains
               (t4_tool_id, subdomain_id, domain_id, t1_tool_id)
           VALUES (?, ?, ?, ?)""",
        (t4_tool_id, subdomain_id, domain_id, t1_tool_id),
    )
    if _commit:
        await db.commit()


async def link_t4_tool_subdomains_bulk(
    t4_tool_id: int,
    memberships: list[tuple[int, int, int]],  # [(subdomain_id, domain_id, t1_tool_id), ...]
    _commit: bool = True,
) -> None:
    """Bulk-insert subdomain membership links for one tool."""
    if not memberships:
        return
    conn = await db._get_conn()
    await conn.executemany(
        """INSERT OR IGNORE INTO t4_tool_subdomains (t4_tool_id, subdomain_id, domain_id, t1_tool_id)
           VALUES (?, ?, ?, ?)""",
        [(t4_tool_id, sd_id, d_id, t1_id) for sd_id, d_id, t1_id in memberships],
    )
    if _commit:
        await conn.commit()


async def get_t4_tool_subdomain_memberships(t4_tool_id: int) -> list[dict[str, Any]]:
    """Return all subdomain memberships for a T4 tool."""
    rows = await db.fetchall(
        """SELECT tts.subdomain_id, sd.name AS subdomain_name,
                  tts.domain_id, d.name AS domain_name, tts.t1_tool_id
           FROM t4_tool_subdomains tts
           JOIN subdomains sd ON sd.id = tts.subdomain_id
           JOIN domains d ON d.id = tts.domain_id
           WHERE tts.t4_tool_id = ?
           ORDER BY d.name, sd.name""",
        (t4_tool_id,),
    )
    return [dict(row) for row in rows]


# ═══════════════════════════════════════════════════════════════════════════
# Tool → Domain Aggregation
# ═══════════════════════════════════════════════════════════════════════════

async def update_t4_tool_domains(
    t4_tool_id: int,
    primary_domain_id: int | None,
    domain_count: int,
    subdomain_count: int,
    domain_list: list[str],
    _commit: bool = True,
) -> None:
    """Upsert domain aggregation for a tool."""
    domain_list_json = json.dumps(domain_list)
    await db.execute(
        """INSERT INTO t4_tool_domains
               (t4_tool_id, primary_domain_id, domain_count, subdomain_count, domain_list, updated_at)
           VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(t4_tool_id) DO UPDATE SET
               primary_domain_id = excluded.primary_domain_id,
               domain_count      = excluded.domain_count,
               subdomain_count   = excluded.subdomain_count,
               domain_list       = excluded.domain_list,
               updated_at        = CURRENT_TIMESTAMP""",
        (t4_tool_id, primary_domain_id, domain_count, subdomain_count, domain_list_json),
    )
    if _commit:
        await db.commit()


async def get_t4_tool_domains(t4_tool_id: int) -> dict[str, Any] | None:
    """Return domain aggregation for a T4 tool."""
    row = await db.fetchone(
        "SELECT * FROM t4_tool_domains WHERE t4_tool_id = ?",
        (t4_tool_id,),
    )
    result = dict(row) if row else None
    if result and result.get("domain_list"):
        try:
            result["domain_list"] = json.loads(result["domain_list"])
        except (json.JSONDecodeError, TypeError):
            pass
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Tool → Feature Support Aggregation
# ═══════════════════════════════════════════════════════════════════════════

async def update_t4_tool_features(
    t4_tool_id: int,
    total_subfeatures: int,
    supported_subfeatures: int,
    partial_subfeatures: int,
    unsupported_subfeatures: int,
    support_rate: float,
    _commit: bool = True,
) -> None:
    """Upsert feature support aggregation for a tool."""
    await db.execute(
        """INSERT INTO t4_tool_features
               (t4_tool_id, total_subfeatures, supported_subfeatures,
                partial_subfeatures, unsupported_subfeatures, support_rate, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(t4_tool_id) DO UPDATE SET
               total_subfeatures     = excluded.total_subfeatures,
               supported_subfeatures = excluded.supported_subfeatures,
               partial_subfeatures   = excluded.partial_subfeatures,
               unsupported_subfeatures = excluded.unsupported_subfeatures,
               support_rate          = excluded.support_rate,
               updated_at            = CURRENT_TIMESTAMP""",
        (t4_tool_id, total_subfeatures, supported_subfeatures,
         partial_subfeatures, unsupported_subfeatures, support_rate),
    )
    if _commit:
        await db.commit()


async def update_t4_tool_subdomain_features(
    t4_tool_id: int,
    subdomain_id: int,
    domain_id: int,
    total_subfeatures: int,
    supported_subfeatures: int,
    partial_subfeatures: int,
    support_pct: float,
    support_level: str,
    _commit: bool = True,
) -> None:
    """Upsert per-tool, per-subdomain feature breakdown."""
    await db.execute(
        """INSERT INTO t4_tool_subdomain_features
               (t4_tool_id, subdomain_id, domain_id, total_subfeatures,
                supported_subfeatures, partial_subfeatures, support_pct, support_level)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(t4_tool_id, subdomain_id) DO UPDATE SET
               total_subfeatures     = excluded.total_subfeatures,
               supported_subfeatures = excluded.supported_subfeatures,
               partial_subfeatures   = excluded.partial_subfeatures,
               support_pct           = excluded.support_pct,
               support_level         = excluded.support_level""",
        (t4_tool_id, subdomain_id, domain_id, total_subfeatures,
         supported_subfeatures, partial_subfeatures, support_pct, support_level),
    )
    if _commit:
        await db.commit()


def _validate_tuple_length(tuples: list[tuple], expected_len: int, name: str) -> None:
    """Validate that all tuples have expected length."""
    for i, t in enumerate(tuples):
        if len(t) != expected_len:
            raise ValueError(f"{name} at index {i} has {len(t)} elements, expected {expected_len}")


async def bulk_update_t4_tool_subdomain_features(
    updates: list[tuple],
) -> None:
    """Bulk upsert per-tool, per-subdomain feature breakdown.
    
    Args:
        updates: List of tuples (t4_tool_id, subdomain_id, domain_id, 
                 total_subfeatures, supported_subfeatures, partial_subfeatures, 
                 support_pct, support_level)
    """
    if not updates:
        return
    _validate_tuple_length(updates, 8, "Feature subdomain update")
    conn = await db._get_conn()
    await conn.executemany(
        """INSERT INTO t4_tool_subdomain_features
               (t4_tool_id, subdomain_id, domain_id, total_subfeatures,
                supported_subfeatures, partial_subfeatures, support_pct, support_level)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(t4_tool_id, subdomain_id) DO UPDATE SET
               total_subfeatures     = excluded.total_subfeatures,
               supported_subfeatures = excluded.supported_subfeatures,
               partial_subfeatures   = excluded.partial_subfeatures,
               support_pct           = excluded.support_pct,
               support_level         = excluded.support_level""",
        updates,
    )
    await conn.commit()


async def bulk_update_t4_tool_features(
    updates: list[tuple],
) -> None:
    """Bulk upsert feature support aggregation for tools.
    
    Args:
        updates: List of tuples (t4_tool_id, total_subfeatures, supported_subfeatures,
                 partial_subfeatures, unsupported_subfeatures, support_rate)
    """
    if not updates:
        return
    _validate_tuple_length(updates, 6, "Feature update")
    conn = await db._get_conn()
    await conn.executemany(
        """INSERT INTO t4_tool_features
               (t4_tool_id, total_subfeatures, supported_subfeatures,
                partial_subfeatures, unsupported_subfeatures, support_rate, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(t4_tool_id) DO UPDATE SET
               total_subfeatures     = excluded.total_subfeatures,
               supported_subfeatures = excluded.supported_subfeatures,
               partial_subfeatures   = excluded.partial_subfeatures,
               unsupported_subfeatures = excluded.unsupported_subfeatures,
               support_rate          = excluded.support_rate,
               updated_at            = CURRENT_TIMESTAMP""",
        updates,
    )
    await conn.commit()


async def bulk_update_t4_tool_domains(
    updates: list[tuple],
) -> None:
    """Bulk upsert domain aggregation for tools.
    
    Args:
        updates: List of tuples (t4_tool_id, primary_domain_id, domain_count, 
                 subdomain_count, domain_list_json)
    """
    if not updates:
        return
    _validate_tuple_length(updates, 5, "Domain update")
    conn = await db._get_conn()
    await conn.executemany(
        """INSERT INTO t4_tool_domains
               (t4_tool_id, primary_domain_id, domain_count, subdomain_count, 
                domain_list, updated_at)
           VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(t4_tool_id) DO UPDATE SET
               primary_domain_id = excluded.primary_domain_id,
               domain_count      = excluded.domain_count,
               subdomain_count   = excluded.subdomain_count,
               domain_list       = excluded.domain_list,
               updated_at        = CURRENT_TIMESTAMP""",
        updates,
    )
    await conn.commit()


async def bulk_update_t4_tool_enrichment(
    updates: list[tuple],
) -> None:
    """Bulk update enrichment fields for tools.
    
    Args:
        updates: List of tuples (t4_tool_id, license_model, url, description)
    """
    if not updates:
        return
    _validate_tuple_length(updates, 4, "Enrichment update")
    conn = await db._get_conn()
    await conn.executemany(
        """UPDATE t4_tools
           SET license_model = ?, url = ?, description = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        [(lic, url, desc, tid) for tid, lic, url, desc in updates],
    )
    await conn.commit()


async def bulk_upsert_t4_tools(
    tools: list[tuple],
) -> None:
    """Bulk insert or update canonical T4 tools.
    
    Args:
        tools: List of tuples (vendor, product_name, tool_type)
    """
    if not tools:
        return
    _validate_tuple_length(tools, 3, "Tool upsert")
    conn = await db._get_conn()
    await conn.executemany(
        """INSERT INTO t4_tools
               (vendor, product_name, tool_type, source_type, updated_at)
           VALUES (?, ?, ?, 't1', CURRENT_TIMESTAMP)
           ON CONFLICT(vendor, product_name) DO UPDATE SET
               tool_type = COALESCE(excluded.tool_type, tool_type),
               updated_at = CURRENT_TIMESTAMP""",
        tools,
    )
    await conn.commit()


async def link_t4_tool_subdomains_bulk_batch(
    links: list[tuple],
) -> None:
    """Bulk insert subdomain membership links for all tools.
    
    Args:
        links: List of tuples (t4_tool_id, subdomain_id, domain_id, t1_tool_id)
    """
    if not links:
        return
    _validate_tuple_length(links, 4, "Subdomain link")
    conn = await db._get_conn()
    await conn.executemany(
        """INSERT OR IGNORE INTO t4_tool_subdomains
               (t4_tool_id, subdomain_id, domain_id, t1_tool_id)
           VALUES (?, ?, ?, ?)""",
        links,
    )
    await conn.commit()


async def get_all_t4_tool_subdomain_features() -> dict[int, list[dict[str, Any]]]:
    """Fetch ALL tool subdomain features in one query - eliminates N+1 for Excel export.
    
    Returns: {t4_tool_id: [{subdomain_id, subdomain_name, domain_id, domain_name, ...}, ...]}
    """
    rows = await db.fetchall(
        """SELECT ttsf.t4_tool_id, ttsf.subdomain_id, ttsf.domain_id,
                  ttsf.total_subfeatures, ttsf.supported_subfeatures,
                  ttsf.partial_subfeatures, ttsf.support_pct, ttsf.support_level,
                  sd.name AS subdomain_name, d.name AS domain_name
           FROM t4_tool_subdomain_features ttsf
           JOIN subdomains sd ON sd.id = ttsf.subdomain_id
           JOIN domains d ON d.id = ttsf.domain_id
           ORDER BY ttsf.t4_tool_id, d.name, sd.name"""
    )
    result: dict[int, list[dict]] = {}
    for row in rows:
        tid = row["t4_tool_id"]
        if tid not in result:
            result[tid] = []
        result[tid].append({
            "t4_tool_id": row["t4_tool_id"],
            "subdomain_id": row["subdomain_id"],
            "domain_id": row["domain_id"],
            "total_subfeatures": row["total_subfeatures"],
            "supported_subfeatures": row["supported_subfeatures"],
            "partial_subfeatures": row["partial_subfeatures"],
            "support_pct": row["support_pct"],
            "support_level": row["support_level"],
            "subdomain_name": row["subdomain_name"],
            "domain_name": row["domain_name"],
        })
    return result


async def get_t4_tool_features(t4_tool_id: int) -> dict[str, Any] | None:
    """Return feature support aggregation for a T4 tool."""
    row = await db.fetchone(
        "SELECT * FROM t4_tool_features WHERE t4_tool_id = ?",
        (t4_tool_id,),
    )
    return dict(row) if row else None


async def get_t4_tool_subdomain_features_list(t4_tool_id: int) -> list[dict[str, Any]]:
    """Return per-subdomain feature breakdown for a T4 tool."""
    rows = await db.fetchall(
        """SELECT ttsf.*, sd.name AS subdomain_name, d.name AS domain_name
           FROM t4_tool_subdomain_features ttsf
           JOIN subdomains sd ON sd.id = ttsf.subdomain_id
           JOIN domains d ON d.id = ttsf.domain_id
           WHERE ttsf.t4_tool_id = ?
           ORDER BY d.name, sd.name""",
        (t4_tool_id,),
    )
    return [dict(row) for row in rows]


# ═══════════════════════════════════════════════════════════════════════════
# Read Queries for TUI + Excel
# ═══════════════════════════════════════════════════════════════════════════

async def get_t4_tools_with_coverage(
    filter_domain_id: int | None = None,
    filter_license: str | None = None,
    filter_type: str | None = None,
    min_domain_count: int = 1,
) -> list[dict[str, Any]]:
    """
    Return all T4 tools with their domain/feature coverage.
    Optional filters: domain_id, license_model, tool_type, min_domain_count.
    """
    conditions: list[str] = ["td.domain_count >= ?"]
    params: list[Any] = [min_domain_count]

    if filter_domain_id is not None:
        conditions.append(
            "EXISTS (SELECT 1 FROM t4_tool_subdomains tts "
            "WHERE tts.t4_tool_id = t.id AND tts.domain_id = ?)"
        )
        params.append(filter_domain_id)

    if filter_license:
        conditions.append("t.license_model = ?")
        params.append(filter_license)

    if filter_type:
        conditions.append("t.tool_type = ?")
        params.append(filter_type)

    where_clause = "WHERE " + " AND ".join(conditions)

    rows = await db.fetchall(
        f"""SELECT
               t.id,
               t.vendor,
               t.product_name,
               t.tool_type,
               t.license_model,
               t.url,
               t.description,
               COALESCE(td.domain_count, 0) AS domain_count,
               COALESCE(td.subdomain_count, 0) AS subdomain_count,
               COALESCE(td.domain_list, '[]') AS domain_list,
               COALESCE(tf.total_subfeatures, 0) AS total_subfeatures,
               COALESCE(tf.supported_subfeatures, 0) AS supported_subfeatures,
               COALESCE(tf.partial_subfeatures, 0) AS partial_subfeatures,
               COALESCE(tf.unsupported_subfeatures, 0) AS unsupported_subfeatures,
               COALESCE(tf.support_rate, 0.0) AS support_rate,
               td.primary_domain_id
           FROM t4_tools t
           LEFT JOIN t4_tool_domains td ON td.t4_tool_id = t.id
           LEFT JOIN t4_tool_features tf ON tf.t4_tool_id = t.id
           {where_clause}
           ORDER BY td.domain_count DESC, t.vendor, t.product_name""",
        tuple(params),
    )
    
    results = []
    for row in rows:
        result = dict(row)
        if result.get("domain_list"):
            try:
                result["domain_list"] = json.loads(result["domain_list"])
            except (json.JSONDecodeError, TypeError):
                result["domain_list"] = []
        results.append(result)
    
    return results


async def get_t4_stats() -> dict[str, Any]:
    """Aggregate statistics for T4 summary view."""
    total = await db.fetchone("SELECT COUNT(*) AS n FROM t4_tools")
    enterprise = await db.fetchone(
        "SELECT COUNT(*) AS n FROM t4_tools WHERE tool_type = 'enterprise'"
    )
    opensource = await db.fetchone(
        "SELECT COUNT(*) AS n FROM t4_tools WHERE tool_type = 'opensource'"
    )
    freemium = await db.fetchone(
        "SELECT COUNT(*) AS n FROM t4_tools WHERE tool_type = 'freemium'"
    )
    multi_domain = await db.fetchone(
        """SELECT COUNT(*) AS n FROM t4_tool_domains WHERE domain_count > 1"""
    )
    
    top_tool = await db.fetchone(
        """SELECT t.vendor, t.product_name, td.domain_count, td.subdomain_count
           FROM t4_tools t
           JOIN t4_tool_domains td ON td.t4_tool_id = t.id
           ORDER BY td.domain_count DESC
           LIMIT 1"""
    )
    
    license_rows = await db.fetchall(
        "SELECT DISTINCT license_model FROM t4_tools WHERE license_model IS NOT NULL"
    )
    license_counts: dict[str, int] = {}
    existing_licenses = {r["license_model"] for r in license_rows}
    
    from models.t4_tool import LICENSE_MODELS
    for lic in LICENSE_MODELS:
        if lic in existing_licenses:
            row = await db.fetchone(
                "SELECT COUNT(*) AS n FROM t4_tools WHERE license_model = ?",
                (lic,),
            )
            license_counts[lic] = row["n"] if row else 0
    
    avg_support = await db.fetchone(
        "SELECT AVG(support_rate) AS avg FROM t4_tool_features"
    )
    
    return {
        "total": total["n"] if total else 0,
        "enterprise": enterprise["n"] if enterprise else 0,
        "opensource": opensource["n"] if opensource else 0,
        "freemium": freemium["n"] if freemium else 0,
        "multi_domain": multi_domain["n"] if multi_domain else 0,
        "top_tool": dict(top_tool) if top_tool else None,
        "license_counts": license_counts,
        "avg_support_rate": avg_support["avg"] if avg_support else 0.0,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Pipeline Run Tracker
# ═══════════════════════════════════════════════════════════════════════════

async def get_t4_run_status() -> dict[str, Any] | None:
    """Return the latest T4 analysis run record."""
    row = await db.fetchone(
        "SELECT * FROM t4_analysis_runs ORDER BY created_at DESC LIMIT 1"
    )
    return dict(row) if row else None


async def upsert_t4_run_status(
    status: str,
    total_tools: int = 0,
    processed_tools: int = 0,
    enriched_tools: int = 0,
) -> int:
    """Create or update the single T4 run row."""
    existing = await db.fetchone(
        "SELECT id FROM t4_analysis_runs ORDER BY created_at DESC LIMIT 1"
    )
    if existing:
        run_id = existing["id"]
        started_sql = "started_at = CASE WHEN ? = 'running' THEN CURRENT_TIMESTAMP ELSE started_at END"
        completed_sql = "completed_at = CASE WHEN ? IN ('done','failed') THEN CURRENT_TIMESTAMP ELSE completed_at END"
        await db.execute(
            f"""UPDATE t4_analysis_runs
                SET status = ?, total_tools = ?, processed_tools = ?, enriched_tools = ?,
                    {started_sql}, {completed_sql}
                WHERE id = ?""",
            (status, total_tools, processed_tools, enriched_tools, status, status, run_id),
        )
    else:
        await db.execute(
            """INSERT INTO t4_analysis_runs
                   (status, total_tools, processed_tools, enriched_tools, started_at)
               VALUES (?, ?, ?, ?, CASE WHEN ? = 'running' THEN CURRENT_TIMESTAMP ELSE NULL END)""",
            (status, total_tools, processed_tools, enriched_tools, status),
        )
        row = await db.fetchone(
            "SELECT id FROM t4_analysis_runs ORDER BY created_at DESC LIMIT 1"
        )
        run_id = row["id"] if row else 0
    await db.commit()
    return run_id


async def reset_t4_data() -> None:
    """Wipe all T4 data for a fresh run."""
    await db.execute("DELETE FROM t4_tool_subdomain_features")
    await db.execute("DELETE FROM t4_tool_features")
    await db.execute("DELETE FROM t4_tool_domains")
    await db.execute("DELETE FROM t4_tool_subdomains")
    await db.execute("DELETE FROM t4_tools")
    await db.execute("DELETE FROM t4_analysis_runs")
    await db.commit()
    logger.info("T4 data reset - ready for fresh analysis run")


async def get_enrichment_progress() -> dict[str, int]:
    """Return count of enriched vs. total tools."""
    total = await db.fetchone("SELECT COUNT(*) AS n FROM t4_tools")
    enriched = await db.fetchone(
        "SELECT COUNT(*) AS n FROM t4_tools WHERE license_model IS NOT NULL"
    )
    return {
        "total": total["n"] if total else 0,
        "enriched": enriched["n"] if enriched else 0,
        "pending": (total["n"] if total else 0) - (enriched["n"] if enriched else 0),
    }
