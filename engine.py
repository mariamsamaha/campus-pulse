"""
engine.py — Async simulation engine for Campus Pulse.

Integrates:
  - Config-driven fleet construction (room count from settings.yml / env)
  - SQLite state persistence (db.py) with crash-recovery on startup
  - Fault injection per tick (faults.py)
  - MQTT telemetry publish (mqtt_manager.py)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Dict

from config import cfg
from db import RoomDatabase
from faults import FaultEngine, FaultState
from mqtt_manager import MQTTManager
from room import Room

# ─────────────────────────────── logging ──────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("engine")

# ─────────────────────────────── singletons ───────────────────────────────────
mqtt_manager = MQTTManager(
    broker_host=cfg.mqtt.broker_host,
    broker_port=cfg.mqtt.broker_port,
    client_id=cfg.mqtt.client_id,
)


# ─────────────────────────────── fleet builder ────────────────────────────────
async def build_fleet(db: RoomDatabase) -> Dict[str, Room]:
    """
    Construct the room fleet.
    1. Load persisted states from DB (empty on first run).
    2. Create each Room with no state to get its canonical .id.
    3. Restore state from DB if a matching row exists.
    Returns a dict {room_id: Room} for O(1) lookup.
    """
    saved_states = await db.load_all_states()
    sim   = cfg.simulation
    therm = cfg.thermal
    rooms: Dict[str, Room] = {}
    restored = 0

    for floor in range(1, sim.num_floors + 1):
        for room_num in range(1, sim.rooms_per_floor + 1):
            # Build with defaults first so we can read the canonical .id
            room = Room(
                building=sim.building_id,
                floor=floor,
                room_num=room_num,
                alpha=therm.alpha,
                beta=therm.beta,
            )
            # Restore persisted state (full continuity after crash)
            if room.id in saved_states:
                s = saved_states[room.id]
                room.temp        = s.last_temp
                room.humidity    = s.last_humidity
                room.hvac_mode   = s.hvac_mode
                room.target_temp = s.target_temp
                room.occupancy   = s.occupancy      # restored — no reset
                room.light_level = s.light_level    # restored — no reset
                restored += 1
                logger.info(
                    "[%s] ♻️  State restored from DB "
                    "(T=%.1f, H=%.1f, HVAC=%s, Occ=%s, Light=%d)",
                    room.id, room.temp, room.humidity,
                    room.hvac_mode, room.occupancy, room.light_level,
                )

            rooms[room.id] = room

    total = len(rooms)
    logger.info(
        "Fleet ready: %d rooms (%d floors × %d/floor) | "
        "restored=%d  fresh=%d",
        total,
        sim.num_floors,
        sim.rooms_per_floor,
        restored,
        total - restored,
    )
    return rooms


# ─────────────────────────────── room task ────────────────────────────────────
async def room_task(
    room:        Room,
    fstate:      FaultState,
    fault_eng:   FaultEngine,
    db:          RoomDatabase,
    rooms_ref:   Dict[str, Room],
    sim_start:   float,
) -> None:
    """
    Per-room coroutine.  Runs forever until cancelled.

    Tick cycle:
      1. Physics update
      2. Environmental correlations
      3. State validation
      4. Random occupancy toggle (10 % chance)
      5. Fault injection / recovery check
      6. Build telemetry payload
      7. Apply fault (may mutate payload, add async delay, or suppress publish)
      8. MQTT publish (telemetry + periodic heartbeat)
      9. Persistence:
           mark_dirty (every tick)  → picked up by periodic sync
           flush_one  (HVAC change) → immediate write, survives crash
    """
    sim = cfg.simulation

    # ── startup jitter ────────────────────────────────────────────────────────
    jitter = random.uniform(0, sim.max_jitter)
    logger.debug("[%s] startup jitter %.2fs", room.id, jitter)
    await asyncio.sleep(jitter)
    logger.info("[%s] task started", room.id)

    tick_count = 0

    while True:
        tick_start = asyncio.get_event_loop().time()
        sim_clock  = time.time() - sim_start

        try:
            # ── 1-4: physics + validation ──────────────────────────────────
            room.apply_physics(sim_clock)
            room.apply_environmental_correlations()
            room.validate_state()

            # Track HVAC mode before occupancy toggle so we can detect changes
            hvac_before = room.hvac_mode

            if random.random() < 0.10:
                room.set_occupancy(not room.occupancy)

            # ── 5: fault lifecycle ────────────────────────────────────────
            fault_eng.maybe_inject_fault(room.id, fstate)

            # ── 6: build payload ─────────────────────────────────────────
            payload = room.telemetry_payload()
            if fstate.active:
                payload["fault"] = fault_eng.fault_summary(room.id, fstate)

            # ── 7: apply fault (may mutate payload / delay / suppress) ───
            should_publish = await fault_eng.apply_fault(room.id, fstate, payload)

            # ── 8: MQTT publish ───────────────────────────────────────────
            if should_publish:
                topic = f"{room.mqtt_path}/telemetry"
                await mqtt_manager.publish(topic, payload, qos=cfg.mqtt.qos)

                if tick_count % sim.heartbeat_every == 0:
                    hb = room.heartbeat_payload()
                    hb["fault"] = fault_eng.fault_summary(room.id, fstate)
                    hb_topic = f"{room.mqtt_path}/heartbeat"
                    await mqtt_manager.publish(hb_topic, hb, qos=cfg.mqtt.qos)
            else:
                logger.debug("[%s] tick %d suppressed (node_dropout)", room.id, tick_count)

            # ── 9: persistence ───────────────────────────────────────────
            # Always mark dirty so periodic sync picks it up
            db.mark_dirty(room)

            # Sync-on-command: if HVAC mode changed this tick, flush immediately
            # so the new mode survives a crash even before the next periodic sync
            if room.hvac_mode != hvac_before:
                logger.info(
                    "[%s] HVAC changed %s → %s — triggering immediate DB sync.",
                    room.id, hvac_before, room.hvac_mode,
                )
                await db.flush_one(room, rooms_ref)

            logger.info(
                "[%s] tick=%d T=%.2f°C H=%.1f%% Occ=%s HVAC=%s Light=%d fault=%s",
                room.id, tick_count,
                room.temp, room.humidity,
                room.occupancy, room.hvac_mode, room.light_level,
                fstate.fault_type or "none",
            )

        except Exception as exc:
            logger.error(
                "[%s] tick %d error: %s", room.id, tick_count, exc,
                exc_info=True,
            )

        tick_count += 1
        elapsed      = asyncio.get_event_loop().time() - tick_start
        sleep_time   = max(0.0, cfg.simulation.tick_interval - elapsed)
        await asyncio.sleep(sleep_time)


# ─────────────────────────────── engine entry ─────────────────────────────────
async def run_engine() -> None:
    sim_start  = time.time()
    stop_event = asyncio.Event()

    logger.info("━━━ Campus Pulse Engine starting ━━━  epoch=%d", int(sim_start))
    logger.info(
        "Config: rooms=%d×%d  tick=%.1fs  fault_prob=%.2f  db=%s",
        cfg.simulation.num_floors,
        cfg.simulation.rooms_per_floor,
        cfg.simulation.tick_interval,
        cfg.faults.probability,
        cfg.database.path,
    )

    # ── Database ──────────────────────────────────────────────────────────────
    db = RoomDatabase(cfg.database)
    await db.init()

    # ── MQTT ──────────────────────────────────────────────────────────────────
    await mqtt_manager.connect()

    # ── Fleet (with crash recovery) ───────────────────────────────────────────
    rooms = await build_fleet(db)

    # ── Fault engine ──────────────────────────────────────────────────────────
    fault_engine = FaultEngine(cfg.faults)
    fault_states: Dict[str, FaultState] = {rid: FaultState() for rid in rooms}

    # ── Background DB sync task ───────────────────────────────────────────────
    sync_task = asyncio.create_task(
        db.periodic_sync_task(rooms, stop_event),
        name="db_sync",
    )

    # ── Room coroutines ───────────────────────────────────────────────────────
    room_tasks = [
        asyncio.create_task(
            room_task(room, fault_states[rid], fault_engine, db, rooms, sim_start),
            name=rid,
        )
        for rid, room in rooms.items()
    ]

    logger.info("Launched %d room tasks + 1 DB sync task", len(room_tasks))

    try:
        await asyncio.gather(*room_tasks, return_exceptions=True)
    finally:
        stop_event.set()
        await db.close()
        await mqtt_manager.disconnect()
        logger.info("━━━ Campus Pulse Engine stopped ━━━")


if __name__ == "__main__":
    try:
        asyncio.run(run_engine())
    except KeyboardInterrupt:
        logger.info("Engine stopped by user (Ctrl+C).")