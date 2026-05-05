"""
phase3/ota/integrity.py — SHA-256 payload signing and verification.

Core security layer for OTA updates. Every configuration payload must
carry a SHA-256 hash that the receiver independently recalculates and
compares. A mismatch means the payload was corrupted or tampered with.

Design choices
--------------
• Hash input is json.dumps(data, sort_keys=True) — canonical ordering
  prevents false mismatches from shuffled key orders.
• The hash covers all fields *except* the "signature" field itself
  (otherwise you'd need the hash to compute the hash — circular).
• The receiver strips the signature, recomputes the hash, and compares.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from .types import OtaPayload, TamperAlert, TamperReason

logger = logging.getLogger("ota.integrity")


def compute_sha256(data: dict[str, Any]) -> str:
    """
    Compute SHA-256 hex digest of a dictionary.

    Uses json.dumps(sort_keys=True) for canonical serialization so
    that structurally identical dicts always produce the same hash
    regardless of insertion order.

    Example
    -------
    >>> compute_sha256({"alpha": 0.01, "beta": 0.20})
    'a1b2c3...'
    """
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sign_payload(params: dict[str, Any], version: str) -> dict[str, Any]:
    """
    Attach a SHA-256 signature to a configuration dict.

    Returns a new dict that includes the original params, version,
    and a "signature" field.
    """
    payload_body = {
        **params,
        "version": version,
    }
    signature = compute_sha256(payload_body)
    return {
        **payload_body,
        "signature": signature,
    }


def _extract_signature(raw: dict[str, Any]) -> Optional[str]:
    """Return and remove the signature from a copy of the dict."""
    if "signature" not in raw:
        return None
    sig = raw["signature"]
    if not isinstance(sig, str) or len(sig) != 64:
        return None
    return sig


class OtaIntegrityVerifier:
    """
    Verifies OTA payloads against their SHA-256 signatures.

    Usage
    -----
    verifier = OtaIntegrityVerifier()
    ota_payload, alert = verifier.verify(raw_bytes, node_id="b01-f01-r101")
    if alert:
        # handle tamper alert
    """

    ALLOWED_PARAM_KEYS = frozenset({
        "alpha",
        "beta",
        "thermal_leakage",
        "heat_capacity",
        "hvac_mode",
        "lighting_dimmer",
        "occupancy_bias",
        "version",
    })

    def __init__(self) -> None:
        self._verified_count = 0
        self._tamper_count = 0

    def verify(
        self,
        raw_bytes: bytes,
        node_id: str = "",
        source_ip: str = "local",
    ) -> tuple[Optional[OtaPayload], Optional[TamperAlert]]:
        """
        Parse raw bytes, verify SHA-256 signature, return OtaPayload or TamperAlert.

        Returns
        -------
        (OtaPayload, None)          on success
        (None, TamperAlert)         on tamper detection
        (None, TamperAlert)         on malformed input
        """
        # 1. Parse JSON
        try:
            raw_dict: dict[str, Any] = json.loads(raw_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError):
            alert = TamperAlert(
                node_id=node_id,
                reason=TamperReason.MALFORMED_JSON,
                expected_hash="",
                received_hash="",
                raw_payload=raw_bytes,
                source_ip=source_ip,
            )
            logger.error(
                "[OTA-INTEGRITY] Tamper alert — MALFORMED_JSON — node=%s ip=%s",
                node_id, source_ip,
            )
            self._tamper_count += 1
            return None, alert

        # 2. Extract signature
        signature = _extract_signature(raw_dict)
        if signature is None:
            alert = TamperAlert(
                node_id=node_id,
                reason=TamperReason.MISSING_SIGNATURE,
                expected_hash="",
                received_hash="",
                raw_payload=raw_bytes,
                source_ip=source_ip,
            )
            logger.error(
                "[OTA-INTEGRITY] Tamper alert — MISSING_SIGNATURE — node=%s ip=%s",
                node_id, source_ip,
            )
            self._tamper_count += 1
            return None, alert

        # 3. Compute expected hash (strip signature from dict first)
        body = {k: v for k, v in raw_dict.items() if k != "signature"}
        expected_hash = compute_sha256(body)

        # 4. Compare hashes
        if expected_hash != signature:
            alert = TamperAlert(
                node_id=node_id,
                reason=TamperReason.HASH_MISMATCH,
                expected_hash=expected_hash,
                received_hash=signature,
                raw_payload=raw_bytes,
                source_ip=source_ip,
            )
            logger.error(
                "[OTA-INTEGRITY] Tamper alert — HASH_MISMATCH — node=%s "
                "expected=%s received=%s ip=%s",
                node_id, expected_hash[:16], signature[:16], source_ip,
            )
            self._tamper_count += 1
            return None, alert

        # 5. All good — extract fields
        params = {
            k: v for k, v in raw_dict.items()
            if k not in ("signature", "version")
        }
        version = raw_dict.get("version", "0.0.0")

        payload = OtaPayload(
            target=self._resolve_target(raw_dict),
            params=params,
            version=version,
            signature=signature,
            raw_payload=raw_bytes,
        )
        logger.info(
            "[OTA-INTEGRITY] Payload verified OK — version=%s node=%s",
            version, node_id,
        )
        self._verified_count += 1
        return payload, None

    def _resolve_target(self, raw_dict: dict[str, Any]) -> str:
        """
        Determine the intended target from the payload.
        Defaults to 'broadcast' if not specified.
        """
        return raw_dict.get("target", "broadcast")

    @property
    def stats(self) -> dict:
        return {
            "verified": self._verified_count,
            "tampered": self._tamper_count,
            "total":    self._verified_count + self._tamper_count,
        }
