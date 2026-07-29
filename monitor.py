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
import os
import re
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from omada_client import OmadaClient, OmadaAuthError
import db

# ---- config -----------------------------------------------------------

def env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def env_list(name: str, default: str) -> list:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


PING_TARGETS = env_list("PING_TARGETS", "1.1.1.1,8.8.8.8,9.9.9.9")
LATENCY_THRESHOLD_MS = float(os.environ.get("LATENCY_THRESHOLD_MS", "150"))
PACKET_LOSS_THRESHOLD_PCT = float(os.environ.get("PACKET_LOSS_THRESHOLD_PCT", "15"))
CONSECUTIVE_BAD_TO_TRIGGER = int(os.environ.get("CONSECUTIVE_BAD_TO_TRIGGER", "12"))
CONSECUTIVE_GOOD_TO_FAILBACK = int(os.environ.get("CONSECUTIVE_GOOD_TO_FAILBACK", "24"))
# A single cycle going the "wrong" direction (e.g. one bad cycle during an
# otherwise-clean good streak) doesn't immediately zero the streak -- it
# takes this many CONSECUTIVE opposite-direction cycles to actually break
# it. 1 = old strict behavior (any single opposite cycle resets). 2 is a
# reasonable default: absorbs isolated single-cycle noise (one dropped ping
# to one of several targets) without absorbing anything that looks like a
# real, sustained change in conditions.
STREAK_TOLERANCE_CYCLES = int(os.environ.get("STREAK_TOLERANCE_CYCLES", "2"))
COOLDOWN_SECONDS = float(os.environ.get("COOLDOWN_SECONDS", "120"))
CHECK_INTERVAL_SECONDS = float(os.environ.get("CHECK_INTERVAL_SECONDS", "5"))
PING_COUNT_PER_CYCLE = int(os.environ.get("PING_COUNT_PER_CYCLE", "3"))
PING_TIMEOUT_SECONDS = float(os.environ.get("PING_TIMEOUT_SECONDS", "2"))

ENABLE_THROUGHPUT_CHECK = env_bool("ENABLE_THROUGHPUT_CHECK", False)
THROUGHPUT_CHECK_EVERY_N_CYCLES = int(os.environ.get("THROUGHPUT_CHECK_EVERY_N_CYCLES", "60"))
THROUGHPUT_TEST_URL = os.environ.get("THROUGHPUT_TEST_URL", "https://speed.cloudflare.com/__down?bytes=2000000")
THROUGHPUT_MIN_MBPS = float(os.environ.get("THROUGHPUT_MIN_MBPS", "5"))

DRY_RUN = env_bool("DRY_RUN", True)

OMADA_BASE_URL = os.environ.get("OMADA_BASE_URL", "")
OMADA_CLIENT_ID = os.environ.get("OMADA_CLIENT_ID", "")
OMADA_CLIENT_SECRET = os.environ.get("OMADA_CLIENT_SECRET", "")
OMADA_OMADAC_ID = os.environ.get("OMADA_OMADAC_ID", "")
OMADA_SITE_ID = os.environ.get("OMADA_SITE_ID", "")
OMADA_GATEWAY_MAC = os.environ.get("OMADA_GATEWAY_MAC", "")
OMADA_VERIFY_TLS = env_bool("OMADA_VERIFY_TLS", False)

WAN_PRIMARY_PORT_ID = os.environ.get("WAN_PRIMARY_PORT_ID", "")
WAN_BACKUP_PORT_ID = os.environ.get("WAN_BACKUP_PORT_ID", "")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

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


def build_omada_client() -> OmadaClient:
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
        raise SystemExit(
            f"Missing required Omada config: {', '.join(missing)}. "
            f"Fill these in .env (see .env.example) before running with DRY_RUN=false."
        )
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
        "starting: targets=%s latency_thresh=%sms loss_thresh=%s%% "
        "trigger_after=%s cycles fail_back_after=%s cycles cooldown=%ss dry_run=%s",
        PING_TARGETS, LATENCY_THRESHOLD_MS, PACKET_LOSS_THRESHOLD_PCT,
        CONSECUTIVE_BAD_TO_TRIGGER, CONSECUTIVE_GOOD_TO_FAILBACK, COOLDOWN_SECONDS, DRY_RUN,
    )

    omada = None
    if not DRY_RUN:
        omada = build_omada_client()
    else:
        log.warning("DRY_RUN=true -- will log decisions but will NOT call the Omada API")

    db.init_db()
    log.info("metrics database ready at %s (retention %s days)", db.DB_PATH, db.RETENTION_DAYS)

    saved_state = db.get_monitor_state()
    failed_over = saved_state["failed_over"]
    last_action_time = saved_state["last_action_time"]
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

    while True:
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

        elif failed_over and consecutive_good >= CONSECUTIVE_GOOD_TO_FAILBACK and not cooling_down:
            log.info(
                "%s consecutive good cycles on original primary -- failing back",
                consecutive_good,
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
                trigger_latency_ms=result.avg_latency_ms, trigger_loss_pct=result.loss_pct,
                consecutive_cycles=consecutive_good,
            )
            failed_over = False
            last_action_time = now
            db.set_monitor_state(failed_over=False, last_action_time=last_action_time)
            consecutive_bad = 0
            consecutive_good = 0
            opposite_streak = 0

        if now - last_prune_time > 3600:
            db.prune_old_rows()
            last_prune_time = now

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
