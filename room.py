import time
import random
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

SPEC = {   
    "temperature":     (15.0, 50.0),
    "humidity":        (0.0,  100.0),
    "light_level":     (0,    1000),
    "lighting_dimmer": (0,    100),
}

VALID_HVAC_MODES = {"ON", "OFF", "ECO"}

OCCUPIED_LIGHT_THRESHOLD = 200   #occupancy thresholds
OCCUPIED_LIGHT_DEFAULT   = 450
UNOCCUPIED_LIGHT_DEFAULT = 50
OCCUPIED_TEMP_BOOST      = 0.05

DAY_OUTSIDE_TEMP   = 32.0
NIGHT_OUTSIDE_TEMP = 20.0

@dataclass
class RoomState:        # complete snapshot for persistence
    room_id:       str
    last_temp:     float
    last_humidity: float
    hvac_mode:     str
    target_temp:   float
    last_update:   int
    # Extended fields — enable full continuity after restart
    occupancy:    bool = False
    light_level:  int  = 50


@dataclass
class DesiredState:
    hvac_mode:       Optional[str]   = None   
    target_temp:     Optional[float] = None  
    lighting_dimmer: Optional[int]   = None   
    occupancy:       Optional[bool]  = None   
 
@dataclass
class OTAConfig:

    alpha:          Optional[float] = None   
    beta:           Optional[float] = None   
    tick_interval:  Optional[float] = None   
    fault_prob:     Optional[float] = None   
 
 
class Room:         # represents a single iot node
    def __init__(
        self,
        building: str,
        floor: int,
        room_num: int,
        alpha: float = 0.01,
        beta: float  = 0.20,
        state: Optional[RoomState] = None,
    ):
        self.building_id = building   # Room Identity
        self.floor_id    = f"f{floor:02d}"
        self.room_id_num = f"r{floor}{room_num:02d}"
        self.id          = f"{self.building_id}-{self.floor_id}-{self.room_id_num}"
        self.mqtt_path   = f"campus/{self.building_id}/{self.floor_id}/{self.room_id_num}"

        self.alpha = alpha
        self.beta  = beta

        if state:
            self.temp            = state.last_temp
            self.humidity        = state.last_humidity
            self.hvac_mode       = state.hvac_mode
            self.target_temp     = state.target_temp
            logger.info("[%s] State restored from DB (temp=%.1f, hvac=%s)",
                        self.id, self.temp, self.hvac_mode)
        else:
            self.temp        = 22.0
            self.humidity    = 50.0
            self.hvac_mode   = "OFF"
            self.target_temp = 22.0

        self.occupancy       = False
        self.light_level     = UNOCCUPIED_LIGHT_DEFAULT
        self.lighting_dimmer = 0

        self.fault_active    = False
        self.fault_type: Optional[str] = None
        self._frozen_temp: Optional[float] = None

        self.desired = DesiredState()
        self._pending_sync = False
        self._ota_version: Optional[str] = None

        logger.debug("[%s] Room initialised at path %s", self.id, self.mqtt_path)

    def _hvac_power(self) -> float:  
        return {"ON": 1.0, "ECO": 0.5, "OFF": 0.0}.get(self.hvac_mode, 0.0)

    def _outside_temp(self, sim_clock: float) -> float:  # Calculates the outside temperature based on time of day (sine wave cycle) 
        hour_of_day = (sim_clock % 86400) / 86400
        import math
        phase = math.sin(2 * math.pi * (hour_of_day - 0.25))
        midpoint = (DAY_OUTSIDE_TEMP + NIGHT_OUTSIDE_TEMP) / 2
        amplitude = (DAY_OUTSIDE_TEMP - NIGHT_OUTSIDE_TEMP) / 2
        return midpoint + amplitude * phase

    def apply_physics(self, sim_clock: float) -> None:   #  update room temperature using newton's law of cooling
        t_outside = self._outside_temp(sim_clock)
        leakage   = self.alpha * (t_outside - self.temp)
        hvac_delta = self.beta * self._hvac_power()

        occupancy_boost = OCCUPIED_TEMP_BOOST if self.occupancy else 0.0

        self.temp = self.temp + leakage + hvac_delta + occupancy_boost

    def apply_environmental_correlations(self) -> None: # Keeps sensor readings physically consistent with each other
        if self.occupancy:
            if self.light_level < OCCUPIED_LIGHT_THRESHOLD:
                self.light_level = min(
                    self.light_level + 50,
                    OCCUPIED_LIGHT_DEFAULT
                )
        else:
            if self.light_level > UNOCCUPIED_LIGHT_DEFAULT:
                self.light_level = max(
                    self.light_level - 30,
                    UNOCCUPIED_LIGHT_DEFAULT
                )

        self.lighting_dimmer = int(self.light_level / 10)

        if self.temp > 30:
            self.humidity = max(self.humidity - 0.2, 30.0)
        elif self.temp < 20:
            self.humidity = min(self.humidity + 0.1, 80.0)

    def set_occupancy(self, occupied: bool) -> None:
        self.occupancy = occupied
        logger.debug("[%s] Occupancy → %s", self.id, occupied)

    def set_hvac(self, mode: str) -> None:
        if mode not in VALID_HVAC_MODES:
            logger.warning("[%s] Invalid HVAC mode '%s' — ignoring", self.id, mode)
            return
        self.hvac_mode = mode
        logger.debug("[%s] HVAC mode → %s", self.id, mode)

    def set_target_temp(self, target: float) -> None:
        self.target_temp = float(target)

    def validate_state(self) -> None:  # clamp all sensor values to their allowed ranges
        original_temp = self.temp
        lo, hi = SPEC["temperature"]
        self.temp = max(lo, min(hi, self.temp))
        if self.temp != original_temp:
            logger.warning("[%s] temp clamped %.2f → %.2f", self.id, original_temp, self.temp)

        lo, hi = SPEC["humidity"]
        self.humidity = max(lo, min(hi, self.humidity))

        lo, hi = SPEC["light_level"]
        self.light_level = max(lo, min(hi, int(self.light_level)))

        lo, hi = SPEC["lighting_dimmer"]
        self.lighting_dimmer = max(lo, min(hi, int(self.lighting_dimmer)))

        if self.hvac_mode not in VALID_HVAC_MODES:
            logger.error("[%s] Invalid hvac_mode '%s', resetting to OFF", self.id, self.hvac_mode)
            self.hvac_mode = "OFF"

    def telemetry_payload(self) -> dict:
        return {
            "metadata": {
                "sensor_id": self.id,
                "building":  self.building_id,
                "floor":     int(self.floor_id[1:]),
                "room":      int(self.room_id_num[1:]),
                "timestamp": int(time.time()),
            },
            "sensors": {
                "temperature": round(self.temp, 2),
                "humidity":    round(self.humidity, 2),
                "occupancy":   self.occupancy,
                "light_level": self.light_level,
            },
            "actuators": {
                "hvac_mode":       self.hvac_mode,
                "lighting_dimmer": self.lighting_dimmer,
            },
        }

    def heartbeat_payload(self) -> dict:
        return {
            "sensor_id": self.id,
            "status":    "healthy",
            "timestamp": int(time.time()),
        }

    def to_state(self) -> RoomState:  # snapshot of current state for saving to db
        return RoomState(
            room_id       = self.id,
            last_temp     = round(self.temp, 4),
            last_humidity = round(self.humidity, 4),
            hvac_mode     = self.hvac_mode,
            target_temp   = self.target_temp,
            last_update   = int(time.time()),
            occupancy     = self.occupancy,
            light_level   = self.light_level,
        )

    def __repr__(self) -> str:
        return (
            f"<Room {self.id} | T={self.temp:.1f}°C "
            f"H={self.humidity:.1f}% "
            f"Occ={self.occupancy} "
            f"HVAC={self.hvac_mode}>"
        )