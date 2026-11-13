"""Client for Valio Aimo's mock Price and Availability APIs.

Rules we follow (from Valio's docs): query only products already narrowed down from the
static data, batch them (max 1,000 per call), and never poll the whole catalog. Every call
is counted so the UI can show exactly how many products were checked live.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from . import config

MAX_PER_CALL = 1000
CACHE_SECONDS = 300


class ValioApiError(Exception):
    pass


class ValioClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, customer: str | None = None):
        self.api_key = api_key if api_key is not None else config.VALIO_API_KEY
        self.base_url = (base_url or config.VALIO_API_BASE).rstrip("/")
        self.customer = customer or config.VALIO_CUSTOMER
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self.stats = {"calls": 0, "products_queried": 0, "cache_hits": 0}

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _post(self, endpoint: str, product_ids: list[str], requested_date: str) -> dict:
        body = json.dumps({"customer": self.customer, "requestedDate": requested_date,
                           "products": product_ids}).encode()
        req = urllib.request.Request(self.base_url + endpoint, data=body, method="POST", headers={
            "X-API-Key": self.api_key, "User-Agent": config.USER_AGENT, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise ValioApiError(f"Valio API {endpoint} returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ValioApiError(f"Valio API {endpoint} unreachable: {exc}") from exc
        with self._lock:
            self.stats["calls"] += 1
            self.stats["products_queried"] += len(product_ids)
        return data

    def _query(self, endpoint: str, product_ids: list[str], requested_date: str) -> dict[str, dict]:
        """Returns {productId: entry}, using a short cache and batching."""
        if not self.available:
            raise ValioApiError("VALIO_API_KEY is not set")
        now = time.monotonic()
        result, missing = {}, []
        for pid in dict.fromkeys(product_ids):
            hit = self._cache.get((endpoint, requested_date, pid))
            if hit and now - hit[0] < CACHE_SECONDS:
                result[pid] = hit[1]
                self.stats["cache_hits"] += 1
            else:
                missing.append(pid)
        for i in range(0, len(missing), MAX_PER_CALL):
            batch = missing[i:i + MAX_PER_CALL]
            data = self._post(endpoint, batch, requested_date)
            meta = {k: v for k, v in data.items() if k != "products"}
            for entry in data.get("products", []):
                entry = {**entry, "_meta": meta}
                result[entry["productId"]] = entry
                self._cache[(endpoint, requested_date, entry["productId"])] = (now, entry)
        return result

    def availability(self, product_ids: list[str], requested_date: str) -> dict[str, dict]:
        return self._query("/api/availability", product_ids, requested_date)

    def price(self, product_ids: list[str], requested_date: str) -> dict[str, dict]:
        return self._query("/api/price", product_ids, requested_date)


client = ValioClient()
