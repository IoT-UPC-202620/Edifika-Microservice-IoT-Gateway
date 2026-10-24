"""HTTP client for the cloud backend (Edge <-> Backend contract, see README)."""

import logging

import requests

from .transport import CloudRejected, CloudUnavailable

logger = logging.getLogger(__name__)


class CloudClient:
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

    def post_events(self, events: list[dict]) -> list[str]:
        """Deliver a batch. Returns the ids the backend accepted (idempotent by eventId)."""
        body = self._request(
            "POST",
            "/api/v1/edge/events",
            json={"gatewayId": self.config.gateway_id, "events": events},
        )
        accepted = body.get("accepted")
        if accepted is None:  # a bare 2xx accepts the whole batch
            return [e["eventId"] for e in events]
        return list(accepted)

    def fetch_sync(self, since_version: int | None) -> dict:
        params = {"since": since_version} if since_version is not None else {}
        return self._request("GET", "/api/v1/edge/sync", params=params)
