"""
phase3/ota/versioning.py — Fleet version tracking and update reconciliation.

Each node maintains a current_version Client Attribute. Shared Attributes
on the ThingsBoard side carry the desired (latest) version. When the two
differ, the node is flagged as "update pending."

Version format: semver-like "MAJOR.MINOR" (e.g. "1.0", "1.1", "2.0").
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("ota.versioning")


def _parse_version_tuple(version: str) -> tuple[int, int]:
    """
    Parse "MAJOR.MINOR" into (major, minor) ints.
    Falls back to (0, 0) for malformed strings.
    """
    try:
        parts = version.strip().split(".")
        return (int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return (0, 0)


def is_newer(candidate: str, current: str) -> bool:
    """Return True if candidate version is strictly newer than current."""
    return _parse_version_tuple(candidate) > _parse_version_tuple(current)


def is_downgrade(candidate: str, current: str) -> bool:
    """Return True if candidate version is strictly older than current."""
    return _parse_version_tuple(candidate) < _parse_version_tuple(current)


@dataclass
class NodeVersionState:
    """Version state for a single node."""
    node_id:          str
    current_version:  str = "1.0"
    desired_version:  str = "1.0"
    last_updated:     float = field(default_factory=time.time)
    last_update_result: Optional[str] = None

    @property
    def update_pending(self) -> bool:
        return self.current_version != self.desired_version

    @property
    def in_sync(self) -> bool:
        return self.current_version == self.desired_version

    def set_desired(self, version: str) -> None:
        self.desired_version = version

    def confirm_applied(self, version: str) -> None:
        self.current_version = version
        self.desired_version = version
        self.last_updated = time.time()
        self.last_update_result = "applied"

    def mark_rejected(self, reason: str) -> None:
        self.desired_version = self.current_version
        self.last_update_result = f"rejected: {reason}"


@dataclass
class FleetVersionRegistry:
    """
    Tracks version state across all 200 nodes.

    Usage
    -----
    registry = FleetVersionRegistry()
    registry.register("b01-f01-r101", "1.0")
    registry.set_desired_version("1.1")
    pending = registry.out_of_sync_nodes()
    registry.confirm_update("b01-f01-r101", "1.1")
    """

    _nodes: dict[str, NodeVersionState] = field(default_factory=dict)

    def register(self, node_id: str, version: str = "1.0") -> None:
        """Register a node with its initial version."""
        self._nodes[node_id] = NodeVersionState(
            node_id=node_id,
            current_version=version,
            desired_version=version,
        )
        logger.info("[OTA-VERSION] Registered node=%s version=%s", node_id, version)

    def set_desired_version(self, version: str, targets: list[str] | None = None) -> int:
        """
        Set the desired version for all (or targeted) nodes.
        Returns the number of nodes updated.
        """
        count = 0
        target_set = set(targets) if targets else set(self._nodes.keys())
        for nid in target_set:
            if nid in self._nodes:
                self._nodes[nid].set_desired(version)
                count += 1
        logger.info(
            "[OTA-VERSION] Set desired version=%s for %d nodes",
            version, count,
        )
        return count

    def confirm_update(self, node_id: str, version: str) -> bool:
        """Confirm that a node has successfully applied an update."""
        if node_id in self._nodes:
            self._nodes[node_id].confirm_applied(version)
            logger.info("[OTA-VERSION] Node %s confirmed version %s", node_id, version)
            return True
        logger.warning("[OTA-VERSION] Unknown node %s for version confirmation", node_id)
        return False

    def reject_update(self, node_id: str, reason: str) -> None:
        """Mark an update as rejected for a node."""
        if node_id in self._nodes:
            self._nodes[node_id].mark_rejected(reason)
            logger.warning(
                "[OTA-VERSION] Node %s rejected update: %s", node_id, reason
            )

    def get_node_state(self, node_id: str) -> Optional[NodeVersionState]:
        return self._nodes.get(node_id)

    def out_of_sync_nodes(self) -> list[NodeVersionState]:
        """Return all nodes where current_version != desired_version."""
        return [n for n in self._nodes.values() if not n.in_sync]

    def in_sync_nodes(self) -> list[NodeVersionState]:
        return [n for n in self._nodes.values() if n.in_sync]

    def all_nodes(self) -> list[NodeVersionState]:
        return list(self._nodes.values())

    def summary(self) -> dict:
        total = len(self._nodes)
        synced = len(self.in_sync_nodes())
        pending = len(self.out_of_sync_nodes())
        versions = {}
        for n in self._nodes.values():
            versions[n.current_version] = versions.get(n.current_version, 0) + 1
        return {
            "total": total,
            "in_sync": synced,
            "out_of_sync": pending,
            "version_distribution": versions,
        }

    def to_dashboard_rows(self) -> list[dict[str, Any]]:
        """
        Produce rows for the Sync Status dashboard table.
        Each row: device_name, current_version, desired_version, sync_status.
        """
        rows = []
        for n in sorted(self._nodes.values(), key=lambda x: x.node_id):
            rows.append({
                "device_name":     n.node_id,
                "current_version": n.current_version,
                "desired_version": n.desired_version,
                "sync_status":     "In Sync" if n.in_sync else "Update Pending",
                "last_updated":    time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(n.last_updated)
                ),
                "last_result":     n.last_update_result or "N/A",
            })
        return rows
