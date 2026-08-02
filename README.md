# wan-failover-monitor

Docker container that watches internet health from the LAN and, on
sustained latency/packet-loss degradation, calls the Omada Open API to
force a WAN failover on your ER605 -- faster and smarter-triggered than the
router's default "is it completely down" failover. A second container
serves a live web dashboard (charts, alerts, manual controls) over the same
data. See "Dashboard" below for the full feature list.

## Read this before deploying

**The trigger direction and the fail-back direction use different data
sources, deliberately.**

- **Trigger** (fail *over* to backup): LAN-side ping health. A container on
  your LAN can't independently probe an inactive WAN, but for the trigger
  decision that's fine -- "is whatever WAN is currently active bad" is
  exactly the question you want answered, and that's what LAN-side pings
  correctly measure.
- **Fail-back** (return to primary): the router's own API-reported WAN
  status (`check_primary_wan_health()`), not LAN-side pings. This one
  genuinely needed the API, not just LAN pings -- see "Fail-back: why it's
  based on primary WAN health" below for the full story of why the obvious
  LAN-ping approach for this direction is actively wrong, not just less
  accurate.

Both paths are confirmed working end-to-end against a real ER605 on Omada
Central, including live tests where traffic actually moved. Nothing here is
a stubbed placeholder.

## Architecture

```
+------------------------+     ping x3 targets      +---------------------+
|  wan-failover-monitor   | -----------------------> |  1.1.1.1 / 8.8.8.8   |
|  (trigger direction)     |                          |  9.9.9.9 (etc.)      |
|                          |                          +---------------------+
|  every 5s: aggregate     |
|  latency + loss ->       |     OAuth2 + PUT internet/load-balance
|  hysteresis state         | ------------------------------------------> Omada
|  machine (trigger)         |                                           Controller
|                              |     GET gateways/{mac}/wan-status              |
|  every 30s: poll primary      | <---------------------------------------------+
|  health -> stability window    |                                             v
|  (fail-back)                     | ------------------------------------> ER605 gateway
+-----------+--------------+                                              (WAN priority
            |  writes                                                      flipped)
            v
     +--------------+     reads      +----------------------+
     |  sqlite db    | <------------ |  wan-failover-dashboard |
     |  (named volume)|                |  Flask, port 8090       |
     +--------------+                +----------------------+
                                          |  also calls Omada API
                                          |  directly for: alerts,
                                          v  speed test, active-WAN,
                                     Omada Controller  manual failover trigger
```

## Dashboard

The `wan-failover-dashboard` container serves a live web UI at
`http://<host>:8090`, reading from the same sqlite db `monitor.py` writes to
(WAL mode, safe single-writer/single-reader), plus making some of its own
direct Omada API calls for things the monitor doesn't otherwise track
(alerts, speed tests, live WAN status).

**Latency & Loss chart** -- your own LAN-side ping data, the same signal
that drives the failover trigger. Its title dynamically shows which WAN
it's currently reporting on (e.g. "Latency & Loss (reporting on: WAN)"),
since this necessarily reflects whichever WAN is currently active, not
always the same physical link.

**WAN Metrics** -- one tab per physical WAN port, each showing router-
reported throughput (blue) and latency (red) together, straight from
Omada's `dashboard/isp-load` endpoint -- independent of this monitor's own
ping data, including for the currently-*inactive* WAN. This endpoint's data
only updates roughly every 5 minutes server-side (confirmed via direct
testing), so it polls on its own slower cadence
(`DASHBOARD_ISP_LOAD_POLL_INTERVAL_SECONDS`, default 60s) with an honest
"(data as of: Xm ago)" label, rather than implying real-time updates that
aren't actually happening.

**Degradation windows table + CSV export** -- contiguous runs of bad
ping cycles collapsed into start/end/duration/avg+peak latency/avg+peak
loss. This is *not* gated by the failover trigger threshold -- a single bad
cycle shows up as a window on its own, independent of whether it ever grew
into a real failover. Cross-reference against the "failover actions" stat
(or the `events` table) if you need to know which windows actually
triggered something. The CSV export is shaped for handing to an ISP as
supporting evidence in an SLA dispute -- see the caveat below about what
that evidence actually proves.

**Active WAN + manual failover trigger** -- a badge showing current
primary/backup, refreshed on the live-poll cycle so it reflects automatic
failovers too, not just manual ones. The "Switch to X" button fetches live
state fresh at click time and calls the exact same `set_active_wan()` used
everywhere else in this project -- functionally identical to running
`./test_load_balance_swap.sh failover` by hand, gated behind a real browser
confirm dialog.

**Alerts panel** -- top 3 unresolved Omada site alerts (newest first),
pulled live via `logs/alerts`, with a working "Acknowledge" button that
resolves the alert through the API (`logs/alerts/resolve`) and refreshes
the list. Separate from this monitor's own degradation windows -- these are
Omada's own alert log (device offline, etc.), not derived from ping data.

**Speed Test** -- select a WAN, click Start, watch a live progress bar via
polling (`gateways/{mac}/speedTest` + `speedTestResult`). Checks
`osgCap.speedTest` up front and hides the button entirely with an
explanation if your gateway/firmware doesn't support it via the API --
confirmed this is a real, common limitation on some ER605 firmware
versions (TP-Link only added API speed test support around firmware 2.4.0,
and even that had rocky early reports -- see git history / conversation
log for the full investigation if you hit this).

**Live refresh** -- polls every `DASHBOARD_REFRESH_INTERVAL_SECONDS`
(default 15s) for most panels, with a Live/Paused toggle. WAN Metrics polls
separately and slower (see above) since its underlying data can't change
that often regardless of how often you ask.

**Light/dark theme** -- toggle next to the Live button, defaults to your
OS-level preference, persists via `localStorage`.

**Danger Zone -> Truncate Database** -- clears ping-cycle/event history
(chart, table, CSV data) but deliberately does NOT touch `monitor_state`
(which WAN is active, the persisted `failed_over` flag) -- wiping that
would make the monitor forget its real operational state mid-flight, a
different and more dangerous kind of reset than just clearing historical
charts. Gated behind typing `DELETE` to confirm, not just a yes/no dialog,
given it's genuinely irreversible.

**Timezone** -- the table and CSV export use `DASHBOARD_TIMEZONE` (default
`America/Los_Angeles`, DST-aware), not the container's default UTC. The
chart used to use your browser's local timezone instead, which was
inconsistent with the table -- both now use the same explicit configured
zone via `Intl.DateTimeFormat`.

**Note on what the CSV proves to an ISP**: these are round-trip times and
loss to public resolvers as seen from your LAN, not a certified line-
quality measurement -- useful as your own supporting evidence, but don't
expect an ISP to treat a self-hosted CSV as authoritative the way they'd
treat their own NOC's monitoring. It's leverage for a conversation, not a
legal instrument.

## Setup

Everything below has been confirmed working end-to-end against a real
ER605 on Omada Central, including a live failover test where traffic
actually moved to the backup WAN. The three helper scripts (`get_site_id.sh`,
`get_wan_ports_config.sh`, `test_load_balance_swap.sh`) are what got us
there and are worth keeping around for re-verification after any firmware
or controller upgrade, since TP-Link has changed these paths before.

1. **Register an Open API application**: Global View > Settings > Platform
   Integrations > Open API > Add New App. Use Client Credentials mode (this
   is a headless container, not a user login flow). Give it a role with
   write access (Administrator, not Viewer) -- Viewer can read config but
   can't trigger the failover write call. Scope Site Privileges to just the
   site with the ER605. This gives you `OMADA_CLIENT_ID` / `OMADA_CLIENT_SECRET`.

   Note: if you're on Omada Central (cloud), the free Essentials tier has
   no Open API access at all -- you need at least a Standard license bound
   to the gateway.

2. **Find `OMADA_OMADAC_ID` and `OMADA_SITE_ID`.** Run `get_site_id.sh`
   (reads `.env`, does the OAuth2 exchange, lists sites) to confirm the
   site ID. The omadac ID is shown in the same Platform Integrations panel
   as the app credentials.

3. **Find your real WAN port IDs.** These are NOT plain integers -- the
   real format is a string like `1_8ff0def98a03428b93d15678efa14052`. Run:
   ```
   ./get_wan_ports_config.sh '/openapi/v1/{omadacId}/sites/{siteId}/internet/ports-config'
   ```
   and match by `portName` ("WAN" vs "WAN/LAN1") or by whichever hostname
   you set on each WAN's DHCP config. Drop the two `portId` values into
   `WAN_PRIMARY_PORT_ID` / `WAN_BACKUP_PORT_ID` in `.env`.

4. **The failover call itself**: `omada_client.py`'s `set_active_wan()`
   targets `PUT /openapi/v1/{omadacId}/sites/{siteId}/internet/load-balance`
   -- this is the real Link Backup config (`primaryWans`, `backupWan`,
   `linkBackup`, `backupMode`, `weights`), a different endpoint than the
   WAN connection-type config from step 3. It fetches the current config
   and only swaps `primaryWans`/`backupWan`, echoing every other field back
   unchanged. You can re-verify this any time with:
   ```
   ./test_load_balance_swap.sh show       # read-only, always safe
   ./test_load_balance_swap.sh failover    # LIVE -- actually moves traffic
   ./test_load_balance_swap.sh failback
   ```
   `failover`/`failback` require typed `yes` confirmation since they're
   real writes with real network impact -- don't run them without knowing
   that.

5. **Copy `.env.example` to `.env` and fill it in.** Leave `DRY_RUN=true`.

6. **Run it and watch the logs for at least a day** before flipping
   `DRY_RUN=false` (see "Operations: Docker commands" below for the full
   command reference -- `docker compose up -d --build` to start,
   `docker compose logs -f wan-failover-monitor` to watch):
   You're looking for: does it ever hit `CONSECUTIVE_BAD_TO_TRIGGER` when
   the network was actually fine (false positive)? Tune
   `LATENCY_THRESHOLD_MS` / `PACKET_LOSS_THRESHOLD_PCT` /
   `CONSECUTIVE_BAD_TO_TRIGGER` against what you observe.

7. **Flip `DRY_RUN=false`** once you trust the thresholds. The API call
   itself is already proven working -- what's left to validate is whether
   your *thresholds* are tuned right, not whether the mechanism works.

## Tuning notes

- `CONSECUTIVE_BAD_TO_TRIGGER=12` at `CHECK_INTERVAL_SECONDS=5` is ~60s to
  trigger, matching your 1-minute target, with headroom to tighten further
  once you've seen real false-positive behavior. Don't start below that --
  a transient 20-second blip (a single congested minute, a Wi-Fi backhaul
  hiccup upstream) shouldn't cost you a failover.
- `CONSECUTIVE_GOOD_TO_FAILBACK` is no longer used for the fail-back
  decision -- see "Fail-back: why it's based on primary WAN health" below.
  Left in place only because `update_streaks()` computes `consecutive_good`
  alongside `consecutive_bad` as a byproduct.
- `COOLDOWN_SECONDS` is the real backstop against flapping -- even if the
  hysteresis counters would trigger again immediately, this blocks it.
- The throughput check is off by default and samples rarely
  (`THROUGHPUT_CHECK_EVERY_N_CYCLES=60`) on purpose -- it's real billed
  bytes, especially painful if your backup WAN is cellular/satellite with a
  data cap. Only turn it on if latency/loss alone isn't catching your
  client's actual complaint (e.g., a saturated-but-technically-low-latency
  link).

## Decision logic: how a cycle becomes a failover

### What makes a single cycle "bad"

A cycle is bad if **either** condition is true -- they're not both required:
- `avg_latency_ms > LATENCY_THRESHOLD_MS` (default 150ms), OR
- `loss_pct > PACKET_LOSS_THRESHOLD_PCT` (default 15%)

100% loss is not required. A cycle with 20% loss and normal latency counts
as bad on its own, same as a cycle with 0% loss but 300ms latency.

### From "bad cycles" to an actual failover: the streak counter

`CONSECUTIVE_BAD_TO_TRIGGER` (default 12) counts consecutive bad cycles.
Once it's reached, `set_active_wan()` fires to fail over to the backup
(subject to `COOLDOWN_SECONDS`, see below). Fail-back is handled
differently -- see the next section, since a symmetric "consecutive good
cycles" approach turns out to be actively wrong for that direction.

### Fail-back: why it's based on primary WAN health, not LAN-side pings

This started as the most important gotcha in the whole design, so it's worth
explaining the history, not just the current behavior.

**The problem with the obvious approach.** The natural first design mirrors
the trigger logic: count consecutive good ping cycles, fail back once enough
accumulate. This is broken, though, because once failed over, the monitor's
pings ride the *backup* WAN -- so "consecutive good cycles" after a failover
measures "is the backup healthy," not "has the primary recovered." Those are
different questions. Left automatic, this produces a real failure loop: fail
over to a healthy backup -> backup stays healthy (trivially, you're riding
it) -> good-cycle threshold hits -> auto fail-back onto the still-broken
primary -> bad cycles resume -> fail over again -> repeat indefinitely,
regardless of whether the primary ever actually recovers. Also relevant if
your backup WAN has a data cap (e.g. cellular/satellite): each pointless
round-trip burns real data for no benefit.

**The actual fix: ask the primary directly.** `check_primary_wan_health()`
in `monitor.py` queries the primary WAN's real status via the Omada API --
data the router's own probes produce regardless of which WAN is currently
active, sidestepping the LAN-side blind spot entirely. This is polled on
its own cadence (`PRIMARY_HEALTH_POLL_INTERVAL_SECONDS`, default 30s)
rather than every ping cycle, since it's a real API call.

**Confirmed working (2026-07-29).** The endpoint is
`GET /openapi/v1/{omadacId}/sites/{siteId}/gateways/{gatewayMac}/wan-status`
-- under the **Gateway** category in Knife4j, not "Wired Network" (where
`getWanPortsConfig` and `getInternetLoadBalance` live, which is why it took
a while to find -- both of those are configuration, not live status, and
neither has an online/offline field). It returns a list of per-port status
entries with real-time `latency` (ms), `loss` (%), `internetState` (0/1),
`status` (0/1 physical link), and `healthLevel`, matched to physical port
numbers -- response was checked against the Omada Central UI's own Ports >
WAN tab and matched exactly.

**"Healthy" means three things, all at once, not just connectivity.**
`check_primary_wan_health()` extracts the port number from
`WAN_PRIMARY_PORT_ID` (e.g. `"1_8ff0..."` -> port `1`), finds that port's
entry, and requires ALL of:
- `internetState == 1`
- `latency <= PRIMARY_HEALTHY_LATENCY_THRESHOLD_MS` (default 100ms)
- `loss <= PRIMARY_HEALTHY_LOSS_THRESHOLD_PCT` (default 5%)

`internetState` alone isn't enough -- a WAN can report "connected" while
still exhibiting the same degradation (high latency, lossy) that caused the
original failover in the first place. These thresholds are intentionally
stricter than `LATENCY_THRESHOLD_MS`/`PACKET_LOSS_THRESHOLD_PCT` (which
gate the LAN-ping failover *trigger*) -- the bar for "trustworthy enough to
fail back onto" should sit above the bar for "bad enough to fail off of,"
or you risk failing back onto a link that's merely just-below the failure
threshold and triggering right back off it minutes later. Missing/null
latency or loss (e.g. while the port is actually down) fails the check
rather than passing it by default.

**The 5-minute stability window.** A single healthy poll -- even one that
clears all three conditions above -- isn't enough confidence to act on.
`PRIMARY_HEALTHY_STABILITY_SECONDS` (default 300) requires the primary to
pass the full health check continuously for that long before fail-back is
even considered; any single failing poll during the window resets the
clock to zero. This is deliberately strict (no tolerance for blips, unlike
the ping-side debounce below) -- an intermittently-flapping primary
shouldn't count as recovered.

**`AUTO_FAILBACK_ENABLED` still defaults to false.** Even though the health
check is now real, the flag stays conservative by default -- watch it
correctly detect at least one real recovery in the logs before trusting it
with real automatic action. Even once the primary clears the full stability
window, this flag gates whether that actually triggers `set_active_wan()`
or just logs a "confirmed healthy, run this manually" reminder:
```
./test_load_balance_swap.sh failback
```

### Debounce: `STREAK_TOLERANCE_CYCLES`

Naively, a *single* cycle going against the current streak would reset it
entirely -- e.g. 11 bad cycles in a row, then one lucky good ping, and the
count goes back to 0, needing a fresh 12-cycle run to actually trigger. In
practice this let one-off noise (a single dropped ping to one of several
targets, unrelated to real link health) meaningfully delay both detection
and fail-back.

`STREAK_TOLERANCE_CYCLES` (default 2) fixes this: it takes this many
*consecutive* opposite-direction cycles to actually break a streak. Fewer
than that, and the cycle is absorbed -- the counter just pauses for that
one cycle rather than resetting or advancing. `1` restores the old strict
behavior (any single opposite cycle resets immediately).

Critically, this is a pause, not a discount: bad cycles on either side of
an absorbed blip still add up toward the same total. `update_streaks()` in
`monitor.py` is the actual implementation -- worth reading directly since
the interaction is easier to see in code than prose. Two worked examples,
both verified against the real function, not hand-traced:

**Example 1** -- 7 bad, 1 good (absorbed), 5 more bad, `STREAK_TOLERANCE_CYCLES=2`:
```
cycle  1-7  bad  -> consecutive_bad climbs 1..7
cycle  8    good -> consecutive_bad stays at 7 (absorbed, opposite_streak=1 < 2)
cycle  9-13 bad  -> consecutive_bad climbs 8..12  <-- TRIGGERS at cycle 13
```
7 + 5 = 12. The lone good cycle cost nothing -- total bad cycles needed to
trigger is unchanged, it just took one extra wall-clock cycle to get there.

**Example 2** -- 7 bad, 1 good, 3 bad, 1 good, 2 bad -- multiple separate
blips, same result:
```
cycle  1-7   bad  -> consecutive_bad climbs 1..7
cycle  8     good -> absorbed, stays at 7
cycle  9-11  bad  -> climbs 8..10
cycle  12    good -> absorbed, stays at 10
cycle  13-14 bad  -> climbs 11..12  <-- TRIGGERS at cycle 14
```
7 + 3 + 2 = 12 again. Any number of *isolated single* blips can interrupt
a run without resetting it -- each one just pauses the count for that cycle.
What actually resets the streak is **2 or more consecutive** opposite-direction
cycles at any point; that's a real, sustained change in conditions, not noise,
and correctly starts a fresh count from that point.

### Cooldown is separate from the streak/stability logic

`COOLDOWN_SECONDS` (default 120) is a hard floor between any two actions,
independent of both the trigger streak counter and the fail-back stability
window above. Even if `consecutive_bad` legitimately hits its threshold, or
the primary clears its full stability window, no action fires until this
many seconds have passed since the last one. This is what prevents a
genuinely flapping link from hammering the Omada API with rapid-fire
failover/fail-back calls.

## Testing the decision logic

### Fast, no network required: replay a sequence against the real function

`update_streaks()` is a pure function (no I/O, no state outside its
arguments) specifically so it can be tested this way. Any scenario you want
to reason about, verify it against the actual code rather than by hand:

```python
import monitor

cb, cg, op = 0, 0, 0
sequence = [True]*7 + [False] + [True]*5   # True = bad cycle, False = good
for i, is_bad in enumerate(sequence, start=1):
    cb, cg, op = monitor.update_streaks(is_bad, cb, cg, op)
    print(f"cycle {i}: bad={is_bad} consecutive_bad={cb} consecutive_good={cg}",
          "<<< TRIGGERS" if cb >= 12 else "")
```
Run this from inside the container (`docker compose exec wan-failover-monitor
python3`) or locally with the repo's Python environment -- it needs nothing
but the `monitor.py` file itself.

### End-to-end, against the real container: force synthetic bad cycles

This validates the whole pipeline (ping aggregation -> streak logic ->
`[DRY_RUN]` logging, or a real Omada call if `DRY_RUN=false`), not just the
counter math.

1. Point `PING_TARGETS` at a reserved, never-routed address block so every
   ping deterministically times out, without needing to actually break
   anything on your real network:
   ```
   PING_TARGETS=192.0.2.1,192.0.2.2,192.0.2.3
   ```
   (`192.0.2.0/24` is TEST-NET-1, reserved for documentation/testing --
   guaranteed 100% loss, never a false signal from a real outage.)

2. Optionally shrink `CONSECUTIVE_BAD_TO_TRIGGER` and `CHECK_INTERVAL_SECONDS`
   temporarily so you're not waiting minutes per test run. Remember to
   revert these before real deployment -- they're only for fast iteration.

3. Rebuild and recreate (not just `restart` -- that doesn't reload `.env`):
   ```
   docker compose up -d --build --force-recreate wan-failover-monitor
   docker compose logs -f wan-failover-monitor
   ```
   Set `LOG_LEVEL=DEBUG` too if you want to see every cycle's latency/loss,
   not just the eventual trigger/fail-back lines.

4. To test fail-back, note it no longer reacts to `PING_TARGETS` at all --
   that path was specifically removed for being wrong (see "Fail-back: why
   it's based on primary WAN health" above). It reacts to the real primary
   WAN status instead, polled every `PRIMARY_HEALTH_POLL_INTERVAL_SECONDS`.
   Shrink that and `PRIMARY_HEALTHY_STABILITY_SECONDS` temporarily (e.g. 5s
   / 15s) the same way you'd shrink `CONSECUTIVE_BAD_TO_TRIGGER`, rebuild,
   and watch for `primary WAN reported healthy -- starting Xs stability
   window` followed by the eventual fail-back log once the window clears --
   this will fire off your real primary's actual current status, so if it's
   genuinely healthy right now, you should see this happen without needing
   to force anything.

5. Revert all test values (`PING_TARGETS`, `CONSECUTIVE_BAD_TO_TRIGGER`,
   `CHECK_INTERVAL_SECONDS`, `LOG_LEVEL`, and the health-polling values if
   you touched them) back to your real tuned settings and recreate one
   final time before leaving it running for real.

### Why state survives a container restart

`monitor_state` in `db.py` persists `failed_over` and `last_action_time`
across restarts specifically so this works -- without it, a fresh process
always starts assuming it's on the primary WAN, which would be wrong (and
untestable this way) if the container restarts while genuinely failed over.

## Files

- `monitor.py` -- main loop: ping aggregation, hysteresis trigger state
  machine, primary-health-based fail-back with stability window, optional
  throughput sampling, calls into `omada_client`, persists to `db`.
- `omada_client.py` -- OAuth2 client-credentials auth + every confirmed
  Omada Open API call this project uses (gateway status, WAN config/status,
  load-balance read/write, alert logs read/resolve, ISP load stats, speed
  test start/result) -- see each method's docstring for the exact
  endpoint/verb and confirmation status.
- `db.py` -- shared SQLite persistence: cycles, events, degradation-window
  aggregation, persisted `monitor_state` (survives restarts), and
  `truncate_history()` for the dashboard's Danger Zone.
- `dashboard.py` -- Flask app serving the full web UI described above.
- `get_site_id.sh` -- standalone script to look up your `OMADA_SITE_ID`.
- `get_wan_ports_config.sh` -- standalone script to call any Open API GET
  endpoint by path (with query string support), used throughout this
  project's development to find and verify real endpoints before wiring
  them into code.
- `test_load_balance_swap.sh` -- standalone script to test/re-verify the
  actual failover write call (`show`/`failover`/`failback`), with typed
  confirmation before any live write. The dashboard's manual failover
  button does functionally the same thing via the API directly.
- `Dockerfile`, `docker-compose.yml` -- builds one image, runs it as two
  services (`wan-failover-monitor` and `wan-failover-dashboard`) sharing a
  named volume for the sqlite db and the same `.env` file. Monitor
  container grants only `CAP_NET_RAW` (needed for `ping`) rather than
  running as root.
- `.env.example` -- every tunable, documented inline. This is the
  authoritative reference for configuration -- if a setting isn't
  mentioned in this README, check there first.

## Operations: Docker commands

**First-time setup**, after `.env` is filled in (see Setup above):
```bash
docker compose up -d --build
```
`-d` runs detached (background). Both containers start; dashboard is at
`http://<host>:8090`.

**After editing `.env`** (any variable) -- env vars are only read once at
process start, so the running container has no way to know the file
changed. `docker compose restart` does NOT reload `.env` either -- it
restarts the existing container without re-reading it. You need to
recreate:
```bash
docker compose up -d --force-recreate wan-failover-monitor
# and/or, if the change affects the dashboard (e.g. OMADA_* vars, DASHBOARD_*):
docker compose up -d --force-recreate wan-failover-dashboard
```
No `--build` needed here since no code changed, just config.

**After editing any `.py` file** (`monitor.py`, `db.py`, `omada_client.py`,
`dashboard.py`) -- a real rebuild is required, `--force-recreate` alone
reuses the old image:
```bash
docker compose up -d --build --force-recreate
```
If you suspect Docker's build cache is serving stale layers (rare, but
possible after Docker Desktop updates or unusual host state), force a
completely clean rebuild:
```bash
docker compose down
docker compose build --no-cache
docker compose up -d
```

**Logs**:
```bash
docker compose logs -f wan-failover-monitor      # monitor only, follow
docker compose logs -f wan-failover-dashboard    # dashboard only, follow
docker compose logs -f                           # both, interleaved
docker compose logs --tail=100 wan-failover-monitor   # last 100 lines, no follow
docker compose logs -t wan-failover-monitor           # with timestamps
docker compose logs --since 30m wan-failover-monitor  # last 30 minutes
```
Ctrl+C stops following (containers keep running). Log rotation is capped
at 10MB x 3 files per container (see `docker-compose.yml`'s `logging`
block) -- old entries roll off; for anything you need to keep long-term,
rely on the dashboard's sqlite history instead of container logs.

**Status / is it actually running**:
```bash
docker compose ps
docker inspect wan-failover-monitor --format '{{.Created}}'   # when the container was created
docker exec wan-failover-monitor grep -n "SOME_STRING" monitor.py  # confirm what code is actually inside a running container
```
That last pattern -- grepping inside the live container -- is the reliable
way to confirm a rebuild actually picked up a code change, rather than
trusting container-creation timestamps (which update on `--force-recreate`
even if the underlying image is stale).

**Stop / remove**:
```bash
docker compose stop              # stop both, keep containers/volumes for later
docker compose down              # stop and remove containers (volumes/db persist)
docker compose down -v           # also destroy the named volume -- WIPES THE DATABASE, rarely what you want
docker rm -f wan-failover-monitor wan-failover-dashboard   # force-remove a specific stuck container by name
```

**Quick health check on the sqlite db directly** (useful when debugging
dashboard issues without going through the web UI):
```bash
docker exec wan-failover-dashboard python3 -c "
import db
print(len(db.fetch_cycles(0)), 'total cycles')
print(db.get_monitor_state())
"
```
