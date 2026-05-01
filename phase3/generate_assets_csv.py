"""
generate_assets_csv.py — Phase 3: Export the full hierarchical asset topology
to a CSV file suitable for ThingsBoard bulk import.

Output columns
──────────────
asset_name, asset_type, parent, label,
square_footage, occupant_capacity, coordinates_x, coordinates_y, room_type

Only Room rows carry attribute columns; Campus / Building / Floor rows leave
those columns blank (they have no room-level metadata).

Usage
─────
    python phase3/generate_assets_csv.py
    # → writes phase3/tb_assets_phase3.csv
"""

from __future__ import annotations

import csv
import pathlib
import sys

# Allow running from project root
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from topology import build_topology, all_rooms, CampusAsset

OUTPUT_PATH = pathlib.Path(__file__).parent / "tb_assets_phase3.csv"

COLUMNS = [
    "asset_name",
    "asset_type",
    "parent",
    "label",
    "square_footage",
    "occupant_capacity",
    "coordinates_x",
    "coordinates_y",
    "room_type",
]


def _campus_rows(campus: CampusAsset) -> list[dict]:
    rows: list[dict] = []

    # ── Campus root ───────────────────────────────────────────────────────────
    rows.append({
        "asset_name": campus.asset_name,
        "asset_type": "Campus",
        "parent":     "",
        "label":      campus.label,
    })

    for building in campus.buildings:
        # ── Building ─────────────────────────────────────────────────────────
        rows.append({
            "asset_name": building.asset_name,
            "asset_type": "Building",
            "parent":     building.parent,
            "label":      building.label,
        })

        for floor in building.floors:
            # ── Floor ────────────────────────────────────────────────────────
            rows.append({
                "asset_name": floor.asset_name,
                "asset_type": "Floor",
                "parent":     floor.parent,
                "label":      floor.label,
            })

            for room in floor.rooms:
                a = room.attributes
                # ── Room (with server-side attributes) ───────────────────────
                rows.append({
                    "asset_name":       room.asset_name,
                    "asset_type":       "Room",
                    "parent":           room.parent,
                    "label":            room.label,
                    "square_footage":   a.square_footage,
                    "occupant_capacity":a.occupant_capacity,
                    "coordinates_x":    a.coordinates_x,
                    "coordinates_y":    a.coordinates_y,
                    "room_type":        a.room_type,
                })

    return rows


def main() -> None:
    campus = build_topology()
    rows   = _campus_rows(campus)

    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    rooms_written = sum(1 for r in rows if r["asset_type"] == "Room")
    print(f"Written {len(rows)} rows ({rooms_written} rooms) → {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
