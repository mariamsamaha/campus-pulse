from __future__ import annotations
import hashlib
import json
import time
from typing import Any, Literal

SCHEMA_VERSION = "2.0.0"

NodeProtocol = Literal["MQTT", "CoAP"]

def build_telemetry(
    *,
    sensor_id: str,
    building: str,
    floor: int,
    room: int,
    protocol: NodeProtocol,
    temperature: float,
    humidity: float,
    occupancy: bool,
    light_level: int,
    hvac_mode: str,
    lighting_dimmer: int,
    fault: dict | None = None,
) -> dict:

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "metadata": {
            "sensor_id": sensor_id,
            "building":  building,
            "floor":     floor,
            "room":      room,
            "protocol":  protocol,        # "MQTT" or "CoAP"
            "timestamp": int(time.time()),
            "ts_ms":     int(time.time() * 1000),   
        },
        "sensors": {
            "temperature": round(temperature, 2),
            "humidity":    round(humidity, 2),
            "occupancy":   occupancy,
            "light_level": light_level,
        },
        "actuators": {
            "hvac_mode":       hvac_mode,
            "lighting_dimmer": lighting_dimmer,
        },
    }

    if fault:
        payload["fault"] = fault
    payload["content_hash"] = _content_hash(payload)
    return payload


def build_lwt_payload(sensor_id: str) -> str:
    """JSON string for the MQTT Last Will and Testament message."""
    return json.dumps({
        "schema_version": SCHEMA_VERSION,
        "sensor_id":  sensor_id,
        "status":     "offline",
        "timestamp":  int(time.time()),
    })


def build_online_payload(sensor_id: str) -> dict:
    """Published once on connect to confirm the node is live."""
    return {
        "schema_version": SCHEMA_VERSION,
        "sensor_id":  sensor_id,
        "status":     "online",
        "timestamp":  int(time.time()),
    }


def build_command_ack(sensor_id: str, command: str, new_state: Any) -> dict:
    """Acknowledgement payload sent after a successful actuator command."""
    return {
        "schema_version": SCHEMA_VERSION,
        "sensor_id":  sensor_id,
        "ack":        True,
        "command":    command,
        "new_state":  new_state,
        "timestamp":  int(time.time()),
        "ts_ms":      int(time.time() * 1000),
    }

def _content_hash(payload: dict) -> str:
    """
    SHA-256 of the sensor readings (excluding timestamp & hash itself).
    Two packets with identical readings produce the same hash, letting the
    dedup layer flag byte-identical retransmits even if timestamps differ.
    """
    stable = {
        "sensor_id":       payload["metadata"]["sensor_id"],
        "temperature":     payload["sensors"]["temperature"],
        "humidity":        payload["sensors"]["humidity"],
        "occupancy":       payload["sensors"]["occupancy"],
        "light_level":     payload["sensors"]["light_level"],
        "hvac_mode":       payload["actuators"]["hvac_mode"],
        "lighting_dimmer": payload["actuators"]["lighting_dimmer"],
    }
    raw = json.dumps(stable, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()[:16]
