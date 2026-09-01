"""Test-only positive harness for render signatures; production never imports this module."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sign_render(schema_version: str, issuer: str, payload: dict[str, Any], key: bytes) -> dict[str, Any]:
    signed = {"schema_version": schema_version, "issuer": issuer, "payload": payload}
    return {**signed, "hmac_sha256": hmac.new(key, canonical_json(signed), hashlib.sha256).hexdigest()}


def verify_render(envelope: dict[str, Any], key_path: Path, issuer: str, engine: dict[str, Any]) -> dict[str, Any]:
    if set(envelope) != {"schema_version", "issuer", "payload", "hmac_sha256"}:
        raise ValueError("render envelope is not closed")
    signed = {key: envelope[key] for key in ("schema_version", "issuer", "payload")}
    expected = hmac.new(key_path.read_bytes(), canonical_json(signed), hashlib.sha256).hexdigest()
    if envelope["schema_version"] != "render-result.v1" or envelope["issuer"] != issuer:
        raise ValueError("test render identity mismatch")
    if not hmac.compare_digest(expected, envelope["hmac_sha256"]):
        raise ValueError("test render signature mismatch")
    if envelope["payload"].get("engine") != engine:
        raise ValueError("test render engine mismatch")
    return envelope["payload"]
