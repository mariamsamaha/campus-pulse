"""
faults.py — Fault Modeling System for Campus Pulse.

Implements four fault types that can afflict individual Room nodes:

  1. sensor_drift      — Values gradually bias away from truth (±drift/tick)
  2. frozen_sensor     — Sensors lock onto the value at fault onset
  3. telemetry_delay   — MQTT publish is delayed by a random duration
  4. node_dropout      — Room goes silent (no publish) for a random period

Design:
  - FaultEngine is stateless beyond its config; each Room carries its own
    fault state in a FaultState object.
  - apply_fault() is called inside the simulation tick before publish,
    mutating the telemetry payload in place (non-destructive to room physics).
  - All fault activations / recoveries are logged at WARNING level so they
    appear prominently in the terminal and can be monitored externally.

Public API:
    engine  = FaultEngine(cfg.faults)
    fstate  = FaultState()           # one per Room, stored alongside Room

    # Inside simulation tick:
    engine.maybe_inject_fault(room, fstate)
    maybe_delay = await engine.apply_fault(room, fstate, payload)
    # maybe_delay is a float seconds >0 if telemetry_delay fault is active
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from config import FaultsConfig

logger = logging.getLogger("faults")

# ──────────────────────────────── constants ────────────────────────────────────

FAULT_SENSOR_DRIFT    = "sensor_drift"
FAULT_FROZEN_SENSOR   = "frozen_sensor"
FAULT_TELEMETRY_DELAY = "telemetry_delay"
FAULT_NODE_DROPOUT    = "node_dropout"

ALL_FAULT_TYPES = [
    FAULT_SENSOR_DRIFT,
    FAULT_FROZEN_SENSOR,
    FAULT_TELEMETRY_DELAY,
    FAULT_NODE_DROPOUT,
]


# ──────────────────────────────── state ───────────────────────────────────────

@dataclass
class FaultState:
    """
    Per-room fault state.  Attach one of these to each Room object alongside
    the regular Room instance — it carries the mutable fault bookkeeping.
    """
    active:           bool            = False
    fault_type:       Optional[str]   = None
    activated_at:     float           = 0.0      # epoch time
    recover_at:       float           = 0.0      # epoch: auto-recover deadline

    # sensor_drift
    drift_sign:       float           = 1.0      # +1 or -1
    cumulative_drift: float           = 0.0

    # frozen_sensor
    frozen_temp:      Optional[float] = None
    frozen_humidity:  Optional[float] = None

    # telemetry_delay
    delay_seconds:    float           = 0.0

    # node_dropout
    silent_until:     float           = 0.0

    # stats
    total_activations: int            = 0
    total_ticks_faulty: int           = 0


# ──────────────────────────────── engine ──────────────────────────────────────

class FaultEngine:
    """
    Injects and manages faults across the simulated room fleet.
    Completely config-driven — no code changes needed to tune probabilities.
    """

    def __init__(self, config: FaultsConfig) -> None:
        self._cfg = config
        self._weights = [
            config.types[ft].weight for ft in ALL_FAULT_TYPES
        ]

    # ──────────────────────── fault lifecycle ─────────────────────────────────

    def _select_fault_type(self) -> str:
        """Weighted random selection among enabled fault types."""
        return random.choices(ALL_FAULT_TYPES, weights=self._weights, k=1)[0]

    def _activate(self, room_id: str, fstate: FaultState, fault_type: str) -> None:
        """Initialise fault state for a newly triggered fault."""
        now = time.time()
        fstate.active             = True
        fstate.fault_type         = fault_type
        fstate.activated_at       = now
        fstate.total_activations += 1
        fstate.cumulative_drift   = 0.0
        fstate.frozen_temp        = None
        fstate.frozen_humidity    = None

        ft_cfg = self._cfg.types[fault_type]

        if fault_type == FAULT_SENSOR_DRIFT:
            fstate.drift_sign = random.choice([-1.0, 1.0])

        elif fault_type == FAULT_TELEMETRY_DELAY:
            fstate.delay_seconds = random.uniform(1.0, ft_cfg.max_delay)
            fstate.recover_at    = now + fstate.delay_seconds * 2   # auto-recover

        elif fault_type == FAULT_NODE_DROPOUT:
            silence = random.uniform(5.0, ft_cfg.max_silence)
            fstate.silent_until = now + silence
            fstate.recover_at   = fstate.silent_until

        logger.warning(
            "[FAULT] ⚡ ACTIVATED [%s] → room=%s  activated_at=%.0f",
            fault_type, room_id, now,
        )

    def _recover(self, room_id: str, fstate: FaultState) -> None:
        """Clear all fault state."""
        logger.warning(
            "[FAULT] ✅ RECOVERED [%s] → room=%s  (was active %.1fs, drift=%.2f)",
            fstate.fault_type,
            room_id,
            time.time() - fstate.activated_at,
            fstate.cumulative_drift,
        )
        fstate.active             = False
        fstate.fault_type         = None
        fstate.cumulative_drift   = 0.0
        fstate.frozen_temp        = None
        fstate.frozen_humidity    = None
        fstate.delay_seconds      = 0.0
        fstate.silent_until       = 0.0
        fstate.recover_at         = 0.0

    # ──────────────────────── per-tick injection ──────────────────────────────

    def maybe_inject_fault(self, room_id: str, fstate: FaultState) -> None:
        """
        Called once per simulation tick, before apply_fault().
        Handles:
          - Activating a new fault (if not already active)
          - Probabilistic recovery from an existing fault
          - Time-based auto-recovery (dropout / delay faults)
        """
        if not self._cfg.enabled:
            return

        now = time.time()

        if fstate.active:
            # Auto-recover if deadline passed
            if fstate.recover_at > 0 and now >= fstate.recover_at:
                self._recover(room_id, fstate)
                return
            # Probabilistic recovery
            if random.random() < self._cfg.recovery_probability:
                self._recover(room_id, fstate)
        else:
            # Probabilistic activation
            if random.random() < self._cfg.probability:
                self._activate(room_id, fstate, self._select_fault_type())

    # ──────────────────────── per-tick application ────────────────────────────

    async def apply_fault(
        self,
        room_id:  str,
        fstate:   FaultState,
        payload:  dict,
    ) -> bool:
        """
        Mutate the telemetry payload dict in-place according to active fault.

        Returns:
            True  → publish should proceed normally (or with delay baked in)
            False → publish should be SUPPRESSED (node_dropout)
        """
        if not fstate.active:
            return True

        fstate.total_ticks_faulty += 1
        ft = fstate.fault_type
        ft_cfg = self._cfg.types[ft]

        # ── sensor_drift ──────────────────────────────────────────────────────
        if ft == FAULT_SENSOR_DRIFT:
            drift_per_tick = random.uniform(0.1, ft_cfg.max_drift / 10.0)
            fstate.cumulative_drift += fstate.drift_sign * drift_per_tick

            sensors = payload.get("sensors", {})
            sensors["temperature"] = round(
                sensors.get("temperature", 0.0) + fstate.cumulative_drift, 2
            )
            sensors["humidity"] = round(
                max(0.0, min(100.0,
                    sensors.get("humidity", 0.0) + fstate.cumulative_drift * 0.5
                )), 2
            )
            sensors["_fault"] = f"drift({fstate.cumulative_drift:+.2f})"
            logger.debug(
                "[FAULT] drift on %s  cumulative=%.2f",
                room_id, fstate.cumulative_drift,
            )

        # ── frozen_sensor ─────────────────────────────────────────────────────
        elif ft == FAULT_FROZEN_SENSOR:
            sensors = payload.get("sensors", {})
            if fstate.frozen_temp is None:
                # Capture the frozen snapshot on first tick of the fault
                fstate.frozen_temp     = sensors.get("temperature")
                fstate.frozen_humidity = sensors.get("humidity")
                logger.warning(
                    "[FAULT] frozen_sensor locked on %s → T=%.1f H=%.1f",
                    room_id, fstate.frozen_temp, fstate.frozen_humidity,
                )
            sensors["temperature"] = fstate.frozen_temp
            sensors["humidity"]    = fstate.frozen_humidity
            sensors["_fault"]      = "frozen"

        # ── telemetry_delay ───────────────────────────────────────────────────
        elif ft == FAULT_TELEMETRY_DELAY:
            delay = fstate.delay_seconds
            logger.debug("[FAULT] telemetry_delay on %s → sleeping %.2fs", room_id, delay)
            payload.setdefault("sensors", {})["_fault"] = f"delay({delay:.1f}s)"
            await asyncio.sleep(delay)   # non-blocking delay before publish

        # ── node_dropout ──────────────────────────────────────────────────────
        elif ft == FAULT_NODE_DROPOUT:
            remaining = max(0.0, fstate.silent_until - time.time())
            logger.debug(
                "[FAULT] node_dropout on %s → suppressing publish (silent for %.1fs more)",
                room_id, remaining,
            )
            return False   # caller must NOT publish

        return True

    # ──────────────────────── reporting ───────────────────────────────────────

    def fault_summary(self, room_id: str, fstate: FaultState) -> dict:
        """Returns a dict suitable for inclusion in a heartbeat or status payload."""
        return {
            "fault_active":          fstate.active,
            "fault_type":            fstate.fault_type,
            "total_activations":     fstate.total_activations,
            "total_ticks_faulty":    fstate.total_ticks_faulty,
            "cumulative_drift":      round(fstate.cumulative_drift, 3),
        }
