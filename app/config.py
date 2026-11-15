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

# Buyers, mandates and keys. The file in the repo holds demo keys only; point BUYERS_FILE at a private file
# in production (keys can be stored there as SHA-256 hashes: "key_sha256" instead of "key").
BUYERS_FILE = Path(os.environ.get("BUYERS_FILE") or ROOT / "data" / "buyers.json")

# Files the running server writes (all git-ignored). Off under pytest, so tests never touch the disk.
PERSIST = "pytest" not in __import__("sys").modules and os.environ.get("AGENTGATE_PERSIST", "1") != "0"
STATE_FILE = ROOT / "data" / "state.json"          # orders, quotes, audit log, mandate changes: survive a restart
DOOR_KEY_FILE = ROOT / "data" / "door_key.pem"     # the door's signing key (Ed25519) for quotes and receipts
LLM_CACHE_FILE = ROOT / "data" / "llm_cache.json"  # same request -> same AI answer, instantly and at no cost
