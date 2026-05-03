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
            self.occupancy   = state.occupancy 
            self.light_level = state.light_level 
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
    

    def receive_desired_state(self, desired_payload: dict) -> None:
        if "hvac_mode" in desired_payload:
            mode = desired_payload["hvac_mode"]
            if mode in VALID_HVAC_MODES:
                self.desired.hvac_mode = mode
            else:
                logger.warning("[%s] Desired HVAC mode '%s' invalid — ignoring", self.id, mode)
 
        if "target_temp" in desired_payload:
            self.desired.target_temp = float(desired_payload["target_temp"])
 
        if "lighting_dimmer" in desired_payload:
            lo, hi = SPEC["lighting_dimmer"]
            val = int(desired_payload["lighting_dimmer"])
            self.desired.lighting_dimmer = max(lo, min(hi, val))
 
        if "occupancy" in desired_payload:
            self.desired.occupancy = bool(desired_payload["occupancy"])
 
        self._pending_sync = True
        logger.info("[%s] Desired state received: %s", self.id, desired_payload)
 
    def apply_desired_state(self) -> bool:

        if not self._pending_sync:
            return False
 
        changed = False
 
        if self.desired.hvac_mode is not None:
            if self.hvac_mode != self.desired.hvac_mode:
                logger.info("[%s] Shadow sync: HVAC %s → %s",
                            self.id, self.hvac_mode, self.desired.hvac_mode)
                self.hvac_mode = self.desired.hvac_mode
                changed = True
 
        if self.desired.target_temp is not None:
            if self.target_temp != self.desired.target_temp:
                logger.info("[%s] Shadow sync: target_temp %.1f → %.1f",
                            self.id, self.target_temp, self.desired.target_temp)
                self.target_temp = self.desired.target_temp
                changed = True
 
        if self.desired.lighting_dimmer is not None:
            if self.lighting_dimmer != self.desired.lighting_dimmer:
                self.lighting_dimmer = self.desired.lighting_dimmer
                self.light_level     = self.lighting_dimmer * 10   # keep in sync
                changed = True
 
        if self.desired.occupancy is not None:
            if self.occupancy != self.desired.occupancy:
                logger.info("[%s] Shadow sync: occupancy %s → %s",
                            self.id, self.occupancy, self.desired.occupancy)
                self.occupancy = self.desired.occupancy
                changed = True
 
        # Clear pending flag once reconciled
        self._pending_sync = False
        return changed
 
    @property
    def is_in_sync(self) -> bool:
        if self.desired.hvac_mode is not None and self.hvac_mode != self.desired.hvac_mode:
            return False
        if self.desired.target_temp is not None and self.target_temp != self.desired.target_temp:
            return False
        if self.desired.lighting_dimmer is not None and self.lighting_dimmer != self.desired.lighting_dimmer:
            return False
        if self.desired.occupancy is not None and self.occupancy != self.desired.occupancy:
            return False
        return True
 

    def apply_ota_config(self, config_payload: dict) -> list[str]:
        changes = []
 
        version = config_payload.get("version")
        if version:
            self._ota_version = str(version)
 
        if "alpha" in config_payload:
            new_alpha = float(config_payload["alpha"])
            if 0 < new_alpha < 1:           # sanity check
                if new_alpha != self.alpha:
                    logger.info("[%s] OTA: alpha %.4f → %.4f", self.id, self.alpha, new_alpha)
                    self.alpha = new_alpha
                    changes.append(f"alpha={new_alpha}")
            else:
                logger.warning("[%s] OTA: alpha %.4f out of range (0-1) — ignored", self.id, new_alpha)
 
        if "beta" in config_payload:
            new_beta = float(config_payload["beta"])
            if 0 < new_beta <= 2.0:         # sanity check
                if new_beta != self.beta:
                    logger.info("[%s] OTA: beta %.4f → %.4f", self.id, self.beta, new_beta)
                    self.beta = new_beta
                    changes.append(f"beta={new_beta}")
            else:
                logger.warning("[%s] OTA: beta %.4f out of range (0-2) — ignored", self.id, new_beta)
 
        if "tick_interval" in config_payload:
            changes.append(f"tick_interval={config_payload['tick_interval']}")
 
        if "fault_prob" in config_payload:
            changes.append(f"fault_prob={config_payload['fault_prob']}")
 
        if changes:
            logger.info("[%s] OTA config applied (version=%s): %s",
                        self.id, self._ota_version, ", ".join(changes))
        else:
            logger.debug("[%s] OTA config received but no changes applied.", self.id)
 
        return changes
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
    
    def shadow_payload(self) -> dict:
        return {
            "desired": {
                "hvac_mode":       self.desired.hvac_mode,
                "target_temp":     self.desired.target_temp,
                "lighting_dimmer": self.desired.lighting_dimmer,
                "occupancy":       self.desired.occupancy,
            },
            "reported": {
                "hvac_mode":       self.hvac_mode,
                "target_temp":     self.target_temp,
                "temperature":     round(self.temp, 2),
                "humidity":        round(self.humidity, 2),
                "occupancy":       self.occupancy,
                "light_level":     self.light_level,
                "lighting_dimmer": self.lighting_dimmer,
            },
            "in_sync":   self.is_in_sync,
            "sensor_id": self.id,
            "timestamp": int(time.time()),
        }
 
    def ota_ack_payload(self, changes: list[str]) -> dict:
        return {
            "sensor_id":   self.id,
            "ota_version": self._ota_version,
            "status":      "applied" if changes else "no_change",
            "changes":     changes,
            "alpha":       self.alpha,
            "beta":        self.beta,
            "timestamp":   int(time.time()),
        }
    
    def heartbeat_payload(self) -> dict:
        return {
            "sensor_id": self.id,
            "status":    "healthy",
            "in_sync": self.is_in_sync,
            "ota_ver": self._ota_version,
            "timestamp": int(time.time()),
        }

    def to_state(self) -> RoomState:  
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
        sync = "✓" if self.is_in_sync else "⟳"
        return (
            f"<Room {self.id} | T={self.temp:.1f}°C "
            f"H={self.humidity:.1f}% "
            f"Occ={self.occupancy} "
            f"HVAC={self.hvac_mode}"
            f"sync={sync}>"  
        )