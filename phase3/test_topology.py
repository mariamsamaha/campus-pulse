"""
test_topology.py — Phase 3: Unit tests for the deterministic asset topology.

Coverage:
    • Structural counts (rooms, floors, buildings)
    • Naming conventions
    • Room attribute value ranges and types
    • Room-type distribution
    • Coordinate bounds (inside 1200 × 800 virtual canvas)
    • Device linkage (1 device per room, correct naming pattern)

Run:
    python -m pytest phase3/test_topology.py -v
"""

from __future__ import annotations

import re
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from topology import (
    build_topology,
    all_rooms,
    CAMPUS_NAME,
    BUILDING_NAME,
    NUM_FLOORS,
    ROOMS_PER_FLOOR,
    ROOM_TYPE_CYCLE,
)

# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def campus():
    return build_topology()


@pytest.fixture(scope="module")
def rooms(campus):
    return all_rooms(campus)


# ── structural integrity ──────────────────────────────────────────────────────

class TestStructure:

    def test_campus_name(self, campus):
        assert campus.asset_name == CAMPUS_NAME

    def test_exactly_one_building(self, campus):
        assert len(campus.buildings) == 1

    def test_building_name(self, campus):
        assert campus.buildings[0].asset_name == BUILDING_NAME

    def test_floor_count(self, campus):
        assert len(campus.buildings[0].floors) == NUM_FLOORS

    def test_rooms_per_floor(self, campus):
        for floor in campus.buildings[0].floors:
            assert len(floor.rooms) == ROOMS_PER_FLOOR, (
                f"Floor {floor.asset_name} has {len(floor.rooms)} rooms, expected {ROOMS_PER_FLOOR}"
            )

    def test_total_room_count(self, rooms):
        assert len(rooms) == NUM_FLOORS * ROOMS_PER_FLOOR  # 200

    def test_unique_room_names(self, rooms):
        names = [r.asset_name for r in rooms]
        assert len(names) == len(set(names)), "Duplicate room asset names found"


# ── naming conventions ────────────────────────────────────────────────────────

class TestNaming:

    _ROOM_RE  = re.compile(r"^B01-F\d{2}-R\d{3,4}$")
    _FLOOR_RE = re.compile(r"^B01-F\d{2}$")
    _DEV_RE   = re.compile(r"^b01-f\d{2}-r\d{3,4}$")

    def test_room_name_pattern(self, rooms):
        for r in rooms:
            assert self._ROOM_RE.match(r.asset_name), (
                f"Room name '{r.asset_name}' does not match expected pattern"
            )

    def test_parent_floor_pattern(self, rooms):
        for r in rooms:
            assert self._FLOOR_RE.match(r.parent), (
                f"Room {r.asset_name} has invalid parent '{r.parent}'"
            )

    def test_room_parent_consistency(self, campus):
        for building in campus.buildings:
            for floor in building.floors:
                for room in floor.rooms:
                    assert room.parent == floor.asset_name

    def test_device_name_pattern(self, rooms):
        for r in rooms:
            for d in r.devices:
                assert self._DEV_RE.match(d), (
                    f"Device name '{d}' does not match expected lowercase pattern"
                )


# ── room attributes ───────────────────────────────────────────────────────────

class TestAttributes:

    def test_square_footage_positive(self, rooms):
        for r in rooms:
            assert r.attributes.square_footage > 0

    def test_occupant_capacity_positive(self, rooms):
        for r in rooms:
            assert r.attributes.occupant_capacity > 0

    def test_room_type_in_cycle(self, rooms):
        for r in rooms:
            assert r.attributes.room_type in ROOM_TYPE_CYCLE, (
                f"Unknown room_type '{r.attributes.room_type}' in {r.asset_name}"
            )

    def test_all_room_types_present(self, rooms):
        present = {r.attributes.room_type for r in rooms}
        assert present == set(ROOM_TYPE_CYCLE)

    def test_coordinates_x_in_bounds(self, rooms):
        for r in rooms:
            assert 0 <= r.attributes.coordinates_x <= 1200, (
                f"{r.asset_name}: coordinates_x={r.attributes.coordinates_x} out of range"
            )

    def test_coordinates_y_in_bounds(self, rooms):
        for r in rooms:
            assert 0 <= r.attributes.coordinates_y <= 800, (
                f"{r.asset_name}: coordinates_y={r.attributes.coordinates_y} out of range"
            )

    def test_attribute_types(self, rooms):
        for r in rooms:
            a = r.attributes
            assert isinstance(a.square_footage,    float), f"{r.asset_name}: sq_ft must be float"
            assert isinstance(a.occupant_capacity, int),   f"{r.asset_name}: capacity must be int"
            assert isinstance(a.coordinates_x,     int),   f"{r.asset_name}: x must be int"
            assert isinstance(a.coordinates_y,     int),   f"{r.asset_name}: y must be int"
            assert isinstance(a.room_type,         str),   f"{r.asset_name}: room_type must be str"


# ── device linkage ────────────────────────────────────────────────────────────

class TestDeviceLinkage:

    def test_exactly_one_device_per_room(self, rooms):
        for r in rooms:
            assert len(r.devices) == 1, (
                f"{r.asset_name} has {len(r.devices)} devices, expected 1"
            )

    def test_device_name_matches_room(self, rooms):
        """Device name must be the lowercase equivalent of the room name."""
        for r in rooms:
            expected_device = r.asset_name.lower()
            assert r.devices[0] == expected_device, (
                f"{r.asset_name}: device '{r.devices[0]}' != expected '{expected_device}'"
            )

    def test_unique_device_names(self, rooms):
        devices = [d for r in rooms for d in r.devices]
        assert len(devices) == len(set(devices)), "Duplicate device names found"


# ── room type distribution ────────────────────────────────────────────────────

class TestDistribution:

    def test_each_type_appears_exactly_40_times(self, rooms):
        """With 200 rooms and a 5-type cycle, each type should appear 40×."""
        from collections import Counter
        counts = Counter(r.attributes.room_type for r in rooms)
        for rtype, count in counts.items():
            assert count == 40, f"room_type '{rtype}' appears {count} times, expected 40"
