from __future__ import annotations
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import List

_PHASE1_DIR = Path(__file__).resolve().parent.parent
if str(_PHASE1_DIR) not in sys.path:
    sys.path.insert(0, str(_PHASE1_DIR))

from room import Room         
from dedup import DedupHandler
from nodes.mqtt_node import MQTTNode
from nodes.coap_node import CoAPNode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("world_engine")

BUILDING_ID:      str = "b01"
NUM_FLOORS:       int = 10
MQTT_ROOMS_START: int = 1       
MQTT_ROOMS_END:   int = 10
COAP_ROOMS_START: int = 11      
COAP_ROOMS_END:   int = 20

MQTT_BROKER_HOST: str = "localhost"   
MQTT_BROKER_PORT: int = 1883

THERMAL_ALPHA: float = 0.01
THERMAL_BETA:  float = 0.20


def build_mqtt_fleet(
    sim_start: float,
    dedup: DedupHandler,
) -> List[MQTTNode]:
    """
    Construct 100 MQTT nodes (rooms 1-10, floors 1-10).
    Each node wraps a Phase 1 Room for physics simulation.
    """
    nodes: List[MQTTNode] = []
    for floor in range(1, NUM_FLOORS + 1):
        for room_num in range(MQTT_ROOMS_START, MQTT_ROOMS_END + 1):
            room = Room(
                building=BUILDING_ID,
                floor=floor,
                room_num=room_num,
                alpha=THERMAL_ALPHA,
                beta=THERMAL_BETA,
            )
            node = MQTTNode(
                room=room,
                broker_host=MQTT_BROKER_HOST,
                broker_port=MQTT_BROKER_PORT,
                dedup=dedup,
                sim_start=sim_start,
            )
            nodes.append(node)
    logger.info("MQTT fleet built: %d nodes", len(nodes))
    return nodes


def build_coap_fleet(
    sim_start: float,
    dedup: DedupHandler,
) -> List[CoAPNode]:
    """
    Construct 100 CoAP nodes (rooms 11-20, floors 1-10).
    Each node wraps a Phase 1 Room for physics simulation.
    """
    nodes: List[CoAPNode] = []
    for floor in range(1, NUM_FLOORS + 1):
        for room_num in range(COAP_ROOMS_START, COAP_ROOMS_END + 1):
            room = Room(
                building=BUILDING_ID,
                floor=floor,
                room_num=room_num,
                alpha=THERMAL_ALPHA,
                beta=THERMAL_BETA,
            )
            node = CoAPNode(
                room=room,
                dedup=dedup,
                sim_start=sim_start,
            )
            nodes.append(node)
    logger.info("CoAP fleet built: %d nodes", len(nodes))
    return nodes

async def run_world_engine() -> None:
    """
    Main coroutine.
    Instantiates all 200 nodes and runs them concurrently.
    """
    sim_start = time.time()
    dedup     = DedupHandler()

    logger.info("  Campus Pulse Phase 2 — Hybrid World Engine   ")
    logger.info("  epoch=%d                                      ", int(sim_start))

    mqtt_nodes = build_mqtt_fleet(sim_start, dedup)
    coap_nodes = build_coap_fleet(sim_start, dedup)

    total = len(mqtt_nodes) + len(coap_nodes)
    logger.info(
        "Fleet ready: %d MQTT + %d CoAP = %d total nodes",
        len(mqtt_nodes), len(coap_nodes), total,
    )

    async def _dedup_reporter():
        while True:
            await asyncio.sleep(60)
            stats = dedup.stats()
            logger.info(
                "[DEDUP] Stats: mqtt_ok=%d mqtt_dup=%d coap_ok=%d coap_dup=%d dup_rate=%.2f%%",
                stats["mqtt_ok"], stats["mqtt_dup"],
                stats["coap_ok"], stats["coap_dup"],
                stats["dup_rate_pct"],
            )

    mqtt_tasks = [asyncio.create_task(n.run(), name=n.node_id)   for n in mqtt_nodes]
    coap_tasks = [asyncio.create_task(n.run(), name=n.node_id)   for n in coap_nodes]
    util_tasks = [asyncio.create_task(_dedup_reporter(), name="dedup_reporter")]

    all_tasks = mqtt_tasks + coap_tasks + util_tasks
    logger.info("Launched %d tasks (100 MQTT + 100 CoAP + 1 dedup reporter)", len(all_tasks))

    try:
        await asyncio.gather(*all_tasks, return_exceptions=True)
    except asyncio.CancelledError:
        logger.info("World engine cancelled — shutting down …")
    finally:
        logger.info("  Campus Pulse Phase 2 — Engine stopped        ")
        stats = dedup.stats()
        logger.info(
            "  Final dedup stats: total=%d dup_rate=%.2f%%",
            stats["total_messages"], stats["dup_rate_pct"],
        )
