# Phase 3 — OTA Security & Integrity System

**Campus Pulse · IoT Infrastructure · Mai's Deliverable**

---

## Overview

This module implements the **Secure Over-The-Air (OTA) update pipeline** for the 200-node campus IoT fleet. It ensures that all remote configuration updates are cryptographically verified, version-tracked, and audit-logged.

### Security Properties

| Property | Mechanism |
|----------|-----------|
| **Integrity** | SHA-256 hash of every payload — receiver recalculates and compares |
| **Authenticity** | Canonical `json.dumps(sort_keys=True)` prevents key-order spoofing |
| **Tamper Detection** | Any hash mismatch triggers a `CRITICAL` security alert with full forensic data |
| **Downgrade Prevention** | Version comparison rejects older configurations |
| **Audit Trail** | All failures persisted to JSON + formatted for ThingsBoard telemetry |

---

## Architecture

```
┌─────────────┐      MQTT: campus/+/+/ota      ┌──────────────────┐
│  Publisher  │ ─── signed JSON payload ─────▶ │    Receiver       │
│  (signs)    │                                │  (verifies)       │
│             │                                │                   │
│  sign_payload()                              │  verify() ─────┐  │
│  broadcast()                                 │                │  │
│  target_floor()                              │  ┌─ SHA-256 ───┤  │
│  target_room()                               │  │  ┌─ Version──┤  │
└─────────────┘                                 │  │  │  └─ Apply─┘  │
                                                │  │  │              │
                                                │  ▼  ▼              │
                                                │  Registry  Audit   │
                                                └──────────────────┘
```

---

## File Map

| File | Purpose |
|------|---------|
| `ota/integrity.py` | SHA-256 signing, verification, canonical serialization |
| `ota/versioning.py` | Fleet version registry, desired vs. reported state tracking |
| `ota/audit_logger.py` | Tamper alert storage, severity classification, ThingsBoard export |
| `ota/mqtt_handler.py` | MQTT wildcard topic subscription & routing (`campus/+/+/ota`) |
| `ota/receiver.py` | End-to-end OTA pipeline (verify → version-check → apply) |
| `ota/publisher.py` | Signed payload creation and dispatch (broadcast/floor/room) |
| `ota/types.py` | Shared dataclasses: `OtaUpdateResult`, `TamperAlert`, `OtaPayload` |
| `test_ota.py` | 50 unit tests — all green ✅ |

---

## Usage

### Signing & Verification (Core)

```python
from phase3.ota.integrity import sign_payload, compute_sha256, OtaIntegrityVerifier

# Sender side — sign a config payload
params = {"alpha": 0.012, "beta": 0.22}
signed = sign_payload(params, version="1.1")
# → {"alpha": 0.012, "beta": 0.22, "version": "1.1", "signature": "a1b2c3..."}

# Receiver side — verify
verifier = OtaIntegrityVerifier()
payload, alert = verifier.verify(
    json.dumps(signed).encode(),
    node_id="b01-f01-r101",
)
if alert:
    print(f"TAMPER DETECTED: {alert.reason}")
else:
    print(f"OK — version={payload.version} params={payload.params}")
```

### Version Registry

```python
from phase3.ota.versioning import FleetVersionRegistry

registry = FleetVersionRegistry()
registry.register("b01-f01-r101", "1.0")
registry.register("b01-f01-r102", "1.0")

# Push desired version
registry.set_desired_version("1.1")
# → All nodes now show update_pending=True

# After a node applies the update
registry.confirm_update("b01-f01-r101", "1.1")

# Dashboard data
rows = registry.to_dashboard_rows()
# → [{"device_name": "b01-f01-r101", "sync_status": "In Sync"}, ...]
```

### Tamper Audit Logger

```python
from phase3.ota.audit_logger import OtaAuditLogger
from phase3.ota.types import TamperAlert, TamperReason

audit = OtaAuditLogger()
alert = TamperAlert(
    node_id="b01-f01-r101",
    reason=TamperReason.HASH_MISMATCH,
    expected_hash="a1b2...",
    received_hash="d4e5...",
    raw_payload=b'{"alpha":0.01,"signature":"bad"}',
    source_ip="192.168.1.42",
)
audit.record(alert)
# → Logs: CRITICAL Security Tampering Alert

# Export for ThingsBoard
tb_rows = audit.get_alerts_for_thingsboard()
# → [{"ts": ..., "values": {"ota_tamper_severity": "CRITICAL", ...}}]
```

### Full Receiver Integration

```python
from phase3.ota.mqtt_handler import OtaMqttHandler
from phase3.ota.receiver import OtaReceiver
from phase3.ota.types import OtaUpdateResult, OtaStatus

mqtt = OtaMqttHandler(broker_host="localhost")
registry = FleetVersionRegistry()
audit = OtaAuditLogger()

# Register nodes
for floor in range(1, 11):
    for room in range(1, 21):
        registry.register(f"b01-f{floor:02d}-r{floor*100+room}", "1.0")

receiver = OtaReceiver(
    mqtt_handler=mqtt,
    version_registry=registry,
    audit_logger=audit,
    node_ids=[n.node_id for n in registry.all_nodes()],
)

# Hook: apply params to your physics engine
def apply_to_engine(node_id, params):
    # e.g., update Room.alpha, Room.beta
    return OtaUpdateResult(node_id=node_id, status=OtaStatus.APPLIED, version="1.1")

receiver.register_apply_hook(apply_to_engine)

await mqtt.connect()
await receiver.start()
await mqtt.start_listening()
```

### Publisher — Dispatch Updates

```python
from phase3.ota.publisher import OtaPublisher

publisher = OtaPublisher(mqtt_handler=mqtt, version_registry=registry)

# Broadcast to ALL 200 rooms
await publisher.broadcast(
    params={"alpha": 0.015, "beta": 0.25},
    version="1.1",
)

# Target a specific floor
await publisher.target_floor(
    floor_num=5,
    params={"alpha": 0.018},
    version="1.2",
)

# Target a single room
await publisher.target_room(
    node_id="b01-f03-r301",
    params={"alpha": 0.011},
    version="1.3",
)
```

---

## MQTT Topic Structure

| Topic | Scope |
|-------|-------|
| `campus/+/+/ota` | Broadcast — all 200 rooms |
| `campus/b01/f05/ota` | Floor-targeted — 20 rooms on floor 5 |
| `campus/b01/f03/ota` | Floor-targeted — 20 rooms on floor 3 |

The `+` wildcard is standard MQTT — every node subscribes to `campus/+/+/ota` and the `OtaTopicTarget` class parses the topic to determine if the update applies to that specific node.

---

## Tamper Alert Severity Levels

| Reason | Severity | Description |
|--------|----------|-------------|
| `HASH_MISMATCH` | CRITICAL | SHA-256 does not match — possible MITM attack |
| `MISSING_SIGNATURE` | CRITICAL | No signature field in payload |
| `MALFORMED_JSON` | HIGH | Payload is not valid JSON |
| `VERSION_DOWNGRADE` | HIGH | Attempt to install older version |
| `UNKNOWN_FIELD` | MEDIUM | Unrecognized parameter key |

---

## Tests

```bash
python -m pytest phase3/test_ota.py -v
# 50 passed
```

### Test Coverage

- SHA-256 determinism & key-order independence
- Valid payload verification
- Tampered payload detection (hash mismatch, missing signature, malformed JSON)
- Version registry CRUD operations
- Desired vs. reported state reconciliation
- Audit log persistence & filtering
- MQTT topic routing (broadcast, floor, room)
- Full receiver pipeline integration
