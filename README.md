# wan-failover-monitor

Docker container that watches internet health from the LAN and, on
sustained latency/packet-loss degradation, calls the Omada Open API to
force a WAN failover on your ER605 -- faster and smarter-triggered than the
router's default "is it completely down" failover.

## Read this before deploying

**A LAN-side container cannot independently probe WAN1 vs WAN2.** Whatever
this container pings goes out through whichever WAN the router currently has
active. So this design detects "the currently active path has gone bad" --
it does not verify the backup is actually better before switching to it, and
it can't watch a currently-inactive WAN's health in the background.

Two ways to close that gap, in order of how much I'd trust them:

1. **Use this as a smarter trigger for a decision the router already
   supports well.** Omada gateways' native Link Backup/SLA feature does
   real independent per-WAN echo tests (it pings out each WAN's own gateway
   before any routing decision is made) and already supports latency and
   packet-loss thresholds, not just up/down. Before building anything
   custom, check Gateway > your ER605 > Preferential/Link Backup settings
   for an SLA or "advanced" mode with configurable ping targets, interval,
   and loss/latency thresholds. If that's available in your firmware, tuning
   *that* directly is both more correct (true per-WAN visibility) and less
   work than this whole container. I'd genuinely start there.

2. **If you still want a custom decision-maker**, have it poll the
   controller's Open API for the gateway's own per-WAN status (the same
   telemetry the SLA feature already computes) instead of pinging from the
   LAN. `omada_client.get_gateway()` is stubbed for this -- once you hit it
   against your real controller you'll see whatever per-port latency/loss
   fields your firmware reports, and you can drive the decision logic in
   `monitor.py` off those instead of (or in addition to) the LAN-side pings.

This repo, as shipped, implements the LAN-side fallback (option not-1) with
hysteresis and a cooldown so it's a safe starting point, plus a periodic
"fail back if things have been good for a while" so it doesn't get stuck on
the backup link. Treat it as a bridge until you've evaluated option 1.

## Architecture

```
+----------------------+       ping x3 targets        +-------------------+
|  wan-failover-monitor | ----------------------------> |  1.1.1.1 / 8.8.8.8 |
|  (this container)     |                                |  9.9.9.9 (etc.)    |
|                        |                                +-------------------+
|  every 5s: aggregate   |
|  latency + loss ->     |        OAuth2 client-credentials + PATCH gateway
|  hysteresis state      | -----------------------------------------------> Omada
|  machine               |                                                  Controller
+------------------------+                                                   |
                                                                               v
                                                                          ER605 gateway
                                                                          (WAN priority
                                                                           flipped)
```

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
   `DRY_RUN=false`:
   ```
   docker compose up --build
   ```
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
- `CONSECUTIVE_GOOD_TO_FAILBACK` is deliberately 2x the trigger threshold.
  Failing back too eagerly onto a link that's degrading slowly (not fully
  down) causes flapping, which is worse for most applications (VoIP, RDP,
  SSH sessions) than staying on the backup a little longer than strictly
  necessary.
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

### From "bad cycles" to an actual failover: the streak counters

`CONSECUTIVE_BAD_TO_TRIGGER` (default 12) and `CONSECUTIVE_GOOD_TO_FAILBACK`
(default 24) count consecutive bad or good cycles. Once `consecutive_bad`
reaches the trigger threshold, `set_active_wan()` fires (subject to
`COOLDOWN_SECONDS`, see below). Once failed over, `consecutive_good`
reaching the fail-back threshold reverses it.

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

### Cooldown is separate from the streak counters

`COOLDOWN_SECONDS` (default 120) is a hard floor between any two actions,
independent of the streak logic above. Even if `consecutive_bad` or
`consecutive_good` legitimately hits its threshold, no action fires until
this many seconds have passed since the last one. This is what prevents a
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

4. Revert `PING_TARGETS` back to real targets and repeat step 3 to watch
   the fail-back path. Because `monitor_state` is persisted to the sqlite
   db (see below), the container correctly remembers `failed_over=True`
   across this restart -- you don't need to keep it running continuously
   to see both directions of the state machine.

5. Revert all test values (`PING_TARGETS`, `CONSECUTIVE_BAD_TO_TRIGGER`,
   `CHECK_INTERVAL_SECONDS`, `LOG_LEVEL`) back to your real tuned settings
   and recreate one final time before leaving it running for real.

### Why state survives a container restart

`monitor_state` in `db.py` persists `failed_over` and `last_action_time`
across restarts specifically so this works -- without it, a fresh process
always starts assuming it's on the primary WAN, which would be wrong (and
untestable this way) if the container restarts while genuinely failed over.

## Dashboard / ISP reporting

`monitor.py` now writes every check cycle and every failover/fail-back
event to a shared SQLite db (`/data/wan-monitor.db` inside the containers,
persisted via the `wan-monitor-data` named volume so it survives rebuilds).
A second container, `wan-failover-dashboard`, serves a read-only web UI over
that data at `http://<this-host>:8090`:

- a latency/loss timeseries chart over a selectable window (6h/24h/7d/30d/90d)
- a table of **degradation windows** -- contiguous runs of bad cycles
  collapsed into start/end/duration/avg+peak latency/avg+peak loss, which is
  the shape you actually want for an ISP dispute rather than a raw
  per-5-second dump
- a "Download ISP report (CSV)" button exporting exactly that table for the
  selected range

Both containers read/write the same SQLite file in WAL mode, which safely
supports this single-writer/single-reader pattern without needing a separate
database server.

Old rows are pruned automatically after `RETENTION_DAYS` (90 by default) --
raise it in `.env` if you want a longer history for a longer-running SLA
dispute, keeping in mind `CHECK_INTERVAL_SECONDS=5` means roughly 17k rows/day.

**Note on what this proves to an ISP**: these are round-trip times and loss
to public resolvers as seen from your LAN, not a certified line-quality
measurement -- useful as your own supporting evidence, but don't expect an
ISP to treat a self-hosted CSV as authoritative the way they would their own
NOC's monitoring. It's leverage for a conversation, not a legal instrument.

## Files

- `monitor.py` -- main loop: ping aggregation, hysteresis state machine,
  optional throughput sampling, calls into `omada_client`, persists to `db`.
- `omada_client.py` -- OAuth2 client-credentials auth + gateway/WAN config
  calls, all confirmed working against a real ER605 (see docstrings for
  which endpoint/verb each one hits).
- `get_site_id.sh` -- standalone script to look up your `OMADA_SITE_ID`.
- `get_wan_ports_config.sh` -- standalone script to call any Open API GET
  endpoint by path, used to find the real WAN `portId` values.
- `test_load_balance_swap.sh` -- standalone script to test/re-verify the
  actual failover write call (`show`/`failover`/`failback`), with typed
  confirmation before any live write.
- `db.py` -- shared SQLite persistence (cycles, events, degradation-window
  aggregation) used by both `monitor.py` and `dashboard.py`.
- `dashboard.py` -- Flask app: chart, degradation-window table, CSV export.
- `Dockerfile`, `docker-compose.yml` -- builds one image, runs it as two
  services (`wan-failover-monitor` and `wan-failover-dashboard`) sharing a
  named volume for the sqlite db. Monitor container grants only
  `CAP_NET_RAW` (needed for `ping`) rather than running as root.
- `.env.example` -- every tunable, documented inline.
