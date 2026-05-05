"""
phase3/ota/receiver.py — Secure OTA config receiver for World Engine nodes.

Receives OTA update messages from the MQTT config bus, verifies integrity
(SHA-256), checks version compatibility, and applies physics parameter
updates (alpha, beta) to room nodes. Handles the desired/reported state
reconciliation protocol.

Flow
----
1. MQTT message arrives on campus/+/+/ota
2. Topic target is parsed (broadcast / specific floor)
3. Payload is verified against SHA-256 signature
4. If verification fails → tamper alert logged
5. If verification passes → version checked, parameters applied
6. Applied update is confirmed back to version registry
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from .integrity import OtaIntegrityVerifier
from .audit_logger import OtaAuditLogger
from .versioning import FleetVersionRegistry, is_downgrade
from .mqtt_handler import OtaMqttHandler, OtaTopicTarget
from .types import OtaStatus, OtaUpdateResult, TamperReason

logger = logging.getLogger("ota.receiver")


class OtaReceiver:
    """
    Receives, verifies, and applies OTA configuration updates.

    Usage
    -----
    receiver = OtaReceiver(
        mqtt_handler=mqtt_handler,
        version_registry=registry,
        audit_logger=audit,
    )
    receiver.register_apply_hook(my_apply_function)
    await receiver.start()

    The apply hook receives (node_id, params) and returns the result.
    This decouples the OTA layer from the physics engine so it can
    plug into any World Engine variant.
    """

    def __init__(
        self,
        mqtt_handler: OtaMqttHandler,
        version_registry: FleetVersionRegistry,
        audit_logger: OtaAuditLogger,
        node_ids: list[str] | None = None,
    ) -> None:
        self._mqtt = mqtt_handler
        self._registry = version_registry
        self._audit = audit_logger
        self._node_ids = node_ids or []
        self._verifier = OtaIntegrityVerifier()
        self._apply_hook: Optional[Callable[[str, dict[str, Any]], OtaUpdateResult]] = None
        self._stats = {"received": 0, "applied": 0, "rejected": 0, "tampered": 0}

    def register_apply_hook(
        self, hook: Callable[[str, dict[str, Any]], OtaUpdateResult],
    ) -> None:
        """
        Register a function that actually applies parameters to a node.

        The hook receives (node_id, params_dict) and must return an
        OtaUpdateResult indicating success or failure.
        """
        self._apply_hook = hook

    async def start(self) -> None:
        """Register with the MQTT handler and start listening."""
        self._mqtt.on_ota_message(self._on_ota_message)
        logger.info(
            "[OTA-RECEIVER] Started — listening for %d nodes",
            len(self._node_ids),
        )

    async def _on_ota_message(
        self, topic: str, payload_bytes: bytes, target: OtaTopicTarget,
    ) -> None:
        """
        Callback invoked by the MQTT handler when an OTA message arrives.
        """
        self._stats["received"] += 1

        for node_id in self._node_ids:
            if not target.matches_node(node_id):
                continue

            self._process_for_node(node_id, payload_bytes, topic)

    def _process_for_node(
        self, node_id: str, payload_bytes: bytes, topic: str,
    ) -> None:
        """
        Process a single OTA payload for a specific node.
        """
        logger.info(
            "[OTA-RECEIVER] Processing update for node=%s topic=%s",
            node_id, topic,
        )

        # Step 1: Verify integrity
        ota_payload, tamper_alert = self._verifier.verify(
            payload_bytes, node_id=node_id, source_ip="mqtt-broker",
        )

        if tamper_alert:
            self._stats["tampered"] += 1
            self._audit.record(tamper_alert)
            self._stats["rejected"] += 1

            if self._registry:
                self._registry.reject_update(
                    node_id, tamper_alert.reason.value
                )
            return

        # ota_payload is guaranteed non-None here
        version = ota_payload.version
        params = ota_payload.params

        # Step 2: Version check — reject downgrades
        node_state = self._registry.get_node_state(node_id) if self._registry else None
        if node_state and is_downgrade(version, node_state.current_version):
            logger.warning(
                "[OTA-RECEIVER] Version downgrade rejected — node=%s "
                "current=%s incoming=%s",
                node_id, node_state.current_version, version,
            )
            self._stats["rejected"] += 1
            tamper_alert_downgrade = tamper_alert = type(
                'TamperAlert', (), {
                    'node_id': node_id,
                    'reason': TamperReason.VERSION_DOWNGRADE,
                    'expected_hash': version,
                    'received_hash': node_state.current_version,
                    'raw_payload': payload_bytes,
                    'source_ip': 'mqtt-broker',
                    'timestamp': 0.0,
                }
            )()
            self._audit.record(tamper_alert_downgrade)
            if self._registry:
                self._registry.reject_update(node_id, "version_downgrade")
            return

        # Step 3: Apply the update
        if self._apply_hook:
            result = self._apply_hook(node_id, params)
        else:
            result = OtaUpdateResult(
                node_id=node_id,
                status=OtaStatus.APPLIED,
                version=version,
            )

        if result.success:
            self._stats["applied"] += 1
            logger.info(
                "[OTA-RECEIVER] Update applied — node=%s version=%s",
                node_id, version,
            )
            if self._registry:
                self._registry.confirm_update(node_id, version)
        else:
            self._stats["rejected"] += 1
            logger.warning(
                "[OTA-RECEIVER] Update failed — node=%s reason=%s",
                node_id, result.reason,
            )
            if self._registry:
                self._registry.reject_update(node_id, result.reason or "apply_failed")

    @property
    def stats(self) -> dict:
        return {
            **self._stats,
            "integrity": self._verifier.stats,
            "audit": self._audit.stats(),
            "versioning": self._registry.summary() if self._registry else {},
        }
