"""
generate_relations_csv.py — Phase 3: Export all asset-to-asset and
device-to-room relations.

Two output files
────────────────
1. tb_relations_phase3.csv
   All parent-child "Contains" relations across the hierarchy:
       Campus → Building, Building → Floor, Floor → Room
   Columns: from_type, from_name, relation_type, to_type, to_name

2. tb_device_relations_phase3.csv
   Each of the 200 devices linked to its Room via a "Contains" relation.
   Columns: from_type, from_name, relation_type, to_type, to_name

Usage
─────
    python phase3/generate_relations_csv.py
"""

from __future__ import annotations

import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from topology import build_topology, CampusAsset

ASSET_RELATIONS_PATH  = pathlib.Path(__file__).parent / "tb_relations_phase3.csv"
DEVICE_RELATIONS_PATH = pathlib.Path(__file__).parent / "tb_device_relations_phase3.csv"

RELATION_COLS = ["from_type", "from_name", "relation_type", "to_type", "to_name"]


def _asset_relations(campus: CampusAsset) -> list[dict]:
    rows: list[dict] = []

    for building in campus.buildings:
        rows.append({
            "from_type":     "Campus",
            "from_name":     campus.asset_name,
            "relation_type": "Contains",
            "to_type":       "Building",
            "to_name":       building.asset_name,
        })

        for floor in building.floors:
            rows.append({
                "from_type":     "Building",
                "from_name":     building.asset_name,
                "relation_type": "Contains",
                "to_type":       "Floor",
                "to_name":       floor.asset_name,
            })

            for room in floor.rooms:
                rows.append({
                    "from_type":     "Floor",
                    "from_name":     floor.asset_name,
                    "relation_type": "Contains",
                    "to_type":       "Room",
                    "to_name":       room.asset_name,
                })

    return rows


def _device_relations(campus: CampusAsset) -> list[dict]:
    """One row per device → room "Contains" link."""
    rows: list[dict] = []

    for building in campus.buildings:
        for floor in building.floors:
            for room in floor.rooms:
                for device_name in room.devices:
                    rows.append({
                        "from_type":     "Room",
                        "from_name":     room.asset_name,
                        "relation_type": "Contains",
                        "to_type":       "Device",
                        "to_name":       device_name,
                    })

    return rows


def main() -> None:
    campus = build_topology()

    asset_rows  = _asset_relations(campus)
    device_rows = _device_relations(campus)

    with ASSET_RELATIONS_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RELATION_COLS)
        writer.writeheader()
        writer.writerows(asset_rows)
    print(f"Asset relations   : {len(asset_rows):3d} rows → {ASSET_RELATIONS_PATH}")

    with DEVICE_RELATIONS_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RELATION_COLS)
        writer.writeheader()
        writer.writerows(device_rows)
    print(f"Device relations  : {len(device_rows):3d} rows → {DEVICE_RELATIONS_PATH}")


if __name__ == "__main__":
    main()
