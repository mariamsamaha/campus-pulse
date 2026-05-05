"""
phase3/ota/ — Secure OTA Update System for Campus Pulse.

Provides:
  - SHA-256 payload signing & verification
  - Fleet versioning and update tracking
  - Tamper detection with audit logging
  - MQTT OTA config topic routing (campus/+/+/ota)
  - Integration with World Engine nodes
"""
from __future__ import annotations

__all__ = [
    "OtaIntegrityVerifier",
    "compute_sha256",
    "FleetVersionRegistry",
    "OtaAuditLogger",
    "OtaMqttHandler",
    "OtaReceiver",
    "OtaPublisher",
    "OtaUpdateResult",
]

from .integrity import OtaIntegrityVerifier, compute_sha256
from .versioning import FleetVersionRegistry
from .audit_logger import OtaAuditLogger
from .mqtt_handler import OtaMqttHandler
from .receiver import OtaReceiver
from .publisher import OtaPublisher
from .types import OtaUpdateResult
