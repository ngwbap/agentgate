"""Download Valio Aimo's challenge data files into data/valio/ (git-ignored).

Usage:  python scripts/download_data.py
Needs VALIO_API_KEY in .env. The files are for Junction 2026 use only - never commit them.
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402

FILES = {
    "/api/products/download": "products.json",
    "/api/delivery-reliability/download": "delivery_reliability.csv",
    "/api/carbon-footprint/download": "carbon_footprint.csv",
}


def main() -> None:
    if not config.VALIO_API_KEY:
        sys.exit("VALIO_API_KEY is missing from .env")
    config.VALIO_DATA_DIR.mkdir(parents=True, exist_ok=True)
    for endpoint, name in FILES.items():
        req = urllib.request.Request(config.VALIO_API_BASE + endpoint,
                                     headers={"X-API-Key": config.VALIO_API_KEY, "User-Agent": config.USER_AGENT})
        with urllib.request.urlopen(req, timeout=300) as resp:   # follows the redirect to the file
            data = resp.read()
        (config.VALIO_DATA_DIR / name).write_bytes(data)
        print(f"{name}: {len(data) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
