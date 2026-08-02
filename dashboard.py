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
ISP_LOAD_REFRESH_INTERVAL_SECONDS = int(os.environ.get("DASHBOARD_ISP_LOAD_POLL_INTERVAL_SECONDS", "60"))

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
    .speedtest-controls { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.75rem; }
    .speedtest-controls select { background: #1a1d24; color: #e6e6e6; border: 1px solid #333;
                                  border-radius: 4px; padding: 0.4rem 0.6rem; font-size: 0.85rem; }
    #speedtest-start-btn { background: #2b6cb0; color: white; border: none; border-radius: 4px;
                            padding: 0.5rem 1rem; font-size: 0.85rem; cursor: pointer; }
    #speedtest-start-btn:disabled { background: #444; cursor: default; opacity: 0.7; }
    #speedtest-result { background: #14161c; border: 1px solid #2a2d35; border-radius: 6px;
                         padding: 0.9rem; font-size: 0.85rem; min-height: 1.5rem; }
    .speedtest-metric { display: inline-block; margin-right: 2rem; }
    .speedtest-metric b { font-size: 1.2rem; display: block; }
    .speedtest-progress-bar { background: #2a2d35; border-radius: 4px; height: 6px; margin-top: 0.6rem; overflow: hidden; }
    .speedtest-progress-fill { background: #4a90e2; height: 100%; transition: width 0.3s; }
    .active-wan-panel { display: flex; align-items: center; gap: 1rem; background: #14161c;
                         border: 1px solid #2a2d35; border-radius: 6px; padding: 0.7rem 1rem; margin-bottom: 1rem; }
    .active-wan-panel .wan-badge { background: #1c3a24; color: #3ecf6e; border-radius: 4px;
                                    padding: 0.2rem 0.6rem; font-weight: bold; font-size: 0.85rem; }
    #failover-btn { background: #a33; color: white; border: none; border-radius: 4px;
                     padding: 0.5rem 1rem; font-size: 0.85rem; cursor: pointer; margin-left: auto; }
    #failover-btn:hover:not(:disabled) { background: #c44; }
    #failover-btn:disabled { background: #444; cursor: default; opacity: 0.7; }
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

  <div class="active-wan-panel">
    <span>Active WAN: <span id="active-wan-name" class="wan-badge">--</span></span>
    <span id="backup-wan-info" style="color:#888; font-size:0.85rem;"></span>
    <button id="failover-btn" onclick="triggerFailover()" disabled>Switch WAN</button>
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

  <h2 id="ping-chart-title" style="font-size:1rem; margin: 1.5rem 0 0.5rem;">Latency &amp; Loss</h2>
  <canvas id="chart" height="90"></canvas>

  <h2 style="font-size:1rem; margin: 1.5rem 0 0.5rem;">
    WAN Metrics (router-reported) <span id="wan-metrics-data-age" style="color:#888; font-size:0.78rem; font-weight:normal;"></span>
  </h2>
  <div class="wan-metrics-tabs" id="wan-metrics-tabs"></div>
  <div id="wan-metrics-error" class="alerts-error" style="display:none;"></div>
  <div id="wan-metrics-charts"></div>

  <h2 style="font-size:1rem; margin: 1.5rem 0 0.5rem;">Speed Test</h2>
  <div class="speedtest-controls">
    <select id="speedtest-wan-select"><option>Loading WANs...</option></select>
    <button id="speedtest-start-btn" onclick="startSpeedTest()">Start Speed Test</button>
  </div>
  <div id="speedtest-result"></div>

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
    const ISP_LOAD_REFRESH_MS = {{ isp_load_refresh_interval_seconds }} * 1000;

    let chartInstance = null;
    let wanPortChartInstances = {};  // keyed by portId -- built dynamically since port count/names come from the API
    let wanPortTabsBuilt = false;
    let activeWanPortTab = null;
    let liveEnabled = true;
    let pollTimer = null;
    let wanMetricsPollTimer = null;

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

    let currentActiveWan = null;
    let currentBackupWan = null;
    let failoverInFlight = false;

    function renderActiveWan(resp) {
      const nameEl = document.getElementById('active-wan-name');
      const backupInfoEl = document.getElementById('backup-wan-info');
      const btn = document.getElementById('failover-btn');
      const chartTitle = document.getElementById('ping-chart-title');

      if (resp.error || !resp.active) {
        nameEl.textContent = 'unavailable';
        backupInfoEl.textContent = resp.error ? escapeHtml(resp.error) : '';
        btn.disabled = true;
        if (chartTitle) chartTitle.textContent = 'Latency & Loss';
        return;
      }

      currentActiveWan = resp.active;
      currentBackupWan = resp.backup;
      nameEl.textContent = resp.active.portName;
      backupInfoEl.textContent = resp.backup ? `(backup: ${resp.backup.portName})` : '';
      if (!failoverInFlight) {
        btn.disabled = !resp.backup;
        btn.textContent = resp.backup ? `Switch to ${resp.backup.portName}` : 'Switch WAN';
      }
      if (chartTitle) chartTitle.textContent = `Latency & Loss (reporting on: ${resp.active.portName})`;
    }

    function triggerFailover() {
      if (!currentActiveWan || !currentBackupWan || failoverInFlight) return;

      const confirmed = window.confirm(
        `This will immediately move traffic from "${currentActiveWan.portName}" to "${currentBackupWan.portName}".\n\n` +
        `This is a LIVE change to your network -- the same underlying action as running ` +
        `./test_load_balance_swap.sh failover on the command line.\n\nAre you sure?`
      );
      if (!confirmed) return;

      failoverInFlight = true;
      const btn = document.getElementById('failover-btn');
      btn.disabled = true;
      btn.textContent = 'Switching...';

      fetch('/api/failover', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: true }),
      }).then(r => r.json()).then(data => {
        failoverInFlight = false;
        if (data.success) {
          loadData();  // refresh everything -- badge, chart title, etc. should reflect the new state
        } else {
          alert('Failover failed: ' + data.error);
          btn.disabled = false;
          btn.textContent = currentBackupWan ? `Switch to ${currentBackupWan.portName}` : 'Switch WAN';
        }
      }).catch(err => {
        failoverInFlight = false;
        alert('Failover failed: ' + err);
        btn.disabled = false;
      });
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

      // Honest freshness indicator: when the underlying DATA last changed,
      // not when we last polled for it. This endpoint's data only updates
      // roughly every 5 minutes server-side (confirmed via direct testing --
      // identical responses 5s apart) -- our poll interval being shorter
      // than that doesn't make the data any fresher, so say so explicitly
      // rather than implying real-time updates that aren't actually happening.
      const latestTimes = ports.map(p => p.data.length ? p.data[p.data.length - 1].time : 0);
      const mostRecentTime = Math.max(...latestTimes, 0);
      const ageEl = document.getElementById('wan-metrics-data-age');
      if (ageEl && mostRecentTime > 0) {
        const ageSeconds = Math.max(0, Math.floor(Date.now() / 1000) - mostRecentTime);
        const ageMinutes = Math.floor(ageSeconds / 60);
        ageEl.textContent = ageMinutes < 1
          ? '(data as of: just now)'
          : `(data as of: ${ageMinutes}m ago -- this endpoint updates roughly every 5min)`;
      }

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

    let speedTestPollTimer = null;
    const SPEEDTEST_POLL_INTERVAL_MS = 2000;
    const SPEEDTEST_TIMEOUT_MS = 90000;

    function loadWanPortsForSpeedTest() {
      fetch('/api/device-capabilities').then(r => r.json()).then(cap => {
        if (cap.speedTestSupported === false) {
          document.getElementById('speedtest-wan-select').outerHTML =
            '<span style="color:#888;">Not available on this device</span>';
          document.getElementById('speedtest-start-btn').style.display = 'none';
          document.getElementById('speedtest-result').innerHTML =
            'This gateway reports it does not support speed tests via the API (confirmed by a real rejection: "This device does not support speed test.").';
          return;  // don't bother populating the port list, the feature can't work here regardless
        }
        // speedTestSupported === true, or null/unknown (e.g. credentials
        // missing) -- in the unknown case, still offer the button; a real
        // click will surface whatever the actual problem is via the normal
        // error path, same as it did when this genuinely wasn't supported.
        loadWanPortOptions();
      }).catch(err => {
        console.error('Failed to check device capabilities:', err);
        loadWanPortOptions();  // fail open -- let a real attempt surface the actual error
      });
    }

    function loadWanPortOptions() {
      fetch('/api/wan-ports').then(r => r.json()).then(resp => {
        const select = document.getElementById('speedtest-wan-select');
        if (resp.error) {
          select.innerHTML = `<option>Unavailable: ${escapeHtml(resp.error)}</option>`;
          document.getElementById('speedtest-start-btn').disabled = true;
          return;
        }
        if (resp.ports.length === 0) {
          select.innerHTML = '<option>No WAN ports found</option>';
          return;
        }
        select.innerHTML = resp.ports.map(p =>
          `<option value="${escapeHtml(p.portId)}">${escapeHtml(p.portName)}</option>`
        ).join('');
      }).catch(err => {
        console.error('Failed to load WAN ports for speed test:', err);
        document.getElementById('speedtest-wan-select').innerHTML = '<option>Failed to load</option>';
      });
    }

    function startSpeedTest() {
      const select = document.getElementById('speedtest-wan-select');
      const btn = document.getElementById('speedtest-start-btn');
      const resultDiv = document.getElementById('speedtest-result');
      const portUuid = select.value;
      if (!portUuid) return;

      select.disabled = true;
      btn.disabled = true;
      btn.textContent = 'Starting...';
      resultDiv.innerHTML = 'Starting speed test...';

      fetch('/api/speedtest/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ portUuid: portUuid }),
      }).then(r => r.json()).then(data => {
        if (!data.success) {
          resultDiv.innerHTML = `<span style="color:#f28b82">Failed to start: ${escapeHtml(data.error)}</span>`;
          select.disabled = false;
          btn.disabled = false;
          btn.textContent = 'Start Speed Test';
          return;
        }
        btn.textContent = 'Running...';
        // Port results are matched by the leading integer of the "1_hash"
        // portId string (e.g. "1_8ff0..." -> port 1) -- same convention
        // already used in monitor.py's check_primary_wan_health().
        const portNum = parseInt(portUuid.split('_')[0], 10);
        pollSpeedTestResult(portNum, Date.now());
      }).catch(err => {
        resultDiv.innerHTML = `<span style="color:#f28b82">Failed to start: ${escapeHtml(String(err))}</span>`;
        select.disabled = false;
        btn.disabled = false;
        btn.textContent = 'Start Speed Test';
      });
    }

    function pollSpeedTestResult(portNum, startedAt) {
      if (speedTestPollTimer !== null) { clearTimeout(speedTestPollTimer); speedTestPollTimer = null; }

      const select = document.getElementById('speedtest-wan-select');
      const btn = document.getElementById('speedtest-start-btn');
      const resultDiv = document.getElementById('speedtest-result');

      if (Date.now() - startedAt > SPEEDTEST_TIMEOUT_MS) {
        resultDiv.innerHTML = '<span style="color:#f28b82">Speed test timed out waiting for a result.</span>';
        select.disabled = false;
        btn.disabled = false;
        btn.textContent = 'Start Speed Test';
        return;
      }

      fetch('/api/speedtest/result').then(r => r.json()).then(data => {
        if (data.error) {
          resultDiv.innerHTML = `<span style="color:#f28b82">Error checking result: ${escapeHtml(data.error)}</span>`;
          select.disabled = false;
          btn.disabled = false;
          btn.textContent = 'Start Speed Test';
          return;
        }

        const results = (data.result && data.result.portSpeedResults) || [];
        const portResult = results.find(r => r.portId === portNum);

        if (!portResult) {
          // No result for this port yet -- keep waiting.
          resultDiv.innerHTML = 'Waiting for result...';
          speedTestPollTimer = setTimeout(() => pollSpeedTestResult(portNum, startedAt), SPEEDTEST_POLL_INTERVAL_MS);
          return;
        }

        const progress = portResult.progress || 0;
        resultDiv.innerHTML = `
          <div class="speedtest-metric"><b>${portResult.down ?? '--'}</b>Download (unit unconfirmed)</div>
          <div class="speedtest-metric"><b>${portResult.up ?? '--'}</b>Upload (unit unconfirmed)</div>
          <div class="speedtest-metric"><b>${portResult.latency ?? '--'} ms</b>Latency</div>
          <div>${escapeHtml(portResult.serverName || '')} ${escapeHtml(portResult.serverLocation || '')}</div>
          <div class="speedtest-progress-bar"><div class="speedtest-progress-fill" style="width:${Math.min(progress, 100)}%"></div></div>
        `;

        if (progress >= 100) {
          select.disabled = false;
          btn.disabled = false;
          btn.textContent = 'Start Speed Test';
        } else {
          speedTestPollTimer = setTimeout(() => pollSpeedTestResult(portNum, startedAt), SPEEDTEST_POLL_INTERVAL_MS);
        }
      }).catch(err => {
        resultDiv.innerHTML = `<span style="color:#f28b82">Error checking result: ${escapeHtml(String(err))}</span>`;
        select.disabled = false;
        btn.disabled = false;
        btn.textContent = 'Start Speed Test';
      });
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
        fetch(`/api/active-wan`).then(r => r.json()),
      ]).then(([cycles, windows, events, alerts, activeWan]) => {
        updateChart(cycles);
        renderTable(windows);
        renderStats(windows, events);
        renderAlerts(alerts);
        renderActiveWan(activeWan);
        document.getElementById('last-updated').textContent =
          'Updated ' + new Date().toLocaleTimeString();
      }).catch(err => {
        console.error('Failed to load dashboard data:', err);
        document.getElementById('last-updated').textContent = 'Update failed -- see console';
      });
    }

    function loadWanMetrics() {
      fetch(`/api/isp-load?range=${CURRENT_RANGE}`).then(r => r.json())
        .then(ispLoad => updateWanMetricsChart(ispLoad))
        .catch(err => console.error('Failed to load WAN metrics:', err));
    }

    function startPolling() {
      if (pollTimer === null) pollTimer = setInterval(loadData, REFRESH_MS);
      if (wanMetricsPollTimer === null) wanMetricsPollTimer = setInterval(loadWanMetrics, ISP_LOAD_REFRESH_MS);
    }

    function stopPolling() {
      if (pollTimer !== null) { clearInterval(pollTimer); pollTimer = null; }
      if (wanMetricsPollTimer !== null) { clearInterval(wanMetricsPollTimer); wanMetricsPollTimer = null; }
    }

    function toggleLive() {
      liveEnabled = !liveEnabled;
      const btn = document.getElementById('live-toggle-btn');
      const indicator = document.getElementById('live-indicator');
      const label = document.getElementById('live-toggle-label');
      if (liveEnabled) {
        startPolling();
        loadData();       // refresh immediately on resume, don't wait for the next interval tick
        loadWanMetrics();
        btn.className = 'live'; indicator.className = 'live'; label.textContent = 'Live';
      } else {
        stopPolling();
        btn.className = 'paused'; indicator.className = 'paused'; label.textContent = 'Paused';
      }
    }

    loadData();
    loadWanMetrics();
    startPolling();
    loadWanPortsForSpeedTest();
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
        isp_load_refresh_interval_seconds=ISP_LOAD_REFRESH_INTERVAL_SECONDS,
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
            {"portId": p["portId"], "portName": p["portName"]}
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

    gateway_mac = os.environ.get("OMADA_GATEWAY_MAC", "")
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

    gateway_mac = os.environ.get("OMADA_GATEWAY_MAC", "")
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

    gateway_mac = os.environ.get("OMADA_GATEWAY_MAC", "")
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
        name_by_id = {p["portId"]: p["portName"] for p in wan_config.get("wanPortsConfig", [])}
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
