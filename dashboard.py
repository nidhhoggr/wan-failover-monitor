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
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, render_template_string, request

import db

app = Flask(__name__)

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

  <canvas id="chart" height="90"></canvas>

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
    let liveEnabled = true;
    let pollTimer = null;

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

    function loadData() {
      Promise.all([
        fetch(`/api/cycles?range=${CURRENT_RANGE}`).then(r => r.json()),
        fetch(`/api/windows?range=${CURRENT_RANGE}`).then(r => r.json()),
        fetch(`/api/events?range=${CURRENT_RANGE}`).then(r => r.json()),
      ]).then(([cycles, windows, events]) => {
        updateChart(cycles);
        renderTable(windows);
        renderStats(windows, events);
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
