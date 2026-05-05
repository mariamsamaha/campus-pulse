"""
phase3/ota/publisher.py — OTA update publisher with SHA-256 signing.

Creates, signs, and dispatches OTA configuration updates to the fleet
via the MQTT config bus. Supports:
  - Broadcast (all 200 rooms)
  - Floor-targeted (e.g. floor 5 only)
  - Room-targeted (single room)

Every payload is signed with SHA-256 before transmission.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from .integrity import sign_payload, compute_sha256
from .mqtt_handler import OtaMqttHandler
from .versioning import FleetVersionRegistry

logger = logging.getLogger("ota.publisher")

BUILDING_ID = "b01"


class OtaPublisher:
    """
    Signs and publishes OTA configuration updates.

    Usage
    -----
    publisher = OtaPublisher(mqtt_handler, version_registry)
    await publisher.connect()

    # Broadcast to all rooms
    await publisher.broadcast(
        params={"alpha": 0.012, "beta": 0.22},
        version="1.1",
    )

    # Target a specific floor
    await publisher.target_floor(
        floor_num=5,
        params={"alpha": 0.015},
        version="1.2",
    )
    """

    def __init__(
        self,
        mqtt_handler: OtaMqttHandler,
        version_registry: FleetVersionRegistry,
    ) -> None:
        self._mqtt = mqtt_handler
        self._registry = version_registry
        self._stats = {"broadcast": 0, "floor_targeted": 0, "room_targeted": 0}

    async def connect(self) -> None:
        """Connect to the MQTT broker."""
        await self._mqtt.connect()

    async def disconnect(self) -> None:
        """Disconnect from the MQTT broker."""
        await self._mqtt.disconnect()

    def _build_signed_payload(
        self,
        params: dict[str, Any],
        version: str,
    ) -> dict[str, Any]:
        """
        Build and sign an OTA payload.

        Uses json.dumps(sort_keys=True) internally so the SHA-256 hash
        is deterministic and verifiable by the receiver.
        """
        signed = sign_payload(params, version)
        logger.info(
            "[OTA-PUBLISHER] Built signed payload — version=%s "
            "signature=%s params=%s",
            version, signed["signature"][:16], list(params.keys()),
        )
        return signed

    async def broadcast(
        self,
        params: dict[str, Any],
        version: str,
    ) -> None:
        """
        Push an OTA update to ALL rooms on the campus.

        Publishes to the wildcard topic campus/+/+/ota which every
        node's MQTT handler is subscribed to.
        """
        signed = self._build_signed_payload(params, version)
        topic = f"campus/+/+/ota"

        await self._mqtt.publish_ota_command(topic, signed)

        # Update the desired version for all nodes in the registry
        self._registry.set_desired_version(version)

        self._stats["broadcast"] += 1
        logger.info(
            "[OTA-PUBLISHER] Broadcast OTA update — version=%s topic=%s",
            version, topic,
        )

    async def target_floor(
        self,
        floor_num: int,
        params: dict[str, Any],
        version: str,
    ) -> None:
        """
        Push an OTA update to all rooms on a specific floor.
        """
        signed = self._build_signed_payload(params, version)
        topic = f"campus/{BUILDING_ID}/f{floor_num:02d}/ota"

        await self._mqtt.publish_ota_command(topic, signed)

        # Update desired version for nodes on this floor
        floor_nodes = [
            n.node_id for n in self._registry.all_nodes()
            if f"f{floor_num:02d}" in n.node_id
        ]
        if floor_nodes:
            self._registry.set_desired_version(version, targets=floor_nodes)

        self._stats["floor_targeted"] += 1
        logger.info(
            "[OTA-PUBLISHER] Floor-targeted OTA update — floor=%d version=%s",
            floor_num, version,
        )

    async def target_room(
        self,
        node_id: str,
        params: dict[str, Any],
        version: str,
    ) -> None:
        """
        Push an OTA update to a single specific room.

        Parses the node_id (e.g. 'b01-f03-r301') to construct the
        correct floor-level topic.
        """
        signed = self._build_signed_payload(params, version)
        parts = node_id.split("-")
        if len(parts) >= 2:
            building = parts[0]
            floor = parts[1]
            topic = f"campus/{building}/{floor}/ota"
        else:
            topic = f"campus/+/+/ota"

        await self._mqtt.publish_ota_command(topic, signed)

        self._registry.set_desired_version(version, targets=[node_id])

        self._stats["room_targeted"] += 1
        logger.info(
            "[OTA-PUBLISHER] Room-targeted OTA update — node=%s version=%s",
            node_id, version,
        )

    @property
    def stats(self) -> dict:
        return {
            **self._stats,
            "fleet_versioning": self._registry.summary(),
        }

    def print_stats(self) -> None:
        """Print a human-readable summary of publishing activity."""
        s = self.stats
        print("\n" + "=" * 55)
        print("  OTA PUBLISHER STATS")
        print("=" * 55)
        print(f"  Broadcasts:       {s['broadcast']}")
        print(f"  Floor-targeted:   {s['floor_targeted']}")
        print(f"  Room-targeted:    {s['room_targeted']}")
        print(f"  Fleet in-sync:    {s['fleet_versioning']['in_sync']}/{s['fleet_versioning']['total']}")
        print(f"  Fleet pending:    {s['fleet_versioning']['out_of_sync']}")
        print("=" * 55 + "\n")
