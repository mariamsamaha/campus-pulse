from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import random
import time
from typing import Optional
import aiocoap
import aiocoap.resource as resource
from aiocoap import Message, Code
from dedup import DedupHandler
from telemetry_schema import build_telemetry, build_command_ack

logger = logging.getLogger("coap_node")

COAP_BASE_PORT:  int   = 5683          
TICK_INTERVAL_S: float = 5.0
MAX_JITTER_S:    float = 5.0
COAP_BIND_ADDR:  str   = "127.0.0.1"  

def coap_port_for(floor: int, room_num: int) -> int:
    """
    Derive the UDP port for a CoAP room node.
    CoAP rooms are rooms 11-20 per floor.
    Floor 1 room 11 → port 5683
    Floor 10 room 20 → port 5782
    """
    room_offset = room_num - 11  
    return COAP_BASE_PORT + (floor - 1) * 10 + room_offset


class TelemetryResource(resource.ObservableResource):
    """
    RFC 7641 Observable resource.

    A Gateway registers as an observer with a single GET.
    Every physics tick, the node calls notify_change() which pushes
    the updated payload to all registered observers over UDP.
    """

    def __init__(self, node: "CoAPNode"):
        super().__init__()
        self._node = node

    async def render_get(self, request: Message) -> Message:
        """Respond to a GET (or Observe subscription registration)."""
        payload = self._node.current_telemetry_json().encode()
        return Message(
            code=Code.CONTENT,
            payload=payload,
            content_format=aiocoap.numbers.contentformat.ContentFormat.JSON,
        )

    def push_update(self) -> None:
        """Called by physics loop to push new readings to all observers."""
        self.updated_state()    

class HVACResource(resource.Resource):
    """
    PUT /f##/r###/actuators/hvac

    Receives commands from the Gateway (translated from ThingsBoard MQTT).
    Uses CoAP Confirmable (CON) messaging for reliability.
    Sends back ACK + response payload.

    Supported body:
      {"action": "SET_HVAC",  "value": "ON"|"OFF"|"ECO"}
      {"action": "SET_TEMP",  "value": 22.5}
      {"action": "SET_OCC",   "value": true}
      {"action": "EMERGENCY_LOCKOUT"}
    """

    def __init__(self, node: "CoAPNode"):
        super().__init__()
        self._node = node

    async def render_put(self, request: Message) -> Message:
        """Handle a PUT request.

        CoAP Confirmable (CON) reliability:
          When the sender uses a CON PUT, aiocoap automatically sends an ACK
          at the transport layer.  If the ACK is lost in transit, the sender
          retransmits the CON; aiocoap delivers the duplicate to this handler.
          The application-level dedup cache (keyed on SHA-256 of the payload)
          detects the duplicate and returns 2.03 Valid — the same semantics
          as "already processed" — without re-applying the actuator command.

          This satisfies the requirement:
            CON PUT sent → ACK returned → no duplicate side-effect on retry.
        """
        node = self._node

        # Stable, deterministic hash: SHA-256 of raw payload bytes
        cmd_hash = hashlib.sha256(request.payload).hexdigest()[:16]

        try:
            cmd = json.loads(request.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.error("[%s] CoAP CON PUT — invalid JSON payload", node.node_id)
            return Message(code=Code.BAD_REQUEST, payload=b"Invalid JSON")

        action = cmd.get("action", "?")

        logger.info(
            "[%s] [RELIABILITY] CoAP CON PUT received — action=%s hash=%s",
            node.node_id, action, cmd_hash,
        )

        # Application-level dedup: identical CON retransmit → drop
        if node.dedup.is_coap_duplicate(node.node_id, cmd_hash):
            logger.warning(
                "[%s] [RELIABILITY] CoAP CON DUPLICATE detected "
                "(hash=%s) — returning 2.03 Valid, actuator NOT re-applied",
                node.node_id, cmd_hash,
            )
            return Message(
                code=Code.VALID,
                payload=b"Duplicate - already processed",
            )

        value     = cmd.get("value")
        new_state: object = None

        if action.upper() == "SET_HVAC":
            old = node.room.hvac_mode
            node.room.set_hvac(str(value).upper())
            new_state = node.room.hvac_mode
            logger.info(
                "[%s] [RELIABILITY] ⚡ CoAP Actuator applied — HVAC %s → %s",
                node.node_id, old, node.room.hvac_mode,
            )

        elif action.upper() == "SET_TEMP":
            node.room.set_target_temp(float(value))
            new_state = node.room.target_temp
            logger.info(
                "[%s] [RELIABILITY] ⚡ CoAP Actuator applied — Target temp → %.1f",
                node.node_id, float(value),
            )

        elif action.upper() == "SET_OCC":
            node.room.set_occupancy(bool(value))
            new_state = node.room.occupancy
            logger.info(
                "[%s] [RELIABILITY] ⚡ CoAP Actuator applied — Occupancy → %s",
                node.node_id, bool(value),
            )

        elif action.upper() == "EMERGENCY_LOCKOUT":
            node.room.set_hvac("OFF")
            node.room.set_occupancy(False)
            node.room.light_level = 0
            new_state = "LOCKOUT_ACTIVE"
            logger.warning(
                "[%s] [RELIABILITY] ⚡ CoAP EMERGENCY LOCKOUT applied",
                node.node_id,
            )

        else:
            logger.warning("[%s] Unknown CoAP action: '%s'", node.node_id, action)
            return Message(code=Code.BAD_OPTION, payload=b"Unknown action")

        ack = build_command_ack(node.node_id, action.upper(), new_state)
        logger.info(
            "[%s] [RELIABILITY] CoAP ACK returning 2.04 Changed "
            "— action=%s new_state=%s hash=%s",
            node.node_id, action, new_state, cmd_hash,
        )
        return Message(
            code=Code.CHANGED,
            payload=json.dumps(ack).encode(),
            content_format=aiocoap.numbers.contentformat.ContentFormat.JSON,
        )

class StatusResource(resource.Resource):
    """Simple health probe — not observable, just answers GET."""

    def __init__(self, node: "CoAPNode"):
        super().__init__()
        self._node = node

    async def render_get(self, request: Message) -> Message:
        status = {
            "sensor_id": self._node.node_id,
            "status":    "online",
            "protocol":  "CoAP",
            "timestamp": int(time.time()),
            "hvac_mode": self._node.room.hvac_mode,
            "port":      self._node.port,
        }
        return Message(
            code=Code.CONTENT,
            payload=json.dumps(status).encode(),
            content_format=aiocoap.numbers.contentformat.ContentFormat.JSON,
        )

class CoAPNode:
    """
    A single CoAP server representing one room on the campus.

    The server binds to 127.0.0.1:<port> and exposes:
      /f##/r###/telemetry        (Observable)
      /f##/r###/actuators/hvac   (PUT)
      /f##/r###/status           (GET)
    """

    def __init__(
        self,
        room,                        
        dedup: DedupHandler,
        sim_start: float,
    ):
        self.room      = room
        self.dedup     = dedup
        self.sim_start = sim_start

        floor_num = int(room.floor_id[1:])
        room_num  = int(room.room_id_num[1:]) % 100

        self.node_id = room.id
        self.port    = coap_port_for(floor_num, room_num)
        self._path_telemetry = (room.floor_id, room.room_id_num, "telemetry")
        self._path_hvac      = (room.floor_id, room.room_id_num, "actuators", "hvac")
        self._path_status    = (room.floor_id, room.room_id_num, "status")

        self._tel_resource: Optional[TelemetryResource] = None
        self._context: Optional[aiocoap.Context] = None

        self._tick = 0

    def current_telemetry_json(self) -> str:
        """Build and return current telemetry as a JSON string."""
        payload = build_telemetry(
            sensor_id       = self.node_id,
            building        = self.room.building_id,
            floor           = int(self.room.floor_id[1:]),
            room            = int(self.room.room_id_num[1:]),
            protocol        = "CoAP",
            temperature     = self.room.temp,
            humidity        = self.room.humidity,
            occupancy       = self.room.occupancy,
            light_level     = self.room.light_level,
            hvac_mode       = self.room.hvac_mode,
            lighting_dimmer = self.room.lighting_dimmer,
        )
        return json.dumps(payload)

    async def run(self) -> None:
        """
        Main coroutine.
        1. Start CoAP server on unique port.
        2. Loop: physics tick → push observable update.
        """
        jitter = random.uniform(0.0, MAX_JITTER_S)
        logger.debug("[%s] CoAP startup jitter %.2fs (port %d)", self.node_id, jitter, self.port)
        await asyncio.sleep(jitter)

        root = resource.Site()
        self._tel_resource = TelemetryResource(self)
        root.add_resource(self._path_telemetry, self._tel_resource)
        root.add_resource(self._path_hvac,      HVACResource(self))
        root.add_resource(self._path_status,    StatusResource(self))

        try:
            self._context = await aiocoap.Context.create_server_context(
                root,
                bind=(COAP_BIND_ADDR, self.port),
            )
        except OSError as exc:
            logger.error("[%s] CoAP bind failed on port %d: %s", self.node_id, self.port, exc)
            return

        logger.info("[%s] CoAP server listening on %s:%d", self.node_id, COAP_BIND_ADDR, self.port)

        try:
            while True:
                tick_start = asyncio.get_event_loop().time()
                sim_clock  = time.time() - self.sim_start

                self.room.apply_physics(sim_clock)
                self.room.apply_environmental_correlations()
                self.room.validate_state()

                if random.random() < 0.10:
                    self.room.set_occupancy(not self.room.occupancy)

                if self._tel_resource is not None:
                    self._tel_resource.push_update()

                logger.info(
                    "[%s] CoAP tick=%d T=%.2f°C H=%.1f%% Occ=%s HVAC=%s port=%d",
                    self.node_id, self._tick,
                    self.room.temp, self.room.humidity,
                    self.room.occupancy, self.room.hvac_mode,
                    self.port,
                )

                self._tick += 1
                elapsed = asyncio.get_event_loop().time() - tick_start
                await asyncio.sleep(max(0.0, TICK_INTERVAL_S - elapsed))

        except asyncio.CancelledError:
            logger.info("[%s] CoAP node cancelled", self.node_id)
        finally:
            if self._context:
                await self._context.shutdown()
                logger.info("[%s] CoAP context shut down", self.node_id)
