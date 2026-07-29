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

from flask import Flask, Response, jsonify, render_template_string, request

import db

app = Flask(__name__)

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
    h1 { font-size: 1.3rem; }
    .controls { margin-bottom: 1rem; }
    .controls a { color: #8ab4f8; margin-right: 1rem; text-decoration: none; }
    .controls a.active { font-weight: bold; text-decoration: underline; }
    table { border-collapse: collapse; width: 100%; margin-top: 1.5rem; font-size: 0.85rem; }
    th, td { border: 1px solid #333; padding: 0.4rem 0.6rem; text-align: right; }
    th { background: #1a1d24; }
    td:first-child, th:first-child { text-align: left; }
    .badge { padding: 0.1rem 0.4rem; border-radius: 3px; font-size: 0.75rem; }
    .badge.bad { background: #5c1e1e; color: #ffb3b3; }
    canvas { background: #14161c; border-radius: 6px; padding: 1rem; }
    .export { margin-top: 1rem; display: inline-block; background: #2b6cb0; color: white; padding: 0.5rem 1rem;
              border-radius: 4px; text-decoration: none; font-size: 0.85rem; }
    .stat { display: inline-block; margin-right: 2rem; }
    .stat b { font-size: 1.4rem; display: block; }
  </style>
</head>
<body>
  <h1>WAN Failover Monitor</h1>
  <div class="controls">
    Range:
    {% for key in ranges %}
      <a href="/?range={{ key }}" class="{{ 'active' if key == selected_range else '' }}">{{ key }}</a>
    {% endfor %}
  </div>

  <div>
    <div class="stat"><b>{{ windows|length }}</b>degradation windows</div>
    <div class="stat"><b>{{ '%.1f'|format(total_bad_minutes) }} min</b>total degraded time</div>
    <div class="stat"><b>{{ event_count }}</b>failover actions</div>
  </div>

  <canvas id="chart" height="90"></canvas>

  <a class="export" href="/report.csv?range={{ selected_range }}">Download ISP report (CSV)</a>

  <table>
    <tr><th>Start</th><th>End</th><th>Duration</th><th>Avg latency</th><th>Peak latency</th><th>Avg loss</th><th>Peak loss</th></tr>
    {% for w in windows %}
    <tr>
      <td>{{ w.start_str }}</td>
      <td>{{ w.end_str }}</td>
      <td>{{ '%.0f'|format(w.duration_seconds) }}s</td>
      <td>{{ '%.0f'|format(w.avg_latency_ms) if w.avg_latency_ms is not none else '—' }} ms</td>
      <td>{{ '%.0f'|format(w.max_latency_ms) if w.max_latency_ms is not none else '—' }} ms</td>
      <td>{{ '%.1f'|format(w.avg_loss_pct) }}%</td>
      <td>{{ '%.1f'|format(w.max_loss_pct) }}%</td>
    </tr>
    {% endfor %}
  </table>

  <script>
    fetch('/api/cycles?range={{ selected_range }}').then(r => r.json()).then(data => {
      const labels = data.map(d => new Date(d.ts * 1000).toLocaleString());
      const latency = data.map(d => d.avg_latency_ms === Infinity ? null : d.avg_latency_ms);
      const loss = data.map(d => d.loss_pct);
      new Chart(document.getElementById('chart'), {
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
    });
  </script>
</body>
</html>
"""


def _since_ts(range_key: str) -> float:
    seconds = RANGE_OPTIONS.get(range_key, RANGE_OPTIONS["24h"])
    return time.time() - seconds


def _fmt(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


@app.route("/")
def index():
    selected_range = request.args.get("range", "24h")
    if selected_range not in RANGE_OPTIONS:
        selected_range = "24h"
    since = _since_ts(selected_range)

    windows = db.compute_degradation_windows(since)
    for w in windows:
        w["start_str"] = _fmt(w["start"])
        w["end_str"] = _fmt(w["end"])

    events = db.fetch_events(since)
    total_bad_seconds = sum(w["duration_seconds"] for w in windows)

    return render_template_string(
        PAGE,
        ranges=list(RANGE_OPTIONS.keys()),
        selected_range=selected_range,
        windows=list(reversed(windows)),
        total_bad_minutes=total_bad_seconds / 60.0,
        event_count=len(events),
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
