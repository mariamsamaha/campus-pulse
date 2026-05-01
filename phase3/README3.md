# Phase 3 — Hierarchical Digital Asset Mapping

**Campus Pulse · IoT Infrastructure · Phase 3**

---

## Overview

Phase 3 transitions the flat device registry from Phase 2 into a strict
**1:1 hierarchical digital mapping** of the physical campus:

```
ZC-Main-Campus
└── B01  (Building 01)
    ├── B01-F01  (Floor 01)
    │   ├── B01-F01-R101  ← Contains → b01-f01-r101  (device)
    │   ├── B01-F01-R102  ← Contains → b01-f01-r102
    │   └── …  (20 rooms per floor)
    ├── B01-F02  …
    └── … (10 floors total)
```

**Total assets created:** 212 (1 campus + 1 building + 10 floors + 200 rooms)  
**Total device relations:** 200 (1 device ↔ 1 room)

---

## File Map

| File | Purpose |
|------|---------|
| `topology.py` | Core data model — deterministic Campus→Building→Floor→Room generator |
| `generate_assets_csv.py` | Exports `tb_assets_phase3.csv` for TB bulk import |
| `generate_relations_csv.py` | Exports relation CSVs for hierarchy + device links |
| `provision_hierarchy.py` | Live ThingsBoard REST provisioner (idempotent) |
| `verify_provisioning.py` | Post-provisioning spot-check verifier |
| `test_topology.py` | 22 unit tests — all green ✅ |

### Generated Outputs

| File | Rows | Description |
|------|------|-------------|
| `tb_assets_phase3.csv` | 212 | All hierarchy assets with room metadata columns |
| `tb_relations_phase3.csv` | 211 | Campus→Building→Floor→Room "Contains" relations |
| `tb_device_relations_phase3.csv` | 200 | Room→Device "Contains" relations |

---

## Room Asset Topology

### Naming Convention

| Level | Pattern | Example |
|-------|---------|---------|
| Campus | `ZC-Main-Campus` | `ZC-Main-Campus` |
| Building | `B01` | `B01` |
| Floor | `B01-F{floor:02d}` | `B01-F03` |
| Room | `B01-F{floor:02d}-R{floor×100+room}` | `B01-F03-R301` |
| Device (Phase 2) | `b01-f{floor:02d}-r{floor×100+room}` | `b01-f03-r301` |

### Server-Side Attributes (Room)

All 200 room assets are provisioned with the following static metadata:

| Attribute | Type | Description |
|-----------|------|-------------|
| `square_footage` | `float` | Room area in ft² |
| `occupant_capacity` | `int` | Max persons allowed |
| `coordinates_x` | `int` | Pixel X on virtual floor-plan (0–1200) |
| `coordinates_y` | `int` | Pixel Y on virtual floor-plan (0–800) |
| `room_type` | `string` | `lab`, `office`, `lecture_hall`, `server_room`, `storage` |

#### Attribute Derivation (deterministic, no RNG)

```
room_type       = ROOM_TYPE_CYCLE[(floor_idx + room_idx) % 5]

square_footage  = base_sqft[room_type] + ((floor_idx × 20 + room_idx) % 9) × 5

occupant_capacity = max(square_footage // 15, min_capacity[room_type])

coordinates_x   = (room_idx % 10) × 110 + 60
coordinates_y   = 40 + (9 - floor_idx) × 70 + (room_idx // 10) × 35
```

#### Room Type Distribution (200 rooms)

| Type | Count | Base ft² | Min Capacity |
|------|-------|----------|--------------|
| `lab` | 40 | 550 | 10 |
| `office` | 40 | 300 | 4 |
| `lecture_hall` | 40 | 900 | 30 |
| `server_room` | 40 | 200 | 2 |
| `storage` | 40 | 150 | 1 |

---

## Usage

### 1 — Generate CSV exports (offline, no TB needed)

```bash
cd /path/to/campus-pulse

python phase3/generate_assets_csv.py
# → phase3/tb_assets_phase3.csv

python phase3/generate_relations_csv.py
# → phase3/tb_relations_phase3.csv
# → phase3/tb_device_relations_phase3.csv
```

### 2 — Live provisioning against ThingsBoard

```bash
# Defaults: http://localhost:8080  /  tenant@thingsboard.org  /  tenant
python phase3/provision_hierarchy.py

# Dry-run (prints all API calls, executes nothing)
python phase3/provision_hierarchy.py --dry-run

# Custom target
python phase3/provision_hierarchy.py \
    --url http://myhost:8080 \
    --user admin@myorg.com \
    --pass secret
```

The provisioner is **idempotent** — running it twice will not create duplicates
(it checks for existing assets by name before creating).

### 3 — Post-provisioning verification

```bash
python phase3/verify_provisioning.py
# exit 0 → all checks passed
# exit 1 → failures printed to stdout
```

### 4 — Run unit tests

```bash
python -m pytest phase3/test_topology.py -v
# 22 passed in 0.03s
```

---

## Asset Relations (ThingsBoard)

All relations use **type = `Contains`**, **typeGroup = `COMMON`**:

```
ASSET(Campus)    →Contains→  ASSET(Building)
ASSET(Building)  →Contains→  ASSET(Floor)
ASSET(Floor)     →Contains→  ASSET(Room)
ASSET(Room)      →Contains→  DEVICE
```

---

## Integration with Phase 2

Phase 3 builds directly on Phase 2's device registry (`tb_devices.csv`).
Every device `b01-fXX-rNNN` from Phase 2 maps 1:1 to Room `B01-FXX-RNNN`
via a "Contains" relation — no device modifications are required.
