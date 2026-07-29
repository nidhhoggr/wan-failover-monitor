"""
db.py

Shared SQLite persistence for wan-failover-monitor. Used by monitor.py
(writer) and dashboard.py (reader). WAL mode lets the two processes share
the db file safely -- one writer, one reader, no separate DB server needed.

Schema:
  cycles: one row per check cycle -- the raw timeseries.
  events: one row per failover/fail-back action actually taken (or that
          would have been taken, under DRY_RUN) -- this is what turns into
          the "degradation window" report.
"""

import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/wan-monitor.db")
RETENTION_DAYS = float(os.environ.get("RETENTION_DAYS", "90"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    avg_latency_ms REAL NOT NULL,
    loss_pct REAL NOT NULL,
    is_bad INTEGER NOT NULL,
    throughput_mbps REAL
);
CREATE INDEX IF NOT EXISTS idx_cycles_ts ON cycles(ts);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    action TEXT NOT NULL,          -- 'failover_to_backup' | 'failback_to_primary'
    dry_run INTEGER NOT NULL,
    trigger_latency_ms REAL,
    trigger_loss_pct REAL,
    consecutive_cycles INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

-- Single-row table holding the monitor's current failover state, so a
-- container restart doesn't forget whether it's currently running on the
-- backup WAN. Without this, a restart mid-failover resets failed_over to
-- False in memory even if the router is genuinely still on backup.
CREATE TABLE IF NOT EXISTS monitor_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    failed_over INTEGER NOT NULL DEFAULT 0,
    last_action_time REAL NOT NULL DEFAULT 0
);
"""


@contextmanager
def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def insert_cycle(ts: float, avg_latency_ms: float, loss_pct: float, is_bad: bool, throughput_mbps=None):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO cycles (ts, avg_latency_ms, loss_pct, is_bad, throughput_mbps) VALUES (?, ?, ?, ?, ?)",
            (ts, avg_latency_ms, loss_pct, int(is_bad), throughput_mbps),
        )


def insert_event(ts: float, action: str, dry_run: bool, trigger_latency_ms=None, trigger_loss_pct=None, consecutive_cycles=None):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO events (ts, action, dry_run, trigger_latency_ms, trigger_loss_pct, consecutive_cycles) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, action, int(dry_run), trigger_latency_ms, trigger_loss_pct, consecutive_cycles),
        )


def prune_old_rows():
    cutoff = time.time() - (RETENTION_DAYS * 86400)
    with _connect() as conn:
        conn.execute("DELETE FROM cycles WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))


def get_monitor_state() -> dict:
    """Returns {'failed_over': bool, 'last_action_time': float}, defaulting
    to a fresh/never-failed-over state if no row exists yet."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT failed_over, last_action_time FROM monitor_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return {"failed_over": False, "last_action_time": 0.0}
        return {"failed_over": bool(row["failed_over"]), "last_action_time": row["last_action_time"]}


def set_monitor_state(failed_over: bool, last_action_time: float):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO monitor_state (id, failed_over, last_action_time) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET failed_over = excluded.failed_over, "
            "last_action_time = excluded.last_action_time",
            (int(failed_over), last_action_time),
        )


def fetch_cycles(since_ts: float):
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ts, avg_latency_ms, loss_pct, is_bad, throughput_mbps FROM cycles WHERE ts >= ? ORDER BY ts ASC",
            (since_ts,),
        ).fetchall()
        return [dict(r) for r in rows]


def fetch_events(since_ts: float):
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ts, action, dry_run, trigger_latency_ms, trigger_loss_pct, consecutive_cycles "
            "FROM events WHERE ts >= ? ORDER BY ts ASC",
            (since_ts,),
        ).fetchall()
        return [dict(r) for r in rows]


def compute_degradation_windows(since_ts: float, max_gap_seconds: float = 15.0):
    """
    Collapse contiguous (or near-contiguous, within max_gap_seconds) runs of
    is_bad=1 cycles into windows -- this is the shape an ISP dispute wants:
    "network was degraded from X to Y (duration), avg latency N ms, avg loss
    M%, peak loss P%" rather than a raw per-5-second timeseries.
    """
    cycles = fetch_cycles(since_ts)
    windows = []
    current = None

    for c in cycles:
        if c["is_bad"]:
            if current is None:
                current = {"start": c["ts"], "end": c["ts"], "samples": [c]}
            elif c["ts"] - current["end"] <= max_gap_seconds:
                current["end"] = c["ts"]
                current["samples"].append(c)
            else:
                windows.append(current)
                current = {"start": c["ts"], "end": c["ts"], "samples": [c]}
        else:
            if current is not None and c["ts"] - current["end"] > max_gap_seconds:
                windows.append(current)
                current = None

    if current is not None:
        windows.append(current)

    result = []
    for w in windows:
        latencies = [s["avg_latency_ms"] for s in w["samples"] if s["avg_latency_ms"] != float("inf")]
        losses = [s["loss_pct"] for s in w["samples"]]
        result.append({
            "start": w["start"],
            "end": w["end"],
            "duration_seconds": w["end"] - w["start"],
            "sample_count": len(w["samples"]),
            "avg_latency_ms": (sum(latencies) / len(latencies)) if latencies else None,
            "max_latency_ms": max(latencies) if latencies else None,
            "avg_loss_pct": sum(losses) / len(losses) if losses else None,
            "max_loss_pct": max(losses) if losses else None,
        })
    return result
