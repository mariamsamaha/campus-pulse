"""
topology.py — Phase 3: Deterministic asset topology generator.

Produces the full Campus → Building → Floor → Room hierarchy for
ZC-Main-Campus / B01 (10 floors × 20 rooms = 200 rooms).

Room metadata (server-side attributes):
    • square_footage    (number)
    • occupant_capacity (integer)
    • coordinates_x     (pixel position, integer)
    • coordinates_y     (pixel position, integer)
    • room_type         ("lab" | "office" | "lecture_hall" | "server_room" | "storage")

Design rationale
────────────────
All values are computed deterministically from the room's (floor, room_index)
so the same topology can be regenerated without a database:

  • room_type is assigned via a repeating 5-cycle per floor
  • square_footage is derived from the type's base area ± a small modulo offset
  • occupant_capacity is (square_footage // 15), clamped to type minimums
  • coordinates are laid out on a virtual 1200 × 800 px floor-plan grid:
      x: room column (0..9) × 110 + 60
      y: floor row  (0..9) × 70  + 40
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import List, Optional

# ─────────────────────────── constants ───────────────────────────────────────

CAMPUS_NAME   = "ZC-Main-Campus"
BUILDING_NAME = "B01"
NUM_FLOORS    = 10
ROOMS_PER_FLOOR = 20

# room_type cycle (repeats every 5 rooms within a floor)
ROOM_TYPE_CYCLE = ["lab", "office", "lecture_hall", "server_room", "storage"]

# base square_footage per room type
_BASE_SQ_FT: dict[str, int] = {
    "lab":          550,
    "office":       300,
    "lecture_hall": 900,
    "server_room":  200,
    "storage":      150,
}

# minimum occupancy per room type (so we never get 0-capacity rooms)
_MIN_CAPACITY: dict[str, int] = {
    "lab":          10,
    "office":        4,
    "lecture_hall": 30,
    "server_room":   2,
    "storage":       1,
}

# Floor-plan grid dimensions (pixels)
_GRID_COLS      = 10        # rooms spread across 10 columns
_COL_SPACING_PX = 110
_COL_OFFSET_PX  = 60
_ROW_SPACING_PX = 70
_ROW_OFFSET_PX  = 40


# ─────────────────────────── data models ─────────────────────────────────────

@dataclass
class RoomAttributes:
    """Server-side static metadata for a Room asset."""
    square_footage:    float
    occupant_capacity: int
    coordinates_x:    int
    coordinates_y:    int
    room_type:        str


@dataclass
class RoomAsset:
    asset_name:  str           # e.g. "B01-F01-R101"
    label:       str           # human-readable label
    parent:      str           # parent Floor asset name
    attributes:  RoomAttributes
    devices:     List[str] = field(default_factory=list)   # linked device names


@dataclass
class FloorAsset:
    asset_name: str            # e.g. "B01-F01"
    label:      str
    parent:     str            # parent Building asset name
    rooms:      List[RoomAsset] = field(default_factory=list)


@dataclass
class BuildingAsset:
    asset_name: str            # e.g. "B01"
    label:      str
    parent:     str            # parent Campus asset name
    floors:     List[FloorAsset] = field(default_factory=list)


@dataclass
class CampusAsset:
    asset_name: str            # "ZC-Main-Campus"
    label:      str
    buildings:  List[BuildingAsset] = field(default_factory=list)


# ─────────────────────────── helpers ─────────────────────────────────────────

def _room_type(floor_idx: int, room_idx: int) -> str:
    """Return room type from the deterministic 5-cycle."""
    return ROOM_TYPE_CYCLE[(floor_idx + room_idx) % len(ROOM_TYPE_CYCLE)]


def _square_footage(rtype: str, floor_idx: int, room_idx: int) -> float:
    """Base area ± small offset so every room has a unique value."""
    base = _BASE_SQ_FT[rtype]
    offset = ((floor_idx * ROOMS_PER_FLOOR + room_idx) % 9) * 5  # 0..40 in steps of 5
    return float(base + offset)


def _occupant_capacity(sq_ft: float, rtype: str) -> int:
    raw = int(sq_ft // 15)
    return max(raw, _MIN_CAPACITY[rtype])


def _coordinates(floor_idx: int, room_idx: int) -> tuple[int, int]:
    """
    Map (floor, room) → (x, y) pixel on a virtual floor-plan canvas.
    Rooms are arranged in rows (floors) and columns (room slots).
    """
    col = room_idx % _GRID_COLS
    x   = col * _COL_SPACING_PX + _COL_OFFSET_PX

    # Y increases downward; floor 0 is ground floor (bottom of campus map)
    y = (_ROW_OFFSET_PX + (NUM_FLOORS - 1 - floor_idx) * _ROW_SPACING_PX
         + (room_idx // _GRID_COLS) * (_ROW_SPACING_PX // 2))
    return x, y


def _room_name(floor_num: int, room_num: int) -> str:
    """Canonical room asset name, e.g. 'B01-F01-R101', 'B01-F10-R1001'."""
    room_number = floor_num * 100 + room_num
    return f"B01-F{floor_num:02d}-R{room_number}"


def _floor_name(floor_num: int) -> str:
    return f"B01-F{floor_num:02d}"


def _device_name_for_room(floor_num: int, room_num: int) -> str:
    """
    Reconstruct the device name that was generated in Phase 2 for this room.
    Phase 2 naming: 'b01-f01-r101', 'b01-f10-r1001' (all lowercase, no padding on room#).
    """
    room_number = floor_num * 100 + room_num
    return f"b01-f{floor_num:02d}-r{room_number}"


# ─────────────────────────── main builder ────────────────────────────────────

def build_topology() -> CampusAsset:
    """
    Construct the full Campus → Building → Floor → Room hierarchy
    with all room attributes and device relations pre-populated.
    """
    campus = CampusAsset(
        asset_name=CAMPUS_NAME,
        label="Zewail City Main Campus",
    )

    building = BuildingAsset(
        asset_name=BUILDING_NAME,
        label="Building 01",
        parent=CAMPUS_NAME,
    )

    for f_idx in range(NUM_FLOORS):
        floor_num = f_idx + 1
        floor = FloorAsset(
            asset_name=_floor_name(floor_num),
            label=f"Floor {floor_num:02d}",
            parent=BUILDING_NAME,
        )

        for r_idx in range(ROOMS_PER_FLOOR):
            room_num = r_idx + 1
            rtype    = _room_type(f_idx, r_idx)
            sq_ft    = _square_footage(rtype, f_idx, r_idx)
            capacity = _occupant_capacity(sq_ft, rtype)
            cx, cy   = _coordinates(f_idx, r_idx)

            room = RoomAsset(
                asset_name=_room_name(floor_num, room_num),
                label=f"Room {floor_num * 100 + room_num}",
                parent=floor.asset_name,
                attributes=RoomAttributes(
                    square_footage=sq_ft,
                    occupant_capacity=capacity,
                    coordinates_x=cx,
                    coordinates_y=cy,
                    room_type=rtype,
                ),
                devices=[_device_name_for_room(floor_num, room_num)],
            )
            floor.rooms.append(room)

        building.floors.append(floor)

    campus.buildings.append(building)
    return campus


def all_rooms(campus: CampusAsset) -> list[RoomAsset]:
    """Flat list of all 200 room assets for convenience."""
    return [
        room
        for building in campus.buildings
        for floor in building.floors
        for room in floor.rooms
    ]


# ─────────────────────────── quick smoke-test ────────────────────────────────

if __name__ == "__main__":
    topology = build_topology()
    rooms    = all_rooms(topology)

    print(f"Campus  : {topology.asset_name}")
    print(f"Building: {topology.buildings[0].asset_name}")
    print(f"Floors  : {len(topology.buildings[0].floors)}")
    print(f"Rooms   : {len(rooms)}")
    print()
    print("Sample rooms:")
    for r in rooms[:3]:
        a = r.attributes
        print(
            f"  {r.asset_name:20s}  type={a.room_type:12s}  "
            f"sqft={a.square_footage:6.0f}  cap={a.occupant_capacity:3d}  "
            f"x={a.coordinates_x:4d}  y={a.coordinates_y:4d}  "
            f"device={r.devices[0]}"
        )
