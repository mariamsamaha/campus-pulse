"""
shadow_provisioner.py — Phase 3: Shadow State Seeder.

Seeds every device's initial Desired (SHARED) and Reported (CLIENT) state
attributes so the Sync Status dashboard and conflict-detection rule chain
have data to work with.

Desired  state → SHARED_SCOPE  attributes: desired_hvac, desired_dimmer
Reported state → CLIENT_SCOPE  attributes: reported_hvac, reported_dimmer, last_seen

Usage
─────
    python phase3/shadow_provisioner.py [--dry-run] [--url URL]

Environment overrides
─────────────────────
    TB_URL, TB_USER, TB_PASS
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import pathlib
from typing import Optional, Any

import requests

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from phase3.topology import build_topology, all_rooms

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("shadow_provisioner")

TB_URL  = os.environ.get("TB_URL",  "http://localhost:9090")
TB_USER = os.environ.get("TB_USER", "tenant@thingsboard.org")
TB_PASS = os.environ.get("TB_PASS", "tenant")

_RATE_DELAY = 0.05   # seconds between API calls


class TBClient:
    """Minimal ThingsBoard REST client for shadow state provisioning."""

    def __init__(self, base_url: str, dry_run: bool = False) -> None:
        self.base = base_url.rstrip("/")
        self.dry  = dry_run
        self._token: Optional[str] = None

    def login(self, email: str, password: str) -> None:
        if self.dry:
            log.info("[DRY-RUN] login(%s)", email)
            self._token = "dry-run-token"
            return
        resp = requests.post(
            f"{self.base}/api/auth/login",
            json={"username": email, "password": password},
            timeout=10,
        )
        resp.raise_for_status()
        self._token = resp.json()["token"]
        log.info("Authenticated as %s", email)

    @property
    def _headers(self) -> dict:
        return {
            "X-Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict | None = None) -> Any:
        if self.dry:
            return {}
        resp = requests.get(
            f"{self.base}{path}", headers=self._headers,
            params=params, timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def _post_attr(self, path: str, body: dict) -> None:
        if self.dry:
            log.info("[DRY-RUN] POST %s  keys=%s", path, list(body.keys()))
            return
        resp = requests.post(
            f"{self.base}{path}", headers=self._headers,
            json=body, timeout=10,
        )
        resp.raise_for_status()

    def find_device_by_name(self, name: str) -> Optional[str]:
        """Return device entity ID string if found, else None."""
        try:
            data = self._get("/api/tenant/devices",
                             {"pageSize": 1, "page": 0, "textSearch": name})
            for item in data.get("data", []):
                if item.get("name") == name:
                    return item["id"]["id"]
        except Exception:
            pass
        return None

    def set_shared_attributes(self, device_id: str, attrs: dict) -> None:
        """Set SHARED_SCOPE attributes (Desired state)."""
        path = f"/api/plugins/telemetry/DEVICE/{device_id}/attributes/SHARED_SCOPE"
        self._post_attr(path, attrs)
        time.sleep(_RATE_DELAY)

    def set_reported_attributes(self, device_id: str, attrs: dict) -> None:
        """Seed initial Reported state into SERVER_SCOPE.

        ThingsBoard CE does NOT allow the server REST API to write CLIENT_SCOPE
        attributes — only the device itself can do that over MQTT/CoAP.
        We seed defaults in SERVER_SCOPE so the Sync Status dashboard has
        initial values to display before the first real device connection.
        When a real device connects and posts its CLIENT attrs, the rule chain
        will read from CLIENT_SCOPE for the live comparison.
        """
        path = f"/api/plugins/telemetry/DEVICE/{device_id}/attributes/SERVER_SCOPE"
        self._post_attr(path, attrs)
        time.sleep(_RATE_DELAY)


def provision_shadow_state(client: TBClient, dry_run: bool = False) -> None:
    """Seed Desired and Reported shadow state for all 200 devices."""
    campus = build_topology()
    rooms  = all_rooms(campus)

    total = len(rooms)
    ok    = 0
    warn  = 0

    log.info("=== Seeding shadow state for %d devices ===", total)

    for i, room in enumerate(rooms, 1):
        device_name = room.devices[0]   # one device per room

        device_id = client.find_device_by_name(device_name)
        if not device_id:
            log.warning("[%3d/%d] Device NOT FOUND in TB: %s", i, total, device_name)
            warn += 1
            continue

        # ── Desired state (SHARED attributes) ─────────────────────────────────
        desired = {
            "desired_hvac":   "OFF",    # HVAC desired state: "ON" | "OFF"
            "desired_dimmer": 50,       # Lighting dimmer 0-100%
        }
        client.set_shared_attributes(device_id, desired)

        # ── Reported state seed (SERVER_SCOPE defaults) ────────────────────────
        # TB REST API forbids server-side writes to CLIENT_SCOPE.
        # These SERVER_SCOPE defaults give the dashboard initial values.
        # Real device CLIENT attrs arrive once simulators connect via MQTT.
        reported = {
            "reported_hvac":   "OFF",
            "reported_dimmer": 50,
            "last_seen":       0,      # epoch ms; 0 = never seen
            "sync_status":     "PENDING",  # PENDING until device first connects
        }
        client.set_reported_attributes(device_id, reported)

        log.info("[%3d/%d] Shadow state seeded for %s", i, total, device_name)
        ok += 1

    log.info("=== Done: %d seeded, %d not found ===", ok, warn)
    if warn:
        log.warning("Run provision_hierarchy.py first to register missing devices.")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 3 shadow state seeder")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--url",  default=TB_URL)
    p.add_argument("--user", default=TB_USER)
    p.add_argument("--pass", dest="password", default=TB_PASS)
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    client = TBClient(args.url, dry_run=args.dry_run)
    client.login(args.user, args.password)
    provision_shadow_state(client, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
