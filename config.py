"""
config.py — Configuration loader for Campus Pulse.

Priority (highest → lowest):
  1. Environment variables (or .env file)
  2. settings.yml
  3. Hard-coded defaults

Usage:
    from config import cfg
    print(cfg.tick_interval)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

import yaml

logger = logging.getLogger(__name__)

# ─────────────────────────────── helpers ──────────────────────────────────────

def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    return float(val) if val is not None else default

def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    return int(val) if val is not None else default

def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)

def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes")


# ─────────────────────────── dataclasses ──────────────────────────────────────

@dataclass
class SimulationConfig:
    building_id:      str   = "b01"
    num_floors:       int   = 10
    rooms_per_floor:  int   = 20
    tick_interval:    float = 5.0
    max_jitter:       float = 5.0
    heartbeat_every:  int   = 5


@dataclass
class ThermalConfig:
    alpha: float = 0.01
    beta:  float = 0.20


@dataclass
class DatabaseConfig:
    path:            str = "campus_pulse.db"
    sync_interval:   int = 30
    dirty_threshold: int = 10


@dataclass
class FaultTypeConfig:
    weight:      int   = 1
    max_drift:   float = 2.0    # sensor_drift only
    max_delay:   float = 20.0   # telemetry_delay only
    max_silence: float = 60.0   # node_dropout only


@dataclass
class FaultsConfig:
    enabled:              bool  = True
    probability:          float = 0.02
    recovery_probability: float = 0.10
    types: Dict[str, FaultTypeConfig] = field(default_factory=lambda: {
        "sensor_drift":     FaultTypeConfig(weight=3, max_drift=2.0),
        "frozen_sensor":    FaultTypeConfig(weight=2),
        "telemetry_delay":  FaultTypeConfig(weight=2, max_delay=20.0),
        "node_dropout":     FaultTypeConfig(weight=3, max_silence=60.0),
    })


@dataclass
class MQTTConfig:
    broker_host: str = "localhost"
    broker_port: int = 1883
    client_id:   str = "campus_engine"
    qos:         int = 1


@dataclass
class AppConfig:
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    thermal:    ThermalConfig    = field(default_factory=ThermalConfig)
    database:   DatabaseConfig   = field(default_factory=DatabaseConfig)
    faults:     FaultsConfig     = field(default_factory=FaultsConfig)
    mqtt:       MQTTConfig       = field(default_factory=MQTTConfig)


# ─────────────────────────────── loader ───────────────────────────────────────

def _load_yaml(yaml_path: Path) -> dict:
    """Load a YAML file, returning empty dict on failure."""
    if not yaml_path.exists():
        logger.warning("Config file '%s' not found — using defaults.", yaml_path)
        return {}
    try:
        with yaml_path.open() as fh:
            data = yaml.safe_load(fh) or {}
        logger.info("Loaded configuration from '%s'", yaml_path)
        return data
    except Exception as exc:
        logger.error("Failed to parse '%s': %s — using defaults.", yaml_path, exc)
        return {}


def _load_dotenv() -> None:
    """Best-effort .env loading (no hard dependency on python-dotenv)."""
    env_path = Path(".env")
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv          # type: ignore
        load_dotenv(dotenv_path=env_path, override=False)
        logger.info("Loaded environment overrides from .env")
    except ImportError:
        # Parse manually if python-dotenv is not installed
        with env_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
        logger.info("Parsed .env manually (python-dotenv not installed)")


def _parse_fault_types(raw: dict) -> Dict[str, FaultTypeConfig]:
    defaults = FaultsConfig().types
    result: Dict[str, FaultTypeConfig] = {}
    for name, default_ft in defaults.items():
        raw_ft = raw.get(name, {})
        result[name] = FaultTypeConfig(
            weight=      raw_ft.get("weight",      default_ft.weight),
            max_drift=   raw_ft.get("max_drift",   default_ft.max_drift),
            max_delay=   raw_ft.get("max_delay",   default_ft.max_delay),
            max_silence= raw_ft.get("max_silence", default_ft.max_silence),
        )
    return result


def load_config(yaml_path: str = "settings.yml") -> AppConfig:
    """
    Build the final AppConfig by merging YAML with environment overrides.
    Call once at startup; import the module-level `cfg` singleton instead.
    """
    _load_dotenv()
    raw = _load_yaml(Path(yaml_path))

    sim_raw = raw.get("simulation", {})
    sim = SimulationConfig(
        building_id=     _env_str("BUILDING_ID",      sim_raw.get("building_id",     "b01")),
        num_floors=      _env_int("NUM_FLOORS",        sim_raw.get("num_floors",      10)),
        rooms_per_floor= _env_int("ROOMS_PER_FLOOR",   sim_raw.get("rooms_per_floor", 20)),
        tick_interval=   _env_float("TICK_INTERVAL",   sim_raw.get("tick_interval",   5.0)),
        max_jitter=      _env_float("MAX_JITTER",      sim_raw.get("max_jitter",      5.0)),
        heartbeat_every= _env_int("HEARTBEAT_EVERY",   sim_raw.get("heartbeat_every", 5)),
    )

    therm_raw = raw.get("thermal", {})
    thermal = ThermalConfig(
        alpha= _env_float("THERMAL_ALPHA", therm_raw.get("alpha", 0.01)),
        beta=  _env_float("THERMAL_BETA",  therm_raw.get("beta",  0.20)),
    )

    db_raw = raw.get("database", {})
    database = DatabaseConfig(
        path=            _env_str("DB_PATH",           db_raw.get("path",            "campus_pulse.db")),
        sync_interval=   _env_int("DB_SYNC_INTERVAL",  db_raw.get("sync_interval",   30)),
        dirty_threshold= _env_int("DB_DIRTY_THRESHOLD",db_raw.get("dirty_threshold", 10)),
    )

    fault_raw = raw.get("faults", {})
    faults = FaultsConfig(
        enabled=              _env_bool("FAULTS_ENABLED",       fault_raw.get("enabled",              True)),
        probability=          _env_float("FAULT_PROBABILITY",   fault_raw.get("probability",          0.02)),
        recovery_probability= _env_float("FAULT_RECOVERY_PROB", fault_raw.get("recovery_probability", 0.10)),
        types=                _parse_fault_types(fault_raw.get("types", {})),
    )

    mqtt_raw = raw.get("mqtt", {})
    mqtt = MQTTConfig(
        broker_host= _env_str("MQTT_BROKER_HOST", mqtt_raw.get("broker_host", "localhost")),
        broker_port= _env_int("MQTT_BROKER_PORT", mqtt_raw.get("broker_port", 1883)),
        client_id=   _env_str("MQTT_CLIENT_ID",   mqtt_raw.get("client_id",   "campus_engine")),
        qos=         _env_int("MQTT_QOS",          mqtt_raw.get("qos",         1)),
    )

    config = AppConfig(
        simulation=sim,
        thermal=thermal,
        database=database,
        faults=faults,
        mqtt=mqtt,
    )

    total_rooms = config.simulation.num_floors * config.simulation.rooms_per_floor
    logger.info(
        "Config loaded: %d rooms (%d floors × %d/floor) | tick=%.1fs | fault_prob=%.2f | db_sync=%ds",
        total_rooms,
        config.simulation.num_floors,
        config.simulation.rooms_per_floor,
        config.simulation.tick_interval,
        config.faults.probability,
        config.database.sync_interval,
    )
    return config


# ─────────────────────────── module singleton ─────────────────────────────────

cfg: AppConfig = load_config()
