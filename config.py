"""
config.py

Single source of truth for every tunable setting in this project, and the
precedence-aware resolver both monitor.py and dashboard.py read through:

    saved in settings.db  >  set in .env  >  hardcoded default

IMPORTANT CAVEAT, worth repeating since it's easy to assume otherwise: both
processes read config ONCE at startup into fixed module-level values (same
as it's always been for .env in this project). Saving a setting via the
Configuration tab updates settings.db correctly and immediately, but it
will NOT change the running monitor/dashboard process's actual behavior
until that container restarts. This module doesn't implement live-reload --
that would need a meaningfully larger, riskier change to how monitor.py in
particular is structured (it currently references config as simple
module-level constants throughout, not through a live-lookup layer). The
Configuration UI says this explicitly rather than implying otherwise.

SETTINGS_REGISTRY is also the schema for the Configuration page -- every
entry here becomes an editable field, grouped by "section" and masked by
default if "sensitive" is true.
"""

import os

import settings_store

SETTINGS_REGISTRY = [
    # ---- Omada Controller / Open API ----
    {"key": "OMADA_BASE_URL", "default": "https://192.168.0.2:8043", "type": "str", "sensitive": False,
     "section": "Omada Connection", "description": "Omada controller/Open API base URL."},
    {"key": "OMADA_CLIENT_ID", "default": "", "type": "str", "sensitive": False,
     "section": "Omada Connection", "description": "Open API application client ID."},
    {"key": "OMADA_CLIENT_SECRET", "default": "", "type": "str", "sensitive": True,
     "section": "Omada Connection", "description": "Open API application client secret."},
    {"key": "OMADA_OMADAC_ID", "default": "", "type": "str", "sensitive": False,
     "section": "Omada Connection", "description": "Omada controller (omadac) ID."},
    {"key": "OMADA_SITE_ID", "default": "", "type": "str", "sensitive": False,
     "section": "Omada Connection", "description": "Site ID within the controller."},
    {"key": "OMADA_GATEWAY_MAC", "default": "", "type": "str", "sensitive": False,
     "section": "Omada Connection", "description": "MAC address of the ER605 gateway."},
    {"key": "OMADA_VERIFY_TLS", "default": "false", "type": "bool", "sensitive": False,
     "section": "Omada Connection", "description": "Verify the controller's TLS certificate."},

    # ---- WAN Ports ----
    {"key": "WAN_PRIMARY_PORT_ID", "default": "", "type": "str", "sensitive": False,
     "section": "WAN Ports", "description": "Primary WAN's portId string (e.g. \"1_8ff0...\")."},
    {"key": "WAN_BACKUP_PORT_ID", "default": "", "type": "str", "sensitive": False,
     "section": "WAN Ports", "description": "Backup WAN's portId string."},
    {"key": "WAN_PRIMARY_LABEL", "default": "", "type": "str", "sensitive": False,
     "section": "WAN Ports", "description": "Display name for the primary WAN across the dashboard (blank = use the router-reported name, e.g. \"WAN\"). Applies live -- no restart needed."},
    {"key": "WAN_BACKUP_LABEL", "default": "", "type": "str", "sensitive": False,
     "section": "WAN Ports", "description": "Display name for the backup WAN (blank = use the router-reported name, e.g. \"WAN/LAN1\"). Applies live -- no restart needed."},

    # ---- Ping Probe ----
    {"key": "PING_TARGETS", "default": "1.1.1.1,8.8.8.8,9.9.9.9", "type": "str", "sensitive": False,
     "section": "Ping Probe", "description": "Comma-separated ping targets, averaged together."},
    {"key": "CHECK_INTERVAL_SECONDS", "default": "5", "type": "float", "sensitive": False,
     "section": "Ping Probe", "description": "Seconds between full ping cycles."},
    {"key": "PING_COUNT_PER_CYCLE", "default": "3", "type": "int", "sensitive": False,
     "section": "Ping Probe", "description": "Pings sent per target, per cycle."},
    {"key": "PING_TIMEOUT_SECONDS", "default": "2", "type": "float", "sensitive": False,
     "section": "Ping Probe", "description": "Per-ping timeout before it counts as lost."},

    # ---- Trigger thresholds ----
    {"key": "LATENCY_THRESHOLD_MS", "default": "150", "type": "float", "sensitive": False,
     "section": "Trigger Thresholds", "description": "Latency above this makes a cycle \"bad\"."},
    {"key": "PACKET_LOSS_THRESHOLD_PCT", "default": "15", "type": "float", "sensitive": False,
     "section": "Trigger Thresholds", "description": "Loss above this makes a cycle \"bad\"."},
    {"key": "CONSECUTIVE_BAD_TO_TRIGGER", "default": "12", "type": "int", "sensitive": False,
     "section": "Trigger Thresholds", "description": "Consecutive bad cycles before failover fires."},
    {"key": "CONSECUTIVE_GOOD_TO_FAILBACK", "default": "24", "type": "int", "sensitive": False,
     "section": "Trigger Thresholds", "description": "Vestigial -- no longer used for fail-back (kept for update_streaks() internals)."},
    {"key": "STREAK_TOLERANCE_CYCLES", "default": "2", "type": "int", "sensitive": False,
     "section": "Trigger Thresholds", "description": "Consecutive opposite-direction cycles needed to break a streak."},
    {"key": "COOLDOWN_SECONDS", "default": "120", "type": "float", "sensitive": False,
     "section": "Trigger Thresholds", "description": "Minimum seconds between any two failover actions."},

    # ---- Fail-back (primary health) ----
    {"key": "PRIMARY_HEALTH_POLL_INTERVAL_SECONDS", "default": "30", "type": "float", "sensitive": False,
     "section": "Fail-back (Primary Health)", "description": "How often to query the primary WAN's real API status."},
    {"key": "PRIMARY_HEALTHY_STABILITY_SECONDS", "default": "300", "type": "float", "sensitive": False,
     "section": "Fail-back (Primary Health)", "description": "Required continuous healthy time before fail-back is considered."},
    {"key": "PRIMARY_HEALTHY_LATENCY_THRESHOLD_MS", "default": "100", "type": "float", "sensitive": False,
     "section": "Fail-back (Primary Health)", "description": "Primary's latency must be under this to count as healthy."},
    {"key": "PRIMARY_HEALTHY_LOSS_THRESHOLD_PCT", "default": "5", "type": "float", "sensitive": False,
     "section": "Fail-back (Primary Health)", "description": "Primary's loss must be under this to count as healthy."},
    {"key": "AUTO_FAILBACK_ENABLED", "default": "false", "type": "bool", "sensitive": False,
     "section": "Fail-back (Primary Health)", "description": "Actually call the API on fail-back vs. just log a reminder."},

    # ---- Throughput sampling ----
    {"key": "ENABLE_THROUGHPUT_CHECK", "default": "false", "type": "bool", "sensitive": False,
     "section": "Throughput Sampling", "description": "Periodically sample real download throughput."},
    {"key": "THROUGHPUT_CHECK_EVERY_N_CYCLES", "default": "60", "type": "int", "sensitive": False,
     "section": "Throughput Sampling", "description": "How many ping cycles between throughput samples."},
    {"key": "THROUGHPUT_TEST_URL", "default": "https://speed.cloudflare.com/__down?bytes=2000000", "type": "str", "sensitive": False,
     "section": "Throughput Sampling", "description": "URL used for the timed download sample."},
    {"key": "THROUGHPUT_MIN_MBPS", "default": "5", "type": "float", "sensitive": False,
     "section": "Throughput Sampling", "description": "Below this counts as a bad cycle too."},

    # ---- Database / Dashboard ----
    {"key": "DB_PATH", "default": "/data/wan-monitor.db", "type": "str", "sensitive": False,
     "section": "Database & Dashboard", "description": "Path to the shared monitoring sqlite db."},
    {"key": "RETENTION_DAYS", "default": "90", "type": "float", "sensitive": False,
     "section": "Database & Dashboard", "description": "Days of cycle/event history kept before pruning."},
    {"key": "DASHBOARD_PORT", "default": "8090", "type": "int", "sensitive": False,
     "section": "Database & Dashboard", "description": "Port the dashboard web UI listens on."},
    {"key": "DASHBOARD_TIMEZONE", "default": "America/Los_Angeles", "type": "str", "sensitive": False,
     "section": "Database & Dashboard", "description": "IANA timezone for table/CSV timestamps."},
    {"key": "DASHBOARD_REFRESH_INTERVAL_SECONDS", "default": "15", "type": "int", "sensitive": False,
     "section": "Database & Dashboard", "description": "Main dashboard live-poll interval."},
    {"key": "DASHBOARD_ISP_LOAD_POLL_INTERVAL_SECONDS", "default": "60", "type": "int", "sensitive": False,
     "section": "Database & Dashboard", "description": "WAN Metrics chart poll interval (separate, slower cadence)."},

    # ---- Misc ----
    {"key": "LOG_LEVEL", "default": "INFO", "type": "str", "sensitive": False,
     "section": "Misc", "description": "Python log level (DEBUG/INFO/WARNING/...)."},
    {"key": "DRY_RUN", "default": "true", "type": "bool", "sensitive": False,
     "section": "Misc", "description": "Log decisions without calling the Omada API."},
]

_REGISTRY_BY_KEY = {entry["key"]: entry for entry in SETTINGS_REGISTRY}


def _cast(value: str, type_name: str):
    if type_name == "bool":
        return value.strip().lower() in ("1", "true", "yes", "on")
    if type_name == "int":
        return int(float(value))  # tolerant of "12.0"-style values too
    if type_name == "float":
        return float(value)
    return value  # "str"


def get_config(key: str):
    """
    Resolves one setting through the full precedence chain, cast to its
    registered type. Unknown keys (not in the registry) fall back to plain
    os.environ.get(key) with no default and no casting -- shouldn't happen
    in practice since every real setting is registered, but fails soft
    rather than raising.
    """
    entry = _REGISTRY_BY_KEY.get(key)
    if entry is None:
        return os.environ.get(key)

    db_value = settings_store.get_setting(key)
    if db_value is not None and db_value != "":
        return _cast(db_value, entry["type"])

    env_value = os.environ.get(key)
    if env_value is not None and env_value != "":
        return _cast(env_value, entry["type"])

    return _cast(entry["default"], entry["type"])


def get_effective_with_source(key: str):
    """Returns (value_as_string, source) where source is 'database', 'env',
    or 'default' -- used by the Configuration UI to show where each value
    is actually coming from right now."""
    entry = _REGISTRY_BY_KEY.get(key)
    if entry is None:
        return os.environ.get(key, ""), "env"

    db_value = settings_store.get_setting(key)
    if db_value is not None and db_value != "":
        return db_value, "database"

    env_value = os.environ.get(key)
    if env_value is not None and env_value != "":
        return env_value, "env"

    return entry["default"], "default"


def list_settings_for_ui():
    """Full registry with current effective value + source, for the
    Configuration page. Sensitive values are still included in full here --
    masking happens client-side (the field renders as a password input by
    default) so the Save flow can still submit real values; this endpoint
    itself is not meant to be exposed outside a trusted local dashboard."""
    result = []
    for entry in SETTINGS_REGISTRY:
        value, source = get_effective_with_source(entry["key"])
        result.append({
            "key": entry["key"],
            "value": value,
            "source": source,
            "sensitive": entry["sensitive"],
            "section": entry["section"],
            "description": entry["description"],
        })
    return result
