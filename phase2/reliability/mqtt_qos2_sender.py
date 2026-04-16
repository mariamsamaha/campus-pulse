"""
reliability/mqtt_qos2_sender.py
================================
Gateway-side MQTT QoS-2 command publisher.

Role in the system
------------------
In production this logic runs inside a ThingsBoard rule chain (via the
"MQTT publish" rule node) or a Node-RED "mqtt out" node configured to
QoS 2.  This Python module replicates that logic for:

  1. The reliability demo (oral evaluation evidence)
  2. The network-flicker test (see test_reliability_flicker.py)
  3. As a standalone CLI tool to fire critical commands at any room

QoS 2 handshake (RFC 3.3 §4.3.3)
----------------------------------
  Publisher          Broker           Subscriber
      │── PUBLISH ──▶│                    │
      │◀── PUBREC ───│                    │
      │── PUBREL ──▶│── PUBLISH ────────▶│
      │◀── PUBCOMP ─│◀── PUBACK ─────────│

The broker guarantees the message is delivered exactly once to the
subscriber.  The subscriber (MQTTNode._on_connect) already subscribes
with qos=2, so the full handshake is active end-to-end.

Application-level dedup in MQTTNode (_on_message) adds a second layer:
if a QoS-2 retransmit somehow reaches the app callback (e.g, broker
failover during handshake), the (client_id, packet_id) cache drops it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import gmqtt
from gmqtt import Client as MQTTClient

logger = logging.getLogger("mqtt_qos2_sender")

# ─── Critical command topics (only these use QoS 2) ─────────────────────────

CRITICAL_ACTIONS = frozenset({
    "SET_HVAC",
    "SET_TEMP",
    "SET_OCC",
    "EMERGENCY_LOCKOUT",
})


def is_critical(action: str) -> bool:
    """Return True if this action must be sent with QoS 2."""
    return action.upper() in CRITICAL_ACTIONS


# ─── Result dataclass ────────────────────────────────────────────────────────

@dataclass
class MqttSendResult:
    node_id:  str
    topic:    str
    action:   str
    qos_used: int
    success:  bool
    error:    Optional[str] = None

    def log(self) -> None:
        status = "SENT" if self.success else "FAIL"
        logger.info(
            "[MQTT-SENDER] [RELIABILITY] %s node=%s action=%s "
            "topic=%s qos=%d",
            status, self.node_id, self.action, self.topic, self.qos_used,
        )


# ─── Stats ───────────────────────────────────────────────────────────────────

@dataclass
class MqttSenderStats:
    total:   int = 0
    qos2:    int = 0      # critical commands
    qos1:    int = 0      # non-critical (if any)
    failed:  int = 0

    def record(self, r: MqttSendResult) -> None:
        self.total += 1
        if r.success:
            if r.qos_used == 2:
                self.qos2 += 1
            else:
                self.qos1 += 1
        else:
            self.failed += 1

    def summary(self) -> dict:
        return {
            "total":          self.total,
            "critical_qos2":  self.qos2,
            "non_critical":   self.qos1,
            "failed":         self.failed,
            "all_critical_qos2": self.qos1 == 0 and self.failed == 0,
        }


# ─── Sender ──────────────────────────────────────────────────────────────────

class MqttQos2Sender:
    """
    Publishes critical actuator commands to MQTT rooms with QoS 2.

    Design
    ------
    • One gmqtt client per sender instance (shared across all rooms).
    • Critical actions (SET_HVAC, SET_TEMP, SET_OCC, EMERGENCY_LOCKOUT)
      are always published with qos=2.
    • Non-critical payloads (future telemetry overrides, config) use qos=1.
    • Every publish is logged with [RELIABILITY] tag for report evidence.

    Assumptions
    -----------
    • HiveMQ CE is running and accessible at broker_host:broker_port.
    • Each room has subscribed to its /cmd topic with qos=2 before the
      publisher connects (otherwise gmqtt queues the message until the
      subscriber comes online, which is correct QoS-2 behaviour).
    """

    SENDER_CLIENT_ID = "campus-pulse-reliability-gateway"

    def __init__(self, broker_host: str = "localhost", broker_port: int = 1883) -> None:
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.stats       = MqttSenderStats()
        self._client: Optional[MQTTClient] = None
        self._connected = False

    # ── Connection ────────────────────────────────────────────────────────────

    def _on_connect(self, client, flags, rc, properties) -> None:
        self._connected = True
        logger.info(
            "[MQTT-SENDER] [RELIABILITY] Connected to %s:%d (rc=%d)",
            self.broker_host, self.broker_port, rc,
        )

    def _on_disconnect(self, client, packet, exc=None) -> None:
        self._connected = False
        logger.warning("[MQTT-SENDER] Disconnected (exc=%s)", exc)

    async def connect(self) -> None:
        self._client = MQTTClient(client_id=self.SENDER_CLIENT_ID)
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        await self._client.connect(self.broker_host, self.broker_port, keepalive=30)
        # Give the connect callback time to fire
        await asyncio.sleep(0.3)
        if not self._connected:
            raise RuntimeError(
                f"Could not connect to MQTT broker at "
                f"{self.broker_host}:{self.broker_port}"
            )
        logger.info("[MQTT-SENDER] [RELIABILITY] Broker connection established")

    async def disconnect(self) -> None:
        if self._client:
            await self._client.disconnect()
            logger.info("[MQTT-SENDER] Disconnected cleanly")

    # ── Publishing ────────────────────────────────────────────────────────────

    def send_command(
        self,
        node_id:  str,
        topic:    str,
        command:  dict[str, Any],
    ) -> MqttSendResult:
        """
        Publish a command to <topic>.

        QoS selection:
          • CRITICAL action  → qos=2  (Exactly Once)
          • everything else  → qos=1  (At Least Once, fine for non-critical)

        Returns immediately (gmqtt handles the async QoS-2 handshake
        internally on the event loop).
        """
        if self._client is None or not self._connected:
            raise RuntimeError("Call await sender.connect() first")

        action = command.get("action", "UNKNOWN").upper()
        qos    = 2 if is_critical(action) else 1
        payload = json.dumps(command)

        logger.info(
            "[MQTT-SENDER] [RELIABILITY] Publishing QoS-%d CMD → "
            "topic=%s  node=%s  action=%s",
            qos, topic, node_id, action,
        )

        try:
            self._client.publish(
                topic,
                payload,
                qos=qos,
                retain=False,
            )
            result = MqttSendResult(
                node_id=node_id, topic=topic, action=action,
                qos_used=qos, success=True,
            )
        except Exception as exc:
            logger.error(
                "[MQTT-SENDER] [RELIABILITY] Publish failed: node=%s err=%s",
                node_id, exc,
            )
            result = MqttSendResult(
                node_id=node_id, topic=topic, action=action,
                qos_used=qos, success=False, error=str(exc),
            )

        result.log()
        self.stats.record(result)
        return result

    def print_stats(self) -> None:
        s = self.stats.summary()
        print("\n" + "─" * 55)
        print("  MQTT QoS-2 SENDER RELIABILITY STATS")
        print("─" * 55)
        print(f"  Total commands sent:      {s['total']}")
        print(f"  Critical (QoS 2):         {s['critical_qos2']}")
        print(f"  Non-critical (QoS 1):     {s['non_critical']}")
        print(f"  Failed:                   {s['failed']}")
        all_ok = "✓ ALL CRITICAL USED QoS 2" if s["all_critical_qos2"] else "✗ SOME CRITICAL NOT QoS 2"
        print(f"  QoS-2 compliance:         {all_ok}")
        print("─" * 55 + "\n")
