"""
provision_hierarchy.py — Phase 3: Live ThingsBoard provisioner.

Executes the full Campus → Building → Floor → Room asset hierarchy creation
against a running ThingsBoard instance via its REST API, then:
  • Sets server-side attributes on every Room asset.
  • Creates "Contains" relations at every hierarchy level.
  • Links all 200 simulated devices to their Room assets.

Prerequisites
─────────────
    pip install requests
    ThingsBoard running (default: http://localhost:8080)

Environment overrides
─────────────────────
    TB_URL      ThingsBoard base URL   (default: http://localhost:8080)
    TB_USER     Tenant admin e-mail    (default: tenant@thingsboard.org)
    TB_PASS     Tenant admin password  (default: tenant)

Usage
─────
    python phase3/provision_hierarchy.py [--dry-run]

    --dry-run  Print every API call without executing it (useful for CI).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Optional

import requests

# Allow running from project root
sys.path.insert(0, str(__file__[: __file__.rfind("/phase3")]))
from phase3.topology import build_topology, all_rooms, CampusAsset

# ─────────────────────── logging ────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("provision")

# ─────────────────────── config ─────────────────────────────────────────────

TB_URL  = os.environ.get("TB_URL",  "http://localhost:8080")
TB_USER = os.environ.get("TB_USER", "tenant@thingsboard.org")
TB_PASS = os.environ.get("TB_PASS", "tenant")

# How long to wait between API calls (seconds) to avoid rate-limit errors
_RATE_DELAY = 0.05


# ─────────────────────── TB REST client ─────────────────────────────────────

class TBClient:
    """Minimal ThingsBoard REST client covering assets, relations, attributes."""

    def __init__(self, base_url: str, dry_run: bool = False) -> None:
        self.base = base_url.rstrip("/")
        self.dry  = dry_run
        self._token: Optional[str] = None

    # ── auth ──────────────────────────────────────────────────────────────

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
        return {"X-Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def _get(self, path: str, params: dict | None = None) -> Any:
        if self.dry:
            return {}
        resp = requests.get(f"{self.base}{path}", headers=self._headers,
                            params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> Any:
        if self.dry:
            log.info("[DRY-RUN] POST %s  body=%s", path, json.dumps(body)[:120])
            return {"id": {"id": "dry-run-id"}}
        resp = requests.post(f"{self.base}{path}", headers=self._headers,
                             json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _post_attr(self, path: str, body: dict) -> None:
        if self.dry:
            log.info("[DRY-RUN] POST %s  keys=%s", path, list(body.keys()))
            return
        resp = requests.post(f"{self.base}{path}", headers=self._headers,
                             json=body, timeout=10)
        resp.raise_for_status()

    # ── assets ────────────────────────────────────────────────────────────

    def find_asset_by_name(self, name: str) -> Optional[str]:
        """Return asset ID string if found, else None."""
        try:
            data = self._get("/api/tenant/assets", {"pageSize": 1, "page": 0,
                                                     "textSearch": name})
            for item in data.get("data", []):
                if item.get("name") == name:
                    return item["id"]["id"]
        except Exception:
            pass
        return None

    def upsert_asset(self, name: str, asset_type: str, label: str) -> str:
        """Create asset if it doesn't exist; return its ID."""
        existing_id = self.find_asset_by_name(name)
        if existing_id:
            log.debug("Asset already exists: %s (%s)", name, existing_id)
            return existing_id

        body = {"name": name, "type": asset_type, "label": label}
        result = self._post("/api/asset", body)
        asset_id = result.get("id", {}).get("id", "dry-run-id")
        log.info("Created asset %-30s  [%s]  id=%s", name, asset_type, asset_id)
        time.sleep(_RATE_DELAY)
        return asset_id

    # ── relations ─────────────────────────────────────────────────────────

    def add_relation(
        self,
        from_type: str, from_id: str,
        to_type:   str, to_id:   str,
        rel_type:  str = "Contains",
    ) -> None:
        body = {
            "from": {"entityType": from_type.upper(), "id": from_id},
            "to":   {"entityType": to_type.upper(),   "id": to_id},
            "type": rel_type,
            "typeGroup": "COMMON",
        }
        self._post("/api/relation", body)
        time.sleep(_RATE_DELAY)

    # ── server-side attributes ────────────────────────────────────────────

    def set_server_attributes(self, asset_id: str, attrs: dict) -> None:
        self._post_attr(f"/api/plugins/telemetry/ASSET/{asset_id}/attributes/SERVER_SCOPE", attrs)
        time.sleep(_RATE_DELAY)

    # ── device lookup ─────────────────────────────────────────────────────

    def find_device_by_name(self, name: str) -> Optional[str]:
        try:
            data = self._get("/api/tenant/devices", {"pageSize": 1, "page": 0,
                                                      "textSearch": name})
            for item in data.get("data", []):
                if item.get("name") == name:
                    return item["id"]["id"]
        except Exception:
            pass
        return None

    def add_device_relation(self, room_id: str, device_id: str) -> None:
        body = {
            "from": {"entityType": "ASSET",  "id": room_id},
            "to":   {"entityType": "DEVICE", "id": device_id},
            "type": "Contains",
            "typeGroup": "COMMON",
        }
        self._post("/api/relation", body)
        time.sleep(_RATE_DELAY)


# ─────────────────────── provisioner ────────────────────────────────────────

def provision(client: TBClient, campus: CampusAsset) -> None:
    id_map: dict[str, str] = {}   # asset_name → ThingsBoard entity ID

    # ── 1. Campus root ────────────────────────────────────────────────────
    log.info("=== Creating campus root: %s ===", campus.asset_name)
    campus_id = client.upsert_asset(campus.asset_name, "Campus", campus.label)
    id_map[campus.asset_name] = campus_id

    for building in campus.buildings:
        # ── 2. Building ───────────────────────────────────────────────────
        log.info("=== Creating building: %s ===", building.asset_name)
        b_id = client.upsert_asset(building.asset_name, "Building", building.label)
        id_map[building.asset_name] = b_id
        client.add_relation("Asset", campus_id, "Asset", b_id)

        for floor in building.floors:
            # ── 3. Floor ─────────────────────────────────────────────────
            log.info("  Creating floor: %s", floor.asset_name)
            f_id = client.upsert_asset(floor.asset_name, "Floor", floor.label)
            id_map[floor.asset_name] = f_id
            client.add_relation("Asset", b_id, "Asset", f_id)

            for room in floor.rooms:
                # ── 4. Room ──────────────────────────────────────────────
                r_id = client.upsert_asset(room.asset_name, "Room", room.label)
                id_map[room.asset_name] = r_id
                client.add_relation("Asset", f_id, "Asset", r_id)

                # ── 5. Server-side attributes ─────────────────────────────
                a = room.attributes
                attrs = {
                    "square_footage":    a.square_footage,
                    "occupant_capacity": a.occupant_capacity,
                    "coordinates_x":     a.coordinates_x,
                    "coordinates_y":     a.coordinates_y,
                    "room_type":         a.room_type,
                }
                client.set_server_attributes(r_id, attrs)
                log.debug("    Attributes set on %s", room.asset_name)

                # ── 6. Device → Room "Contains" relation ──────────────────
                for device_name in room.devices:
                    device_id = client.find_device_by_name(device_name)
                    if device_id:
                        client.add_device_relation(r_id, device_id)
                        log.debug("    Linked device %s → %s", device_name, room.asset_name)
                    else:
                        log.warning("    Device NOT FOUND in TB: %s", device_name)

    rooms_provisioned = len(all_rooms(campus))
    log.info("=== Provisioning complete: %d rooms, %d total assets ===",
             rooms_provisioned, len(id_map))


# ─────────────────────── entry point ────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 3 ThingsBoard hierarchy provisioner")
    p.add_argument("--dry-run", action="store_true",
                   help="Print API calls without executing them")
    p.add_argument("--url",  default=TB_URL,  help="ThingsBoard base URL")
    p.add_argument("--user", default=TB_USER, help="Tenant admin e-mail")
    p.add_argument("--pass", dest="password", default=TB_PASS, help="Password")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    client = TBClient(args.url, dry_run=args.dry_run)
    client.login(args.user, args.password)

    campus = build_topology()
    provision(client, campus)


if __name__ == "__main__":
    main()
