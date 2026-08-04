"""
dashboard.py

Small read-only web dashboard over the SQLite db that monitor.py writes to.
Shows a latency/loss timeseries chart and a table of degradation windows,
and exposes a CSV export shaped for handing to an ISP as evidence in an SLA
dispute (start/end/duration/avg+peak latency/avg+peak loss per outage
window, rather than a raw per-cycle dump).

Deliberately server-rendered + vanilla JS + Chart.js from CDN -- no build
step, no separate frontend container.
"""

import csv
import io
import itertools
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, render_template, request

import db
import settings_store
from config import get_config, list_settings_for_ui, SETTINGS_REGISTRY
from omada_client import OmadaClient

log = logging.getLogger("dashboard")

app = Flask(__name__)
settings_store.init_settings_db()

# Lazily built, module-level, reused across requests -- only needed for the
# alerts panel (everything else on this dashboard reads the local sqlite db
# monitor.py writes, not the Omada API directly). Built lazily rather than
# at import time so missing/misconfigured Omada settings disable just the
# alerts panel with a clear message, instead of crashing the whole
# dashboard on startup.
_omada_client = None
_omada_client_error = None


def get_omada_client():
    global _omada_client, _omada_client_error
    if _omada_client is None and _omada_client_error is None:
        missing = [
            k for k in ("OMADA_BASE_URL", "OMADA_CLIENT_ID", "OMADA_CLIENT_SECRET", "OMADA_OMADAC_ID", "OMADA_SITE_ID")
            if not get_config(k)
        ]
        if missing:
            _omada_client_error = f"Missing required Omada config: {', '.join(missing)}"
            log.warning("Alerts panel disabled: %s", _omada_client_error)
            return None
        try:
            _omada_client = OmadaClient(
                base_url=get_config("OMADA_BASE_URL"),
                client_id=get_config("OMADA_CLIENT_ID"),
                client_secret=get_config("OMADA_CLIENT_SECRET"),
                omadac_id=get_config("OMADA_OMADAC_ID"),
                site_id=get_config("OMADA_SITE_ID"),
                verify_tls=get_config("OMADA_VERIFY_TLS"),
            )
        except Exception as e:
            _omada_client_error = str(e)
            log.warning("Alerts panel disabled: %s", e)
    return _omada_client

# Server's own local time (container default is UTC, which is why table/CSV
# timestamps looked like GMT regardless of where you are) -- explicit
# IANA zone name so it's correct and DST-aware regardless of container TZ
# config. America/Los_Angeles covers PST/PDT automatically.
DASHBOARD_TIMEZONE = get_config("DASHBOARD_TIMEZONE")
_TZ = ZoneInfo(DASHBOARD_TIMEZONE)

REFRESH_INTERVAL_SECONDS = get_config("DASHBOARD_REFRESH_INTERVAL_SECONDS")
ISP_LOAD_REFRESH_INTERVAL_SECONDS = get_config("DASHBOARD_ISP_LOAD_POLL_INTERVAL_SECONDS")

RANGE_OPTIONS = {
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "90d": 90 * 86400,
}



def _since_ts(range_key: str) -> float:
    seconds = RANGE_OPTIONS.get(range_key, RANGE_OPTIONS["24h"])
    return time.time() - seconds


def display_port_name(port_ref, api_name: str) -> str:
    """
    Resolves the user-facing name for a WAN port: the user-configured label
    (WAN_PRIMARY_LABEL / WAN_BACKUP_LABEL) if one is set, otherwise the
    router-reported name (api_name) unchanged.

    port_ref may be either the full portId string ("1_8ff0...") as used by
    ports-config/load-balance, or a bare integer port number as used by
    isp-load -- matching handles both by comparing against the configured
    WAN_*_PORT_ID and its leading integer.

    Deliberately reads config at call time (not module import), which makes
    label changes apply LIVE on the next poll with no restart -- cheap
    enough (one small sqlite lookup per key) at this dashboard's request
    rates, and the one place live behavior is safe since it's purely
    cosmetic.
    """
    primary_id = get_config("WAN_PRIMARY_PORT_ID") or ""
    backup_id = get_config("WAN_BACKUP_PORT_ID") or ""

    def matches(configured_id: str) -> bool:
        if not configured_id:
            return False
        if str(port_ref) == configured_id:
            return True
        # isp-load reports bare integer port numbers; ports-config ids are
        # "N_hash" -- compare against the leading integer too.
        try:
            return str(port_ref) == configured_id.split("_")[0]
        except (AttributeError, IndexError):
            return False

    if matches(primary_id):
        label = get_config("WAN_PRIMARY_LABEL")
        if label:
            return label
    elif matches(backup_id):
        label = get_config("WAN_BACKUP_LABEL")
        if label:
            return label
    return api_name


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


@app.route("/")
def index():
    selected_range = request.args.get("range", "24h")
    if selected_range not in RANGE_OPTIONS:
        selected_range = "24h"

    return render_template(
        "dashboard.html",
        ranges=list(RANGE_OPTIONS.keys()),
        selected_range=selected_range,
        dashboard_timezone=DASHBOARD_TIMEZONE,
        refresh_interval_seconds=REFRESH_INTERVAL_SECONDS,
        isp_load_refresh_interval_seconds=ISP_LOAD_REFRESH_INTERVAL_SECONDS,
    )


@app.route("/docs")
def docs():
    return render_template("docs.html")


@app.route("/configuration")
def configuration():
    all_settings = list_settings_for_ui()
    # SETTINGS_REGISTRY (and therefore list_settings_for_ui(), which walks
    # it in order) is already grouped into contiguous same-section blocks --
    # groupby only needs contiguous runs, not a full sort, and this
    # preserves the registry's intentional section ordering rather than
    # alphabetizing it.
    sections = [
        (section_name, list(fields))
        for section_name, fields in itertools.groupby(all_settings, key=lambda f: f["section"])
    ]
    return render_template("configuration.html", sections=sections)


@app.route("/api/config/save", methods=["POST"])
def api_config_save():
    """
    Saves one or more settings as database overrides. Only accepts keys
    that actually exist in SETTINGS_REGISTRY -- silently ignores anything
    else, rather than letting an arbitrary key get written into settings.db
    (defense against a malformed/malicious request body, even though this
    is a local trusted dashboard).
    """
    body = request.get_json(silent=True) or {}
    values = body.get("values", {})
    if not isinstance(values, dict):
        return jsonify({"success": False, "error": "'values' must be an object"}), 400

    valid_keys = {entry["key"] for entry in SETTINGS_REGISTRY}
    saved_count = 0
    try:
        for key, value in values.items():
            if key not in valid_keys:
                log.warning("Ignoring unknown config key in save request: %s", key)
                continue
            settings_store.set_setting(key, value)
            saved_count += 1
        return jsonify({"success": True, "error": None, "saved_count": saved_count})
    except Exception as e:
        log.warning("Failed to save configuration: %s", e)
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/config/reset", methods=["POST"])
def api_config_reset():
    """Deletes every saved override -- the Configuration tab's Danger Zone.
    Reverts everything to .env/default; does NOT touch .env itself or the
    monitoring database (cycles/events/monitor_state)."""
    body = request.get_json(silent=True) or {}
    if not body.get("confirm"):
        return jsonify({"success": False, "error": "Missing confirmation"}), 400

    try:
        settings_store.delete_all_settings()
        return jsonify({"success": True, "error": None})
    except Exception as e:
        log.warning("Failed to reset configuration: %s", e)
        return jsonify({"success": False, "error": str(e)})


def _schedule_self_exit(delay_seconds: float = 0.7):
    """Exits this process shortly after the current request finishes, so
    the HTTP response gets flushed to the client first. Docker's
    `restart: unless-stopped` policy restarts the container, and the fresh
    process reads current config. Split into its own function so tests can
    monkeypatch it instead of actually killing the test runner."""
    import threading
    threading.Timer(delay_seconds, lambda: os._exit(0)).start()


@app.route("/api/restart", methods=["POST"])
def api_restart():
    """
    The Configuration page's "Apply & Restart" action -- restarts BOTH
    services so saved configuration takes effect, without the user touching
    docker: writes the restart flag to the shared settings db (monitor.py
    sees it within one check cycle, ~CHECK_INTERVAL_SECONDS, and exits
    cleanly), then exits this dashboard process itself just after the
    response flushes. Docker's restart policy brings both back with fresh
    config. Requires {"confirm": true}, same pattern as every other
    state-changing endpoint here.
    """
    body = request.get_json(silent=True) or {}
    if not body.get("confirm"):
        return jsonify({"success": False, "error": "Missing confirmation"}), 400

    try:
        settings_store.request_restart()
        _schedule_self_exit()
        return jsonify({"success": True, "error": None})
    except Exception as e:
        log.warning("Failed to initiate restart: %s", e)
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/cycles")
def api_cycles():
    selected_range = request.args.get("range", "24h")
    since = _since_ts(selected_range)
    return jsonify(db.fetch_cycles(since))


@app.route("/api/windows")
def api_windows():
    selected_range = request.args.get("range", "24h")
    since = _since_ts(selected_range)
    return jsonify(db.compute_degradation_windows(since))


@app.route("/api/events")
def api_events():
    selected_range = request.args.get("range", "24h")
    since = _since_ts(selected_range)
    return jsonify(db.fetch_events(since))


@app.route("/api/alerts")
def api_alerts():
    """
    Top 3 unresolved Omada alert logs, newest first. Unlike the other
    /api/* routes, this hits the Omada API live on every call rather than
    the local sqlite db -- there's no local cache/polling of alerts.
    Fails soft: missing credentials or an API error returns an empty list
    with an `error` field, rather than a 500, so a problem here doesn't
    take down the rest of the dashboard.
    """
    client = get_omada_client()
    if client is None:
        return jsonify({"alerts": [], "unresolved_count": None, "error": _omada_client_error})

    try:
        result = client.get_alert_logs(resolved=False, page=1, page_size=10)
        data = result.get("data", [])
        top3 = sorted(data, key=lambda a: a.get("time", 0), reverse=True)[:3]
        unresolved_count = result.get("alertLogStat", {}).get("unResolvedLogNum")
        return jsonify({"alerts": top3, "unresolved_count": unresolved_count, "error": None})
    except Exception as e:
        log.warning("Failed to fetch alerts: %s", e)
        return jsonify({"alerts": [], "unresolved_count": None, "error": str(e)})


@app.route("/api/alerts/resolve", methods=["POST"])
def api_alerts_resolve():
    """Resolves a single alert log by ID. Fails soft with a JSON error field
    rather than a 500, matching /api/alerts's error handling."""
    client = get_omada_client()
    if client is None:
        return jsonify({"success": False, "error": _omada_client_error})

    body = request.get_json(silent=True) or {}
    alert_id = body.get("id")
    if not alert_id:
        return jsonify({"success": False, "error": "Missing 'id' in request body"}), 400

    try:
        client.resolve_alert_logs([alert_id])
        return jsonify({"success": True, "error": None})
    except Exception as e:
        log.warning("Failed to resolve alert %s: %s", alert_id, e)
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/isp-load")
def api_isp_load():
    """
    Router-reported per-WAN throughput + latency history, straight from the
    Omada API (not the local sqlite db). NOTE: this endpoint's start/end
    params are SECONDS, unlike /api/alerts/resolve's milliseconds -- don't
    copy the *1000 pattern from elsewhere in this file for this one.
    Fails soft, same pattern as /api/alerts.
    """
    client = get_omada_client()
    if client is None:
        return jsonify({"ports": [], "error": _omada_client_error})

    selected_range = request.args.get("range", "24h")
    since = _since_ts(selected_range)
    now = time.time()

    try:
        result = client.get_isp_load(int(since), int(now))
        # Apply user-configured display labels (isp-load's portId is a bare
        # integer, which display_port_name handles by leading-int match)
        for port in result:
            port["portName"] = display_port_name(port.get("portId"), port.get("portName", ""))
        return jsonify({"ports": result, "error": None})
    except Exception as e:
        log.warning("Failed to fetch ISP load: %s", e)
        return jsonify({"ports": [], "error": str(e)})


@app.route("/api/wan-ports")
def api_wan_ports():
    """Populates the speed-test WAN dropdown. Reuses the already-confirmed
    get_wan_ports_config() -- static-ish data, not part of the live-poll
    cycle, fetched once on page load."""
    client = get_omada_client()
    if client is None:
        return jsonify({"ports": [], "error": _omada_client_error})
    try:
        result = client.get_wan_ports_config()
        ports = [
            {"portId": p["portId"], "portName": display_port_name(p["portId"], p["portName"])}
            for p in result.get("wanPortsConfig", [])
        ]
        return jsonify({"ports": ports, "error": None})
    except Exception as e:
        log.warning("Failed to fetch WAN ports list: %s", e)
        return jsonify({"ports": [], "error": str(e)})


@app.route("/api/device-capabilities")
def api_device_capabilities():
    """
    Checks whether this gateway actually supports speed tests before the UI
    offers the button -- confirmed via a real rejection that osgCap.speedTest
    reports this accurately (an ER605 with speedTest:false correctly failed
    with "This device does not support speed test." on a real start attempt).
    """
    client = get_omada_client()
    if client is None:
        return jsonify({"speedTestSupported": None, "error": _omada_client_error})

    gateway_mac = get_config("OMADA_GATEWAY_MAC")
    if not gateway_mac:
        return jsonify({"speedTestSupported": None, "error": "OMADA_GATEWAY_MAC not configured"})

    try:
        gateway = client.get_gateway(gateway_mac)
        supported = gateway.get("osgCap", {}).get("speedTest")
        return jsonify({"speedTestSupported": bool(supported), "error": None})
    except Exception as e:
        log.warning("Failed to check device capabilities: %s", e)
        return jsonify({"speedTestSupported": None, "error": str(e)})


@app.route("/api/speedtest/start", methods=["POST"])
def api_speedtest_start():
    client = get_omada_client()
    if client is None:
        return jsonify({"success": False, "error": _omada_client_error})

    gateway_mac = get_config("OMADA_GATEWAY_MAC")
    if not gateway_mac:
        return jsonify({"success": False, "error": "OMADA_GATEWAY_MAC not configured"})

    body = request.get_json(silent=True) or {}
    port_uuid = body.get("portUuid")
    if not port_uuid:
        return jsonify({"success": False, "error": "Missing 'portUuid' in request body"}), 400

    try:
        client.start_speed_test(gateway_mac, [port_uuid])
        return jsonify({"success": True, "error": None})
    except Exception as e:
        log.warning("Failed to start speed test on %s: %s", port_uuid, e)
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/speedtest/result")
def api_speedtest_result():
    client = get_omada_client()
    if client is None:
        return jsonify({"result": None, "error": _omada_client_error})

    gateway_mac = get_config("OMADA_GATEWAY_MAC")
    if not gateway_mac:
        return jsonify({"result": None, "error": "OMADA_GATEWAY_MAC not configured"})

    try:
        result = client.get_speed_test_result(gateway_mac)
        return jsonify({"result": result, "error": None})
    except Exception as e:
        log.warning("Failed to fetch speed test result: %s", e)
        return jsonify({"result": None, "error": str(e)})


@app.route("/api/active-wan")
def api_active_wan():
    """
    Current primary/backup WAN, with human-readable names joined in from
    get_wan_ports_config() (load-balance config only has portId strings,
    not names). Two real API calls per invocation -- acceptable at the
    normal 15s live-poll cadence, same pattern as /api/alerts and
    /api/isp-load already adding their own per-cycle calls.
    """
    client = get_omada_client()
    if client is None:
        return jsonify({"active": None, "backup": None, "linkBackup": None, "error": _omada_client_error})
    try:
        lb = client.get_internet_load_balance()
        wan_config = client.get_wan_ports_config()
        name_by_id = {p["portId"]: display_port_name(p["portId"], p["portName"]) for p in wan_config.get("wanPortsConfig", [])}
        primary_id = (lb.get("primaryWans") or [None])[0]
        backup_id = lb.get("backupWan")
        return jsonify({
            "active": {"portId": primary_id, "portName": name_by_id.get(primary_id, primary_id)} if primary_id else None,
            "backup": {"portId": backup_id, "portName": name_by_id.get(backup_id, backup_id)} if backup_id else None,
            "linkBackup": lb.get("linkBackup"),
            "error": None,
        })
    except Exception as e:
        log.warning("Failed to fetch active WAN status: %s", e)
        return jsonify({"active": None, "backup": None, "linkBackup": None, "error": str(e)})


@app.route("/api/failover", methods=["POST"])
def api_failover():
    """
    Manually triggers a failover from the dashboard -- functionally
    identical to `./test_load_balance_swap.sh failover`/`failback`: fetches
    the CURRENT live config fresh (not trusting whatever the frontend last
    saw, to avoid acting on stale state) and swaps primaryWans/backupWan via
    the same set_active_wan() used everywhere else in this project.

    This is a LIVE write moving real traffic -- requires an explicit
    {"confirm": true} in the request body. The frontend is expected to have
    already gotten an explicit user confirmation (a real confirm dialog)
    before ever sending that -- this check exists so the endpoint itself
    can't be triggered by an accidental/automated POST without intent.
    """
    client = get_omada_client()
    if client is None:
        return jsonify({"success": False, "error": _omada_client_error})

    body = request.get_json(silent=True) or {}
    if not body.get("confirm"):
        return jsonify({"success": False, "error": "Missing confirmation"}), 400

    try:
        current = client.get_internet_load_balance()
        current_primary = (current.get("primaryWans") or [None])[0]
        current_backup = current.get("backupWan")
        if not current_primary or not current_backup:
            return jsonify({"success": False, "error": "Could not determine current primary/backup WAN from live config"})

        client.set_active_wan(current_backup, current_primary)  # swap: new primary = old backup
        return jsonify({"success": True, "error": None, "newPrimary": current_backup, "newBackup": current_primary})
    except Exception as e:
        log.warning("Failed to trigger failover: %s", e)
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/database/truncate", methods=["POST"])
def api_database_truncate():
    """
    Clears historical data (ping cycles, failover events) -- the raw
    material behind the chart, table, and CSV export. Deliberately does
    NOT touch monitor_state (which WAN is active, persisted failed_over
    flag) -- see truncate_history()'s docstring in db.py for why. Requires
    {"confirm": true}, same pattern as /api/failover -- the frontend is
    expected to have already gotten an explicit, real confirm dialog
    before ever sending this.
    """
    body = request.get_json(silent=True) or {}
    if not body.get("confirm"):
        return jsonify({"success": False, "error": "Missing confirmation"}), 400

    try:
        db.truncate_history()
        return jsonify({"success": True, "error": None})
    except Exception as e:
        log.warning("Failed to truncate database: %s", e)
        return jsonify({"success": False, "error": str(e)})


@app.route("/report.csv")
def report_csv():
    selected_range = request.args.get("range", "24h")
    since = _since_ts(selected_range)
    windows = db.compute_degradation_windows(since)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "start", "end", "duration_seconds", "duration_minutes",
        "avg_latency_ms", "peak_latency_ms", "avg_loss_pct", "peak_loss_pct", "sample_count",
    ])
    for w in windows:
        writer.writerow([
            _fmt(w["start"]), _fmt(w["end"]),
            f"{w['duration_seconds']:.0f}", f"{w['duration_seconds']/60:.1f}",
            f"{w['avg_latency_ms']:.0f}" if w["avg_latency_ms"] is not None else "",
            f"{w['max_latency_ms']:.0f}" if w["max_latency_ms"] is not None else "",
            f"{w['avg_loss_pct']:.1f}" if w["avg_loss_pct"] is not None else "",
            f"{w['max_loss_pct']:.1f}" if w["max_loss_pct"] is not None else "",
            w["sample_count"],
        ])

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=wan-degradation-report-{selected_range}.csv"},
    )


if __name__ == "__main__":
    db.init_db()
    port = get_config("DASHBOARD_PORT")
    app.run(host="0.0.0.0", port=port)
