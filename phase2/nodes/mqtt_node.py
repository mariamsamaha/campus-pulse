from __future__ import annotations
import asyncio
import json
import logging
import random
import time
import gmqtt
from gmqtt import Client as MQTTClient
from dedup import DedupHandler
from telemetry_schema import (
    build_command_ack,
    build_lwt_payload,
    build_online_payload,
    build_telemetry,
)

logger = logging.getLogger("mqtt_node")

TICK_INTERVAL_S: float = 5.0     
MAX_JITTER_S:    float = 5.0     # startup stagger to avoid thundering herd
HEARTBEAT_EVERY: int   = 12      

class MQTTNode:
    """
    A single async MQTT room node.

    Instantiate one per room; run all 100 via asyncio.gather() in world_engine.
    The `room` argument is the Phase 1 Room object (physics engine).
    """

    def __init__(
        self,
        room,                         
        broker_host: str,
        broker_port: int,
        dedup: DedupHandler,
        sim_start: float,
    ):
        self.room        = room
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.dedup       = dedup
        self.sim_start   = sim_start

        self.node_id   = room.id                        
        self.client_id = f"campus-mqtt-{self.node_id}"  # unique per broker session

        self.topic_telemetry = f"{room.mqtt_path}/telemetry"
        self.topic_status    = f"{room.mqtt_path}/status"
        self.topic_heartbeat = f"{room.mqtt_path}/heartbeat"
        self.topic_cmd       = f"{room.mqtt_path}/cmd"
        self.topic_ack       = f"{room.mqtt_path}/ack"

        self.connected = False
        self._tick     = 0
        self._client: MQTTClient | None = None

    def _on_connect(self, client, flags, rc, properties):
        logger.info("[%s] Connected to HiveMQ (rc=%d)", self.node_id, rc)
        self.connected = True
        client.subscribe(self.topic_cmd, qos=2)
        logger.debug("[%s] Subscribed to %s (QoS 2)", self.node_id, self.topic_cmd)

    def _on_disconnect(self, client, packet, exc=None):
        self.connected = False
        logger.warning("[%s] Disconnected (exc=%s)", self.node_id, exc)

    def _on_message(self, client, topic, payload_bytes, qos, properties):
        """
        Handles incoming commands (downstream / southbound).

        DUP-flag check:
          gmqtt passes the raw MQTT packet_id via properties["message_expiry_interval"]
          or via the gmqtt internal _pid attribute on QoS 1/2 messages.
          We use a fallback approach: track (client_id, hash-of-payload).
        """
        try:
            packet_id: int | None = properties.get("message_expiry_interval", None)
            payload_hash = hash(payload_bytes) & 0xFFFF

            if self.dedup.is_mqtt_duplicate(self.client_id, packet_id or payload_hash):
                logger.debug(
                    "[%s] CMD dropped — DUP flag detected (pkt_id=%s)",
                    self.node_id, packet_id,
                )
                return
            cmd = json.loads(payload_bytes.decode())
            logger.info("[%s] CMD received: %s", self.node_id, cmd)
            self._dispatch_command(client, cmd)

        except json.JSONDecodeError:
            logger.error("[%s] CMD parse error — not valid JSON", self.node_id)
        except Exception as exc:
            logger.error("[%s] CMD handler error: %s", self.node_id, exc, exc_info=True)

    def _on_subscribe(self, client, mid, qos, properties):
        logger.debug("[%s] Subscription confirmed (mid=%s, qos=%s)", self.node_id, mid, qos)

    def _dispatch_command(self, client, cmd: dict) -> None:
        """
        Interprets a southbound command and applies it to the virtual actuator.

        Supported commands
        ──────────────────
        { "action": "SET_HVAC",   "value": "ON"|"OFF"|"ECO" }
        { "action": "SET_TEMP",   "value": 22.5 }
        { "action": "SET_OCC",    "value": true|false }
        { "action": "EMERGENCY_LOCKOUT" }
        """
        action = cmd.get("action", "").upper()
        value  = cmd.get("value")

        if action == "SET_HVAC":
            old = self.room.hvac_mode
            self.room.set_hvac(str(value).upper())
            logger.info(
                "[%s] ⚡ Virtual Actuator — HVAC %s → %s",
                self.node_id, old, self.room.hvac_mode,
            )
            self._publish_ack(client, action, self.room.hvac_mode)

        elif action == "SET_TEMP":
            self.room.set_target_temp(float(value))
            logger.info("[%s] ⚡ Virtual Actuator — Target temp → %.1f", self.node_id, float(value))
            self._publish_ack(client, action, self.room.target_temp)

        elif action == "SET_OCC":
            self.room.set_occupancy(bool(value))
            logger.info("[%s] ⚡ Virtual Actuator — Occupancy → %s", self.node_id, bool(value))
            self._publish_ack(client, action, self.room.occupancy)

        elif action == "EMERGENCY_LOCKOUT":
            self.room.set_hvac("OFF")
            self.room.set_occupancy(False)
            self.room.light_level = 0
            logger.warning("[%s] EMERGENCY LOCKOUT applied", self.node_id)
            self._publish_ack(client, action, "LOCKOUT_ACTIVE")

        else:
            logger.warning("[%s] Unknown command action: '%s'", self.node_id, action)

    def _publish_ack(self, client: MQTTClient, command: str, new_state) -> None:
        """Publish a command acknowledgement (northbound confirmation)."""
        ack = build_command_ack(self.node_id, command, new_state)
        client.publish(
            self.topic_ack,
            json.dumps(ack),
            qos=1,
            retain=False,
        )

    def _build_client(self) -> MQTTClient:
        """Construct a gmqtt Client with LWT pre-configured."""
        will_msg = gmqtt.Message(
            topic=self.topic_status,
            payload=build_lwt_payload(self.node_id),
            qos=1,
            retain=True,
        )
        client = MQTTClient(
            client_id=self.client_id,
            will_message=will_msg,
        )
        client.on_connect    = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message    = self._on_message
        client.on_subscribe  = self._on_subscribe
        return client

    def _publish_telemetry(self) -> None:
        if not self.connected:
            logger.debug("[%s] Skipping telemetry — not connected", self.node_id)
            return

        payload = build_telemetry(
            sensor_id       = self.node_id,
            building        = self.room.building_id,
            floor           = int(self.room.floor_id[1:]),
            room            = int(self.room.room_id_num[1:]),
            protocol        = "MQTT",
            temperature     = self.room.temp,
            humidity        = self.room.humidity,
            occupancy       = self.room.occupancy,
            light_level     = self.room.light_level,
            hvac_mode       = self.room.hvac_mode,
            lighting_dimmer = self.room.lighting_dimmer,
        )
        self._client.publish(
            self.topic_telemetry,
            json.dumps(payload),
            qos=1,
            retain=False,
        )
        logger.debug(
            "[%s] MQTT telemetry published | T=%.2f H=%.1f HVAC=%s",
            self.node_id, self.room.temp, self.room.humidity, self.room.hvac_mode,
        )

    def _publish_heartbeat(self) -> None:
        if not self.connected:
            return
        hb = {
            "schema_version": "2.0.0",
            "sensor_id":  self.node_id,
            "status":     "online",
            "protocol":   "MQTT",
            "timestamp":  int(time.time()),
            "tick":       self._tick,
        }
        self._client.publish(
            self.topic_heartbeat,
            json.dumps(hb),
            qos=1,
            retain=True,
        )

    def _publish_online(self) -> None:
        online = build_online_payload(self.node_id)
        self._client.publish(
            self.topic_status,
            json.dumps(online),
            qos=1,
            retain=True,
        )

    async def run(self) -> None:
        """
        Main coroutine for this MQTT node.
        Connects to HiveMQ, then loops: physics tick → publish.
        Run 100 of these concurrently via asyncio.gather().
        """
        jitter = random.uniform(0.0, MAX_JITTER_S)
        logger.debug("[%s] Startup jitter %.2fs", self.node_id, jitter)
        await asyncio.sleep(jitter)

        self._client = self._build_client()

        while True:
            try:
                await self._client.connect(self.broker_host, self.broker_port, keepalive=60)
                break
            except Exception as exc:
                logger.error("[%s] Connection failed: %s — retrying in 5s", self.node_id, exc)
                await asyncio.sleep(5)

        self._publish_online()

        logger.info("[%s] MQTT node online", self.node_id)

        try:
            while True:
                tick_start = asyncio.get_event_loop().time()
                sim_clock  = time.time() - self.sim_start

                self.room.apply_physics(sim_clock)
                self.room.apply_environmental_correlations()
                self.room.validate_state()

                if random.random() < 0.10:
                    self.room.set_occupancy(not self.room.occupancy)

                self._publish_telemetry()

                if self._tick % HEARTBEAT_EVERY == 0:
                    self._publish_heartbeat()

                logger.info(
                    "[%s] tick=%d T=%.2f°C H=%.1f%% Occ=%s HVAC=%s",
                    self.node_id, self._tick,
                    self.room.temp, self.room.humidity,
                    self.room.occupancy, self.room.hvac_mode,
                )

                self._tick += 1
                elapsed    = asyncio.get_event_loop().time() - tick_start
                await asyncio.sleep(max(0.0, TICK_INTERVAL_S - elapsed))

        except asyncio.CancelledError:
            logger.info("[%s] MQTT node cancelled — disconnecting gracefully", self.node_id)
        finally:
            if self._client:
                await self._client.disconnect()
