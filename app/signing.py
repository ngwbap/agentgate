"""The door's signature: quotes and receipts are signed with Ed25519, so an agent (or an auditor) can check
offline that Valio's door really said this - the price, the stock source, the delivery date, the decision.

The public key is published in the standard JWKS format at /.well-known/jwks.json.
The private key lives in data/door_key.pem (git-ignored); a fresh one is made on first start.
"""
from __future__ import annotations

import base64
import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from . import config

_key: Ed25519PrivateKey | None = None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _private() -> Ed25519PrivateKey:
    global _key
    if _key is None:
        path = config.DOOR_KEY_FILE
        if config.PERSIST and path.exists():
            _key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        else:
            _key = Ed25519PrivateKey.generate()
            if config.PERSIST:
                path.write_bytes(_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
    return _key


def _public_raw() -> bytes:
    return _private().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def key_id() -> str:
    return hashlib.sha256(_public_raw()).hexdigest()[:16]


def canonical(payload: dict) -> bytes:
    """One fixed way to write the payload as bytes, so anyone can rebuild exactly what was signed."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False).encode()


def sign(payload: dict) -> dict:
    """The payload together with the door's signature over its canonical JSON."""
    return {"payload": payload, "alg": "Ed25519", "key_id": key_id(), "signature": _b64(_private().sign(canonical(payload))),
            "verify": "Ed25519 over the canonical JSON of payload (sorted keys, no spaces, UTF-8); "
                      "public key at /.well-known/jwks.json, or POST the whole object to /api/v1/verify"}


def verify(payload: dict, signature: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(_public_raw()).verify(_unb64(signature), canonical(payload))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def jwks() -> dict:
    return {"keys": [{"kty": "OKP", "crv": "Ed25519", "x": _b64(_public_raw()), "kid": key_id(), "use": "sig", "alg": "EdDSA"}]}
