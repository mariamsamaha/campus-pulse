# Phase 3 — Dashboards & Rule Chains: Import Guide

**Campus Pulse · ThingsBoard Configuration**

---

## Overview of Deliverables

```
phase3/
├── dashboards/
│   ├── campus_main_dashboard.json     ← Main: heatmap + sync status + KPIs
│   └── room_control_popup.json        ← Room state: HVAC + dimmer controls
├── rule_chains/
│   ├── floor_aggregation_rule_chain.json  ← Floor avg temp aggregation
│   └── shadow_sync_rule_chain.json        ← Desired vs Reported conflict detection
├── shadow_provisioner.py              ← Seeds SHARED/CLIENT shadow attributes
└── aggregate_floor_telemetry.py       ← Server-side floor aggregation poller (CE fix)
```

---

## Step-by-Step Import Order

> ⚠️ **Run in this exact order** — later steps depend on earlier ones.

### Step 1 — Run Hierarchy Provisioner (if not already done)

```bash
python phase3/provision_hierarchy.py --url http://localhost:9090
```

Verify with:
```bash
python phase3/verify_provisioning.py --url http://localhost:9090
```

---

### Step 2 — Seed Shadow State

```bash
python phase3/shadow_provisioner.py --url http://localhost:9090
```

This seeds `desired_hvac`, `desired_dimmer` (SHARED) and `reported_hvac`, `reported_dimmer`, `sync_status`, `last_seen` (CLIENT) on all 200 devices.

---

### Step 3 — Import Rule Chains

#### 3a. Floor Aggregation Rule Chain
1. ThingsBoard UI → **Rule Chains** → **⊕ Import Rule Chain**
2. Upload: `phase3/rule_chains/floor_aggregation_rule_chain.json`
3. Open the imported rule chain and **Save** to activate
4. Assign this rule chain to the **Room** device profile:
   - Go to **Device Profiles** → `room-sensor` → Edit → **Rule Chain** dropdown → select `Floor Average Temperature Aggregation`

#### 3b. Shadow Sync Rule Chain
1. ThingsBoard UI → **Rule Chains** → **⊕ Import Rule Chain**
2. Upload: `phase3/rule_chains/shadow_sync_rule_chain.json`
3. Open the **Root Rule Chain** → drag in a **Rule Chain** node
4. Connect `POST_ATTRIBUTES_REQUEST` messages to this chain

---

### Step 4 — Start Floor Aggregation Poller

> Required for ThingsBoard CE (Community Edition), which lacks a native multi-device aggregation node.

```bash
# Run as a background service
python phase3/aggregate_floor_telemetry.py \
  --url http://localhost:9090 \
  --interval 30

# Or run once to test
python phase3/aggregate_floor_telemetry.py --once
```

This writes `avg_temperature`, `avg_humidity`, `total_occupancy`, and `reporting_devices` as Floor asset timeseries.

---

### Step 5 — Import Dashboards

#### 5a. Main Campus Dashboard
1. ThingsBoard UI → **Dashboards** → **⊕ Import Dashboard**
2. Upload: `phase3/dashboards/campus_main_dashboard.json`
3. After import, **update entity aliases** with real asset IDs:
   - Go to **Entity Aliases** in the dashboard editor
   - `floor-f01-alias` → select asset `B01-F01`
   - `floor-f02-alias` → select asset `B01-F02`
   - `floor-f03-alias` → select asset `B01-F03`
   - Repeat for all 10 floors

#### 5b. (Optional) Room Control as Standalone Dashboard
1. Upload: `phase3/dashboards/room_control_popup.json`
2. The main dashboard already has the room control state embedded — this standalone import is only needed if you want a separate URL.

---

## Widget Configuration Details

### Image Map — Floor Heatmap

| Setting | Value |
|---------|-------|
| Widget type | `system.maps.image-map` |
| Data source | Entity alias: `floor-rooms-alias` (relations query: Floor → Devices) |
| Color function | Blue (16°C) → Cyan → Green (22°C) → Yellow (26°C) → Red (32°C) |
| Tooltip | Occupancy, HVAC state, temperature, last update timestamp |
| Polygon click action | Opens `room-control-state` dashboard state |
| Refresh interval | 5 seconds (real-time) |

**To configure room polygons:**
1. In the image map widget settings, open **Polygon settings**
2. Each polygon represents one room
3. Use `coordinates_x` and `coordinates_y` SERVER attributes as the center point
4. Bounding box per room: **110px wide × 70px tall** (from topology grid constants)

```
Polygon for room R = {
  center: (coordinates_x, coordinates_y),
  corners: [
    (x - 55, y - 35),
    (x + 55, y - 35),
    (x + 55, y + 35),
    (x - 55, y + 35)
  ]
}
```

---

### Sync Status Table — Required Columns

| Column | Source | Scope |
|--------|--------|-------|
| Device Name | `entityName` field | — |
| Last Seen | `last_seen` attribute | CLIENT_SCOPE |
| Desired HVAC | `desired_hvac` attribute | SHARED_SCOPE |
| Reported HVAC | `reported_hvac` attribute | CLIENT_SCOPE |
| Desired Dimmer | `desired_dimmer` attribute | SHARED_SCOPE |
| Reported Dimmer | `reported_dimmer` attribute | CLIENT_SCOPE |
| Sync Status | `sync_status` attribute | CLIENT_SCOPE |

**Row highlighting:**
- 🔴 `OUT_OF_SYNC` → red background
- 🟠 `PENDING` → orange background
- ✅ `SYNCED` → no highlight

**Filter for unsynced only:** Use the search box and filter expression:
```
sync_status != 'SYNCED'
```

---

### RPC Command Payloads

| Action | Method | Params |
|--------|--------|--------|
| Turn HVAC ON | `setHvac` | `"ON"` |
| Turn HVAC OFF | `setHvac` | `"OFF"` |
| Set dimmer | `setDimmer` | `0` – `100` (integer) |

Full payload format:
```json
{"method": "setHvac",   "params": "ON"}
{"method": "setDimmer", "params": 75}
```

---

## Shadow State Model

```
Device (b01-f01-r101)
  ├── SHARED_SCOPE (Desired — set by operator/dashboard)
  │   ├── desired_hvac:    "ON" | "OFF"
  │   └── desired_dimmer:  0-100
  │
  └── CLIENT_SCOPE (Reported — set by device + rule chain)
      ├── reported_hvac:   "ON" | "OFF"
      ├── reported_dimmer: 0-100
      ├── last_seen:       epoch ms
      └── sync_status:     "SYNCED" | "OUT_OF_SYNC" | "PENDING"
```

---

## Architecture Notes

### Why `aggregate_floor_telemetry.py`?

ThingsBoard **Community Edition** (CE) does not include the `Aggregate Latest` node — that is a Professional Edition (PE) feature. The Python poller is the production-grade CE alternative that:
1. Reads latest telemetry from all 20 devices per floor via REST API
2. Computes true server-side average
3. Writes `avg_temperature` to the Floor ASSET timeseries
4. Dashboards bind to Floor ASSET telemetry (not device)

This keeps aggregation **server-side** (not client-side browser calculation).

### ThingsBoard URL

The infrastructure uses port **9090** (not 8080, which is occupied by HiveMQ):
```
TB_URL=http://localhost:9090
```
