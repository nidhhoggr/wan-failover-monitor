"""
settings_store.py

A deliberately SEPARATE sqlite database from the main monitoring db
(wan-monitor.db) -- holds config overrides saved via the dashboard's
Configuration tab. Kept separate specifically so "delete the settings
database" (the Configuration tab's own Danger Zone) can be a clean,
low-risk file/table wipe that can't accidentally touch ping history,
failover events, or monitor_state, the way sharing one db file might risk.

Lives in the same shared /data volume both containers already mount, so a
setting saved via the dashboard is visible to the monitor process too --
though see config.py's module docstring for the important caveat that
config is only read once at process startup, so a saved change still needs
a container restart to actually take effect.
"""

import os
import sqlite3
from contextlib import contextmanager

SETTINGS_DB_PATH = os.environ.get("SETTINGS_DB_PATH", "/data/settings.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@contextmanager
def _connect():
    os.makedirs(os.path.dirname(SETTINGS_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(SETTINGS_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    # Ensure the schema exists on EVERY connection, not just when some
    # caller remembers to call init_settings_db() first. This module gets
    # imported by db.py, which itself gets imported before dashboard.py's
    # own module body would otherwise call init_settings_db() -- relying on
    # call-order there was a real bug (get_config("DB_PATH") at db.py's
    # import time failing because the table didn't exist yet). CREATE TABLE
    # IF NOT EXISTS is cheap and idempotent, safe to run on every connect.
    conn.executescript(_SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_settings_db():
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def get_setting(key: str):
    """Returns the stored override value (as a string) for key, or None if
    no override has been saved -- callers fall back to env/default in that
    case. Returns None (not an empty string) for "no override", so an
    intentionally-empty override is distinguishable in principle, though in
    practice set_setting() below treats an empty value as "delete the
    override" per the spec (clearing a field and saving reverts to
    env/default)."""
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


def get_all_settings() -> dict:
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {k: v for k, v in rows}


def set_setting(key: str, value: str):
    """An empty value DELETES the override instead of storing an empty
    string -- matches the intended UX: clearing a field in the
    Configuration UI and saving means "go back to using env/default for
    this one," not "override it with emptiness.\""""
    if value is None or value == "":
        delete_setting(key)
        return
    with _connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def delete_setting(key: str):
    with _connect() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def delete_all_settings():
    """The Configuration tab's Danger Zone action -- reverts every setting
    back to whatever .env/hardcoded-default provides."""
    with _connect() as conn:
        conn.execute("DELETE FROM settings")


# ---- restart signaling ----------------------------------------------------
# The dashboard's "Apply & Restart" button needs to restart BOTH containers
# so config changes take effect, but the dashboard process can only exit
# itself -- it has no way to reach into the monitor container (and mounting
# the Docker socket to allow that would be a much larger security surface
# than this feature justifies). Instead: the dashboard writes a timestamp
# flag into this shared settings db, and monitor.py checks it once per
# cycle -- if the flag is newer than the process's own start time, it exits
# cleanly and Docker's `restart: unless-stopped` policy brings it back with
# fresh config. Worst-case signal latency is one CHECK_INTERVAL (~5s).

RESTART_FLAG_KEY = "_RESTART_REQUESTED_AT"


def request_restart():
    """Stamps the restart flag with the current time. Any process whose
    start time predates this stamp should exit at its next check."""
    import time
    with _connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (RESTART_FLAG_KEY, str(time.time())),
        )


def restart_requested_since(process_start_time: float) -> bool:
    """True if a restart was requested after the given process start time.
    A stale flag (from a previous restart cycle) compares older than the
    freshly-restarted process's start time, so it can't cause a loop --
    no cleanup step needed."""
    raw = get_setting(RESTART_FLAG_KEY)
    if raw is None:
        return False
    try:
        return float(raw) > process_start_time
    except ValueError:
        return False
