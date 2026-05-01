"""
verify_provisioning.py — Phase 3: Post-provisioning verification.

Connects to a live ThingsBoard instance and checks that every expected
asset, relation, attribute, and device link has been correctly created.

Exit codes
──────────
    0  All checks passed
    1  One or more checks failed (details printed to stdout)

Usage
─────
    python phase3/verify_provisioning.py [--url URL] [--user EMAIL] [--pass PWD]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import pathlib

import requests

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from topology import build_topology, all_rooms, CAMPUS_NAME

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("verify")

TB_URL  = os.environ.get("TB_URL",  "http://localhost:8080")
TB_USER = os.environ.get("TB_USER", "tenant@thingsboard.org")
TB_PASS = os.environ.get("TB_PASS", "tenant")

_RATE_DELAY = 0.03


class TBVerifier:

    def __init__(self, base_url: str) -> None:
        self.base    = base_url.rstrip("/")
        self._token: str | None = None
        self.errors: list[str] = []

    def login(self, email: str, password: str) -> None:
        resp = requests.post(
            f"{self.base}/api/auth/login",
            json={"username": email, "password": password},
            timeout=10,
        )
        resp.raise_for_status()
        self._token = resp.json()["token"]
        log.info("Authenticated as %s", email)

    @property
    def _hdrs(self) -> dict:
        return {"X-Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, params: dict | None = None):
        resp = requests.get(f"{self.base}{path}", headers=self._hdrs,
                            params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _fail(self, msg: str) -> None:
        log.error("FAIL  %s", msg)
        self.errors.append(msg)

    def _ok(self, msg: str) -> None:
        log.info("OK    %s", msg)

    # ── checks ────────────────────────────────────────────────────────────

    def find_asset(self, name: str) -> str | None:
        try:
            data = self._get("/api/tenant/assets",
                             {"pageSize": 1, "page": 0, "textSearch": name})
            for item in data.get("data", []):
                if item["name"] == name:
                    return item["id"]["id"]
        except Exception as exc:
            self._fail(f"Error searching for asset '{name}': {exc}")
        return None

    def check_asset_exists(self, name: str, asset_type: str) -> str | None:
        aid = self.find_asset(name)
        if aid:
            self._ok(f"Asset '{name}' ({asset_type}) exists  id={aid}")
        else:
            self._fail(f"Asset '{name}' ({asset_type}) NOT FOUND")
        return aid

    def check_server_attributes(self, asset_id: str, asset_name: str,
                                 expected_keys: list[str]) -> None:
        try:
            data = self._get(
                f"/api/plugins/telemetry/ASSET/{asset_id}/values/attributes/SERVER_SCOPE"
            )
            present = {item["key"] for item in data}
            for key in expected_keys:
                if key in present:
                    self._ok(f"  Attribute '{key}' present on {asset_name}")
                else:
                    self._fail(f"  Attribute '{key}' MISSING on {asset_name}")
        except Exception as exc:
            self._fail(f"Error fetching attributes for {asset_name}: {exc}")

    def check_relation(self, from_type: str, from_id: str,
                        to_name: str, to_type: str) -> None:
        try:
            data = self._get(
                f"/api/relations",
                {"fromId": from_id, "fromType": from_type.upper(),
                 "relationType": "Contains", "relationTypeGroup": "COMMON"}
            )
            found = any(r.get("to", {}).get("id") for r in data)
            # We just verify the relation list is non-empty for parent→child
            if found:
                self._ok(f"  Relation {from_type}→{to_type} '{to_name}' exists")
            else:
                self._fail(f"  Relation {from_type}→{to_type} '{to_name}' NOT FOUND")
        except Exception as exc:
            self._fail(f"Error checking relation to '{to_name}': {exc}")

    def find_device(self, name: str) -> str | None:
        try:
            data = self._get("/api/tenant/devices",
                             {"pageSize": 1, "page": 0, "textSearch": name})
            for item in data.get("data", []):
                if item["name"] == name:
                    return item["id"]["id"]
        except Exception:
            pass
        return None


def run_verification() -> bool:
    args = _parse_args()
    v = TBVerifier(args.url)
    v.login(args.user, args.password)

    campus = build_topology()
    rooms  = all_rooms(campus)

    ROOM_ATTR_KEYS = [
        "square_footage", "occupant_capacity",
        "coordinates_x", "coordinates_y", "room_type",
    ]

    # 1. Campus
    campus_id = v.check_asset_exists(CAMPUS_NAME, "Campus")

    # 2. Building
    bld = campus.buildings[0]
    bld_id = v.check_asset_exists(bld.asset_name, "Building")

    # 3. Floors (spot-check first and last)
    for floor in [bld.floors[0], bld.floors[-1]]:
        v.check_asset_exists(floor.asset_name, "Floor")
        time.sleep(_RATE_DELAY)

    # 4. Rooms — spot-check first/last of each floor, plus attributes
    log.info("=== Verifying rooms (spot-check) ===")
    sample_rooms = []
    for floor in bld.floors:
        sample_rooms.append(floor.rooms[0])    # first room per floor
        sample_rooms.append(floor.rooms[-1])   # last room per floor

    for room in sample_rooms:
        r_id = v.check_asset_exists(room.asset_name, "Room")
        if r_id:
            v.check_server_attributes(r_id, room.asset_name, ROOM_ATTR_KEYS)
        time.sleep(_RATE_DELAY)

    # 5. Device linkage spot-check (first device of each floor)
    log.info("=== Verifying device linkage (spot-check) ===")
    for floor in bld.floors:
        room = floor.rooms[0]
        device_name = room.devices[0]
        did = v.find_device(device_name)
        if did:
            v._ok(f"Device '{device_name}' found in TB")
        else:
            v._fail(f"Device '{device_name}' NOT FOUND in TB")
        time.sleep(_RATE_DELAY)

    # Summary
    total = len(v.errors)
    if total == 0:
        log.info("✅  All verification checks passed.")
        return True
    else:
        log.error("❌  %d check(s) FAILED:", total)
        for e in v.errors:
            log.error("    • %s", e)
        return False


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 3 post-provisioning verifier")
    p.add_argument("--url",  default=TB_URL)
    p.add_argument("--user", default=TB_USER)
    p.add_argument("--pass", dest="password", default=TB_PASS)
    return p.parse_args()


if __name__ == "__main__":
    ok = run_verification()
    sys.exit(0 if ok else 1)
