## Hybrid World Engine (MQTT + CoAP)

## Project Structure

```
campus_pulse_phase2/
├── main_phase2.py         
├── world_engine.py        
│   ├── __init__.py
│   ├── mqtt_node.py        # MQTTNode class
│   └── coap_node.py        # CoAPNode class
├── telemetry_schema.py     # Unified JSON schema (v2.0.0)
├── dedup.py                # DUP flag / CON deduplication
├── generate_registry.py    # ThingsBoard CSV export
└── requirements_phase2.txt
```

---

## Room Assignment

```
Building b01  ·  10 floors  ·  20 rooms/floor  ·  200 total

Floor XX:  Rooms XX01–XX10  →  MQTT  (100 nodes total)
           Rooms XX11–XX20  →  CoAP  (100 nodes total)

Example — Floor 01:
  b01-f01-r101  … b01-f01-r110  → MQTT  → topic: campus/b01/f01/r10X/telemetry
  b01-f01-r111  … b01-f01-r120  → CoAP  → coap://127.0.0.1:5683-5692/f01/r11X/telemetry
```

---

## MQTT Node Details (`nodes/mqtt_node.py`)

### Connection
- **Client ID**: `campus-mqtt-b01-f01-r101` (unique per node)
- **Broker**: HiveMQ CE at `localhost:1883`
- **Keep-alive**: 60 s
- **Reconnect**: automatic retry every 5 s

### Last Will & Testament
```json
Topic:   campus/b01/f01/r101/status   (retain=true, QoS=1)
Payload: {"schema_version":"2.0.0","sensor_id":"b01-f01-r101",
          "status":"offline","timestamp":1712345678}
```
ThingsBoard sees `status=offline` if the TCP connection drops.

### Topics
| Topic | Direction | QoS | Description |
|---|---|---|---|
| `campus/b01/f##/r###/telemetry` | ↑ upstream | 1 | Physics-driven telemetry every 5 s |
| `campus/b01/f##/r###/status` | ↑ upstream | 1 retain | Online/Offline heartbeat |
| `campus/b01/f##/r###/heartbeat` | ↑ upstream | 1 retain | Periodic health pulse |
| `campus/b01/f##/r###/cmd` | ↓ downstream | 2 (Exactly Once) | Actuator commands |
| `campus/b01/f##/r###/ack` | ↑ upstream | 1 | Command acknowledgement |

### Command Format
```json
{ "action": "SET_HVAC",   "value": "ON"   }
{ "action": "SET_HVAC",   "value": "OFF"  }
{ "action": "SET_HVAC",   "value": "ECO"  }
{ "action": "SET_TEMP",   "value": 22.5   }
{ "action": "SET_OCC",    "value": true   }
{ "action": "EMERGENCY_LOCKOUT"           }
```

---

## CoAP Node Details (`nodes/coap_node.py`)

### Port Assignment
```
port = 5683 + (floor - 1) * 10 + (room_num - 11)

Floor 1, Room 11  → port 5683
Floor 1, Room 20  → port 5692
Floor 10, Room 20 → port 5782
```

### Resources
| URI | Method | Description |
|---|---|---|
| `/f##/r###/telemetry` | GET (Observable) | Physics telemetry, pushed on every tick |
| `/f##/r###/actuators/hvac` | PUT | HVAC command from Gateway |
| `/f##/r###/status` | GET | Health probe |

### RFC 7641 Observe Flow
```
Gateway                    CoAP Node
   |                           |
   |── GET /telemetry ─────────▶|  Register observation
   |◀─────────── 2.05 Content ──|  Initial response
   |                           |
   |                       [physics tick]
   |◀────── 2.05 Notification ──|  Pushed automatically
   |◀────── 2.05 Notification ──|  Pushed automatically
   ...
```

### CoAP PUT Command (CON reliability)
```
Gateway                    CoAP Node
   |── CON PUT /actuators/hvac ▶|  Confirmable request
   |◀─────────── ACK 2.04 ──────|  Acknowledgement (stops retransmit)
   |◀─────────── ACK payload ───|  {"ack":true, "new_state":"ON", ...}
```
If the gateway doesn't receive ACK within the CoAP retransmit timeout, it
retransmits the CON. The node's dedup layer detects the duplicate via
content-hash and returns `2.03 Valid` without re-applying the command.

---

## Telemetry Schema v2.0.0

All 200 nodes emit the same JSON structure, regardless of transport:

```json
{
  "schema_version": "2.0.0",
  "metadata": {
    "sensor_id": "b01-f01-r101",
    "building":  "b01",
    "floor":     1,
    "room":      101,
    "protocol":  "MQTT",
    "timestamp": 1712345678,
    "ts_ms":     1712345678123
  },
  "sensors": {
    "temperature": 23.45,
    "humidity":    55.2,
    "occupancy":   true,
    "light_level": 450
  },
  "actuators": {
    "hvac_mode":       "ON",
    "lighting_dimmer": 45
  },
  "content_hash": "a1b2c3d4e5f6a7b8"
}
```

The `content_hash` field is a SHA-256 hash of the sensor readings (excluding
timestamps), used by the deduplication layer to identify byte-identical
retransmits.

---

## DUP Flag / Deduplication (`dedup.py`)

### MQTT DUP
When a broker re-sends a QoS 1 packet (no PUBACK received), it sets the
DUP flag. The `DedupHandler` tracks `(client_id, packet_id)` pairs in a
30-second TTL cache. A matching entry → message is silently dropped.

### CoAP CON Retransmit
aiocoap handles ACK at the transport layer, but app-level retransmits
can still occur. The `DedupHandler` tracks `(node_id, content_hash)` pairs.
Matching entry → returns `2.03 Valid` without re-applying the command.

---

## Installation & Run

```bash
# 1. Install deps
pip install -r campus_pulse_phase2/requirements_phase2.txt

# 2. Start HiveMQ (Docker)
docker run -d -p 1883:1883 hivemq/hivemq-ce

# 3. Run Phase 2 engine (from repo root)
cd campus_pulse_phase2
python main_phase2.py
```

---

## Generate ThingsBoard Registry

```bash
cd campus_pulse_phase2
python generate_registry.py
# → tb_devices.csv   (200 device rows)
# → tb_assets.csv    (Campus/Building/Floor/Room hierarchy)
```

Import `tb_devices.csv` in ThingsBoard: **Devices → Import Devices**.