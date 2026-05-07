"""
aggregate_floor_telemetry.py — Phase 3: Server-side floor temperature aggregation.

ThingsBoard CE lacks a native multi-device aggregation node.
This script bridges that gap: it polls the latest temperature telemetry
from all 20 devices on each floor, computes the true average, and writes it
back as a Floor asset timeseries (avg_temperature).

This is NOT client-side simulation — it reads live TB telemetry and writes
to the Floor ASSET, making the aggregated data available to dashboards.

Run as a background service:
    python phase3/aggregate_floor_telemetry.py --interval 30

Environment overrides
─────────────────────
    TB_URL, TB_USER, TB_PASS
    AGG_INTERVAL  — poll interval in seconds (default: 30)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import pathlib
from typing import Optional, Any

import requests

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from phase3.topology import build_topology, CAMPUS_NAME

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("floor_agg")

TB_URL       = os.environ.get("TB_URL",       "http://localhost:9090")
TB_USER      = os.environ.get("TB_USER",      "tenant@thingsboard.org")
TB_PASS      = os.environ.get("TB_PASS",      "tenant")
AGG_INTERVAL = int(os.environ.get("AGG_INTERVAL", "30"))

_RATE_DELAY = 0.03


class TBAggClient:
    """ThingsBoard client for floor telemetry aggregation."""

    def __init__(self, base_url: str) -> None:
        self.base   = base_url.rstrip("/")
        self._token: Optional[str] = None

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
        return {
            "X-Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict | None = None) -> Any:
        resp = requests.get(
            f"{self.base}{path}", headers=self._hdrs,
            params=params, timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: Any) -> None:
        resp = requests.post(
            f"{self.base}{path}", headers=self._hdrs,
            json=body, timeout=10,
        )
        resp.raise_for_status()

    def find_device(self, name: str) -> Optional[str]:
        try:
            data = self._get("/api/tenant/devices",
                             {"pageSize": 1, "page": 0, "textSearch": name})
            for item in data.get("data", []):
                if item["name"] == name:
                    return item["id"]["id"]
        except Exception:
            pass
        return None

    def find_asset(self, name: str) -> Optional[str]:
        try:
            data = self._get("/api/tenant/assets",
                             {"pageSize": 1, "page": 0, "textSearch": name})
            for item in data.get("data", []):
                if item["name"] == name:
                    return item["id"]["id"]
        except Exception:
            pass
        return None

    def get_latest_telemetry(self, device_id: str, keys: list[str]) -> dict:
        """Return latest telemetry dict {key: value} for a device."""
        try:
            keys_str = ",".join(keys)
            data = self._get(
                f"/api/plugins/telemetry/DEVICE/{device_id}/values/timeseries",
                {"keys": keys_str},
            )
            return {k: v[0]["value"] for k, v in data.items() if v}
        except Exception as exc:
            log.debug("Telemetry fetch failed for %s: %s", device_id, exc)
            return {}

    def save_asset_timeseries(self, asset_id: str, payload: dict) -> None:
        """Save telemetry on a Floor asset."""
        self._post(
            f"/api/plugins/telemetry/ASSET/{asset_id}/timeseries/ANY",
            payload,
        )


def run_aggregation_cycle(client: TBAggClient) -> None:
    """One full aggregation cycle across all 10 floors."""
    campus = build_topology()

    for building in campus.buildings:
        for floor in building.floors:
            floor_id = client.find_asset(floor.asset_name)
            if not floor_id:
                log.warning("Floor asset not found in TB: %s", floor.asset_name)
                continue

            temps     = []
            humidities= []
            occupancies = []

            for room in floor.rooms:
                device_name = room.devices[0]
                device_id = client.find_device(device_name)
                if not device_id:
                    continue

                tel = client.get_latest_telemetry(
                    device_id, ["temperature", "humidity", "occupancy"]
                )
                if "temperature" in tel:
                    try:
                        temps.append(float(tel["temperature"]))
                    except (ValueError, TypeError):
                        pass
                if "humidity" in tel:
                    try:
                        humidities.append(float(tel["humidity"]))
                    except (ValueError, TypeError):
                        pass
                if "occupancy" in tel:
                    try:
                        occupancies.append(float(tel["occupancy"]))
                    except (ValueError, TypeError):
                        pass

                time.sleep(_RATE_DELAY)

            if not temps:
                log.debug("No temperature data for floor %s", floor.asset_name)
                continue

            avg_temp  = round(sum(temps) / len(temps), 2)
            avg_humid = round(sum(humidities) / len(humidities), 2) if humidities else None
            total_occ = int(sum(occupancies)) if occupancies else None

            ts_payload: dict = {
                "avg_temperature":   avg_temp,
                "reporting_devices": len(temps),
            }
            if avg_humid is not None:
                ts_payload["avg_humidity"] = avg_humid
            if total_occ is not None:
                ts_payload["total_occupancy"] = total_occ

            try:
                client.save_asset_timeseries(floor_id, ts_payload)
                log.info(
                    "Floor %-8s  avg_temp=%.2f°C  devices=%d/%d",
                    floor.asset_name, avg_temp, len(temps), len(floor.rooms),
                )
            except Exception as exc:
                log.error("Failed to save floor telemetry for %s: %s",
                          floor.asset_name, exc)


def main() -> None:
    p = argparse.ArgumentParser(description="Floor temperature aggregation poller")
    p.add_argument("--url",      default=TB_URL)
    p.add_argument("--user",     default=TB_USER)
    p.add_argument("--pass",     dest="password", default=TB_PASS)
    p.add_argument("--interval", type=int, default=AGG_INTERVAL,
                   help="Poll interval in seconds (default: 30)")
    p.add_argument("--once",     action="store_true",
                   help="Run a single aggregation cycle then exit")
    args = p.parse_args()

    client = TBAggClient(args.url)
    client.login(args.user, args.password)

    if args.once:
        log.info("Running single aggregation cycle…")
        run_aggregation_cycle(client)
        log.info("Done.")
        return

    log.info("Starting floor aggregation loop (interval=%ds)", args.interval)
    while True:
        try:
            run_aggregation_cycle(client)
        except Exception as exc:
            log.error("Aggregation cycle error: %s", exc)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
