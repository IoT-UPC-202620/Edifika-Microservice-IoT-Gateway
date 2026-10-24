"""HTTP client for the IoT Access Management sync endpoint."""

import logging
import requests

from .transport import CloudRejected, CloudUnavailable

logger = logging.getLogger(__name__)


class SyncClient:
    def __init__(self, config, session=None):
        self.config = config
        self.base_url = config.sync_base_url.rstrip("/")
        self.session = session or requests.Session()

    def _headers(self) -> dict:
        headers = {"X-Gateway-Id": self.config.gateway_id}
        if self.config.sync_api_key:
            headers["Authorization"] = f"Bearer {self.config.sync_api_key}"
        return headers

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(),
                timeout=self.config.cloud_timeout_s,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise CloudUnavailable(str(exc)) from exc
        if response.status_code >= 500 or response.status_code in (401, 403, 408, 429):
            raise CloudUnavailable(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise CloudRejected(f"HTTP {response.status_code}: {response.text[:200]}")
        try:
            return response.json() if response.content else {}
        except ValueError as exc:
            raise CloudUnavailable("Backend returned invalid JSON") from exc

    def fetch_sync(self, since: int | None = None) -> dict:
        kwargs = {}
        if since is not None:
            kwargs["params"] = {"since": since}
        return self._request("GET", "/api/v1/edge/sync", **kwargs)


# Backwards compatibility alias
CloudClient = SyncClient
