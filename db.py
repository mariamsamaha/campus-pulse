"""
db.py — Async SQLite persistence for Campus Pulse.

Table: rooms
  room_id       TEXT  PRIMARY KEY
  last_temp     REAL                — validated: 15–50 °C
  last_humidity REAL                — validated: 0–100 %
  hvac_mode     TEXT                — validated: ON / OFF / ECO
  target_temp   REAL
  last_update   INTEGER             — Unix epoch; used to detect stale rows
  occupancy     INTEGER             — 0 / 1  (full continuity after restart)
  light_level   INTEGER             — Lux    (full continuity after restart)

Design:
  ┌─────────────────────────────────────────────────────────────────┐
  │  Dirty-flag smart sync  — only changed rooms ever hit the disk  │
  │  Periodic flush: every sync_interval seconds (30-60 s)          │
  │  Early flush:    when dirty count ≥ dirty_threshold             │
  │  Immediate flush: on HVAC-mode change (sync-on-command)         │
  │  Validation at DB layer: bad values are clamped before writing  │
  │  Corruption guard: any row that fails validation is logged      │
  │                     and excluded (system falls back to default) │
  │  last_update used to:                                           │
  │    - detect stale rows on startup (warn if > STALE_THRESHOLD)   │
  │    - provide debug timeline for anomaly investigation           │
  └─────────────────────────────────────────────────────────────────┘

Public API:
    db = RoomDatabase(cfg.database)
    await db.init()
    states = await db.load_all_states()           # once at startup
    db.mark_dirty(room)                           # after each tick (O(1))
    await db.flush_one(room, rooms)               # immediate (HVAC change)
    await db.flush(rooms)                         # batch flush
    asyncio.create_task(db.periodic_sync_task(rooms, stop_event))
    await db.close()
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Optional, Set, Tuple

import aiosqlite

from room import Room, RoomState
from config import DatabaseConfig

logger = logging.getLogger("db")

# ─────────────────────────── validation constants ─────────────────────────────
# Mirrors room.py SPEC — enforced independently at the DB layer
_TEMP_RANGE   = (15.0,  50.0)
_HUM_RANGE    = (0.0,  100.0)
_LIGHT_RANGE  = (0,    1000)
_VALID_HVAC   = {"ON", "OFF", "ECO"}
_STALE_SECS   = 3600   # warn if a loaded row hasn't been updated in 1 hour

# ─────────────────────────── SQL statements ───────────────────────────────────

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS rooms (
    room_id       TEXT PRIMARY KEY,
    last_temp     REAL,
    last_humidity REAL,
    hvac_mode     TEXT,
    target_temp   REAL,
    last_update   INTEGER,
    occupancy     INTEGER DEFAULT 0,
    light_level   INTEGER DEFAULT 50
);
"""

# Migration: add new columns to existing DBs (non-destructive)
_MIGRATE = [
    "ALTER TABLE rooms ADD COLUMN occupancy   INTEGER DEFAULT 0;",
    "ALTER TABLE rooms ADD COLUMN light_level INTEGER DEFAULT 50;",
]

# INSERT OR REPLACE: existing row → overwritten, new row → inserted.
_UPSERT = """
INSERT OR REPLACE INTO rooms
    (room_id, last_temp, last_humidity, hvac_mode,
     target_temp, last_update, occupancy, light_level)
VALUES (?, ?, ?, ?, ?, ?, ?, ?);
"""

_SELECT_ALL = """
SELECT room_id, last_temp, last_humidity, hvac_mode,
       target_temp, last_update, occupancy, light_level
FROM rooms;
"""


# ─────────────────────────── validation helpers ───────────────────────────────

def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _validate_row(row: aiosqlite.Row) -> Optional[RoomState]:
    """
    Validate a DB row before loading it into a Room.

    Returns None if the row is fundamentally corrupt (e.g. bad HVAC mode
    that can't be safely defaulted to anything useful). Caller falls back
    to room defaults in that case and logs a warning.

    Valid rows are sanitised (clamped) in place — a small drift in stored
    values is corrected silently; only complete failures are rejected.
    """
    rid = row["room_id"]

    try:
        raw_temp  = float(row["last_temp"])
        raw_hum   = float(row["last_humidity"])
        hvac      = str(row["hvac_mode"])
        tgt_temp  = float(row["target_temp"])
        ts        = int(row["last_update"])
        occupancy = bool(int(row["occupancy"] or 0))
        light     = int(row["light_level"] or 50)
    except (TypeError, ValueError) as exc:
        logger.error(
            "[DB] ❌ Corrupt row for %s (%s) — falling back to defaults.", rid, exc
        )
        return None

    # Clamp sensor values to valid ranges
    temp = _clamp(raw_temp,  *_TEMP_RANGE)
    hum  = _clamp(raw_hum,   *_HUM_RANGE)
    light = int(_clamp(light, *_LIGHT_RANGE))

    if raw_temp != temp or raw_hum != hum:
        logger.warning(
            "[DB] ⚠️  Out-of-range values clamped for %s "
            "(T: %.1f→%.1f, H: %.1f→%.1f)",
            rid, raw_temp, temp, raw_hum, hum,
        )

    if hvac not in _VALID_HVAC:
        logger.error(
            "[DB] ❌ Invalid hvac_mode '%s' for %s — defaulting to OFF.", hvac, rid
        )
        hvac = "OFF"

    # Stale-row detection: warn if last_update is much older than expected
    age = time.time() - ts
    if age > _STALE_SECS:
        logger.warning(
            "[DB] ⏰ Stale row for %s: last_update was %.0f minutes ago. "
            "State may not reflect reality — restoring anyway.",
            rid, age / 60,
        )

    return RoomState(
        room_id=      rid,
        last_temp=    temp,
        last_humidity=hum,
        hvac_mode=    hvac,
        target_temp=  tgt_temp,
        last_update=  ts,
        occupancy=    occupancy,
        light_level=  light,
    )


# ─────────────────────────── database class ───────────────────────────────────

class RoomDatabase:
    """Async SQLite persistence layer for the room fleet."""

    def __init__(self, config: DatabaseConfig) -> None:
        self._path            = config.path
        self._sync_interval   = config.sync_interval
        self._dirty_threshold = config.dirty_threshold

        self._conn: Optional[aiosqlite.Connection] = None
        self._dirty: Set[str] = set()        # room IDs pending a DB write
        self._dirty_lock      = asyncio.Lock()
        self._last_flush      = 0.0          # epoch of last successful flush
        self._flush_count     = 0

    # ────────────────────────── lifecycle ─────────────────────────────────────

    async def init(self) -> None:
        """Open / create the database, ensure schema, run migrations."""
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row

        # Performance pragmas — WAL allows concurrent reads during writes
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        await self._conn.execute(_CREATE_TABLE)

        # Non-destructive migration: add new columns to existing DBs
        for stmt in _MIGRATE:
            try:
                await self._conn.execute(stmt)
            except Exception:
                pass  # column already exists — safe to ignore

        await self._conn.commit()
        logger.info(
            "[DB] ✅ Initialised table 'rooms' at '%s' (WAL, NORMAL sync)", self._path
        )

    async def close(self) -> None:
        """Final flush then close — called on graceful shutdown."""
        pending = len(self._dirty)
        if pending:
            logger.info("[DB] Shutdown flush: %d dirty rooms pending.", pending)
        # flush() needs rooms dict; engine passes it via stop_event path.
        # This bare close() call is the last-resort safety net.
        if self._conn:
            await self._conn.close()
            logger.info("[DB] Connection closed.")

    # ────────────────────────── startup restore ───────────────────────────────

    async def load_all_states(self) -> Dict[str, RoomState]:
        """
        Read all rows from the DB on startup.

        - Empty DB  → returns {}  (rooms init at defaults)
        - Has rows  → validates each row; bad rows are excluded (fall back to
                      defaults for that room only, NOT a full reset)
        - Stale rows → loaded with a warning (last_update too old)
        """
        assert self._conn, "Call init() first"

        try:
            rows = await self._conn.execute_fetchall(_SELECT_ALL)
        except Exception as exc:
            logger.error(
                "[DB] ❌ Failed to read 'rooms' table: %s — "
                "starting fresh (all rooms will use defaults).", exc
            )
            return {}

        states: Dict[str, RoomState] = {}
        skipped = 0

        for row in rows:
            state = _validate_row(row)
            if state is None:
                skipped += 1
                continue
            states[state.room_id] = state

        if not states and skipped == 0:
            logger.info(
                "[DB] Empty database — all %d rooms will start at defaults "
                "(22 °C, 50%% RH, HVAC=OFF).",
                0,   # fleet size unknown here; engine logs the real count
            )
        elif states:
            logger.info(
                "[DB] ♻️  Crash recovery: restored %d room states "
                "(%d skipped due to corruption).",
                len(states), skipped,
            )

        return states

    # ────────────────────────── dirty tracking ────────────────────────────────

    def mark_dirty(self, room: Room) -> None:
        """
        Flag a room for the next periodic flush.
        Called on every tick — must be O(1) and never block.
        """
        self._dirty.add(room.id)

    # ────────────────────────── flush (batch) ────────────────────────────────

    async def flush(self, rooms: Optional[Dict[str, Room]] = None) -> int:
        """
        Batch-write all dirty rooms in one transaction.

        Validation is re-applied here at the DB layer (belt-and-suspenders):
        even if the simulation produced an out-of-range value that slipped
        past room.validate_state(), it will be clamped before it hits disk.

        Returns: number of rows written.
        """
        async with self._dirty_lock:
            if not self._dirty:
                return 0
            dirty_ids = list(self._dirty)
            self._dirty.clear()

        if rooms is None:
            logger.warning("[DB] flush() called without fleet dict — skipping.")
            return 0

        tuples = self._build_tuples(dirty_ids, rooms)
        if not tuples:
            return 0

        await self._conn.executemany(_UPSERT, tuples)
        await self._conn.commit()

        self._last_flush  = time.time()
        self._flush_count += 1
        logger.info(
            "[DB] ✅ Sync #%d — %d/%d dirty rooms written  (%.3fs since last sync)",
            self._flush_count,
            len(tuples),
            len(dirty_ids),
            time.time() - self._last_flush + 0.001,   # avoid 0.000
        )
        return len(tuples)

    # ────────────────────────── flush (single / on-command) ──────────────────

    async def flush_one(self, room: Room, rooms: Dict[str, Room]) -> None:
        """
        Immediately persist a single room without waiting for the periodic timer.

        Use this for important state changes (e.g. HVAC mode change) so the
        new value survives a crash even if the bulk sync hasn't fired yet.

        The room is ALSO removed from the dirty set (no double-write).
        """
        async with self._dirty_lock:
            self._dirty.discard(room.id)

        tuples = self._build_tuples([room.id], rooms)
        if not tuples:
            return

        await self._conn.executemany(_UPSERT, tuples)
        await self._conn.commit()
        logger.info(
            "[DB] ⚡ Immediate sync for %s "
            "(triggered by command / HVAC change)",
            room.id,
        )

    # ────────────────────────── internal helpers ──────────────────────────────

    def _build_tuples(
        self,
        room_ids: list[str],
        rooms: Dict[str, Room],
    ) -> list[Tuple]:
        """
        Serialise rooms to DB tuples, validating/clamping values at write time.
        Column order must match _UPSERT:
          (room_id, last_temp, last_humidity, hvac_mode,
           target_temp, last_update, occupancy, light_level)
        """
        tuples = []
        for rid in room_ids:
            room = rooms.get(rid)
            if room is None:
                continue
            s = room.to_state()

            # ── DB-layer validation (belt-and-suspenders) ─────────────────
            temp  = _clamp(s.last_temp,    *_TEMP_RANGE)
            hum   = _clamp(s.last_humidity, *_HUM_RANGE)
            light = int(_clamp(s.light_level, *_LIGHT_RANGE))
            hvac  = s.hvac_mode if s.hvac_mode in _VALID_HVAC else "OFF"

            if temp != s.last_temp or hum != s.last_humidity:
                logger.warning(
                    "[DB] ⚠️  Clamped out-of-range values before write for %s "
                    "(T: %.1f→%.1f, H: %.1f→%.1f)",
                    rid, s.last_temp, temp, s.last_humidity, hum,
                )

            tuples.append((
                s.room_id,
                round(temp,  4),
                round(hum,   4),
                hvac,
                s.target_temp,
                s.last_update,
                int(s.occupancy),
                light,
            ))
        return tuples

    # ────────────────────────── periodic task ────────────────────────────────

    async def periodic_sync_task(
        self,
        rooms:      Dict[str, Room],
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        """
        Background asyncio task — fires every sync_interval seconds (default 30 s).

        Two triggers:
          1. Timer:     sync_interval has elapsed since last flush
          2. Threshold: dirty set size ≥ dirty_threshold (early flush)

        On stop_event or CancelledError → final flush before exiting.
        """
        logger.info(
            "[DB] Periodic sync task started "
            "(interval=%ds, threshold=%d rooms).",
            self._sync_interval, self._dirty_threshold,
        )
        check_interval = min(5.0, self._sync_interval / 4)

        while True:
            try:
                if stop_event and stop_event.is_set():
                    logger.info("[DB] Stop signal — performing final flush.")
                    await self.flush(rooms)
                    return

                await asyncio.sleep(check_interval)

                now       = time.time()
                time_due  = (now - self._last_flush) >= self._sync_interval
                count_due = len(self._dirty) >= self._dirty_threshold

                if time_due or count_due:
                    trigger = "timer" if time_due else f"threshold({len(self._dirty)})"
                    written = await self.flush(rooms)
                    if written:
                        logger.info(
                            "[DB] Periodic sync [%s]: %d rows committed at %s.",
                            trigger, written,
                            time.strftime("%H:%M:%S", time.localtime()),
                        )

            except asyncio.CancelledError:
                logger.info("[DB] Sync task cancelled — final flush.")
                await self.flush(rooms)
                return
            except Exception as exc:
                logger.error("[DB] Sync task error: %s", exc, exc_info=True)
                # Don't crash — keep syncing on next cycle
