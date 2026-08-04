"""
monitor.py

Watches aggregate internet health from the LAN side (see README for why this
is "aggregate health of whichever WAN is currently active" rather than
independent per-WAN visibility) and triggers a failover via the Omada Open
API when the active path is consistently bad, with hysteresis in both
directions so a single flaky ping cycle doesn't flip anything.

State machine, per check cycle:
  - run PING_COUNT_PER_CYCLE pings against each of PING_TARGETS, in parallel
  - aggregate: avg latency across all successful replies, loss% across all
    attempts
  - cycle is BAD if avg_latency > LATENCY_THRESHOLD_MS OR loss% > PACKET_LOSS_THRESHOLD_PCT
  - track consecutive BAD cycles; at CONSECUTIVE_BAD_TO_TRIGGER, fail over
    (subject to COOLDOWN_SECONDS since the last action)
  - once failed over, track consecutive GOOD cycles; at
    CONSECUTIVE_GOOD_TO_FAILBACK, fail back to primary
"""

import logging
import re
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from omada_client import OmadaClient, OmadaAuthError
import db
import settings_store
from config import get_config

# ---- config -------------------------------------------------------------
# Every value below is resolved through config.get_config(), which checks
# settings.db (saved via the dashboard's Configuration tab) first, then
# .env, then the hardcoded default in config.py's SETTINGS_REGISTRY --
# that registry is now the single source of truth for defaults, replacing
# the os.environ.get(..., "default") calls that used to live here directly.
# See config.py's module docstring for why a saved Configuration change
# still needs a container restart to actually take effect.

settings_store.init_settings_db()

PING_TARGETS = [t.strip() for t in get_config("PING_TARGETS").split(",") if t.strip()]
LATENCY_THRESHOLD_MS = get_config("LATENCY_THRESHOLD_MS")
PACKET_LOSS_THRESHOLD_PCT = get_config("PACKET_LOSS_THRESHOLD_PCT")
CONSECUTIVE_BAD_TO_TRIGGER = get_config("CONSECUTIVE_BAD_TO_TRIGGER")
CONSECUTIVE_GOOD_TO_FAILBACK = get_config("CONSECUTIVE_GOOD_TO_FAILBACK")
# IMPORTANT: once failed over, subsequent good cycles reflect the health of
# whichever WAN is now ACTIVE (the backup) -- not whether the original
# primary has recovered. There is no way for this LAN-side monitor to tell
# those apart. Left at the default (false), CONSECUTIVE_GOOD_TO_FAILBACK
# reaching its threshold only logs and stops counting -- it does NOT call
# the Omada API. Fail-back should be a deliberate decision (confirmed with
# your ISP, or after a known outage window) made via
# `test_load_balance_swap.sh failback`, not an automatic action based on
# unrelated telemetry. Setting this true restores fully automatic
# fail-back and accepts the real risk of flapping onto a still-broken
# primary the moment the backup happens to look healthy for a while.
AUTO_FAILBACK_ENABLED = get_config("AUTO_FAILBACK_ENABLED")

# How often to poll the primary WAN's real status via the Omada API (a
# separate, lower-frequency cadence than CHECK_INTERVAL_SECONDS -- this is
# an actual API call, not a local ping, so it shouldn't run every cycle).
PRIMARY_HEALTH_POLL_INTERVAL_SECONDS = get_config("PRIMARY_HEALTH_POLL_INTERVAL_SECONDS")

# Once the primary is reported healthy via the API, require it to stay
# continuously healthy for this long before fail-back is even considered.
# Any single unhealthy poll during this window resets the clock to zero.
# Default 300s (5 min) -- deliberately conservative.
PRIMARY_HEALTHY_STABILITY_SECONDS = get_config("PRIMARY_HEALTHY_STABILITY_SECONDS")

# "Healthy" for fail-back purposes requires internetState==1 (the router's
# own connectivity flag) AND latency/loss from that same wan-status
# response falling within these thresholds. Separate from
# LATENCY_THRESHOLD_MS/PACKET_LOSS_THRESHOLD_PCT above (those gate the LAN
# ping-based failover trigger) -- kept independent since the API-reported
# values come from the router's own probe, not this monitor's LAN-side
# pings, and you may reasonably want a different tolerance for "good enough
# to trust as primary again" than for "bad enough to fail off of."
PRIMARY_HEALTHY_LATENCY_THRESHOLD_MS = get_config("PRIMARY_HEALTHY_LATENCY_THRESHOLD_MS")
PRIMARY_HEALTHY_LOSS_THRESHOLD_PCT = get_config("PRIMARY_HEALTHY_LOSS_THRESHOLD_PCT")
# A single cycle going the "wrong" direction (e.g. one bad cycle during an
# otherwise-clean good streak) doesn't immediately zero the streak -- it
# takes this many CONSECUTIVE opposite-direction cycles to actually break
# it. 1 = old strict behavior (any single opposite cycle resets). 2 is a
# reasonable default: absorbs isolated single-cycle noise (one dropped ping
# to one of several targets) without absorbing anything that looks like a
# real, sustained change in conditions.
STREAK_TOLERANCE_CYCLES = get_config("STREAK_TOLERANCE_CYCLES")
COOLDOWN_SECONDS = get_config("COOLDOWN_SECONDS")
CHECK_INTERVAL_SECONDS = get_config("CHECK_INTERVAL_SECONDS")
PING_COUNT_PER_CYCLE = get_config("PING_COUNT_PER_CYCLE")
PING_TIMEOUT_SECONDS = get_config("PING_TIMEOUT_SECONDS")

ENABLE_THROUGHPUT_CHECK = get_config("ENABLE_THROUGHPUT_CHECK")
THROUGHPUT_CHECK_EVERY_N_CYCLES = get_config("THROUGHPUT_CHECK_EVERY_N_CYCLES")
THROUGHPUT_TEST_URL = get_config("THROUGHPUT_TEST_URL")
THROUGHPUT_MIN_MBPS = get_config("THROUGHPUT_MIN_MBPS")

DRY_RUN = get_config("DRY_RUN")

OMADA_BASE_URL = get_config("OMADA_BASE_URL")
OMADA_CLIENT_ID = get_config("OMADA_CLIENT_ID")
OMADA_CLIENT_SECRET = get_config("OMADA_CLIENT_SECRET")
OMADA_OMADAC_ID = get_config("OMADA_OMADAC_ID")
OMADA_SITE_ID = get_config("OMADA_SITE_ID")
OMADA_GATEWAY_MAC = get_config("OMADA_GATEWAY_MAC")
OMADA_VERIFY_TLS = get_config("OMADA_VERIFY_TLS")

WAN_PRIMARY_PORT_ID = get_config("WAN_PRIMARY_PORT_ID")
WAN_BACKUP_PORT_ID = get_config("WAN_BACKUP_PORT_ID")

LOG_LEVEL = get_config("LOG_LEVEL")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("monitor")

_PING_TIME_RE = re.compile(r"time[=<]([\d.]+)\s*ms")


@dataclass
class CycleResult:
    avg_latency_ms: float  # None-ish handled as inf if no replies at all
    loss_pct: float
    is_bad: bool


def ping_target(target: str) -> tuple:
    """Run ping against a single target, return (successes, attempts, latencies_ms)."""
    try:
        proc = subprocess.run(
            [
                "ping",
                "-c", str(PING_COUNT_PER_CYCLE),
                "-W", str(int(PING_TIMEOUT_SECONDS)),
                target,
            ],
            capture_output=True,
            text=True,
            timeout=PING_TIMEOUT_SECONDS * PING_COUNT_PER_CYCLE + 5,
        )
        latencies = [float(m) for m in _PING_TIME_RE.findall(proc.stdout)]
        attempts = PING_COUNT_PER_CYCLE
        successes = len(latencies)
        return successes, attempts, latencies
    except Exception as e:
        log.warning("ping to %s failed to run: %s", target, e)
        return 0, PING_COUNT_PER_CYCLE, []


def run_cycle() -> CycleResult:
    all_latencies = []
    total_attempts = 0
    total_successes = 0

    with ThreadPoolExecutor(max_workers=len(PING_TARGETS)) as pool:
        futures = {pool.submit(ping_target, t): t for t in PING_TARGETS}
        for fut in as_completed(futures):
            successes, attempts, latencies = fut.result()
            total_successes += successes
            total_attempts += attempts
            all_latencies.extend(latencies)

    loss_pct = 100.0 * (1 - (total_successes / total_attempts)) if total_attempts else 100.0
    avg_latency = statistics.mean(all_latencies) if all_latencies else float("inf")

    is_bad = (avg_latency > LATENCY_THRESHOLD_MS) or (loss_pct > PACKET_LOSS_THRESHOLD_PCT)

    log.debug(
        "cycle: avg_latency=%.1fms loss=%.1f%% bad=%s",
        avg_latency, loss_pct, is_bad,
    )
    return CycleResult(avg_latency_ms=avg_latency, loss_pct=loss_pct, is_bad=is_bad)


def run_throughput_check() -> bool:
    """Returns True if throughput is acceptable (or check disabled/errored -- fail open)."""
    try:
        start = time.time()
        proc = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{size_download} %{time_total}", THROUGHPUT_TEST_URL],
            capture_output=True, text=True, timeout=30,
        )
        parts = proc.stdout.strip().split()
        if len(parts) != 2:
            log.warning("throughput check: unexpected curl output %r", proc.stdout)
            return True  # fail open -- don't trigger failover off a broken probe
        size_bytes, elapsed_s = float(parts[0]), float(parts[1])
        if elapsed_s <= 0 or size_bytes <= 0:
            return True
        mbps = (size_bytes * 8 / 1_000_000) / elapsed_s
        log.info("throughput check: %.2f Mbps (min acceptable %.2f Mbps)", mbps, THROUGHPUT_MIN_MBPS)
        return mbps >= THROUGHPUT_MIN_MBPS
    except Exception as e:
        log.warning("throughput check failed to run: %s", e)
        return True  # fail open


def update_streaks(is_bad: bool, consecutive_bad: int, consecutive_good: int, opposite_streak: int):
    """
    Debounced streak tracking: a streak (good or bad) only gets reset once
    STREAK_TOLERANCE_CYCLES consecutive opposite-direction cycles have
    occurred, not on the very first one. This absorbs isolated single-cycle
    noise (e.g. one dropped ping to one of several targets during an
    otherwise clean run) without weakening real degradation detection --
    STREAK_TOLERANCE_CYCLES consecutive bad cycles still breaks a good
    streak exactly as fast as before, just not a single one.
    """
    if is_bad:
        if consecutive_good > 0:
            opposite_streak += 1
            if opposite_streak >= STREAK_TOLERANCE_CYCLES:
                # enough consecutive bad cycles to treat this as real, not noise
                consecutive_bad = opposite_streak
                consecutive_good = 0
                opposite_streak = 0
            # else: still within tolerance -- leave both streaks as-is,
            # this cycle is absorbed rather than counted either direction
        else:
            consecutive_bad += 1
            opposite_streak = 0
    else:
        if consecutive_bad > 0:
            opposite_streak += 1
            if opposite_streak >= STREAK_TOLERANCE_CYCLES:
                consecutive_good = opposite_streak
                consecutive_bad = 0
                opposite_streak = 0
        else:
            consecutive_good += 1
            opposite_streak = 0
    return consecutive_bad, consecutive_good, opposite_streak


def check_primary_wan_health(omada: OmadaClient) -> bool:
    """
    CONFIRMED WORKING (2026-07-29): get_wan_status() tested against a real
    ER605, response matched the UI exactly. This function's matching logic
    has been unit-tested against the real confirmed response shape.

    Queries the primary WAN's real status via the Omada API -- this is the
    correct signal for fail-back decisions, unlike LAN-side ping health
    (which only ever reflects whatever WAN is currently active).

    "Healthy" requires ALL of:
      - internetState == 1 (the router's own connectivity flag)
      - latency <= PRIMARY_HEALTHY_LATENCY_THRESHOLD_MS
      - loss <= PRIMARY_HEALTHY_LOSS_THRESHOLD_PCT
    all three from the same wan-status response. internetState alone isn't
    enough -- a WAN can report "connected" while still being the same kind
    of degraded (high latency, lossy) that caused the original failover, so
    fail-back needs to confirm real quality, not just link presence.
    Missing/null latency or loss (e.g. while the port is actually down) is
    treated as failing the check, not passing it by default.
    """
    port_num = int(WAN_PRIMARY_PORT_ID.split("_")[0])
    wan_status = omada.get_wan_status(OMADA_GATEWAY_MAC)
    for entry in wan_status:
        if entry.get("port") == port_num:
            internet_state = entry.get("internetState")
            latency = entry.get("latency")
            loss = entry.get("loss")
            healthy = (
                internet_state == 1
                and latency is not None and latency <= PRIMARY_HEALTHY_LATENCY_THRESHOLD_MS
                and loss is not None and loss <= PRIMARY_HEALTHY_LOSS_THRESHOLD_PCT
            )
            log.debug(
                "primary WAN (port %s) status: internetState=%s latency=%sms (<=%s) loss=%s%% (<=%s) -> healthy=%s",
                port_num, internet_state, latency, PRIMARY_HEALTHY_LATENCY_THRESHOLD_MS,
                loss, PRIMARY_HEALTHY_LOSS_THRESHOLD_PCT, healthy,
            )
            return healthy
    log.warning("primary WAN (port %s) not found in wan-status response -- treating as unhealthy", port_num)
    return False


def build_omada_client(required: bool = True):
    missing = [
        name for name, val in [
            ("OMADA_BASE_URL", OMADA_BASE_URL),
            ("OMADA_CLIENT_ID", OMADA_CLIENT_ID),
            ("OMADA_CLIENT_SECRET", OMADA_CLIENT_SECRET),
            ("OMADA_OMADAC_ID", OMADA_OMADAC_ID),
            ("OMADA_SITE_ID", OMADA_SITE_ID),
            ("OMADA_GATEWAY_MAC", OMADA_GATEWAY_MAC),
        ] if not val
    ]
    if missing:
        msg = f"Missing required Omada config: {', '.join(missing)}. Fill these in .env (see .env.example)."
        if required:
            raise SystemExit(msg)
        log.warning("%s Primary-health polling disabled until these are set.", msg)
        return None
    return OmadaClient(
        base_url=OMADA_BASE_URL,
        client_id=OMADA_CLIENT_ID,
        client_secret=OMADA_CLIENT_SECRET,
        omadac_id=OMADA_OMADAC_ID,
        site_id=OMADA_SITE_ID,
        verify_tls=OMADA_VERIFY_TLS,
    )


def main():
    log.info(
        "starting: targets=%s latency_thresh=%sms loss_thresh=%s%% trigger_after=%s cycles "
        "cooldown=%ss dry_run=%s auto_failback=%s health_poll_interval=%ss stability_window=%ss",
        PING_TARGETS, LATENCY_THRESHOLD_MS, PACKET_LOSS_THRESHOLD_PCT,
        CONSECUTIVE_BAD_TO_TRIGGER, COOLDOWN_SECONDS, DRY_RUN, AUTO_FAILBACK_ENABLED,
        PRIMARY_HEALTH_POLL_INTERVAL_SECONDS, PRIMARY_HEALTHY_STABILITY_SECONDS,
    )

    omada = None
    if not DRY_RUN:
        omada = build_omada_client(required=True)
    else:
        log.warning("DRY_RUN=true -- will log decisions but will NOT call the Omada API")
        # Still build a client for read-only primary-health polling, so
        # DRY_RUN testing can observe the health signal too. Not required --
        # falls back to no polling if credentials aren't filled in yet.
        omada = build_omada_client(required=False)

    db.init_db()
    log.info("metrics database ready at %s (retention %s days)", db.DB_PATH, db.RETENTION_DAYS)

    saved_state = db.get_monitor_state()
    failed_over = saved_state["failed_over"]
    last_action_time = saved_state["last_action_time"]
    primary_healthy_since = saved_state.get("primary_healthy_since")
    if failed_over:
        log.warning(
            "Resuming with failed_over=True from persisted state (last action at %s) -- "
            "this process believes the backup WAN is currently active from a prior run.",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_action_time)) if last_action_time else "unknown",
        )

    consecutive_bad = 0
    consecutive_good = 0
    opposite_streak = 0
    cycle_count = 0
    last_prune_time = 0.0
    last_health_poll_time = 0.0
    process_start_time = time.time()

    while True:
        # Dashboard's "Apply & Restart" button writes a timestamp flag into
        # the shared settings db; exiting cleanly here lets Docker's
        # `restart: unless-stopped` policy bring this container back with
        # fresh config -- the mechanism that makes configuration changes
        # apply without the user ever touching docker commands. Checked
        # once per cycle, so worst-case signal latency is one
        # CHECK_INTERVAL_SECONDS. monitor_state (failed_over etc.) is
        # already persisted at every change, so a clean exit loses nothing.
        if settings_store.restart_requested_since(process_start_time):
            log.info("restart requested via dashboard Configuration page -- "
                     "exiting so Docker restarts this container with fresh settings")
            sys.exit(0)

        cycle_count += 1
        result = run_cycle()

        db.insert_cycle(
            ts=time.time(),
            avg_latency_ms=result.avg_latency_ms,
            loss_pct=result.loss_pct,
            is_bad=result.is_bad,
        )

        cycle_is_bad = result.is_bad

        throughput_ok = True
        if ENABLE_THROUGHPUT_CHECK and cycle_count % THROUGHPUT_CHECK_EVERY_N_CYCLES == 0:
            throughput_ok = run_throughput_check()
            if not throughput_ok:
                cycle_is_bad = True

        consecutive_bad, consecutive_good, opposite_streak = update_streaks(
            cycle_is_bad, consecutive_bad, consecutive_good, opposite_streak
        )

        now = time.time()
        cooling_down = (now - last_action_time) < COOLDOWN_SECONDS

        if not failed_over and consecutive_bad >= CONSECUTIVE_BAD_TO_TRIGGER and not cooling_down:
            log.warning(
                "THRESHOLD BREACHED: %s consecutive bad cycles (latency=%.1fms loss=%.1f%%) "
                "-- triggering failover to backup WAN",
                consecutive_bad, result.avg_latency_ms, result.loss_pct,
            )
            if DRY_RUN:
                log.warning("[DRY_RUN] would call set_active_wan(primary=%s, backup=%s)", WAN_BACKUP_PORT_ID, WAN_PRIMARY_PORT_ID)
            else:
                try:
                    omada.set_active_wan(WAN_BACKUP_PORT_ID, WAN_PRIMARY_PORT_ID)
                    log.info("failover command sent successfully")
                except OmadaAuthError as e:
                    log.error("Omada auth failed, cannot fail over: %s", e)
                except Exception as e:
                    log.error("failover command failed: %s", e)
            db.insert_event(
                ts=now, action="failover_to_backup", dry_run=DRY_RUN,
                trigger_latency_ms=result.avg_latency_ms, trigger_loss_pct=result.loss_pct,
                consecutive_cycles=consecutive_bad,
            )
            failed_over = True
            last_action_time = now
            db.set_monitor_state(failed_over=True, last_action_time=last_action_time)
            consecutive_bad = 0
            consecutive_good = 0
            opposite_streak = 0

        elif failed_over and omada is not None and (now - last_health_poll_time) >= PRIMARY_HEALTH_POLL_INTERVAL_SECONDS:
            last_health_poll_time = now
            try:
                primary_ok = check_primary_wan_health(omada)
            except Exception as e:
                log.warning("primary health check failed: %s", e)
                primary_ok = False

            if primary_ok:
                if primary_healthy_since is None:
                    primary_healthy_since = now
                    log.info(
                        "primary WAN reported healthy -- starting %ss stability window before fail-back is considered",
                        PRIMARY_HEALTHY_STABILITY_SECONDS,
                    )
                healthy_duration = now - primary_healthy_since
                db.set_monitor_state(failed_over=True, last_action_time=last_action_time, primary_healthy_since=primary_healthy_since)

                if healthy_duration >= PRIMARY_HEALTHY_STABILITY_SECONDS and not cooling_down:
                    if not AUTO_FAILBACK_ENABLED:
                        log.info(
                            "Primary WAN has been continuously healthy for %.0fs (>= %ss stability window) -- "
                            "but AUTO_FAILBACK_ENABLED=false, so no automatic fail-back. Confirmed healthy, "
                            "run when ready: ./test_load_balance_swap.sh failback",
                            healthy_duration, PRIMARY_HEALTHY_STABILITY_SECONDS,
                        )
                        db.insert_event(
                            ts=now, action="failback_skipped_manual_required", dry_run=True,
                            trigger_latency_ms=None, trigger_loss_pct=None,
                            consecutive_cycles=None,
                        )
                    else:
                        log.info(
                            "Primary WAN confirmed healthy for %.0fs -- AUTO_FAILBACK_ENABLED=true, failing back.",
                            healthy_duration,
                        )
                        if DRY_RUN:
                            log.warning("[DRY_RUN] would call set_active_wan(primary=%s, backup=%s)", WAN_PRIMARY_PORT_ID, WAN_BACKUP_PORT_ID)
                        else:
                            try:
                                omada.set_active_wan(WAN_PRIMARY_PORT_ID, WAN_BACKUP_PORT_ID)
                                log.info("fail-back command sent successfully")
                            except OmadaAuthError as e:
                                log.error("Omada auth failed, cannot fail back: %s", e)
                            except Exception as e:
                                log.error("fail-back command failed: %s", e)
                        db.insert_event(
                            ts=now, action="failback_to_primary", dry_run=DRY_RUN,
                            trigger_latency_ms=None, trigger_loss_pct=None,
                            consecutive_cycles=None,
                        )
                        failed_over = False
                        last_action_time = now
                        primary_healthy_since = None
                        db.set_monitor_state(failed_over=False, last_action_time=last_action_time, primary_healthy_since=None)
                        consecutive_bad = 0
                        consecutive_good = 0
                        opposite_streak = 0
            else:
                if primary_healthy_since is not None:
                    log.info("primary WAN reported unhealthy again -- resetting stability window")
                primary_healthy_since = None
                db.set_monitor_state(failed_over=True, last_action_time=last_action_time, primary_healthy_since=None)

        if now - last_prune_time > 3600:
            db.prune_old_rows()
            last_prune_time = now

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
