"""
omada_client.py

Thin wrapper around the Omada Controller Open API.

STATUS (2026-07-28): the auth flow, get_gateway, get_wan_ports_config, and
get_internet_load_balance / set_active_wan have all been confirmed working
end-to-end against a real ER605 on Omada Central -- including a live
failover test that actually moved traffic. See each method's docstring for
the specific endpoint and verb confirmed.
"""

import logging
import os
import time
from typing import Any, Optional

import requests

log = logging.getLogger("omada_client")


class OmadaAuthError(RuntimeError):
    pass


class OmadaClient:
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        omadac_id: str,
        site_id: str,
        verify_tls: bool = True,
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.omadac_id = omadac_id
        self.site_id = site_id
        self.verify_tls = verify_tls
        self.timeout = timeout

        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

        self._session = requests.Session()
        if not verify_tls:
            requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]

    # ---- auth ---------------------------------------------------------

    def _ensure_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 30:
            return self._access_token

        url = f"{self.base_url}/openapi/authorize/token"
        params = {"grant_type": "client_credentials"}
        body = {
            "omadacId": self.omadac_id,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        resp = self._session.post(
            url, params=params, json=body, timeout=self.timeout, verify=self.verify_tls
        )
        resp.raise_for_status()
        data = resp.json()

        # Omada Open API wraps responses as {"errorCode": 0, "msg": "...", "result": {...}}
        if data.get("errorCode", 0) != 0:
            raise OmadaAuthError(f"Auth failed: {data.get('msg')}")

        result = data.get("result", {})
        token = result.get("accessToken")
        expires_in = result.get("expiresIn", 3600)
        if not token:
            raise OmadaAuthError(f"No accessToken in response: {data}")

        self._access_token = token
        self._token_expires_at = time.time() + float(expires_in)
        log.info("Obtained Omada access token, expires in %ss", expires_in)
        return token

    def _headers(self) -> dict:
        return {
            "Authorization": f"AccessToken={self._ensure_token()}",
            "Content-Type": "application/json",
        }

    # ---- generic request helpers --------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        resp = self._session.get(
            url, headers=self._headers(), params=params, timeout=self.timeout, verify=self.verify_tls
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errorCode", 0) != 0:
            raise RuntimeError(f"GET {path} failed: {data.get('msg')}")
        return data.get("result")

    def _post(self, path: str, body: dict) -> Any:
        url = f"{self.base_url}{path}"
        resp = self._session.post(
            url, headers=self._headers(), json=body, timeout=self.timeout, verify=self.verify_tls
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errorCode", 0) != 0:
            raise RuntimeError(f"POST {path} failed: {data.get('msg')}")
        return data.get("result")

    def _put(self, path: str, body: dict) -> Any:
        url = f"{self.base_url}{path}"
        resp = self._session.put(
            url, headers=self._headers(), json=body, timeout=self.timeout, verify=self.verify_tls
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errorCode", 0) != 0:
            raise RuntimeError(f"PUT {path} failed: {data.get('msg')}")
        return data.get("result")

    def _patch(self, path: str, body: dict) -> Any:
        url = f"{self.base_url}{path}"
        resp = self._session.patch(
            url, headers=self._headers(), json=body, timeout=self.timeout, verify=self.verify_tls
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errorCode", 0) != 0:
            raise RuntimeError(f"PATCH {path} failed: {data.get('msg')}")
        return data.get("result")

    # ---- gateway-specific ------------------------------------------------

    def get_wan_ports_config(self) -> dict:
        """
        CONFIRMED WORKING (2026-07-28) against a real ER605 on Omada Central.
        Site-scoped -- no gateway MAC needed in the path.

            GET /openapi/v1/{omadacId}/sites/{siteId}/internet/ports-config

        Returns wanPortsConfig: a list of per-WAN-port connection settings
        (DHCP/PPPoE/static, MTU, VLAN, MAC). Each entry's `portId` is a
        string like "1_8ff0def98a03428b93d15678efa14052" -- NOT a bare
        integer. Use these exact portId strings for WAN_PRIMARY_PORT_ID /
        WAN_BACKUP_PORT_ID in .env.

        NOTE: this response has no priority/failover/Link-Backup fields --
        it's connection-type config only. It confirms port identity, not
        which one is primary. The write call for failover itself is a
        separate, still-unconfirmed endpoint -- see set_wan_link_backup().
        """
        path = f"/openapi/v1/{self.omadac_id}/sites/{self.site_id}/internet/ports-config"
        return self._get(path)

    def get_gateway(self, mac: str) -> dict:
        """
        CONFIRMED WORKING (2026-07-28) against a real ER605 on Omada Central.

            GET /openapi/v1/{omadacId}/sites/{siteId}/gateways/{mac}

        Returns device-level info: cpuUtil, memUtil, uptime, firmwareVersion,
        and portConfigs (physical port link status/VLAN -- NOT WAN-specific
        config, that's get_wan_ports_config() above). Useful for basic
        health/uptime checks but not for the failover decision itself.
        """
        path = f"/openapi/v1/{self.omadac_id}/sites/{self.site_id}/gateways/{mac}"
        return self._get(path)

    def get_wan_status(self, mac: str) -> list:
        """
        CONFIRMED WORKING (2026-07-29) against a real ER605 on Omada Central.
        Response matched the UI's Ports > WAN tab exactly (latency, loss,
        internetState all lined up).

            GET /openapi/v1/{omadacId}/sites/{siteId}/gateways/{gatewayMac}/wan-status

        Returns a list of per-port status entries, each with (among many
        other fields): port, name, type (0:WAN,1:WAN/LAN,2:LAN), status
        (0/1 physical link), internetState (0/1 WAN connectivity),
        onlineDetection, latency (ms), loss (%), healthLevel. This is the
        real-time status endpoint -- unlike get_wan_ports_config() (dial-up
        connection config) or get_internet_load_balance() (primary/backup
        assignment), neither of which carry live health data.
        """
        path = f"/openapi/v1/{self.omadac_id}/sites/{self.site_id}/gateways/{mac}/wan-status"
        return self._get(path)

    def get_internet_load_balance(self) -> dict:
        """
        CONFIRMED WORKING (2026-07-28) against a real ER605 on Omada Central.

            GET /openapi/v1/{omadacId}/sites/{siteId}/internet/load-balance

        This is the real Link Backup config -- primaryWans (list), backupWan,
        linkBackup (bool), backupMode, mode, weights. This is the endpoint
        that actually controls failover, as opposed to get_wan_ports_config()
        which is connection-type config only.
        """
        path = f"/openapi/v1/{self.omadac_id}/sites/{self.site_id}/internet/load-balance"
        return self._get(path)

    def set_active_wan(self, primary_port_id: str, backup_port_id: str) -> Any:
        """
        CONFIRMED WORKING (2026-07-28) against a real ER605 on Omada Central.
        Verified end-to-end: fires a real failover, traffic actually moves,
        and PUT is the correct verb (405 on other verbs was not needed).

            PUT /openapi/v1/{omadacId}/sites/{siteId}/internet/load-balance

        Triggers a real failover (or fail-back) by swapping which portId is
        primary vs backup in the Internet Load Balance config.

        Fetches the current config first and only mutates primaryWans /
        backupWan -- every other field (appOptRouting, backupMode, mode,
        weights) is echoed back exactly as the controller already had it,
        so we never guess at fields we don't fully understand.

        WARNING: this is a live write. Calling this with real primary/backup
        port IDs genuinely moves traffic to the specified primary WAN the
        moment it succeeds -- there is no dry-run at the API level (DRY_RUN
        in monitor.py works by not calling this method at all).
        """
        current = self.get_internet_load_balance()
        path = f"/openapi/v1/{self.omadac_id}/sites/{self.site_id}/internet/load-balance"
        body = dict(current)  # copy, don't mutate the fetched dict in place
        body["primaryWans"] = [primary_port_id]
        body["backupWan"] = backup_port_id
        return self._put(path, body)

    def get_alert_logs(self, resolved: bool = False, page: int = 1, page_size: int = 10,
                        time_start_ms: int = None, time_end_ms: int = None) -> dict:
        """
        CONFIRMED WORKING (2026-07-29): GET /openapi/v1/{omadacId}/sites/{siteId}/logs/alerts

        filters.timeStart/timeEnd are REQUIRED (epoch milliseconds), unlike
        most other endpoints in this file -- defaults to the last 90 days
        if not given. pageSize's documented allowed values are
        10/15/20/30/50/100 only -- request the minimum (10) and slice to
        however many you actually want in Python, rather than relying on
        an undocumented pageSize value working indefinitely.

        Returns the full result object: {totalRows, currentPage, currentSize,
        data: [{id, key, module, content, time, level}, ...],
        alertLogStat: {totalLogNum, unResolvedLogNum, resolvedLogNum, ...}}
        """
        if time_end_ms is None:
            time_end_ms = int(time.time() * 1000)
        if time_start_ms is None:
            time_start_ms = time_end_ms - (90 * 86400 * 1000)
        path = f"/openapi/v1/{self.omadac_id}/sites/{self.site_id}/logs/alerts"
        params = {
            "page": page,
            "pageSize": page_size,
            "filters.timeStart": time_start_ms,
            "filters.timeEnd": time_end_ms,
            "filters.resolved": "true" if resolved else "false",
        }
        return self._get(path, params=params)

    def resolve_alert_logs(self, log_ids: list, start_time_ms: int = None, end_time_ms: int = None) -> Any:
        """
        CONFIRMED WORKING (2026-07-29): POST /openapi/v1/{omadacId}/sites/{siteId}/logs/alerts/resolve

        Resolves specific alert logs by ID (selectType='include' -- as
        opposed to 'exclude' or 'all', which this doesn't use).

        startTime/endTime are required by the API even when selecting
        specific IDs, AND the API enforces a hard 31-day max duration
        between them (confirmed by a real rejection: "Log query time
        duration filter should not longer than 31 days"). Defaults to a
        flat 30-day-from-now window (under the limit) -- note this will
        miss alerts older than 30 days; pass start_time_ms/end_time_ms
        explicitly if you need to resolve something older than that.
        """
        if end_time_ms is None:
            end_time_ms = int(time.time() * 1000)
        if start_time_ms is None:
            start_time_ms = end_time_ms - (30 * 86400 * 1000)

        path = f"/openapi/v1/{self.omadac_id}/sites/{self.site_id}/logs/alerts/resolve"
        body = {
            "logs": log_ids,
            "selectType": "include",
            "startTime": start_time_ms,
            "endTime": end_time_ms,
        }
        return self._post(path, body)

    def get_isp_load(self, start_ts: int, end_ts: int) -> list:
        """
        PENDING LIVE CONFIRMATION: GET /openapi/v1/{omadacId}/sites/{siteId}/dashboard/isp-load

        NOTE: start/end here are SECONDS, unlike get_alert_logs()'s
        start_time_ms/end_time_ms which are MILLISECONDS -- this API is
        inconsistent about units between endpoints, easy to silently mix up.

        Returns a list, one entry per WAN port:
          [{portId, portName, data: [{totalRate, latency, time}, ...]}, ...]
        totalRate is "rxR+txR" per the docs -- unit not explicitly stated
        for this endpoint, but other rate fields in this API (wan-status's
        rxRate/txRate) are documented as KB/s, so treating it as KB/s until
        a live response confirms or contradicts that. Verify against a real
        response before trusting the unit label on any chart built from this.
        """
        path = f"/openapi/v1/{self.omadac_id}/sites/{self.site_id}/dashboard/isp-load"
        params = {"start": start_ts, "end": end_ts}
        return self._get(path, params=params)

    def start_speed_test(self, gateway_mac: str, port_uuids: list) -> Any:
        """
        PENDING LIVE CONFIRMATION: POST /openapi/v1/{omadacId}/sites/{siteId}/gateways/{gatewayMac}/speedTest

        port_uuids: list of WAN port identifier strings -- the SAME format
        as WAN_PRIMARY_PORT_ID/WAN_BACKUP_PORT_ID (e.g.
        "1_8ff0def98a03428b93d15678efa14052"), confirmed from
        get_wan_ports_config()'s response. NOT MAC addresses, despite the
        gateway itself being identified by MAC as a separate path param --
        easy to conflate the two, worth being careful about.

        This starts an ASYNC test -- the response just confirms which
        device is running it (deviceMac). Poll get_speed_test_result()
        afterward until the relevant port's progress reaches 100.
        """
        path = f"/openapi/v1/{self.omadac_id}/sites/{self.site_id}/gateways/{gateway_mac}/speedTest"
        body = {"portUuidList": port_uuids}
        return self._post(path, body)

    def get_speed_test_result(self, gateway_mac: str) -> dict:
        """
        PENDING LIVE CONFIRMATION: GET /openapi/v1/{omadacId}/sites/{siteId}/gateways/{gatewayMac}/speedTestResult

        Poll this after start_speed_test(). Returns {status, portSpeedResults:
        [{portId (bare int, NOT the "1_hash" uuid string -- matches by the
        leading integer), time, portName, isp, serverName, serverLocation,
        status, latency, down, up, progress}, ...]}. down/up units not
        stated in the docs -- unconfirmed until checked against a real
        response, don't trust a unit label on this without verifying.
        """
        path = f"/openapi/v1/{self.omadac_id}/sites/{self.site_id}/gateways/{gateway_mac}/speedTestResult"
        return self._get(path)

    @classmethod
    def from_env(cls):
        """
        Convenience constructor reading the standard OMADA_* env vars
        directly -- used by dashboard.py, which (unlike monitor.py) has no
        existing env-parsing/config module of its own.
        Raises RuntimeError with a clear message if anything's missing.
        """
        base_url = os.environ.get("OMADA_BASE_URL", "")
        client_id = os.environ.get("OMADA_CLIENT_ID", "")
        client_secret = os.environ.get("OMADA_CLIENT_SECRET", "")
        omadac_id = os.environ.get("OMADA_OMADAC_ID", "")
        site_id = os.environ.get("OMADA_SITE_ID", "")
        verify_tls = os.environ.get("OMADA_VERIFY_TLS", "false").strip().lower() in ("1", "true", "yes", "on")
        missing = [
            name for name, val in [
                ("OMADA_BASE_URL", base_url), ("OMADA_CLIENT_ID", client_id),
                ("OMADA_CLIENT_SECRET", client_secret), ("OMADA_OMADAC_ID", omadac_id),
                ("OMADA_SITE_ID", site_id),
            ] if not val
        ]
        if missing:
            raise RuntimeError(f"Missing required Omada config: {', '.join(missing)}")
        return cls(
            base_url=base_url, client_id=client_id, client_secret=client_secret,
            omadac_id=omadac_id, site_id=site_id, verify_tls=verify_tls,
        )
