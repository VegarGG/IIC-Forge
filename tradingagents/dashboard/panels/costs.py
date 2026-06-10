"""Costs panel — daily cost / token trend chart."""

from __future__ import annotations

import sqlite3


def fetch_provider_split(conn: sqlite3.Connection) -> dict:
    """Return a local-vs-API call-volume split across all cost rows.

    Classification rules (order matters):
    * ``local_calls``   — rows where provider = 'local'
    * ``api_calls``     — rows where provider != 'local'
    * ``free_calls``    — rows where usd_estimate = 0.0  (real zero, NOT NULL)
    * ``unknown_calls`` — rows where usd_estimate IS NULL
    * ``api_spend``     — SUM(usd_estimate) over non-NULL rows
                          (SQLite SUM naturally skips NULL; we do NOT COALESCE
                          NULL→0 so unknowns are never conflated with free rows)
    """
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN provider = 'local' THEN 1 ELSE 0 END)   AS local_calls,
            SUM(CASE WHEN provider != 'local' THEN 1 ELSE 0 END)   AS api_calls,
            -- free = explicitly zero, not null
            SUM(CASE WHEN usd_estimate = 0.0
                          AND usd_estimate IS NOT NULL THEN 1 ELSE 0 END) AS free_calls,
            -- unknown = null (price unrecorded)
            SUM(CASE WHEN usd_estimate IS NULL THEN 1 ELSE 0 END)  AS unknown_calls,
            -- SUM skips NULL rows natively; no COALESCE to avoid conflation
            COALESCE(SUM(CASE WHEN usd_estimate IS NOT NULL
                              THEN usd_estimate END), 0.0)          AS api_spend
        FROM costs
        JOIN runs ON runs.run_id = costs.run_id
        """
    ).fetchone()

    if row is None:
        return {
            "local_calls": 0,
            "api_calls": 0,
            "free_calls": 0,
            "unknown_calls": 0,
            "api_spend": 0.0,
        }

    return {
        "local_calls":   int(row["local_calls"]   or 0),
        "api_calls":     int(row["api_calls"]      or 0),
        "free_calls":    int(row["free_calls"]     or 0),
        "unknown_calls": int(row["unknown_calls"]  or 0),
        "api_spend":     float(row["api_spend"]),
    }


def fetch_daily_cost_trend(conn: sqlite3.Connection, *, days: int = 30) -> list[dict]:
    rows = conn.execute(
        """
        SELECT substr(r.started_ts, 1, 10) AS day,
               c.model AS model,
               SUM(c.usd_estimate) AS total_usd,
               SUM(c.in_tokens) AS in_tokens,
               SUM(c.out_tokens) AS out_tokens,
               SUM(COALESCE(c.cache_hit_tokens, 0)) AS cache_hit_tokens,
               SUM(COALESCE(c.cache_miss_tokens, 0)) AS cache_miss_tokens
        FROM costs c
        JOIN runs r ON r.run_id = c.run_id
        -- datetime(r.started_ts): ISO 'T'+offset must be normalized before
        -- comparing to datetime('now', ?), else same-day rows are mis-filtered.
        WHERE datetime(r.started_ts) > datetime('now', ?)
        GROUP BY day, c.model
        ORDER BY day ASC, c.model ASC
        """,
        (f"-{int(days)} days",),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        hit = int(item.get("cache_hit_tokens") or 0)
        miss = int(item.get("cache_miss_tokens") or 0)
        total = hit + miss
        item["cache_hit_ratio"] = (hit / total) if total > 0 else None
        out.append(item)
    return out
