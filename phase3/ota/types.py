"""
phase3/ota/types.py — Shared dataclasses and enums for the OTA system.
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any, Optional


class OtaStatus(enum.Enum):
    """Lifecycle states for an OTA update on a single node."""
    PENDING = "pending"
    DELIVERED = "delivered"
    VERIFIED = "verified"
    APPLIED = "applied"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class TamperReason(enum.Enum):
    """Why an OTA payload was rejected as tampered."""
    HASH_MISMATCH = "hash_mismatch"
    MALFORMED_JSON = "malformed_json"
    MISSING_SIGNATURE = "missing_signature"
    VERSION_DOWNGRADE = "version_downgrade"
    UNKNOWN_FIELD = "unknown_field"


@dataclass
class OtaUpdateResult:
    """Outcome of applying an OTA update to a single node."""
    node_id:    str
    status:     OtaStatus
    version:    str
    reason:     Optional[str] = None
    timestamp:  float = field(default_factory=time.time)

    @property
    def success(self) -> bool:
        return self.status in (OtaStatus.VERIFIED, OtaStatus.APPLIED)


@dataclass
class OtaPayload:
    """Parsed OTA configuration payload with signature."""
    target:     str          # "broadcast", "floor:<N>", "room:<id>"
    params:     dict[str, Any]
    version:    str
    signature:  str          # SHA-256 hex digest
    raw_payload: bytes       # original bytes for forensic audit
    timestamp:  float = field(default_factory=time.time)


@dataclass
class TamperAlert:
    """Security alert for a failed OTA verification."""
    node_id:        str
    reason:         TamperReason
    expected_hash:  str
    received_hash:  str
    raw_payload:    bytes
    source_ip:      str = "local"
    timestamp:      float = field(default_factory=time.time)
