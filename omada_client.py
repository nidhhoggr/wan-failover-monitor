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
        PENDING CONFIRMATION against a real ER605 -- endpoint and schema are
        from the Knife4j docs (category: Gateway), not yet tested live.

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
