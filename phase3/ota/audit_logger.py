"""
phase3/ota/audit_logger.py — Tamper detection audit log with ThingsBoard integration.

Records every failed OTA verification as a security alert. Stores alerts
locally (in-memory + JSON file) and can push them to ThingsBoard as
device telemetry for dashboard visualization.

Alert payload sent to ThingsBoard:
{
    "node_id":       "b01-f01-r101",
    "tamper_reason": "hash_mismatch",
    "expected_hash": "a1b2c3...",
    "received_hash": "d4e5f6...",
    "source_ip":     "192.168.1.42",
    "raw_payload":   "..." (hex-encoded),
    "severity":      "CRITICAL"
}
"""
from __future__ import annotations

import json
import logging
import pathlib
import time
from dataclasses import asdict, dataclass, field
from typing import Any, List, Optional

from .types import TamperAlert, TamperReason

logger = logging.getLogger("ota.audit_logger")

TAMPER_FILE = pathlib.Path(__file__).parent.parent.parent / "phase3" / "ota_tamper_log.json"

SEVERITY_MAP: dict[TamperReason, str] = {
    TamperReason.HASH_MISMATCH:     "CRITICAL",
    TamperReason.MALFORMED_JSON:    "HIGH",
    TamperReason.MISSING_SIGNATURE: "CRITICAL",
    TamperReason.VERSION_DOWNGRADE: "HIGH",
    TamperReason.UNKNOWN_FIELD:     "MEDIUM",
}


@dataclass
class AuditEntry:
    """Persistent audit log entry."""
    alert_id:       str
    node_id:        str
    tamper_reason:  str
    expected_hash:  str
    received_hash:  str
    source_ip:      str
    raw_payload_hex: str
    severity:       str
    timestamp:      float


class OtaAuditLogger:
    """
    Collects, stores, and reports OTA tamper alerts.

    Usage
    -----
    audit = OtaAuditLogger()
    audit.record(tamper_alert)
    print(audit.stats())
    audit.save()       # persist to disk
    """

    def __init__(self, log_file: pathlib.Path = TAMPER_FILE) -> None:
        self._log_file = log_file
        self._entries: list[AuditEntry] = []
        self._counter = 0
        self._load_existing()

    def record(self, alert: TamperAlert) -> AuditEntry:
        """
        Record a tamper alert and return the audit entry.
        """
        self._counter += 1
        severity = SEVERITY_MAP.get(alert.reason, "MEDIUM")
        entry = AuditEntry(
            alert_id=f"OTA-ALERT-{self._counter:06d}",
            node_id=alert.node_id,
            tamper_reason=alert.reason.value,
            expected_hash=alert.expected_hash,
            received_hash=alert.received_hash,
            source_ip=alert.source_ip,
            raw_payload_hex=alert.raw_payload.hex(),
            severity=severity,
            timestamp=alert.timestamp,
        )
        self._entries.append(entry)

        logger.critical(
            "[OTA-AUDIT] %s — Security Tampering Alert! node=%s reason=%s "
            "severity=%s ip=%s",
            entry.alert_id, entry.node_id, entry.tamper_reason,
            entry.severity, entry.source_ip,
        )
        return entry

    def get_alerts(
        self,
        node_id: str | None = None,
        severity: str | None = None,
        since: float | None = None,
    ) -> list[AuditEntry]:
        """Filter alerts by node, severity, and/or timestamp."""
        results = self._entries
        if node_id:
            results = [e for e in results if e.node_id == node_id]
        if severity:
            results = [e for e in results if e.severity == severity]
        if since:
            results = [e for e in results if e.timestamp >= since]
        return results

    def get_alerts_for_thingsboard(self, limit: int = 100) -> list[dict[str, Any]]:
        """
        Format recent alerts for ThingsBoard telemetry ingestion.
        Each dict can be sent as telemetry on the rootcampus asset.
        """
        recent = self._entries[-limit:]
        return [
            {
                "ts": int(e.timestamp * 1000),
                "values": {
                    "ota_tamper_alert_id":    e.alert_id,
                    "ota_tamper_node_id":     e.node_id,
                    "ota_tamper_reason":      e.tamper_reason,
                    "ota_tamper_severity":    e.severity,
                    "ota_tamper_expected":    e.expected_hash[:16],
                    "ota_tamper_received":    e.received_hash[:16],
                    "ota_tamper_source_ip":   e.source_ip,
                },
            }
            for e in recent
        ]

    def stats(self) -> dict:
        """Summary statistics of all recorded tamper alerts."""
        by_reason: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        by_node: dict[str, int] = {}
        for e in self._entries:
            by_reason[e.tamper_reason] = by_reason.get(e.tamper_reason, 0) + 1
            by_severity[e.severity] = by_severity.get(e.severity, 0) + 1
            by_node[e.node_id] = by_node.get(e.node_id, 0) + 1
        return {
            "total_alerts": len(self._entries),
            "by_reason": by_reason,
            "by_severity": by_severity,
            "by_node": by_node,
            "critical_count": by_severity.get("CRITICAL", 0),
        }

    def save(self) -> None:
        """Persist all entries to JSON file."""
        data = [asdict(e) for e in self._entries]
        self._log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._log_file, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("[OTA-AUDIT] Saved %d entries to %s", len(data), self._log_file)

    def clear(self) -> None:
        """Clear all in-memory entries and the log file."""
        self._entries.clear()
        self._counter = 0
        if self._log_file.exists():
            self._log_file.unlink()
        logger.info("[OTA-AUDIT] Cleared all entries")

    def _load_existing(self) -> None:
        """Load entries from a previous log file if it exists."""
        if self._log_file.exists():
            try:
                with open(self._log_file) as f:
                    raw = json.load(f)
                self._entries = [
                    AuditEntry(**item) for item in raw
                ]
                self._counter = len(self._entries)
                logger.info(
                    "[OTA-AUDIT] Loaded %d existing entries from %s",
                    len(self._entries), self._log_file,
                )
            except Exception as exc:
                logger.warning(
                    "[OTA-AUDIT] Failed to load existing log: %s", exc
                )
