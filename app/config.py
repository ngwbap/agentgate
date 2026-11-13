"""Settings from environment variables and the local .env file (never committed)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VALIO_DATA_DIR = ROOT / "data" / "valio"


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value and key.strip() not in os.environ:
            os.environ[key.strip()] = value


_load_dotenv()

VALIO_API_BASE = os.environ.get("VALIO_API_BASE", "https://junction26.valioaimo.fi")
VALIO_API_KEY = os.environ.get("VALIO_API_KEY", "")
VALIO_CUSTOMER = os.environ.get("VALIO_CUSTOMER", "hotel-aurora")
# Valio's firewall rejects default script user agents, so identify the app explicitly.
USER_AGENT = "AgentGate/0.2 (Junction 2026 team STEEL BALL)"

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = os.environ.get("CLAUDE_EFFORT", "low")
