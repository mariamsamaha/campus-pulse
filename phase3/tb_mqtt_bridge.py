"""
tb_mqtt_bridge.py — HiveMQ → ThingsBoard MQTT telemetry bridge.

Subscribes to campus telemetry on HiveMQ and republishes each device's
telemetry to ThingsBoard using that device's access token as the MQTT username.

ThingsBoard MQTT telemetry format:
  topic:    v1/devices/me/telemetry
  username: <device_access_token>
  payload:  {"temperature": 22.1, "humidity": 50.0, ...}

This runs as a persistent background service.

Usage:
    python3 phase3/tb_mqtt_bridge.py

Environment:
    HIVEMQ_HOST  (default: localhost)
    HIVEMQ_PORT  (default: 1883)
    TB_MQTT_HOST (default: localhost)
    TB_MQTT_PORT (default: 1884)   # ThingsBoard's mapped MQTT port
    TB_URL       (default: http://localhost:9090)
    TB_USER      (default: tenant@thingsboard.org)
    TB_PASS      (default: tenant)
"""

from __future__ import annotations
import json
import logging
import os
import sys
import time
import threading
import requests
import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tb_bridge")

HIVEMQ_HOST  = os.environ.get("HIVEMQ_HOST",  "localhost")
HIVEMQ_PORT  = int(os.environ.get("HIVEMQ_PORT",  "1883"))
TB_MQTT_HOST = os.environ.get("TB_MQTT_HOST", "localhost")
TB_MQTT_PORT = int(os.environ.get("TB_MQTT_PORT", "1884"))
TB_URL       = os.environ.get("TB_URL",       "http://localhost:9090")
TB_USER      = os.environ.get("TB_USER",      "tenant@thingsboard.org")
TB_PASS      = os.environ.get("TB_PASS",      "tenant")

# Cache: device_name -> access_token
_token_cache: dict[str, str] = {}
# Cache: access_token -> paho client (persistent connection to TB)
_tb_clients:  dict[str, mqtt.Client] = {}
_tb_lock = threading.Lock()


def get_tb_token() -> str:
    """Authenticate with TB REST API and return JWT."""
    resp = requests.post(
        f"{TB_URL}/api/auth/login",
        json={"username": TB_USER, "password": TB_PASS},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def fetch_device_token(device_name: str, jwt: str) -> str | None:
    """Fetch the MQTT access token for a device by name."""
    resp = requests.get(
        f"{TB_URL}/api/tenant/devices",
        headers={"X-Authorization": f"Bearer {jwt}"},
        params={"pageSize": 1, "page": 0, "textSearch": device_name},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    for dev in data.get("data", []):
        if dev["name"] == device_name:
            dev_id = dev["id"]["id"]
            cred = requests.get(
                f"{TB_URL}/api/device/{dev_id}/credentials",
                headers={"X-Authorization": f"Bearer {jwt}"},
                timeout=10,
            )
            cred.raise_for_status()
            return cred.json().get("credentialsId")
    return None


def get_tb_client(access_token: str) -> mqtt.Client:
    """Get or create a persistent TB MQTT client for a device token."""
    with _tb_lock:
        if access_token in _tb_clients:
            return _tb_clients[access_token]

        client = mqtt.Client(client_id=f"bridge_{access_token[:8]}")
        client.username_pw_set(access_token)

        def on_connect(c, u, f, rc):
            if rc == 0:
                log.debug("TB client connected for token %s...", access_token[:8])
            else:
                log.warning("TB client connect failed rc=%d for %s...", rc, access_token[:8])

        client.on_connect = on_connect
        client.connect(TB_MQTT_HOST, TB_MQTT_PORT, keepalive=60)
        client.loop_start()
        time.sleep(0.3)  # brief wait for connection
        _tb_clients[access_token] = client
        return client


def normalize_device_name(topic: str) -> str | None:
    """
    Extract device name from topic: campus/b01/f01/r101/telemetry
    Returns: b01-f01-r101
    """
    parts = topic.split("/")
    if len(parts) < 5:
        return None
    # campus / b01 / f01 / r101 / telemetry
    building = parts[1]  # b01
    floor    = parts[2]  # f01
    room     = parts[3]  # r101
    return f"{building}-{floor}-{room}"


def build_tb_payload(raw: dict) -> dict:
    """
    Convert simulation telemetry format to flat ThingsBoard telemetry.
    Input: {"sensors": {"temperature": 22.1, "humidity": 50.0}, "occupancy": True, ...}
    Output: {"temperature": 22.1, "humidity": 50.0, "occupancy": 1, ...}
    """
    payload = {}

    # Handle nested sensors dict (Phase 2 format)
    sensors = raw.get("sensors") or raw.get("data") or {}
    if isinstance(sensors, dict):
        payload.update({k: v for k, v in sensors.items() if v is not None})

    # Flat format (Phase 1 format)
    for key in ("temperature", "humidity", "occupancy", "hvac_state",
                "dimmer_level", "light_level", "fault"):
        if key in raw:
            val = raw[key]
            if isinstance(val, bool):
                val = int(val)
            payload[key] = val

    # Normalize occupancy to int
    if "occupancy" in payload and isinstance(payload["occupancy"], bool):
        payload["occupancy"] = int(payload["occupancy"])

    # Add HVAC state if present under different keys
    if "hvac_mode" in raw:
        payload["hvac_state"] = raw["hvac_mode"]

    return payload


# JWT and token state
_jwt: str | None = None
_jwt_expiry: float = 0.0


def ensure_jwt() -> str:
    global _jwt, _jwt_expiry
    if _jwt is None or time.time() > _jwt_expiry:
        _jwt = get_tb_token()
        _jwt_expiry = time.time() + 3600  # refresh hourly
    return _jwt


def on_hivemq_message(client, userdata, msg):
    """Handle incoming telemetry from HiveMQ and forward to ThingsBoard."""
    topic   = msg.topic
    device_name = normalize_device_name(topic)
    if not device_name:
        return

    # Skip non-telemetry topics
    if not topic.endswith("/telemetry"):
        return

    try:
        raw = json.loads(msg.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        log.debug("Invalid JSON on %s", topic)
        return

    tb_payload = build_tb_payload(raw)
    if not tb_payload:
        return

    # Get device access token (cached)
    if device_name not in _token_cache:
        try:
            jwt = ensure_jwt()
            token = fetch_device_token(device_name, jwt)
            if not token:
                log.warning("No token found for %s", device_name)
                return
            _token_cache[device_name] = token
            log.info("Cached token for %s: %s...", device_name, token[:8])
        except Exception as exc:
            log.error("Token fetch failed for %s: %s", device_name, exc)
            return

    access_token = _token_cache[device_name]

    # Publish to ThingsBoard
    try:
        tb_client = get_tb_client(access_token)
        result = tb_client.publish(
            "v1/devices/me/telemetry",
            json.dumps(tb_payload),
            qos=1,
        )
        if result.rc == 0:
            log.debug("→ TB: %s  %s", device_name, tb_payload)
        else:
            log.warning("TB publish failed rc=%d for %s", result.rc, device_name)
    except Exception as exc:
        log.error("TB publish error for %s: %s", device_name, exc)


def run_bridge():
    """Main bridge loop — subscribe to HiveMQ, forward to ThingsBoard."""
    log.info("=== ThingsBoard MQTT Bridge Starting ===")
    log.info("HiveMQ:      %s:%d", HIVEMQ_HOST, HIVEMQ_PORT)
    log.info("ThingsBoard: %s:%d", TB_MQTT_HOST, TB_MQTT_PORT)

    # Pre-fetch JWT
    try:
        ensure_jwt()
        log.info("ThingsBoard REST auth: OK")
    except Exception as exc:
        log.error("ThingsBoard auth failed: %s", exc)
        sys.exit(1)

    hive_client = mqtt.Client(client_id="tb-bridge-subscriber")

    def on_connect(c, u, f, rc):
        if rc == 0:
            log.info("Connected to HiveMQ (%s:%d)", HIVEMQ_HOST, HIVEMQ_PORT)
            # Subscribe to all floor telemetry
            c.subscribe("campus/b01/+/+/telemetry", qos=1)
            log.info("Subscribed to campus/b01/+/+/telemetry")
        else:
            log.error("HiveMQ connect failed rc=%d", rc)

    def on_disconnect(c, u, rc):
        log.warning("Disconnected from HiveMQ (rc=%d) — will auto-reconnect", rc)

    hive_client.on_connect    = on_connect
    hive_client.on_message    = on_hivemq_message
    hive_client.on_disconnect = on_disconnect

    hive_client.connect(HIVEMQ_HOST, HIVEMQ_PORT, keepalive=60)
    log.info("Bridge running — forwarding HiveMQ → ThingsBoard")
    hive_client.loop_forever()


if __name__ == "__main__":
    run_bridge()
