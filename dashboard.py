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
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, render_template_string, request

import db
from omada_client import OmadaClient

log = logging.getLogger("dashboard")

app = Flask(__name__)

# Lazily built, module-level, reused across requests -- only needed for the
# alerts panel (everything else on this dashboard reads the local sqlite db
# monitor.py writes, not the Omada API directly). Built lazily rather than
# at import time so a missing/misconfigured OMADA_* env var disables just
# the alerts panel with a clear message, instead of crashing the whole
# dashboard on startup.
_omada_client = None
_omada_client_error = None


def get_omada_client():
    global _omada_client, _omada_client_error
    if _omada_client is None and _omada_client_error is None:
        try:
            _omada_client = OmadaClient.from_env()
        except Exception as e:
            _omada_client_error = str(e)
            log.warning("Alerts panel disabled: %s", e)
    return _omada_client

# Server's own local time (container default is UTC, which is why table/CSV
# timestamps looked like GMT regardless of where you are) -- explicit
# IANA zone name so it's correct and DST-aware regardless of container TZ
# config. America/Los_Angeles covers PST/PDT automatically.
DASHBOARD_TIMEZONE = os.environ.get("DASHBOARD_TIMEZONE", "America/Los_Angeles")
_TZ = ZoneInfo(DASHBOARD_TIMEZONE)

REFRESH_INTERVAL_SECONDS = int(os.environ.get("DASHBOARD_REFRESH_INTERVAL_SECONDS", "15"))

RANGE_OPTIONS = {
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "90d": 90 * 86400,
}

PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>WAN Failover Monitor</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
  <style>
    body { font-family: -apple-system, Helvetica, Arial, sans-serif; margin: 2rem; background: #0f1115; color: #e6e6e6; }
    h1 { font-size: 1.3rem; display: inline-block; margin-right: 1rem; }
    .controls { margin-bottom: 1rem; display: flex; align-items: center; flex-wrap: wrap; gap: 1rem; }
    .controls a { color: #8ab4f8; margin-right: 1rem; text-decoration: none; }
    .controls a.active { font-weight: bold; text-decoration: underline; }
    table { border-collapse: collapse; width: 100%; margin-top: 1.5rem; font-size: 0.85rem; }
    th, td { border: 1px solid #333; padding: 0.4rem 0.6rem; text-align: right; }
    th { background: #1a1d24; }
    td:first-child, th:first-child { text-align: left; }
    canvas { background: #14161c; border-radius: 6px; padding: 1rem; }
    .export { margin-top: 1rem; display: inline-block; background: #2b6cb0; color: white; padding: 0.5rem 1rem;
              border-radius: 4px; text-decoration: none; font-size: 0.85rem; }
    .stat { display: inline-block; margin-right: 2rem; }
    .stat b { font-size: 1.4rem; display: block; }
    #live-toggle-btn { background: #1a1d24; color: #e6e6e6; border: 1px solid #333; border-radius: 4px;
                        padding: 0.4rem 0.8rem; cursor: pointer; font-size: 0.85rem; }
    #live-toggle-btn.live { border-color: #3ecf6e; color: #3ecf6e; }
    #live-toggle-btn.paused { border-color: #999; color: #999; }
    #live-indicator { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 0.4rem; }
    #live-indicator.live { background: #3ecf6e; }
    #live-indicator.paused { background: #999; }
    #last-updated { color: #888; font-size: 0.8rem; }
    .count-badge { background: #5c1e1e; color: #ffb3b3; border-radius: 10px; padding: 0.05rem 0.5rem;
                   font-size: 0.75rem; vertical-align: middle; }
    .alert-item { display: flex; align-items: center; gap: 0.75rem; background: #14161c;
                  border: 1px solid #2a2d35; border-radius: 6px; padding: 0.6rem 0.9rem; margin-bottom: 0.5rem; }
    .alert-level { font-size: 0.7rem; text-transform: uppercase; padding: 0.15rem 0.5rem; border-radius: 3px;
                    background: #3a2a12; color: #f0b155; white-space: nowrap; }
    .alert-content { flex: 1; font-size: 0.85rem; }
    .alert-time { color: #888; font-size: 0.78rem; white-space: nowrap; }
    .alert-ack-btn { background: transparent; border: 1px solid #444; color: #999; border-radius: 4px;
                      padding: 0.25rem 0.6rem; font-size: 0.75rem; cursor: pointer; white-space: nowrap; }
    .alert-ack-btn:hover:not(:disabled) { border-color: #3ecf6e; color: #3ecf6e; }
    .alert-ack-btn:disabled { cursor: default; opacity: 0.6; }
    .alerts-empty { color: #888; font-size: 0.85rem; padding: 0.5rem 0; }
    .alerts-error { color: #f28b82; font-size: 0.85rem; padding: 0.5rem 0; }
    .wan-metrics-tabs { margin-bottom: 0.75rem; }
    .tab-btn { background: #1a1d24; color: #999; border: 1px solid #333; padding: 0.4rem 0.9rem;
               font-size: 0.85rem; cursor: pointer; }
    .tab-btn:first-child { border-radius: 4px 0 0 4px; }
    .tab-btn:last-child { border-radius: 0 4px 4px 0; border-left: none; }
    .tab-btn.active { background: #2b2f38; color: #e6e6e6; font-weight: bold; }
  </style>
</head>
<body>
  <h1>WAN Failover Monitor</h1>

  <div class="controls">
    <span>Range:
    {% for key in ranges %}
      <a href="/?range={{ key }}" class="{{ 'active' if key == selected_range else '' }}">{{ key }}</a>
    {% endfor %}
    </span>
    <button id="live-toggle-btn" class="live" onclick="toggleLive()">
      <span id="live-indicator" class="live"></span><span id="live-toggle-label">Live</span>
    </button>
    <span id="last-updated"></span>
  </div>

  <div>
    <div class="stat"><b id="stat-windows">--</b>degradation windows</div>
    <div class="stat"><b id="stat-minutes">--</b>total degraded time</div>
    <div class="stat"><b id="stat-events">--</b>failover actions</div>
  </div>

  <div id="alerts-panel">
    <h2 style="font-size:1rem; margin: 1rem 0 0.5rem;">
      Unresolved Alerts <span id="alerts-count-badge" class="count-badge"></span>
    </h2>
    <div id="alerts-list">Loading...</div>
  </div>

  <canvas id="chart" height="90"></canvas>

  <h2 style="font-size:1rem; margin: 1.5rem 0 0.5rem;">WAN Metrics (router-reported)</h2>
  <div class="wan-metrics-tabs" id="wan-metrics-tabs"></div>
  <div id="wan-metrics-error" class="alerts-error" style="display:none;"></div>
  <div id="wan-metrics-charts"></div>

  <a class="export" href="/report.csv?range={{ selected_range }}">Download ISP report (CSV)</a>

  <table>
    <thead>
      <tr><th>Start</th><th>End</th><th>Duration</th><th>Avg latency</th><th>Peak latency</th><th>Avg loss</th><th>Peak loss</th></tr>
    </thead>
    <tbody id="windows-tbody">
      <tr><td colspan="7">Loading...</td></tr>
    </tbody>
  </table>

  <script>
    const CURRENT_RANGE = {{ selected_range|tojson }};
    const DASHBOARD_TZ = {{ dashboard_timezone|tojson }};
    const REFRESH_MS = {{ refresh_interval_seconds }} * 1000;

    let chartInstance = null;
    let wanPortChartInstances = {};  // keyed by portId -- built dynamically since port count/names come from the API
    let wanPortTabsBuilt = false;
    let activeWanPortTab = null;
    let liveEnabled = true;
    let pollTimer = null;

    // Fixed color-by-metric-type within each port's chart (per your
    // preference): blue for throughput, red for latency.
    const RATE_COLOR = '#4a90e2';
    const LATENCY_COLOR = '#d64545';

    // Formats a unix timestamp in DASHBOARD_TZ as "YYYY-MM-DD HH:MM:SS" --
    // explicit IANA zone via Intl, NOT the browser's local timezone, so the
    // on-screen table always matches the CSV export regardless of where
    // you're viewing this from.
    function fmtTs(ts) {
      const parts = new Intl.DateTimeFormat('en-US', {
        timeZone: DASHBOARD_TZ, hour12: false,
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      }).formatToParts(new Date(ts * 1000));
      const p = {};
      parts.forEach(part => { p[part.type] = part.value; });
      return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`;
    }

    function updateChart(cycles) {
      const labels = cycles.map(d => fmtTs(d.ts));
      const latency = cycles.map(d => d.avg_latency_ms);
      const loss = cycles.map(d => d.loss_pct);

      if (chartInstance === null) {
        chartInstance = new Chart(document.getElementById('chart'), {
          type: 'line',
          data: {
            labels: labels,
            datasets: [
              { label: 'Latency (ms)', data: latency, borderColor: '#8ab4f8', yAxisID: 'y', pointRadius: 0, borderWidth: 1.5 },
              { label: 'Loss (%)', data: loss, borderColor: '#f28b82', yAxisID: 'y1', pointRadius: 0, borderWidth: 1.5 },
            ]
          },
          options: {
            animation: false,
            scales: {
              x: { ticks: { maxTicksLimit: 8, color: '#999' }, grid: { color: '#222' } },
              y: { type: 'linear', position: 'left', title: { display: true, text: 'ms' }, grid: { color: '#222' } },
              y1: { type: 'linear', position: 'right', title: { display: true, text: '%' }, grid: { display: false } },
            },
            plugins: { legend: { labels: { color: '#ccc' } } }
          }
        });
      } else {
        chartInstance.data.labels = labels;
        chartInstance.data.datasets[0].data = latency;
        chartInstance.data.datasets[1].data = loss;
        chartInstance.update('none');  // 'none' = no animation on refresh, avoids distracting flicker
      }
    }

    function buildWanPortTabs(ports) {
      const tabsContainer = document.getElementById('wan-metrics-tabs');
      const chartsContainer = document.getElementById('wan-metrics-charts');
      tabsContainer.innerHTML = '';
      chartsContainer.innerHTML = '';
      wanPortChartInstances = {};

      ports.forEach((port, i) => {
        const isActive = i === 0;
        if (isActive) activeWanPortTab = port.portId;

        const btn = document.createElement('button');
        btn.className = 'tab-btn' + (isActive ? ' active' : '');
        btn.textContent = port.portName;
        btn.dataset.portId = port.portId;
        btn.addEventListener('click', () => switchWanPortTab(port.portId));
        tabsContainer.appendChild(btn);

        const panel = document.createElement('div');
        panel.id = `wan-port-panel-${port.portId}`;
        panel.style.display = isActive ? 'block' : 'none';
        const canvas = document.createElement('canvas');
        canvas.id = `wan-port-canvas-${port.portId}`;
        canvas.height = 90;
        panel.appendChild(canvas);
        chartsContainer.appendChild(panel);
      });

      wanPortTabsBuilt = true;
    }

    function switchWanPortTab(portId) {
      activeWanPortTab = portId;
      document.querySelectorAll('#wan-metrics-tabs .tab-btn').forEach(btn => {
        // Compare as strings -- dataset values are always strings, portId from the API is a number.
        btn.className = 'tab-btn' + (String(btn.dataset.portId) === String(portId) ? ' active' : '');
      });
      document.querySelectorAll('#wan-metrics-charts > div').forEach(panel => {
        const panelPortId = panel.id.replace('wan-port-panel-', '');
        panel.style.display = String(panelPortId) === String(portId) ? 'block' : 'none';
      });
      // Chart.js can under-size a chart that was created (or last updated)
      // while its canvas was display:none -- force a resize now that it's
      // visible again, on whichever chart was just switched to.
      const chart = wanPortChartInstances[portId];
      if (chart) chart.resize();
    }

    function updateWanMetricsChart(resp) {
      const errorDiv = document.getElementById('wan-metrics-error');

      if (resp.error) {
        errorDiv.textContent = 'WAN metrics unavailable: ' + resp.error;
        errorDiv.style.display = 'block';
        document.getElementById('wan-metrics-tabs').style.display = 'none';
        document.getElementById('wan-metrics-charts').style.display = 'none';
        return;
      }
      errorDiv.style.display = 'none';
      document.getElementById('wan-metrics-tabs').style.display = 'block';
      document.getElementById('wan-metrics-charts').style.display = 'block';

      const ports = resp.ports || [];
      if (ports.length === 0) return;

      // Rebuild tabs/canvases only once, or if the actual set of ports
      // changes (e.g. a port added/removed) -- not on every refresh, which
      // would destroy and recreate chart instances unnecessarily and cause
      // visible flicker on every live-poll tick.
      const currentPortIds = ports.map(p => String(p.portId)).sort().join(',');
      const builtPortIds = Object.keys(wanPortChartInstances).sort().join(',');
      if (!wanPortTabsBuilt || (builtPortIds !== '' && currentPortIds !== builtPortIds)) {
        buildWanPortTabs(ports);
      }

      ports.forEach(port => {
        const labels = port.data.map(d => fmtTs(d.time));
        const datasets = [
          {
            label: 'Throughput', data: port.data.map(d => d.totalRate),
            borderColor: RATE_COLOR, yAxisID: 'y', pointRadius: 0, borderWidth: 1.5,
          },
          {
            label: 'Latency', data: port.data.map(d => d.latency),
            borderColor: LATENCY_COLOR, yAxisID: 'y1', pointRadius: 0, borderWidth: 1.5,
          },
        ];

        const existing = wanPortChartInstances[port.portId];
        if (!existing) {
          const canvas = document.getElementById(`wan-port-canvas-${port.portId}`);
          if (!canvas) return;  // tab structure not built yet for this port -- next refresh will catch it
          wanPortChartInstances[port.portId] = new Chart(canvas, {
            type: 'line',
            data: { labels: labels, datasets: datasets },
            options: {
              animation: false,
              scales: {
                x: { ticks: { maxTicksLimit: 8, color: '#999' }, grid: { color: '#222' } },
                // Unit is provisional -- see get_isp_load()'s docstring in
                // omada_client.py, not yet confirmed against a live response.
                y: { type: 'linear', position: 'left', title: { display: true, text: 'KB/s (unconfirmed unit)' }, grid: { color: '#222' } },
                y1: { type: 'linear', position: 'right', title: { display: true, text: 'ms' }, grid: { display: false } },
              },
              plugins: { legend: { labels: { color: '#ccc' } } }
            }
          });
        } else {
          existing.data.labels = labels;
          existing.data.datasets = datasets;
          existing.update('none');
        }
      });
    }

    function renderTable(windows) {
      const tbody = document.getElementById('windows-tbody');
      if (windows.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7">No degradation windows in this range.</td></tr>';
        return;
      }
      const rows = windows.slice().reverse().map(w => `
        <tr>
          <td>${fmtTs(w.start)}</td>
          <td>${fmtTs(w.end)}</td>
          <td>${w.duration_seconds.toFixed(0)}s</td>
          <td>${w.avg_latency_ms !== null ? w.avg_latency_ms.toFixed(0) + ' ms' : '\u2014'}</td>
          <td>${w.max_latency_ms !== null ? w.max_latency_ms.toFixed(0) + ' ms' : '\u2014'}</td>
          <td>${w.avg_loss_pct.toFixed(1)}%</td>
          <td>${w.max_loss_pct.toFixed(1)}%</td>
        </tr>
      `).join('');
      tbody.innerHTML = rows;
    }

    function renderStats(windows, events) {
      const totalBadMinutes = windows.reduce((sum, w) => sum + w.duration_seconds, 0) / 60.0;
      document.getElementById('stat-windows').textContent = windows.length;
      document.getElementById('stat-minutes').textContent = totalBadMinutes.toFixed(1) + ' min';
      document.getElementById('stat-events').textContent = events.length;
    }

    function escapeHtml(s) {
      const div = document.createElement('div');
      div.textContent = s == null ? '' : String(s);
      return div.innerHTML;
    }

    function resolveAlert(id, btn) {
      btn.disabled = true;
      btn.textContent = 'Resolving...';
      fetch('/api/alerts/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: id }),
      }).then(r => r.json()).then(data => {
        if (data.success) {
          loadData();  // refresh everything -- this alert should drop out of the unresolved list
        } else {
          console.error('Failed to resolve alert:', data.error);
          btn.disabled = false;
          btn.textContent = 'Acknowledge';
        }
      }).catch(err => {
        console.error('Failed to resolve alert:', err);
        btn.disabled = false;
        btn.textContent = 'Acknowledge';
      });
    }

    function renderAlerts(alertsResp) {
      const list = document.getElementById('alerts-list');
      const badge = document.getElementById('alerts-count-badge');

      if (alertsResp.error) {
        badge.textContent = '';
        list.innerHTML = `<div class="alerts-error">Alerts unavailable: ${escapeHtml(alertsResp.error)}</div>`;
        return;
      }

      badge.textContent = alertsResp.unresolved_count !== null ? alertsResp.unresolved_count : '';

      if (alertsResp.alerts.length === 0) {
        list.innerHTML = '<div class="alerts-empty">No unresolved alerts.</div>';
        return;
      }

      list.innerHTML = alertsResp.alerts.map(a => `
        <div class="alert-item">
          <span class="alert-level">${escapeHtml(a.level || a.module || '')}</span>
          <span class="alert-content">${escapeHtml(a.content)}</span>
          <span class="alert-time">${fmtTs(a.time / 1000)}</span>
          <button class="alert-ack-btn" data-alert-id="${escapeHtml(a.id)}">Acknowledge</button>
        </div>
      `).join('');

      list.querySelectorAll('.alert-ack-btn').forEach(btn => {
        btn.addEventListener('click', () => resolveAlert(btn.dataset.alertId, btn));
      });
    }

    function loadData() {
      Promise.all([
        fetch(`/api/cycles?range=${CURRENT_RANGE}`).then(r => r.json()),
        fetch(`/api/windows?range=${CURRENT_RANGE}`).then(r => r.json()),
        fetch(`/api/events?range=${CURRENT_RANGE}`).then(r => r.json()),
        fetch(`/api/alerts`).then(r => r.json()),
        fetch(`/api/isp-load?range=${CURRENT_RANGE}`).then(r => r.json()),
      ]).then(([cycles, windows, events, alerts, ispLoad]) => {
        updateChart(cycles);
        renderTable(windows);
        renderStats(windows, events);
        renderAlerts(alerts);
        updateWanMetricsChart(ispLoad);
        document.getElementById('last-updated').textContent =
          'Updated ' + new Date().toLocaleTimeString();
      }).catch(err => {
        console.error('Failed to load dashboard data:', err);
        document.getElementById('last-updated').textContent = 'Update failed -- see console';
      });
    }

    function startPolling() {
      if (pollTimer !== null) return;
      pollTimer = setInterval(loadData, REFRESH_MS);
    }

    function stopPolling() {
      if (pollTimer !== null) { clearInterval(pollTimer); pollTimer = null; }
    }

    function toggleLive() {
      liveEnabled = !liveEnabled;
      const btn = document.getElementById('live-toggle-btn');
      const indicator = document.getElementById('live-indicator');
      const label = document.getElementById('live-toggle-label');
      if (liveEnabled) {
        startPolling();
        loadData();  // refresh immediately on resume, don't wait for the next interval tick
        btn.className = 'live'; indicator.className = 'live'; label.textContent = 'Live';
      } else {
        stopPolling();
        btn.className = 'paused'; indicator.className = 'paused'; label.textContent = 'Paused';
      }
    }

    loadData();
    startPolling();
  </script>
</body>
</html>
"""


def _since_ts(range_key: str) -> float:
    seconds = RANGE_OPTIONS.get(range_key, RANGE_OPTIONS["24h"])
    return time.time() - seconds


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


@app.route("/")
def index():
    selected_range = request.args.get("range", "24h")
    if selected_range not in RANGE_OPTIONS:
        selected_range = "24h"

    return render_template_string(
        PAGE,
        ranges=list(RANGE_OPTIONS.keys()),
        selected_range=selected_range,
        dashboard_timezone=DASHBOARD_TIMEZONE,
        refresh_interval_seconds=REFRESH_INTERVAL_SECONDS,
    )


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
        return jsonify({"ports": result, "error": None})
    except Exception as e:
        log.warning("Failed to fetch ISP load: %s", e)
        return jsonify({"ports": [], "error": str(e)})


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
    port = int(os.environ.get("DASHBOARD_PORT", "8090"))
    app.run(host="0.0.0.0", port=port)
