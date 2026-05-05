"""
phase3/ota/mqtt_handler.py — MQTT wildcard topic handler for OTA config bus.

Subscribes to campus/+/+/ota using MQTT wildcards and routes incoming
updates to the correct nodes based on the topic path:

  campus/b01/f01/ota      → all rooms on floor 1 of building 1
  campus/b01/f05/ota      → all rooms on floor 5 of building 1
  campus/+/+/ota          → broadcast to all 200 rooms

Each node can determine whether an update applies to it by parsing the
topic string against its own building/floor/room identity.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Optional

import gmqtt
from gmqtt import Client as MQTTClient

logger = logging.getLogger("ota.mqtt_handler")

OTA_TOPIC_WILDCARD = "campus/+/+/ota"
OTA_QOS = 1

TOPIC_PATTERN = re.compile(r"^campus/(b\d\d)/f(\d\d)/ota$")


@dataclass
class OtaTopicTarget:
    """Parsed target from an OTA MQTT topic."""
    building: str          # "b01"
    floor: str             # "f01"
    is_floor_target: bool  # True = specific floor, False = wildcard

    @classmethod
    def from_topic(cls, topic: str) -> "OtaTopicTarget":
        m = TOPIC_PATTERN.match(topic)
        if m:
            return cls(
                building=m.group(1),
                floor=m.group(2),
                is_floor_target=True,
            )
        return cls(building="*", floor="*", is_floor_target=False)

    def matches_node(self, node_id: str) -> bool:
        parts = node_id.split("-")
        if len(parts) < 3:
            return False
        node_building = parts[0]    # "b01"
        node_floor = parts[1][1:]   # "f05" → "05"

        if self.building != "*" and node_building != self.building:
            return False
        if self.is_floor_target and self.floor != "*" and node_floor != self.floor:
            return False
        return True


class OtaMqttHandler:
    """
    MQTT handler that subscribes to the OTA config bus and dispatches
    incoming messages to registered callbacks.

    Usage
    -----
    handler = OtaMqttHandler(broker_host="localhost")
    await handler.connect()
    handler.on_ota_message(my_callback)
    await handler.start_listening()

    Callback signature:
        async def my_callback(
            topic: str,
            payload_bytes: bytes,
            target: OtaTopicTarget,
        ) -> None:
            ...
    """

    def __init__(
        self,
        broker_host: str = "localhost",
        broker_port: int = 1883,
        client_id: str = "campus-pulse-ota-listener",
    ) -> None:
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.client_id = client_id
        self._client: Optional[MQTTClient] = None
        self._callbacks: list[Callable[[str, bytes, OtaTopicTarget], Coroutine]] = []
        self._connected = False
        self._listening = False

    def on_ota_message(
        self, callback: Callable[[str, bytes, OtaTopicTarget], Coroutine],
    ) -> None:
        """Register a callback for incoming OTA config messages."""
        self._callbacks.append(callback)

    def _on_connect(self, client, flags, rc, properties) -> None:
        logger.info("[OTA-MQTT] Connected to broker (rc=%d) — subscribing %s", rc, OTA_TOPIC_WILDCARD)
        self._connected = True
        client.subscribe(OTA_TOPIC_WILDCARD, qos=OTA_QOS)

    def _on_disconnect(self, client, packet, exc=None) -> None:
        self._connected = False
        logger.warning("[OTA-MQTT] Disconnected from broker (exc=%s)", exc)

    def _on_message(self, client, topic, payload_bytes, qos, properties) -> None:
        target = OtaTopicTarget.from_topic(topic)
        logger.info(
            "[OTA-MQTT] Received OTA config on topic=%s target=%s size=%d bytes",
            topic,
            "broadcast" if not target.is_floor_target else f"{target.building}/{target.floor}",
            len(payload_bytes),
        )
        for cb in self._callbacks:
            asyncio.ensure_future(cb(topic, payload_bytes, target))

    async def connect(self) -> None:
        """Connect to the MQTT broker and subscribe to OTA topics."""
        self._client = MQTTClient(client_id=self.client_id)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        retries = 0
        while not self._connected:
            try:
                await self._client.connect(self.broker_host, self.broker_port, keepalive=60)
                await asyncio.sleep(0.5)
            except Exception as exc:
                retries += 1
                if retries > 10:
                    raise RuntimeError(
                        f"[OTA-MQTT] Failed to connect after 10 retries: {exc}"
                    )
                logger.warning(
                    "[OTA-MQTT] Connection failed (retry %d): %s", retries, exc
                )
                await asyncio.sleep(3)

    async def start_listening(self) -> None:
        """Block and keep the listener alive. Run as a background task."""
        self._listening = True
        logger.info("[OTA-MQTT] Listening for OTA config messages on %s", OTA_TOPIC_WILDCARD)
        while self._listening:
            await asyncio.sleep(1)

    def stop_listening(self) -> None:
        """Stop the listening loop."""
        self._listening = False
        logger.info("[OTA-MQTT] Stopping OTA config listener")

    async def disconnect(self) -> None:
        """Disconnect from the MQTT broker."""
        self.stop_listening()
        if self._client:
            await self._client.disconnect()
            logger.info("[OTA-MQTT] Disconnected from broker")

    async def publish_ota_command(
        self,
        topic: str,
        payload: dict[str, Any],
        qos: int = OTA_QOS,
    ) -> None:
        """
        Publish an OTA config command to a specific topic.
        Used by the OTA publisher side.
        """
        if not self._client or not self._connected:
            raise RuntimeError("Not connected to MQTT broker")
        self._client.publish(topic, json.dumps(payload), qos=qos, retain=False)
        logger.info("[OTA-MQTT] Published OTA command to topic=%s", topic)
